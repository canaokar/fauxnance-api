"""SQS worker for scheduled and historical US EOD ingestion."""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Protocol

from src.adapters.alpha_vantage import AlphaVantageAdapter
from src.adapters.coingecko import CoinGeckoAdapter
from src.adapters.frankfurter import FrankfurterAdapter
from src.adapters.market_eod import MarketEodRouter
from src.adapters.yahoo import YahooAdapter
from src.ingest.messages import BackfillYearMessage, EodBatchMessage, parse_message
from src.ingest.repository import DynamoDBIngestRepository
from src.ingest.validation import validate_actions, validate_candles
from src.shared.market_data import EodResult
from src.shared.source_guard import ALPHA_VANTAGE_DAILY_LIMIT, DynamoDbSourceGuard


_LOGGER = logging.getLogger(__name__)


class TerminalReconciliation(RuntimeError):
    """Marks a receive reserved for terminal-state persistence, not upstream I/O."""


class EodSource(Protocol):
    def get_eod(
        self,
        symbol: str,
        start: Any,
        end: Any,
        *,
        market: str,
        purpose: str,
        adapter_hints: Mapping[str, Any] | None = None,
    ) -> EodResult: ...


class IngestWorker:
    def __init__(self, source: EodSource, repository: DynamoDBIngestRepository) -> None:
        self._source = source
        self._repository = repository

    def process(self, body: str) -> None:
        message = parse_message(body)
        result = self._source.get_eod(
            message.symbol,
            message.start,
            message.end,
            market=message.market,
            purpose="backfill" if isinstance(message, BackfillYearMessage) else "scheduled",
            adapter_hints=self._repository.get_adapter_hints(message.symbol),
        )
        candles = validate_candles(
            result.candles, start=message.start, end=message.end
        )
        actions = validate_actions(
            result.actions, start=message.start, end=message.end
        )

        if candles:
            self._repository.write_candles(message.symbol, candles)
            self._repository.advance_symbol_coverage(
                message.symbol, candles[0].date, candles[-1].date
            )
            self._repository.advance_market_status(message.market, candles[-1].date)
        if actions:
            self._repository.write_actions(message.symbol, actions)

        if isinstance(message, BackfillYearMessage):
            self._repository.complete_backfill_work(
                message.job_id, message.symbol, message.year
            )
        elif not isinstance(message, EodBatchMessage):  # pragma: no cover
            raise TypeError("unsupported parsed ingest message")

    def record_terminal_failure(self, body: str, error: Exception) -> bool:
        """Persist the terminal state for a backfill unit before SQS redrives it."""

        message = parse_message(body)
        if not isinstance(message, BackfillYearMessage):
            return False
        return self._repository.fail_backfill_work(
            message.job_id,
            message.symbol,
            message.year,
            failure=type(error).__name__,
        )


_worker: IngestWorker | None = None


def handler(
    event: Mapping[str, Any],
    _context: object,
    *,
    worker: IngestWorker | None = None,
    max_receive_count: int | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Process SQS records and report only the records that should retry."""

    active_worker = worker or _default_worker()
    processing_receive_limit = max_receive_count or int(
        os.environ.get("MAX_PROCESSING_RECEIVES", "5")
    )
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        identifier = str(record.get("messageId", ""))
        receive_count = _receive_count(record)
        body = record.get("body")
        if receive_count > processing_receive_limit:
            try:
                if not isinstance(body, str):
                    raise ValueError("SQS record body must be a string")
                active_worker.record_terminal_failure(
                    body, TerminalReconciliation("terminal reconciliation")
                )
            except Exception:
                _LOGGER.exception(
                    "Terminal ingest reconciliation failed: messageId=%s",
                    identifier,
                )
            failures.append({"itemIdentifier": identifier})
            continue
        try:
            if not isinstance(body, str):
                raise ValueError("SQS record body must be a string")
            active_worker.process(body)
        except Exception as exc:
            _LOGGER.exception(
                "Ingest record failed: messageId=%s",
                identifier,
            )
            if receive_count >= processing_receive_limit and isinstance(body, str):
                try:
                    active_worker.record_terminal_failure(body, exc)
                except Exception:
                    _LOGGER.exception(
                        "Terminal ingest failure could not be recorded: messageId=%s",
                        identifier,
                    )
            failures.append({"itemIdentifier": identifier})
    return {"batchItemFailures": failures}


lambda_handler = handler


def _receive_count(record: Mapping[str, Any]) -> int:
    attributes = record.get("attributes")
    value = attributes.get("ApproximateReceiveCount") if isinstance(attributes, Mapping) else None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 1
    return max(count, 1)


def _default_worker() -> IngestWorker:
    global _worker
    if _worker is not None:
        return _worker

    import boto3

    dynamodb = boto3.resource("dynamodb")
    data_table = dynamodb.Table(os.environ["DATA_TABLE"])
    control_table = dynamodb.Table(os.environ["CONTROL_TABLE"])
    yahoo = YahooAdapter()
    yahoo_guard = DynamoDbSourceGuard(control_table, "yahoo")

    alpha = None
    alpha_guard = None
    parameter_name = os.environ.get("ALPHA_VANTAGE_API_KEY_PARAMETER", "").strip()
    if parameter_name:
        ssm = boto3.client("ssm")
        api_key = _read_parameter(ssm, parameter_name)
        if api_key is not None:
            alpha = AlphaVantageAdapter(api_key)
            alpha_guard = DynamoDbSourceGuard(
                control_table,
                "alpha_vantage",
                daily_limit=ALPHA_VANTAGE_DAILY_LIMIT,
            )

    frankfurter = FrankfurterAdapter()
    frankfurter_guard = DynamoDbSourceGuard(control_table, "frankfurter")

    coingecko = None
    coingecko_guard = None
    coin_parameter = os.environ.get("COINGECKO_API_KEY_PARAMETER", "").strip()
    if coin_parameter:
        ssm = boto3.client("ssm")
        coin_key = _read_parameter(ssm, coin_parameter)
        if coin_key is not None:
            coingecko = CoinGeckoAdapter(coin_key)
            coingecko_guard = DynamoDbSourceGuard(control_table, "coingecko")

    source = MarketEodRouter(
        yahoo,
        yahoo_guard,
        alpha_vantage=alpha,
        alpha_vantage_guard=alpha_guard,
        frankfurter=frankfurter,
        frankfurter_guard=frankfurter_guard,
        coingecko=coingecko,
        coingecko_guard=coingecko_guard,
    )
    _worker = IngestWorker(
        source,
        DynamoDBIngestRepository(data_table, control_table),
    )
    return _worker


def _read_parameter(ssm: Any, name: str) -> str | None:
    try:
        response = ssm.get_parameter(Name=name, WithDecryption=True)
    except Exception as exc:
        response_data = getattr(exc, "response", {})
        if response_data.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise
    value = str(response.get("Parameter", {}).get("Value", "")).strip()
    if not value:
        raise ValueError(f"SSM parameter {name!r} is empty")
    return value

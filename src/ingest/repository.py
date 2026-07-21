"""DynamoDB writes shared by manual and queued EOD ingestion."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
import random
import time
from typing import Any, Callable, Mapping, Sequence

from src.shared.market_data import Candle


class DynamoDBIngestRepository:
    def __init__(
        self,
        data_table: Any,
        control_table: Any | None = None,
        *,
        transaction_client: Any | None = None,
        max_merge_attempts: int = 5,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self._data = data_table
        self._control = control_table
        self._transaction_client = transaction_client
        self._max_merge_attempts = max_merge_attempts
        self._pause = pause

    def write_candles(self, symbol: str, candles: Sequence[Candle]) -> None:
        chunks: dict[str, list[Candle]] = defaultdict(list)
        for candle in candles:
            chunks[candle.date.strftime("%Y-%m")].append(candle)
        for month, month_candles in sorted(chunks.items()):
            self._merge_chunk(symbol, month, month_candles)

    def write_symbol(
        self,
        metadata: Mapping[str, str],
        *,
        market: str,
        first_date: date,
        last_date: date,
    ) -> None:
        """Seed a registry item without allowing reruns to regress coverage."""

        symbol = metadata["symbol"]
        common = {
            "symbol": symbol,
            "name": metadata["name"],
            "type": metadata["type"],
            "exchange": metadata["exchange"],
            "currency": metadata["currency"],
            "market": market,
            "active": True,
        }
        try:
            self._data.put_item(
                Item={
                    "PK": f"SYM#{symbol}",
                    "SK": "META",
                    **common,
                    "coverage": {
                        "eodFrom": first_date.isoformat(),
                        "eodTo": last_date.isoformat(),
                    },
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise
            self._refresh_symbol_metadata(common)
            self.advance_symbol_coverage(symbol, first_date, last_date)

        self._data.put_item(Item={"PK": "SYMBOLS", "SK": symbol, **common})

    def seed_symbol(self, metadata: Mapping[str, str], *, market: str) -> None:
        """Register a symbol before its first candle without inventing coverage."""

        common = _symbol_metadata(metadata, market)
        symbol = common["symbol"]
        try:
            self._data.put_item(
                Item={
                    "PK": f"SYM#{symbol}",
                    "SK": "META",
                    **common,
                    "coverage": {},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise
            self._refresh_symbol_metadata(common)
        self._data.put_item(Item={"PK": "SYMBOLS", "SK": symbol, **common})

    def advance_symbol_coverage(
        self, symbol: str, first_date: date, last_date: date
    ) -> None:
        key = {"PK": f"SYM#{symbol}", "SK": "META"}
        updates = (
            ("eodFrom", first_date.isoformat(), ">"),
            ("eodTo", last_date.isoformat(), "<"),
        )
        for field, value, comparison in updates:
            try:
                self._advance_coverage_field(key, field, value, comparison)
            except Exception as exc:
                if _is_missing_document_path(exc):
                    self._ensure_coverage_map(key)
                    try:
                        self._advance_coverage_field(key, field, value, comparison)
                    except Exception as retry_exc:
                        if not _is_conditional_failure(retry_exc):
                            raise
                elif not _is_conditional_failure(exc):
                    raise

    def _advance_coverage_field(
        self,
        key: Mapping[str, str],
        field: str,
        value: str,
        comparison: str,
    ) -> None:
        self._data.update_item(
            Key=key,
            UpdateExpression="SET #coverage.#field = :value",
            ConditionExpression=(
                "attribute_exists(PK) AND "
                f"(attribute_not_exists(#coverage.#field) OR "
                f"#coverage.#field {comparison} :value)"
            ),
            ExpressionAttributeNames={
                "#coverage": "coverage",
                "#field": field,
            },
            ExpressionAttributeValues={":value": value},
        )

    def _ensure_coverage_map(self, key: Mapping[str, str]) -> None:
        self._data.update_item(
            Key=key,
            UpdateExpression=(
                "SET #coverage = if_not_exists(#coverage, :emptyCoverage)"
            ),
            ExpressionAttributeNames={"#coverage": "coverage"},
            ExpressionAttributeValues={":emptyCoverage": {}},
        )

    def advance_market_status(
        self, market: str, latest_eod: date, *, now: datetime | None = None
    ) -> None:
        try:
            self._data.update_item(
                Key={"PK": f"MARKET#{market}", "SK": "STATUS"},
                UpdateExpression="SET latestEod = :latest, updatedAt = :updated",
                ConditionExpression=(
                    "attribute_not_exists(latestEod) OR latestEod < :latest"
                ),
                ExpressionAttributeValues={
                    ":latest": latest_eod.isoformat(),
                    ":updated": _utc_timestamp(now),
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def complete_backfill_work(
        self,
        job_id: str,
        symbol: str,
        year: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Atomically complete one work item and increment its job exactly once."""

        if self._control is None:
            raise RuntimeError("control table is required for backfill completion")
        client = self._transaction_client or self._control.meta.client
        timestamp = _utc_timestamp(now)
        work_key = {"PK": f"JOB#{job_id}", "SK": f"WORK#{symbol}#YEAR#{year}"}
        job_key = {"PK": f"JOB#{job_id}", "SK": "META"}
        for attempt in range(self._max_merge_attempts):
            try:
                client.transact_write_items(
                    TransactItems=[
                    {
                        "Update": {
                            "TableName": self._control.name,
                            "Key": work_key,
                            "UpdateExpression": (
                                "SET #state = :completed, completedAt = :now"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(PK) AND "
                                "(attribute_not_exists(#state) OR "
                                "#state <> :completed)"
                            ),
                            "ExpressionAttributeNames": {"#state": "state"},
                            "ExpressionAttributeValues": {
                                ":completed": "completed",
                                ":now": timestamp,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._control.name,
                            "Key": job_key,
                            "UpdateExpression": (
                                "SET updatedAt = :now ADD #completed :one"
                            ),
                            "ConditionExpression": "attribute_exists(PK)",
                            "ExpressionAttributeNames": {"#completed": "completed"},
                            "ExpressionAttributeValues": {
                                ":now": timestamp,
                                ":one": 1,
                            },
                        }
                    },
                    ]
                )
                self._complete_job_if_done(job_id, timestamp)
                return True
            except Exception as exc:
                if not _is_transaction_cancelled(exc):
                    raise
                item = self._control.get_item(
                    Key=work_key, ConsistentRead=True
                ).get("Item")
                if item and item.get("state") == "completed":
                    self._complete_job_if_done(job_id, timestamp)
                    return False
                if (
                    _is_transaction_conflict(exc)
                    and attempt + 1 < self._max_merge_attempts
                ):
                    self._pause(random.uniform(0.02, 0.1) * (attempt + 1))
                    continue
                raise
        raise RuntimeError("backfill completion attempts exhausted")  # pragma: no cover

    def _complete_job_if_done(self, job_id: str, timestamp: str) -> None:
        if self._control is None:  # pragma: no cover - guarded by caller
            return
        key = {"PK": f"JOB#{job_id}", "SK": "META"}
        item = self._control.get_item(Key=key, ConsistentRead=True).get("Item")
        if not item:
            return
        completed = int(item.get("completed", 0))
        total = int(item.get("total", 0))
        if total < 1 or completed < total:
            return
        try:
            self._control.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #state = :completedState, completedAt = :now, "
                    "updatedAt = :now"
                ),
                ConditionExpression=(
                    "#completed >= :total AND "
                    "(attribute_not_exists(#state) OR #state <> :completedState)"
                ),
                ExpressionAttributeNames={
                    "#state": "state",
                    "#completed": "completed",
                },
                ExpressionAttributeValues={
                    ":completedState": "completed",
                    ":total": total,
                    ":now": timestamp,
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def _merge_chunk(
        self, symbol: str, month: str, incoming: Sequence[Candle]
    ) -> None:
        key = {"PK": f"SYM#{symbol}", "SK": f"EOD#{month}"}
        for attempt in range(self._max_merge_attempts):
            existing = self._data.get_item(Key=key, ConsistentRead=True).get("Item")
            prior_candles = existing.get("candles", []) if existing else []
            by_date = {str(row["d"]): dict(row) for row in prior_candles}
            changed = False
            for candle in incoming:
                day = candle.date.isoformat()
                if day not in by_date:
                    by_date[day] = _candle_item(candle)
                    changed = True
            if existing and not changed:
                return

            revision = int(existing.get("revision", 0)) if existing else 0
            item = {
                **key,
                "revision": revision + 1,
                "candles": [by_date[day] for day in sorted(by_date)],
            }
            request: dict[str, Any] = {"Item": item}
            if existing:
                request.update(
                    {
                        "ConditionExpression": "revision = :expected",
                        "ExpressionAttributeValues": {":expected": revision},
                    }
                )
            else:
                request["ConditionExpression"] = "attribute_not_exists(PK)"
            try:
                self._data.put_item(**request)
                return
            except Exception as exc:
                if (
                    not _is_conditional_failure(exc)
                    or attempt + 1 == self._max_merge_attempts
                ):
                    raise
                self._pause(random.uniform(0.02, 0.1) * (attempt + 1))

    def _refresh_symbol_metadata(self, common: Mapping[str, Any]) -> None:
        self._data.update_item(
            Key={"PK": f"SYM#{common['symbol']}", "SK": "META"},
            UpdateExpression=(
                "SET #name = :name, #type = :type, exchange = :exchange, "
                "currency = :currency, market = :market, active = :active, "
                "#coverage = if_not_exists(#coverage, :emptyCoverage)"
            ),
            ExpressionAttributeNames={
                "#name": "name",
                "#type": "type",
                "#coverage": "coverage",
            },
            ExpressionAttributeValues={
                ":name": common["name"],
                ":type": common["type"],
                ":exchange": common["exchange"],
                ":currency": common["currency"],
                ":market": common["market"],
                ":active": True,
                ":emptyCoverage": {},
            },
        )


def _candle_item(candle: Candle) -> dict[str, Any]:
    return {
        "d": candle.date.isoformat(),
        "o": candle.open,
        "h": candle.high,
        "l": candle.low,
        "c": candle.close,
        "v": candle.volume,
        "src": candle.source,
    }


def _symbol_metadata(
    metadata: Mapping[str, str], market: str
) -> dict[str, Any]:
    return {
        "symbol": metadata["symbol"],
        "name": metadata["name"],
        "type": metadata["type"],
        "exchange": metadata["exchange"],
        "currency": metadata["currency"],
        "market": market,
        "active": True,
    }


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _is_missing_document_path(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    error = response.get("Error", {})
    return (
        error.get("Code") == "ValidationException"
        and "document path" in str(error.get("Message", "")).lower()
    )


def _is_transaction_cancelled(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "TransactionCanceledException"


def _is_transaction_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    codes = {
        reason.get("Code")
        for reason in response.get("CancellationReasons", [])
        if isinstance(reason, Mapping)
        and reason.get("Code") not in (None, "None")
    }
    return codes == {"TransactionConflict"}


def _utc_timestamp(now: datetime | None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

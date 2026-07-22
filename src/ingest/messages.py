"""Strict versioned queue messages consumed by the ingest worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Any

from src.shared.symbols import Market, SUPPORTED_MARKETS, parse_symbol


_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class InvalidIngestMessage(ValueError):
    """Raised when an SQS body does not match a supported message contract."""


@dataclass(frozen=True, slots=True)
class EodBatchMessage:
    symbol: str
    start: date
    end: date
    market: str = "US"


@dataclass(frozen=True, slots=True)
class BackfillYearMessage:
    job_id: str
    symbol: str
    year: int
    market: str = "US"
    range_start: date | None = None
    range_end: date | None = None

    @property
    def start(self) -> date:
        return self.range_start or date(self.year, 1, 1)

    @property
    def end(self) -> date:
        return self.range_end or date(self.year, 12, 31)


IngestMessage = EodBatchMessage | BackfillYearMessage


def parse_message(body: str) -> IngestMessage:
    try:
        document = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidIngestMessage("message body must be a JSON object") from exc
    if not isinstance(document, dict):
        raise InvalidIngestMessage("message body must be a JSON object")
    if type(document.get("v")) is not int or document["v"] not in {1, 2, 3}:
        raise InvalidIngestMessage("message version must be 1, 2, or 3")
    version = document["v"]

    kind = document.get("kind")
    if kind == "eod_batch":
        if version == 3:
            raise InvalidIngestMessage("version 3 is reserved for backfill_year")
        _require_exact_fields(
            document, {"v", "kind", "market", "symbol", "from", "to"}
        )
        market = _parse_market(document["market"])
        if version == 1 and market != Market.US:
            raise InvalidIngestMessage("version 1 eod_batch market must be US")
        symbol = _parse_symbol(document["symbol"], market)
        start = _parse_date(document["from"], "from")
        end = _parse_date(document["to"], "to")
        if start > end:
            raise InvalidIngestMessage("from must not be after to")
        return EodBatchMessage(
            symbol=symbol, start=start, end=end, market=market.value
        )

    if kind == "backfill_year":
        expected = {"v", "kind", "jobId", "symbol", "year"}
        if version >= 2:
            expected.add("market")
        if version == 3:
            expected.update({"from", "to"})
        _require_exact_fields(document, expected)
        job_id = document["jobId"]
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise InvalidIngestMessage("jobId is invalid")
        market = Market.US if version == 1 else _parse_market(document["market"])
        symbol = _parse_symbol(document["symbol"], market)
        year = document["year"]
        if type(year) is not int or not 1900 <= year <= 2100:
            raise InvalidIngestMessage("year must be an integer from 1900 to 2100")
        range_start = None
        range_end = None
        if version == 3:
            range_start = _parse_date(document["from"], "from")
            range_end = _parse_date(document["to"], "to")
            if range_start > range_end:
                raise InvalidIngestMessage("from must not be after to")
            if range_start.year != year or range_end.year != year:
                raise InvalidIngestMessage("backfill range must stay within year")
        return BackfillYearMessage(
            job_id=job_id,
            symbol=symbol,
            year=year,
            market=market.value,
            range_start=range_start,
            range_end=range_end,
        )

    raise InvalidIngestMessage("message kind is unsupported")


def _require_exact_fields(document: dict[str, Any], expected: set[str]) -> None:
    if set(document) != expected:
        raise InvalidIngestMessage("message fields do not match its kind")


def _parse_symbol(value: object, market: Market) -> str:
    if not isinstance(value, str):
        raise InvalidIngestMessage("symbol must be canonical")
    try:
        info = parse_symbol(value)
    except ValueError as exc:
        raise InvalidIngestMessage("symbol must be canonical") from exc
    if value != info.symbol or info.market != market:
        raise InvalidIngestMessage("symbol does not match its market")
    return info.symbol


def _parse_market(value: object) -> Market:
    if not isinstance(value, str) or value not in SUPPORTED_MARKETS:
        raise InvalidIngestMessage("market is unsupported")
    return Market(value)


def _parse_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise InvalidIngestMessage(f"{field} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidIngestMessage(f"{field} must be a YYYY-MM-DD date") from exc

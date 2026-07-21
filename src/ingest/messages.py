"""Strict versioned queue messages consumed by the ingest worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Any


_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
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

    @property
    def start(self) -> date:
        return date(self.year, 1, 1)

    @property
    def end(self) -> date:
        return date(self.year, 12, 31)


IngestMessage = EodBatchMessage | BackfillYearMessage


def parse_message(body: str) -> IngestMessage:
    try:
        document = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidIngestMessage("message body must be a JSON object") from exc
    if not isinstance(document, dict):
        raise InvalidIngestMessage("message body must be a JSON object")
    if type(document.get("v")) is not int or document["v"] != 1:
        raise InvalidIngestMessage("message version must be 1")

    kind = document.get("kind")
    if kind == "eod_batch":
        _require_exact_fields(
            document, {"v", "kind", "market", "symbol", "from", "to"}
        )
        market = document["market"]
        if market != "US":
            raise InvalidIngestMessage("eod_batch market must be US")
        symbol = _parse_symbol(document["symbol"])
        start = _parse_date(document["from"], "from")
        end = _parse_date(document["to"], "to")
        if start > end:
            raise InvalidIngestMessage("from must not be after to")
        return EodBatchMessage(symbol=symbol, start=start, end=end)

    if kind == "backfill_year":
        _require_exact_fields(document, {"v", "kind", "jobId", "symbol", "year"})
        job_id = document["jobId"]
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise InvalidIngestMessage("jobId is invalid")
        symbol = _parse_symbol(document["symbol"])
        year = document["year"]
        if type(year) is not int or not 1900 <= year <= 2100:
            raise InvalidIngestMessage("year must be an integer from 1900 to 2100")
        return BackfillYearMessage(job_id=job_id, symbol=symbol, year=year)

    raise InvalidIngestMessage("message kind is unsupported")


def _require_exact_fields(document: dict[str, Any], expected: set[str]) -> None:
    if set(document) != expected:
        raise InvalidIngestMessage("message fields do not match its kind")


def _parse_symbol(value: object) -> str:
    if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
        raise InvalidIngestMessage("symbol must be canonical US ticker")
    return value


def _parse_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise InvalidIngestMessage(f"{field} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidIngestMessage(f"{field} must be a YYYY-MM-DD date") from exc

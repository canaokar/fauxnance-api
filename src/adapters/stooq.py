"""Stooq CSV adapter for Phase 1 US end-of-day data."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import io
import re
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.shared.market_data import (
    Candle,
    Capability,
    CapabilityUnavailable,
    EodResult,
)


class StooqError(Exception):
    """Raised when Stooq returns an unusable response."""


Fetch = Callable[[str, float], bytes]
_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class StooqAdapter:
    name = "stooq"
    base_url = "https://stooq.com/q/d/l/"

    def __init__(self, *, fetch: Fetch | None = None, timeout: float = 10.0) -> None:
        self._fetch = fetch or _fetch
        self._timeout = timeout

    def capabilities(self) -> set[Capability]:
        return {Capability.EOD_US}

    def get_eod(self, symbol: str, start: date, end: date) -> EodResult:
        if start > end:
            raise ValueError("start must not be after end")

        params = urlencode(
            {
                "s": self.vendor_symbol(symbol),
                "d1": start.strftime("%Y%m%d"),
                "d2": end.strftime("%Y%m%d"),
                "i": "d",
            }
        )
        body = self._fetch(f"{self.base_url}?{params}", self._timeout)
        return EodResult(candles=_parse_csv(body, start=start, end=end))

    def get_quote(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Stooq quote support is not part of Phase 1")

    def discover(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Stooq discovery support is not part of Phase 1")

    @staticmethod
    def vendor_symbol(symbol: str) -> str:
        """Translate a canonical Phase 1 US symbol to Stooq's identifier."""

        canonical = symbol.strip().upper()
        if not _US_SYMBOL.fullmatch(canonical):
            raise CapabilityUnavailable("Stooq adapter only supports US symbols")
        # Stooq represents share classes with a dash and US listings with `.us`.
        return f"{canonical.replace('.', '-').lower()}.us"


def _fetch(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "text/csv",
            "User-Agent": "fauxnance-api/0.1 (educational market-data service)",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed host
        return response.read()


def _parse_csv(payload: bytes, *, start: date, end: date) -> list[Candle]:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StooqError("Stooq returned invalid text") from exc

    if not text.strip() or text.lstrip().lower().startswith("no data"):
        return []

    reader = csv.DictReader(io.StringIO(text))
    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise StooqError("Stooq CSV has an unexpected header")

    candles: list[Candle] = []
    try:
        for row in reader:
            candle_date = date.fromisoformat(row["Date"])
            if not start <= candle_date <= end:
                continue
            volume_text = row["Volume"].strip()
            candles.append(
                Candle(
                    date=candle_date,
                    open=Decimal(row["Open"]),
                    high=Decimal(row["High"]),
                    low=Decimal(row["Low"]),
                    close=Decimal(row["Close"]),
                    volume=int(Decimal(volume_text)) if volume_text else None,
                    source="stooq",
                )
            )
    except (InvalidOperation, ValueError, KeyError, AttributeError) as exc:
        raise StooqError("Stooq CSV contains an invalid row") from exc

    candles.sort(key=lambda candle: candle.date)
    return candles

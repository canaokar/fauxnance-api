"""Yahoo chart adapter for Phase 1 US end-of-day data."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from src.shared.market_data import (
    Candle,
    Capability,
    CapabilityUnavailable,
    EodResult,
)


class YahooError(Exception):
    """Raised when Yahoo returns an unusable chart response."""


Fetch = Callable[[str, float], bytes]
_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class YahooAdapter:
    name = "yahoo"
    base_url = "https://query1.finance.yahoo.com/v8/finance/chart"

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
                "period1": _epoch(start),
                "period2": _epoch(end + timedelta(days=1)),
                "interval": "1d",
                "includeAdjustedClose": "false",
            }
        )
        vendor_symbol = quote(self.vendor_symbol(symbol), safe="-.")
        body = self._fetch(
            f"{self.base_url}/{vendor_symbol}?{params}", self._timeout
        )
        return _parse_chart(body, start=start, end=end)

    def get_quote(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Yahoo quote support is not part of Phase 1")

    def discover(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Yahoo discovery support is not part of Phase 1")

    @staticmethod
    def vendor_symbol(symbol: str) -> str:
        canonical = symbol.strip().upper()
        if not _US_SYMBOL.fullmatch(canonical):
            raise CapabilityUnavailable(
                "Yahoo Phase 1 adapter only supports US symbols"
            )
        return canonical.replace(".", "-")


def _epoch(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp())


def _fetch(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "fauxnance-api/0.1 (educational market-data service)",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed host
        return response.read()


def _parse_chart(payload: bytes, *, start: date, end: date) -> EodResult:
    try:
        document = json.loads(payload.decode("utf-8"), parse_float=Decimal)
        chart = document["chart"]
        if chart.get("error"):
            message = chart["error"].get("description") or "Yahoo chart error"
            raise YahooError(str(message))
        result = chart["result"][0]
        timestamps = result["timestamp"]
        quote_values = result["indicators"]["quote"][0]
        series = [
            quote_values[field]
            for field in ("open", "high", "low", "close", "volume")
        ]
        if any(len(values) != len(timestamps) for values in series):
            raise YahooError("Yahoo chart arrays have inconsistent lengths")
    except YahooError:
        raise
    except (
        AttributeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
    ) as exc:
        raise YahooError("Yahoo returned an unexpected chart response") from exc

    all_candles: list[Candle] = []
    try:
        for timestamp, open_, high, low, close_, volume in zip(
            timestamps, *series, strict=True
        ):
            if None in (open_, high, low, close_):
                continue
            candle_date = datetime.fromtimestamp(int(timestamp), UTC).date()
            all_candles.append(
                Candle(
                    date=candle_date,
                    open=Decimal(open_),
                    high=Decimal(high),
                    low=Decimal(low),
                    close=Decimal(close_),
                    volume=int(Decimal(volume)) if volume is not None else None,
                    source="yahoo",
                )
            )
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise YahooError("Yahoo chart contains an invalid candle") from exc

    all_candles.sort(key=lambda candle: candle.date)
    candles = [candle for candle in all_candles if start <= candle.date <= end]
    return EodResult(candles=candles)

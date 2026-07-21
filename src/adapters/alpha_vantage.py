"""Alpha Vantage adapter for US daily end-of-day candles."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import json
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


class AlphaVantageError(Exception):
    """Raised when Alpha Vantage returns an unusable daily response."""


Fetch = Callable[[str, float], bytes]
_US_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class AlphaVantageAdapter:
    name = "alpha_vantage"
    base_url = "https://www.alphavantage.co/query"

    def __init__(
        self,
        api_key: str,
        *,
        fetch: Fetch | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._fetch = fetch or _fetch
        self._timeout = timeout

    def capabilities(self) -> set[Capability]:
        return {Capability.EOD_US}

    def get_eod(self, symbol: str, start: date, end: date) -> EodResult:
        if start > end:
            raise ValueError("start must not be after end")

        params = urlencode(
            {
                "function": "TIME_SERIES_DAILY",
                "symbol": self.vendor_symbol(symbol),
                "outputsize": "full",
                "apikey": self._api_key,
            }
        )
        payload = self._fetch(f"{self.base_url}?{params}", self._timeout)
        return EodResult(candles=_parse_daily(payload, start=start, end=end))

    def get_quote(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Alpha Vantage quote support is unavailable")

    def discover(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Alpha Vantage discovery support is unavailable")

    @staticmethod
    def vendor_symbol(symbol: str) -> str:
        canonical = symbol.strip().upper()
        if not _US_SYMBOL.fullmatch(canonical):
            raise CapabilityUnavailable(
                "Alpha Vantage adapter only supports US symbols"
            )
        return canonical


def _fetch(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "fauxnance-api/0.2 (educational market-data service)",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed host
        return response.read()


def _parse_daily(payload: bytes, *, start: date, end: date) -> list[Candle]:
    try:
        document = json.loads(payload.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlphaVantageError(
            "Alpha Vantage returned an unexpected response"
        ) from exc

    if not isinstance(document, dict):
        raise AlphaVantageError("Alpha Vantage returned an unexpected response")
    for field in ("Error Message", "Note", "Information"):
        if document.get(field):
            raise AlphaVantageError(str(document[field]))

    series = document.get("Time Series (Daily)")
    if not isinstance(series, dict):
        raise AlphaVantageError("Alpha Vantage response has no daily series")

    candles: list[Candle] = []
    try:
        for day, values in series.items():
            candle_date = date.fromisoformat(day)
            if not start <= candle_date <= end:
                continue
            if not isinstance(values, dict):
                raise TypeError("daily value is not an object")
            candles.append(
                Candle(
                    date=candle_date,
                    open=Decimal(values["1. open"]),
                    high=Decimal(values["2. high"]),
                    low=Decimal(values["3. low"]),
                    close=Decimal(values["4. close"]),
                    volume=int(Decimal(values["5. volume"])),
                    source="alpha_vantage",
                )
            )
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise AlphaVantageError(
            "Alpha Vantage daily series contains an invalid candle"
        ) from exc

    candles.sort(key=lambda candle: candle.date)
    return candles

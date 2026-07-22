"""Frankfurter v2 adapter for ECB daily FX reference rates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import json
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.shared.market_data import Candle, Capability, CapabilityUnavailable, EodResult
from src.shared.symbols import Market, parse_symbol


class FrankfurterError(Exception):
    """Raised when Frankfurter returns an unusable rate response."""


Fetch = Callable[[str, float], bytes]


class FrankfurterAdapter:
    name = "frankfurter"
    base_url = "https://api.frankfurter.dev/v2/rates"

    def __init__(self, *, fetch: Fetch | None = None, timeout: float = 10.0) -> None:
        self._fetch = fetch or _fetch
        self._timeout = timeout

    def capabilities(self) -> set[Capability]:
        return {Capability.EOD_FX}

    def get_eod(self, symbol: str, start: date, end: date) -> EodResult:
        if start > end:
            raise ValueError("start must not be after end")
        try:
            info = parse_symbol(symbol)
        except ValueError as exc:
            raise CapabilityUnavailable("Frankfurter requires an FX symbol") from exc
        if info.market != Market.FX:
            raise CapabilityUnavailable("Frankfurter requires an FX symbol")
        base = info.symbol[3:6]
        quote = info.symbol[6:9]
        query = urlencode(
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "base": base,
                "quotes": quote,
                "providers": "ECB",
            }
        )
        payload = self._fetch(f"{self.base_url}?{query}", self._timeout)
        return _parse_rates(payload, base=base, quote=quote, start=start, end=end)

    def get_quote(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Frankfurter quote support is not configured")

    def discover(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Frankfurter discovery is not supported")


def _parse_rates(
    payload: bytes, *, base: str, quote: str, start: date, end: date
) -> EodResult:
    try:
        document = json.loads(payload.decode("utf-8"), parse_float=Decimal)
        if not isinstance(document, list):
            raise FrankfurterError("Frankfurter rates response is not a list")
        candles: list[Candle] = []
        seen: set[date] = set()
        for row in document:
            if row.get("base") != base or row.get("quote") != quote:
                raise FrankfurterError("Frankfurter returned a different currency pair")
            day = date.fromisoformat(row["date"])
            rate = Decimal(str(row["rate"]))
            if day in seen or not start <= day <= end or not rate.is_finite() or rate <= 0:
                raise FrankfurterError("Frankfurter returned an invalid daily rate")
            seen.add(day)
            candles.append(
                Candle(
                    date=day,
                    open=rate,
                    high=rate,
                    low=rate,
                    close=rate,
                    volume=None,
                    source="frankfurter",
                )
            )
    except FrankfurterError:
        raise
    except (
        AttributeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        InvalidOperation,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise FrankfurterError("Frankfurter returned an invalid rates response") from exc
    candles.sort(key=lambda candle: candle.date)
    return EodResult(candles=candles)


def _fetch(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "fauxnance-api/0.1 (educational market-data service)",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()

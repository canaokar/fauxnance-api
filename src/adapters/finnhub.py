"""Finnhub adapter for best-effort US quotes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
from typing import Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.shared.market_data import Capability, CapabilityUnavailable, Quote
from src.shared.symbols import Market, parse_symbol


class FinnhubError(Exception):
    """Raised when Finnhub returns an unusable quote."""


Fetch = Callable[[str, float, Mapping[str, str]], bytes]


class FinnhubAdapter:
    name = "finnhub"
    base_url = "https://finnhub.io/api/v1/quote"

    def __init__(
        self,
        api_key: str,
        *,
        fetch: Fetch | None = None,
        timeout: float = 3.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Finnhub API key must not be empty")
        self._api_key = api_key.strip()
        self._fetch = fetch or _fetch
        self._timeout = timeout

    def capabilities(self) -> set[Capability]:
        return {Capability.QUOTE_US}

    def get_quote(self, symbol: str, **_kwargs: object) -> Quote:
        try:
            info = parse_symbol(symbol)
        except ValueError as exc:
            raise CapabilityUnavailable("Finnhub requires a US symbol") from exc
        if info.market != Market.US:
            raise CapabilityUnavailable("Finnhub requires a US symbol")
        url = f"{self.base_url}?{urlencode({'symbol': info.yahoo_symbol})}"
        payload = self._fetch(
            url,
            self._timeout,
            {"Accept": "application/json", "X-Finnhub-Token": self._api_key},
        )
        return _parse_quote(payload)

    def get_eod(self, *_args: object, **_kwargs: object) -> None:
        raise CapabilityUnavailable("Finnhub EOD history is not configured")

    def discover(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Finnhub discovery is not supported")


def _parse_quote(payload: bytes) -> Quote:
    try:
        document = json.loads(payload.decode("utf-8"), parse_float=Decimal)
        price = Decimal(str(document["c"]))
        previous = Decimal(str(document["pc"]))
        timestamp = int(document["t"])
        if not price.is_finite() or price <= 0 or not previous.is_finite() or previous <= 0:
            raise FinnhubError("Finnhub quote prices are invalid")
        if timestamp <= 0:
            raise FinnhubError("Finnhub returned no quote data")
        change = _optional_decimal(document.get("d"))
        if change is None:
            change = price - previous
        percent = _optional_decimal(document.get("dp"))
        if percent is None:
            percent = change / previous * Decimal("100")
        return Quote(
            price=price,
            currency=None,
            change=change,
            change_percent=percent,
            previous_close=previous,
            as_of=datetime.fromtimestamp(timestamp, UTC),
            market_state="unknown",
            source="finnhub",
        )
    except FinnhubError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        InvalidOperation,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        raise FinnhubError("Finnhub returned an invalid quote response") from exc


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


def _fetch(url: str, timeout: float, headers: Mapping[str, str]) -> bytes:
    request = Request(
        url,
        headers={
            **headers,
            "User-Agent": "fauxnance-api/0.1 (educational market-data service)",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()

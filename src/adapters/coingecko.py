"""CoinGecko Demo adapter for recent daily cryptocurrency observations."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Callable, Mapping
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from src.shared.market_data import Candle, Capability, CapabilityUnavailable, EodResult
from src.shared.symbols import Market, parse_symbol


class CoinGeckoError(Exception):
    """Raised when CoinGecko returns an unusable market chart."""


Fetch = Callable[[str, float, Mapping[str, str]], bytes]
_COIN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class CoinGeckoAdapter:
    name = "coingecko"
    base_url = "https://api.coingecko.com/api/v3"

    def __init__(
        self,
        api_key: str,
        *,
        fetch: Fetch | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("CoinGecko API key must not be empty")
        self._api_key = api_key.strip()
        self._fetch = fetch or _fetch
        self._timeout = timeout

    def capabilities(self) -> set[Capability]:
        return {Capability.EOD_CRYPTO, Capability.QUOTE_CRYPTO}

    def get_eod(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        coin_id: str | None = None,
    ) -> EodResult:
        if start > end:
            raise ValueError("start must not be after end")
        try:
            info = parse_symbol(symbol)
        except ValueError as exc:
            raise CapabilityUnavailable("CoinGecko requires a crypto symbol") from exc
        if info.market != Market.CRYPTO:
            raise CapabilityUnavailable("CoinGecko requires a crypto symbol")
        if (end - start).days > 365:
            raise CapabilityUnavailable("CoinGecko Demo history is limited to 365 days")
        identifier = str(coin_id or "").strip().lower()
        if not _COIN_ID.fullmatch(identifier):
            raise CapabilityUnavailable("CoinGecko coin ID is unavailable")

        query = urlencode(
            {
                "vs_currency": info.currency.lower(),
                "from": start.isoformat(),
                "to": (end + timedelta(days=1)).isoformat(),
                "interval": "daily",
                "precision": "full",
            }
        )
        headers = {"Accept": "application/json", "x-cg-demo-api-key": self._api_key}
        payload = self._fetch(
            f"{self.base_url}/coins/{quote(identifier, safe='-')}/market_chart/range?{query}",
            self._timeout,
            headers,
        )
        return _parse_market_chart(payload, start=start, end=end)

    def get_quote(self, _symbol: str) -> None:
        raise CapabilityUnavailable("CoinGecko quote support is not configured")

    def discover(self, _symbol: str) -> None:
        raise CapabilityUnavailable("CoinGecko discovery is not supported")


def _parse_market_chart(payload: bytes, *, start: date, end: date) -> EodResult:
    try:
        document = json.loads(payload.decode("utf-8"), parse_float=Decimal)
        prices = document["prices"]
        volumes = document.get("total_volumes", [])
        volume_by_day = {
            _millis_date(row[0]): int(Decimal(str(row[1])))
            for row in volumes
            if len(row) == 2 and Decimal(str(row[1])).is_finite() and Decimal(str(row[1])) >= 0
        }
        by_day: dict[date, Candle] = {}
        for row in prices:
            if len(row) != 2:
                raise CoinGeckoError("CoinGecko price row is invalid")
            day = _millis_date(row[0])
            price_value = Decimal(str(row[1]))
            if not price_value.is_finite() or price_value <= 0:
                raise CoinGeckoError("CoinGecko price is invalid")
            if start <= day <= end:
                by_day[day] = Candle(
                    date=day,
                    open=price_value,
                    high=price_value,
                    low=price_value,
                    close=price_value,
                    volume=volume_by_day.get(day),
                    source="coingecko",
                )
    except CoinGeckoError:
        raise
    except (
        AttributeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        InvalidOperation,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        raise CoinGeckoError("CoinGecko returned an invalid market chart") from exc
    return EodResult(candles=[by_day[day] for day in sorted(by_day)])


def _millis_date(value: object) -> date:
    return datetime.fromtimestamp(int(value) / 1000, UTC).date()


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

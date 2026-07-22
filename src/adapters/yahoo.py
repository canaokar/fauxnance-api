"""Yahoo chart adapter for end-of-day data and corporate actions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.shared.market_data import (
    Candle,
    Capability,
    CapabilityUnavailable,
    CorporateAction,
    EodResult,
    Quote,
)
from src.shared.symbols import parse_symbol


class YahooError(Exception):
    """Raised when Yahoo returns an unusable chart response."""


Fetch = Callable[[str, float], bytes]


class YahooAdapter:
    name = "yahoo"
    base_url = "https://query1.finance.yahoo.com/v8/finance/chart"

    def __init__(self, *, fetch: Fetch | None = None, timeout: float = 10.0) -> None:
        self._fetch = fetch or _fetch
        self._timeout = timeout

    def capabilities(self) -> set[Capability]:
        return {
            Capability.EOD_US,
            Capability.EOD_IN,
            Capability.EOD_FX,
            Capability.EOD_CRYPTO,
            Capability.QUOTE_US,
            Capability.QUOTE_IN,
            Capability.QUOTE_FX,
            Capability.QUOTE_CRYPTO,
        }

    def get_eod(self, symbol: str, start: date, end: date) -> EodResult:
        if start > end:
            raise ValueError("start must not be after end")

        params = urlencode(
            {
                # The warm-up supplies a reference close when a dividend falls
                # on the first requested day. Returned candles are still trimmed
                # to the caller's inclusive range.
                "period1": _epoch(start - timedelta(days=10)),
                "period2": _epoch(end + timedelta(days=1)),
                "interval": "1d",
                "includeAdjustedClose": "false",
                "events": "div,splits",
            }
        )
        vendor_symbol = quote(self.vendor_symbol(symbol), safe="-.")
        body = self._fetch(
            f"{self.base_url}/{vendor_symbol}?{params}", self._timeout
        )
        return _parse_chart(body, start=start, end=end)

    def get_quote(self, symbol: str) -> Quote:
        vendor_symbol = quote(self.vendor_symbol(symbol), safe="-.")
        params = urlencode({"range": "1d", "interval": "1m"})
        body = self._fetch(
            f"{self.base_url}/{vendor_symbol}?{params}", self._timeout
        )
        return _parse_quote(body)

    def discover(self, _symbol: str) -> None:
        raise CapabilityUnavailable("Yahoo discovery support is not part of Phase 1")

    @staticmethod
    def vendor_symbol(symbol: str) -> str:
        try:
            return parse_symbol(symbol).yahoo_symbol
        except ValueError as exc:
            raise CapabilityUnavailable("Yahoo does not support this symbol") from exc


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
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read()
    except HTTPError as exc:
        if exc.code == 400:
            try:
                body = exc.read()
            finally:
                exc.close()
            if _is_no_data_payload(body):
                return body
        raise


def _parse_chart(payload: bytes, *, start: date, end: date) -> EodResult:
    try:
        document = json.loads(payload.decode("utf-8"), parse_float=Decimal)
        chart = document["chart"]
        if chart.get("error"):
            message = chart["error"].get("description") or "Yahoo chart error"
            if isinstance(message, str) and message.startswith(
                "Data doesn't exist for startDate"
            ):
                return EodResult(candles=[])
            raise YahooError(str(message))
        result = chart["result"][0]
        timezone = _timezone(result.get("meta", {}).get("exchangeTimezoneName"))
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
            candle_date = datetime.fromtimestamp(int(timestamp), timezone).date()
            candle = Candle(
                date=candle_date,
                open=Decimal(open_),
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close_),
                volume=int(Decimal(volume)) if volume is not None else None,
                source="yahoo",
            )
            if not candle.low <= candle.open <= candle.high:
                continue
            if not candle.low <= candle.close <= candle.high:
                continue
            all_candles.append(candle)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise YahooError("Yahoo chart contains an invalid candle") from exc

    all_candles.sort(key=lambda candle: candle.date)
    actions = _parse_actions(
        result.get("events", {}), all_candles, start, end, timezone
    )
    candles = [candle for candle in all_candles if start <= candle.date <= end]
    return EodResult(candles=candles, actions=actions)


def _parse_actions(
    events: object,
    candles: list[Candle],
    start: date,
    end: date,
    timezone: ZoneInfo,
) -> list[CorporateAction]:
    if events is None:
        return []
    if not isinstance(events, dict):
        raise YahooError("Yahoo chart events are invalid")

    actions: list[CorporateAction] = []
    try:
        splits = events.get("splits", {})
        if not isinstance(splits, dict):
            raise YahooError("Yahoo split events are invalid")
        for raw in splits.values():
            action_date = datetime.fromtimestamp(int(raw["date"]), timezone).date()
            if not start <= action_date <= end:
                continue
            numerator = Decimal(str(raw["numerator"]))
            denominator = Decimal(str(raw["denominator"]))
            if numerator <= 0 or denominator <= 0:
                raise YahooError("Yahoo split ratio must be positive")
            actions.append(
                CorporateAction(
                    date=action_date,
                    type="split",
                    value=numerator / denominator,
                    factor=denominator / numerator,
                )
            )

        dividends = events.get("dividends", {})
        if not isinstance(dividends, dict):
            raise YahooError("Yahoo dividend events are invalid")
        for raw in dividends.values():
            action_date = datetime.fromtimestamp(int(raw["date"]), timezone).date()
            if not start <= action_date <= end:
                continue
            amount = Decimal(str(raw["amount"]))
            reference = next(
                (row.close for row in reversed(candles) if row.date < action_date),
                None,
            )
            # Yahoo can return an event at the start boundary without the prior
            # session required by the documented adjustment convention. A later
            # overlapping ingest will include the reference and persist it.
            if reference is None:
                continue
            if amount < 0 or reference <= amount:
                raise YahooError("Yahoo dividend event is invalid")
            actions.append(
                CorporateAction(
                    date=action_date,
                    type="dividend",
                    value=amount,
                    factor=(reference - amount) / reference,
                    reference_close=reference,
                )
            )
    except YahooError:
        raise
    except (InvalidOperation, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise YahooError("Yahoo chart contains an invalid corporate action") from exc

    actions.sort(key=lambda action: (action.date, action.type), reverse=True)
    return actions


def _timezone(name: object) -> ZoneInfo:
    if name is None:
        return ZoneInfo("UTC")
    if not isinstance(name, str):
        raise YahooError("Yahoo exchange timezone is invalid")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise YahooError("Yahoo exchange timezone is invalid") from exc


def _is_no_data_payload(payload: bytes) -> bool:
    try:
        message = json.loads(payload.decode("utf-8"))["chart"]["error"][
            "description"
        ]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        return False
    return isinstance(message, str) and message.startswith(
        "Data doesn't exist for startDate"
    )


def _parse_quote(payload: bytes) -> Quote:
    try:
        document = json.loads(payload.decode("utf-8"), parse_float=Decimal)
        chart = document["chart"]
        if chart.get("error"):
            message = chart["error"].get("description") or "Yahoo chart error"
            raise YahooError(str(message))
        meta = chart["result"][0]["meta"]
        price = Decimal(str(meta["regularMarketPrice"]))
        previous = Decimal(str(meta.get("chartPreviousClose", meta.get("previousClose"))))
        timestamp = int(meta["regularMarketTime"])
        if not price.is_finite() or price <= 0 or not previous.is_finite() or previous <= 0:
            raise YahooError("Yahoo quote prices are invalid")
        change = price - previous
        raw_state = str(meta.get("marketState", "unknown")).upper()
        market_state = {
            "REGULAR": "open",
            "PRE": "pre",
            "PREPRE": "pre",
            "POST": "post",
            "POSTPOST": "post",
            "CLOSED": "closed",
        }.get(raw_state, "unknown")
        currency = meta.get("currency")
        return Quote(
            price=price,
            currency=str(currency).upper() if currency else None,
            change=change,
            change_percent=change / previous * Decimal("100"),
            previous_close=previous,
            as_of=datetime.fromtimestamp(timestamp, UTC),
            market_state=market_state,
            source="yahoo",
        )
    except YahooError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        InvalidOperation,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        raise YahooError("Yahoo returned an invalid quote response") from exc

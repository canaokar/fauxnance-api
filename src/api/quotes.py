"""Cache-first quote resolution with guarded upstream fallbacks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, Sequence

from src.shared.market_data import Quote
from src.shared.symbols import Market, parse_symbol


class QuoteSource(Protocol):
    name: str

    def get_quote(self, symbol: str) -> Quote: ...


class SourceGuard(Protocol):
    def try_acquire(self) -> bool: ...

    def record_success(self) -> None: ...

    def record_failure(self) -> None: ...


class QuoteRepository(Protocol):
    def get_quote(self, symbol: str) -> Mapping[str, Any] | None: ...

    def put_quote(
        self,
        symbol: str,
        quote: Quote,
        *,
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None: ...


class QuoteUnavailable(Exception):
    """Raised when no fresh, upstream, or stale quote can be served."""


@dataclass(frozen=True, slots=True)
class ResolvedQuote:
    quote: Quote
    source: str
    stale: bool


class QuoteResolver:
    def __init__(
        self,
        repository: QuoteRepository,
        yahoo: QuoteSource,
        yahoo_guard: SourceGuard,
        *,
        finnhub: QuoteSource | None = None,
        finnhub_guard: SourceGuard | None = None,
        freshness: timedelta = timedelta(minutes=5),
        cleanup_ttl: timedelta = timedelta(days=7),
    ) -> None:
        if (finnhub is None) != (finnhub_guard is None):
            raise ValueError("Finnhub source and guard must be configured together")
        self._repository = repository
        self._yahoo = (yahoo, yahoo_guard)
        self._finnhub = (
            (finnhub, finnhub_guard)
            if finnhub is not None and finnhub_guard is not None
            else None
        )
        self._freshness = freshness
        self._cleanup_ttl = cleanup_ttl

    def resolve(
        self,
        symbol: str,
        metadata: Mapping[str, Any],
        *,
        now: datetime,
    ) -> ResolvedQuote:
        now = _as_utc(now)
        cached = _cached_quote(self._repository.get_quote(symbol))
        if cached is not None and cached[1] > now + timedelta(minutes=1):
            cached = None
        if cached is not None:
            quote, fetched_at = cached
            age = now - fetched_at
            if timedelta(0) <= age <= self._freshness:
                return ResolvedQuote(
                    quote=_with_currency(quote, metadata),
                    source="cache",
                    stale=False,
                )

        failures: list[str] = []
        for source, guard in self._sources(symbol):
            if not guard.try_acquire():
                failures.append(f"{source.name}: unavailable")
                continue
            try:
                quote = _with_currency(source.get_quote(symbol), metadata)
            except Exception as exc:
                guard.record_failure()
                failures.append(f"{source.name}: {exc}")
                continue
            guard.record_success()
            self._repository.put_quote(
                symbol,
                quote,
                fetched_at=now,
                expires_at=now + self._cleanup_ttl,
            )
            return ResolvedQuote(
                quote=quote,
                source=f"upstream:{source.name}",
                stale=False,
            )

        if cached is not None:
            quote, _fetched_at = cached
            return ResolvedQuote(
                quote=_with_currency(quote, metadata),
                source="cache",
                stale=True,
            )
        detail = "; ".join(failures) or "no quote sources configured"
        raise QuoteUnavailable(detail)

    def _sources(self, symbol: str) -> Sequence[tuple[QuoteSource, SourceGuard]]:
        market = parse_symbol(symbol).market
        if market == Market.US and self._finnhub is not None:
            return (self._finnhub, self._yahoo)
        return (self._yahoo,)


def quote_data(symbol: str, quote: Quote) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "price": _number(quote.price),
        "currency": quote.currency,
        "change": _optional_number(quote.change),
        "changePercent": _optional_number(quote.change_percent),
        "previousClose": _optional_number(quote.previous_close),
        "asOf": _timestamp(quote.as_of),
        "marketState": quote.market_state,
    }


def _cached_quote(item: Mapping[str, Any] | None) -> tuple[Quote, datetime] | None:
    if not item:
        return None
    try:
        raw = item["quote"]
        fetched_at = _parse_timestamp(item["fetchedAt"])
        quote = Quote(
            price=_decimal(raw["price"]),
            currency=str(raw["currency"]).upper() if raw.get("currency") else None,
            change=_optional_decimal(raw.get("change")),
            change_percent=_optional_decimal(raw.get("changePercent")),
            previous_close=_optional_decimal(raw.get("previousClose")),
            as_of=_parse_timestamp(raw["asOf"]),
            market_state=str(raw.get("marketState", "unknown")),
            source=str(item.get("src", "cache")),
        )
        if quote.price <= 0:
            return None
        return quote, fetched_at
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def _with_currency(quote: Quote, metadata: Mapping[str, Any]) -> Quote:
    if quote.currency:
        return quote
    return Quote(
        price=quote.price,
        currency=str(metadata.get("currency")) if metadata.get("currency") else None,
        change=quote.change,
        change_percent=quote.change_percent,
        previous_close=quote.previous_close,
        as_of=quote.as_of,
        market_state=quote.market_state,
        source=quote.source,
    )


def _decimal(value: object) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("quote number is not finite")
    return number


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else _decimal(value)


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _optional_number(value: Decimal | None) -> int | float | None:
    return None if value is None else _number(value)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("quote timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)

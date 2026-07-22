"""Market-aware guarded EOD source routing."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Protocol

from src.shared.market_data import EodResult
from src.shared.symbols import Market


class EodSource(Protocol):
    name: str

    def get_eod(self, symbol: str, start: date, end: date, **kwargs: Any) -> EodResult: ...


class SourceGuard(Protocol):
    def try_acquire(self) -> bool: ...

    def record_success(self) -> None: ...

    def record_failure(self) -> None: ...


class EodSourcesUnavailable(Exception):
    """Raised after all sources for a market and purpose are unavailable."""


class MarketEodRouter:
    def __init__(
        self,
        yahoo: EodSource,
        yahoo_guard: SourceGuard,
        *,
        alpha_vantage: EodSource | None = None,
        alpha_vantage_guard: SourceGuard | None = None,
        frankfurter: EodSource | None = None,
        frankfurter_guard: SourceGuard | None = None,
        coingecko: EodSource | None = None,
        coingecko_guard: SourceGuard | None = None,
    ) -> None:
        self._yahoo = (yahoo, yahoo_guard)
        self._alpha = _optional_pair(alpha_vantage, alpha_vantage_guard, "Alpha Vantage")
        self._frankfurter = _optional_pair(frankfurter, frankfurter_guard, "Frankfurter")
        self._coingecko = _optional_pair(coingecko, coingecko_guard, "CoinGecko")

    def get_eod(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        market: str,
        purpose: str,
        adapter_hints: Mapping[str, Any] | None = None,
    ) -> EodResult:
        sources = self._sources(Market(market), purpose)
        failures: list[str] = []
        for source, guard in sources:
            if not guard.try_acquire():
                failures.append(f"{source.name}: unavailable")
                continue
            try:
                kwargs: dict[str, Any] = {}
                if source.name == "coingecko":
                    kwargs["coin_id"] = (adapter_hints or {}).get("coinGeckoId")
                result = source.get_eod(symbol, start, end, **kwargs)
            except Exception as exc:
                guard.record_failure()
                failures.append(f"{source.name}: {exc}")
                continue
            guard.record_success()
            return result
        detail = "; ".join(failures) or "no sources configured"
        raise EodSourcesUnavailable(
            f"{market} EOD sources unavailable for {purpose} ({detail})"
        )

    def _sources(self, market: Market, purpose: str) -> list[tuple[EodSource, SourceGuard]]:
        if purpose not in {"scheduled", "backfill"}:
            raise ValueError("EOD purpose is unsupported")
        if market == Market.US:
            return [self._yahoo] + ([self._alpha] if self._alpha else [])
        if market == Market.IN:
            return [self._yahoo]
        if market == Market.FX:
            return ([self._frankfurter] if self._frankfurter else []) + [self._yahoo]
        if market == Market.CRYPTO:
            if purpose == "backfill":
                return [self._yahoo]
            return ([self._coingecko] if self._coingecko else []) + [self._yahoo]
        raise ValueError("market is unsupported")  # pragma: no cover


def _optional_pair(
    source: EodSource | None,
    guard: SourceGuard | None,
    label: str,
) -> tuple[EodSource, SourceGuard] | None:
    if (source is None) != (guard is None):
        raise ValueError(f"{label} source and guard must be configured together")
    return (source, guard) if source is not None and guard is not None else None

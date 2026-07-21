"""US EOD source order for Phase 2 ingestion."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from src.shared.market_data import EodResult


class EodSource(Protocol):
    name: str

    def get_eod(self, symbol: str, start: date, end: date) -> EodResult: ...


class SourceGuard(Protocol):
    def try_acquire(self) -> bool: ...

    def record_success(self) -> None: ...

    def record_failure(self) -> None: ...


class EodSourcesUnavailable(Exception):
    """Raised after every configured US EOD source is unavailable."""


class UsEodChain:
    """Try Yahoo first and the configured Alpha Vantage fallback second."""

    def __init__(
        self,
        yahoo: EodSource,
        yahoo_guard: SourceGuard,
        *,
        alpha_vantage: EodSource | None = None,
        alpha_vantage_guard: SourceGuard | None = None,
    ) -> None:
        if (alpha_vantage is None) != (alpha_vantage_guard is None):
            raise ValueError("Alpha Vantage source and guard must be configured together")
        self._sources = [(yahoo, yahoo_guard)]
        if alpha_vantage is not None and alpha_vantage_guard is not None:
            self._sources.append((alpha_vantage, alpha_vantage_guard))

    def get_eod(self, symbol: str, start: date, end: date) -> EodResult:
        failures: list[str] = []
        for source, guard in self._sources:
            if not guard.try_acquire():
                failures.append(f"{source.name}: unavailable")
                continue
            try:
                result = source.get_eod(symbol, start, end)
            except Exception as exc:
                guard.record_failure()
                failures.append(f"{source.name}: {exc}")
                continue
            guard.record_success()
            return result

        detail = "; ".join(failures) or "no sources configured"
        raise EodSourcesUnavailable(f"US EOD sources unavailable ({detail})")

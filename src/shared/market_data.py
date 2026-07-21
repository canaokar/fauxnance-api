"""Vendor-neutral market-data values used by adapters and API handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    EOD_US = "eod_us"


class CapabilityUnavailable(Exception):
    """Raised when an adapter cannot provide a requested operation."""


@dataclass(frozen=True, slots=True)
class Candle:
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None
    source: str


@dataclass(frozen=True, slots=True)
class CorporateAction:
    date: date
    type: str
    value: Decimal
    factor: Decimal
    reference_close: Decimal | None = None


@dataclass(frozen=True, slots=True)
class EodResult:
    candles: list[Candle] = field(default_factory=list)
    actions: list[CorporateAction] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SymbolMetadata:
    symbol: str
    name: str
    type: str
    exchange: str
    currency: str
    active: bool = True
    adapter_hints: dict[str, Any] = field(default_factory=dict)

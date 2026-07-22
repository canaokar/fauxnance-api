"""Vendor-neutral market-data values used by adapters and API handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Capability(StrEnum):
    EOD_US = "eod_us"
    EOD_IN = "eod_in"
    EOD_FX = "eod_fx"
    EOD_CRYPTO = "eod_crypto"
    QUOTE_US = "quote_us"
    QUOTE_IN = "quote_in"
    QUOTE_FX = "quote_fx"
    QUOTE_CRYPTO = "quote_crypto"
    DISCOVERY = "discovery"


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


@dataclass(frozen=True, slots=True)
class Quote:
    price: Decimal
    currency: str | None
    change: Decimal | None
    change_percent: Decimal | None
    previous_close: Decimal | None
    as_of: datetime
    market_state: str
    source: str

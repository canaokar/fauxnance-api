"""Canonical Fauxnance symbol parsing shared by API and ingestion paths."""

from __future__ import annotations

from dataclasses import dataclass
import re
from enum import StrEnum


class Market(StrEnum):
    US = "US"
    IN = "IN"
    FX = "FX"
    CRYPTO = "CRYPTO"


SUPPORTED_MARKETS = frozenset(market.value for market in Market)

_INDIA = re.compile(r"^[A-Z][A-Z0-9.-]{0,12}\.(NS|BO)$")
_FX = re.compile(r"^FX:([A-Z]{3})([A-Z]{3})$")
_CRYPTO = re.compile(r"^X:([A-Z0-9]{2,10})-([A-Z]{3,5})$")
_US = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    symbol: str
    market: Market
    asset_type: str
    exchange: str
    currency: str
    yahoo_symbol: str


def parse_symbol(value: str) -> SymbolInfo:
    if not isinstance(value, str):
        raise ValueError("symbol must be a string")
    symbol = value.strip().upper()

    india = _INDIA.fullmatch(symbol)
    if india:
        suffix = india.group(1)
        return SymbolInfo(
            symbol=symbol,
            market=Market.IN,
            asset_type="equity",
            exchange="NSE" if suffix == "NS" else "BSE",
            currency="INR",
            yahoo_symbol=symbol,
        )

    fx = _FX.fullmatch(symbol)
    if fx and fx.group(1) != fx.group(2):
        base, quote = fx.groups()
        return SymbolInfo(
            symbol=symbol,
            market=Market.FX,
            asset_type="fx",
            exchange="FX",
            currency=quote,
            yahoo_symbol=f"{base}{quote}=X",
        )

    crypto = _CRYPTO.fullmatch(symbol)
    if crypto:
        base, quote_currency = crypto.groups()
        return SymbolInfo(
            symbol=symbol,
            market=Market.CRYPTO,
            asset_type="crypto",
            exchange="CRYPTO",
            currency=quote_currency,
            yahoo_symbol=f"{base}-{quote_currency}",
        )

    if _US.fullmatch(symbol):
        return SymbolInfo(
            symbol=symbol,
            market=Market.US,
            asset_type="equity",
            exchange="US",
            currency="USD",
            yahoo_symbol=symbol.replace(".", "-"),
        )

    raise ValueError("symbol is not a supported canonical symbol")


def canonical_symbol(value: str) -> str:
    return parse_symbol(value).symbol

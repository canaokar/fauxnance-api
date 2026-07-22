"""Loader for checked-in, versioned symbol universes."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from src.shared.symbols import SUPPORTED_MARKETS, parse_symbol


@dataclass(frozen=True, slots=True)
class Universe:
    version: int
    universe_id: str
    market: str
    symbols: tuple[Mapping[str, Any], ...]


def load_universe(path: Path) -> Universe:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required_root = {"version", "id", "market", "symbols"}
    if not required_root.issubset(raw) or raw["version"] != 1:
        raise ValueError("universe must use the version 1 schema")
    if not isinstance(raw["symbols"], list) or not raw["symbols"]:
        raise ValueError("universe must contain symbols")

    required_symbol = {"symbol", "name", "type", "exchange", "currency"}
    market = str(raw["market"]).upper()
    if market not in SUPPORTED_MARKETS:
        raise ValueError("universe market is unsupported")
    normalized: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in raw["symbols"]:
        if not isinstance(item, dict) or not required_symbol.issubset(item):
            raise ValueError("each symbol needs symbol, name, type, exchange, currency")
        symbol = str(item["symbol"]).strip().upper()
        if not symbol or symbol in seen:
            raise ValueError(f"invalid or duplicate symbol: {symbol!r}")
        try:
            parsed = parse_symbol(symbol)
        except ValueError as exc:
            raise ValueError(f"invalid or duplicate symbol: {symbol!r}") from exc
        if parsed.market.value != market:
            raise ValueError(f"symbol {symbol!r} does not belong to {market}")
        seen.add(symbol)
        normalized_item: dict[str, Any] = {
            "symbol": symbol,
            "name": str(item["name"]),
            "type": str(item["type"]),
            "exchange": str(item["exchange"]),
            "currency": str(item["currency"]),
        }
        hints = item.get("adapterHints")
        if hints is not None:
            if not isinstance(hints, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in hints.items()
            ):
                raise ValueError("adapterHints must be a string map")
            normalized_item["adapterHints"] = dict(hints)
        normalized.append(normalized_item)
    return Universe(
        version=1,
        universe_id=str(raw["id"]),
        market=market,
        symbols=tuple(normalized),
    )

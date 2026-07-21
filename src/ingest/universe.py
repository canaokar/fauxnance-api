"""Loader for checked-in, versioned symbol universes."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Universe:
    version: int
    universe_id: str
    market: str
    symbols: tuple[Mapping[str, str], ...]


def load_universe(path: Path) -> Universe:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required_root = {"version", "id", "market", "symbols"}
    if not required_root.issubset(raw) or raw["version"] != 1:
        raise ValueError("universe must use the version 1 schema")
    if not isinstance(raw["symbols"], list) or not raw["symbols"]:
        raise ValueError("universe must contain symbols")

    required_symbol = {"symbol", "name", "type", "exchange", "currency"}
    normalized: list[Mapping[str, str]] = []
    seen: set[str] = set()
    for item in raw["symbols"]:
        if not isinstance(item, dict) or not required_symbol.issubset(item):
            raise ValueError("each symbol needs symbol, name, type, exchange, currency")
        symbol = str(item["symbol"]).strip().upper()
        if not symbol or symbol in seen:
            raise ValueError(f"invalid or duplicate symbol: {symbol!r}")
        seen.add(symbol)
        normalized.append(
            {
                "symbol": symbol,
                "name": str(item["name"]),
                "type": str(item["type"]),
                "exchange": str(item["exchange"]),
                "currency": str(item["currency"]),
            }
        )
    return Universe(
        version=1,
        universe_id=str(raw["id"]),
        market=str(raw["market"]),
        symbols=tuple(normalized),
    )

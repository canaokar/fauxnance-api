#!/usr/bin/env python3
"""Manually backfill the versioned Phase 1 dev universe from Yahoo."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
from pathlib import Path
import random
import sys
import time
from typing import Any, Callable, Mapping, Sequence

# Keep the documented direct invocation working without installing the project.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from src.adapters.yahoo import YahooAdapter
from src.shared.market_data import Candle


DEFAULT_UNIVERSE = (
    _REPOSITORY_ROOT / "data" / "universes" / "dev-v1.json"
)


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


class DynamoDBDevBackfillRepository:
    """Small write-side repository used only by the Phase 1 manual backfill."""

    def __init__(
        self,
        table: Any,
        *,
        max_merge_attempts: int = 5,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self._table = table
        self._max_merge_attempts = max_merge_attempts
        self._pause = pause

    def write_candles(self, symbol: str, candles: Sequence[Candle]) -> None:
        chunks: dict[str, list[Candle]] = defaultdict(list)
        for candle in candles:
            chunks[candle.date.strftime("%Y-%m")].append(candle)
        for month, month_candles in sorted(chunks.items()):
            self._merge_chunk(symbol, month, month_candles)

    def write_symbol(
        self,
        metadata: Mapping[str, str],
        *,
        market: str,
        first_date: date,
        last_date: date,
    ) -> None:
        symbol = metadata["symbol"]
        common = {
            "symbol": symbol,
            "name": metadata["name"],
            "type": metadata["type"],
            "exchange": metadata["exchange"],
            "currency": metadata["currency"],
            "market": market,
            "active": True,
            "coverage": {
                "eodFrom": first_date.isoformat(),
                "eodTo": last_date.isoformat(),
            },
        }
        self._table.put_item(Item={"PK": f"SYM#{symbol}", "SK": "META", **common})
        self._table.put_item(Item={"PK": "SYMBOLS", "SK": symbol, **common})

    def advance_market_status(
        self, market: str, latest_eod: date, *, now: datetime | None = None
    ) -> None:
        timestamp = _utc_timestamp(now)
        try:
            self._table.update_item(
                Key={"PK": f"MARKET#{market}", "SK": "STATUS"},
                UpdateExpression="SET latestEod = :latest, updatedAt = :updated",
                ConditionExpression=(
                    "attribute_not_exists(latestEod) OR latestEod < :latest"
                ),
                ExpressionAttributeValues={
                    ":latest": latest_eod.isoformat(),
                    ":updated": timestamp,
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def _merge_chunk(
        self, symbol: str, month: str, incoming: Sequence[Candle]
    ) -> None:
        key = {"PK": f"SYM#{symbol}", "SK": f"EOD#{month}"}
        for attempt in range(self._max_merge_attempts):
            existing = self._table.get_item(Key=key, ConsistentRead=True).get("Item")
            prior_candles = existing.get("candles", []) if existing else []
            by_date = {str(row["d"]): dict(row) for row in prior_candles}
            for candle in incoming:
                by_date.setdefault(candle.date.isoformat(), _candle_item(candle))
            revision = int(existing.get("revision", 0)) if existing else 0
            item = {
                **key,
                "revision": revision + 1,
                "candles": [by_date[candle_date] for candle_date in sorted(by_date)],
            }
            request: dict[str, Any] = {"Item": item}
            if existing:
                request.update(
                    {
                        "ConditionExpression": "revision = :expected",
                        "ExpressionAttributeValues": {":expected": revision},
                    }
                )
            else:
                request["ConditionExpression"] = "attribute_not_exists(PK)"
            try:
                self._table.put_item(**request)
                return
            except Exception as exc:
                if (
                    not _is_conditional_failure(exc)
                    or attempt + 1 == self._max_merge_attempts
                ):
                    raise
                self._pause(random.uniform(0.02, 0.1) * (attempt + 1))


def run_backfill(
    adapter: Any,
    repository: DynamoDBDevBackfillRepository,
    universe: Universe,
    *,
    start: date,
    end: date,
    progress: Callable[[str], None] = print,
) -> list[str]:
    if start > end:
        raise ValueError("start must not be after end")
    failures: list[str] = []
    latest: date | None = None
    for metadata in universe.symbols:
        symbol = metadata["symbol"]
        try:
            result = adapter.get_eod(symbol, start, end)
            if not result.candles:
                raise ValueError("source returned no candles")
            repository.write_candles(symbol, result.candles)
            first_date = min(candle.date for candle in result.candles)
            last_date = max(candle.date for candle in result.candles)
            repository.write_symbol(
                metadata,
                market=universe.market,
                first_date=first_date,
                last_date=last_date,
            )
            latest = max(latest, last_date) if latest else last_date
            progress(f"{symbol}: stored {len(result.candles)} candles")
        except Exception as exc:
            failures.append(symbol)
            progress(f"{symbol}: FAILED ({exc})")
    if latest is not None:
        repository.advance_market_status(universe.market, latest)
    return failures


def _candle_item(candle: Candle) -> dict[str, Any]:
    return {
        "d": candle.date.isoformat(),
        "o": candle.open,
        "h": candle.high,
        "l": candle.low,
        "c": candle.close,
        "v": candle.volume,
        "src": candle.source,
    }


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _utc_timestamp(now: datetime | None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _ten_years_before(value: date) -> date:
    try:
        return value.replace(year=value.year - 10)
    except ValueError:  # February 29
        return value.replace(year=value.year - 10, day=28)


def _parser() -> argparse.ArgumentParser:
    today = datetime.now(UTC).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--profile", default="megh.io")
    parser.add_argument("--data-table")
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument(
        "--from",
        dest="start",
        type=date.fromisoformat,
        default=_ten_years_before(today),
    )
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=today)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - operator environment guard
        raise SystemExit("boto3 is required; install requirements-dev.txt") from exc

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table_name = args.data_table or f"fauxnance-{args.stage}-data"
    table = session.resource("dynamodb").Table(table_name)
    failures = run_backfill(
        YahooAdapter(),
        DynamoDBDevBackfillRepository(table),
        load_universe(args.universe),
        start=args.start,
        end=args.end,
    )
    if failures:
        print(
            f"Backfill completed with {len(failures)} failure(s): "
            f"{', '.join(failures)}"
        )
        return 1
    print("Backfill completed successfully")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

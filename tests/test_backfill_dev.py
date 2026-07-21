from datetime import date
from decimal import Decimal
import unittest

from scripts.backfill_dev import (
    DEFAULT_UNIVERSE,
    DynamoDBDevBackfillRepository,
    load_universe,
    run_backfill,
)
from src.shared.market_data import Candle, EodResult


def candle(day, close, source="stooq"):
    value = Decimal(str(close))
    return Candle(
        date=date.fromisoformat(day),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=100,
        source=source,
    )


class FakeTable:
    def __init__(self):
        self.items = {}
        self.updates = []

    def get_item(self, *, Key, **_kwargs):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def put_item(self, *, Item, **_kwargs):
        self.items[(Item["PK"], Item["SK"])] = Item

    def update_item(self, **request):
        self.updates.append(request)


class Adapter:
    def __init__(self, results):
        self.results = results

    def get_eod(self, symbol, _start, _end):
        result = self.results[symbol]
        if isinstance(result, Exception):
            raise result
        return EodResult(candles=result)


class BackfillDevTests(unittest.TestCase):
    def test_versioned_dev_universe_has_twenty_unique_symbols(self):
        universe = load_universe(DEFAULT_UNIVERSE)
        symbols = [item["symbol"] for item in universe.symbols]
        self.assertEqual(universe.version, 1)
        self.assertEqual(universe.market, "US")
        self.assertEqual(len(symbols), 20)
        self.assertEqual(len(set(symbols)), 20)
        self.assertIn("AAPL", symbols)

    def test_month_merge_preserves_first_written_date(self):
        table = FakeTable()
        table.items[("SYM#AAPL", "EOD#2026-07")] = {
            "PK": "SYM#AAPL",
            "SK": "EOD#2026-07",
            "revision": 2,
            "candles": [
                {
                    "d": "2026-07-20",
                    "o": Decimal("1"),
                    "h": Decimal("1"),
                    "l": Decimal("1"),
                    "c": Decimal("1"),
                    "v": 100,
                    "src": "original",
                }
            ],
        }
        repository = DynamoDBDevBackfillRepository(table, pause=lambda _seconds: None)

        repository.write_candles(
            "AAPL",
            [candle("2026-07-20", 99), candle("2026-07-21", 2)],
        )

        stored = table.items[("SYM#AAPL", "EOD#2026-07")]
        self.assertEqual(stored["revision"], 3)
        self.assertEqual(stored["candles"][0]["c"], Decimal("1"))
        self.assertEqual(stored["candles"][0]["src"], "original")
        self.assertEqual(stored["candles"][1]["c"], Decimal("2"))

    def test_run_backfill_continues_after_a_symbol_failure(self):
        # Build the same value object directly; the loader itself is covered above.
        full = load_universe(DEFAULT_UNIVERSE)
        universe = type(full)(
            version=1,
            universe_id="test",
            market="US",
            symbols=full.symbols[:2],
        )
        first, second = (item["symbol"] for item in universe.symbols)
        table = FakeTable()
        messages = []

        failures = run_backfill(
            Adapter(
                {
                    first: [candle("2026-07-20", 10)],
                    second: RuntimeError("upstream unavailable"),
                }
            ),
            DynamoDBDevBackfillRepository(table),
            universe,
            start=date(2026, 7, 1),
            end=date(2026, 7, 21),
            progress=messages.append,
        )

        self.assertEqual(failures, [second])
        self.assertIn((f"SYM#{first}", "META"), table.items)
        self.assertEqual(len(table.updates), 1)
        self.assertIn("FAILED", messages[-1])


if __name__ == "__main__":
    unittest.main()

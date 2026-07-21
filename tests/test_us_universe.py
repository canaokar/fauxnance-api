import json
from pathlib import Path
import re
import unittest


UNIVERSE = (
    Path(__file__).resolve().parents[1] / "data" / "universes" / "us-v1.json"
)
ETF_SYMBOLS = {
    "SPY",
    "XLC",
    "XLY",
    "XLP",
    "XLE",
    "XLF",
    "XLV",
    "XLI",
    "XLB",
    "XLRE",
    "XLK",
    "XLU",
}
CANONICAL_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class UsUniverseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.universe = json.loads(UNIVERSE.read_text(encoding="utf-8"))
        cls.symbols = cls.universe["symbols"]

    def test_versioned_snapshot_has_source_metadata(self):
        self.assertEqual(self.universe["version"], 1)
        self.assertEqual(self.universe["id"], "us-phase2-v1")
        self.assertEqual(self.universe["market"], "US")
        self.assertEqual(
            self.universe["source"],
            {
                "name": "datasets/s-and-p-500-companies",
                "url": "https://github.com/datasets/s-and-p-500-companies",
                "commit": "d8811227e3cf83caa03f17c7022a964dd19eee52",
                "asOf": "2026-07-21",
            },
        )

    def test_all_items_use_the_v1_symbol_shape_and_are_unique(self):
        required = {"symbol", "name", "type", "exchange", "currency"}
        self.assertEqual(len(self.symbols), 515)
        for item in self.symbols:
            self.assertEqual(set(item), required)
            self.assertRegex(item["symbol"], CANONICAL_SYMBOL)
            self.assertTrue(item["name"])
            self.assertEqual(item["currency"], "USD")
        values = [item["symbol"] for item in self.symbols]
        self.assertEqual(len(set(values)), 515)

    def test_snapshot_contains_503_equities_and_the_exact_twelve_etfs(self):
        equities = [item for item in self.symbols if item["type"] == "equity"]
        etfs = [item for item in self.symbols if item["type"] == "etf"]
        self.assertEqual(len(equities), 503)
        self.assertEqual(len(etfs), 12)
        self.assertEqual({item["symbol"] for item in etfs}, ETF_SYMBOLS)
        self.assertTrue(all(item["exchange"] == "US" for item in equities))
        self.assertTrue(all(item["exchange"] == "NYSE ARCA" for item in etfs))

    def test_representative_share_classes_and_etfs_are_preserved(self):
        by_symbol = {item["symbol"]: item for item in self.symbols}
        self.assertEqual(by_symbol["BRK.B"]["name"], "Berkshire Hathaway")
        self.assertEqual(by_symbol["BF.B"]["name"], "Brown–Forman")
        self.assertEqual(by_symbol["SPY"]["type"], "etf")
        self.assertEqual(by_symbol["XLRE"]["exchange"], "NYSE ARCA")


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
import unittest

from src.ingest.universe import load_universe
from src.shared.symbols import parse_symbol


ROOT = Path(__file__).resolve().parents[1]


class MultiMarketUniverseTests(unittest.TestCase):
    def test_phase_three_universes_are_versioned_unique_and_market_consistent(self):
        expected = {
            "india-v1.json": ("IN", 30),
            "fx-v1.json": ("FX", 12),
            "crypto-v1.json": ("CRYPTO", 12),
        }
        for filename, (market, count) in expected.items():
            with self.subTest(filename=filename):
                universe = load_universe(ROOT / "data" / "universes" / filename)
                self.assertEqual(universe.version, 1)
                self.assertEqual(universe.market, market)
                self.assertEqual(len(universe.symbols), count)
                symbols = [item["symbol"] for item in universe.symbols]
                self.assertEqual(len(symbols), len(set(symbols)))
                self.assertTrue(
                    all(parse_symbol(symbol).market.value == market for symbol in symbols)
                )

    def test_crypto_universe_pins_a_coingecko_id_for_every_symbol(self):
        universe = load_universe(ROOT / "data" / "universes" / "crypto-v1.json")
        self.assertTrue(
            all(item.get("adapterHints", {}).get("coinGeckoId") for item in universe.symbols)
        )

    def test_serverless_enables_all_market_schedules_without_a_custom_domain(self):
        template = (ROOT / "serverless.yml").read_text(encoding="utf-8")
        for market in ("US", "IN", "FX", "CRYPTO"):
            self.assertIn(f"market: {market}", template)
        self.assertIn("HEALTH_MARKETS: US,IN,FX,CRYPTO", template)
        self.assertIn("COINGECKO_API_KEY_PARAMETER", template)
        self.assertNotIn("customDomain", template)
        self.assertNotIn("domainName", template)


if __name__ == "__main__":
    unittest.main()

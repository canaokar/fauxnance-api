import unittest

from src.shared.symbols import Market, canonical_symbol, parse_symbol


class SymbolTests(unittest.TestCase):
    def test_parses_every_supported_symbol_family(self):
        cases = {
            "aapl": ("AAPL", Market.US, "AAPL"),
            "brk.b": ("BRK.B", Market.US, "BRK-B"),
            "infy.ns": ("INFY.NS", Market.IN, "INFY.NS"),
            "tatasteel.bo": ("TATASTEEL.BO", Market.IN, "TATASTEEL.BO"),
            "fx:eurusd": ("FX:EURUSD", Market.FX, "EURUSD=X"),
            "x:btc-usd": ("X:BTC-USD", Market.CRYPTO, "BTC-USD"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = parse_symbol(raw)
                self.assertEqual(
                    (parsed.symbol, parsed.market, parsed.yahoo_symbol), expected
                )

    def test_rejects_invalid_or_ambiguous_shapes(self):
        for raw in ("", "BAD SYMBOL", "FX:USDUSD", "FX:USDE", "X:BTC", "AAPL:NS"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                canonical_symbol(raw)


if __name__ == "__main__":
    unittest.main()

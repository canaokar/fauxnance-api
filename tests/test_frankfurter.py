from datetime import date
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse
import unittest

from src.adapters.frankfurter import FrankfurterAdapter, FrankfurterError
from src.shared.market_data import CapabilityUnavailable


class FrankfurterAdapterTests(unittest.TestCase):
    def test_fetches_ecb_time_series_as_close_only_candles(self):
        calls = []
        payload = [
            {"date": "2026-07-20", "base": "EUR", "quote": "USD", "rate": 1.1426},
            {"date": "2026-07-21", "base": "EUR", "quote": "USD", "rate": 1.1418},
        ]

        adapter = FrankfurterAdapter(
            fetch=lambda url, timeout: calls.append((url, timeout)) or json.dumps(payload).encode()
        )
        result = adapter.get_eod("FX:EURUSD", date(2026, 7, 20), date(2026, 7, 21))

        self.assertEqual([row.date for row in result.candles], [date(2026, 7, 20), date(2026, 7, 21)])
        self.assertEqual(result.candles[0].open, Decimal("1.1426"))
        self.assertEqual(result.candles[0].open, result.candles[0].close)
        self.assertIsNone(result.candles[0].volume)
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(query["providers"], ["ECB"])
        self.assertEqual((query["base"], query["quotes"]), (["EUR"], ["USD"]))

    def test_rejects_wrong_symbol_and_malformed_pair_response(self):
        with self.assertRaises(CapabilityUnavailable):
            FrankfurterAdapter().get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21))
        with self.assertRaises(FrankfurterError):
            FrankfurterAdapter(
                fetch=lambda _url, _timeout: b'[{"date":"2026-07-21","base":"USD","quote":"EUR","rate":1}]'
            ).get_eod("FX:EURUSD", date(2026, 7, 20), date(2026, 7, 21))


if __name__ == "__main__":
    unittest.main()

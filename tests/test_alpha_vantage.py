from datetime import date
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse
import unittest

from src.adapters.alpha_vantage import AlphaVantageAdapter, AlphaVantageError
from src.shared.market_data import Capability, CapabilityUnavailable


DAILY = {
    "Meta Data": {"2. Symbol": "AAPL"},
    "Time Series (Daily)": {
        "2026-07-21": {
            "1. open": "212.1000",
            "2. high": "215.2500",
            "3. low": "211.9000",
            "4. close": "214.8000",
            "5. volume": "41234567",
        },
        "2026-07-20": {
            "1. open": "210.5000",
            "2. high": "214.0000",
            "3. low": "209.7500",
            "4. close": "213.2500",
            "5. volume": "50123456",
        },
        "2026-07-19": {
            "1. open": "209.0000",
            "2. high": "211.0000",
            "3. low": "208.0000",
            "4. close": "210.0000",
            "5. volume": "45000000",
        },
    },
}


class AlphaVantageAdapterTests(unittest.TestCase):
    def test_fetches_normalizes_sorts_and_bounds_daily_series(self):
        calls = []

        def fetch(url, timeout):
            calls.append((url, timeout))
            return json.dumps(DAILY).encode()

        result = AlphaVantageAdapter("secret-key", fetch=fetch).get_eod(
            "aapl", date(2026, 7, 20), date(2026, 7, 21)
        )

        self.assertEqual(
            [candle.date for candle in result.candles],
            [date(2026, 7, 20), date(2026, 7, 21)],
        )
        self.assertEqual(result.candles[0].open, Decimal("210.5000"))
        self.assertEqual(result.candles[0].volume, 50123456)
        self.assertEqual(result.candles[0].source, "alpha_vantage")
        self.assertEqual(result.actions, [])
        self.assertEqual(calls[0][1], 10.0)
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(query["function"], ["TIME_SERIES_DAILY"])
        self.assertEqual(query["symbol"], ["AAPL"])
        self.assertEqual(query["outputsize"], ["full"])
        self.assertEqual(query["apikey"], ["secret-key"])

    def test_provider_limit_and_malformed_candle_are_explicit(self):
        with self.assertRaisesRegex(AlphaVantageError, "call frequency"):
            AlphaVantageAdapter(
                "key",
                fetch=lambda _url, _timeout: json.dumps(
                    {"Note": "API call frequency exceeded"}
                ).encode(),
            ).get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21))

        malformed = json.loads(json.dumps(DAILY))
        del malformed["Time Series (Daily)"]["2026-07-21"]["4. close"]
        with self.assertRaisesRegex(AlphaVantageError, "invalid candle"):
            AlphaVantageAdapter(
                "key",
                fetch=lambda _url, _timeout: json.dumps(malformed).encode(),
            ).get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21))

    def test_constructor_symbol_and_unsupported_capabilities_are_scoped(self):
        with self.assertRaises(ValueError):
            AlphaVantageAdapter("  ")

        adapter = AlphaVantageAdapter("key")
        self.assertEqual(adapter.capabilities(), {Capability.EOD_US})
        with self.assertRaises(CapabilityUnavailable):
            adapter.vendor_symbol("bad symbol")
        with self.assertRaises(CapabilityUnavailable):
            adapter.get_quote("AAPL")
        with self.assertRaises(CapabilityUnavailable):
            adapter.discover("AAPL")


if __name__ == "__main__":
    unittest.main()

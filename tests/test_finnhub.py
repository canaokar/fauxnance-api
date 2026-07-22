from datetime import UTC, datetime
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse
import unittest

from src.adapters.finnhub import FinnhubAdapter, FinnhubError
from src.shared.market_data import CapabilityUnavailable


class FinnhubAdapterTests(unittest.TestCase):
    def test_normalizes_quote_and_keeps_the_token_out_of_the_url(self):
        calls = []

        def fetch(url, timeout, headers):
            calls.append((url, timeout, headers))
            return json.dumps(
                {"c": 232.71, "d": 0.21, "dp": 0.09, "pc": 232.5, "t": 1784648530}
            ).encode()

        quote = FinnhubAdapter("secret-token", fetch=fetch).get_quote("AAPL")

        self.assertEqual(quote.price, Decimal("232.71"))
        self.assertEqual(quote.change_percent, Decimal("0.09"))
        self.assertEqual(quote.previous_close, Decimal("232.5"))
        self.assertEqual(quote.market_state, "unknown")
        self.assertNotIn("secret-token", calls[0][0])
        self.assertEqual(calls[0][2]["X-Finnhub-Token"], "secret-token")
        self.assertEqual(parse_qs(urlparse(calls[0][0]).query)["symbol"], ["AAPL"])

    def test_computes_missing_change_fields(self):
        quote = FinnhubAdapter(
            "key",
            fetch=lambda *_args: b'{"c":105,"pc":100,"t":1784648530}',
        ).get_quote("MSFT")
        self.assertEqual(quote.change, Decimal("5"))
        self.assertEqual(quote.change_percent, Decimal("5.00"))

    def test_rejects_non_us_no_data_and_malformed_quotes(self):
        adapter = FinnhubAdapter("key", fetch=lambda *_args: b'{"c":0,"pc":0,"t":0}')
        with self.assertRaises(CapabilityUnavailable):
            adapter.get_quote("INFY.NS")
        with self.assertRaises(FinnhubError):
            adapter.get_quote("AAPL")
        with self.assertRaises(FinnhubError):
            FinnhubAdapter("key", fetch=lambda *_args: b"not json").get_quote("AAPL")


if __name__ == "__main__":
    unittest.main()

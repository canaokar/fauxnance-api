from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
import unittest

from src.adapters.stooq import StooqAdapter, StooqError
from src.shared.market_data import Capability, CapabilityUnavailable


CSV = b"""Date,Open,High,Low,Close,Volume
2026-07-20,210.50,214.00,209.75,213.25,50123456
2026-07-21,213.00,215.25,212.10,214.80,41234567
"""


class StooqAdapterTests(unittest.TestCase):
    def test_fetches_and_normalizes_inclusive_daily_csv(self):
        calls = []

        def fetch(url, timeout):
            calls.append((url, timeout))
            return CSV

        result = StooqAdapter(fetch=fetch).get_eod(
            "AAPL", date(2026, 7, 20), date(2026, 7, 21)
        )

        self.assertEqual(len(result.candles), 2)
        self.assertEqual(result.actions, [])
        self.assertEqual(result.candles[0].date, date(2026, 7, 20))
        self.assertEqual(result.candles[0].open, Decimal("210.50"))
        self.assertEqual(result.candles[0].volume, 50123456)
        self.assertEqual(result.candles[0].source, "stooq")
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(
            query,
            {"s": ["aapl.us"], "d1": ["20260720"], "d2": ["20260721"], "i": ["d"]},
        )
        self.assertEqual(calls[0][1], 10.0)

    def test_filters_out_of_range_rows_defensively(self):
        result = StooqAdapter(fetch=lambda _url, _timeout: CSV).get_eod(
            "AAPL", date(2026, 7, 21), date(2026, 7, 21)
        )
        self.assertEqual([row.date for row in result.candles], [date(2026, 7, 21)])

    def test_vendor_symbol_mapping_supports_us_share_classes(self):
        self.assertEqual(StooqAdapter.vendor_symbol("brk.b"), "brk-b.us")
        self.assertEqual(StooqAdapter().capabilities(), {Capability.EOD_US})
        with self.assertRaises(CapabilityUnavailable):
            StooqAdapter.vendor_symbol("bad symbol")

    def test_no_data_is_an_empty_success(self):
        result = StooqAdapter(fetch=lambda _url, _timeout: b"No data").get_eod(
            "AAPL", date(2026, 7, 20), date(2026, 7, 21)
        )
        self.assertEqual(result.candles, [])

    def test_invalid_csv_and_unsupported_operations_are_explicit(self):
        adapter = StooqAdapter(fetch=lambda _url, _timeout: b"bad,data\n1,2\n")
        with self.assertRaises(StooqError):
            adapter.get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21))
        with self.assertRaises(CapabilityUnavailable):
            adapter.get_quote("AAPL")
        with self.assertRaises(CapabilityUnavailable):
            adapter.discover("AAPL")


if __name__ == "__main__":
    unittest.main()

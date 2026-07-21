from datetime import UTC, date, datetime
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse
import unittest

from src.adapters.yahoo import YahooAdapter, YahooError
from src.shared.market_data import Capability, CapabilityUnavailable


def epoch(day):
    value = date.fromisoformat(day)
    timestamp = datetime(value.year, value.month, value.day, 13, 30, tzinfo=UTC)
    return int(timestamp.timestamp())


CHART = {
    "chart": {
        "result": [
            {
                "timestamp": [
                    epoch("2026-07-19"),
                    epoch("2026-07-20"),
                    epoch("2026-07-21"),
                ],
                "indicators": {
                    "quote": [
                        {
                            "open": [209.0, 210.5, 213.0],
                            "high": [211.0, 214.0, 215.25],
                            "low": [208.0, 209.75, 212.1],
                            "close": [210.0, 213.25, 214.8],
                            "volume": [45000000, 50123456, 41234567],
                        }
                    ]
                },
            }
        ],
        "error": None,
    }
}


class YahooAdapterTests(unittest.TestCase):
    def test_fetches_normalizes_and_bounds_daily_chart(self):
        calls = []

        def fetch(url, timeout):
            calls.append((url, timeout))
            return json.dumps(CHART).encode()

        result = YahooAdapter(fetch=fetch).get_eod(
            "AAPL", date(2026, 7, 20), date(2026, 7, 21)
        )

        self.assertEqual(
            [row.date for row in result.candles],
            [date(2026, 7, 20), date(2026, 7, 21)],
        )
        self.assertEqual(result.actions, [])
        self.assertEqual(result.candles[0].open, Decimal("210.5"))
        self.assertEqual(result.candles[0].volume, 50123456)
        self.assertEqual(result.candles[0].source, "yahoo")
        self.assertEqual(calls[0][1], 10.0)
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(query["interval"], ["1d"])
        self.assertEqual(
            datetime.fromtimestamp(int(query["period1"][0]), UTC).date(),
            date(2026, 7, 20),
        )
        self.assertEqual(
            datetime.fromtimestamp(int(query["period2"][0]), UTC).date(),
            date(2026, 7, 22),
        )

    def test_skips_null_price_rows_and_allows_null_volume(self):
        chart = json.loads(json.dumps(CHART))
        quote = chart["chart"]["result"][0]["indicators"]["quote"][0]
        quote["open"][1] = None
        quote["volume"][2] = None

        result = YahooAdapter(
            fetch=lambda _url, _timeout: json.dumps(chart).encode()
        ).get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21))

        self.assertEqual([row.date for row in result.candles], [date(2026, 7, 21)])
        self.assertIsNone(result.candles[0].volume)

    def test_vendor_symbol_mapping_and_capability_are_phase_one_scoped(self):
        adapter = YahooAdapter()
        self.assertEqual(adapter.vendor_symbol("brk.b"), "BRK-B")
        self.assertEqual(adapter.capabilities(), {Capability.EOD_US})
        with self.assertRaises(CapabilityUnavailable):
            adapter.vendor_symbol("bad symbol")
        with self.assertRaises(CapabilityUnavailable):
            adapter.get_quote("AAPL")
        with self.assertRaises(CapabilityUnavailable):
            adapter.discover("AAPL")

    def test_provider_error_and_malformed_payload_are_explicit(self):
        provider_error = {
            "chart": {
                "result": None,
                "error": {"code": "Not Found", "description": "No data found"},
            }
        }
        with self.assertRaisesRegex(YahooError, "No data found"):
            YahooAdapter(
                fetch=lambda _url, _timeout: json.dumps(provider_error).encode()
            ).get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21))
        with self.assertRaises(YahooError):
            YahooAdapter(fetch=lambda _url, _timeout: b"not json").get_eod(
                "AAPL", date(2026, 7, 20), date(2026, 7, 21)
            )


if __name__ == "__main__":
    unittest.main()

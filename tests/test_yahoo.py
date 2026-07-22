from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
import json
from unittest.mock import patch
from urllib.error import HTTPError
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
        self.assertEqual(query["events"], ["div,splits"])
        self.assertEqual(
            datetime.fromtimestamp(int(query["period1"][0]), UTC).date(),
            date(2026, 7, 10),
        )
        self.assertEqual(
            datetime.fromtimestamp(int(query["period2"][0]), UTC).date(),
            date(2026, 7, 22),
        )

    def test_extracts_split_and_dividend_adjustment_factors(self):
        chart = json.loads(json.dumps(CHART))
        chart["chart"]["result"][0]["events"] = {
            "splits": {
                "split": {
                    "date": epoch("2026-07-21"),
                    "numerator": 4,
                    "denominator": 1,
                    "splitRatio": "4:1",
                }
            },
            "dividends": {
                "dividend": {
                    "date": epoch("2026-07-21"),
                    "amount": 1.25,
                }
            },
        }

        result = YahooAdapter(
            fetch=lambda _url, _timeout: json.dumps(chart).encode()
        ).get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21))

        self.assertEqual([action.type for action in result.actions], ["split", "dividend"])
        split, dividend = result.actions
        self.assertEqual(split.value, Decimal("4"))
        self.assertEqual(split.factor, Decimal("0.25"))
        self.assertEqual(dividend.value, Decimal("1.25"))
        self.assertEqual(dividend.reference_close, Decimal("213.25"))
        self.assertEqual(
            dividend.factor,
            (Decimal("213.25") - Decimal("1.25")) / Decimal("213.25"),
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

    def test_skips_provider_rows_with_inconsistent_ohlc_bounds(self):
        chart = json.loads(json.dumps(CHART))
        quote = chart["chart"]["result"][0]["indicators"]["quote"][0]
        quote["open"][2] = 207.0

        result = YahooAdapter(
            fetch=lambda _url, _timeout: json.dumps(chart).encode()
        ).get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21))

        self.assertEqual([row.date for row in result.candles], [date(2026, 7, 20)])

    def test_vendor_symbol_mapping_and_multi_market_capabilities(self):
        adapter = YahooAdapter()
        self.assertEqual(adapter.vendor_symbol("brk.b"), "BRK-B")
        self.assertEqual(adapter.vendor_symbol("infy.ns"), "INFY.NS")
        self.assertEqual(adapter.vendor_symbol("FX:EURUSD"), "EURUSD=X")
        self.assertEqual(adapter.vendor_symbol("X:BTC-USD"), "BTC-USD")
        self.assertEqual(
            adapter.capabilities(),
            {
                Capability.EOD_US,
                Capability.EOD_IN,
                Capability.EOD_FX,
                Capability.EOD_CRYPTO,
            },
        )
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

    def test_pre_listing_http_400_is_an_empty_success(self):
        payload = json.dumps(
            {
                "chart": {
                    "result": None,
                    "error": {
                        "code": "Bad Request",
                        "description": (
                            "Data doesn't exist for startDate = 1451606400, "
                            "endDate = 1483228800"
                        ),
                    },
                }
            }
        ).encode()
        error = HTTPError(
            "https://query1.finance.yahoo.com",
            400,
            "Bad Request",
            {},
            BytesIO(payload),
        )

        with patch("src.adapters.yahoo.urlopen", side_effect=error):
            result = YahooAdapter().get_eod(
                "ABNB", date(2016, 1, 1), date(2016, 12, 31)
            )

        self.assertEqual(result.candles, [])

    def test_other_http_errors_are_not_hidden(self):
        other_400 = HTTPError(
            "https://query1.finance.yahoo.com",
            400,
            "Bad Request",
            {},
            BytesIO(
                json.dumps(
                    {
                        "chart": {
                            "result": None,
                            "error": {
                                "code": "Bad Request",
                                "description": "Invalid symbol",
                            },
                        }
                    }
                ).encode()
            ),
        )
        with patch("src.adapters.yahoo.urlopen", side_effect=other_400):
            with self.assertRaises(HTTPError):
                YahooAdapter().get_eod(
                    "AAPL", date(2016, 1, 1), date(2016, 12, 31)
                )

        other_404 = HTTPError(
            "https://query1.finance.yahoo.com",
            404,
            "Not Found",
            {},
            BytesIO(b"not found"),
        )
        with patch("src.adapters.yahoo.urlopen", side_effect=other_404):
            with self.assertRaises(HTTPError):
                YahooAdapter().get_eod(
                    "AAPL", date(2016, 1, 1), date(2016, 12, 31)
                )
        other_404.close()


if __name__ == "__main__":
    unittest.main()

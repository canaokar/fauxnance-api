from datetime import UTC, date, datetime
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse
import unittest

from src.adapters.coingecko import CoinGeckoAdapter, CoinGeckoError
from src.shared.market_data import CapabilityUnavailable


def millis(day: str) -> int:
    return int(datetime.fromisoformat(f"{day}T00:00:00+00:00").timestamp() * 1000)


class CoinGeckoAdapterTests(unittest.TestCase):
    def test_fetches_daily_price_and_volume_with_demo_header(self):
        calls = []
        payload = {
            "prices": [[millis("2026-07-20"), 118000.5], [millis("2026-07-21"), 119250.25]],
            "total_volumes": [[millis("2026-07-20"), 123456789.9], [millis("2026-07-21"), 222222222.1]],
        }

        def fetch(url, timeout, headers):
            calls.append((url, timeout, headers))
            return json.dumps(payload).encode()

        result = CoinGeckoAdapter("demo-secret", fetch=fetch).get_eod(
            "X:BTC-USD",
            date(2026, 7, 20),
            date(2026, 7, 21),
            coin_id="bitcoin",
        )

        self.assertEqual([row.date for row in result.candles], [date(2026, 7, 20), date(2026, 7, 21)])
        self.assertEqual(result.candles[0].close, Decimal("118000.5"))
        self.assertEqual(result.candles[0].volume, 123456789)
        self.assertEqual(calls[0][2]["x-cg-demo-api-key"], "demo-secret")
        self.assertIn("/coins/bitcoin/market_chart/range", calls[0][0])
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(query["interval"], ["daily"])
        self.assertEqual(query["vs_currency"], ["usd"])

    def test_requires_crypto_coin_id_and_bounded_recent_range(self):
        adapter = CoinGeckoAdapter("key", fetch=lambda *_args: b"{}")
        with self.assertRaises(CapabilityUnavailable):
            adapter.get_eod("AAPL", date(2026, 7, 20), date(2026, 7, 21), coin_id="apple")
        with self.assertRaises(CapabilityUnavailable):
            adapter.get_eod("X:BTC-USD", date(2026, 7, 20), date(2026, 7, 21))
        with self.assertRaises(CapabilityUnavailable):
            adapter.get_eod("X:BTC-USD", date(2025, 1, 1), date(2026, 7, 21), coin_id="bitcoin")

    def test_malformed_or_nonpositive_prices_are_explicit(self):
        adapter = CoinGeckoAdapter(
            "key",
            fetch=lambda *_args: json.dumps(
                {"prices": [[millis("2026-07-21"), 0]], "total_volumes": []}
            ).encode(),
        )
        with self.assertRaises(CoinGeckoError):
            adapter.get_eod("X:BTC-USD", date(2026, 7, 20), date(2026, 7, 21), coin_id="bitcoin")


if __name__ == "__main__":
    unittest.main()

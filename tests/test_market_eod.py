from datetime import date
import unittest

from src.adapters.market_eod import EodSourcesUnavailable, MarketEodRouter
from src.shared.market_data import EodResult


class Guard:
    def __init__(self, available=True):
        self.available = available
        self.calls = []

    def try_acquire(self):
        self.calls.append("acquire")
        return self.available

    def record_success(self):
        self.calls.append("success")

    def record_failure(self):
        self.calls.append("failure")


class Source:
    def __init__(self, name, outcome=None):
        self.name = name
        self.outcome = outcome if outcome is not None else EodResult()
        self.calls = []

    def get_eod(self, symbol, start, end, **kwargs):
        self.calls.append((symbol, start, end, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class MarketEodRouterTests(unittest.TestCase):
    def setUp(self):
        self.yahoo = Source("yahoo")
        self.yahoo_guard = Guard()

    def test_market_and_purpose_choose_documented_source_order(self):
        frankfurter = Source("frankfurter")
        coin = Source("coingecko")
        router = MarketEodRouter(
            self.yahoo,
            self.yahoo_guard,
            frankfurter=frankfurter,
            frankfurter_guard=Guard(),
            coingecko=coin,
            coingecko_guard=Guard(),
        )

        router.get_eod(
            "FX:EURUSD", date(2026, 7, 20), date(2026, 7, 21),
            market="FX", purpose="scheduled",
        )
        router.get_eod(
            "X:BTC-USD", date(2026, 7, 20), date(2026, 7, 21),
            market="CRYPTO", purpose="scheduled", adapter_hints={"coinGeckoId": "bitcoin"},
        )
        router.get_eod(
            "X:BTC-USD", date(2020, 1, 1), date(2020, 12, 31),
            market="CRYPTO", purpose="backfill", adapter_hints={"coinGeckoId": "bitcoin"},
        )

        self.assertEqual(len(frankfurter.calls), 1)
        self.assertEqual(coin.calls[0][3], {"coin_id": "bitcoin"})
        self.assertEqual(len(coin.calls), 1)
        self.assertEqual(self.yahoo.calls[-1][0], "X:BTC-USD")

    def test_failure_falls_through_and_records_guard_state(self):
        frankfurter = Source("frankfurter", RuntimeError("down"))
        frank_guard = Guard()
        router = MarketEodRouter(
            self.yahoo,
            self.yahoo_guard,
            frankfurter=frankfurter,
            frankfurter_guard=frank_guard,
        )

        router.get_eod(
            "FX:EURUSD", date(2026, 7, 20), date(2026, 7, 21),
            market="FX", purpose="scheduled",
        )

        self.assertEqual(frank_guard.calls, ["acquire", "failure"])
        self.assertEqual(self.yahoo_guard.calls, ["acquire", "success"])

    def test_exhaustion_is_explicit(self):
        router = MarketEodRouter(self.yahoo, Guard(available=False))
        with self.assertRaises(EodSourcesUnavailable):
            router.get_eod(
                "INFY.NS", date(2026, 7, 20), date(2026, 7, 21),
                market="IN", purpose="scheduled",
            )


if __name__ == "__main__":
    unittest.main()

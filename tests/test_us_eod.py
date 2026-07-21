from datetime import date
from decimal import Decimal
import unittest

from src.adapters.us_eod import EodSourcesUnavailable, UsEodChain
from src.shared.market_data import Candle, EodResult


START = date(2026, 7, 20)
END = date(2026, 7, 21)


def result(source):
    return EodResult(
        candles=[
            Candle(
                date=END,
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=100,
                source=source,
            )
        ]
    )


class Source:
    def __init__(self, name, outcome):
        self.name = name
        self.outcome = outcome
        self.calls = []

    def get_eod(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class Guard:
    def __init__(self, available=True):
        self.available = available
        self.successes = 0
        self.failures = 0

    def try_acquire(self):
        return self.available

    def record_success(self):
        self.successes += 1

    def record_failure(self):
        self.failures += 1


class UsEodChainTests(unittest.TestCase):
    def test_yahoo_success_does_not_call_alpha_vantage(self):
        yahoo = Source("yahoo", result("yahoo"))
        alpha = Source("alpha_vantage", result("alpha_vantage"))
        yahoo_guard = Guard()
        alpha_guard = Guard()
        chain = UsEodChain(
            yahoo,
            yahoo_guard,
            alpha_vantage=alpha,
            alpha_vantage_guard=alpha_guard,
        )

        actual = chain.get_eod("AAPL", START, END)

        self.assertEqual(actual.candles[0].source, "yahoo")
        self.assertEqual(yahoo_guard.successes, 1)
        self.assertEqual(alpha.calls, [])

    def test_yahoo_failure_falls_back_to_configured_alpha_vantage(self):
        yahoo = Source("yahoo", RuntimeError("Yahoo unavailable"))
        alpha = Source("alpha_vantage", result("alpha_vantage"))
        yahoo_guard = Guard()
        alpha_guard = Guard()
        chain = UsEodChain(
            yahoo,
            yahoo_guard,
            alpha_vantage=alpha,
            alpha_vantage_guard=alpha_guard,
        )

        actual = chain.get_eod("AAPL", START, END)

        self.assertEqual(actual.candles[0].source, "alpha_vantage")
        self.assertEqual(yahoo_guard.failures, 1)
        self.assertEqual(alpha_guard.successes, 1)

    def test_guarded_source_is_skipped_and_exhaustion_is_explicit(self):
        yahoo = Source("yahoo", result("yahoo"))
        alpha = Source("alpha_vantage", RuntimeError("limit reached"))
        chain = UsEodChain(
            yahoo,
            Guard(available=False),
            alpha_vantage=alpha,
            alpha_vantage_guard=Guard(),
        )

        with self.assertRaisesRegex(
            EodSourcesUnavailable, "yahoo: unavailable.*alpha_vantage: limit reached"
        ):
            chain.get_eod("AAPL", START, END)
        self.assertEqual(yahoo.calls, [])

    def test_alpha_vantage_is_optional_but_requires_its_guard(self):
        chain = UsEodChain(Source("yahoo", result("yahoo")), Guard())
        self.assertEqual(
            chain.get_eod("AAPL", START, END).candles[0].source,
            "yahoo",
        )
        with self.assertRaises(ValueError):
            UsEodChain(
                Source("yahoo", result("yahoo")),
                Guard(),
                alpha_vantage=Source("alpha_vantage", result("alpha_vantage")),
            )


if __name__ == "__main__":
    unittest.main()

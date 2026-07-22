from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from src.api.quotes import QuoteResolver, QuoteUnavailable
from src.shared.market_data import Quote


NOW = datetime(2026, 7, 21, 16, tzinfo=UTC)


def quote(*, source="yahoo", price="232.71"):
    return Quote(
        price=Decimal(price),
        currency=None,
        change=Decimal("0.21"),
        change_percent=Decimal("0.09"),
        previous_close=Decimal("232.5"),
        as_of=NOW - timedelta(seconds=5),
        market_state="open",
        source=source,
    )


def cache_item(*, fetched_at=NOW - timedelta(seconds=30)):
    value = quote(source="finnhub")
    return {
        "quote": {
            "price": value.price,
            "currency": "USD",
            "change": value.change,
            "changePercent": value.change_percent,
            "previousClose": value.previous_close,
            "asOf": value.as_of.isoformat().replace("+00:00", "Z"),
            "marketState": value.market_state,
        },
        "src": value.source,
        "fetchedAt": fetched_at.isoformat().replace("+00:00", "Z"),
    }


class Repository:
    def __init__(self, cached=None):
        self.cached = cached
        self.puts = []

    def get_quote(self, symbol):
        return self.cached

    def put_quote(self, symbol, value, **kwargs):
        self.puts.append((symbol, value, kwargs))


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
        self.outcome = outcome if outcome is not None else quote(source=name)
        self.calls = []

    def get_quote(self, symbol):
        self.calls.append(symbol)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class QuoteResolverTests(unittest.TestCase):
    def test_fresh_cache_avoids_upstream_calls(self):
        repository = Repository(cache_item())
        yahoo = Source("yahoo")
        resolved = QuoteResolver(repository, yahoo, Guard()).resolve(
            "AAPL", {"currency": "USD"}, now=NOW
        )

        self.assertEqual((resolved.source, resolved.stale), ("cache", False))
        self.assertEqual(yahoo.calls, [])
        self.assertEqual(repository.puts, [])

    def test_us_falls_through_finnhub_to_yahoo_and_writes_cache(self):
        repository = Repository()
        finnhub = Source("finnhub", RuntimeError("down"))
        finnhub_guard = Guard()
        yahoo = Source("yahoo")
        yahoo_guard = Guard()
        resolver = QuoteResolver(
            repository,
            yahoo,
            yahoo_guard,
            finnhub=finnhub,
            finnhub_guard=finnhub_guard,
        )

        resolved = resolver.resolve("AAPL", {"currency": "USD"}, now=NOW)

        self.assertEqual(resolved.source, "upstream:yahoo")
        self.assertEqual(finnhub_guard.calls, ["acquire", "failure"])
        self.assertEqual(yahoo_guard.calls, ["acquire", "success"])
        self.assertEqual(repository.puts[0][0], "AAPL")
        self.assertEqual(repository.puts[0][1].currency, "USD")
        self.assertEqual(
            repository.puts[0][2]["expires_at"], NOW + timedelta(days=7)
        )

    def test_stale_cache_is_served_only_after_upstream_failure(self):
        repository = Repository(cache_item(fetched_at=NOW - timedelta(hours=1)))
        yahoo = Source("yahoo", RuntimeError("down"))
        resolved = QuoteResolver(repository, yahoo, Guard()).resolve(
            "INFY.NS", {"currency": "INR"}, now=NOW
        )

        self.assertEqual((resolved.source, resolved.stale), ("cache", True))
        self.assertEqual(resolved.quote.currency, "USD")

    def test_no_cache_and_exhausted_sources_is_unavailable(self):
        with self.assertRaises(QuoteUnavailable):
            QuoteResolver(Repository(), Source("yahoo"), Guard(False)).resolve(
                "AAPL", {"currency": "USD"}, now=NOW
            )

    def test_implausible_future_cache_is_not_served(self):
        repository = Repository(cache_item(fetched_at=NOW + timedelta(days=1)))
        with self.assertRaises(QuoteUnavailable):
            QuoteResolver(repository, Source("yahoo"), Guard(False)).resolve(
                "AAPL", {"currency": "USD"}, now=NOW
            )


if __name__ == "__main__":
    unittest.main()

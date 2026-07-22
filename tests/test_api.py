from datetime import UTC, date, datetime
from decimal import Decimal
import json
import unittest

from src.api.handler import ApiService, lambda_handler
from src.api.quotes import QuoteUnavailable, ResolvedQuote
from src.api.repository import IdentityRepository, MarketDataRepository
from src.shared.market_data import Candle, Quote
from src.shared.quota import QuotaExceeded, Usage


NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


class FakeDataRepository:
    def __init__(self):
        self.symbol = {
            "name": "Apple Inc.",
            "type": "equity",
            "exchange": "NASDAQ",
            "currency": "USD",
            "active": True,
            "coverage": {"eodFrom": "2016-07-21", "eodTo": "2026-07-20"},
        }
        self.chunks = []
        self.actions = []
        self.market_status = {"latestEod": "2026-07-20"}
        self.calls = []

    def get_symbol(self, symbol):
        self.calls.append(("symbol", symbol))
        return self.symbol

    def get_candle_chunks(self, symbol, start, end):
        self.calls.append(("candles", symbol, start, end))
        return self.chunks

    def get_actions(self, symbol):
        self.calls.append(("actions", symbol))
        return self.actions

    def get_recent_real_candles(self, symbol, *, before, limit=91):
        self.calls.append(("recent", symbol, before, limit))
        found = []
        for chunk in self.chunks:
            for row in chunk.get("candles", []):
                day = date.fromisoformat(row["d"])
                if day <= before:
                    found.append(
                        Candle(
                            date=day,
                            open=Decimal(str(row["o"])),
                            high=Decimal(str(row["h"])),
                            low=Decimal(str(row["l"])),
                            close=Decimal(str(row["c"])),
                            volume=int(row["v"]) if row.get("v") is not None else None,
                            source=str(row.get("src", "yahoo")),
                        )
                    )
        return sorted(found, key=lambda candle: candle.date)[-limit:]

    def get_market_status(self, market):
        self.calls.append(("market", market))
        return self.market_status


class FakeIdentityRepository:
    def get_usage_identity(self, key_id):
        return {"keyLabel": "team-3", "cohort": "Grad Batch"}


class FakeQuotaService:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def consume(self, key_id, daily_quota, *, at):
        self.calls.append((key_id, daily_quota, at))
        if self.error:
            raise self.error
        return Usage(27, daily_quota, datetime(2026, 7, 22, tzinfo=UTC))


class FakeQuoteResolver:
    def __init__(self):
        self.outcomes = {}
        self.calls = []

    def resolve(self, symbol, metadata, *, now):
        self.calls.append((symbol, metadata, now))
        outcome = self.outcomes.get(symbol)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or ResolvedQuote(
            Quote(
                price=Decimal("232.71"),
                currency=metadata.get("currency"),
                change=Decimal("0.21"),
                change_percent=Decimal("0.09"),
                previous_close=Decimal("232.5"),
                as_of=datetime(2026, 7, 21, 15, 42, 10, tzinfo=UTC),
                market_state="open",
                source="finnhub",
            ),
            source="upstream:finnhub",
            stale=False,
        )


def event(path, *, query=None, symbol=None, auth=True):
    request = {
        "rawPath": path,
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": query,
    }
    if symbol:
        request["pathParameters"] = {"symbol": symbol}
    if auth:
        request["requestContext"]["authorizer"] = {
            "lambda": {
                "keyId": "student-1",
                "keyType": "student",
                "cohortId": "cohort-1",
                "dailyQuota": "2000",
            }
        }
    return request


def decoded(response):
    return json.loads(response["body"])


class PublicApiTests(unittest.TestCase):
    def setUp(self):
        self.data = FakeDataRepository()
        self.identity = FakeIdentityRepository()
        self.quota = FakeQuotaService()
        self.quotes = FakeQuoteResolver()
        self.service = ApiService(
            self.data,
            self.identity,
            self.quota,
            quote_resolver=self.quotes,
            health_markets=("US",),
            clock=lambda: NOW,
        )

    def invoke(self, request):
        return lambda_handler(request, None, service=self.service)

    def test_health_is_unauthenticated_and_reports_freshness(self):
        response = self.invoke(event("/v1/health", auth=False))

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            decoded(response)["data"],
            {
                "status": "ok",
                "markets": [
                    {"market": "US", "latestEod": "2026-07-20", "stale": False}
                ],
            },
        )
        self.assertEqual(self.quota.calls, [])

    def test_health_degrades_when_market_status_is_missing(self):
        self.data.market_status = None
        response = self.invoke(event("/v1/health", auth=False))
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(decoded(response)["data"]["status"], "degraded")

    def test_health_remains_200_when_status_storage_is_unavailable(self):
        self.data.get_market_status = lambda _market: (_ for _ in ()).throw(
            RuntimeError("unavailable")
        )
        response = self.invoke(event("/v1/health", auth=False))
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(decoded(response)["data"]["status"], "degraded")

    def test_usage_includes_its_own_quota_increment(self):
        response = self.invoke(event("/v1/usage"))

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            decoded(response)["data"],
            {
                "keyLabel": "team-3",
                "cohort": "Grad Batch",
                "dailyQuota": 2000,
                "usedToday": 27,
                "resetsAt": "2026-07-22T00:00:00Z",
            },
        )
        self.assertEqual(self.quota.calls, [("student-1", 2000, NOW)])

    def test_symbol_detail_is_canonical_and_does_not_leak_storage_keys(self):
        response = self.invoke(event("/v1/symbols/aapl", symbol="aapl"))

        body = decoded(response)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["data"]["symbol"], "AAPL")
        self.assertEqual(body["data"]["coverage"]["eodTo"], "2026-07-20")
        self.assertNotIn("PK", body["data"])
        self.assertEqual(body["meta"]["source"], "stored")

    def test_missing_symbol_returns_documented_error_after_consuming_quota(self):
        self.data.symbol = None
        response = self.invoke(event("/v1/symbols/NOPE", symbol="NOPE"))

        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(decoded(response)["error"]["code"], "SYMBOL_NOT_FOUND")
        self.assertEqual(len(self.quota.calls), 1)

    def test_candles_are_trimmed_sorted_and_adjusted_at_read_time(self):
        self.data.chunks = [
            {
                "candles": [
                    {
                        "d": "2020-08-31",
                        "o": Decimal("125"),
                        "h": Decimal("130"),
                        "l": Decimal("124"),
                        "c": Decimal("129.04"),
                        "v": Decimal("225702700"),
                    },
                    {
                        "d": "2020-08-28",
                        "o": Decimal("500"),
                        "h": Decimal("505"),
                        "l": Decimal("495"),
                        "c": Decimal("499.23"),
                        "v": Decimal("187630000"),
                    },
                    {"d": "2020-08-27", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                ]
            }
        ]
        self.data.actions = [
            {"date": "2020-08-31", "type": "split", "factor": Decimal("0.25")}
        ]
        response = self.invoke(
            event(
                "/v1/candles/aapl",
                symbol="aapl",
                query={"from": "2020-08-28", "to": "2020-08-31"},
            )
        )

        body = decoded(response)
        self.assertEqual(response["statusCode"], 200)
        candles = body["data"]["candles"]
        self.assertEqual([row["date"] for row in candles], ["2020-08-28", "2020-08-31"])
        self.assertEqual(candles[0]["adjclose"], 124.8075)
        self.assertEqual(candles[1]["adjclose"], 129.04)
        self.assertEqual(candles[0]["synthetic"], False)
        self.assertEqual(body["meta"]["asOf"], "2020-08-31T00:00:00Z")

    def test_candle_defaults_are_one_year_inclusive(self):
        response = self.invoke(event("/v1/candles/AAPL", symbol="AAPL"))
        self.assertEqual(response["statusCode"], 200)
        self.assertIn(
            ("candles", "AAPL", date(2025, 7, 21), date(2026, 7, 21)),
            self.data.calls,
        )

    def test_single_quote_returns_source_staleness_and_percentage_points(self):
        response = self.invoke(event("/v1/quotes/AAPL", symbol="AAPL"))

        body = decoded(response)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["data"]["price"], 232.71)
        self.assertEqual(body["data"]["changePercent"], 0.09)
        self.assertEqual(body["meta"]["source"], "upstream:finnhub")
        self.assertEqual(body["meta"]["stale"], False)
        self.assertEqual(body["meta"]["asOf"], "2026-07-21T15:42:10Z")

    def test_quote_without_any_usable_source_returns_retryable_202(self):
        self.quotes.outcomes["AAPL"] = QuoteUnavailable("all sources down")
        response = self.invoke(event("/v1/quotes/AAPL", symbol="AAPL"))

        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(response["headers"]["Retry-After"], "60")
        self.assertEqual(decoded(response)["error"]["code"], "BACKFILL_IN_PROGRESS")

    def test_batch_quotes_mix_success_and_errors_for_one_quota_unit(self):
        original = self.data.get_symbol
        self.data.get_symbol = lambda symbol: None if symbol == "NOPE" else original(symbol)
        response = self.invoke(
            event(
                "/v1/quotes",
                query={"symbols": "aapl,NOPE,bad symbol"},
            )
        )

        body = decoded(response)
        self.assertEqual(response["statusCode"], 200)
        self.assertIn("quote", body["data"]["quotes"][0])
        self.assertEqual(body["data"]["quotes"][1]["error"]["code"], "SYMBOL_NOT_FOUND")
        self.assertEqual(body["data"]["quotes"][2]["error"]["code"], "SYMBOL_NOT_FOUND")
        self.assertEqual(len(self.quota.calls), 1)

    def test_batch_quote_request_validates_presence_and_limit(self):
        for query in ({}, {"symbols": ",".join(["AAPL"] * 26)}):
            with self.subTest(query=query):
                response = self.invoke(event("/v1/quotes", query=query))
                self.assertEqual(response["statusCode"], 400)
                self.assertEqual(decoded(response)["error"]["code"], "VALIDATION_ERROR")

    def test_invalid_interval_date_and_large_range_are_rejected(self):
        cases = [
            ({"interval": "1wk"}, "VALIDATION_ERROR"),
            ({"from": "yesterday"}, "VALIDATION_ERROR"),
            ({"from": "2016-07-20", "to": "2026-07-21"}, "RANGE_TOO_LARGE"),
        ]
        for query, code in cases:
            with self.subTest(query=query):
                response = self.invoke(
                    event("/v1/candles/AAPL", symbol="AAPL", query=query)
                )
                self.assertEqual(response["statusCode"], 400)
                self.assertEqual(decoded(response)["error"]["code"], code)

    def test_candles_fill_trailing_weekday_gaps_without_persisting(self):
        self.data.chunks = [
            {
                "candles": [
                    {
                        "d": "2026-07-20",
                        "o": Decimal("230"),
                        "h": Decimal("234"),
                        "l": Decimal("229"),
                        "c": Decimal("232.5"),
                        "v": 100,
                        "src": "yahoo",
                    }
                ]
            }
        ]
        response = self.invoke(
            event(
                "/v1/candles/AAPL",
                symbol="AAPL",
                query={"from": "2026-07-20", "to": "2026-07-21"},
            )
        )

        body = decoded(response)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["meta"]["source"], "mixed")
        self.assertEqual(
            [row["synthetic"] for row in body["data"]["candles"]],
            [False, True],
        )
        self.assertIsNone(body["data"]["candles"][1]["volume"])

    def test_exhausted_quota_returns_retry_after_without_data_reads(self):
        self.quota.error = QuotaExceeded(
            2000, datetime(2026, 7, 22, tzinfo=UTC), 43200
        )
        response = self.invoke(event("/v1/symbols/AAPL", symbol="AAPL"))

        self.assertEqual(response["statusCode"], 429)
        self.assertEqual(response["headers"]["Retry-After"], "43200")
        self.assertEqual(decoded(response)["error"]["code"], "RATE_LIMITED")
        self.assertEqual(self.data.calls, [])


class MarketDataRepositoryTests(unittest.TestCase):
    def test_month_chunk_query_uses_documented_partition_and_paginates(self):
        class Table:
            def __init__(self):
                self.calls = []

            def query(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {"Items": [{"SK": "EOD#2026-06"}], "LastEvaluatedKey": {"x": 1}}
                return {"Items": [{"SK": "EOD#2026-07"}]}

        table = Table()
        items = MarketDataRepository(table).get_candle_chunks(
            "AAPL", date(2026, 6, 1), date(2026, 7, 21)
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(
            table.calls[0]["ExpressionAttributeValues"],
            {":pk": "SYM#AAPL", ":start": "EOD#2026-06", ":end": "EOD#2026-07"},
        )
        self.assertEqual(table.calls[1]["ExclusiveStartKey"], {"x": 1})

    def test_quote_cache_write_uses_documented_item_and_cleanup_ttl(self):
        class Table:
            def __init__(self):
                self.item = None

            def put_item(self, *, Item):
                self.item = Item

        table = Table()
        value = Quote(
            price=Decimal("232.71"),
            currency="USD",
            change=Decimal("0.21"),
            change_percent=Decimal("0.09"),
            previous_close=Decimal("232.5"),
            as_of=datetime(2026, 7, 21, 15, 42, 10, tzinfo=UTC),
            market_state="open",
            source="finnhub",
        )
        MarketDataRepository(table).put_quote(
            "AAPL",
            value,
            fetched_at=NOW,
            expires_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        )

        self.assertEqual((table.item["PK"], table.item["SK"]), ("SYM#AAPL", "QUOTE"))
        self.assertEqual(table.item["quote"]["price"], Decimal("232.71"))
        self.assertEqual(table.item["fetchedAt"], "2026-07-21T12:00:00Z")
        self.assertEqual(
            table.item["expiresAt"],
            int(datetime(2026, 7, 28, 12, tzinfo=UTC).timestamp()),
        )


class IdentityRepositoryTests(unittest.TestCase):
    def test_usage_identity_follows_key_id_hash_and_cohort_lookups(self):
        class Table:
            def __init__(self):
                self.calls = []

            def get_item(self, **kwargs):
                self.calls.append(kwargs)
                key = kwargs["Key"]
                if key["PK"] == "KEYID#student-1":
                    return {"Item": {"keyHash": "abc", "cohortId": "cohort-1"}}
                if key["PK"] == "KEY#abc":
                    return {"Item": {"label": "team-3", "cohortId": "cohort-1"}}
                return {"Item": {"name": "Grad Batch"}}

        table = Table()
        identity = IdentityRepository(table).get_usage_identity("student-1")

        self.assertEqual(identity, {"keyLabel": "team-3", "cohort": "Grad Batch"})
        self.assertEqual(
            [call["Key"] for call in table.calls],
            [
                {"PK": "KEYID#student-1", "SK": "META"},
                {"PK": "KEY#abc", "SK": "META"},
                {"PK": "COHORT#cohort-1", "SK": "META"},
            ],
        )
        self.assertTrue(all(call["ConsistentRead"] for call in table.calls))


if __name__ == "__main__":
    unittest.main()

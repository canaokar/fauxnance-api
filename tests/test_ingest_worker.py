from datetime import date
from decimal import Decimal
import json
import unittest

from src.ingest.handler import IngestWorker, _read_parameter, handler
from src.shared.market_data import Candle, EodResult


def candle(day="2026-07-21", *, low="9", open_="10", close="10.5", high="11"):
    return Candle(
        date=date.fromisoformat(day),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=100,
        source="yahoo",
    )


def eod_body(symbol="AAPL"):
    return json.dumps(
        {
            "v": 1,
            "kind": "eod_batch",
            "market": "US",
            "symbol": symbol,
            "from": "2026-07-13",
            "to": "2026-07-21",
        }
    )


class Source:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def get_eod(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        outcome = self.outcomes[symbol]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Repository:
    def __init__(self):
        self.calls = []

    def write_candles(self, symbol, candles):
        self.calls.append(("write", symbol, candles))

    def advance_symbol_coverage(self, symbol, first, last):
        self.calls.append(("coverage", symbol, first, last))

    def advance_market_status(self, market, latest):
        self.calls.append(("market", market, latest))

    def complete_backfill_work(self, job_id, symbol, year):
        self.calls.append(("complete", job_id, symbol, year))


class ParameterNotFound(Exception):
    response = {"Error": {"Code": "ParameterNotFound"}}


class Ssm:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def get_parameter(self, **_request):
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


class IngestWorkerTests(unittest.TestCase):
    def test_sqs_handler_reports_only_failed_record_identifiers(self):
        source = Source(
            {
                "AAPL": EodResult(candles=[candle()]),
                "MSFT": RuntimeError("upstream unavailable"),
            }
        )
        repository = Repository()
        worker = IngestWorker(source, repository)

        response = handler(
            {
                "Records": [
                    {"messageId": "ok-1", "body": eod_body("AAPL")},
                    {"messageId": "retry-2", "body": eod_body("MSFT")},
                ]
            },
            None,
            worker=worker,
        )

        self.assertEqual(
            response, {"batchItemFailures": [{"itemIdentifier": "retry-2"}]}
        )
        self.assertEqual(
            source.calls[0],
            ("AAPL", date(2026, 7, 13), date(2026, 7, 21)),
        )
        self.assertIn(
            ("coverage", "AAPL", date(2026, 7, 21), date(2026, 7, 21)),
            repository.calls,
        )
        self.assertIn(("market", "US", date(2026, 7, 21)), repository.calls)

    def test_empty_backfill_completes_without_candle_or_coverage_writes(self):
        repository = Repository()
        worker = IngestWorker(Source({"AAPL": EodResult()}), repository)
        body = json.dumps(
            {
                "v": 1,
                "kind": "backfill_year",
                "jobId": "job_123",
                "symbol": "AAPL",
                "year": 2000,
            }
        )

        worker.process(body)

        self.assertEqual(repository.calls, [("complete", "job_123", "AAPL", 2000)])

    def test_intrinsically_invalid_candle_retries_without_persistence(self):
        repository = Repository()
        invalid = candle(low="12", open_="10", close="10.5", high="11")
        worker = IngestWorker(
            Source({"AAPL": EodResult(candles=[invalid])}), repository
        )

        response = handler(
            {"Records": [{"messageId": "bad-candle", "body": eod_body()}]},
            None,
            worker=worker,
        )

        self.assertEqual(
            response, {"batchItemFailures": [{"itemIdentifier": "bad-candle"}]}
        )
        self.assertEqual(repository.calls, [])

    def test_missing_optional_alpha_parameter_keeps_yahoo_available(self):
        self.assertIsNone(
            _read_parameter(Ssm(error=ParameterNotFound()), "/missing/key")
        )
        self.assertEqual(_read_parameter(Ssm(" secret "), "/key"), "secret")


if __name__ == "__main__":
    unittest.main()

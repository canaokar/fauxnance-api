from datetime import date
import json
import unittest

from src.ingest.messages import (
    BackfillYearMessage,
    EodBatchMessage,
    InvalidIngestMessage,
    parse_message,
)


class IngestMessageTests(unittest.TestCase):
    def test_parses_one_symbol_eod_batch_with_inclusive_bounds(self):
        message = parse_message(
            json.dumps(
                {
                    "v": 1,
                    "kind": "eod_batch",
                    "market": "US",
                    "symbol": "BRK.B",
                    "from": "2026-07-13",
                    "to": "2026-07-21",
                }
            )
        )

        self.assertEqual(
            message,
            EodBatchMessage(
                symbol="BRK.B",
                start=date(2026, 7, 13),
                end=date(2026, 7, 21),
            ),
        )

    def test_parses_symbol_year_backfill(self):
        message = parse_message(
            '{"v":1,"kind":"backfill_year","jobId":"job_123",'
            '"symbol":"AAPL","year":2020}'
        )

        self.assertIsInstance(message, BackfillYearMessage)
        self.assertEqual(message.start, date(2020, 1, 1))
        self.assertEqual(message.end, date(2020, 12, 31))

    def test_rejects_noncanonical_or_nonexact_contracts(self):
        invalid = [
            "not json",
            "[]",
            '{"v":2,"kind":"eod_batch"}',
            (
                '{"v":1,"kind":"eod_batch","market":"US",'
                '"symbols":["AAPL"],"from":"2026-07-01","to":"2026-07-21"}'
            ),
            (
                '{"v":1,"kind":"eod_batch","market":"US","symbol":"aapl",'
                '"from":"2026-07-01","to":"2026-07-21"}'
            ),
            (
                '{"v":1,"kind":"eod_batch","market":"US","symbol":"AAPL",'
                '"from":"2026-07-22","to":"2026-07-21"}'
            ),
            (
                '{"v":1,"kind":"backfill_year","jobId":"bad job",'
                '"symbol":"AAPL","year":2020}'
            ),
            (
                '{"v":1,"kind":"backfill_year","jobId":"job_1",'
                '"symbol":"AAPL","year":"2020"}'
            ),
        ]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(InvalidIngestMessage):
                parse_message(body)


if __name__ == "__main__":
    unittest.main()

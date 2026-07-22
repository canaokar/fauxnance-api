from datetime import UTC, datetime
from decimal import Decimal
import json
import unittest

from src.ingest.backfill import BackfillJobs
from src.ingest.messages import BackfillYearMessage, parse_message
from src.ingest.universe import Universe


NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class SeedRepository:
    def __init__(self):
        self.calls = []

    def seed_symbol(self, metadata, *, market):
        self.calls.append((metadata["symbol"], market))


class ControlTable:
    def __init__(self, *, page_size=100):
        self.items = {}
        self.put_calls = []
        self.query_calls = []
        self.page_size = page_size

    def put_item(self, *, Item, **request):
        self.put_calls.append({"Item": Item, **request})
        key = (Item["PK"], Item["SK"])
        if key in self.items and request.get("ConditionExpression"):
            raise ConditionalFailure()
        self.items[key] = Item

    def get_item(self, *, Key, **_request):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def query(self, **request):
        self.query_calls.append(request)
        pk = request["ExpressionAttributeValues"][":pk"]
        items = sorted(
            (
                item
                for (item_pk, sk), item in self.items.items()
                if item_pk == pk and sk.startswith("WORK#")
            ),
            key=lambda item: item["SK"],
        )
        start = self.page_size if "ExclusiveStartKey" in request else 0
        page = items[start : start + self.page_size]
        response = {"Items": page}
        if start + self.page_size < len(items):
            response["LastEvaluatedKey"] = {"PK": pk, "SK": page[-1]["SK"]}
        return response


class FakeSqs:
    def __init__(self):
        self.calls = []

    def send_message_batch(self, **request):
        self.calls.append(request)
        return {"Successful": [{"Id": item["Id"]} for item in request["Entries"]]}


def universe():
    symbols = tuple(
        {
            "symbol": symbol,
            "name": symbol,
            "type": "equity",
            "exchange": "US",
            "currency": "USD",
        }
        for symbol in ("MSFT", "AAPL")
    )
    return Universe(1, "test-us-v1", "US", symbols)


class BackfillJobsTests(unittest.TestCase):
    def test_creates_conditional_job_and_work_items_after_seeding_symbols(self):
        repository = SeedRepository()
        control = ControlTable()
        jobs = BackfillJobs(
            repository,
            control,
            FakeSqs(),
            "queue-url",
            clock=lambda: NOW,
        )

        job_id = jobs.create_job(
            universe(), from_year=2025, to_year=2026, job_id="job_test"
        )

        self.assertEqual(job_id, "job_test")
        self.assertEqual(repository.calls, [("MSFT", "US"), ("AAPL", "US")])
        meta = control.items[("JOB#job_test", "META")]
        self.assertEqual(meta["total"], 4)
        self.assertEqual(meta["completed"], 0)
        work = [item for item in control.items.values() if item["SK"].startswith("WORK#")]
        self.assertEqual(len(work), 4)
        self.assertTrue(all(item["state"] == "pending" for item in work))
        self.assertTrue(
            all(
                call["ConditionExpression"] == "attribute_not_exists(PK)"
                for call in control.put_calls
            )
        )

    def test_resume_paginates_and_enqueues_only_noncompleted_work(self):
        control = ControlTable(page_size=2)
        sqs = FakeSqs()
        jobs = BackfillJobs(SeedRepository(), control, sqs, "queue-url")
        control.items[("JOB#job_resume", "META")] = {
            "PK": "JOB#job_resume",
            "SK": "META",
            "state": "running",
        }
        for index, symbol in enumerate(("AAPL", "MSFT", "NVDA")):
            control.items[("JOB#job_resume", f"WORK#{symbol}#YEAR#2025")] = {
                "PK": "JOB#job_resume",
                "SK": f"WORK#{symbol}#YEAR#2025",
                "symbol": symbol,
                "year": Decimal("2025"),
                "state": "completed" if index == 1 else "pending",
            }

        count = jobs.enqueue_pending("job_resume")

        self.assertEqual(count, 2)
        self.assertEqual(len(control.query_calls), 2)
        bodies = [entry["MessageBody"] for entry in sqs.calls[0]["Entries"]]
        messages = [parse_message(body) for body in bodies]
        self.assertTrue(all(isinstance(item, BackfillYearMessage) for item in messages))
        self.assertEqual([item.symbol for item in messages], ["AAPL", "NVDA"])
        self.assertEqual(
            set(json.loads(bodies[0])),
            {"v", "kind", "jobId", "market", "symbol", "year"},
        )
        self.assertTrue(all(json.loads(body)["v"] == 2 for body in bodies))
        self.assertTrue(all(item.market == "US" for item in messages))

    def test_recreating_a_partial_job_adds_only_missing_work(self):
        control = ControlTable()
        jobs = BackfillJobs(SeedRepository(), control, FakeSqs(), "queue-url")
        jobs.create_job(universe(), from_year=2025, to_year=2026, job_id="job_resume")
        del control.items[("JOB#job_resume", "WORK#MSFT#YEAR#2026")]

        jobs.create_job(universe(), from_year=2025, to_year=2026, job_id="job_resume")

        work = [item for item in control.items.values() if item["SK"].startswith("WORK#")]
        self.assertEqual(len(work), 4)

        with self.assertRaisesRegex(ValueError, "different parameters"):
            jobs.create_job(
                universe(), from_year=2024, to_year=2026, job_id="job_resume"
            )

    def test_missing_job_and_invalid_year_range_are_rejected(self):
        jobs = BackfillJobs(SeedRepository(), ControlTable(), FakeSqs(), "queue")
        with self.assertRaises(ValueError):
            jobs.create_job(universe(), from_year=2026, to_year=2025)
        with self.assertRaises(LookupError):
            jobs.enqueue_pending("job_missing")


if __name__ == "__main__":
    unittest.main()

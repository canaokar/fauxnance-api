from datetime import UTC, date, datetime
import unittest

from src.ingest.repository import DynamoDBIngestRepository


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class TransactionCancelled(Exception):
    response = {"Error": {"Code": "TransactionCanceledException"}}


class CoverageTable:
    def __init__(self):
        self.coverage = {"eodFrom": "2020-01-02", "eodTo": "2025-12-31"}
        self.calls = []

    def update_item(self, **request):
        self.calls.append(request)
        field = request.get("ExpressionAttributeNames", {}).get("#field")
        if not field:
            return {}
        value = request["ExpressionAttributeValues"][":value"]
        current = self.coverage[field]
        should_write = value < current if field == "eodFrom" else value > current
        if not should_write:
            raise ConditionalFailure()
        self.coverage[field] = value
        return {}


class TransactionClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def transact_write_items(self, **request):
        self.calls.append(request)
        if self.error:
            raise self.error


class ControlTable:
    name = "fauxnance-dev-control"

    def __init__(self, state=None):
        self.state = state

    def get_item(self, **_request):
        return {"Item": {"state": self.state}} if self.state else {}


class IngestRepositoryTests(unittest.TestCase):
    def test_symbol_coverage_only_expands(self):
        table = CoverageTable()
        repository = DynamoDBIngestRepository(table)

        repository.advance_symbol_coverage(
            "AAPL", date(2019, 1, 2), date(2026, 7, 21)
        )
        repository.advance_symbol_coverage(
            "AAPL", date(2021, 1, 2), date(2024, 7, 21)
        )

        self.assertEqual(
            table.coverage,
            {"eodFrom": "2019-01-02", "eodTo": "2026-07-21"},
        )

    def test_backfill_completion_updates_work_and_job_in_one_transaction(self):
        client = TransactionClient()
        repository = DynamoDBIngestRepository(
            object(), ControlTable(), transaction_client=client
        )

        changed = repository.complete_backfill_work(
            "job_123",
            "AAPL",
            2020,
            now=datetime(2026, 7, 21, 12, tzinfo=UTC),
        )

        self.assertTrue(changed)
        writes = client.calls[0]["TransactItems"]
        self.assertEqual(len(writes), 2)
        self.assertIn("#state = :completed", writes[0]["Update"]["UpdateExpression"])
        self.assertIn("ADD #completed :one", writes[1]["Update"]["UpdateExpression"])

    def test_duplicate_completed_work_does_not_increment_job_again(self):
        client = TransactionClient(TransactionCancelled())
        repository = DynamoDBIngestRepository(
            object(), ControlTable("completed"), transaction_client=client
        )

        changed = repository.complete_backfill_work("job_123", "AAPL", 2020)

        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()

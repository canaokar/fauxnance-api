from datetime import UTC, date, datetime
import unittest

from src.ingest.repository import DynamoDBIngestRepository


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class TransactionCancelled(Exception):
    response = {"Error": {"Code": "TransactionCanceledException"}}


class MissingDocumentPath(Exception):
    response = {
        "Error": {
            "Code": "ValidationException",
            "Message": "The document path provided in the update expression is invalid",
        }
    }


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


class MissingCoverageTable:
    def __init__(self):
        self.coverage = None
        self.calls = []

    def update_item(self, **request):
        self.calls.append(request)
        expression = request["UpdateExpression"]
        if "if_not_exists" in expression:
            self.coverage = {}
            return {}
        if self.coverage is None:
            raise MissingDocumentPath()
        field = request["ExpressionAttributeNames"]["#field"]
        self.coverage[field] = request["ExpressionAttributeValues"][":value"]
        return {}


class SeedTable:
    def __init__(self):
        self.puts = []

    def put_item(self, **request):
        self.puts.append(request)
        return {}


class ExistingSeedTable(SeedTable):
    def __init__(self):
        super().__init__()
        self.updates = []

    def put_item(self, **request):
        self.puts.append(request)
        if len(self.puts) == 1:
            raise ConditionalFailure()
        return {}

    def update_item(self, **request):
        self.updates.append(request)
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

    def __init__(self, state=None, job=None):
        self.state = state
        self.job = job
        self.updates = []

    def get_item(self, *, Key, **_request):
        if Key["SK"] == "META" and self.job is not None:
            return {"Item": self.job}
        return {"Item": {"state": self.state}} if self.state else {}

    def update_item(self, **request):
        self.updates.append(request)
        return {}


class IngestRepositoryTests(unittest.TestCase):
    def test_seed_symbol_registers_metadata_without_fake_coverage(self):
        table = SeedTable()
        repository = DynamoDBIngestRepository(table)

        repository.seed_symbol(
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "type": "equity",
                "exchange": "US",
                "currency": "USD",
            },
            market="US",
        )

        self.assertEqual(len(table.puts), 2)
        meta = table.puts[0]["Item"]
        self.assertEqual((meta["PK"], meta["SK"]), ("SYM#AAPL", "META"))
        self.assertEqual(meta["coverage"], {})
        self.assertEqual(table.puts[1]["Item"]["PK"], "SYMBOLS")

    def test_seed_symbol_repairs_missing_coverage_map_on_existing_metadata(self):
        table = ExistingSeedTable()
        repository = DynamoDBIngestRepository(table)

        repository.seed_symbol(
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "type": "equity",
                "exchange": "US",
                "currency": "USD",
            },
            market="US",
        )

        refresh = table.updates[0]
        self.assertIn(
            "#coverage = if_not_exists(#coverage, :emptyCoverage)",
            refresh["UpdateExpression"],
        )
        self.assertEqual(refresh["ExpressionAttributeValues"][":emptyCoverage"], {})

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

    def test_symbol_coverage_repairs_legacy_missing_parent_map(self):
        table = MissingCoverageTable()
        repository = DynamoDBIngestRepository(table)

        repository.advance_symbol_coverage(
            "AAPL", date(2019, 1, 2), date(2026, 7, 21)
        )

        self.assertEqual(
            table.coverage,
            {"eodFrom": "2019-01-02", "eodTo": "2026-07-21"},
        )
        self.assertEqual(len(table.calls), 4)

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

    def test_final_work_marks_the_job_meta_completed(self):
        client = TransactionClient()
        control = ControlTable(job={"completed": 2, "total": 2, "state": "running"})
        repository = DynamoDBIngestRepository(
            object(), control, transaction_client=client
        )

        repository.complete_backfill_work(
            "job_123",
            "AAPL",
            2020,
            now=datetime(2026, 7, 21, 12, tzinfo=UTC),
        )

        self.assertEqual(len(control.updates), 1)
        request = control.updates[0]
        self.assertEqual(request["Key"], {"PK": "JOB#job_123", "SK": "META"})
        self.assertEqual(
            request["ExpressionAttributeValues"][":completedState"],
            "completed",
        )
        self.assertIn("#completed >= :total", request["ConditionExpression"])


if __name__ == "__main__":
    unittest.main()

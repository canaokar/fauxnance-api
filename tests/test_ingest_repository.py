from datetime import UTC, date, datetime
from decimal import Decimal
import json
import unittest

from src.ingest.repository import DynamoDBIngestRepository
from src.shared.market_data import CorporateAction


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class TransactionCancelled(Exception):
    response = {"Error": {"Code": "TransactionCanceledException"}}


class TransactionConflict(Exception):
    response = {
        "Error": {"Code": "TransactionCanceledException"},
        "CancellationReasons": [
            {"Code": "None"},
            {"Code": "TransactionConflict"},
        ],
    }


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


class ActionTable:
    def __init__(self):
        self.item = None
        self.puts = []

    def get_item(self, **_request):
        return {"Item": self.item} if self.item else {}

    def put_item(self, **request):
        self.puts.append(request)
        self.item = request["Item"]
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


class ConflictOnceClient(TransactionClient):
    def transact_write_items(self, **request):
        self.calls.append(request)
        if len(self.calls) == 1:
            raise TransactionConflict()


class ConflictClient(TransactionClient):
    def transact_write_items(self, **request):
        self.calls.append(request)
        raise TransactionConflict()


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
    def test_action_merge_updates_corrections_and_preserves_other_events(self):
        table = ActionTable()
        table.item = {
            "PK": "SYM#AAPL",
            "SK": "ADJ",
            "revision": 3,
            "actions": [
                {
                    "date": "2026-05-01",
                    "type": "dividend",
                    "value": 1,
                    "factor": Decimal("0.99"),
                    "referenceClose": 100,
                },
                {
                    "date": "2020-08-31",
                    "type": "split",
                    "value": 4,
                    "factor": Decimal("0.25"),
                },
            ],
        }
        repository = DynamoDBIngestRepository(table)

        repository.write_actions(
            "AAPL",
            [
                CorporateAction(
                    date=date(2026, 5, 1),
                    type="dividend",
                    value=Decimal("1.1"),
                    factor=Decimal("0.989"),
                    reference_close=Decimal("100"),
                )
            ],
        )

        written = table.puts[0]
        self.assertEqual(written["ExpressionAttributeValues"], {":expected": 3})
        self.assertEqual(written["Item"]["revision"], 4)
        self.assertEqual(len(written["Item"]["actions"]), 2)
        self.assertEqual(written["Item"]["actions"][0]["value"], Decimal("1.1"))

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
        self.assertEqual(
            writes[0]["Update"]["Key"],
            {"PK": "JOB#job_123", "SK": "WORK#AAPL#YEAR#2020"},
        )
        self.assertEqual(
            writes[1]["Update"]["ExpressionAttributeValues"][":one"], 1
        )

    def test_resource_client_serializes_transaction_values_exactly_once(self):
        import boto3

        class RequestCaptured(Exception):
            pass

        dynamodb = boto3.resource(
            "dynamodb",
            region_name="eu-west-2",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        control = dynamodb.Table("fauxnance-dev-control")
        captured = {}

        def capture_request(model, params, **_kwargs):
            del model
            captured.update(json.loads(params["body"]))
            raise RequestCaptured()

        control.meta.client.meta.events.register_first(
            "before-call.dynamodb.TransactWriteItems", capture_request
        )
        repository = DynamoDBIngestRepository(object(), control)

        with self.assertRaises(RequestCaptured):
            repository.complete_backfill_work(
                "job_123",
                "AAPL",
                2020,
                now=datetime(2026, 7, 21, 12, tzinfo=UTC),
            )

        writes = captured["TransactItems"]
        self.assertEqual(writes[0]["Update"]["Key"]["PK"], {"S": "JOB#job_123"})
        self.assertEqual(
            writes[1]["Update"]["ExpressionAttributeValues"][":one"],
            {"N": "1"},
        )

    def test_duplicate_completed_work_does_not_increment_job_again(self):
        client = TransactionClient(TransactionCancelled())
        repository = DynamoDBIngestRepository(
            object(), ControlTable("completed"), transaction_client=client
        )

        changed = repository.complete_backfill_work("job_123", "AAPL", 2020)

        self.assertFalse(changed)

    def test_transaction_conflict_retries_inside_the_worker(self):
        client = ConflictOnceClient()
        pauses = []
        repository = DynamoDBIngestRepository(
            object(),
            ControlTable(),
            transaction_client=client,
            pause=pauses.append,
        )

        changed = repository.complete_backfill_work("job_123", "AAPL", 2020)

        self.assertTrue(changed)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(pauses), 1)

    def test_transaction_conflict_retries_are_bounded(self):
        client = ConflictClient()
        pauses = []
        repository = DynamoDBIngestRepository(
            object(),
            ControlTable(),
            transaction_client=client,
            max_merge_attempts=3,
            pause=pauses.append,
        )

        with self.assertRaises(TransactionConflict):
            repository.complete_backfill_work("job_123", "AAPL", 2020)

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(pauses), 2)

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

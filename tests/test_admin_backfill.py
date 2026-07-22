from __future__ import annotations

from datetime import UTC, date, datetime
import json
from pathlib import Path
import unittest

from src.admin.coordinator import (
    BackfillCoordinator,
    DynamoCoordinatorRepository,
    handler as coordinator_handler,
)
from src.admin.handler import AdminService, LambdaCoordinatorInvoker, lambda_handler
from src.admin.repository import DynamoAdminRepository
from src.ingest.handler import IngestWorker, handler as ingest_handler
from src.ingest.messages import BackfillYearMessage, parse_message
from src.ingest.repository import DynamoDBIngestRepository


NOW = datetime(2026, 7, 22, 12, tzinfo=UTC)
UNIVERSE_ROOT = Path(__file__).resolve().parents[1] / "data" / "universes"


class AdminRepository:
    def __init__(self):
        self.seeded = []
        self.jobs = {}
        self.work = {}
        self.symbols = {
            "AAPL": {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "type": "equity",
                "exchange": "US",
                "currency": "USD",
                "market": "US",
                "active": True,
            },
            "INFY.NS": {
                "symbol": "INFY.NS",
                "name": "Infosys Limited",
                "type": "equity",
                "exchange": "NSE",
                "currency": "INR",
                "market": "IN",
                "active": True,
            },
        }

    def seed_symbols(self, symbols):
        self.seeded.extend(dict(item) for item in symbols)

    def get_symbol(self, symbol):
        return self.symbols.get(symbol)

    def create_backfill_job(self, item):
        self.jobs[item["jobId"]] = dict(item)

    def get_backfill_job(self, job_id):
        return self.jobs.get(job_id)

    def record_backfill_dispatch_error(self, job_id, *, error, updated_at):
        self.jobs[job_id].update(
            dispatchError=error, dispatchErrorAt=updated_at, updatedAt=updated_at
        )

    def backfill_failures(self, job_id, *, limit):
        items = [
            item
            for item in self.work.get(job_id, [])
            if item.get("state") == "failed"
        ]
        return items[:limit], len(items) > limit


class Invoker:
    def __init__(self):
        self.jobs = []

    def invoke(self, job_id):
        self.jobs.append(job_id)


def admin_event(method, path, *, body=None, path_parameters=None):
    event = {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "lambda": {"keyId": "admin-1", "keyType": "admin"}
            },
        },
    }
    if body is not None:
        event["body"] = json.dumps(body)
    if path_parameters:
        event["pathParameters"] = path_parameters
    return event


def decoded(response):
    return json.loads(response["body"])


class AdminMarketOperationsTests(unittest.TestCase):
    def setUp(self):
        self.repository = AdminRepository()
        self.invoker = Invoker()
        identifiers = iter(["job-one", "job-two", "job-three"])
        self.service = AdminService(
            self.repository,
            stage="dev",
            clock=lambda: NOW,
            identifier=lambda _prefix: next(identifiers),
            coordinator=self.invoker,
        )

    def invoke(self, request):
        return lambda_handler(request, None, service=self.service)

    def test_register_symbols_accepts_bounded_metadata_schema(self):
        response = self.invoke(
            admin_event(
                "POST",
                "/v1/admin/symbols",
                body={
                    "symbols": [
                        {
                            "symbol": "INFY.NS",
                            "name": " Infosys Limited ",
                            "type": "equity",
                            "exchange": "NSE",
                            "currency": "INR",
                            "adapterHints": {"yahooSymbol": "INFY.NS"},
                        },
                        {
                            "symbol": "X:BTC-USD",
                            "name": "Bitcoin",
                            "type": "crypto",
                            "exchange": "CRYPTO",
                            "currency": "USD",
                        },
                    ]
                },
            )
        )

        self.assertEqual(response["statusCode"], 201)
        self.assertEqual([item["market"] for item in self.repository.seeded], ["IN", "CRYPTO"])
        self.assertEqual(self.repository.seeded[0]["name"], "Infosys Limited")
        self.assertEqual(decoded(response)["data"]["symbols"][1]["symbol"], "X:BTC-USD")

    def test_register_symbols_rejects_noncanonical_duplicates_and_market_mismatch(self):
        cases = [
            {"symbols": [{"symbol": "infy.ns", "name": "Infosys", "type": "equity", "exchange": "NSE", "currency": "INR"}]},
            {"symbols": [{"symbol": "INFY.NS", "name": "Infosys", "type": "equity", "exchange": "BSE", "currency": "INR"}]},
            {"symbols": [
                {"symbol": "AAPL", "name": "Apple", "type": "equity", "exchange": "US", "currency": "USD"},
                {"symbol": "AAPL", "name": "Apple", "type": "equity", "exchange": "US", "currency": "USD"},
            ]},
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.invoke(admin_event("POST", "/v1/admin/symbols", body=body))
                self.assertEqual(response["statusCode"], 400)

    def test_named_backfill_is_persisted_before_async_202(self):
        response = self.invoke(
            admin_event(
                "POST",
                "/v1/admin/ingest/backfill",
                body={
                    "universe": "india-phase3-v1",
                    "from": "2025-03-15",
                    "to": "2026-04-20",
                },
            )
        )

        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(decoded(response)["data"], {"jobId": "job-one", "state": "preparing"})
        self.assertEqual(self.invoker.jobs, ["job-one"])
        job = self.repository.jobs["job-one"]
        self.assertEqual(job["state"], "preparing")
        self.assertEqual(job["params"]["universe"], "india-phase3-v1")

    def test_custom_backfill_canonicalizes_registered_symbols(self):
        response = self.invoke(
            admin_event(
                "POST",
                "/v1/admin/ingest/backfill",
                body={
                    "symbols": ["aapl", "INFY.NS"],
                    "from": "2026-01-01",
                    "to": "2026-07-22",
                },
            )
        )
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(self.repository.jobs["job-one"]["params"]["symbols"], ["AAPL", "INFY.NS"])

    def test_backfill_rejects_xor_bad_dates_unknown_universe_and_symbol(self):
        cases = [
            {"from": "2026-01-01", "to": "2026-02-01"},
            {"universe": "us-phase2-v1", "symbols": ["AAPL"], "from": "2026-01-01", "to": "2026-02-01"},
            {"universe": "not-a-universe", "from": "2026-01-01", "to": "2026-02-01"},
            {"symbols": ["AAPL"], "from": "2026-02-01", "to": "2026-01-01"},
            {"universe": "us-phase2-v1", "from": "2000-01-01", "to": "2026-01-01"},
            {"symbols": ["AAPL"], "from": "2026-07-01", "to": "2026-07-23"},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    self.invoke(admin_event("POST", "/v1/admin/ingest/backfill", body=body))["statusCode"],
                    400,
                )
        missing = self.invoke(
            admin_event(
                "POST",
                "/v1/admin/ingest/backfill",
                body={"symbols": ["MSFT"], "from": "2026-01-01", "to": "2026-02-01"},
            )
        )
        self.assertEqual(missing["statusCode"], 404)

    def test_job_status_reports_done_and_symbol_year_failures(self):
        self.repository.jobs["job-status"] = {
            "type": "admin_backfill",
            "state": "failed",
            "total": 3,
            "completed": 1,
            "failed": 1,
        }
        self.repository.work["job-status"] = [
            {"symbol": "AAPL", "year": 2025, "state": "completed"},
            {"symbol": "AAPL", "year": 2026, "state": "failed", "failure": "RuntimeError"},
            {"symbol": "MSFT", "year": 2026, "state": "pending"},
        ]
        response = self.invoke(admin_event("GET", "/v1/admin/ingest/jobs/job-status"))
        data = decoded(response)["data"]
        self.assertEqual(data["done"], 1)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["failedCount"], 1)
        self.assertFalse(data["failuresTruncated"])
        self.assertEqual(data["failed"], [{"symbol": "AAPL", "year": 2026, "error": "RuntimeError"}])

    def test_job_status_bounds_failure_projection_without_reading_all_work(self):
        self.repository.jobs["job-many-failures"] = {
            "type": "admin_backfill",
            "state": "failed",
            "total": 101,
            "completed": 0,
            "failed": 101,
        }
        self.repository.work["job-many-failures"] = [
            {
                "symbol": f"S{index}",
                "year": 2026,
                "state": "failed",
                "failure": "RuntimeError",
            }
            for index in range(101)
        ]
        response = self.invoke(
            admin_event("GET", "/v1/admin/ingest/jobs/job-many-failures")
        )
        data = decoded(response)["data"]
        self.assertEqual(len(data["failed"]), 100)
        self.assertEqual(data["failedCount"], 101)
        self.assertTrue(data["failuresTruncated"])

    def test_lambda_invoker_uses_event_payload_with_only_job_id(self):
        class Client:
            def __init__(self):
                self.request = None

            def invoke(self, **request):
                self.request = request
                return {"StatusCode": 202}

        client = Client()
        LambdaCoordinatorInvoker(client, "coordinator").invoke("job-safe")
        self.assertEqual(json.loads(client.request["Payload"]), {"jobId": "job-safe"})
        self.assertEqual(client.request["InvocationType"], "Event")

    def test_ambiguous_async_invoke_keeps_job_recoverable_without_leaking_error(self):
        class Rejected:
            def invoke(self, _job_id):
                raise RuntimeError("fnx_dev_" + "S" * 32)

        service = AdminService(
            self.repository,
            stage="dev",
            clock=lambda: NOW,
            identifier=lambda _prefix: "job-rejected",
            coordinator=Rejected(),
        )
        response = lambda_handler(
            admin_event(
                "POST",
                "/v1/admin/ingest/backfill",
                body={
                    "universe": "crypto-phase3-v1",
                    "from": "2026-01-01",
                    "to": "2026-01-31",
                },
            ),
            None,
            service=service,
        )
        self.assertEqual(response["statusCode"], 202)
        self.assertNotIn("fnx_dev_", response["body"])
        self.assertEqual(
            decoded(response)["data"],
            {
                "jobId": "job-rejected",
                "state": "preparing",
                "dispatchDeferred": True,
            },
        )
        self.assertEqual(self.repository.jobs["job-rejected"]["state"], "preparing")
        self.assertEqual(
            self.repository.jobs["job-rejected"]["dispatchError"],
            "RuntimeError",
        )

    def test_dispatch_error_write_tolerates_coordinator_claim_race(self):
        class ConditionalFailure(Exception):
            response = {"Error": {"Code": "ConditionalCheckFailedException"}}

        class Table:
            name = "control"

            def update_item(self, **_request):
                raise ConditionalFailure()

        class Meta:
            class Client:
                pass

            client = Client()

        class Dynamo:
            meta = Meta()

        repository = DynamoAdminRepository(Table(), Dynamo())
        repository.record_backfill_dispatch_error(
            "job-raced", error="TimeoutError", updated_at="2026-07-22T12:00:00Z"
        )

    def test_backfill_creation_atomically_adds_recovery_index(self):
        class Client:
            def __init__(self):
                self.request = None

            def transact_write_items(self, **request):
                self.request = request

        class Meta:
            def __init__(self, client):
                self.client = client

        class Dynamo:
            def __init__(self, client):
                self.meta = Meta(client)

        class Table:
            name = "control"

        client = Client()
        repository = DynamoAdminRepository(Table(), Dynamo(client))
        repository.create_backfill_job(
            {
                "PK": "JOB#job-indexed",
                "SK": "META",
                "jobId": "job-indexed",
                "type": "admin_backfill",
                "state": "preparing",
                "createdAt": "2026-07-22T12:00:00Z",
            }
        )
        writes = client.request["TransactItems"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            writes[1]["Put"]["Item"]["PK"], {"S": "BACKFILL_JOBS"}
        )

    def test_status_repository_queries_only_bounded_failure_projection(self):
        class Table:
            name = "control"

            def __init__(self):
                self.request = None

            def query(self, **request):
                self.request = request
                return {
                    "Items": [
                        {
                            "PK": "JOB#job-status",
                            "SK": f"FAIL#S{index}#YEAR#2026",
                            "symbol": f"S{index}",
                            "year": 2026,
                        }
                        for index in range(101)
                    ]
                }

        class Meta:
            class Client:
                pass

            client = Client()

        class Dynamo:
            meta = Meta()

        table = Table()
        failures, truncated = DynamoAdminRepository(
            table, Dynamo()
        ).backfill_failures("job-status", limit=100)
        self.assertEqual(table.request["ExpressionAttributeValues"][":prefix"], "FAIL#")
        self.assertEqual(table.request["Limit"], 101)
        self.assertEqual(len(failures), 100)
        self.assertTrue(truncated)


class CoordinatorRepository:
    def __init__(self, job, symbols=None):
        self.job = job
        self.symbols = symbols or {}
        self.claimed = []
        self.seeded = []
        self.work = []
        self.running = []
        self.messages = []
        self.enqueue_finished = []
        self.failed = []
        self.released = []

    def get_job(self, _job_id):
        return self.job

    def claim_job(self, job_id, *, owner, updated_at, lease_until):
        self.claimed.append((job_id, owner, updated_at, lease_until))
        self.job.update(
            state="expanding",
            coordinatorOwner=owner,
            coordinatorLeaseUntil=lease_until,
        )
        return True

    def get_symbol(self, symbol):
        return self.symbols.get(symbol)

    def seed_symbol(self, metadata, *, market):
        self.seeded.append((dict(metadata), market))

    def put_work(self, item):
        self.work.append(dict(item))

    def set_running(self, job_id, *, owner, total, updated_at):
        self.running.append((job_id, owner, total, updated_at))
        self.job.update(state="running", total=total, enqueueComplete=False)

    def enqueue(self, messages):
        self.messages.extend(messages)

    def finish_enqueue(self, job_id, *, owner, updated_at):
        self.enqueue_finished.append((job_id, owner, updated_at))
        self.job.update(enqueueComplete=True)

    def fail_job(self, job_id, *, owner, error, updated_at):
        self.failed.append((job_id, owner, error, updated_at))
        self.job["state"] = "failed"

    def release_job(self, job_id, *, owner, error, updated_at):
        self.released.append((job_id, owner, error, updated_at))
        self.job["state"] = "preparing"

    def recoverable_job_ids(self, *, now, limit):
        del now
        return ["job-sweep"][:limit]


class CoordinatorTests(unittest.TestCase):
    def test_custom_job_expands_exact_date_clipped_symbol_year_messages(self):
        repository = CoordinatorRepository(
            {
                "type": "admin_backfill",
                "state": "preparing",
                "params": {"symbols": ["AAPL"], "from": "2025-03-15", "to": "2026-04-20"},
            },
            symbols={
                "AAPL": {
                    "name": "Apple Inc.",
                    "type": "equity",
                    "exchange": "US",
                    "currency": "USD",
                    "active": True,
                }
            },
        )
        coordinator = BackfillCoordinator(repository, universe_root=UNIVERSE_ROOT, clock=lambda: NOW)

        total = coordinator.process("job-range")

        self.assertEqual(total, 2)
        self.assertEqual(repository.running[0][2], 2)
        self.assertEqual(repository.enqueue_finished[0][0], "job-range")
        owner = repository.claimed[0][1]
        self.assertEqual(repository.running[0][1], owner)
        self.assertEqual(repository.enqueue_finished[0][1], owner)
        self.assertEqual(repository.claimed[0][3], "2026-07-22T12:16:00Z")
        self.assertEqual([(item["from"], item["to"]) for item in repository.work], [("2025-03-15", "2025-12-31"), ("2026-01-01", "2026-04-20")])
        parsed = [parse_message(message) for message in repository.messages]
        self.assertTrue(all(isinstance(message, BackfillYearMessage) for message in parsed))
        self.assertEqual((parsed[0].start, parsed[1].end), (date(2025, 3, 15), date(2026, 4, 20)))

    def test_named_universe_uses_internal_id_and_checked_in_metadata(self):
        repository = CoordinatorRepository(
            {
                "type": "admin_backfill",
                "state": "preparing",
                "params": {"universe": "crypto-phase3-v1", "from": "2026-01-01", "to": "2026-01-31"},
            }
        )
        total = BackfillCoordinator(repository, universe_root=UNIVERSE_ROOT, clock=lambda: NOW).process("job-crypto")
        self.assertEqual(total, 12)
        self.assertEqual(len(repository.seeded), 12)
        self.assertTrue(all(market == "CRYPTO" for _, market in repository.seeded))

    def test_handler_records_preparation_failure_without_secrets(self):
        repository = CoordinatorRepository(
            {"type": "admin_backfill", "state": "preparing", "params": {"symbols": ["NOPE"], "from": "2026-01-01", "to": "2026-01-31"}}
        )
        coordinator = BackfillCoordinator(repository, universe_root=UNIVERSE_ROOT, clock=lambda: NOW)
        with self.assertRaises(ValueError):
            coordinator_handler({"jobId": "job-bad"}, None, coordinator=coordinator)
        self.assertEqual(repository.failed[0][0], "job-bad")
        self.assertEqual(repository.failed[0][2], "ValueError")

    def test_partial_enqueue_error_is_released_for_full_idempotent_replay(self):
        class RetryRepository(CoordinatorRepository):
            def __init__(self, job, symbols):
                super().__init__(job, symbols)
                self.attempts = 0

            def enqueue(self, messages):
                self.attempts += 1
                self.messages.extend(messages)
                if self.attempts == 1:
                    raise RuntimeError("partial SQS rejection")

        repository = RetryRepository(
            {
                "type": "admin_backfill",
                "state": "preparing",
                "params": {
                    "symbols": ["AAPL"],
                    "from": "2026-01-01",
                    "to": "2026-01-31",
                },
            },
            {
                "AAPL": {
                    "name": "Apple Inc.",
                    "type": "equity",
                    "exchange": "US",
                    "currency": "USD",
                    "active": True,
                }
            },
        )
        coordinator = BackfillCoordinator(
            repository, universe_root=UNIVERSE_ROOT, clock=lambda: NOW
        )
        with self.assertRaises(RuntimeError):
            coordinator_handler(
                {"jobId": "job-retry"}, None, coordinator=coordinator
            )
        self.assertEqual(repository.failed, [])
        self.assertEqual(repository.released[0][0], "job-retry")
        self.assertEqual(repository.released[0][2], "RuntimeError")

        coordinator_handler({"jobId": "job-retry"}, None, coordinator=coordinator)
        self.assertEqual(repository.attempts, 2)
        self.assertEqual(repository.messages[0], repository.messages[1])
        self.assertEqual(repository.enqueue_finished[-1][0], "job-retry")

    def test_dynamo_claim_can_recover_legacy_incomplete_running_job(self):
        class Table:
            def __init__(self):
                self.request = None

            def update_item(self, **request):
                self.request = request

        class Sqs:
            pass

        data = Table()
        control = Table()
        repository = DynamoCoordinatorRepository(data, control, Sqs(), "queue")
        claimed = repository.claim_job(
            "job-legacy",
            owner="owner-1",
            updated_at="2026-07-22T12:00:00Z",
            lease_until="2026-07-22T12:16:00Z",
        )
        self.assertTrue(claimed)
        condition = control.request["ConditionExpression"]
        self.assertIn("attribute_not_exists(enqueueComplete)", condition)
        self.assertIn("attribute_not_exists(coordinatorLeaseUntil)", condition)
        self.assertEqual(
            control.request["ExpressionAttributeValues"][":owner"], "owner-1"
        )

    def test_successful_enqueue_removes_active_recovery_projection(self):
        class Table:
            def __init__(self):
                self.updated = []
                self.deleted = []

            def update_item(self, **request):
                self.updated.append(request)

            def delete_item(self, **request):
                self.deleted.append(request)

        table = Table()
        repository = DynamoCoordinatorRepository(table, table, object(), "queue")

        repository.finish_enqueue(
            "job-finished",
            owner="owner-1",
            updated_at="2026-07-22T12:00:00Z",
        )

        self.assertEqual(len(table.updated), 1)
        self.assertEqual(
            table.deleted,
            [{"Key": {"PK": "BACKFILL_JOBS", "SK": "job-finished"}}],
        )

    def test_permanent_preparation_failure_removes_active_recovery_projection(self):
        class Table:
            def __init__(self):
                self.updated = []
                self.deleted = []

            def update_item(self, **request):
                self.updated.append(request)

            def delete_item(self, **request):
                self.deleted.append(request)

        table = Table()
        repository = DynamoCoordinatorRepository(table, table, object(), "queue")

        repository.fail_job(
            "job-failed",
            owner="owner-1",
            error="ValueError",
            updated_at="2026-07-22T12:00:00Z",
        )

        self.assertEqual(len(table.updated), 1)
        self.assertEqual(
            table.deleted,
            [{"Key": {"PK": "BACKFILL_JOBS", "SK": "job-failed"}}],
        )

    def test_sweeper_finds_preparing_and_expired_jobs_but_skips_busy_lease(self):
        class Table:
            jobs = {
                "completed": {"state": "completed"},
                "preparing": {"state": "preparing"},
                "expired": {
                    "state": "running",
                    "enqueueComplete": False,
                    "coordinatorLeaseUntil": "2026-07-22T11:59:00Z",
                },
                "busy": {
                    "state": "expanding",
                    "coordinatorLeaseUntil": "2026-07-22T12:16:00Z",
                },
            }

            def query(self, **_request):
                return {
                    "Items": [
                        {"PK": "BACKFILL_JOBS", "SK": job_id, "jobId": job_id}
                        for job_id in self.jobs
                    ]
                }

            def get_item(self, *, Key, **_request):
                job_id = Key["PK"].removeprefix("JOB#")
                return {"Item": self.jobs[job_id]}

            def delete_item(self, **_request):
                pass

        repository = DynamoCoordinatorRepository(Table(), Table(), object(), "queue")
        found = repository.recoverable_job_ids(
            now="2026-07-22T12:00:00Z", limit=10
        )
        self.assertEqual(found, ["preparing", "expired"])

    def test_terminal_history_cannot_starve_active_recovery(self):
        class Table:
            def __init__(self):
                self.jobs = {
                    **{
                        f"completed-{index}": {"state": "completed"}
                        for index in range(100)
                    },
                    "stuck": {
                        "state": "running",
                        "enqueueComplete": False,
                        "coordinatorLeaseUntil": "2026-07-22T11:59:00Z",
                    },
                }
                self.deleted = []

            def query(self, **request):
                if "ExclusiveStartKey" not in request:
                    terminal_ids = [
                        job_id for job_id in self.jobs if job_id != "stuck"
                    ]
                    return {
                        "Items": [
                            {
                                "PK": "BACKFILL_JOBS",
                                "SK": job_id,
                                "jobId": job_id,
                            }
                            for job_id in terminal_ids
                        ],
                        "LastEvaluatedKey": {
                            "PK": "BACKFILL_JOBS",
                            "SK": terminal_ids[-1],
                        },
                    }
                return {
                    "Items": [
                        {
                            "PK": "BACKFILL_JOBS",
                            "SK": "stuck",
                            "jobId": "stuck",
                        }
                    ]
                }

            def get_item(self, *, Key, **_request):
                return {
                    "Item": self.jobs[Key["PK"].removeprefix("JOB#")]
                }

            def delete_item(self, **request):
                self.deleted.append(request["Key"]["SK"])

        table = Table()
        repository = DynamoCoordinatorRepository(table, table, object(), "queue")
        self.assertEqual(
            repository.recoverable_job_ids(
                now="2026-07-22T12:00:00Z", limit=1
            ),
            ["stuck"],
        )
        self.assertEqual(len(table.deleted), 100)
        self.assertNotIn("stuck", table.deleted)


class TerminalFailureTests(unittest.TestCase):
    def test_final_sqs_receive_records_failed_work_and_still_redrives(self):
        class Source:
            def get_eod(self, *_args, **_kwargs):
                raise RuntimeError("upstream unavailable")

        class Repository:
            def __init__(self):
                self.failures = []

            def get_adapter_hints(self, _symbol):
                return {}

            def fail_backfill_work(self, job_id, symbol, year, *, failure):
                self.failures.append((job_id, symbol, year, failure))
                return True

        repository = Repository()
        worker = IngestWorker(Source(), repository)
        body = json.dumps({"v": 2, "kind": "backfill_year", "jobId": "job-terminal", "market": "US", "symbol": "AAPL", "year": 2026})
        response = ingest_handler(
            {"Records": [{"messageId": "failed-5", "body": body, "attributes": {"ApproximateReceiveCount": "5"}}]},
            None,
            worker=worker,
            max_receive_count=5,
        )
        self.assertEqual(response, {"batchItemFailures": [{"itemIdentifier": "failed-5"}]})
        self.assertEqual(repository.failures, [("job-terminal", "AAPL", 2026, "RuntimeError")])

    def test_receive_six_reconciles_transient_persistence_without_upstream_call(self):
        class Source:
            def __init__(self):
                self.calls = 0

            def get_eod(self, *_args, **_kwargs):
                self.calls += 1
                raise RuntimeError("upstream unavailable")

        class Repository:
            def __init__(self):
                self.attempts = 0
                self.failures = []

            def get_adapter_hints(self, _symbol):
                return {}

            def fail_backfill_work(self, job_id, symbol, year, *, failure):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("transient dynamodb error")
                self.failures.append((job_id, symbol, year, failure))
                return True

        source = Source()
        repository = Repository()
        worker = IngestWorker(source, repository)
        body = json.dumps(
            {
                "v": 2,
                "kind": "backfill_year",
                "jobId": "job-reconcile",
                "market": "US",
                "symbol": "AAPL",
                "year": 2026,
            }
        )
        fifth = ingest_handler(
            {
                "Records": [
                    {
                        "messageId": "failed-5",
                        "body": body,
                        "attributes": {"ApproximateReceiveCount": "5"},
                    }
                ]
            },
            None,
            worker=worker,
            max_receive_count=5,
        )
        sixth = ingest_handler(
            {
                "Records": [
                    {
                        "messageId": "reconcile-6",
                        "body": body,
                        "attributes": {"ApproximateReceiveCount": "6"},
                    }
                ]
            },
            None,
            worker=worker,
            max_receive_count=5,
        )

        self.assertEqual(source.calls, 1)
        self.assertEqual(repository.attempts, 2)
        self.assertEqual(repository.failures[0][:3], ("job-reconcile", "AAPL", 2026))
        self.assertEqual(repository.failures[0][3], "TerminalReconciliation")
        self.assertEqual(fifth["batchItemFailures"][0]["itemIdentifier"], "failed-5")
        self.assertEqual(sixth["batchItemFailures"][0]["itemIdentifier"], "reconcile-6")

    def test_repository_failure_is_atomic_and_finishes_exhausted_job(self):
        class Client:
            def __init__(self):
                self.calls = []

            def transact_write_items(self, **request):
                self.calls.append(request)

        class Control:
            name = "control"

            def __init__(self):
                self.updates = []

            def get_item(self, *, Key, **_request):
                if Key["SK"] == "META":
                    return {
                        "Item": {
                            "state": "running",
                            "completed": 0,
                            "failed": 1,
                            "total": 1,
                        }
                    }
                return {"Item": {"state": "pending"}}

            def update_item(self, **request):
                self.updates.append(request)

        client = Client()
        control = Control()
        repository = DynamoDBIngestRepository(
            object(), control, transaction_client=client
        )
        changed = repository.fail_backfill_work(
            "job-terminal",
            "AAPL",
            2026,
            failure="RuntimeError",
            now=NOW,
        )

        self.assertTrue(changed)
        writes = client.calls[0]["TransactItems"]
        self.assertEqual(len(writes), 3)
        self.assertIn("#failed", writes[1]["Update"]["ExpressionAttributeNames"])
        self.assertEqual(
            writes[0]["Update"]["Key"],
            {"PK": "JOB#job-terminal", "SK": "WORK#AAPL#YEAR#2026"},
        )
        self.assertEqual(
            writes[2]["Put"]["Item"]["SK"], "FAIL#AAPL#YEAR#2026"
        )
        self.assertEqual(control.updates[0]["ExpressionAttributeValues"][":failedState"], "failed")

    def test_last_success_finishes_job_failed_when_an_earlier_unit_exhausted(self):
        class Client:
            def transact_write_items(self, **_request):
                return None

        class Control:
            name = "control"

            def __init__(self):
                self.updates = []

            def get_item(self, *, Key, **_request):
                if Key["SK"] == "META":
                    return {
                        "Item": {
                            "state": "running",
                            "completed": 2,
                            "failed": 1,
                            "total": 3,
                        }
                    }
                return {"Item": {"state": "pending"}}

            def update_item(self, **request):
                self.updates.append(request)

        control = Control()
        repository = DynamoDBIngestRepository(
            object(), control, transaction_client=Client()
        )
        repository.complete_backfill_work(
            "job-mixed", "MSFT", 2026, now=NOW
        )
        self.assertEqual(
            control.updates[0]["ExpressionAttributeValues"][":failedState"],
            "failed",
        )


if __name__ == "__main__":
    unittest.main()

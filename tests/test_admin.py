from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import json
import os
import unittest
from unittest import mock

from boto3.dynamodb.types import TypeDeserializer

import src.admin.handler as admin_handler
from src.admin.handler import AdminService, lambda_handler
from src.admin.repository import DynamoAdminRepository


NOW = datetime(2026, 7, 22, 12, tzinfo=UTC)


class FakeRepository:
    def __init__(self):
        self.cohorts = {
            "cohort-existing": {
                "PK": "COHORT#cohort-existing",
                "SK": "META",
                "cohortId": "cohort-existing",
                "name": "Existing cohort",
                "defaultDailyQuota": 2000,
                "expiresAt": "2026-12-31",
                "status": "active",
                "createdAt": "2026-07-01T00:00:00Z",
                "updatedAt": "2026-07-01T00:00:00Z",
            }
        }
        self.keys = {
            "cohort-existing": [
                {
                    "PK": "COHORT#cohort-existing",
                    "SK": "KEY#student-existing",
                    "keyId": "student-existing",
                    "label": "team-3",
                    "dailyQuota": 2000,
                    "status": "active",
                    "createdAt": "2026-07-01T00:00:00Z",
                    "expiresAt": "2026-12-31",
                    "keyHash": "must-not-leak",
                }
            ]
        }
        self.usage = {"student-existing": 27}
        self.lookups = {
            "student-existing": {
                "keyHash": "abc123",
                "cohortId": "cohort-existing",
            }
        }
        self.created = []
        self.updated = []
        self.issued = []
        self.revoked = []
        self.calls = []

    def create_cohort(self, item):
        self.created.append(dict(item))
        self.cohorts[item["cohortId"]] = dict(item)

    def get_cohort(self, cohort_id):
        self.calls.append(("get_cohort", cohort_id))
        return self.cohorts.get(cohort_id)

    def update_cohort(self, item):
        self.updated.append(dict(item))
        self.cohorts[item["cohortId"]] = dict(item)

    def all_cohorts(self):
        return list(self.cohorts.values())

    def query_cohort_keys(self, cohort_id, *, limit=None, cursor=None):
        self.calls.append(("keys", cohort_id, limit, cursor))
        return list(self.keys.get(cohort_id, []))[:limit], None

    def usage_counts(self, key_ids, usage_date):
        self.calls.append(("usage", tuple(key_ids), usage_date))
        return {key_id: self.usage.get(key_id, 0) for key_id in key_ids}

    def issue_keys(self, cohort, records):
        self.issued.append((dict(cohort), [dict(record) for record in records]))

    def get_key_lookup(self, key_id):
        return self.lookups.get(key_id)

    def revoke_key(self, lookup, *, revoked_at):
        self.revoked.append((dict(lookup), revoked_at))


def event(method, path, *, body=None, query=None, admin=True, path_parameters=None):
    request = {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "lambda": {
                    "keyId": "admin-1" if admin else "student-1",
                    "keyType": "admin" if admin else "student",
                }
            },
        },
    }
    if not admin:
        request["requestContext"]["authorizer"]["lambda"].update(
            {"cohortId": "cohort-existing", "dailyQuota": "2000"}
        )
    if body is not None:
        request["body"] = json.dumps(body)
    if query is not None:
        request["queryStringParameters"] = query
    if path_parameters is not None:
        request["pathParameters"] = path_parameters
    return request


def decoded(response):
    return json.loads(response["body"])


class AdminServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = FakeRepository()
        identifiers = iter(
            ["cohort-new", "student-one", "student-two", "student-three"]
        )
        plaintext = iter(
            [
                "fnx_dev_" + "A" * 32,
                "fnx_dev_" + "B" * 32,
                "fnx_dev_" + "C" * 32,
            ]
        )
        self.service = AdminService(
            self.repository,
            stage="dev",
            clock=lambda: NOW,
            identifier=lambda _prefix: next(identifiers),
            plaintext_key=lambda: next(plaintext),
        )

    def invoke(self, request):
        return lambda_handler(request, None, service=self.service)

    def test_student_identity_is_rejected_before_any_repository_call(self):
        response = self.invoke(event("GET", "/v1/admin/cohorts", admin=False))

        self.assertEqual(response["statusCode"], 403)
        self.assertEqual(decoded(response)["error"]["code"], "ADMIN_ONLY")
        self.assertEqual(self.repository.calls, [])

    def test_create_cohort_validates_and_returns_201(self):
        response = self.invoke(
            event(
                "POST",
                "/v1/admin/cohorts",
                body={
                    "name": "  HDFC Grad Batch  ",
                    "defaultDailyQuota": 2000,
                    "expiresAt": "2026-12-31",
                },
            )
        )

        self.assertEqual(response["statusCode"], 201)
        data = decoded(response)["data"]
        self.assertEqual(data["cohortId"], "cohort-new")
        self.assertEqual(data["name"], "HDFC Grad Batch")
        self.assertEqual(data["status"], "active")
        self.assertEqual(self.repository.created[0]["PK"], "COHORT#cohort-new")

    def test_create_cohort_rejects_extra_fields_bad_quota_and_past_expiry(self):
        cases = [
            {"name": "x", "defaultDailyQuota": 1, "expiresAt": "2026-12-31", "extra": True},
            {"name": "x", "defaultDailyQuota": True, "expiresAt": "2026-12-31"},
            {"name": "x", "defaultDailyQuota": 1, "expiresAt": "2026-07-21"},
        ]
        for body in cases:
            with self.subTest(body=body):
                response = self.invoke(event("POST", "/v1/admin/cohorts", body=body))
                self.assertEqual(response["statusCode"], 400)
        self.assertEqual(self.repository.created, [])

    def test_get_and_list_cohorts_include_key_counts_and_usage(self):
        detail = self.invoke(
            event("GET", "/v1/admin/cohorts/cohort-existing")
        )
        listed = self.invoke(event("GET", "/v1/admin/cohorts"))

        for response in (detail, listed):
            self.assertEqual(response["statusCode"], 200)
        detail_data = decoded(detail)["data"]
        self.assertEqual(detail_data["keyCounts"], {"total": 1, "active": 1, "revoked": 0})
        self.assertEqual(detail_data["usedToday"], 27)
        self.assertEqual(decoded(listed)["data"]["cohorts"][0]["usedToday"], 27)

    def test_list_cohort_cursor_is_opaque_and_invalid_cursor_is_400(self):
        self.repository.cohorts["cohort-second"] = {
            **self.repository.cohorts["cohort-existing"],
            "PK": "COHORT#cohort-second",
            "cohortId": "cohort-second",
            "name": "Second",
            "createdAt": "2026-07-02T00:00:00Z",
        }
        first = self.invoke(event("GET", "/v1/admin/cohorts", query={"limit": "1"}))
        cursor = decoded(first)["data"]["cursor"]
        second = self.invoke(event("GET", "/v1/admin/cohorts", query={"limit": "1", "cursor": cursor}))

        self.assertIsInstance(cursor, str)
        self.assertEqual(decoded(second)["data"]["cohorts"][0]["cohortId"], "cohort-second")
        invalid = self.invoke(event("GET", "/v1/admin/cohorts", query={"cursor": "not-base64"}))
        self.assertEqual(invalid["statusCode"], 400)

    def test_patch_cohort_accepts_only_lifecycle_fields_and_mirrors_complete_item(self):
        response = self.invoke(
            event(
                "PATCH",
                "/v1/admin/cohorts/cohort-existing",
                body={"name": "Renamed", "status": "inactive", "defaultDailyQuota": 1500},
            )
        )

        self.assertEqual(response["statusCode"], 200)
        item = self.repository.updated[0]
        self.assertEqual(item["name"], "Renamed")
        self.assertEqual(item["status"], "inactive")
        self.assertEqual(item["expiresAt"], "2026-12-31")
        bad = self.invoke(event("PATCH", "/v1/admin/cohorts/cohort-existing", body={"unknown": 1}))
        self.assertEqual(bad["statusCode"], 400)

    def test_missing_cohort_returns_404(self):
        response = self.invoke(event("GET", "/v1/admin/cohorts/cohort-missing"))
        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(decoded(response)["error"]["code"], "COHORT_NOT_FOUND")

    def test_issue_keys_is_atomic_input_and_returns_plaintext_once(self):
        response = self.invoke(
            event(
                "POST",
                "/v1/admin/keys",
                body={
                    "cohortId": "cohort-existing",
                    "labels": ["amara", "team,3"],
                    "dailyQuota": 1500,
                },
            )
        )

        self.assertEqual(response["statusCode"], 201)
        data = decoded(response)["data"]
        self.assertEqual(
            [item["label"] for item in data["keys"]], ["amara", "team,3"]
        )
        self.assertTrue(data["keys"][0]["key"].startswith("fnx_dev_"))
        _, records = self.repository.issued[0]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["dailyQuota"], 1500)
        self.assertIn("keyHash", records[0])
        self.assertNotIn("key", records[0])
        self.assertNotIn(data["keys"][0]["key"], json.dumps(records))

    def test_issue_by_count_uses_safe_generated_labels_and_cohort_default(self):
        response = self.invoke(
            event("POST", "/v1/admin/keys", body={"cohortId": "cohort-existing", "count": 2})
        )
        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(
            [item["label"] for item in decoded(response)["data"]["keys"]],
            ["student-001", "student-002"],
        )
        self.assertTrue(all(item["dailyQuota"] == 2000 for item in self.repository.issued[0][1]))

    def test_issue_rejects_xor_duplicates_limit_and_inactive_cohort(self):
        bodies = [
            {"cohortId": "cohort-existing"},
            {"cohortId": "cohort-existing", "count": 1, "labels": ["a"]},
            {"cohortId": "cohort-existing", "labels": ["Team", "team"]},
            {"cohortId": "cohort-existing", "count": 26},
            {"cohortId": "cohort-existing", "labels": ["@unsafe"]},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    self.invoke(event("POST", "/v1/admin/keys", body=body))["statusCode"],
                    400,
                )
        self.repository.cohorts["cohort-existing"]["status"] = "inactive"
        response = self.invoke(event("POST", "/v1/admin/keys", body={"cohortId": "cohort-existing", "count": 1}))
        self.assertEqual(response["statusCode"], 409)
        self.assertEqual(decoded(response)["error"]["code"], "COHORT_INACTIVE")

    def test_list_keys_paginates_usage_and_redacts_secret_fields(self):
        response = self.invoke(
            event("GET", "/v1/admin/keys", query={"cohortId": "cohort-existing", "limit": "25"})
        )

        self.assertEqual(response["statusCode"], 200)
        item = decoded(response)["data"]["keys"][0]
        self.assertEqual(item["usedToday"], 27)
        self.assertNotIn("keyHash", item)
        self.assertNotIn("key", item)

        tampered = self.invoke(
            event(
                "GET",
                "/v1/admin/keys",
                query={
                    "cohortId": "cohort-existing",
                    "cursor": "eyJQSyI6IkNPSE9SVCNvdGhlciIsIlNLIjoiS0VZI3gifQ",
                },
            )
        )
        self.assertEqual(tampered["statusCode"], 400)

    def test_revoke_key_is_idempotent_at_service_boundary_and_missing_is_404(self):
        first = self.invoke(event("DELETE", "/v1/admin/keys/student-existing"))
        second = self.invoke(event("DELETE", "/v1/admin/keys/student-existing"))

        self.assertEqual(first["statusCode"], 200)
        self.assertEqual(second["statusCode"], 200)
        self.assertEqual(len(self.repository.revoked), 2)
        self.assertEqual(self.repository.revoked[0][0]["keyHash"], "abc123")
        missing = self.invoke(event("DELETE", "/v1/admin/keys/student-missing"))
        self.assertEqual(missing["statusCode"], 404)

    def test_issue_keys_without_student_identity_omits_student_fields(self):
        response = self.invoke(
            event(
                "POST",
                "/v1/admin/keys",
                body={"cohortId": "cohort-existing", "labels": ["amara"]},
            )
        )
        self.assertEqual(response["statusCode"], 201)
        data = decoded(response)["data"]
        self.assertNotIn("studentName", data["keys"][0])
        self.assertNotIn("studentEmail", data["keys"][0])
        _, records = self.repository.issued[0]
        self.assertNotIn("studentName", records[0])
        self.assertNotIn("studentEmail", records[0])

    def test_issue_keys_with_student_identity_returns_it_and_forwards_it(self):
        response = self.invoke(
            event(
                "POST",
                "/v1/admin/keys",
                body={
                    "cohortId": "cohort-existing",
                    "students": [{"label": "amara", "name": "Amara Diallo", "email": "amara@example.com"}],
                },
            )
        )
        self.assertEqual(response["statusCode"], 201)
        data = decoded(response)["data"]
        self.assertEqual(data["keys"][0]["studentName"], "Amara Diallo")
        self.assertEqual(data["keys"][0]["studentEmail"], "amara@example.com")
        _, records = self.repository.issued[0]
        self.assertEqual(records[0]["studentName"], "Amara Diallo")
        self.assertEqual(records[0]["studentEmail"], "amara@example.com")

    def test_issue_keys_rejects_malformed_students_and_mixed_fields(self):
        bodies = [
            {"cohortId": "cohort-existing", "students": []},
            {"cohortId": "cohort-existing", "students": [{"label": "a", "name": "A"}]},
            {"cohortId": "cohort-existing", "students": [{"label": "a", "name": "A", "email": "bad"}]},
            {"cohortId": "cohort-existing", "students": [{"label": "a", "name": "", "email": "a@b.com"}]},
            {"cohortId": "cohort-existing", "students": [{"label": "a", "name": "A", "email": "a@b.com"}], "labels": ["a"]},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    self.invoke(event("POST", "/v1/admin/keys", body=body))["statusCode"],
                    400,
                )

    def test_public_method_names_are_directly_callable(self):
        self.assertTrue(hasattr(self.service, "create_cohort"))
        self.assertTrue(hasattr(self.service, "get_cohort_detail"))
        self.assertTrue(hasattr(self.service, "list_cohorts"))
        self.assertTrue(hasattr(self.service, "issue_keys"))
        self.assertTrue(hasattr(self.service, "list_keys"))
        self.assertTrue(hasattr(self.service, "revoke_key"))
        self.assertTrue(hasattr(self.service, "required_cohort"))
        self.assertTrue(hasattr(self.service, "cohort_stats"))
        detail = self.service.get_cohort_detail("cohort-existing", NOW)
        self.assertEqual(detail["cohortId"], "cohort-existing")

    def test_malformed_json_and_unknown_routes_use_common_error_envelope(self):
        malformed = event("POST", "/v1/admin/cohorts")
        malformed["body"] = "{"
        response = self.invoke(malformed)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(set(decoded(response)["error"]), {"code", "message", "details"})
        self.assertEqual(self.invoke(event("GET", "/v1/admin/nope"))["statusCode"], 404)


class DynamoAdminRepositoryTests(unittest.TestCase):
    def test_cohort_create_is_two_conditional_transaction_puts(self):
        table, dynamodb, client = _aws_fakes()
        repository = DynamoAdminRepository(table, dynamodb)
        repository.create_cohort(
            {
                "PK": "COHORT#cohort-1",
                "SK": "META",
                "cohortId": "cohort-1",
                "name": "One",
                "defaultDailyQuota": 2000,
                "expiresAt": "2026-12-31",
                "status": "active",
            }
        )

        puts = client.transactions[0]["TransactItems"]
        self.assertEqual(len(puts), 2)
        decoded_items = [_deserialize(item["Put"]["Item"]) for item in puts]
        self.assertEqual(decoded_items[0]["PK"], "COHORT#cohort-1")
        self.assertEqual(decoded_items[1]["PK"], "COHORTS")
        self.assertTrue(all("ConditionExpression" in item["Put"] for item in puts))

    def test_key_issue_is_one_condition_check_plus_three_puts_per_key(self):
        table, dynamodb, client = _aws_fakes()
        repository = DynamoAdminRepository(table, dynamodb)
        cohort = {"cohortId": "cohort-1", "expiresAt": "2026-12-31"}
        records = [
            {"keyId": "student-1", "keyHash": "hash-1", "label": "one", "dailyQuota": 2000, "createdAt": "now"},
            {"keyId": "student-2", "keyHash": "hash-2", "label": "two", "dailyQuota": 2000, "createdAt": "now"},
        ]
        repository.issue_keys(cohort, records)

        writes = client.transactions[0]["TransactItems"]
        self.assertEqual(len(writes), 7)
        self.assertIn("ConditionCheck", writes[0])
        puts = [_deserialize(write["Put"]["Item"]) for write in writes[1:]]
        self.assertEqual(
            [item["PK"] for item in puts],
            ["KEY#hash-1", "KEYID#student-1", "COHORT#cohort-1", "KEY#hash-2", "KEYID#student-2", "COHORT#cohort-1"],
        )
        self.assertTrue(all("ConditionExpression" in write["Put"] for write in writes[1:]))

    def test_key_issue_puts_student_identity_only_on_cohort_index_item(self):
        table, dynamodb, client = _aws_fakes()
        repository = DynamoAdminRepository(table, dynamodb)
        cohort = {"cohortId": "cohort-1", "expiresAt": "2026-12-31"}
        records = [
            {
                "keyId": "student-1",
                "keyHash": "hash-1",
                "label": "one",
                "dailyQuota": 2000,
                "createdAt": "now",
                "studentName": "Amara Diallo",
                "studentEmail": "amara@example.com",
            },
            {
                "keyId": "student-2",
                "keyHash": "hash-2",
                "label": "two",
                "dailyQuota": 2000,
                "createdAt": "now",
            },
        ]
        repository.issue_keys(cohort, records)

        writes = client.transactions[0]["TransactItems"]
        puts = [_deserialize(write["Put"]["Item"]) for write in writes[1:]]
        by_pk = {(item["PK"], item["SK"]): item for item in puts}
        with_identity = by_pk[("COHORT#cohort-1", "KEY#student-1")]
        self.assertEqual(with_identity["studentName"], "Amara Diallo")
        self.assertEqual(with_identity["studentEmail"], "amara@example.com")
        self.assertNotIn("studentName", by_pk[("KEY#hash-1", "META")])
        self.assertNotIn("studentEmail", by_pk[("KEY#hash-1", "META")])
        self.assertNotIn("studentName", by_pk[("KEYID#student-1", "META")])
        self.assertNotIn("studentEmail", by_pk[("KEYID#student-1", "META")])
        without_identity = by_pk[("COHORT#cohort-1", "KEY#student-2")]
        self.assertNotIn("studentName", without_identity)
        self.assertNotIn("studentEmail", without_identity)

    def test_legacy_scan_overrides_projection_and_deduplicates(self):
        table, dynamodb, _client = _aws_fakes()
        table.query_response = {"Items": [{"PK": "COHORTS", "SK": "legacy", "cohortId": "legacy", "name": "Stale"}]}
        table.scan_response = {"Items": [{"PK": "COHORT#legacy", "SK": "META", "name": "Fresh"}]}
        values = DynamoAdminRepository(table, dynamodb).all_cohorts()
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["cohortId"], "legacy")
        self.assertEqual(values[0]["name"], "Fresh")


def _aws_fakes():
    class Client:
        def __init__(self):
            self.transactions = []

        def transact_write_items(self, **request):
            self.transactions.append(request)

    class Meta:
        def __init__(self, client):
            self.client = client

    class Dynamo:
        def __init__(self, client):
            self.meta = Meta(client)

        def batch_get_item(self, **_request):
            return {"Responses": {"control": []}}

    class Table:
        name = "control"

        def __init__(self):
            self.query_response = {"Items": []}
            self.scan_response = {"Items": []}

        def query(self, **_request):
            return self.query_response

        def scan(self, **_request):
            return self.scan_response

    client = Client()
    return Table(), Dynamo(client), client


def _deserialize(item):
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


class DefaultServiceRepositoryClientTests(unittest.TestCase):
    def test_default_service_builds_repository_with_standalone_dynamodb_client(self):
        resource = mock.Mock(name="dynamodb_resource")
        low_level_client = mock.Mock(name="dynamodb_client")
        clients = {"dynamodb": low_level_client, "lambda": mock.Mock(name="lambda_client")}

        with mock.patch.object(
            admin_handler, "_service", None
        ), mock.patch("boto3.resource", return_value=resource), mock.patch(
            "boto3.client", side_effect=lambda name: clients[name]
        ) as client_mock, mock.patch.dict(
            os.environ,
            {
                "CONTROL_TABLE": "control",
                "DATA_TABLE": "data",
                "BACKFILL_COORDINATOR_FUNCTION": "fn",
            },
        ):
            service = admin_handler._default_service()

        client_mock.assert_any_call("dynamodb")
        self.assertIs(service._repository._client, low_level_client)
        self.assertIsNot(service._repository._client, resource.meta.client)


if __name__ == "__main__":
    unittest.main()

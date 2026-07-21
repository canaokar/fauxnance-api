from datetime import UTC, datetime
import hashlib
import unittest

from src.authorizer.handler import (
    AuthorizationDenied,
    AuthorizerService,
    DynamoKeyRepository,
    SsmAdminAllowlist,
    lambda_handler,
)
from src.shared.auth import AuthContext, hash_api_key, is_expired, parse_authorizer_context


NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)
STUDENT_KEY = "fnx_dev_" + "A" * 32
ADMIN_KEY = "fnx_dev_" + "B" * 32


class FakeRepository:
    def __init__(self, key=None, cohort=None):
        self.key = key
        self.cohort = cohort
        self.digests = []
        self.cohort_ids = []

    def get_key(self, digest):
        self.digests.append(digest)
        return self.key

    def get_cohort(self, cohort_id):
        self.cohort_ids.append(cohort_id)
        return self.cohort


class FakeAllowlist:
    def __init__(self, *allowed):
        self.allowed = set(allowed)

    def contains(self, key_id):
        return key_id in self.allowed


def student_item(**overrides):
    item = {
        "keyId": "student-1",
        "type": "student",
        "status": "active",
        "cohortId": "cohort-1",
        "dailyQuota": 2_000,
        "expiresAt": "2026-08-31",
    }
    item.update(overrides)
    return item


def cohort_item(**overrides):
    item = {
        "status": "active",
        "defaultDailyQuota": 1_000,
        "expiresAt": "2026-08-31",
    }
    item.update(overrides)
    return item


class AuthPrimitiveTests(unittest.TestCase):
    def test_hash_api_key_is_sha256_hex(self):
        self.assertEqual(
            hash_api_key(STUDENT_KEY),
            hashlib.sha256(STUDENT_KEY.encode("utf-8")).hexdigest(),
        )

    def test_date_expiry_is_valid_through_the_named_utc_date(self):
        self.assertFalse(
            is_expired("2026-07-21", now=datetime(2026, 7, 21, 23, 59, tzinfo=UTC))
        )
        self.assertTrue(
            is_expired("2026-07-21", now=datetime(2026, 7, 22, tzinfo=UTC))
        )

    def test_timestamp_expiry_uses_the_exact_instant(self):
        self.assertTrue(is_expired("2026-07-21T12:00:00Z", now=NOW))

    def test_parse_payload_v2_authorizer_context(self):
        event = {
            "requestContext": {
                "authorizer": {
                    "lambda": {
                        "keyId": "student-1",
                        "keyType": "student",
                        "cohortId": "cohort-1",
                        "dailyQuota": "2000",
                    }
                }
            }
        }
        self.assertEqual(
            parse_authorizer_context(event),
            AuthContext("student-1", "student", "cohort-1", 2_000),
        )


class AuthorizerServiceTests(unittest.TestCase):
    def make_service(self, key=None, cohort=None, allowed=()):
        repository = FakeRepository(key, cohort)
        service = AuthorizerService(
            repository,
            FakeAllowlist(*allowed),
            clock=lambda: NOW,
        )
        return service, repository

    def test_active_student_gets_non_secret_context(self):
        service, repository = self.make_service(student_item(), cohort_item())

        context = service.authorize(STUDENT_KEY)

        self.assertEqual(
            context,
            AuthContext("student-1", "student", "cohort-1", 2_000),
        )
        self.assertEqual(repository.digests, [hash_api_key(STUDENT_KEY)])
        self.assertNotIn(STUDENT_KEY, context.to_gateway_context().values())

    def test_cohort_default_is_a_safe_fallback_for_older_key_items(self):
        service, _ = self.make_service(
            student_item(dailyQuota=None), cohort_item(defaultDailyQuota=1_500)
        )
        self.assertEqual(service.authorize(STUDENT_KEY).daily_quota, 1_500)

    def test_admin_must_be_in_ssm_allowlist(self):
        admin = {
            "keyId": "admin-1",
            "type": "admin",
            "status": "active",
            "expiresAt": "2026-08-31",
        }
        denied, _ = self.make_service(admin, allowed=())
        with self.assertRaises(AuthorizationDenied):
            denied.authorize(ADMIN_KEY)

        allowed, repository = self.make_service(admin, allowed=("admin-1",))
        self.assertEqual(allowed.authorize(ADMIN_KEY), AuthContext("admin-1", "admin"))
        self.assertEqual(repository.cohort_ids, [])

    def test_inactive_or_expired_key_is_denied(self):
        for key in (
            student_item(status="revoked"),
            student_item(expiresAt="2026-07-20"),
        ):
            with self.subTest(key=key):
                service, _ = self.make_service(key, cohort_item())
                with self.assertRaises(AuthorizationDenied):
                    service.authorize(STUDENT_KEY)

    def test_missing_inactive_or_expired_cohort_is_denied(self):
        for cohort in (
            None,
            cohort_item(status="inactive"),
            cohort_item(expiresAt="2026-07-20"),
        ):
            with self.subTest(cohort=cohort):
                service, _ = self.make_service(student_item(), cohort)
                with self.assertRaises(AuthorizationDenied):
                    service.authorize(STUDENT_KEY)

    def test_malformed_key_is_denied_without_a_table_read(self):
        service, repository = self.make_service(student_item(), cohort_item())
        with self.assertRaises(AuthorizationDenied):
            service.authorize("not-a-key")
        self.assertEqual(repository.digests, [])

    def test_lambda_handler_returns_payload_v2_simple_response(self):
        service, _ = self.make_service(student_item(), cohort_item())
        response = lambda_handler(
            {"identitySource": [STUDENT_KEY]}, None, service=service
        )
        self.assertEqual(
            response,
            {
                "isAuthorized": True,
                "context": {
                    "keyId": "student-1",
                    "keyType": "student",
                    "cohortId": "cohort-1",
                    "dailyQuota": 2_000,
                },
            },
        )

    def test_lambda_handler_denies_unknown_key(self):
        service, _ = self.make_service(None, None)
        self.assertEqual(
            lambda_handler({"headers": {"X-Api-Key": STUDENT_KEY}}, None, service=service),
            {"isAuthorized": False},
        )


class AwsAdapterTests(unittest.TestCase):
    def test_dynamo_repository_uses_documented_keys_and_strong_reads(self):
        class Table:
            def __init__(self):
                self.calls = []

            def get_item(self, **kwargs):
                self.calls.append(kwargs)
                return {"Item": {"ok": True}}

        table = Table()
        repository = DynamoKeyRepository(table)
        self.assertEqual(repository.get_key("abc"), {"ok": True})
        self.assertEqual(repository.get_cohort("cohort-1"), {"ok": True})
        self.assertEqual(
            table.calls,
            [
                {
                    "Key": {"PK": "KEY#abc", "SK": "META"},
                    "ConsistentRead": True,
                },
                {
                    "Key": {"PK": "COHORT#cohort-1", "SK": "META"},
                    "ConsistentRead": True,
                },
            ],
        )

    def test_ssm_allowlist_is_trimmed_and_cached(self):
        class Ssm:
            def __init__(self):
                self.calls = 0

            def get_parameter(self, **kwargs):
                self.calls += 1
                return {"Parameter": {"Value": "admin-1, admin-2, "}}

        ssm = Ssm()
        allowlist = SsmAdminAllowlist(ssm, "/fauxnance/dev/admin/key_ids")
        self.assertTrue(allowlist.contains("admin-1"))
        self.assertTrue(allowlist.contains("admin-2"))
        self.assertFalse(allowlist.contains("admin-3"))
        self.assertEqual(ssm.calls, 1)


if __name__ == "__main__":
    unittest.main()

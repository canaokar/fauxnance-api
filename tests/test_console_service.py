from __future__ import annotations

from datetime import UTC, datetime
import unittest

from src.admin.handler import AdminService
from src.admin.repository import RepositoryConflict
from src.console.passwords import hash_password
from src.console.service import ConsoleError, ConsoleService
from tests.dynamo_fidelity import dynamo_roundtrip


NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class FakeConsoleRepository:
    def __init__(self):
        self.users_by_id: dict[str, dict] = {}
        self.users_by_email: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.login_failures: dict[str, dict] = {}
        self.instructor_assignments: dict[tuple[str, str], dict] = {}
        self.reverse_assignments: dict[tuple[str, str], dict] = {}

    def seed_user(self, item):
        record = dict(item)
        self.users_by_id[record["userId"]] = record
        self.users_by_email[record["email"]] = record
        return record

    def seed_assignment(self, cohort_id, user_id, *, name="Instructor", email="i@example.com"):
        assignment = {
            "PK": f"COHORT#{cohort_id}",
            "SK": f"INSTRUCTOR#{user_id}",
            "userId": user_id,
            "cohortId": cohort_id,
            "name": name,
            "email": email,
            "assignedAt": "2026-08-01T00:00:00Z",
        }
        reverse = {
            "PK": f"USER#{user_id}",
            "SK": f"COHORT#{cohort_id}",
            "cohortId": cohort_id,
            "assignedAt": "2026-08-01T00:00:00Z",
        }
        self.instructor_assignments[(assignment["PK"], assignment["SK"])] = assignment
        self.reverse_assignments[(reverse["PK"], reverse["SK"])] = reverse

    def create_user(self, item):
        email = item["email"]
        if email in self.users_by_email:
            raise RepositoryConflict("email already registered")
        record = dict(item)
        self.users_by_id[item["userId"]] = record
        self.users_by_email[email] = record

    def get_user(self, user_id):
        item = self.users_by_id.get(user_id)
        return dict(item) if item is not None else None

    def get_user_by_email(self, email):
        item = self.users_by_email.get(email)
        return dict(item) if item is not None else None

    def all_users(self):
        return [dict(item) for item in self.users_by_id.values()]

    def update_user_status(self, user_id, *, status, updated_at):
        item = self.users_by_id.get(user_id)
        if item is None:
            raise RepositoryConflict(f"user {user_id} changed")
        item["status"] = status
        item["updatedAt"] = updated_at

    def create_session(self, item):
        self.sessions[item["PK"]] = dict(item)

    def get_session(self, token_hash):
        item = self.sessions.get(f"SESSION#{token_hash}")
        return dict(item) if item is not None else None

    def delete_session(self, token_hash):
        self.sessions.pop(f"SESSION#{token_hash}", None)

    def record_login_failure(self, email, *, expires_at):
        record = self.login_failures.setdefault(email, {"count": 0, "expiresAt": expires_at})
        record["count"] += 1
        record["expiresAt"] = expires_at
        return record["count"]

    def get_login_failures(self, email, now):
        record = self.login_failures.get(email)
        if record is None:
            return 0
        if record["expiresAt"] <= int(now.timestamp()):
            return 0
        return record["count"]

    def clear_login_failures(self, email):
        self.login_failures.pop(email, None)

    def assign_instructor(self, assignment, reverse):
        key = (assignment["PK"], assignment["SK"])
        if key in self.instructor_assignments:
            raise RepositoryConflict("instructor already assigned")
        self.instructor_assignments[key] = dict(assignment)
        self.reverse_assignments[(reverse["PK"], reverse["SK"])] = dict(reverse)

    def unassign_instructor(self, cohort_id, user_id):
        self.instructor_assignments.pop((f"COHORT#{cohort_id}", f"INSTRUCTOR#{user_id}"), None)
        self.reverse_assignments.pop((f"USER#{user_id}", f"COHORT#{cohort_id}"), None)

    def instructors_for_class(self, cohort_id):
        prefix = f"COHORT#{cohort_id}"
        return [dict(v) for k, v in self.instructor_assignments.items() if k[0] == prefix]

    def classes_for_user(self, user_id):
        prefix = f"USER#{user_id}"
        return [dict(v) for k, v in self.reverse_assignments.items() if k[0] == prefix]

    def is_assigned(self, cohort_id, user_id):
        return (f"COHORT#{cohort_id}", f"INSTRUCTOR#{user_id}") in self.instructor_assignments


class FakeAdminRepository:
    def __init__(self):
        self.cohorts: dict[str, dict] = {}
        self.keys_by_cohort: dict[str, list[dict]] = {}
        self.key_lookup: dict[str, dict] = {}
        self.usage: dict[str, int] = {}

    def seed_cohort(self, cohort_id, **overrides):
        item = {
            "PK": f"COHORT#{cohort_id}",
            "SK": "META",
            "cohortId": cohort_id,
            "name": "Cohort",
            "defaultDailyQuota": 100,
            "expiresAt": "2027-01-01",
            "status": "active",
            "createdAt": "2026-08-01T00:00:00Z",
            "updatedAt": "2026-08-01T00:00:00Z",
        }
        item.update(overrides)
        self.cohorts[cohort_id] = item
        self.keys_by_cohort.setdefault(cohort_id, [])
        return item

    def create_cohort(self, item):
        cohort_id = item["cohortId"]
        if cohort_id in self.cohorts:
            raise RepositoryConflict("cohort already exists")
        self.cohorts[cohort_id] = dict(item)

    def get_cohort(self, cohort_id):
        item = self.cohorts.get(cohort_id)
        return dynamo_roundtrip(item) if item is not None else None

    def update_cohort(self, item):
        self.cohorts[item["cohortId"]] = dict(item)

    def all_cohorts(self):
        return [dynamo_roundtrip(item) for item in self.cohorts.values()]

    def query_cohort_keys(self, cohort_id, *, limit=None, cursor=None):
        items = [dynamo_roundtrip(item) for item in self.keys_by_cohort.get(cohort_id, [])]
        if limit is not None:
            items = items[:limit]
        return items, None

    def usage_counts(self, key_ids, usage_date):
        return {key_id: self.usage.get(key_id, 0) for key_id in key_ids}

    def issue_keys(self, cohort, records):
        cohort_id = cohort["cohortId"]
        bucket = self.keys_by_cohort.setdefault(cohort_id, [])
        for record in records:
            item = {
                "PK": f"COHORT#{cohort_id}",
                "SK": f"KEY#{record['keyId']}",
                "keyId": record["keyId"],
                "label": record["label"],
                "cohortId": cohort_id,
                "dailyQuota": record["dailyQuota"],
                "status": "active",
                "createdAt": record["createdAt"],
                "expiresAt": cohort["expiresAt"],
            }
            for field in ("studentName", "studentEmail"):
                if record.get(field):
                    item[field] = record[field]
            bucket.append(item)
            self.key_lookup[record["keyId"]] = {
                "keyId": record["keyId"],
                "keyHash": record["keyHash"],
                "cohortId": cohort_id,
            }

    def get_key_lookup(self, key_id):
        item = self.key_lookup.get(key_id)
        return dynamo_roundtrip(item) if item is not None else None

    def revoke_key(self, lookup, *, revoked_at):
        key_id = lookup["keyId"]
        cohort_id = lookup["cohortId"]
        for item in self.keys_by_cohort.get(cohort_id, []):
            if item["keyId"] == key_id:
                item["status"] = "revoked"
                item["revokedAt"] = revoked_at


def _sequential_identifier():
    """A per-prefix counting identifier, so repeated calls (e.g. issuing 25 keys) yield distinct ids."""

    counters: dict[str, int] = {}

    def make(prefix):
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]}"

    return make


def build_service(*, identifier=None, token_factory=None, plaintext_key=None, admin_identifier=None):
    console_repo = FakeConsoleRepository()
    admin_repo = FakeAdminRepository()
    admin_service = AdminService(
        admin_repo,
        stage="dev",
        clock=lambda: NOW,
        identifier=admin_identifier or _sequential_identifier(),
        plaintext_key=plaintext_key or (lambda: "fnx_dev_" + "A" * 32),
    )
    service = ConsoleService(
        console_repo,
        admin_service,
        admin_repo,
        identifier=identifier,
        token_factory=token_factory,
    )
    return console_repo, admin_repo, admin_service, service


def make_user(user_id, *, role="instructor", email=None, status="active", password="correct horse battery"):
    return {
        "PK": f"USER#{user_id}",
        "SK": "META",
        "userId": user_id,
        "email": email or f"{user_id}@example.com",
        "name": user_id,
        "passwordHash": hash_password(password),
        "role": role,
        "status": status,
        "createdAt": "2026-08-01T00:00:00Z",
        "updatedAt": "2026-08-01T00:00:00Z",
    }


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.console_repo, self.admin_repo, self.admin_service, self.service = build_service(
            token_factory=lambda: "test-token"
        )
        self.user = make_user("user_1", role="instructor", password="correct horse battery")
        self.console_repo.seed_user(self.user)

    def test_login_success_returns_token_and_clears_failures(self):
        self.console_repo.login_failures[self.user["email"]] = {
            "count": 3,
            "expiresAt": int(NOW.timestamp()) + 900,
        }

        result = self.service.login(
            {"email": self.user["email"], "password": "correct horse battery"}, NOW
        )

        self.assertEqual(result["token"], "test-token")
        self.assertEqual(result["expiresAt"], "2026-08-22T00:00:00Z")
        self.assertEqual(result["user"]["userId"], "user_1")
        self.assertNotIn("passwordHash", result["user"])
        self.assertEqual(self.console_repo.get_login_failures(self.user["email"], NOW), 0)

    def test_wrong_password_unknown_email_and_disabled_user_return_identical_error(self):
        disabled = make_user("user_2", role="instructor", status="disabled")
        self.console_repo.seed_user(disabled)

        cases = {
            "wrong password": (self.user["email"], "nope nope nope"),
            "unknown email": ("nobody@example.com", "nope nope nope"),
            "disabled user, correct password": (disabled["email"], "correct horse battery"),
        }
        for description, (email, password) in cases.items():
            with self.subTest(description):
                with self.assertRaises(ConsoleError) as ctx:
                    self.service.login({"email": email, "password": password}, NOW)
                self.assertEqual(ctx.exception.status, 401)
                self.assertEqual(ctx.exception.code, "INVALID_CREDENTIALS")
                self.assertEqual(ctx.exception.message, "Incorrect email or password.")

    def test_lockout_after_ten_failures_checked_before_password_verify(self):
        self.console_repo.login_failures[self.user["email"]] = {
            "count": 10,
            "expiresAt": int(NOW.timestamp()) + 900,
        }

        with self.assertRaises(ConsoleError) as ctx:
            self.service.login(
                {"email": self.user["email"], "password": "correct horse battery"}, NOW
            )

        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(ctx.exception.code, "TOO_MANY_ATTEMPTS")
        # No session was created even though the password was correct.
        self.assertEqual(self.console_repo.sessions, {})

    def test_expired_throttle_record_does_not_lock_out(self):
        # DynamoDB TTL deletion is asynchronous, so a count of 10 can still be
        # sitting in the table well after its expiresAt has passed. It must
        # not lock the user out.
        self.console_repo.login_failures[self.user["email"]] = {
            "count": 10,
            "expiresAt": int(NOW.timestamp()) - 1,
        }

        result = self.service.login(
            {"email": self.user["email"], "password": "correct horse battery"}, NOW
        )

        self.assertEqual(result["user"]["userId"], "user_1")

    def test_each_failure_increments_the_counter(self):
        for _ in range(3):
            with self.assertRaises(ConsoleError):
                self.service.login({"email": self.user["email"], "password": "wrong"}, NOW)
        self.assertEqual(self.console_repo.get_login_failures(self.user["email"], NOW), 3)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.console_repo, self.admin_repo, self.admin_service, self.service = build_service(
            token_factory=lambda: "valid-token"
        )
        self.user = make_user("user_1")
        self.console_repo.seed_user(self.user)
        self.login = self.service.login(
            {"email": self.user["email"], "password": "correct horse battery"}, NOW
        )

    def test_valid_session_resolves_to_user(self):
        resolved = self.service.authenticate("valid-token", NOW)
        self.assertEqual(resolved["userId"], "user_1")

    def test_missing_or_unknown_token_is_unauthenticated(self):
        for token in (None, "not-a-real-token"):
            with self.subTest(token=token):
                with self.assertRaises(ConsoleError) as ctx:
                    self.service.authenticate(token, NOW)
                self.assertEqual(ctx.exception.status, 401)
                self.assertEqual(ctx.exception.code, "UNAUTHENTICATED")

    def test_expired_session_row_still_present_is_rejected(self):
        from datetime import timedelta

        later = NOW + timedelta(hours=13)
        with self.assertRaises(ConsoleError) as ctx:
            self.service.authenticate("valid-token", later)
        self.assertEqual(ctx.exception.code, "UNAUTHENTICATED")
        # The row was never deleted by validation; expiry is enforced in code.
        self.assertIn("SESSION#" + _hash("valid-token"), self.console_repo.sessions)

    def test_user_disabled_after_session_issued_is_rejected(self):
        self.console_repo.users_by_id["user_1"]["status"] = "disabled"

        with self.assertRaises(ConsoleError) as ctx:
            self.service.authenticate("valid-token", NOW)

        self.assertEqual(ctx.exception.code, "UNAUTHENTICATED")

    def test_logout_deletes_session(self):
        self.service.logout("valid-token")
        with self.assertRaises(ConsoleError):
            self.service.authenticate("valid-token", NOW)


def _hash(token):
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthorizationMatrixTests(unittest.TestCase):
    def setUp(self):
        self.console_repo, self.admin_repo, self.admin_service, self.service = build_service()
        self.admin = make_user("user_admin", role="admin")
        self.instructor_in = make_user("user_in", role="instructor")
        self.instructor_out = make_user("user_out", role="instructor")
        for user in (self.admin, self.instructor_in, self.instructor_out):
            self.console_repo.seed_user(user)
        self.admin_repo.seed_cohort("cohort-1")
        self.console_repo.seed_assignment("cohort-1", "user_in")

    def _forbidden(self, callable_):
        with self.assertRaises(ConsoleError) as ctx:
            callable_()
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "FORBIDDEN")

    def test_user_management_routes_are_admin_only(self):
        self.service.list_users(self.admin)
        with self.subTest("list_users"):
            self._forbidden(lambda: self.service.list_users(self.instructor_in))
        with self.subTest("create_user"):
            body = {"email": "new@example.com", "name": "New", "role": "instructor", "password": "x" * 12}
            self._forbidden(lambda: self.service.create_user(self.instructor_in, body, NOW))
        with self.subTest("update_user_status"):
            self._forbidden(
                lambda: self.service.update_user_status(
                    self.instructor_in, "user_out", {"status": "disabled"}, NOW
                )
            )

    def test_create_class_is_admin_only(self):
        body = {"name": "New class", "dailyQuota": 100, "expiresAt": "2027-01-01"}
        self._forbidden(lambda: self.service.create_class(self.instructor_in, body, NOW))

    def test_instructor_assignment_routes_are_admin_only(self):
        with self.subTest("add_instructor"):
            self._forbidden(
                lambda: self.service.add_instructor(
                    self.instructor_in, "cohort-1", {"userId": "user_out"}, NOW
                )
            )
        with self.subTest("remove_instructor"):
            self._forbidden(
                lambda: self.service.remove_instructor(
                    self.instructor_in, "cohort-1", "user_out", NOW
                )
            )

    def test_instructor_refused_on_class_not_assigned_to(self):
        self.service.get_class(self.admin, "cohort-1", NOW)
        self.service.get_class(self.instructor_in, "cohort-1", NOW)
        self._forbidden(lambda: self.service.get_class(self.instructor_out, "cohort-1", NOW))

    def test_instructor_get_classes_returns_only_assigned_classes(self):
        self.admin_repo.seed_cohort("cohort-2")

        result = self.service.list_classes(self.instructor_in, NOW)

        cohort_ids = {item["cohortId"] for item in result["classes"]}
        self.assertEqual(cohort_ids, {"cohort-1"})

    def test_admin_cannot_disable_self(self):
        with self.assertRaises(ConsoleError) as ctx:
            self.service.update_user_status(self.admin, "user_admin", {"status": "disabled"}, NOW)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "CONFLICT")

    def test_assigning_admin_role_user_as_instructor_is_rejected(self):
        with self.assertRaises(ConsoleError) as ctx:
            self.service.add_instructor(self.admin, "cohort-1", {"userId": "user_admin"}, NOW)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")


class LabelGenerationTests(unittest.TestCase):
    def setUp(self):
        self.console_repo, self.admin_repo, self.admin_service, self.service = build_service()
        self.admin = make_user("user_admin", role="admin")
        self.console_repo.seed_user(self.admin)
        self.admin_repo.seed_cohort("cohort-1")

    def _issue(self, names):
        body = {"students": [{"name": name, "email": f"{index}@example.com"} for index, name in enumerate(names)]}
        return self.service.issue_students(self.admin, "cohort-1", body, NOW)

    def test_collisions_get_numeric_suffixes(self):
        result = self._issue(["Amara Okafor", "Amara Okafor", "Amara Okafor"])
        labels = [entry["label"] for entry in result["issued"]]
        self.assertEqual(labels, ["amara-okafor", "amara-okafor-2", "amara-okafor-3"])

    def test_suffixed_labels_do_not_collide_with_generated_suffixes(self):
        # A suffixed candidate can itself collide with another student's
        # literal name, or with a later-generated suffix; every candidate
        # must be checked against the whole request, not just its own base.
        with self.subTest("plain name collides with an earlier generated suffix"):
            result = self._issue(["Jane Smith", "Jane Smith", "Jane Smith 2"])
            labels = [entry["label"] for entry in result["issued"]]
            self.assertEqual(len(labels), len(set(label.casefold() for label in labels)))
            self.assertEqual(labels, ["jane-smith", "jane-smith-2", "jane-smith-2-2"])

        with self.subTest("literal suffix collides with a later generated suffix"):
            result = self._issue(["Ana Ruiz 2", "Ana Ruiz", "Ana Ruiz"])
            labels = [entry["label"] for entry in result["issued"]]
            self.assertEqual(len(labels), len(set(label.casefold() for label in labels)))
            self.assertEqual(labels, ["ana-ruiz-2", "ana-ruiz", "ana-ruiz-3"])

    def test_name_reducing_to_empty_is_validation_error(self):
        with self.subTest("punctuation only"):
            with self.assertRaises(ConsoleError) as ctx:
                self._issue(["   ---   "])
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

        with self.subTest("fully non-ASCII"):
            with self.assertRaises(ConsoleError) as ctx:
                self._issue(["中文名字"])
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")
            self.assertIn("ASCII", ctx.exception.message)

    def test_twenty_five_student_cap_is_enforced(self):
        with self.subTest("25 accepted"):
            result = self._issue([f"Student {i}" for i in range(25)])
            self.assertEqual(len(result["issued"]), 25)

            # studentCount must reflect active keys only, not the total
            # including revoked ones.
            self.service.revoke_student(
                self.admin, "cohort-1", result["issued"][0]["keyId"], NOW
            )
            classes = self.service.list_classes(self.admin, NOW)["classes"]
            klass = next(item for item in classes if item["cohortId"] == "cohort-1")
            self.assertEqual(klass["studentCount"], 24)

        with self.subTest("26 rejected"):
            with self.assertRaises(ConsoleError) as ctx:
                self._issue([f"Student {i}" for i in range(26)])
            self.assertEqual(ctx.exception.status, 400)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

    def test_get_class_students_dailyquota_is_json_safe_int_not_decimal(self):
        # The repository hands back dailyQuota as Decimal, the way boto3
        # actually does; get_class must coerce it back to int so the console
        # HTTP handler's json.dumps does not raise.
        result = self._issue(["Amara Okafor"])
        key_id = result["issued"][0]["keyId"]

        detail = self.service.get_class(self.admin, "cohort-1", NOW)

        student = next(s for s in detail["students"] if s["keyId"] == key_id)
        self.assertIsInstance(student["dailyQuota"], int)


if __name__ == "__main__":
    unittest.main()

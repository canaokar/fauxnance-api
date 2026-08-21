from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import unittest
from unittest import mock

import src.console.handler as console_handler
from src.console.handler import handler
from src.console.service import ConsoleError


NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class FakeConsoleService:
    """Hand-written double covering the method surface ConsoleHandler calls.

    Routing correctness is exercised by asserting on ``self.calls`` rather than
    by wiring a real ConsoleService, so these tests stay focused on the HTTP
    layer: path/method dispatch, the envelope, and error translation.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self.raise_on: dict[str, Exception] = {}
        self.fail_authenticate: Exception | None = None

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise self.raise_on[name]

    def login(self, body, now):
        self.calls.append(("login", body, now))
        self._maybe_raise("login")
        return {"token": "tok", "expiresAt": "2026-08-22T00:00:00Z", "user": {"userId": "u1"}}

    def authenticate(self, token, now):
        self.calls.append(("authenticate", token, now))
        if self.fail_authenticate is not None:
            raise self.fail_authenticate
        return {"userId": "u1", "role": "admin"}

    def logout(self, token):
        self.calls.append(("logout", token))
        return {}

    def get_me(self, user):
        self.calls.append(("get_me", user))
        return {"user": user}

    def list_users(self, user):
        self.calls.append(("list_users", user))
        return {"users": []}

    def create_user(self, user, body, now):
        self.calls.append(("create_user", user, body, now))
        return {"user": {"userId": "new"}}

    def update_user_status(self, user, target_user_id, body, now):
        self.calls.append(("update_user_status", user, target_user_id, body, now))
        return {"user": {"userId": target_user_id}}

    def list_classes(self, user, now):
        self.calls.append(("list_classes", user, now))
        return {"classes": []}

    def create_class(self, user, body, now):
        self.calls.append(("create_class", user, body, now))
        return {"class": {"cohortId": "c1"}}

    def get_class(self, user, cohort_id, now):
        self.calls.append(("get_class", user, cohort_id, now))
        self._maybe_raise("get_class")
        return {"class": {"cohortId": cohort_id}, "students": [], "instructors": []}

    def add_instructor(self, user, cohort_id, body, now):
        self.calls.append(("add_instructor", user, cohort_id, body, now))
        return {"instructors": []}

    def remove_instructor(self, user, cohort_id, target_user_id, now):
        self.calls.append(("remove_instructor", user, cohort_id, target_user_id, now))
        return {"instructors": []}

    def issue_students(self, user, cohort_id, body, now):
        self.calls.append(("issue_students", user, cohort_id, body, now))
        return {"issued": []}

    def revoke_student(self, user, cohort_id, key_id, now):
        self.calls.append(("revoke_student", user, cohort_id, key_id, now))
        return {"keyId": key_id, "status": "revoked", "revokedAt": "2026-08-21T12:00:00Z"}


def event(method, path, *, body=None, token="valid-token", headers=None):
    request_headers = dict(headers) if headers is not None else (
        {"authorization": f"Bearer {token}"} if token is not None else {}
    )
    request = {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "headers": request_headers,
    }
    if body is not None:
        request["body"] = json.dumps(body)
    return request


def decoded(response):
    return json.loads(response["body"])


def invoke(service, request):
    return handler(request, None, service=service, clock=lambda: NOW)


class RoutingTests(unittest.TestCase):
    def test_routes_dispatch_to_correct_operation(self):
        cases = [
            ("login bypasses authentication", "POST", "/v1/console/login",
             {"email": "a", "password": "b"}, None, 200, ["login"]),
            ("get me", "GET", "/v1/console/me", None, "valid-token", 200,
             ["authenticate", "get_me"]),
            ("logout", "POST", "/v1/console/logout", None, "valid-token", 200,
             ["authenticate", "logout"]),
            ("list users", "GET", "/v1/console/users", None, "valid-token", 200,
             ["authenticate", "list_users"]),
            ("create user", "POST", "/v1/console/users", {"email": "a@example.com"},
             "valid-token", 201, ["authenticate", "create_user"]),
            ("update user status", "PATCH", "/v1/console/users/user_abc123",
             {"status": "disabled"}, "valid-token", 200,
             ["authenticate", "update_user_status"]),
            ("list classes", "GET", "/v1/console/classes", None, "valid-token", 200,
             ["authenticate", "list_classes"]),
            ("create class", "POST", "/v1/console/classes",
             {"name": "x", "dailyQuota": 1, "expiresAt": "2027-01-01"}, "valid-token", 201,
             ["authenticate", "create_class"]),
            ("get class detail", "GET", "/v1/console/classes/cohort-1", None, "valid-token",
             200, ["authenticate", "get_class"]),
            ("add instructor", "POST", "/v1/console/classes/cohort-1/instructors",
             {"userId": "u2"}, "valid-token", 201, ["authenticate", "add_instructor"]),
            ("remove instructor", "DELETE", "/v1/console/classes/cohort-1/instructors/user-2",
             None, "valid-token", 200, ["authenticate", "remove_instructor"]),
            ("issue students", "POST", "/v1/console/classes/cohort-1/students",
             {"students": [{"name": "A", "email": "a@example.com"}]}, "valid-token", 201,
             ["authenticate", "issue_students"]),
            ("revoke student", "DELETE", "/v1/console/classes/cohort-1/students/student-1",
             None, "valid-token", 200, ["authenticate", "revoke_student"]),
        ]
        for description, method, path, body, token, expected_status, expected_calls in cases:
            with self.subTest(description):
                service = FakeConsoleService()
                response = invoke(service, event(method, path, body=body, token=token))
                self.assertEqual(response["statusCode"], expected_status)
                self.assertEqual([call[0] for call in service.calls], expected_calls)

        with self.subTest("path parameters are extracted correctly"):
            service = FakeConsoleService()
            invoke(service, event("PATCH", "/v1/console/users/user_abc123", body={"status": "disabled"}))
            update_call = next(c for c in service.calls if c[0] == "update_user_status")
            self.assertEqual(update_call[2], "user_abc123")

            service = FakeConsoleService()
            invoke(service, event("DELETE", "/v1/console/classes/cohort-1/instructors/user-2"))
            remove_call = next(c for c in service.calls if c[0] == "remove_instructor")
            self.assertEqual((remove_call[2], remove_call[3]), ("cohort-1", "user-2"))

            service = FakeConsoleService()
            invoke(service, event("DELETE", "/v1/console/classes/cohort-1/students/student-1"))
            revoke_call = next(c for c in service.calls if c[0] == "revoke_student")
            self.assertEqual((revoke_call[2], revoke_call[3]), ("cohort-1", "student-1"))

    def test_unknown_route_or_method_is_404(self):
        service = FakeConsoleService()
        with self.subTest("unknown path"):
            response = invoke(service, event("GET", "/v1/console/nope"))
            self.assertEqual(response["statusCode"], 404)
            self.assertEqual(decoded(response)["error"]["code"], "NOT_FOUND")

        with self.subTest("wrong method on known path does not authenticate"):
            service = FakeConsoleService()
            response = invoke(service, event("GET", "/v1/console/login", token=None))
            self.assertEqual(response["statusCode"], 404)
            self.assertEqual(service.calls, [])

        with self.subTest("unknown nested path under classes"):
            response = invoke(
                service, event("PUT", "/v1/console/classes/cohort-1/nope/extra/segments")
            )
            self.assertEqual(response["statusCode"], 404)

    def test_bad_cohort_id_shape_is_400(self):
        service = FakeConsoleService()
        response = invoke(service, event("GET", "/v1/console/classes/../etc"))
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(decoded(response)["error"]["code"], "VALIDATION_ERROR")


class EnvelopeTests(unittest.TestCase):
    def test_success_envelope_has_data_and_meta(self):
        service = FakeConsoleService()
        response = invoke(service, event("GET", "/v1/console/me"))
        self.assertEqual(response["statusCode"], 200)
        body = decoded(response)
        self.assertIn("data", body)
        self.assertIn("meta", body)
        self.assertEqual(body["meta"]["asOf"], "2026-08-21T12:00:00Z")

    def test_known_console_error_maps_to_error_envelope(self):
        service = FakeConsoleService()
        service.raise_on["get_class"] = ConsoleError(403, "FORBIDDEN", "nope")
        response = invoke(service, event("GET", "/v1/console/classes/cohort-1"))
        self.assertEqual(response["statusCode"], 403)
        body = decoded(response)
        self.assertEqual(body["error"], {"code": "FORBIDDEN", "message": "nope", "details": {}})


class AuthHeaderTests(unittest.TestCase):
    def test_missing_or_malformed_authorization_is_401(self):
        cases = {
            "missing header": {"token": None},
            "malformed scheme": {"headers": {"authorization": "Basic abc123"}},
        }
        for description, kwargs in cases.items():
            with self.subTest(description):
                service = FakeConsoleService()
                service.fail_authenticate = ConsoleError(401, "UNAUTHENTICATED", "nope")
                response = invoke(service, event("GET", "/v1/console/me", **kwargs))
                self.assertEqual(response["statusCode"], 401)
                self.assertEqual(decoded(response)["error"]["code"], "UNAUTHENTICATED")
                call = next(c for c in service.calls if c[0] == "authenticate")
                self.assertIsNone(call[1])


class BodyParsingTests(unittest.TestCase):
    def test_malformed_request_body_is_400(self):
        cases = {
            "missing body": None,
            "malformed json": "{not json",
            "non-object json": "[1, 2, 3]",
        }
        for description, raw_body in cases.items():
            with self.subTest(description):
                service = FakeConsoleService()
                request = event("POST", "/v1/console/login", token=None)
                if raw_body is None:
                    request.pop("body", None)
                else:
                    request["body"] = raw_body
                response = invoke(service, request)
                self.assertEqual(response["statusCode"], 400)
                self.assertEqual(decoded(response)["error"]["code"], "VALIDATION_ERROR")


class UnexpectedExceptionTests(unittest.TestCase):
    def test_unexpected_exception_becomes_500_without_internal_detail(self):
        cases = {
            "authenticated route": ("get_class", "GET", "/v1/console/classes/cohort-1", "valid-token"),
            "unauthenticated route": ("login", "POST", "/v1/console/login", None),
        }
        for description, (op, method, path, token) in cases.items():
            with self.subTest(description):
                service = FakeConsoleService()
                service.raise_on[op] = RuntimeError("db connection string: secret-stuff")
                body = {"email": "a", "password": "b"} if op == "login" else None
                response = invoke(service, event(method, path, body=body, token=token))
                self.assertEqual(response["statusCode"], 500)
                decoded_body = decoded(response)
                self.assertEqual(decoded_body["error"]["code"], "INTERNAL_ERROR")
                self.assertNotIn("secret-stuff", json.dumps(decoded_body))
                self.assertNotIn("Traceback", json.dumps(decoded_body))


class DefaultServiceRepositoryClientTests(unittest.TestCase):
    def test_default_service_builds_repositories_with_standalone_dynamodb_client(self):
        resource = mock.Mock(name="dynamodb_resource")
        low_level_client = mock.Mock(name="dynamodb_client")

        with mock.patch.object(
            console_handler, "_service", None
        ), mock.patch("boto3.resource", return_value=resource), mock.patch(
            "boto3.client", return_value=low_level_client
        ) as client_mock, mock.patch.dict(
            os.environ, {"CONTROL_TABLE": "control"}
        ):
            service = console_handler._default_service()

        client_mock.assert_any_call("dynamodb")
        self.assertIs(service._repository._client, low_level_client)
        self.assertIsNot(service._repository._client, resource.meta.client)
        self.assertIs(service._admin_service._repository._client, low_level_client)


if __name__ == "__main__":
    unittest.main()

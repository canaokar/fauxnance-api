"""HTTP API entry point for the instructor console."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import json
import logging
import os
import re
from typing import Any, Callable, Mapping

from src.admin.handler import AdminService
from src.admin.repository import DynamoAdminRepository
from src.console.repository import DynamoConsoleRepository
from src.console.service import ConsoleError, ConsoleService


_LOGGER = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_PREFIX = "/v1/console"


class ConsoleHandler:
    def __init__(
        self,
        service: ConsoleService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        now = _as_utc(self._clock())
        method = _method(event)
        path = str(event.get("rawPath") or event.get("path") or "")

        try:
            if path == f"{_PREFIX}/login":
                if method != "POST":
                    raise ConsoleError(404, "NOT_FOUND", "Route was not found.")
                return _success(self._service.login(_body(event), now), now)

            token = _bearer_token(event)
            user = self._service.authenticate(token, now)

            if path == f"{_PREFIX}/logout" and method == "POST":
                return _success(self._service.logout(token or ""), now)
            if path == f"{_PREFIX}/me" and method == "GET":
                return _success(self._service.get_me(user), now)
            if path == f"{_PREFIX}/users" and method == "GET":
                return _success(self._service.list_users(user), now)
            if path == f"{_PREFIX}/users" and method == "POST":
                return _success(self._service.create_user(user, _body(event), now), now, status=201)
            if path.startswith(f"{_PREFIX}/users/") and method == "PATCH":
                target_id = _identifier_value(
                    path.removeprefix(f"{_PREFIX}/users/"), "userId"
                )
                return _success(
                    self._service.update_user_status(user, target_id, _body(event), now), now
                )
            if path == f"{_PREFIX}/classes" and method == "GET":
                return _success(self._service.list_classes(user, now), now)
            if path == f"{_PREFIX}/classes" and method == "POST":
                return _success(self._service.create_class(user, _body(event), now), now, status=201)
            if path.startswith(f"{_PREFIX}/classes/"):
                return self._route_class(event, path, method, user, now)

            raise ConsoleError(404, "NOT_FOUND", "Route was not found.")
        except ConsoleError as exc:
            return _error(exc.status, exc.code, exc.message)

    def _route_class(
        self,
        event: Mapping[str, Any],
        path: str,
        method: str,
        user: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        remainder = path.removeprefix(f"{_PREFIX}/classes/")
        segments = [segment for segment in remainder.split("/") if segment]

        if len(segments) == 1:
            cohort_id = _identifier_value(segments[0], "cohortId")
            if method == "GET":
                return _success(self._service.get_class(user, cohort_id, now), now)

        elif len(segments) == 2:
            cohort_id = _identifier_value(segments[0], "cohortId")
            resource = segments[1]
            if resource == "instructors" and method == "POST":
                return _success(
                    self._service.add_instructor(user, cohort_id, _body(event), now),
                    now,
                    status=201,
                )
            if resource == "students" and method == "POST":
                return _success(
                    self._service.issue_students(user, cohort_id, _body(event), now),
                    now,
                    status=201,
                )

        elif len(segments) == 3:
            cohort_id = _identifier_value(segments[0], "cohortId")
            resource = segments[1]
            if resource == "instructors" and method == "DELETE":
                target_user_id = _identifier_value(segments[2], "userId")
                return _success(
                    self._service.remove_instructor(user, cohort_id, target_user_id, now), now
                )
            if resource == "students" and method == "DELETE":
                key_id = _identifier_value(segments[2], "keyId")
                return _success(
                    self._service.revoke_student(user, cohort_id, key_id, now), now
                )

        raise ConsoleError(404, "NOT_FOUND", "Route was not found.")


_service: ConsoleService | None = None


def handler(
    event: Mapping[str, Any],
    _context: object,
    *,
    service: ConsoleService | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Handle one API Gateway payload-v2 request."""

    try:
        active_service = service or _default_service()
        return ConsoleHandler(active_service, clock=clock).handle(event)
    except Exception:
        _LOGGER.exception("Console API request failed")
        return _error(500, "INTERNAL_ERROR", "An unexpected error occurred.")


lambda_handler = handler


def _default_service() -> ConsoleService:
    global _service
    if _service is None:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        client = boto3.client("dynamodb")
        control_table = dynamodb.Table(os.environ["CONTROL_TABLE"])
        admin_repository = DynamoAdminRepository(control_table, dynamodb, client=client)
        _service = ConsoleService(
            DynamoConsoleRepository(control_table, dynamodb, client=client),
            AdminService(admin_repository, stage=os.environ.get("STAGE", "dev")),
            admin_repository,
        )
    return _service


def _method(event: Mapping[str, Any]) -> str:
    return str(
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or ""
    ).upper()


def _bearer_token(event: Mapping[str, Any]) -> str | None:
    headers = event.get("headers")
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "authorization":
            if not isinstance(value, str) or not value.startswith("Bearer "):
                return None
            token = value.removeprefix("Bearer ").strip()
            return token or None
    return None


def _body(event: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = event.get("body")
    if not isinstance(raw, str):
        raise ConsoleError(400, "VALIDATION_ERROR", "A JSON object body is required.")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception as exc:
            raise ConsoleError(
                400, "VALIDATION_ERROR", "Request body is not valid base64 UTF-8."
            ) from exc
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConsoleError(400, "VALIDATION_ERROR", "Request body must be valid JSON.") from exc
    if not isinstance(body, dict):
        raise ConsoleError(400, "VALIDATION_ERROR", "Request body must be a JSON object.")
    return body


def _identifier_value(value: str, field: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ConsoleError(400, "VALIDATION_ERROR", f"{field} is invalid.")
    return value


def _success(data: Any, now: datetime, *, status: int = 200) -> dict[str, Any]:
    return _response(status, {"data": data, "meta": {"asOf": _timestamp(now)}})


def _error(status: int, code: str, message: str) -> dict[str, Any]:
    return _response(status, {"error": {"code": code, "message": message, "details": {}}})


def _response(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)

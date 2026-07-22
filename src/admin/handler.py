"""HTTP API entry point for instructor-only cohort and key operations."""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime
import hashlib
import json
import logging
import os
import re
import secrets
import string
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from src.admin.repository import DynamoAdminRepository, RepositoryConflict
from src.shared.auth import AuthContext, is_expired, parse_authorizer_context


DISCLAIMER = "Educational data. Not for investment use."
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_UNSAFE_SPREADSHEET_PREFIXES = ("=", "+", "-", "@")
_BASE62 = string.ascii_letters + string.digits
_LOGGER = logging.getLogger(__name__)


class AdminRepository(Protocol):
    def create_cohort(self, item: Mapping[str, Any]) -> None: ...
    def get_cohort(self, cohort_id: str) -> Mapping[str, Any] | None: ...
    def update_cohort(self, item: Mapping[str, Any]) -> None: ...
    def all_cohorts(self) -> list[Mapping[str, Any]]: ...
    def query_cohort_keys(self, cohort_id: str, *, limit: int | None = None, cursor: Mapping[str, Any] | None = None) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]: ...
    def usage_counts(self, key_ids: Sequence[str], usage_date: date) -> dict[str, int]: ...
    def issue_keys(self, cohort: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> None: ...
    def get_key_lookup(self, key_id: str) -> Mapping[str, Any] | None: ...
    def revoke_key(self, lookup: Mapping[str, Any], *, revoked_at: str) -> None: ...


class AdminError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class AdminService:
    def __init__(
        self,
        repository: AdminRepository,
        *,
        stage: str,
        clock: Callable[[], datetime] | None = None,
        identifier: Callable[[str], str] | None = None,
        plaintext_key: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._stage = stage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier = identifier or (lambda prefix: f"{prefix}_{uuid4().hex[:16]}")
        self._plaintext_key = plaintext_key or self._generate_key

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        now = _as_utc(self._clock())
        try:
            auth = _authorizer_context(event)
            if auth.key_type != "admin":
                raise AdminError(403, "ADMIN_ONLY", "An admin API key is required.")

            method = _method(event)
            path = str(event.get("rawPath") or event.get("path") or "")
            if path == "/v1/admin/cohorts":
                if method == "POST":
                    return _success(self._create_cohort(_body(event), now), now, status=201)
                if method == "GET":
                    return _success(self._list_cohorts(_query(event), now), now)
            if path.startswith("/v1/admin/cohorts/"):
                cohort_id = _path_id(event, path, "/v1/admin/cohorts/", "cohortId")
                if method == "GET":
                    return _success(self._get_cohort(cohort_id, now), now)
                if method == "PATCH":
                    return _success(self._patch_cohort(cohort_id, _body(event), now), now)
            if path == "/v1/admin/keys":
                if method == "POST":
                    return _success(self._issue_keys(_body(event), now), now, status=201)
                if method == "GET":
                    return _success(self._list_keys(_query(event), now), now)
            if path.startswith("/v1/admin/keys/") and method == "DELETE":
                key_id = _path_id(event, path, "/v1/admin/keys/", "keyId")
                return _success(self._revoke_key(key_id, now), now)
            raise AdminError(404, "NOT_FOUND", "Route was not found.")
        except AdminError as exc:
            return _error(exc.status, exc.code, exc.message)
        except RepositoryConflict as exc:
            return _error(409, "CONFLICT", str(exc))

    def _create_cohort(self, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        _exact_fields(body, {"name", "defaultDailyQuota", "expiresAt"})
        name = _name(body["name"])
        quota = _quota(body["defaultDailyQuota"])
        expires = _expiry(body["expiresAt"], today=now.date())
        cohort_id = self._identifier("cohort")
        timestamp = _timestamp(now)
        item = {
            "PK": f"COHORT#{cohort_id}",
            "SK": "META",
            "cohortId": cohort_id,
            "name": name,
            "defaultDailyQuota": quota,
            "expiresAt": expires,
            "status": "active",
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        self._repository.create_cohort(item)
        return _cohort_data(item)

    def _get_cohort(self, cohort_id: str, now: datetime) -> dict[str, Any]:
        item = self._required_cohort(cohort_id)
        stats = self._cohort_stats(cohort_id, now.date())
        return {**_cohort_data(item), **stats}

    def _list_cohorts(self, query: Mapping[str, str], now: datetime) -> dict[str, Any]:
        _allowed_query(query, {"limit", "cursor"})
        limit = _limit(query.get("limit"), default=50, maximum=200)
        offset = _offset_cursor(query.get("cursor"))
        cohorts = sorted(
            self._repository.all_cohorts(),
            key=lambda item: (str(item.get("createdAt", "")), str(item.get("cohortId", item.get("SK", "")))),
        )
        page = cohorts[offset : offset + limit]
        values = []
        for item in page:
            cohort_id = str(item.get("cohortId") or item.get("SK"))
            values.append({**_cohort_data({**item, "cohortId": cohort_id}), **self._cohort_stats(cohort_id, now.date())})
        next_cursor = _encode_offset(offset + limit) if offset + limit < len(cohorts) else None
        return {"cohorts": values, "cursor": next_cursor}

    def _patch_cohort(self, cohort_id: str, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        allowed = {"name", "defaultDailyQuota", "expiresAt", "status"}
        if not body or not set(body).issubset(allowed):
            raise AdminError(400, "VALIDATION_ERROR", "PATCH requires only supported cohort fields.")
        existing = dict(self._required_cohort(cohort_id))
        if "name" in body:
            existing["name"] = _name(body["name"])
        if "defaultDailyQuota" in body:
            existing["defaultDailyQuota"] = _quota(body["defaultDailyQuota"])
        if "expiresAt" in body:
            existing["expiresAt"] = _expiry(body["expiresAt"], today=now.date())
        if "status" in body:
            if body["status"] not in {"active", "inactive"}:
                raise AdminError(400, "VALIDATION_ERROR", "status must be active or inactive.")
            existing["status"] = body["status"]
        existing.update({"PK": f"COHORT#{cohort_id}", "SK": "META", "cohortId": cohort_id, "updatedAt": _timestamp(now)})
        self._repository.update_cohort(existing)
        return _cohort_data(existing)

    def _issue_keys(self, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        allowed = {"cohortId", "labels", "count", "dailyQuota"}
        if not set(body).issubset(allowed) or "cohortId" not in body:
            raise AdminError(400, "VALIDATION_ERROR", "Key issuance fields are invalid.")
        cohort_id = _identifier_value(body["cohortId"], "cohortId")
        has_labels = "labels" in body
        has_count = "count" in body
        if has_labels == has_count:
            raise AdminError(400, "VALIDATION_ERROR", "Provide exactly one of labels or count.")
        if has_labels:
            raw_labels = body["labels"]
            if not isinstance(raw_labels, list) or not 1 <= len(raw_labels) <= 25:
                raise AdminError(400, "VALIDATION_ERROR", "labels must contain 1 to 25 entries.")
            labels = [_label(value) for value in raw_labels]
        else:
            count = body["count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 25:
                raise AdminError(400, "VALIDATION_ERROR", "count must be an integer from 1 to 25.")
            labels = [f"student-{index:03d}" for index in range(1, count + 1)]
        if len({label.casefold() for label in labels}) != len(labels):
            raise AdminError(400, "VALIDATION_ERROR", "labels must be unique.")

        cohort = dict(self._required_cohort(cohort_id))
        if cohort.get("status") != "active" or is_expired(cohort.get("expiresAt"), now=now):
            raise AdminError(409, "COHORT_INACTIVE", "The cohort is inactive or expired.")
        quota = _quota(body["dailyQuota"]) if "dailyQuota" in body else _quota(cohort.get("defaultDailyQuota"))
        timestamp = _timestamp(now)
        issued: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        for label in labels:
            plaintext = self._plaintext_key()
            key_id = self._identifier("student")
            records.append({"keyId": key_id, "keyHash": hashlib.sha256(plaintext.encode()).hexdigest(), "label": label, "dailyQuota": quota, "createdAt": timestamp})
            issued.append({"keyId": key_id, "label": label, "key": plaintext})
        self._repository.issue_keys({**cohort, "cohortId": cohort_id}, records)
        return {"cohortId": cohort_id, "keys": issued}

    def _list_keys(self, query: Mapping[str, str], now: datetime) -> dict[str, Any]:
        _allowed_query(query, {"cohortId", "limit", "cursor"})
        if "cohortId" not in query:
            raise AdminError(400, "VALIDATION_ERROR", "cohortId is required.")
        cohort_id = _identifier_value(query["cohortId"], "cohortId")
        self._required_cohort(cohort_id)
        limit = _limit(query.get("limit"), default=50, maximum=200)
        cursor = _decode_key_cursor(query.get("cursor"), cohort_id=cohort_id)
        items, next_key = self._repository.query_cohort_keys(cohort_id, limit=limit, cursor=cursor)
        usage = self._repository.usage_counts([str(item["keyId"]) for item in items], now.date())
        keys = [_key_data(item, usage.get(str(item["keyId"]), 0)) for item in items]
        return {"cohortId": cohort_id, "keys": keys, "cursor": _encode_key_cursor(next_key) if next_key else None}

    def _revoke_key(self, key_id: str, now: datetime) -> dict[str, Any]:
        lookup = self._repository.get_key_lookup(key_id)
        if not lookup:
            raise AdminError(404, "KEY_NOT_FOUND", "API key was not found.")
        required = {"keyHash", "cohortId"}
        if not required.issubset(lookup):
            raise AdminError(409, "CONFLICT", "API key lookup is incomplete.")
        self._repository.revoke_key({**lookup, "keyId": key_id}, revoked_at=_timestamp(now))
        return {"keyId": key_id, "status": "revoked", "revokedAt": _timestamp(now)}

    def _required_cohort(self, cohort_id: str) -> Mapping[str, Any]:
        item = self._repository.get_cohort(cohort_id)
        if not item:
            raise AdminError(404, "COHORT_NOT_FOUND", "Cohort was not found.")
        return item

    def _cohort_stats(self, cohort_id: str, usage_date: date) -> dict[str, Any]:
        items, cursor = self._repository.query_cohort_keys(cohort_id)
        while cursor:
            page, cursor = self._repository.query_cohort_keys(cohort_id, cursor=cursor)
            items.extend(page)
        key_ids = [str(item["keyId"]) for item in items]
        usage = self._repository.usage_counts(key_ids, usage_date)
        return {
            "keyCounts": {
                "total": len(items),
                "active": sum(item.get("status") == "active" for item in items),
                "revoked": sum(item.get("status") == "revoked" for item in items),
            },
            "usedToday": sum(usage.values()),
        }

    def _generate_key(self) -> str:
        token = "".join(secrets.choice(_BASE62) for _ in range(32))
        return f"fnx_{self._stage}_{token}"


_service: AdminService | None = None


def handler(event: Mapping[str, Any], _context: object, *, service: AdminService | None = None) -> dict[str, Any]:
    try:
        return (service or _default_service()).handle(event)
    except Exception:
        _LOGGER.exception("Admin API request failed")
        return _error(500, "INTERNAL_ERROR", "An unexpected error occurred.")


lambda_handler = handler


def _default_service() -> AdminService:
    global _service
    if _service is None:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        table = dynamodb.Table(os.environ["CONTROL_TABLE"])
        _service = AdminService(DynamoAdminRepository(table, dynamodb), stage=os.environ.get("STAGE", "dev"))
    return _service


def _authorizer_context(event: Mapping[str, Any]) -> AuthContext:
    try:
        return parse_authorizer_context(event)
    except ValueError as exc:
        raise AdminError(500, "INTERNAL_ERROR", "Request identity is unavailable.") from exc


def _method(event: Mapping[str, Any]) -> str:
    return str(event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "").upper()


def _body(event: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = event.get("body")
    if not isinstance(raw, str):
        raise AdminError(400, "VALIDATION_ERROR", "A JSON object body is required.")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception as exc:
            raise AdminError(400, "VALIDATION_ERROR", "Request body is not valid base64 UTF-8.") from exc
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdminError(400, "VALIDATION_ERROR", "Request body must be valid JSON.") from exc
    if not isinstance(body, dict):
        raise AdminError(400, "VALIDATION_ERROR", "Request body must be a JSON object.")
    return body


def _query(event: Mapping[str, Any]) -> dict[str, str]:
    raw = event.get("queryStringParameters")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value is not None}


def _path_id(event: Mapping[str, Any], path: str, prefix: str, field: str) -> str:
    parameters = event.get("pathParameters")
    raw = parameters.get(field) if isinstance(parameters, Mapping) else None
    return _identifier_value(raw or path.removeprefix(prefix), field)


def _identifier_value(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AdminError(400, "VALIDATION_ERROR", f"{field} is invalid.")
    return value


def _exact_fields(body: Mapping[str, Any], expected: set[str]) -> None:
    if set(body) != expected:
        raise AdminError(400, "VALIDATION_ERROR", "Request fields do not match the endpoint contract.")


def _allowed_query(query: Mapping[str, str], allowed: set[str]) -> None:
    if not set(query).issubset(allowed):
        raise AdminError(400, "VALIDATION_ERROR", "Query parameters are invalid.")


def _name(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 120:
        raise AdminError(400, "VALIDATION_ERROR", "name must contain 1 to 120 characters.")
    return value.strip()


def _label(value: object) -> str:
    if not isinstance(value, str):
        raise AdminError(
            400, "VALIDATION_ERROR", "Each label must be 1 to 120 safe characters."
        )
    label = value.strip()
    if (
        not 1 <= len(label) <= 120
        or label.startswith(_UNSAFE_SPREADSHEET_PREFIXES)
        or any(ord(character) < 32 for character in label)
    ):
        raise AdminError(
            400, "VALIDATION_ERROR", "Each label must be 1 to 120 safe characters."
        )
    return label


def _quota(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100_000:
        raise AdminError(400, "VALIDATION_ERROR", "daily quota must be an integer from 1 to 100000.")
    return value


def _expiry(value: object, *, today: date) -> str:
    if not isinstance(value, str):
        raise AdminError(400, "VALIDATION_ERROR", "expiresAt must be a YYYY-MM-DD date.")
    try:
        expiry = date.fromisoformat(value)
    except ValueError as exc:
        raise AdminError(400, "VALIDATION_ERROR", "expiresAt must be a YYYY-MM-DD date.") from exc
    if expiry < today:
        raise AdminError(400, "VALIDATION_ERROR", "expiresAt must not be in the past.")
    return expiry.isoformat()


def _limit(value: str | None, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        limit = int(value)
    except ValueError as exc:
        raise AdminError(400, "VALIDATION_ERROR", "limit must be an integer.") from exc
    if str(limit) != value or not 1 <= limit <= maximum:
        raise AdminError(400, "VALIDATION_ERROR", f"limit must be from 1 to {maximum}.")
    return limit


def _encode_offset(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}, separators=(",", ":")).encode()).decode().rstrip("=")


def _offset_cursor(value: str | None) -> int:
    if value is None:
        return 0
    document = _decode_cursor(value)
    offset = document.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise AdminError(400, "VALIDATION_ERROR", "cursor is invalid.")
    return offset


def _encode_key_cursor(key: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(dict(key), separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_key_cursor(
    value: str | None, *, cohort_id: str
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    document = _decode_cursor(value)
    if (
        set(document) != {"PK", "SK"}
        or document.get("PK") != f"COHORT#{cohort_id}"
        or not str(document.get("SK", "")).startswith("KEY#")
    ):
        raise AdminError(400, "VALIDATION_ERROR", "cursor is invalid.")
    return document


def _decode_cursor(value: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(value) % 4)
        document = json.loads(base64.urlsafe_b64decode(value + padding))
    except Exception as exc:
        raise AdminError(400, "VALIDATION_ERROR", "cursor is invalid.") from exc
    if not isinstance(document, dict):
        raise AdminError(400, "VALIDATION_ERROR", "cursor is invalid.")
    return document


def _cohort_data(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("cohortId", "name", "defaultDailyQuota", "expiresAt", "status", "createdAt", "updatedAt") if item.get(key) is not None}


def _key_data(item: Mapping[str, Any], used_today: int) -> dict[str, Any]:
    return {key: item.get(key) for key in ("keyId", "label", "dailyQuota", "status", "createdAt", "expiresAt", "revokedAt") if item.get(key) is not None} | {"usedToday": used_today}


def _success(data: Any, now: datetime, *, status: int = 200) -> dict[str, Any]:
    return _response(status, {"data": data, "meta": {"asOf": _timestamp(now), "disclaimer": DISCLAIMER}})


def _error(status: int, code: str, message: str) -> dict[str, Any]:
    return _response(status, {"error": {"code": code, "message": message, "details": {}}})


def _response(status: int, body: Any) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, separators=(",", ":"))}


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)

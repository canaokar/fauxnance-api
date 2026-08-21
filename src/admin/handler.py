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
from src.shared.symbols import Market, canonical_symbol, parse_symbol


DISCLAIMER = "Educational data. Not for investment use."
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_UNSAFE_SPREADSHEET_PREFIXES = ("=", "+", "-", "@")
_BASE62 = string.ascii_letters + string.digits
_UNIVERSES = frozenset(
    {
        "us-phase2-v1",
        "india-phase3-v1",
        "fx-phase3-v1",
        "crypto-phase3-v1",
    }
)
_UNIVERSE_SIZES = {
    "us-phase2-v1": 515,
    "india-phase3-v1": 30,
    "fx-phase3-v1": 12,
    "crypto-phase3-v1": 12,
}
_MAX_BACKFILL_WORK = 8_000
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
    def seed_symbols(self, symbols: Sequence[Mapping[str, Any]]) -> None: ...
    def get_symbol(self, symbol: str) -> Mapping[str, Any] | None: ...
    def create_backfill_job(self, item: Mapping[str, Any]) -> None: ...
    def get_backfill_job(self, job_id: str) -> Mapping[str, Any] | None: ...
    def record_backfill_dispatch_error(self, job_id: str, *, error: str, updated_at: str) -> None: ...
    def backfill_failures(self, job_id: str, *, limit: int) -> tuple[list[Mapping[str, Any]], bool]: ...


class CoordinatorInvoker(Protocol):
    def invoke(self, job_id: str) -> None: ...


class LambdaCoordinatorInvoker:
    def __init__(self, client: Any, function_name: str) -> None:
        self._client = client
        self._function_name = function_name

    def invoke(self, job_id: str) -> None:
        response = self._client.invoke(
            FunctionName=self._function_name,
            InvocationType="Event",
            Payload=json.dumps({"jobId": job_id}, separators=(",", ":")).encode(),
        )
        if int(response.get("StatusCode", 0)) != 202:
            raise RuntimeError("backfill coordinator did not accept the job")


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
        coordinator: CoordinatorInvoker | None = None,
    ) -> None:
        self._repository = repository
        self._stage = stage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._identifier = identifier or (lambda prefix: f"{prefix}_{uuid4().hex[:16]}")
        self._plaintext_key = plaintext_key or self._generate_key
        self._coordinator = coordinator

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
                    return _success(self.create_cohort(_body(event), now), now, status=201)
                if method == "GET":
                    return _success(self.list_cohorts(_query(event), now), now)
            if path.startswith("/v1/admin/cohorts/"):
                cohort_id = _path_id(event, path, "/v1/admin/cohorts/", "cohortId")
                if method == "GET":
                    return _success(self.get_cohort_detail(cohort_id, now), now)
                if method == "PATCH":
                    return _success(self._patch_cohort(cohort_id, _body(event), now), now)
            if path == "/v1/admin/keys":
                if method == "POST":
                    return _success(self.issue_keys(_body(event), now), now, status=201)
                if method == "GET":
                    return _success(self.list_keys(_query(event), now), now)
            if path.startswith("/v1/admin/keys/") and method == "DELETE":
                key_id = _path_id(event, path, "/v1/admin/keys/", "keyId")
                return _success(self.revoke_key(key_id, now), now)
            if path == "/v1/admin/symbols" and method == "POST":
                return _success(
                    self._register_symbols(_body(event)), now, status=201
                )
            if path == "/v1/admin/ingest/backfill" and method == "POST":
                return _success(
                    self._start_backfill(_body(event), now), now, status=202
                )
            if (
                path.startswith("/v1/admin/ingest/jobs/")
                and method == "GET"
            ):
                job_id = _path_id(
                    event, path, "/v1/admin/ingest/jobs/", "jobId"
                )
                return _success(self._backfill_status(job_id), now)
            raise AdminError(404, "NOT_FOUND", "Route was not found.")
        except AdminError as exc:
            return _error(exc.status, exc.code, exc.message)
        except RepositoryConflict as exc:
            return _error(409, "CONFLICT", str(exc))

    def create_cohort(self, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
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

    def get_cohort_detail(self, cohort_id: str, now: datetime) -> dict[str, Any]:
        item = self.required_cohort(cohort_id)
        stats = self.cohort_stats(cohort_id, now.date())
        return {**_cohort_data(item), **stats}

    def list_cohorts(self, query: Mapping[str, str], now: datetime) -> dict[str, Any]:
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
            values.append({**_cohort_data({**item, "cohortId": cohort_id}), **self.cohort_stats(cohort_id, now.date())})
        next_cursor = _encode_offset(offset + limit) if offset + limit < len(cohorts) else None
        return {"cohorts": values, "cursor": next_cursor}

    def _patch_cohort(self, cohort_id: str, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        allowed = {"name", "defaultDailyQuota", "expiresAt", "status"}
        if not body or not set(body).issubset(allowed):
            raise AdminError(400, "VALIDATION_ERROR", "PATCH requires only supported cohort fields.")
        existing = dict(self.required_cohort(cohort_id))
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

    def issue_keys(self, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        allowed = {"cohortId", "labels", "students", "count", "dailyQuota"}
        if not set(body).issubset(allowed) or "cohortId" not in body:
            raise AdminError(400, "VALIDATION_ERROR", "Key issuance fields are invalid.")
        cohort_id = _identifier_value(body["cohortId"], "cohortId")
        has_labels = "labels" in body
        has_students = "students" in body
        has_count = "count" in body
        if sum((has_labels, has_students, has_count)) != 1:
            raise AdminError(400, "VALIDATION_ERROR", "Provide exactly one of labels, students, or count.")
        identities: list[dict[str, str]] = []
        if has_labels:
            raw_labels = body["labels"]
            if not isinstance(raw_labels, list) or not 1 <= len(raw_labels) <= 25:
                raise AdminError(400, "VALIDATION_ERROR", "labels must contain 1 to 25 entries.")
            labels = [_label(value) for value in raw_labels]
        elif has_students:
            raw_students = body["students"]
            if not isinstance(raw_students, list) or not 1 <= len(raw_students) <= 25:
                raise AdminError(400, "VALIDATION_ERROR", "students must contain 1 to 25 entries.")
            labels = []
            for value in raw_students:
                label, name, email = _student_identity(value)
                labels.append(label)
                identities.append({"studentName": name, "studentEmail": email})
        else:
            count = body["count"]
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 25:
                raise AdminError(400, "VALIDATION_ERROR", "count must be an integer from 1 to 25.")
            labels = [f"student-{index:03d}" for index in range(1, count + 1)]
        if len({label.casefold() for label in labels}) != len(labels):
            raise AdminError(400, "VALIDATION_ERROR", "labels must be unique.")

        cohort = dict(self.required_cohort(cohort_id))
        if cohort.get("status") != "active" or is_expired(cohort.get("expiresAt"), now=now):
            raise AdminError(409, "COHORT_INACTIVE", "The cohort is inactive or expired.")
        quota = _quota(body["dailyQuota"]) if "dailyQuota" in body else _quota(cohort.get("defaultDailyQuota"))
        timestamp = _timestamp(now)
        issued: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        for index, label in enumerate(labels):
            plaintext = self._plaintext_key()
            key_id = self._identifier("student")
            record = {"keyId": key_id, "keyHash": hashlib.sha256(plaintext.encode()).hexdigest(), "label": label, "dailyQuota": quota, "createdAt": timestamp}
            issued_entry = {"keyId": key_id, "label": label, "key": plaintext}
            if identities:
                record.update(identities[index])
                issued_entry.update(identities[index])
            records.append(record)
            issued.append(issued_entry)
        self._repository.issue_keys({**cohort, "cohortId": cohort_id}, records)
        return {"cohortId": cohort_id, "keys": issued}

    def list_keys(self, query: Mapping[str, str], now: datetime) -> dict[str, Any]:
        _allowed_query(query, {"cohortId", "limit", "cursor"})
        if "cohortId" not in query:
            raise AdminError(400, "VALIDATION_ERROR", "cohortId is required.")
        cohort_id = _identifier_value(query["cohortId"], "cohortId")
        self.required_cohort(cohort_id)
        limit = _limit(query.get("limit"), default=50, maximum=200)
        cursor = _decode_key_cursor(query.get("cursor"), cohort_id=cohort_id)
        items, next_key = self._repository.query_cohort_keys(cohort_id, limit=limit, cursor=cursor)
        usage = self._repository.usage_counts([str(item["keyId"]) for item in items], now.date())
        keys = [_key_data(item, usage.get(str(item["keyId"]), 0)) for item in items]
        return {"cohortId": cohort_id, "keys": keys, "cursor": _encode_key_cursor(next_key) if next_key else None}

    def revoke_key(self, key_id: str, now: datetime) -> dict[str, Any]:
        lookup = self._repository.get_key_lookup(key_id)
        if not lookup:
            raise AdminError(404, "KEY_NOT_FOUND", "API key was not found.")
        required = {"keyHash", "cohortId"}
        if not required.issubset(lookup):
            raise AdminError(409, "CONFLICT", "API key lookup is incomplete.")
        self._repository.revoke_key({**lookup, "keyId": key_id}, revoked_at=_timestamp(now))
        return {"keyId": key_id, "status": "revoked", "revokedAt": _timestamp(now)}

    def _register_symbols(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _exact_fields(body, {"symbols"})
        raw_symbols = body["symbols"]
        if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= 50:
            raise AdminError(
                400,
                "VALIDATION_ERROR",
                "symbols must contain 1 to 50 metadata objects.",
            )
        normalized = [_symbol_metadata(value) for value in raw_symbols]
        identities = [str(item["symbol"]) for item in normalized]
        if len(set(identities)) != len(identities):
            raise AdminError(400, "VALIDATION_ERROR", "symbols must be unique.")
        self._repository.seed_symbols(normalized)
        return {
            "symbols": [
                {
                    key: item[key]
                    for key in (
                        "symbol",
                        "name",
                        "type",
                        "exchange",
                        "currency",
                        "market",
                    )
                }
                for item in normalized
            ]
        }

    def _start_backfill(
        self, body: Mapping[str, Any], now: datetime
    ) -> dict[str, Any]:
        if not set(body).issubset({"universe", "symbols", "from", "to"}):
            raise AdminError(400, "VALIDATION_ERROR", "Backfill fields are invalid.")
        if set(body).isdisjoint({"universe", "symbols"}) or (
            "universe" in body and "symbols" in body
        ):
            raise AdminError(
                400,
                "VALIDATION_ERROR",
                "Provide exactly one of universe or symbols.",
            )
        if "from" not in body or "to" not in body:
            raise AdminError(400, "VALIDATION_ERROR", "from and to are required.")
        start = _date_value(body["from"], "from")
        end = _date_value(body["to"], "to")
        if start > end:
            raise AdminError(400, "VALIDATION_ERROR", "from must not be after to.")
        if end > now.date():
            raise AdminError(400, "VALIDATION_ERROR", "to must not be in the future.")
        if start.year < 1900 or end.year > 2100:
            raise AdminError(
                400,
                "VALIDATION_ERROR",
                "Backfill dates must be between 1900 and 2100.",
            )

        params: dict[str, Any] = {
            "from": start.isoformat(),
            "to": end.isoformat(),
        }
        if "universe" in body:
            universe = body["universe"]
            if not isinstance(universe, str) or universe not in _UNIVERSES:
                raise AdminError(400, "VALIDATION_ERROR", "universe is unsupported.")
            params["universe"] = universe
            symbol_count = _UNIVERSE_SIZES[universe]
        else:
            raw_symbols = body["symbols"]
            if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= 100:
                raise AdminError(
                    400,
                    "VALIDATION_ERROR",
                    "symbols must contain 1 to 100 canonical symbols.",
                )
            try:
                symbols = [canonical_symbol(value) for value in raw_symbols]
            except (TypeError, ValueError) as exc:
                raise AdminError(
                    400, "VALIDATION_ERROR", "symbols must be canonical symbols."
                ) from exc
            if len(set(symbols)) != len(symbols):
                raise AdminError(400, "VALIDATION_ERROR", "symbols must be unique.")
            registered = {
                symbol: self._repository.get_symbol(symbol) for symbol in symbols
            }
            missing = [
                symbol
                for symbol, metadata in registered.items()
                if metadata is None or metadata.get("active") is False
            ]
            if missing:
                raise AdminError(
                    404,
                    "SYMBOL_NOT_FOUND",
                    f"Registered symbol was not found: {missing[0]}.",
                )
            params["symbols"] = symbols
            symbol_count = len(symbols)

        year_count = end.year - start.year + 1
        if symbol_count * year_count > _MAX_BACKFILL_WORK:
            raise AdminError(
                400,
                "VALIDATION_ERROR",
                f"Backfill cannot exceed {_MAX_BACKFILL_WORK} symbol-year units.",
            )

        job_id = self._identifier("job")
        timestamp = _timestamp(now)
        item = {
            "PK": f"JOB#{job_id}",
            "SK": "META",
            "jobId": job_id,
            "type": "admin_backfill",
            "state": "preparing",
            "params": params,
            "total": 0,
            "completed": 0,
            "failed": 0,
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        self._repository.create_backfill_job(item)
        if self._coordinator is None:
            raise RuntimeError("backfill coordinator is unavailable")
        try:
            self._coordinator.invoke(job_id)
        except Exception as exc:
            self._repository.record_backfill_dispatch_error(
                job_id,
                error=type(exc).__name__,
                updated_at=timestamp,
            )
            return {
                "jobId": job_id,
                "state": "preparing",
                "dispatchDeferred": True,
            }
        return {"jobId": job_id, "state": "preparing"}

    def _backfill_status(self, job_id: str) -> dict[str, Any]:
        job = self._repository.get_backfill_job(job_id)
        if not job or job.get("type") not in {"backfill", "lazy_backfill", "admin_backfill"}:
            raise AdminError(404, "JOB_NOT_FOUND", "Backfill job was not found.")
        failed_count = int(job.get("failed", 0))
        failed_work, truncated = self._repository.backfill_failures(
            job_id, limit=100
        )
        failures = [
            {
                "symbol": item.get("symbol"),
                "year": int(item["year"]),
                "error": item.get("failure", "INGEST_FAILED"),
            }
            for item in failed_work
        ]
        done = int(job.get("completed", 0))
        data = {
            "jobId": job_id,
            "state": job.get("state"),
            "done": done,
            "failed": failures,
            "failedCount": failed_count,
            "failuresTruncated": truncated or failed_count > len(failures),
            "total": int(job.get("total", 0)),
        }
        if job.get("preparationError"):
            data["preparationError"] = job["preparationError"]
        return data

    def required_cohort(self, cohort_id: str) -> Mapping[str, Any]:
        item = self._repository.get_cohort(cohort_id)
        if not item:
            raise AdminError(404, "COHORT_NOT_FOUND", "Cohort was not found.")
        return item

    def cohort_stats(self, cohort_id: str, usage_date: date) -> dict[str, Any]:
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
        client = boto3.client("dynamodb")
        table = dynamodb.Table(os.environ["CONTROL_TABLE"])
        data_table = dynamodb.Table(os.environ["DATA_TABLE"])
        coordinator = LambdaCoordinatorInvoker(
            boto3.client("lambda"), os.environ["BACKFILL_COORDINATOR_FUNCTION"]
        )
        _service = AdminService(
            DynamoAdminRepository(table, dynamodb, data_table, client=client),
            stage=os.environ.get("STAGE", "dev"),
            coordinator=coordinator,
        )
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


def _student_identity(value: object) -> tuple[str, str, str]:
    if not isinstance(value, Mapping) or set(value) != {"label", "name", "email"}:
        raise AdminError(
            400, "VALIDATION_ERROR", "Each student must include label, name, and email."
        )
    label = _label(value["label"])
    name = _name(value["name"])
    email = _student_email(value["email"])
    return label, name, email


def _student_email(value: object) -> str:
    if not isinstance(value, str):
        raise AdminError(400, "VALIDATION_ERROR", "email is invalid.")
    email = value.strip()
    if not 1 <= len(email) <= 254 or email.count("@") != 1:
        raise AdminError(400, "VALIDATION_ERROR", "email is invalid.")
    local, _, domain = email.partition("@")
    if not local or not domain:
        raise AdminError(400, "VALIDATION_ERROR", "email is invalid.")
    return email


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


def _date_value(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise AdminError(400, "VALIDATION_ERROR", f"{field} must be a YYYY-MM-DD date.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AdminError(
            400, "VALIDATION_ERROR", f"{field} must be a YYYY-MM-DD date."
        ) from exc


def _symbol_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AdminError(400, "VALIDATION_ERROR", "Each symbol must be an object.")
    required = {"symbol", "name", "type", "exchange", "currency"}
    allowed = required | {"adapterHints"}
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise AdminError(400, "VALIDATION_ERROR", "Symbol metadata fields are invalid.")
    try:
        info = parse_symbol(value["symbol"])
    except (TypeError, ValueError) as exc:
        raise AdminError(400, "VALIDATION_ERROR", "symbol is not canonical.") from exc
    if value["symbol"] != info.symbol:
        raise AdminError(400, "VALIDATION_ERROR", "symbol must be uppercase canonical form.")
    name = value["name"]
    asset_type = value["type"]
    exchange = value["exchange"]
    currency = value["currency"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
        raise AdminError(400, "VALIDATION_ERROR", "symbol name must contain 1 to 200 characters.")
    allowed_types = {"equity", "etf"} if info.market == Market.US else {info.asset_type}
    if asset_type not in allowed_types:
        raise AdminError(400, "VALIDATION_ERROR", "symbol type does not match its market.")
    if not isinstance(exchange, str) or not 1 <= len(exchange.strip()) <= 32:
        raise AdminError(400, "VALIDATION_ERROR", "exchange is invalid.")
    if info.market != Market.US and exchange != info.exchange:
        raise AdminError(400, "VALIDATION_ERROR", "exchange does not match the symbol.")
    if currency != info.currency:
        raise AdminError(400, "VALIDATION_ERROR", "currency does not match the symbol.")
    metadata: dict[str, Any] = {
        "symbol": info.symbol,
        "name": name.strip(),
        "type": asset_type,
        "exchange": exchange.strip(),
        "currency": currency,
        "market": info.market.value,
    }
    hints = value.get("adapterHints")
    if hints is not None:
        if (
            not isinstance(hints, Mapping)
            or len(hints) > 20
            or not all(
                isinstance(key, str)
                and 1 <= len(key) <= 80
                and isinstance(item, str)
                and 1 <= len(item) <= 200
                for key, item in hints.items()
            )
        ):
            raise AdminError(400, "VALIDATION_ERROR", "adapterHints must be a bounded string map.")
        metadata["adapterHints"] = dict(hints)
    return metadata


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

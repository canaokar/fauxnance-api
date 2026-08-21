"""Business logic and authorization for the instructor console."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
import re
import secrets
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from src.admin.handler import AdminError, AdminService
from src.admin.repository import RepositoryConflict
from src.console.passwords import hash_password, validate_password_strength, verify_password


_SESSION_LIFETIME = timedelta(hours=12)
_LOGIN_FAILURE_TTL_SECONDS = 900
_LOGIN_FAILURE_LIMIT = 10
_MAX_STUDENTS_PER_REQUEST = 25
# A fixed dummy hash so an unknown-email login pays the same PBKDF2 cost as a
# known one, closing the timing oracle that would otherwise reveal whether an
# account exists. Generated once at import from random material, never a
# hardcoded literal.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))
_MAX_NAME_LENGTH = 120
_LABEL_MAX_LENGTH = 40
_LABEL_RUN = re.compile(r"[^a-z0-9]+")
_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")
_STATUS_TO_CODE = {
    400: "VALIDATION_ERROR",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    429: "TOO_MANY_ATTEMPTS",
}


class ConsoleRepository(Protocol):
    def create_user(self, item: Mapping[str, Any]) -> None: ...
    def get_user(self, user_id: str) -> Mapping[str, Any] | None: ...
    def get_user_by_email(self, email: str) -> Mapping[str, Any] | None: ...
    def all_users(self) -> list[Mapping[str, Any]]: ...
    def update_user_status(self, user_id: str, *, status: str, updated_at: str) -> None: ...
    def create_session(self, item: Mapping[str, Any]) -> None: ...
    def get_session(self, token_hash: str) -> Mapping[str, Any] | None: ...
    def delete_session(self, token_hash: str) -> None: ...
    def record_login_failure(self, email: str, *, expires_at: int) -> int: ...
    def get_login_failures(self, email: str, now: datetime) -> int: ...
    def clear_login_failures(self, email: str) -> None: ...
    def assign_instructor(self, assignment: Mapping[str, Any], reverse: Mapping[str, Any]) -> None: ...
    def unassign_instructor(self, cohort_id: str, user_id: str) -> None: ...
    def instructors_for_class(self, cohort_id: str) -> list[Mapping[str, Any]]: ...
    def classes_for_user(self, user_id: str) -> list[Mapping[str, Any]]: ...
    def is_assigned(self, cohort_id: str, user_id: str) -> bool: ...


class KeyStore(Protocol):
    """Read-only access to the raw cohort key-index items the admin API keeps private.

    ``AdminService.list_keys`` redacts student identity from its response, and
    ``AdminService.revoke_key`` does not verify the key belongs to a given
    cohort. The console needs both, so it reads the same repository rows the
    admin layer already reads for those calls, without touching any of the
    admin layer's own validation, id generation, or write logic.
    """

    def query_cohort_keys(
        self, cohort_id: str, *, limit: int | None = None, cursor: Mapping[str, Any] | None = None
    ) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]: ...

    def usage_counts(self, key_ids: Sequence[str], usage_date: date) -> dict[str, int]: ...

    def get_key_lookup(self, key_id: str) -> Mapping[str, Any] | None: ...


class ConsoleError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


class ConsoleService:
    def __init__(
        self,
        repository: ConsoleRepository,
        admin_service: AdminService,
        key_store: KeyStore,
        *,
        identifier: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._admin_service = admin_service
        self._key_store = key_store
        self._identifier = identifier or (lambda: f"user_{uuid4().hex[:16]}")
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    # -- authentication --------------------------------------------------

    def login(self, body: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        if set(body) != {"email", "password"}:
            raise ConsoleError(400, "VALIDATION_ERROR", "email and password are required.")
        email = _email(body["email"])
        password = body["password"]
        if not isinstance(password, str) or not password:
            raise ConsoleError(400, "VALIDATION_ERROR", "password is required.")

        if self._repository.get_login_failures(email, now) >= _LOGIN_FAILURE_LIMIT:
            raise ConsoleError(
                429, "TOO_MANY_ATTEMPTS", "Too many failed login attempts. Try again later."
            )

        user = self._repository.get_user_by_email(email)
        if user is None:
            # No such account: still run verify_password against a dummy hash
            # so this path costs the same PBKDF2 work as a wrong-password
            # match below, instead of returning early and leaking existence
            # via response latency.
            verify_password(password, _DUMMY_PASSWORD_HASH)
            self._record_login_failure(email, now)
            raise ConsoleError(401, "INVALID_CREDENTIALS", "Incorrect email or password.")
        if (
            not verify_password(password, str(user.get("passwordHash", "")))
            or user.get("status") != "active"
        ):
            self._record_login_failure(email, now)
            raise ConsoleError(401, "INVALID_CREDENTIALS", "Incorrect email or password.")

        self._repository.clear_login_failures(email)
        token = self._token_factory()
        token_hash = _hash_token(token)
        expiry = now + _SESSION_LIFETIME
        self._repository.create_session(
            {
                "PK": f"SESSION#{token_hash}",
                "SK": "META",
                "userId": user["userId"],
                "createdAt": _timestamp(now),
                "expiresAt": int(expiry.timestamp()),
            }
        )
        return {"token": token, "expiresAt": _timestamp(expiry), "user": _user_data(user)}

    def logout(self, token: str) -> dict[str, Any]:
        self._repository.delete_session(_hash_token(token))
        return {}

    def authenticate(self, token: str | None, now: datetime) -> Mapping[str, Any]:
        if not token:
            raise ConsoleError(401, "UNAUTHENTICATED", "A bearer token is required.")
        session = self._repository.get_session(_hash_token(token))
        if session is None:
            raise ConsoleError(401, "UNAUTHENTICATED", "Session is invalid or expired.")
        expires_at = session.get("expiresAt")
        if expires_at is None or int(expires_at) <= int(now.timestamp()):
            raise ConsoleError(401, "UNAUTHENTICATED", "Session is invalid or expired.")
        user_id = session.get("userId")
        user = self._repository.get_user(str(user_id)) if user_id else None
        if user is None or user.get("status") != "active":
            raise ConsoleError(401, "UNAUTHENTICATED", "Session is invalid or expired.")
        return user

    def get_me(self, user: Mapping[str, Any]) -> dict[str, Any]:
        return {"user": _user_data(user)}

    def _record_login_failure(self, email: str, now: datetime) -> None:
        expires_at = int(now.timestamp()) + _LOGIN_FAILURE_TTL_SECONDS
        self._repository.record_login_failure(email, expires_at=expires_at)

    # -- users -------------------------------------------------------------

    def list_users(self, user: Mapping[str, Any]) -> dict[str, Any]:
        self._require_admin(user)
        users = self._repository.all_users()
        return {
            "users": [
                _user_data(item, class_count=len(self._repository.classes_for_user(str(item["userId"]))))
                for item in users
            ]
        }

    def create_user(
        self, user: Mapping[str, Any], body: Mapping[str, Any], now: datetime
    ) -> dict[str, Any]:
        self._require_admin(user)
        if set(body) != {"email", "name", "role", "password"}:
            raise ConsoleError(
                400, "VALIDATION_ERROR", "email, name, role, and password are required."
            )
        email = _email(body["email"])
        name = _name(body["name"])
        role = body["role"]
        if role not in {"admin", "instructor"}:
            raise ConsoleError(400, "VALIDATION_ERROR", "role must be admin or instructor.")
        password = body["password"]
        if not isinstance(password, str):
            raise ConsoleError(400, "VALIDATION_ERROR", "password must be a string.")
        try:
            validate_password_strength(password)
        except ValueError as exc:
            raise ConsoleError(400, "VALIDATION_ERROR", str(exc)) from exc

        user_id = self._identifier()
        timestamp = _timestamp(now)
        item = {
            "PK": f"USER#{user_id}",
            "SK": "META",
            "userId": user_id,
            "email": email,
            "name": name,
            "passwordHash": hash_password(password),
            "role": role,
            "status": "active",
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        try:
            self._repository.create_user(item)
        except RepositoryConflict as exc:
            raise ConsoleError(409, "CONFLICT", str(exc)) from exc
        return {"user": _user_data(item)}

    def update_user_status(
        self,
        user: Mapping[str, Any],
        target_user_id: str,
        body: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        self._require_admin(user)
        if set(body) != {"status"}:
            raise ConsoleError(400, "VALIDATION_ERROR", "Only status is accepted.")
        status = body["status"]
        if status not in {"active", "disabled"}:
            raise ConsoleError(400, "VALIDATION_ERROR", "status must be active or disabled.")
        if status == "disabled" and target_user_id == str(user.get("userId")):
            raise ConsoleError(409, "CONFLICT", "You cannot disable your own account.")
        target = self._repository.get_user(target_user_id)
        if target is None:
            raise ConsoleError(404, "NOT_FOUND", "User was not found.")

        timestamp = _timestamp(now)
        try:
            self._repository.update_user_status(target_user_id, status=status, updated_at=timestamp)
        except RepositoryConflict as exc:
            raise ConsoleError(409, "CONFLICT", str(exc)) from exc
        updated = {**target, "status": status, "updatedAt": timestamp}
        return {"user": _user_data(updated)}

    # -- classes -------------------------------------------------------------

    def list_classes(self, user: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        if user.get("role") == "admin":
            cohorts = self._all_cohorts(now)
        else:
            assignments = self._repository.classes_for_user(str(user["userId"]))
            cohorts = []
            for assignment in assignments:
                cohort_id = str(assignment["cohortId"])
                try:
                    cohorts.append(self._admin_service.get_cohort_detail(cohort_id, now))
                except AdminError as exc:
                    if exc.code == "COHORT_NOT_FOUND":
                        continue
                    raise self._translate_admin_error(exc) from exc
        classes = [
            _klass_data(item, self._repository.instructors_for_class(str(item["cohortId"])))
            for item in cohorts
        ]
        return {"classes": classes}

    def create_class(
        self, user: Mapping[str, Any], body: Mapping[str, Any], now: datetime
    ) -> dict[str, Any]:
        self._require_admin(user)
        if set(body) != {"name", "dailyQuota", "expiresAt"}:
            raise ConsoleError(
                400, "VALIDATION_ERROR", "name, dailyQuota, and expiresAt are required."
            )
        admin_body = {
            "name": body["name"],
            "defaultDailyQuota": body["dailyQuota"],
            "expiresAt": body["expiresAt"],
        }
        try:
            cohort = self._admin_service.create_cohort(admin_body, now)
        except AdminError as exc:
            raise self._translate_admin_error(exc) from exc
        except RepositoryConflict as exc:
            raise ConsoleError(409, "CONFLICT", str(exc)) from exc
        return {"class": _klass_data(cohort, [])}

    def get_class(self, user: Mapping[str, Any], cohort_id: str, now: datetime) -> dict[str, Any]:
        self._require_class_access(user, cohort_id)
        try:
            detail = self._admin_service.get_cohort_detail(cohort_id, now)
        except AdminError as exc:
            raise self._translate_admin_error(exc) from exc
        instructor_items = self._repository.instructors_for_class(cohort_id)
        students = self._students_for_class(cohort_id, now)
        klass = _klass_data({**detail, "cohortId": cohort_id}, instructor_items)
        return {"class": klass, "students": students, "instructors": klass["instructors"]}

    def add_instructor(
        self,
        user: Mapping[str, Any],
        cohort_id: str,
        body: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        self._require_admin(user)
        if set(body) != {"userId"}:
            raise ConsoleError(400, "VALIDATION_ERROR", "Only userId is accepted.")
        target_id = body["userId"]
        if not isinstance(target_id, str) or not target_id:
            raise ConsoleError(400, "VALIDATION_ERROR", "userId is invalid.")
        target = self._repository.get_user(target_id)
        if target is None:
            raise ConsoleError(404, "NOT_FOUND", "User was not found.")
        if target.get("role") != "instructor":
            raise ConsoleError(
                400, "VALIDATION_ERROR", "Only instructors may be assigned to a class."
            )
        try:
            self._admin_service.required_cohort(cohort_id)
        except AdminError as exc:
            raise self._translate_admin_error(exc) from exc

        timestamp = _timestamp(now)
        assignment = {
            "PK": f"COHORT#{cohort_id}",
            "SK": f"INSTRUCTOR#{target_id}",
            "userId": target_id,
            "cohortId": cohort_id,
            "name": target.get("name"),
            "email": target.get("email"),
            "assignedAt": timestamp,
        }
        reverse = {
            "PK": f"USER#{target_id}",
            "SK": f"COHORT#{cohort_id}",
            "cohortId": cohort_id,
            "assignedAt": timestamp,
        }
        try:
            self._repository.assign_instructor(assignment, reverse)
        except RepositoryConflict as exc:
            raise ConsoleError(409, "CONFLICT", str(exc)) from exc
        return {"instructors": _instructor_data(self._repository.instructors_for_class(cohort_id))}

    def remove_instructor(
        self,
        user: Mapping[str, Any],
        cohort_id: str,
        target_user_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        self._require_admin(user)
        self._repository.unassign_instructor(cohort_id, target_user_id)
        return {"instructors": _instructor_data(self._repository.instructors_for_class(cohort_id))}

    def issue_students(
        self,
        user: Mapping[str, Any],
        cohort_id: str,
        body: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        self._require_class_access(user, cohort_id)
        if set(body) != {"students"}:
            raise ConsoleError(400, "VALIDATION_ERROR", "Only students is accepted.")
        raw_students = body["students"]
        if not isinstance(raw_students, list) or not raw_students:
            raise ConsoleError(400, "VALIDATION_ERROR", "students must be a non-empty list.")
        if len(raw_students) > _MAX_STUDENTS_PER_REQUEST:
            raise ConsoleError(
                400,
                "VALIDATION_ERROR",
                f"students supports at most {_MAX_STUDENTS_PER_REQUEST} entries per call; "
                "split the list into multiple requests.",
            )

        names: list[str] = []
        entries: list[dict[str, Any]] = []
        for raw in raw_students:
            if not isinstance(raw, Mapping) or set(raw) != {"name", "email"}:
                raise ConsoleError(
                    400, "VALIDATION_ERROR", "Each student must include name and email."
                )
            name = raw["name"]
            if not isinstance(name, str):
                raise ConsoleError(400, "VALIDATION_ERROR", "Each student name must be a string.")
            names.append(name)
            entries.append({"name": name, "email": raw["email"]})

        labels = _labels_for_students(names)
        students_payload = [
            {"label": label, "name": entry["name"], "email": entry["email"]}
            for label, entry in zip(labels, entries)
        ]
        try:
            result = self._admin_service.issue_keys(
                {"cohortId": cohort_id, "students": students_payload}, now
            )
        except AdminError as exc:
            raise self._translate_admin_error(exc) from exc
        except RepositoryConflict as exc:
            raise ConsoleError(409, "CONFLICT", str(exc)) from exc
        return {"issued": result["keys"]}

    def revoke_student(
        self,
        user: Mapping[str, Any],
        cohort_id: str,
        key_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        self._require_class_access(user, cohort_id)
        lookup = self._key_store.get_key_lookup(key_id)
        if lookup is None or str(lookup.get("cohortId")) != cohort_id:
            raise ConsoleError(404, "NOT_FOUND", "Student key was not found in this class.")
        try:
            return dict(self._admin_service.revoke_key(key_id, now))
        except AdminError as exc:
            raise self._translate_admin_error(exc) from exc

    # -- authorization -------------------------------------------------------

    def _require_admin(self, user: Mapping[str, Any]) -> None:
        if user.get("role") != "admin":
            raise ConsoleError(403, "FORBIDDEN", "An admin account is required.")

    def _require_class_access(self, user: Mapping[str, Any], cohort_id: str) -> None:
        if user.get("role") == "admin":
            return
        if not self._repository.is_assigned(cohort_id, str(user.get("userId"))):
            raise ConsoleError(403, "FORBIDDEN", "You are not assigned to this class.")

    def _translate_admin_error(self, exc: AdminError) -> ConsoleError:
        code = _STATUS_TO_CODE.get(exc.status)
        if code is None:
            return ConsoleError(500, "INTERNAL_ERROR", "An unexpected error occurred.")
        return ConsoleError(exc.status, code, exc.message)

    # -- helpers -------------------------------------------------------------

    def _all_cohorts(self, now: datetime) -> list[Mapping[str, Any]]:
        cohorts: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while True:
            query = {"cursor": cursor} if cursor else {}
            page = self._admin_service.list_cohorts(query, now)
            cohorts.extend(page["cohorts"])
            cursor = page.get("cursor")
            if not cursor:
                break
        return cohorts

    def _students_for_class(self, cohort_id: str, now: datetime) -> list[dict[str, Any]]:
        items: list[Mapping[str, Any]] = []
        cursor: Mapping[str, Any] | None = None
        while True:
            page, cursor = self._key_store.query_cohort_keys(cohort_id, cursor=cursor)
            items.extend(page)
            if not cursor:
                break
        usage = self._key_store.usage_counts([str(item["keyId"]) for item in items], now.date())
        students = []
        for item in items:
            key_id = str(item.get("keyId"))
            student = {
                "keyId": item.get("keyId"),
                "label": item.get("label"),
                "studentName": item.get("studentName"),
                "studentEmail": item.get("studentEmail"),
                "status": item.get("status"),
                "dailyQuota": item.get("dailyQuota"),
                "usedToday": usage.get(key_id, 0),
                "createdAt": item.get("createdAt"),
            }
            if item.get("revokedAt") is not None:
                student["revokedAt"] = item["revokedAt"]
            students.append(student)
        return students


def _labels_for_students(names: Sequence[str]) -> list[str]:
    # AdminService.issue_keys compares labels case-folded and rejects the
    # whole batch on any collision, so every candidate must be checked
    # against every label already allocated in this request -- not just
    # other occurrences of the same base -- before it is accepted.
    used: set[str] = set()
    suffixes: dict[str, int] = {}
    labels = []
    for name in names:
        base = _label_base(name)
        candidate = base
        while candidate.casefold() in used:
            suffixes[base] = suffixes.get(base, 1) + 1
            candidate = f"{base}-{suffixes[base]}"
        used.add(candidate.casefold())
        labels.append(candidate)
    return labels


def _label_base(name: str) -> str:
    collapsed = _LABEL_RUN.sub("-", name.lower()).strip("-")
    truncated = collapsed[:_LABEL_MAX_LENGTH].strip("-")
    if truncated:
        return truncated
    if not _ASCII_ALNUM.search(name):
        raise ConsoleError(
            400,
            "VALIDATION_ERROR",
            f"Student name {name!r} has no ASCII letters or digits; "
            "supply an ASCII handle to generate a label from.",
        )
    raise ConsoleError(
        400, "VALIDATION_ERROR", f"Student name {name!r} does not produce a usable label."
    )


def _instructor_data(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"userId": item.get("userId"), "name": item.get("name"), "email": item.get("email")}
        for item in items
    ]


def _klass_data(cohort: Mapping[str, Any], instructors: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    key_counts = cohort.get("keyCounts") or {}
    return {
        "cohortId": cohort.get("cohortId"),
        "name": cohort.get("name"),
        "dailyQuota": cohort.get("defaultDailyQuota"),
        "expiresAt": cohort.get("expiresAt"),
        "status": cohort.get("status"),
        "studentCount": key_counts.get("active", 0),
        "instructors": _instructor_data(instructors),
    }


def _user_data(item: Mapping[str, Any], *, class_count: int | None = None) -> dict[str, Any]:
    data = {
        "userId": item.get("userId"),
        "email": item.get("email"),
        "name": item.get("name"),
        "role": item.get("role"),
        "status": item.get("status"),
        "createdAt": item.get("createdAt"),
    }
    if class_count is not None:
        data["classCount"] = class_count
    return data


def _email(value: object) -> str:
    if not isinstance(value, str):
        raise ConsoleError(400, "VALIDATION_ERROR", "email is invalid.")
    email = value.strip().lower()
    if not 1 <= len(email) <= 254 or email.count("@") != 1:
        raise ConsoleError(400, "VALIDATION_ERROR", "email is invalid.")
    local, _, domain = email.partition("@")
    if not local or not domain:
        raise ConsoleError(400, "VALIDATION_ERROR", "email is invalid.")
    return email


def _name(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= _MAX_NAME_LENGTH:
        raise ConsoleError(
            400, "VALIDATION_ERROR", f"name must contain 1 to {_MAX_NAME_LENGTH} characters."
        )
    return value.strip()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)

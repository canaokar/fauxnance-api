"""Authentication primitives shared by the authorizer and API handlers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
import hashlib
import re
from typing import Any, Mapping


_API_KEY_PATTERN = re.compile(r"^fnx_[a-z][a-z0-9-]*_[A-Za-z0-9]{32}$")


def is_api_key_shape(value: str) -> bool:
    """Return whether ``value`` has the documented Fauxnance key shape."""

    return bool(_API_KEY_PATTERN.fullmatch(value))


def hash_api_key(value: str) -> str:
    """Return the lowercase SHA-256 digest used in a control-table key."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_expired(value: object, *, now: datetime) -> bool:
    """Evaluate an ``expiresAt`` value using UTC.

    Date-only values are valid for the full named date. Timestamps and epoch
    values expire at their exact instant. Missing values mean no expiry.
    """

    if value in (None, ""):
        return False

    current = _as_utc(now)
    if isinstance(value, Decimal):
        value = int(value)
    if isinstance(value, (int, float)):
        return current >= datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str):
        raise ValueError("expiresAt must be an ISO date, timestamp, or epoch")

    raw = value.strip()
    if not raw:
        return False
    try:
        expiry_date = date.fromisoformat(raw)
    except ValueError:
        try:
            expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expiresAt must be an ISO date or timestamp") from exc
        return current >= _as_utc(expiry)

    expires_after = datetime.combine(expiry_date, time.max, tzinfo=UTC)
    return current > expires_after


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Non-secret identity passed from the authorizer to an API handler."""

    key_id: str
    key_type: str
    cohort_id: str | None = None
    daily_quota: int | None = None

    def to_gateway_context(self) -> dict[str, str | int]:
        context: dict[str, str | int] = {
            "keyId": self.key_id,
            "keyType": self.key_type,
        }
        if self.cohort_id is not None:
            context["cohortId"] = self.cohort_id
        if self.daily_quota is not None:
            context["dailyQuota"] = self.daily_quota
        return context


def parse_authorizer_context(event: Mapping[str, Any]) -> AuthContext:
    """Read an HTTP API payload-v2 Lambda authorizer context."""

    try:
        raw = event["requestContext"]["authorizer"]["lambda"]
        key_id = str(raw["keyId"])
        key_type = str(raw["keyType"])
    except (KeyError, TypeError) as exc:
        raise ValueError("request has no valid authorizer context") from exc

    cohort_id = raw.get("cohortId")
    daily_quota = raw.get("dailyQuota")
    return AuthContext(
        key_id=key_id,
        key_type=key_type,
        cohort_id=str(cohort_id) if cohort_id is not None else None,
        daily_quota=int(daily_quota) if daily_quota is not None else None,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)

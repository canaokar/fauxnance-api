"""DynamoDB-backed fixed-window daily quota accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import math
from typing import Any, Callable, Protocol


class DynamoTable(Protocol):
    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Usage:
    used_today: int
    daily_quota: int
    resets_at: datetime


class QuotaExceeded(Exception):
    """Raised when the caller has exhausted its current UTC-day allowance."""

    def __init__(self, daily_quota: int, resets_at: datetime, retry_after: int):
        self.daily_quota = daily_quota
        self.resets_at = resets_at
        self.retry_after = retry_after
        super().__init__(
            f"Daily quota of {daily_quota} requests exhausted. "
            "Resets at 00:00 UTC."
        )


class QuotaService:
    """Atomically consume one request from a student's daily quota."""

    def __init__(
        self,
        table: DynamoTable,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._table = table
        self._clock = clock or (lambda: datetime.now(UTC))

    def consume(
        self,
        key_id: str,
        daily_quota: int,
        *,
        at: datetime | None = None,
    ) -> Usage:
        if not key_id:
            raise ValueError("key_id is required")
        if daily_quota < 1:
            raise ValueError("daily_quota must be positive")

        now = _as_utc(at or self._clock())
        usage_date = now.date()
        resets_at = datetime.combine(usage_date + timedelta(days=1), time.min, UTC)
        # DynamoDB TTL is asynchronous; this is cleanup metadata, not quota logic.
        expires_at = int(
            datetime.combine(usage_date + timedelta(days=35), time.min, UTC).timestamp()
        )

        try:
            result = self._table.update_item(
                Key={"PK": f"KEYID#{key_id}", "SK": f"USAGE#{usage_date.isoformat()}"},
                UpdateExpression=(
                    "SET #count = if_not_exists(#count, :zero) + :one, "
                    "expiresAt = if_not_exists(expiresAt, :expires_at)"
                ),
                ConditionExpression="attribute_not_exists(#count) OR #count < :quota",
                ExpressionAttributeNames={"#count": "count"},
                ExpressionAttributeValues={
                    ":zero": 0,
                    ":one": 1,
                    ":quota": daily_quota,
                    ":expires_at": expires_at,
                },
                ReturnValues="UPDATED_NEW",
            )
        except Exception as exc:
            if _aws_error_code(exc) != "ConditionalCheckFailedException":
                raise
            retry_after = max(1, math.ceil((resets_at - now).total_seconds()))
            raise QuotaExceeded(daily_quota, resets_at, retry_after) from None

        return Usage(
            used_today=int(result["Attributes"]["count"]),
            daily_quota=daily_quota,
            resets_at=resets_at,
        )


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    return error.get("Code") if isinstance(error, dict) else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)

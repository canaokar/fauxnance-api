"""Small DynamoDB-backed call budget and circuit breaker for data sources."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any, Callable


ALPHA_VANTAGE_DAILY_LIMIT = 25


class DynamoDbSourceGuard:
    """Reserve a daily call budget and pause a repeatedly failing source."""

    def __init__(
        self,
        table: Any,
        source: str,
        *,
        daily_limit: int | None = None,
        failure_threshold: int = 3,
        cooldown: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if daily_limit is not None and daily_limit < 1:
            raise ValueError("daily_limit must be positive")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        self._table = table
        self._source = source
        self._daily_limit = daily_limit
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._clock = clock or (lambda: datetime.now(UTC))

    def try_acquire(self) -> bool:
        now = _as_utc(self._clock())
        if self._circuit_is_open(now):
            return False
        if self._daily_limit is None:
            return True

        day = now.date().isoformat()
        expires_at = int(
            datetime.combine(
                now.date() + timedelta(days=2), time.min, tzinfo=UTC
            ).timestamp()
        )
        try:
            self._table.update_item(
                Key={"PK": f"SRC#{self._source}", "SK": f"BUDGET#{day}"},
                UpdateExpression="SET expiresAt = :expires ADD calls :one",
                ConditionExpression=(
                    "attribute_not_exists(calls) OR calls < :limit"
                ),
                ExpressionAttributeValues={
                    ":expires": expires_at,
                    ":one": 1,
                    ":limit": self._daily_limit,
                },
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def record_success(self) -> None:
        self._table.update_item(
            Key={"PK": f"SRC#{self._source}", "SK": "STATE"},
            UpdateExpression=(
                "SET #state = :closed, failureCount = :zero, updatedAt = :now "
                "REMOVE openedUntil"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":closed": "closed",
                ":zero": 0,
                ":now": _timestamp(_as_utc(self._clock())),
            },
        )

    def record_failure(self) -> None:
        now = _as_utc(self._clock())
        response = self._table.update_item(
            Key={"PK": f"SRC#{self._source}", "SK": "STATE"},
            UpdateExpression="SET updatedAt = :now ADD failureCount :one",
            ExpressionAttributeValues={
                ":now": _timestamp(now),
                ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
        failures = int(response.get("Attributes", {}).get("failureCount", 1))
        if failures < self._failure_threshold:
            return
        self._table.update_item(
            Key={"PK": f"SRC#{self._source}", "SK": "STATE"},
            UpdateExpression=(
                "SET #state = :open, openedUntil = :until, updatedAt = :now"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":open": "open",
                ":until": _timestamp(now + self._cooldown),
                ":now": _timestamp(now),
            },
        )

    def _circuit_is_open(self, now: datetime) -> bool:
        item = self._table.get_item(
            Key={"PK": f"SRC#{self._source}", "SK": "STATE"},
            ConsistentRead=True,
        ).get("Item")
        if not item or item.get("state") != "open":
            return False
        opened_until = item.get("openedUntil")
        if not isinstance(opened_until, str):
            return True
        try:
            return datetime.fromisoformat(opened_until.replace("Z", "+00:00")) > now
        except ValueError:
            return True


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"

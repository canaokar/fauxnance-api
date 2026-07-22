"""On-demand symbol registration and idempotent lazy backfill."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any, Mapping, Protocol

from src.shared.market_data import SymbolMetadata
from src.shared.symbols import parse_symbol


class DiscoverySource(Protocol):
    name: str

    def discover(self, symbol: str) -> SymbolMetadata | None: ...


class SourceGuard(Protocol):
    def try_acquire(self) -> bool: ...

    def record_success(self) -> None: ...

    def record_failure(self) -> None: ...


class DiscoveryRepository(Protocol):
    def get_symbol(self, symbol: str) -> Mapping[str, Any] | None: ...

    def register_discovered(
        self,
        metadata: SymbolMetadata,
        *,
        market: str,
        discovered_at: datetime,
        job_id: str,
    ) -> Mapping[str, Any]: ...


class DiscoveryUnavailable(Exception):
    """Raised when discovery or lazy-backfill coordination is unavailable."""


class LazyBackfill:
    def __init__(self, control_table: Any, sqs: Any, queue_url: str) -> None:
        self._control = control_table
        self._sqs = sqs
        self._queue_url = queue_url

    @staticmethod
    def job_id(symbol: str, to_year: int) -> str:
        digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:16]
        return f"lazy_{digest}_{to_year}"

    def ensure_enqueued(
        self, symbol: str, market: str, *, now: datetime
    ) -> str:
        now = _as_utc(now)
        to_year = now.year
        from_year = to_year - 10
        job_id = self.job_id(symbol, to_year)
        timestamp = _timestamp(now)
        key = {"PK": f"JOB#{job_id}", "SK": "META"}
        existing = self._control.get_item(Key=key, ConsistentRead=True).get("Item")
        if existing:
            state = existing.get("state")
            if state in {"running", "completed"}:
                return job_id
            if state == "enqueueing":
                lease = existing.get("enqueueLeaseUntil")
                if isinstance(lease, str):
                    try:
                        if datetime.fromisoformat(lease.replace("Z", "+00:00")) > now:
                            return job_id
                    except ValueError:
                        pass
        self._put_if_absent(
            {
                **key,
                "type": "lazy_backfill",
                "symbol": symbol,
                "market": market,
                "state": "prepared",
                "params": {"fromYear": from_year, "toYear": to_year},
                "total": to_year - from_year + 1,
                "completed": 0,
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }
        )
        for year in range(from_year, to_year + 1):
            self._put_if_absent(
                {
                    "PK": key["PK"],
                    "SK": f"WORK#{symbol}#YEAR#{year}",
                    "symbol": symbol,
                    "market": market,
                    "year": year,
                    "state": "pending",
                    "createdAt": timestamp,
                }
            )

        lease_until = now + timedelta(minutes=2)
        try:
            self._control.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #state = :enqueueing, enqueueLeaseUntil = :lease, "
                    "updatedAt = :now"
                ),
                ConditionExpression=(
                    "#state = :prepared OR "
                    "(#state = :enqueueing AND enqueueLeaseUntil < :now)"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":prepared": "prepared",
                    ":enqueueing": "enqueueing",
                    ":lease": _timestamp(lease_until),
                    ":now": timestamp,
                },
            )
        except Exception as exc:
            if _is_conditional_failure(exc):
                return job_id
            raise

        try:
            messages = [
                json.dumps(
                    {
                        "v": 2,
                        "kind": "backfill_year",
                        "jobId": job_id,
                        "market": market,
                        "symbol": symbol,
                        "year": year,
                    },
                    separators=(",", ":"),
                )
                for year in range(from_year, to_year + 1)
            ]
            for offset in range(0, len(messages), 10):
                entries = [
                    {"Id": f"work-{offset + index}", "MessageBody": body}
                    for index, body in enumerate(messages[offset : offset + 10])
                ]
                response = self._sqs.send_message_batch(
                    QueueUrl=self._queue_url, Entries=entries
                )
                if response.get("Failed"):
                    raise DiscoveryUnavailable("SQS rejected lazy backfill work")
            self._control.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #state = :running, updatedAt = :now REMOVE enqueueLeaseUntil"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":running": "running", ":now": timestamp},
            )
        except Exception:
            self._control.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #state = :prepared, updatedAt = :now REMOVE enqueueLeaseUntil"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":prepared": "prepared", ":now": timestamp},
            )
            raise
        return job_id

    def _put_if_absent(self, item: Mapping[str, Any]) -> bool:
        try:
            self._control.put_item(
                Item=dict(item), ConditionExpression="attribute_not_exists(PK)"
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise


class DiscoveryService:
    def __init__(
        self,
        repository: DiscoveryRepository,
        source: DiscoverySource,
        guard: SourceGuard,
        lazy_backfill: LazyBackfill,
    ) -> None:
        self._repository = repository
        self._source = source
        self._guard = guard
        self._lazy = lazy_backfill

    def ensure_registered(
        self, symbol: str, *, now: datetime
    ) -> Mapping[str, Any] | None:
        now = _as_utc(now)
        existing = self._repository.get_symbol(symbol)
        if existing is not None:
            if existing.get("active") is not False and existing.get("discovered") is True:
                coverage = existing.get("coverage") or {}
                if not coverage.get("eodFrom"):
                    self._lazy.ensure_enqueued(
                        symbol, str(existing.get("market") or parse_symbol(symbol).market.value), now=now
                    )
            return existing

        if not self._guard.try_acquire():
            raise DiscoveryUnavailable("Yahoo discovery is temporarily unavailable")
        try:
            metadata = self._source.discover(symbol)
        except Exception as exc:
            self._guard.record_failure()
            raise DiscoveryUnavailable("Yahoo discovery failed") from exc
        self._guard.record_success()
        if metadata is None:
            return None

        market = parse_symbol(symbol).market.value
        job_id = self._lazy.job_id(symbol, now.year)
        item = self._repository.register_discovered(
            metadata,
            market=market,
            discovered_at=now,
            job_id=job_id,
        )
        self._lazy.ensure_enqueued(symbol, market, now=now)
        return item


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") in {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    }

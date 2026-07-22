"""Asynchronous expansion of administrative backfill jobs."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from src.ingest.repository import DynamoDBIngestRepository
from src.ingest.universe import load_universe
from src.shared.symbols import parse_symbol


_LOGGER = logging.getLogger(__name__)
_UNIVERSE_FILES = {
    "us-phase2-v1": "us-v1.json",
    "india-phase3-v1": "india-v1.json",
    "fx-phase3-v1": "fx-v1.json",
    "crypto-phase3-v1": "crypto-v1.json",
}
_MAX_BACKFILL_WORK = 8_000
_LEASE_DURATION = timedelta(minutes=16)


class CoordinatorRepository(Protocol):
    def get_job(self, job_id: str) -> Mapping[str, Any] | None: ...
    def claim_job(self, job_id: str, *, owner: str, updated_at: str, lease_until: str) -> bool: ...
    def get_symbol(self, symbol: str) -> Mapping[str, Any] | None: ...
    def seed_symbol(self, metadata: Mapping[str, Any], *, market: str) -> None: ...
    def put_work(self, item: Mapping[str, Any]) -> None: ...
    def set_running(self, job_id: str, *, owner: str, total: int, updated_at: str) -> None: ...
    def enqueue(self, messages: Sequence[str]) -> None: ...
    def finish_enqueue(self, job_id: str, *, owner: str, updated_at: str) -> None: ...
    def fail_job(self, job_id: str, *, owner: str, error: str, updated_at: str) -> None: ...
    def release_job(self, job_id: str, *, owner: str, error: str, updated_at: str) -> None: ...
    def recoverable_job_ids(self, *, now: str, limit: int) -> list[str]: ...


class BackfillCoordinator:
    def __init__(
        self,
        repository: CoordinatorRepository,
        *,
        universe_root: Path,
        clock: Callable[[], datetime] | None = None,
        owner_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repository = repository
        self._universe_root = universe_root
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_factory = owner_factory or (lambda: uuid4().hex)

    def process(self, job_id: str) -> int:
        now = _as_utc(self._clock())
        timestamp = _timestamp(now)
        owner = self._owner_factory()
        job = self._repository.get_job(job_id)
        if not job or job.get("type") != "admin_backfill":
            raise ValueError("administrative backfill job was not found")
        if job.get("state") in {"completed", "failed"} or (
            job.get("state") == "running" and job.get("enqueueComplete") is True
        ):
            return int(job.get("total", 0))
        if not self._repository.claim_job(
            job_id,
            owner=owner,
            updated_at=timestamp,
            lease_until=_timestamp(now + _LEASE_DURATION),
        ):
            return int(job.get("total", 0))
        try:
            params = job.get("params")
            if not isinstance(params, Mapping):
                raise ValueError("backfill job parameters are invalid")
            start = date.fromisoformat(str(params["from"]))
            end = date.fromisoformat(str(params["to"]))
            symbols = self._symbols(params)
            years = range(start.year, end.year + 1)
            total = len(symbols) * len(years)
            if total > _MAX_BACKFILL_WORK:
                raise ValueError("backfill exceeds the symbol-year work limit")
            messages: list[str] = []
            for metadata in symbols:
                symbol = str(metadata["symbol"])
                market = str(metadata["market"])
                self._repository.seed_symbol(metadata, market=market)
                for year in years:
                    work_start = max(start, date(year, 1, 1))
                    work_end = min(end, date(year, 12, 31))
                    self._repository.put_work(
                        {
                            "PK": f"JOB#{job_id}",
                            "SK": f"WORK#{symbol}#YEAR#{year}",
                            "symbol": symbol,
                            "market": market,
                            "year": year,
                            "from": work_start.isoformat(),
                            "to": work_end.isoformat(),
                            "state": "pending",
                            "createdAt": timestamp,
                        }
                    )
                    messages.append(
                        json.dumps(
                            {
                                "v": 3,
                                "kind": "backfill_year",
                                "jobId": job_id,
                                "market": market,
                                "symbol": symbol,
                                "year": year,
                                "from": work_start.isoformat(),
                                "to": work_end.isoformat(),
                            },
                            separators=(",", ":"),
                        )
                    )
            self._repository.set_running(
                job_id, owner=owner, total=total, updated_at=timestamp
            )
            self._repository.enqueue(messages)
            self._repository.finish_enqueue(
                job_id, owner=owner, updated_at=timestamp
            )
            return total
        except ValueError as exc:
            self._repository.fail_job(
                job_id,
                owner=owner,
                error=type(exc).__name__,
                updated_at=timestamp,
            )
            raise
        except Exception as exc:
            self._repository.release_job(
                job_id,
                owner=owner,
                error=type(exc).__name__,
                updated_at=timestamp,
            )
            raise

    def sweep(self, *, limit: int = 1) -> list[str]:
        now = _timestamp(_as_utc(self._clock()))
        recovered: list[str] = []
        for job_id in self._repository.recoverable_job_ids(now=now, limit=limit):
            try:
                self.process(job_id)
                recovered.append(job_id)
            except Exception:
                _LOGGER.exception("Backfill sweep failed: jobId=%s", job_id)
        return recovered

    def _symbols(self, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        universe_id = params.get("universe")
        if isinstance(universe_id, str):
            filename = _UNIVERSE_FILES.get(universe_id)
            if filename is None:
                raise ValueError("backfill universe is unsupported")
            universe = load_universe(self._universe_root / filename)
            if universe.universe_id != universe_id:
                raise ValueError("backfill universe identity does not match its file")
            return [
                {**metadata, "market": universe.market}
                for metadata in universe.symbols
            ]

        raw_symbols = params.get("symbols")
        if not isinstance(raw_symbols, list) or not raw_symbols:
            raise ValueError("custom backfill symbols are invalid")
        values: list[Mapping[str, Any]] = []
        for symbol in raw_symbols:
            metadata = self._repository.get_symbol(str(symbol))
            if not metadata or metadata.get("active") is False:
                raise ValueError("custom backfill symbol is no longer registered")
            info = parse_symbol(str(symbol))
            values.append(
                {
                    "symbol": info.symbol,
                    "name": metadata.get("name") or info.symbol,
                    "type": metadata.get("type") or info.asset_type,
                    "exchange": metadata.get("exchange") or info.exchange,
                    "currency": metadata.get("currency") or info.currency,
                    "adapterHints": dict(metadata.get("adapterHints") or {}),
                    "market": info.market.value,
                }
            )
        return values


class DynamoCoordinatorRepository:
    def __init__(
        self,
        data_table: Any,
        control_table: Any,
        sqs: Any,
        queue_url: str,
    ) -> None:
        self._data = data_table
        self._control = control_table
        self._sqs = sqs
        self._queue_url = queue_url
        self._ingest = DynamoDBIngestRepository(data_table, control_table)

    def get_job(self, job_id: str) -> Mapping[str, Any] | None:
        return self._control.get_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")

    def claim_job(
        self,
        job_id: str,
        *,
        owner: str,
        updated_at: str,
        lease_until: str,
    ) -> bool:
        try:
            self._control.update_item(
                Key={"PK": f"JOB#{job_id}", "SK": "META"},
                UpdateExpression=(
                    "SET #state = :expanding, updatedAt = :now, "
                    "coordinatorLeaseUntil = :lease, coordinatorOwner = :owner"
                ),
                ConditionExpression=(
                    "#state = :preparing OR "
                    "(#state = :expanding AND "
                    "(attribute_not_exists(coordinatorLeaseUntil) OR "
                    "coordinatorLeaseUntil < :now)) OR "
                    "(#state = :running AND "
                    "(attribute_not_exists(enqueueComplete) OR "
                    "enqueueComplete = :false) "
                    "AND (attribute_not_exists(coordinatorLeaseUntil) OR "
                    "coordinatorLeaseUntil < :now))"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":preparing": "preparing",
                    ":expanding": "expanding",
                    ":running": "running",
                    ":false": False,
                    ":now": updated_at,
                    ":lease": lease_until,
                    ":owner": owner,
                },
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def get_symbol(self, symbol: str) -> Mapping[str, Any] | None:
        return self._data.get_item(
            Key={"PK": f"SYM#{symbol}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")

    def seed_symbol(self, metadata: Mapping[str, Any], *, market: str) -> None:
        self._ingest.seed_symbol(metadata, market=market)

    def put_work(self, item: Mapping[str, Any]) -> None:
        try:
            self._control.put_item(
                Item=dict(item), ConditionExpression="attribute_not_exists(PK)"
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def set_running(
        self, job_id: str, *, owner: str, total: int, updated_at: str
    ) -> None:
        self._control.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression=(
                "SET #state = :running, #total = :total, updatedAt = :now, "
                "#failed = if_not_exists(#failed, :zero), "
                "#completed = if_not_exists(#completed, :zero), "
                "enqueueComplete = :false"
            ),
            ConditionExpression="#state = :expanding AND coordinatorOwner = :owner",
            ExpressionAttributeNames={
                "#state": "state",
                "#total": "total",
                "#failed": "failed",
                "#completed": "completed",
            },
            ExpressionAttributeValues={
                ":expanding": "expanding",
                ":running": "running",
                ":total": total,
                ":zero": 0,
                ":false": False,
                ":now": updated_at,
                ":owner": owner,
            },
        )

    def enqueue(self, messages: Sequence[str]) -> None:
        for offset in range(0, len(messages), 10):
            entries = [
                {"Id": f"work-{offset + index}", "MessageBody": message}
                for index, message in enumerate(messages[offset : offset + 10])
            ]
            response = self._sqs.send_message_batch(
                QueueUrl=self._queue_url, Entries=entries
            )
            if response.get("Failed"):
                raise RuntimeError("SQS rejected administrative backfill work")

    def finish_enqueue(
        self, job_id: str, *, owner: str, updated_at: str
    ) -> None:
        self._control.update_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            UpdateExpression=(
                "SET enqueueComplete = :true, updatedAt = :now "
                "REMOVE coordinatorLeaseUntil, coordinatorOwner"
            ),
            ConditionExpression=(
                "attribute_exists(PK) AND coordinatorOwner = :owner"
            ),
            ExpressionAttributeValues={
                ":true": True,
                ":now": updated_at,
                ":owner": owner,
            },
        )
        self._remove_recovery_projection(job_id)

    def fail_job(
        self, job_id: str, *, owner: str, error: str, updated_at: str
    ) -> None:
        try:
            self._control.update_item(
                Key={"PK": f"JOB#{job_id}", "SK": "META"},
                UpdateExpression=(
                    "SET #state = :failed, preparationError = :error, "
                    "updatedAt = :now REMOVE coordinatorLeaseUntil, "
                    "coordinatorOwner"
                ),
                ConditionExpression=(
                    "coordinatorOwner = :owner AND #state <> :completed "
                    "AND #state <> :failed"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":completed": "completed",
                    ":failed": "failed",
                    ":error": error,
                    ":now": updated_at,
                    ":owner": owner,
                },
            )
            self._remove_recovery_projection(job_id)
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def release_job(
        self, job_id: str, *, owner: str, error: str, updated_at: str
    ) -> None:
        """Release an incomplete expansion so asynchronous Lambda retry can replay it."""

        try:
            self._control.update_item(
                Key={"PK": f"JOB#{job_id}", "SK": "META"},
                UpdateExpression=(
                    "SET #state = :preparing, lastCoordinatorError = :error, "
                    "updatedAt = :now REMOVE coordinatorLeaseUntil, "
                    "coordinatorOwner"
                ),
                ConditionExpression=(
                    "coordinatorOwner = :owner AND (#state = :expanding OR "
                    "(#state = :running AND "
                    "(attribute_not_exists(enqueueComplete) OR "
                    "enqueueComplete = :false)))"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":preparing": "preparing",
                    ":expanding": "expanding",
                    ":running": "running",
                    ":false": False,
                    ":error": error,
                    ":now": updated_at,
                    ":owner": owner,
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def recoverable_job_ids(self, *, now: str, limit: int) -> list[str]:
        found: list[str] = []
        cursor: Mapping[str, Any] | None = None
        while True:
            request: dict[str, Any] = {
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": "BACKFILL_JOBS"},
                "Limit": 100,
            }
            if cursor:
                request["ExclusiveStartKey"] = cursor
            response = self._control.query(**request)
            for projection in response.get("Items", []):
                job_id = projection.get("jobId") or projection.get("SK")
                if not isinstance(job_id, str):
                    continue
                item = self.get_job(job_id)
                if not item:
                    self._remove_recovery_projection(job_id)
                    continue
                state = item.get("state")
                if state in {"completed", "failed"} or (
                    state == "running" and item.get("enqueueComplete") is True
                ):
                    self._remove_recovery_projection(job_id)
                    continue
                lease = item.get("coordinatorLeaseUntil")
                lease_expired = not isinstance(lease, str) or lease <= now
                recoverable = (
                    state == "preparing"
                    or (state == "expanding" and lease_expired)
                    or (
                        state == "running"
                        and item.get("enqueueComplete") is not True
                        and lease_expired
                    )
                )
                if recoverable:
                    found.append(job_id)
                    if len(found) >= limit:
                        return found
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
        return found

    def _remove_recovery_projection(self, job_id: str) -> None:
        self._control.delete_item(
            Key={"PK": "BACKFILL_JOBS", "SK": job_id}
        )


_coordinator: BackfillCoordinator | None = None


def handler(
    event: Mapping[str, Any],
    _context: object,
    *,
    coordinator: BackfillCoordinator | None = None,
) -> dict[str, Any]:
    active = coordinator or _default_coordinator()
    if event.get("action") == "sweep":
        recovered = active.sweep(limit=1)
        return {"recovered": recovered}
    job_id = event.get("jobId")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("jobId is required")
    try:
        total = active.process(job_id)
    except Exception:
        _LOGGER.exception("Backfill coordination failed: jobId=%s", job_id)
        raise
    return {"jobId": job_id, "total": total}


lambda_handler = handler


def _default_coordinator() -> BackfillCoordinator:
    global _coordinator
    if _coordinator is None:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        repository = DynamoCoordinatorRepository(
            dynamodb.Table(os.environ["DATA_TABLE"]),
            dynamodb.Table(os.environ["CONTROL_TABLE"]),
            boto3.client("sqs"),
            os.environ["INGEST_QUEUE_URL"],
        )
        root = Path(__file__).resolve().parents[2] / "data" / "universes"
        _coordinator = BackfillCoordinator(repository, universe_root=root)
    return _coordinator


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")

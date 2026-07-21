"""Creation and resumable enqueueing of symbol-year backfill jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import json
import re
from typing import Any, Callable, Mapping
from uuid import uuid4

from src.ingest.repository import DynamoDBIngestRepository
from src.ingest.universe import Universe


_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class BackfillEnqueueError(RuntimeError):
    """Raised when SQS rejects backfill work."""


class BackfillJobs:
    def __init__(
        self,
        repository: DynamoDBIngestRepository,
        control_table: Any,
        sqs: Any,
        queue_url: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._control = control_table
        self._sqs = sqs
        self._queue_url = queue_url
        self._clock = clock or (lambda: datetime.now(UTC))

    def create_job(
        self,
        universe: Universe,
        *,
        from_year: int,
        to_year: int,
        job_id: str | None = None,
    ) -> str:
        if from_year > to_year:
            raise ValueError("from_year must not be after to_year")
        if from_year < 1900 or to_year > 2100:
            raise ValueError("years must be between 1900 and 2100")
        identifier = job_id or f"job_{uuid4().hex}"
        if not _JOB_ID.fullmatch(identifier):
            raise ValueError("job_id is invalid")

        for metadata in universe.symbols:
            self._repository.seed_symbol(metadata, market=universe.market)

        symbols = sorted({metadata["symbol"] for metadata in universe.symbols})
        total = len(symbols) * (to_year - from_year + 1)
        timestamp = _timestamp(self._clock())
        params = {
            "universe": universe.universe_id,
            "fromYear": from_year,
            "toYear": to_year,
        }
        meta = {
            "PK": f"JOB#{identifier}",
            "SK": "META",
            "type": "backfill",
            "state": "running",
            "params": params,
            "total": total,
            "completed": 0,
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        if not self._put_if_absent(meta):
            existing = self._control.get_item(
                Key={"PK": meta["PK"], "SK": "META"}, ConsistentRead=True
            ).get("Item")
            if (
                not existing
                or existing.get("type") != "backfill"
                or existing.get("params") != params
                or int(existing.get("total", -1)) != total
            ):
                raise ValueError("job_id already exists with different parameters")
        for symbol in symbols:
            for year in range(from_year, to_year + 1):
                self._put_if_absent(
                    {
                        "PK": f"JOB#{identifier}",
                        "SK": f"WORK#{symbol}#YEAR#{year}",
                        "symbol": symbol,
                        "year": year,
                        "state": "pending",
                        "createdAt": timestamp,
                    }
                )
        return identifier

    def _put_if_absent(self, item: dict[str, Any]) -> bool:
        try:
            self._control.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True
        except Exception as exc:
            if _is_conditional_failure(exc):
                return False
            raise

    def enqueue_pending(self, job_id: str) -> int:
        if not _JOB_ID.fullmatch(job_id):
            raise ValueError("job_id is invalid")
        job_key = {"PK": f"JOB#{job_id}", "SK": "META"}
        if not self._control.get_item(Key=job_key, ConsistentRead=True).get("Item"):
            raise LookupError(f"backfill job {job_id!r} was not found")

        pending: set[tuple[str, int]] = set()
        cursor: Mapping[str, Any] | None = None
        while True:
            request: dict[str, Any] = {
                "KeyConditionExpression": (
                    "PK = :pk AND begins_with(SK, :workPrefix)"
                ),
                "ExpressionAttributeValues": {
                    ":pk": f"JOB#{job_id}",
                    ":workPrefix": "WORK#",
                },
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._control.query(**request)
            for item in response.get("Items", []):
                if item.get("state") == "completed":
                    continue
                symbol = item.get("symbol")
                raw_year = item.get("year")
                if (
                    not isinstance(symbol, str)
                    or isinstance(raw_year, bool)
                    or not isinstance(raw_year, (int, Decimal))
                    or raw_year != int(raw_year)
                ):
                    raise ValueError("backfill work item is invalid")
                year = int(raw_year)
                pending.add((symbol, year))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break

        messages = [
            json.dumps(
                {
                    "v": 1,
                    "kind": "backfill_year",
                    "jobId": job_id,
                    "symbol": symbol,
                    "year": year,
                },
                separators=(",", ":"),
            )
            for symbol, year in sorted(pending)
        ]
        for offset in range(0, len(messages), 10):
            entries = [
                {"Id": f"work-{offset + index}", "MessageBody": body}
                for index, body in enumerate(messages[offset : offset + 10])
            ]
            response = self._sqs.send_message_batch(
                QueueUrl=self._queue_url,
                Entries=entries,
            )
            failed = response.get("Failed", [])
            if failed:
                identifiers = ", ".join(str(item.get("Id")) for item in failed)
                raise BackfillEnqueueError(
                    f"SQS rejected backfill messages: {identifiers}"
                )
        return len(messages)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"

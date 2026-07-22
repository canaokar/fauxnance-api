"""DynamoDB writes shared by manual and queued EOD ingestion."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime
import random
import time
from typing import Any, Callable, Mapping, Sequence

from src.shared.market_data import Candle, CorporateAction


class DynamoDBIngestRepository:
    def __init__(
        self,
        data_table: Any,
        control_table: Any | None = None,
        *,
        transaction_client: Any | None = None,
        max_merge_attempts: int = 5,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self._data = data_table
        self._control = control_table
        self._transaction_client = transaction_client
        self._max_merge_attempts = max_merge_attempts
        self._pause = pause

    def write_candles(self, symbol: str, candles: Sequence[Candle]) -> None:
        chunks: dict[str, list[Candle]] = defaultdict(list)
        for candle in candles:
            chunks[candle.date.strftime("%Y-%m")].append(candle)
        for month, month_candles in sorted(chunks.items()):
            self._merge_chunk(symbol, month, month_candles)

    def get_adapter_hints(self, symbol: str) -> Mapping[str, Any]:
        item = self._data.get_item(
            Key={"PK": f"SYM#{symbol}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")
        hints = item.get("adapterHints") if item else None
        return dict(hints) if isinstance(hints, Mapping) else {}

    def write_actions(
        self, symbol: str, actions: Sequence[CorporateAction]
    ) -> None:
        """Merge action corrections by ex-date and type with optimistic locking."""

        if not actions:
            return
        key = {"PK": f"SYM#{symbol}", "SK": "ADJ"}
        for attempt in range(self._max_merge_attempts):
            existing = self._data.get_item(Key=key, ConsistentRead=True).get("Item")
            prior = existing.get("actions", []) if existing else []
            by_identity = {
                (str(item["date"]), str(item["type"])): dict(item)
                for item in prior
            }
            for action in actions:
                by_identity[(action.date.isoformat(), action.type)] = _action_item(action)

            revision = int(existing.get("revision", 0)) if existing else 0
            item = {
                **key,
                "revision": revision + 1,
                "actions": sorted(
                    by_identity.values(),
                    key=lambda value: (str(value["date"]), str(value["type"])),
                    reverse=True,
                ),
            }
            request: dict[str, Any] = {"Item": item}
            if existing:
                request.update(
                    {
                        "ConditionExpression": "revision = :expected",
                        "ExpressionAttributeValues": {":expected": revision},
                    }
                )
            else:
                request["ConditionExpression"] = "attribute_not_exists(PK)"
            try:
                self._data.put_item(**request)
                return
            except Exception as exc:
                if (
                    not _is_conditional_failure(exc)
                    or attempt + 1 == self._max_merge_attempts
                ):
                    raise
                self._pause(random.uniform(0.02, 0.1) * (attempt + 1))

    def write_symbol(
        self,
        metadata: Mapping[str, Any],
        *,
        market: str,
        first_date: date,
        last_date: date,
    ) -> None:
        """Seed a registry item without allowing reruns to regress coverage."""

        symbol = metadata["symbol"]
        common = _symbol_metadata(metadata, market)
        try:
            self._data.put_item(
                Item={
                    "PK": f"SYM#{symbol}",
                    "SK": "META",
                    **common,
                    "coverage": {
                        "eodFrom": first_date.isoformat(),
                        "eodTo": last_date.isoformat(),
                    },
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise
            self._refresh_symbol_metadata(common)
            self.advance_symbol_coverage(symbol, first_date, last_date)

        self._data.put_item(Item={"PK": "SYMBOLS", "SK": symbol, **common})

    def seed_symbol(self, metadata: Mapping[str, Any], *, market: str) -> None:
        """Register a symbol before its first candle without inventing coverage."""

        common = _symbol_metadata(metadata, market)
        symbol = common["symbol"]
        existing = False
        try:
            self._data.put_item(
                Item={
                    "PK": f"SYM#{symbol}",
                    "SK": "META",
                    **common,
                    "coverage": {},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise
            existing = True
            self._refresh_symbol_metadata(common)
        if existing:
            self._refresh_symbol_projection(common)
        else:
            self._data.put_item(Item={"PK": "SYMBOLS", "SK": symbol, **common})

    def advance_symbol_coverage(
        self, symbol: str, first_date: date, last_date: date
    ) -> None:
        key = {"PK": f"SYM#{symbol}", "SK": "META"}
        updates = (
            ("eodFrom", first_date.isoformat(), ">"),
            ("eodTo", last_date.isoformat(), "<"),
        )
        for field, value, comparison in updates:
            try:
                self._advance_coverage_field(key, field, value, comparison)
            except Exception as exc:
                if _is_missing_document_path(exc):
                    self._ensure_coverage_map(key)
                    try:
                        self._advance_coverage_field(key, field, value, comparison)
                    except Exception as retry_exc:
                        if not _is_conditional_failure(retry_exc):
                            raise
                elif not _is_conditional_failure(exc):
                    raise

    def _advance_coverage_field(
        self,
        key: Mapping[str, str],
        field: str,
        value: str,
        comparison: str,
    ) -> None:
        self._data.update_item(
            Key=key,
            UpdateExpression="SET #coverage.#field = :value",
            ConditionExpression=(
                "attribute_exists(PK) AND "
                f"(attribute_not_exists(#coverage.#field) OR "
                f"#coverage.#field {comparison} :value)"
            ),
            ExpressionAttributeNames={
                "#coverage": "coverage",
                "#field": field,
            },
            ExpressionAttributeValues={":value": value},
        )

    def _ensure_coverage_map(self, key: Mapping[str, str]) -> None:
        self._data.update_item(
            Key=key,
            UpdateExpression=(
                "SET #coverage = if_not_exists(#coverage, :emptyCoverage)"
            ),
            ExpressionAttributeNames={"#coverage": "coverage"},
            ExpressionAttributeValues={":emptyCoverage": {}},
        )

    def advance_market_status(
        self, market: str, latest_eod: date, *, now: datetime | None = None
    ) -> None:
        try:
            self._data.update_item(
                Key={"PK": f"MARKET#{market}", "SK": "STATUS"},
                UpdateExpression="SET latestEod = :latest, updatedAt = :updated",
                ConditionExpression=(
                    "attribute_not_exists(latestEod) OR latestEod < :latest"
                ),
                ExpressionAttributeValues={
                    ":latest": latest_eod.isoformat(),
                    ":updated": _utc_timestamp(now),
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def complete_backfill_work(
        self,
        job_id: str,
        symbol: str,
        year: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Atomically complete one work item and increment its job exactly once."""

        if self._control is None:
            raise RuntimeError("control table is required for backfill completion")
        client = self._transaction_client or self._control.meta.client
        timestamp = _utc_timestamp(now)
        work_key = {"PK": f"JOB#{job_id}", "SK": f"WORK#{symbol}#YEAR#{year}"}
        job_key = {"PK": f"JOB#{job_id}", "SK": "META"}
        for attempt in range(self._max_merge_attempts):
            try:
                client.transact_write_items(
                    TransactItems=[
                    {
                        "Update": {
                            "TableName": self._control.name,
                            "Key": work_key,
                            "UpdateExpression": (
                                "SET #state = :completed, completedAt = :now"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(PK) AND "
                                "(attribute_not_exists(#state) OR "
                                "(#state <> :completed AND #state <> :failed))"
                            ),
                            "ExpressionAttributeNames": {"#state": "state"},
                            "ExpressionAttributeValues": {
                                ":completed": "completed",
                                ":failed": "failed",
                                ":now": timestamp,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._control.name,
                            "Key": job_key,
                            "UpdateExpression": (
                                "SET updatedAt = :now ADD #completed :one"
                            ),
                            "ConditionExpression": "attribute_exists(PK)",
                            "ExpressionAttributeNames": {"#completed": "completed"},
                            "ExpressionAttributeValues": {
                                ":now": timestamp,
                                ":one": 1,
                            },
                        }
                    },
                    ]
                )
                self._complete_job_if_done(job_id, timestamp)
                return True
            except Exception as exc:
                if not _is_transaction_cancelled(exc):
                    raise
                item = self._control.get_item(
                    Key=work_key, ConsistentRead=True
                ).get("Item")
                if item and item.get("state") in {"completed", "failed"}:
                    if item.get("state") == "completed":
                        self._complete_job_if_done(job_id, timestamp)
                    else:
                        self._fail_job_if_done(job_id, timestamp)
                    return False
                if (
                    _is_transaction_conflict(exc)
                    and attempt + 1 < self._max_merge_attempts
                ):
                    self._pause(random.uniform(0.02, 0.1) * (attempt + 1))
                    continue
                raise
        raise RuntimeError("backfill completion attempts exhausted")  # pragma: no cover

    def _complete_job_if_done(self, job_id: str, timestamp: str) -> None:
        if self._control is None:  # pragma: no cover - guarded by caller
            return
        key = {"PK": f"JOB#{job_id}", "SK": "META"}
        item = self._control.get_item(Key=key, ConsistentRead=True).get("Item")
        if not item:
            return
        completed = int(item.get("completed", 0))
        failed = int(item.get("failed", 0))
        total = int(item.get("total", 0))
        if total < 1 or completed + failed < total:
            return
        if failed > 0:
            self._fail_job_if_done(job_id, timestamp)
            return
        try:
            self._control.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #state = :completedState, completedAt = :now, "
                    "updatedAt = :now"
                ),
                ConditionExpression=(
                    "#completed >= :total AND "
                    "(attribute_not_exists(#state) OR #state <> :completedState)"
                ),
                ExpressionAttributeNames={
                    "#state": "state",
                    "#completed": "completed",
                },
                ExpressionAttributeValues={
                    ":completedState": "completed",
                    ":total": total,
                    ":now": timestamp,
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def fail_backfill_work(
        self,
        job_id: str,
        symbol: str,
        year: int,
        *,
        failure: str,
        now: datetime | None = None,
    ) -> bool:
        """Atomically fail one exhausted work item and its parent job exactly once."""

        if self._control is None:
            raise RuntimeError("control table is required for backfill failure")
        client = self._transaction_client or self._control.meta.client
        timestamp = _utc_timestamp(now)
        work_key = {"PK": f"JOB#{job_id}", "SK": f"WORK#{symbol}#YEAR#{year}"}
        job_key = {"PK": f"JOB#{job_id}", "SK": "META"}
        failure_key = {
            "PK": f"JOB#{job_id}",
            "SK": f"FAIL#{symbol}#YEAR#{year}",
        }
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._control.name,
                            "Key": work_key,
                            "UpdateExpression": (
                                "SET #state = :failedState, failure = :failure, "
                                "failedAt = :now"
                            ),
                            "ConditionExpression": (
                                "attribute_exists(PK) AND "
                                "(attribute_not_exists(#state) OR "
                                "(#state <> :completedState AND "
                                "#state <> :failedState))"
                            ),
                            "ExpressionAttributeNames": {"#state": "state"},
                            "ExpressionAttributeValues": {
                                ":failedState": "failed",
                                ":completedState": "completed",
                                ":failure": failure,
                                ":now": timestamp,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._control.name,
                            "Key": job_key,
                            "UpdateExpression": (
                                "SET updatedAt = :now ADD #failed :one"
                            ),
                            "ConditionExpression": "attribute_exists(PK)",
                            "ExpressionAttributeNames": {"#failed": "failed"},
                            "ExpressionAttributeValues": {
                                ":now": timestamp,
                                ":one": 1,
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._control.name,
                            "Item": {
                                **failure_key,
                                "symbol": symbol,
                                "year": year,
                                "failure": failure,
                                "failedAt": timestamp,
                            },
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except Exception as exc:
            if not _is_transaction_cancelled(exc):
                raise
            item = self._control.get_item(
                Key=work_key, ConsistentRead=True
            ).get("Item")
            if item and item.get("state") in {"failed", "completed"}:
                self._fail_job_if_done(job_id, timestamp)
                return False
            raise
        self._fail_job_if_done(job_id, timestamp)
        return True

    def _fail_job_if_done(self, job_id: str, timestamp: str) -> None:
        if self._control is None:  # pragma: no cover - guarded by caller
            return
        key = {"PK": f"JOB#{job_id}", "SK": "META"}
        item = self._control.get_item(Key=key, ConsistentRead=True).get("Item")
        if not item:
            return
        completed = int(item.get("completed", 0))
        failed = int(item.get("failed", 0))
        total = int(item.get("total", 0))
        if total < 1 or completed + failed < total:
            return
        try:
            self._control.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #state = :failedState, failedAt = :now, updatedAt = :now"
                ),
                ConditionExpression=(
                    "#completed = :completed AND #failed = :failed AND "
                    "#failed > :zero AND #state <> :completedState"
                ),
                ExpressionAttributeNames={
                    "#state": "state",
                    "#completed": "completed",
                    "#failed": "failed",
                },
                ExpressionAttributeValues={
                    ":failedState": "failed",
                    ":completedState": "completed",
                    ":completed": completed,
                    ":failed": failed,
                    ":zero": 0,
                    ":now": timestamp,
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def _merge_chunk(
        self, symbol: str, month: str, incoming: Sequence[Candle]
    ) -> None:
        key = {"PK": f"SYM#{symbol}", "SK": f"EOD#{month}"}
        for attempt in range(self._max_merge_attempts):
            existing = self._data.get_item(Key=key, ConsistentRead=True).get("Item")
            prior_candles = existing.get("candles", []) if existing else []
            by_date = {str(row["d"]): dict(row) for row in prior_candles}
            changed = False
            for candle in incoming:
                day = candle.date.isoformat()
                if day not in by_date:
                    by_date[day] = _candle_item(candle)
                    changed = True
            if existing and not changed:
                return

            revision = int(existing.get("revision", 0)) if existing else 0
            item = {
                **key,
                "revision": revision + 1,
                "candles": [by_date[day] for day in sorted(by_date)],
            }
            request: dict[str, Any] = {"Item": item}
            if existing:
                request.update(
                    {
                        "ConditionExpression": "revision = :expected",
                        "ExpressionAttributeValues": {":expected": revision},
                    }
                )
            else:
                request["ConditionExpression"] = "attribute_not_exists(PK)"
            try:
                self._data.put_item(**request)
                return
            except Exception as exc:
                if (
                    not _is_conditional_failure(exc)
                    or attempt + 1 == self._max_merge_attempts
                ):
                    raise
                self._pause(random.uniform(0.02, 0.1) * (attempt + 1))

    def _refresh_symbol_metadata(self, common: Mapping[str, Any]) -> None:
        update = (
            "SET #name = :name, #type = :type, exchange = :exchange, "
            "currency = :currency, market = :market, active = :active, "
            "#coverage = if_not_exists(#coverage, :emptyCoverage)"
        )
        values = {
            ":name": common["name"],
            ":type": common["type"],
            ":exchange": common["exchange"],
            ":currency": common["currency"],
            ":market": common["market"],
            ":active": True,
            ":emptyCoverage": {},
        }
        if "adapterHints" in common:
            update += ", adapterHints = :adapterHints"
            values[":adapterHints"] = common["adapterHints"]
        self._data.update_item(
            Key={"PK": f"SYM#{common['symbol']}", "SK": "META"},
            UpdateExpression=update,
            ExpressionAttributeNames={
                "#name": "name",
                "#type": "type",
                "#coverage": "coverage",
            },
            ExpressionAttributeValues=values,
        )

    def _refresh_symbol_projection(self, common: Mapping[str, Any]) -> None:
        update = (
            "SET symbol = :symbol, #name = :name, #type = :type, "
            "exchange = :exchange, currency = :currency, market = :market, "
            "active = :active"
        )
        values = {
            ":symbol": common["symbol"],
            ":name": common["name"],
            ":type": common["type"],
            ":exchange": common["exchange"],
            ":currency": common["currency"],
            ":market": common["market"],
            ":active": True,
        }
        if "adapterHints" in common:
            update += ", adapterHints = :adapterHints"
            values[":adapterHints"] = common["adapterHints"]
        self._data.update_item(
            Key={"PK": "SYMBOLS", "SK": common["symbol"]},
            UpdateExpression=update,
            ExpressionAttributeNames={"#name": "name", "#type": "type"},
            ExpressionAttributeValues=values,
        )


def _candle_item(candle: Candle) -> dict[str, Any]:
    return {
        "d": candle.date.isoformat(),
        "o": candle.open,
        "h": candle.high,
        "l": candle.low,
        "c": candle.close,
        "v": candle.volume,
        "src": candle.source,
    }


def _action_item(action: CorporateAction) -> dict[str, Any]:
    item: dict[str, Any] = {
        "date": action.date.isoformat(),
        "type": action.type,
        "value": action.value,
        "factor": action.factor,
    }
    if action.reference_close is not None:
        item["referenceClose"] = action.reference_close
    return item


def _symbol_metadata(
    metadata: Mapping[str, Any], market: str
) -> dict[str, Any]:
    item = {
        "symbol": metadata["symbol"],
        "name": metadata["name"],
        "type": metadata["type"],
        "exchange": metadata["exchange"],
        "currency": metadata["currency"],
        "market": market,
        "active": True,
    }
    if metadata.get("adapterHints"):
        item["adapterHints"] = dict(metadata["adapterHints"])
    return item


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def _is_missing_document_path(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    error = response.get("Error", {})
    return (
        error.get("Code") == "ValidationException"
        and "document path" in str(error.get("Message", "")).lower()
    )


def _is_transaction_cancelled(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "TransactionCanceledException"


def _is_transaction_conflict(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    codes = {
        reason.get("Code")
        for reason in response.get("CancellationReasons", [])
        if isinstance(reason, Mapping)
        and reason.get("Code") not in (None, "None")
    }
    return codes == {"TransactionConflict"}


def _utc_timestamp(now: datetime | None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

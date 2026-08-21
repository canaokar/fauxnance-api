"""DynamoDB persistence for the administrative API."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence


class RepositoryConflict(RuntimeError):
    """A conditional administrative write lost a race."""


class DynamoAdminRepository:
    def __init__(
        self,
        table: Any,
        dynamodb: Any,
        data_table: Any | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self._table = table
        self._dynamodb = dynamodb
        # A boto3 resource's `.meta.client` auto-serializes plain Python
        # values into DynamoDB AttributeValues. `_transact`/`_put`/`_update`
        # below already pre-serialize with TypeSerializer, so using
        # `.meta.client` here double-serializes every transactional write
        # and corrupts it. Real AWS callers must pass a genuine low-level
        # client (boto3.client("dynamodb")) via `client`.
        self._client = client if client is not None else dynamodb.meta.client
        self._data_table = data_table

    def create_cohort(self, item: Mapping[str, Any]) -> None:
        projection = _cohort_projection(item)
        try:
            self._transact(
                [
                    _put(self._table.name, dict(item), conditional=True),
                    _put(self._table.name, projection, conditional=True),
                ]
            )
        except Exception as exc:
            if _is_transaction_cancelled(exc):
                raise RepositoryConflict("cohort already exists") from exc
            raise

    def get_cohort(self, cohort_id: str) -> Mapping[str, Any] | None:
        return self._table.get_item(
            Key={"PK": f"COHORT#{cohort_id}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")

    def update_cohort(self, item: Mapping[str, Any]) -> None:
        cohort_id = str(item["cohortId"])
        projection = _cohort_projection(item)
        try:
            self._transact(
                [
                    _put(
                        self._table.name,
                        dict(item),
                        condition="attribute_exists(PK)",
                    ),
                    _put(self._table.name, projection),
                ]
            )
        except Exception as exc:
            if _is_transaction_cancelled(exc):
                raise RepositoryConflict(f"cohort {cohort_id} changed") from exc
            raise

    def all_cohorts(self, *, max_pages: int = 10, page_size: int = 200) -> list[Mapping[str, Any]]:
        """Return indexed cohorts plus bounded legacy META scans, without duplicates."""

        by_id: dict[str, Mapping[str, Any]] = {}
        cursor: Mapping[str, Any] | None = None
        for _ in range(max_pages):
            request: dict[str, Any] = {
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": "COHORTS"},
                "Limit": page_size,
            }
            if cursor:
                request["ExclusiveStartKey"] = cursor
            response = self._table.query(**request)
            for item in response.get("Items", []):
                cohort_id = item.get("cohortId") or item.get("SK")
                if isinstance(cohort_id, str):
                    by_id[cohort_id] = item
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break

        cursor = None
        for _ in range(max_pages):
            request = {
                "FilterExpression": "begins_with(PK, :prefix) AND SK = :meta",
                "ExpressionAttributeValues": {":prefix": "COHORT#", ":meta": "META"},
                "Limit": page_size,
            }
            if cursor:
                request["ExclusiveStartKey"] = cursor
            response = self._table.scan(**request)
            for item in response.get("Items", []):
                cohort_id = item.get("cohortId")
                if not isinstance(cohort_id, str):
                    pk = item.get("PK")
                    if isinstance(pk, str) and pk.startswith("COHORT#"):
                        cohort_id = pk.removeprefix("COHORT#")
                if isinstance(cohort_id, str):
                    by_id[cohort_id] = {**item, "cohortId": cohort_id}
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
        return list(by_id.values())

    def query_cohort_keys(
        self,
        cohort_id: str,
        *,
        limit: int | None = None,
        cursor: Mapping[str, Any] | None = None,
    ) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]:
        request: dict[str, Any] = {
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {
                ":pk": f"COHORT#{cohort_id}",
                ":prefix": "KEY#",
            },
        }
        if limit is not None:
            request["Limit"] = limit
        if cursor:
            request["ExclusiveStartKey"] = dict(cursor)
        response = self._table.query(**request)
        return list(response.get("Items", [])), response.get("LastEvaluatedKey")

    def usage_counts(self, key_ids: Sequence[str], usage_date: date) -> dict[str, int]:
        counts: dict[str, int] = {}
        pending = [
            {"PK": f"KEYID#{key_id}", "SK": f"USAGE#{usage_date.isoformat()}"}
            for key_id in key_ids
        ]
        while pending:
            request_keys = pending[:100]
            pending = pending[100:]
            response = self._dynamodb.batch_get_item(
                RequestItems={self._table.name: {"Keys": request_keys}}
            )
            for item in response.get("Responses", {}).get(self._table.name, []):
                pk = item.get("PK")
                if isinstance(pk, str) and pk.startswith("KEYID#"):
                    counts[pk.removeprefix("KEYID#")] = int(item.get("count", 0))
            pending.extend(
                response.get("UnprocessedKeys", {})
                .get(self._table.name, {})
                .get("Keys", [])
            )
        return counts

    def issue_keys(
        self,
        cohort: Mapping[str, Any],
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        cohort_id = str(cohort["cohortId"])
        items: list[dict[str, Any]] = [
            {
                "ConditionCheck": {
                    "TableName": self._table.name,
                    "Key": _serialize({"PK": f"COHORT#{cohort_id}", "SK": "META"}),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ConditionExpression": (
                        "#status = :active AND expiresAt = :expires"
                    ),
                    "ExpressionAttributeValues": _serialize(
                        {
                            ":active": "active",
                            ":expires": cohort["expiresAt"],
                        }
                    ),
                }
            }
        ]
        for record in records:
            digest = str(record["keyHash"])
            key_id = str(record["keyId"])
            common = {
                "keyId": key_id,
                "label": record["label"],
                "cohortId": cohort_id,
                "dailyQuota": record["dailyQuota"],
                "status": "active",
                "createdAt": record["createdAt"],
                "expiresAt": cohort["expiresAt"],
            }
            identity = {
                key: record[key]
                for key in ("studentName", "studentEmail")
                if isinstance(record.get(key), str) and record[key]
            }
            items.extend(
                [
                    _put(
                        self._table.name,
                        {"PK": f"KEY#{digest}", "SK": "META", "type": "student", **common},
                        conditional=True,
                    ),
                    _put(
                        self._table.name,
                        {
                            "PK": f"KEYID#{key_id}",
                            "SK": "META",
                            "keyHash": digest,
                            "cohortId": cohort_id,
                            "createdAt": record["createdAt"],
                        },
                        conditional=True,
                    ),
                    _put(
                        self._table.name,
                        {"PK": f"COHORT#{cohort_id}", "SK": f"KEY#{key_id}", **common, **identity},
                        conditional=True,
                    ),
                ]
            )
        try:
            self._transact(items)
        except Exception as exc:
            if _is_transaction_cancelled(exc):
                raise RepositoryConflict("key issuance conflicted with another write") from exc
            raise

    def get_key_lookup(self, key_id: str) -> Mapping[str, Any] | None:
        return self._table.get_item(
            Key={"PK": f"KEYID#{key_id}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")

    def revoke_key(self, lookup: Mapping[str, Any], *, revoked_at: str) -> None:
        key_id = str(lookup["keyId"])
        digest = str(lookup["keyHash"])
        cohort_id = str(lookup["cohortId"])
        values = _serialize({":revoked": "revoked", ":now": revoked_at})
        names = {"#status": "status"}
        updates = []
        for key in (
            {"PK": f"KEY#{digest}", "SK": "META"},
            {"PK": f"COHORT#{cohort_id}", "SK": f"KEY#{key_id}"},
        ):
            updates.append(
                {
                    "Update": {
                        "TableName": self._table.name,
                        "Key": _serialize(key),
                        "UpdateExpression": "SET #status = :revoked, revokedAt = :now, updatedAt = :now",
                        "ConditionExpression": "attribute_exists(PK)",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                }
            )
        try:
            self._transact(updates)
        except Exception as exc:
            if _is_transaction_cancelled(exc):
                raise RepositoryConflict("key changed during revocation") from exc
            raise

    def seed_symbols(self, symbols: Sequence[Mapping[str, Any]]) -> None:
        if self._data_table is None:
            raise RuntimeError("data table is required for symbol registration")
        from src.ingest.repository import DynamoDBIngestRepository

        repository = DynamoDBIngestRepository(self._data_table)
        for metadata in symbols:
            repository.seed_symbol(metadata, market=str(metadata["market"]))

    def get_symbol(self, symbol: str) -> Mapping[str, Any] | None:
        if self._data_table is None:
            raise RuntimeError("data table is required for symbol reads")
        return self._data_table.get_item(
            Key={"PK": f"SYM#{symbol}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")

    def create_backfill_job(self, item: Mapping[str, Any]) -> None:
        try:
            self._transact(
                [
                    _put(self._table.name, dict(item), conditional=True),
                    _put(
                        self._table.name,
                        {
                            "PK": "BACKFILL_JOBS",
                            "SK": str(item["jobId"]),
                            "jobId": item["jobId"],
                            "createdAt": item["createdAt"],
                        },
                        conditional=True,
                    ),
                ]
            )
        except Exception as exc:
            if _is_transaction_cancelled(exc):
                raise RepositoryConflict("backfill job already exists") from exc
            raise

    def get_backfill_job(self, job_id: str) -> Mapping[str, Any] | None:
        return self._table.get_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")

    def record_backfill_dispatch_error(
        self, job_id: str, *, error: str, updated_at: str
    ) -> None:
        try:
            self._table.update_item(
                Key={"PK": f"JOB#{job_id}", "SK": "META"},
                UpdateExpression=(
                    "SET dispatchError = :error, dispatchErrorAt = :now, "
                    "updatedAt = :now"
                ),
                ConditionExpression="#state = :preparing",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":preparing": "preparing",
                    ":error": error,
                    ":now": updated_at,
                },
            )
        except Exception as exc:
            if not _is_conditional_failure(exc):
                raise

    def backfill_failures(
        self, job_id: str, *, limit: int
    ) -> tuple[list[Mapping[str, Any]], bool]:
        response = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk": f"JOB#{job_id}",
                ":prefix": "FAIL#",
            },
            ConsistentRead=True,
            Limit=limit + 1,
        )
        items = list(response.get("Items", []))
        truncated = len(items) > limit or bool(response.get("LastEvaluatedKey"))
        return items[:limit], truncated

    def _transact(self, items: Sequence[Mapping[str, Any]]) -> None:
        self._client.transact_write_items(TransactItems=list(items))


def _cohort_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in {"PK", "SK"}
    } | {"PK": "COHORTS", "SK": str(item["cohortId"])}


def _put(
    table_name: str,
    item: Mapping[str, Any],
    *,
    conditional: bool = False,
    condition: str | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "TableName": table_name,
        "Item": _serialize(item),
    }
    if conditional:
        request["ConditionExpression"] = "attribute_not_exists(PK)"
    elif condition:
        request["ConditionExpression"] = condition
    return {"Put": request}


def _serialize(item: Mapping[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def _is_transaction_cancelled(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "TransactionCanceledException"


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"

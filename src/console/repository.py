"""DynamoDB persistence for the instructor console (control table)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from src.admin.repository import RepositoryConflict


class DynamoConsoleRepository:
    def __init__(self, table: Any, dynamodb: Any) -> None:
        self._table = table
        self._dynamodb = dynamodb
        self._client = dynamodb.meta.client

    # -- users ---------------------------------------------------------

    def create_user(self, item: Mapping[str, Any]) -> None:
        email_lookup = {
            "PK": f"USEREMAIL#{item['email']}",
            "SK": "META",
            "userId": item["userId"],
            "createdAt": item["createdAt"],
        }
        projection = _user_projection(item)
        try:
            self._transact(
                [
                    _put(self._table.name, dict(item), conditional=True),
                    _put(self._table.name, email_lookup, conditional=True),
                    _put(self._table.name, projection, conditional=True),
                ]
            )
        except Exception as exc:
            if _is_transaction_cancelled(exc):
                raise RepositoryConflict("email already registered") from exc
            raise

    def get_user(self, user_id: str) -> Mapping[str, Any] | None:
        return self._table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")

    def get_user_by_email(self, email: str) -> Mapping[str, Any] | None:
        lookup = self._table.get_item(
            Key={"PK": f"USEREMAIL#{email}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")
        if lookup is None:
            return None
        user_id = lookup.get("userId")
        if not isinstance(user_id, str):
            return None
        return self.get_user(user_id)

    def all_users(
        self, *, max_pages: int = 10, page_size: int = 200
    ) -> list[Mapping[str, Any]]:
        """Query the USERS index partition, mirroring admin's COHORTS projection."""

        return self._query_paginated(
            key_condition="PK = :pk",
            values={":pk": "USERS"},
            max_pages=max_pages,
            page_size=page_size,
        )

    def update_user_status(
        self, user_id: str, *, status: str, updated_at: str
    ) -> None:
        update_expression = "SET #status = :status, updatedAt = :now"
        names = {"#status": "status"}
        values = {":status": status, ":now": updated_at}
        try:
            self._transact(
                [
                    _update(
                        self._table.name,
                        {"PK": f"USER#{user_id}", "SK": "META"},
                        update_expression=update_expression,
                        condition="attribute_exists(PK)",
                        names=names,
                        values=values,
                    ),
                    _update(
                        self._table.name,
                        {"PK": "USERS", "SK": user_id},
                        update_expression=update_expression,
                        condition="attribute_exists(PK)",
                        names=names,
                        values=values,
                    ),
                ]
            )
        except Exception as exc:
            if _is_transaction_cancelled(exc):
                raise RepositoryConflict(f"user {user_id} changed") from exc
            raise

    # -- sessions --------------------------------------------------------

    def create_session(self, item: Mapping[str, Any]) -> None:
        self._table.put_item(Item=dict(item))

    def get_session(self, token_hash: str) -> Mapping[str, Any] | None:
        return self._table.get_item(
            Key={"PK": f"SESSION#{token_hash}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")

    def delete_session(self, token_hash: str) -> None:
        self._table.delete_item(Key={"PK": f"SESSION#{token_hash}", "SK": "META"})

    # -- login throttle ----------------------------------------------------

    def record_login_failure(self, email: str, *, expires_at: int) -> int:
        # Fixed window, not sliding: a failure inside an active window must
        # increment the count without pushing expiresAt forward, or an
        # operator who keeps mistyping their password after a lockout starts
        # would re-arm a fresh window on every attempt and could never log
        # back in. Try a hard reset first (fires when there is no record, or
        # the stored window has already elapsed); only when that condition
        # fails - meaning a window is genuinely still active - fall back to
        # a bare increment that leaves expiresAt untouched.
        now = int(datetime.now(UTC).timestamp())
        key = {"PK": f"LOGINFAIL#{email}", "SK": "META"}
        try:
            response = self._table.update_item(
                Key=key,
                UpdateExpression="SET #count = :one, #expiresAt = :expiresAt",
                ConditionExpression="attribute_not_exists(PK) OR #expiresAt <= :now",
                ExpressionAttributeNames={"#count": "count", "#expiresAt": "expiresAt"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":expiresAt": int(expires_at),
                    ":now": now,
                },
                ReturnValues="UPDATED_NEW",
            )
        except Exception as exc:
            if not _is_conditional_check_failed(exc):
                raise
            response = self._table.update_item(
                Key=key,
                UpdateExpression="ADD #count :one",
                ExpressionAttributeNames={"#count": "count"},
                ExpressionAttributeValues={":one": 1},
                ReturnValues="UPDATED_NEW",
            )
        return int(response["Attributes"]["count"])

    def get_login_failures(self, email: str, now: datetime) -> int:
        item = self._table.get_item(
            Key={"PK": f"LOGINFAIL#{email}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            return 0
        expires_at = item.get("expiresAt")
        # DynamoDB TTL deletion is asynchronous (up to ~48h), so an expired
        # record can still be sitting in the table. Treat it as zero failures.
        # Uses the same "<=" boundary as the write path's reset condition, so
        # a record whose window ends exactly at `now` is read as expired here
        # and reset (not incremented) there - the two paths agree.
        if expires_at is None or int(expires_at) <= int(now.timestamp()):
            return 0
        return int(item.get("count", 0))

    def clear_login_failures(self, email: str) -> None:
        self._table.delete_item(Key={"PK": f"LOGINFAIL#{email}", "SK": "META"})

    # -- class assignment --------------------------------------------------

    def assign_instructor(
        self, assignment: Mapping[str, Any], reverse: Mapping[str, Any]
    ) -> None:
        try:
            self._transact(
                [
                    _put(self._table.name, dict(assignment), conditional=True),
                    _put(self._table.name, dict(reverse), conditional=True),
                ]
            )
        except Exception as exc:
            if _is_transaction_cancelled(exc):
                raise RepositoryConflict("instructor already assigned") from exc
            raise

    def unassign_instructor(self, cohort_id: str, user_id: str) -> None:
        self._transact(
            [
                _delete(
                    self._table.name,
                    {"PK": f"COHORT#{cohort_id}", "SK": f"INSTRUCTOR#{user_id}"},
                ),
                _delete(
                    self._table.name,
                    {"PK": f"USER#{user_id}", "SK": f"COHORT#{cohort_id}"},
                ),
            ]
        )

    def instructors_for_class(
        self, cohort_id: str, *, max_pages: int = 10, page_size: int = 200
    ) -> list[Mapping[str, Any]]:
        return self._query_paginated(
            key_condition="PK = :pk AND begins_with(SK, :prefix)",
            values={":pk": f"COHORT#{cohort_id}", ":prefix": "INSTRUCTOR#"},
            max_pages=max_pages,
            page_size=page_size,
        )

    def classes_for_user(
        self, user_id: str, *, max_pages: int = 10, page_size: int = 200
    ) -> list[Mapping[str, Any]]:
        return self._query_paginated(
            key_condition="PK = :pk AND begins_with(SK, :prefix)",
            values={":pk": f"USER#{user_id}", ":prefix": "COHORT#"},
            max_pages=max_pages,
            page_size=page_size,
        )

    def is_assigned(self, cohort_id: str, user_id: str) -> bool:
        item = self._table.get_item(
            Key={"PK": f"COHORT#{cohort_id}", "SK": f"INSTRUCTOR#{user_id}"},
            ConsistentRead=True,
        ).get("Item")
        return item is not None

    # -- internal ------------------------------------------------------

    def _query_paginated(
        self,
        *,
        key_condition: str,
        values: Mapping[str, Any],
        max_pages: int,
        page_size: int,
    ) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        cursor: Mapping[str, Any] | None = None
        for _ in range(max_pages):
            request: dict[str, Any] = {
                "KeyConditionExpression": key_condition,
                "ExpressionAttributeValues": dict(values),
                "Limit": page_size,
            }
            if cursor:
                request["ExclusiveStartKey"] = cursor
            response = self._table.query(**request)
            items.extend(response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
        return items

    def _transact(self, items: Sequence[Mapping[str, Any]]) -> None:
        self._client.transact_write_items(TransactItems=list(items))


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


def _delete(table_name: str, key: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "Delete": {
            "TableName": table_name,
            "Key": _serialize(key),
        }
    }


def _update(
    table_name: str,
    key: Mapping[str, Any],
    *,
    update_expression: str,
    values: Mapping[str, Any],
    names: Mapping[str, str] | None = None,
    condition: str | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "TableName": table_name,
        "Key": _serialize(key),
        "UpdateExpression": update_expression,
        "ExpressionAttributeValues": _serialize(values),
    }
    if names:
        request["ExpressionAttributeNames"] = dict(names)
    if condition:
        request["ConditionExpression"] = condition
    return {"Update": request}


def _user_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    """Thin USERS-index projection for create_user; never carries passwordHash."""

    return {
        "PK": "USERS",
        "SK": str(item["userId"]),
        "userId": item["userId"],
        "email": item.get("email"),
        "name": item.get("name"),
        "role": item.get("role"),
        "status": item.get("status"),
        "createdAt": item.get("createdAt"),
    }


def _serialize(item: Mapping[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in item.items()}


def _is_transaction_cancelled(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "TransactionCanceledException"


def _is_conditional_check_failed(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"

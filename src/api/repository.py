"""DynamoDB reads required by the Phase 1 public API."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping


class MarketDataRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    def get_symbol(self, symbol: str) -> Mapping[str, Any] | None:
        response = self._table.get_item(Key={"PK": f"SYM#{symbol}", "SK": "META"})
        return response.get("Item")

    def get_market_status(self, market: str) -> Mapping[str, Any] | None:
        response = self._table.get_item(
            Key={"PK": f"MARKET#{market}", "SK": "STATUS"}
        )
        return response.get("Item")

    def get_candle_chunks(
        self, symbol: str, start: date, end: date
    ) -> list[Mapping[str, Any]]:
        values = {
            ":pk": f"SYM#{symbol}",
            ":start": f"EOD#{start:%Y-%m}",
            ":end": f"EOD#{end:%Y-%m}",
        }
        items: list[Mapping[str, Any]] = []
        cursor: Mapping[str, Any] | None = None
        while True:
            request: dict[str, Any] = {
                "KeyConditionExpression": "PK = :pk AND SK BETWEEN :start AND :end",
                "ExpressionAttributeValues": values,
                "ConsistentRead": False,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._table.query(**request)
            items.extend(response.get("Items", []))
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return items

    def get_actions(self, symbol: str) -> list[Mapping[str, Any]]:
        response = self._table.get_item(Key={"PK": f"SYM#{symbol}", "SK": "ADJ"})
        item = response.get("Item")
        return list(item.get("actions", [])) if item else []


class IdentityRepository:
    def __init__(self, table: Any) -> None:
        self._table = table

    def get_usage_identity(self, key_id: str) -> dict[str, str | None]:
        lookup = self._table.get_item(
            Key={"PK": f"KEYID#{key_id}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")
        if not lookup:
            raise LookupError("API key lookup is missing")

        digest = lookup.get("keyHash")
        if not digest:
            raise LookupError("API key lookup has no hash")
        key = self._table.get_item(
            Key={"PK": f"KEY#{digest}", "SK": "META"},
            ConsistentRead=True,
        ).get("Item")
        if not key:
            raise LookupError("API key metadata is missing")

        cohort_id = key.get("cohortId") or lookup.get("cohortId")
        cohort_name: str | None = None
        if cohort_id:
            cohort = self._table.get_item(
                Key={"PK": f"COHORT#{cohort_id}", "SK": "META"},
                ConsistentRead=True,
            ).get("Item")
            if cohort:
                cohort_name = cohort.get("name")

        return {
            "keyLabel": key.get("label"),
            "cohort": cohort_name or cohort_id,
        }

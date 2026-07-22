"""DynamoDB reads required by the Phase 1 public API."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Mapping

from src.shared.market_data import Candle, Quote, SymbolMetadata


class MarketDataRepository:
    def __init__(self, table: Any, *, transaction_client: Any | None = None) -> None:
        self._table = table
        self._transaction_client = transaction_client

    def get_symbol(self, symbol: str) -> Mapping[str, Any] | None:
        response = self._table.get_item(Key={"PK": f"SYM#{symbol}", "SK": "META"})
        return response.get("Item")

    def get_market_status(self, market: str) -> Mapping[str, Any] | None:
        response = self._table.get_item(
            Key={"PK": f"MARKET#{market}", "SK": "STATUS"}
        )
        return response.get("Item")

    def register_discovered(
        self,
        metadata: SymbolMetadata,
        *,
        market: str,
        discovered_at: datetime,
        job_id: str,
    ) -> Mapping[str, Any]:
        timestamp = _timestamp(discovered_at)
        common = {
            "symbol": metadata.symbol,
            "name": metadata.name,
            "type": metadata.type,
            "exchange": metadata.exchange,
            "currency": metadata.currency,
            "market": market,
            "active": metadata.active,
            "discovered": True,
            "discoveredAt": timestamp,
            "adapterHints": dict(metadata.adapter_hints),
            "lazyBackfillJobId": job_id,
        }
        meta = {
            "PK": f"SYM#{metadata.symbol}",
            "SK": "META",
            **common,
            "coverage": {},
        }
        projection = {"PK": "SYMBOLS", "SK": metadata.symbol, **common}
        client = self._transaction_client or self._table.meta.client
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table.name,
                            "Item": meta,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table.name,
                            "Item": projection,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
            return meta
        except Exception as exc:
            if not _is_transaction_cancelled(exc):
                raise
            existing = self._table.get_item(
                Key={"PK": f"SYM#{metadata.symbol}", "SK": "META"},
                ConsistentRead=True,
            ).get("Item")
            if existing is None:
                raise
            return existing

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

    def get_quote(self, symbol: str) -> Mapping[str, Any] | None:
        response = self._table.get_item(Key={"PK": f"SYM#{symbol}", "SK": "QUOTE"})
        return response.get("Item")

    def put_quote(
        self,
        symbol: str,
        quote: Quote,
        *,
        fetched_at: datetime,
        expires_at: datetime,
    ) -> None:
        fetched_at = _as_utc(fetched_at)
        expires_at = _as_utc(expires_at)
        self._table.put_item(
            Item={
                "PK": f"SYM#{symbol}",
                "SK": "QUOTE",
                "quote": {
                    "price": quote.price,
                    "currency": quote.currency,
                    "change": quote.change,
                    "changePercent": quote.change_percent,
                    "previousClose": quote.previous_close,
                    "asOf": _timestamp(quote.as_of),
                    "marketState": quote.market_state,
                },
                "src": quote.source,
                "fetchedAt": _timestamp(fetched_at),
                "expiresAt": int(expires_at.timestamp()),
            }
        )

    def get_recent_real_candles(
        self, symbol: str, *, before: date, limit: int = 91
    ) -> list[Candle]:
        if limit < 1:
            raise ValueError("limit must be positive")
        values = {":pk": f"SYM#{symbol}", ":prefix": "EOD#"}
        found: list[Candle] = []
        cursor: Mapping[str, Any] | None = None
        while len(found) < limit:
            request: dict[str, Any] = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "ExpressionAttributeValues": values,
                "ScanIndexForward": False,
                "ConsistentRead": False,
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._table.query(**request)
            for item in response.get("Items", []):
                for row in reversed(item.get("candles", [])):
                    day = date.fromisoformat(str(row["d"]))
                    if day > before:
                        continue
                    found.append(
                        Candle(
                            date=day,
                            open=Decimal(str(row["o"])),
                            high=Decimal(str(row["h"])),
                            low=Decimal(str(row["l"])),
                            close=Decimal(str(row["c"])),
                            volume=int(row["v"]) if row.get("v") is not None else None,
                            source=str(row.get("src", "stored")),
                        )
                    )
                    if len(found) == limit:
                        break
                if len(found) == limit:
                    break
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
        return sorted(found, key=lambda candle: candle.date)


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _is_transaction_cancelled(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code") == "TransactionCanceledException"

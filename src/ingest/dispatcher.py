"""Nightly dispatcher for the registered US symbol universe."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import os
from typing import Any, Callable, Mapping


class DispatchError(RuntimeError):
    """Raised when SQS rejects one or more ingest messages."""


class EodDispatcher:
    def __init__(
        self,
        data_table: Any,
        sqs: Any,
        queue_url: str,
        *,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._data = data_table
        self._sqs = sqs
        self._queue_url = queue_url
        self._today = today or (lambda: datetime.now(UTC).date())

    def dispatch(self, market: str = "US") -> int:
        market = _market(market)
        end = self._today()
        start = end - timedelta(days=6)
        bodies = [
            json.dumps(
                {
                    "v": 2,
                    "kind": "eod_batch",
                    "market": market,
                    "symbol": symbol,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                },
                separators=(",", ":"),
            )
            for symbol in self._active_symbols(market)
        ]
        for offset in range(0, len(bodies), 10):
            entries = [
                {"Id": f"message-{offset + index}", "MessageBody": body}
                for index, body in enumerate(bodies[offset : offset + 10])
            ]
            response = self._sqs.send_message_batch(
                QueueUrl=self._queue_url,
                Entries=entries,
            )
            failed = response.get("Failed", [])
            if failed:
                identifiers = ", ".join(str(item.get("Id")) for item in failed)
                raise DispatchError(f"SQS rejected ingest messages: {identifiers}")
        return len(bodies)

    def _active_symbols(self, market: str) -> list[str]:
        curated: list[Mapping[str, Any]] = []
        discovered: list[Mapping[str, Any]] = []
        cursor: Mapping[str, Any] | None = None
        while True:
            request: dict[str, Any] = {
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": "SYMBOLS"},
            }
            if cursor is not None:
                request["ExclusiveStartKey"] = cursor
            response = self._data.query(**request)
            for item in response.get("Items", []):
                if item.get("active") is not True:
                    continue
                if item.get("discovered") is True:
                    if isinstance(item.get("discoveredAt"), str):
                        discovered.append(item)
                else:
                    curated.append(item)
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                selected = curated + sorted(
                    discovered,
                    key=lambda value: (
                        str(value.get("discoveredAt")),
                        str(value.get("symbol") or value.get("SK")),
                    ),
                    reverse=True,
                )[:300]
                symbols = {
                    str(item.get("symbol") or item.get("SK"))
                    for item in selected
                    if item.get("market") == market
                    and isinstance(item.get("symbol") or item.get("SK"), str)
                }
                return sorted(symbols)


_dispatcher: EodDispatcher | None = None


def handler(
    event: Mapping[str, Any],
    _context: object,
    *,
    dispatcher: EodDispatcher | None = None,
) -> dict[str, int]:
    active_dispatcher = dispatcher or _default_dispatcher()
    return {"enqueued": active_dispatcher.dispatch(str(event.get("market", "US")))}


lambda_handler = handler


def _default_dispatcher() -> EodDispatcher:
    global _dispatcher
    if _dispatcher is None:
        import boto3

        data_table = boto3.resource("dynamodb").Table(os.environ["DATA_TABLE"])
        _dispatcher = EodDispatcher(
            data_table,
            boto3.client("sqs"),
            os.environ["INGEST_QUEUE_URL"],
        )
    return _dispatcher


def _market(value: str) -> str:
    from src.shared.symbols import SUPPORTED_MARKETS

    canonical = value.strip().upper()
    if canonical not in SUPPORTED_MARKETS:
        raise ValueError("market is unsupported")
    return canonical

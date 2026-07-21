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

    def dispatch(self) -> int:
        end = self._today()
        start = end - timedelta(days=6)
        bodies = [
            json.dumps(
                {
                    "v": 1,
                    "kind": "eod_batch",
                    "market": "US",
                    "symbol": symbol,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                },
                separators=(",", ":"),
            )
            for symbol in self._active_us_symbols()
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

    def _active_us_symbols(self) -> list[str]:
        symbols: set[str] = set()
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
                if item.get("active") is True and item.get("market") == "US":
                    symbol = item.get("symbol") or item.get("SK")
                    if isinstance(symbol, str) and symbol:
                        symbols.add(symbol)
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                return sorted(symbols)


_dispatcher: EodDispatcher | None = None


def handler(
    _event: Mapping[str, Any],
    _context: object,
    *,
    dispatcher: EodDispatcher | None = None,
) -> dict[str, int]:
    active_dispatcher = dispatcher or _default_dispatcher()
    return {"enqueued": active_dispatcher.dispatch()}


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

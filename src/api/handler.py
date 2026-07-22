"""Single Lambda entry point for the Phase 1 public HTTP API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from typing import Any, Callable, Mapping

from src.api.repository import IdentityRepository, MarketDataRepository
from src.shared.auth import AuthContext, parse_authorizer_context
from src.shared.quota import QuotaExceeded, QuotaService, Usage
from src.shared.symbols import canonical_symbol


DISCLAIMER = "Educational data. Not for investment use."
_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        super().__init__(message)


class ApiService:
    def __init__(
        self,
        data_repository: MarketDataRepository,
        identity_repository: IdentityRepository,
        quota_service: QuotaService,
        *,
        health_markets: tuple[str, ...] = ("US",),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._data = data_repository
        self._identities = identity_repository
        self._quota = quota_service
        self._health_markets = health_markets
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        now = _as_utc(self._clock())
        path = str(event.get("rawPath") or event.get("path") or "")
        method = str(
            event.get("requestContext", {}).get("http", {}).get("method")
            or event.get("httpMethod")
            or ""
        ).upper()

        try:
            if method != "GET":
                raise ApiError(404, "NOT_FOUND", "Route was not found.")
            if path == "/v1/health":
                return _success(self._health(now), now=now)

            auth = _authorizer_context(event)
            usage = self._consume_quota(auth, now)

            if path == "/v1/usage":
                return _success(self._usage(auth, usage), now=now)
            if path.startswith("/v1/symbols/"):
                symbol = _path_symbol(event, path, "/v1/symbols/")
                data = self._symbol(symbol)
                return _success(data, now=now, symbol=symbol, source="stored")
            if path.startswith("/v1/candles/"):
                symbol = _path_symbol(event, path, "/v1/candles/")
                data, as_of = self._candles(symbol, _query(event), now.date())
                return _success(
                    data,
                    now=now,
                    symbol=symbol,
                    source="stored",
                    as_of=as_of,
                )
            raise ApiError(404, "NOT_FOUND", "Route was not found.")
        except QuotaExceeded as exc:
            return _error(
                429,
                "RATE_LIMITED",
                str(exc),
                headers={"Retry-After": str(exc.retry_after)},
            )
        except ApiError as exc:
            return _error(exc.status, exc.code, exc.message, headers=exc.headers)

    def _consume_quota(self, auth: AuthContext, now: datetime) -> Usage | None:
        if auth.key_type == "admin":
            return None
        if auth.daily_quota is None:
            raise ApiError(500, "INTERNAL_ERROR", "Request identity has no quota.")
        return self._quota.consume(auth.key_id, auth.daily_quota, at=now)

    def _usage(self, auth: AuthContext, usage: Usage | None) -> dict[str, Any]:
        if usage is None:
            raise ApiError(400, "VALIDATION_ERROR", "Admin keys have no daily quota.")
        identity = self._identities.get_usage_identity(auth.key_id)
        return {
            **identity,
            "dailyQuota": usage.daily_quota,
            "usedToday": usage.used_today,
            "resetsAt": _timestamp(usage.resets_at),
        }

    def _health(self, now: datetime) -> dict[str, Any]:
        expected = _latest_expected_weekday(now.date())
        markets: list[dict[str, Any]] = []
        for market in self._health_markets:
            try:
                item = self._data.get_market_status(market)
                latest = item.get("latestEod") if item else None
                stale = not latest or _parse_stored_date(latest) < expected
            except Exception:
                # Health remains a liveness response when the status read is unavailable.
                latest = None
                stale = True
            markets.append({"market": market, "latestEod": latest, "stale": stale})
        return {
            "status": "degraded" if any(item["stale"] for item in markets) else "ok",
            "markets": markets,
        }

    def _symbol(self, symbol: str) -> dict[str, Any]:
        item = self._data.get_symbol(symbol)
        if not item or item.get("active") is False:
            raise ApiError(404, "SYMBOL_NOT_FOUND", "Symbol was not recognized.")

        coverage = item.get("coverage") or {
            "eodFrom": item.get("eodFrom"),
            "eodTo": item.get("eodTo"),
        }
        return {
            "symbol": symbol,
            "name": item.get("name"),
            "type": item.get("type"),
            "exchange": item.get("exchange"),
            "currency": item.get("currency"),
            "active": bool(item.get("active", True)),
            "coverage": coverage,
        }

    def _candles(
        self, symbol: str, query: Mapping[str, str], today: date
    ) -> tuple[dict[str, Any], str | None]:
        item = self._data.get_symbol(symbol)
        if not item or item.get("active") is False:
            raise ApiError(404, "SYMBOL_NOT_FOUND", "Symbol was not recognized.")

        interval = query.get("interval", "1d")
        if interval != "1d":
            raise ApiError(400, "VALIDATION_ERROR", "interval must be 1d.")
        end = _date_parameter(query.get("to"), default=today, name="to")
        start = _date_parameter(
            query.get("from"), default=_years_before(end, 1), name="from"
        )
        if start > end:
            raise ApiError(400, "VALIDATION_ERROR", "from must not be after to.")
        if start < _years_before(end, 10):
            raise ApiError(400, "RANGE_TOO_LARGE", "Candle range cannot exceed 10 years.")

        rows: list[Mapping[str, Any]] = []
        for chunk in self._data.get_candle_chunks(symbol, start, end):
            rows.extend(chunk.get("candles", []))

        actions = self._data.get_actions(symbol)
        candles = []
        for row in rows:
            candle_date = _parse_stored_date(row.get("d"))
            if not start <= candle_date <= end:
                continue
            close = _decimal(row.get("c"), "close")
            factor = Decimal("1")
            for action in actions:
                if _parse_stored_date(action.get("date")) > candle_date:
                    factor *= _decimal(action.get("factor"), "action factor")
            candles.append(
                {
                    "date": candle_date.isoformat(),
                    "open": _number(_decimal(row.get("o"), "open")),
                    "high": _number(_decimal(row.get("h"), "high")),
                    "low": _number(_decimal(row.get("l"), "low")),
                    "close": _number(close),
                    "adjclose": _number(close * factor),
                    "volume": _volume(row.get("v")),
                    "synthetic": False,
                }
            )
        candles.sort(key=lambda candle: candle["date"])
        if len(candles) > 5_000:
            raise ApiError(400, "RANGE_TOO_LARGE", "Candle response is too large.")

        as_of = f"{candles[-1]['date']}T00:00:00Z" if candles else None
        return (
            {
                "symbol": symbol,
                "interval": "1d",
                "currency": item.get("currency"),
                "candles": candles,
            },
            as_of,
        )


_service: ApiService | None = None


def handler(
    event: Mapping[str, Any],
    _context: object,
    *,
    service: ApiService | None = None,
) -> dict[str, Any]:
    """Handle one API Gateway payload-v2 request."""

    try:
        return (service or _default_service()).handle(event)
    except Exception:
        _LOGGER.exception("Public API request failed")
        return _error(500, "INTERNAL_ERROR", "An unexpected error occurred.")


lambda_handler = handler


def _default_service() -> ApiService:
    global _service
    if _service is None:
        import boto3

        dynamodb = boto3.resource("dynamodb")
        data_table = dynamodb.Table(os.environ["DATA_TABLE"])
        control_table = dynamodb.Table(os.environ["CONTROL_TABLE"])
        markets = tuple(
            market.strip().upper()
            for market in os.environ.get("HEALTH_MARKETS", "US").split(",")
            if market.strip()
        )
        _service = ApiService(
            MarketDataRepository(data_table),
            IdentityRepository(control_table),
            QuotaService(control_table),
            health_markets=markets,
        )
    return _service


def _authorizer_context(event: Mapping[str, Any]) -> AuthContext:
    try:
        return parse_authorizer_context(event)
    except ValueError as exc:
        raise ApiError(500, "INTERNAL_ERROR", "Request identity is unavailable.") from exc


def _path_symbol(event: Mapping[str, Any], path: str, prefix: str) -> str:
    parameters = event.get("pathParameters")
    raw = parameters.get("symbol") if isinstance(parameters, Mapping) else None
    raw = raw or path.removeprefix(prefix)
    try:
        symbol = canonical_symbol(str(raw))
    except ValueError:
        raise ApiError(404, "SYMBOL_NOT_FOUND", "Symbol was not recognized.")
    return symbol


def _query(event: Mapping[str, Any]) -> dict[str, str]:
    query = event.get("queryStringParameters")
    if not isinstance(query, Mapping):
        return {}
    return {str(key): str(value) for key, value in query.items() if value is not None}


def _date_parameter(value: str | None, *, default: date, name: str) -> date:
    if value is None:
        return default
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(
            400, "VALIDATION_ERROR", f"{name} must be a YYYY-MM-DD date."
        ) from exc


def _parse_stored_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("stored date is invalid")
    return date.fromisoformat(value)


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _latest_expected_weekday(today: date) -> date:
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _decimal(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"stored {field} is invalid") from exc
    if not number.is_finite():
        raise ValueError(f"stored {field} is invalid")
    return number


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _volume(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _success(
    data: Any,
    *,
    now: datetime,
    symbol: str | None = None,
    source: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "asOf": as_of or _timestamp(now),
        "disclaimer": DISCLAIMER,
    }
    if symbol is not None:
        meta["symbol"] = symbol
    if source is not None:
        meta["source"] = source
    return _response(200, {"data": data, "meta": meta})


def _error(
    status: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _response(
        status,
        {"error": {"code": code, "message": message, "details": {}}},
        headers=headers,
    )


def _response(
    status: int, body: Any, *, headers: Mapping[str, str] | None = None
) -> dict[str, Any]:
    response_headers = {"Content-Type": "application/json"}
    response_headers.update(headers or {})
    return {
        "statusCode": status,
        "headers": response_headers,
        "body": json.dumps(body, separators=(",", ":")),
    }


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)

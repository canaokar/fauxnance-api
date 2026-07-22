"""Small, dependency-free HTTP client for the Fauxnance admin API."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1"
_KEY_PATTERN = re.compile(r"fnx_[a-z][a-z0-9-]*_[A-Za-z0-9]{32}")


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[Request, float], TransportResponse]


class FnxClientError(RuntimeError):
    """A safe-to-display client or transport error."""


class FnxApiError(FnxClientError):
    """An error response returned by API Gateway or the Fauxnance API."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: str | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        suffix = f" Retry after {retry_after} seconds." if retry_after else ""
        super().__init__(f"{code} ({status}): {message}{suffix}")


class FnxApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: Transport | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base URL is required")
        if not api_key.strip():
            raise ValueError("API key is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport or urllib_transport
        self._timeout = timeout

    def create_cohort(
        self, name: str, *, default_daily_quota: int, expires_at: str
    ) -> Any:
        return self._request(
            "POST",
            "/admin/cohorts",
            {
                "name": name,
                "defaultDailyQuota": default_daily_quota,
                "expiresAt": expires_at,
            },
        )

    def list_cohorts(self) -> Any:
        return self._request("GET", "/admin/cohorts")

    def issue_keys(
        self,
        cohort_id: str,
        labels: list[str],
        *,
        daily_quota: int | None = None,
    ) -> Any:
        body: dict[str, Any] = {"cohortId": cohort_id, "labels": labels}
        if daily_quota is not None:
            body["dailyQuota"] = daily_quota
        return self._request("POST", "/admin/keys", body)

    def list_keys(self, cohort_id: str) -> Any:
        query = urlencode({"cohortId": cohort_id})
        return self._request("GET", f"/admin/keys?{query}")

    def revoke_key(self, key_id: str) -> Any:
        return self._request("DELETE", f"/admin/keys/{quote(key_id, safe='')}")

    def start_backfill(
        self,
        universe: str,
        *,
        from_date: str,
        to_date: str | None = None,
    ) -> Any:
        body = {"universe": universe, "from": from_date}
        if to_date is not None:
            body["to"] = to_date
        return self._request("POST", "/admin/ingest/backfill", body)

    def job_status(self, job_id: str) -> Any:
        return self._request(
            "GET", f"/admin/ingest/jobs/{quote(job_id, safe='')}"
        )

    def _request(self, method: str, path: str, body: Any | None = None) -> Any:
        encoded = None
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Api-Key": self._api_key,
            },
        )
        try:
            response = self._transport(request, self._timeout)
        except FnxClientError:
            raise
        except Exception:
            # Transport exceptions can contain a Request repr. Do not risk
            # printing a credential-bearing header through their messages.
            raise FnxClientError("request failed before receiving a response") from None

        payload = _json_payload(response.body)
        if not 200 <= response.status < 300:
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code") or "HTTP_ERROR")
                message = str(error.get("message") or "The request failed.")
            else:
                code = "HTTP_ERROR"
                raw_message = payload.get("message") if isinstance(payload, dict) else None
                message = str(raw_message or "The request failed.")
            raise FnxApiError(
                response.status,
                _redact(code, self._api_key),
                _redact(message, self._api_key),
                retry_after=_header(response.headers, "Retry-After"),
            )
        if not isinstance(payload, dict) or "data" not in payload:
            raise FnxClientError("API response did not contain a data envelope")
        return payload["data"]


def urllib_transport(request: Request, timeout: float) -> TransportResponse:
    """Execute one request while normalizing urllib's HTTP error behavior."""

    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return TransportResponse(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except HTTPError as exc:
        return TransportResponse(
            status=int(exc.code),
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=exc.read(),
        )
    except (URLError, TimeoutError, OSError):
        raise FnxClientError("request failed before receiving a response") from None


def _json_payload(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FnxClientError("API response was not valid JSON") from None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for candidate, value in headers.items():
        if candidate.lower() == lowered:
            return str(value)
    return None


def _redact(value: str, api_key: str) -> str:
    return _KEY_PATTERN.sub("[REDACTED]", value.replace(api_key, "[REDACTED]"))

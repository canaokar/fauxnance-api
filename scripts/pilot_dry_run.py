#!/usr/bin/env python3
"""Run the destructive Phase 4 cohort pilot against the dev HTTP API.

The command is deliberately opt-in. It creates a disposable cohort and 50
student credentials, verifies the operator workflow, then revokes every key
and inactivates the cohort even when a verification step fails.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Protocol, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://y4t9nq2bqf.execute-api.eu-west-2.amazonaws.com/v1"
KEY_COUNT = 50
DEFAULT_CHUNK_SIZE = 25
DEFAULT_MAX_SECONDS = 600.0


class PilotError(RuntimeError):
    """A pilot failure whose message is safe to show to an operator."""


class PilotRunError(PilotError):
    def __init__(self, summary: Mapping[str, Any]) -> None:
        self.summary = dict(summary)
        super().__init__("pilot dry run failed")


class PilotClient(Protocol):
    def create_cohort(
        self, name: str, *, default_daily_quota: int, expires_at: str
    ) -> Mapping[str, Any]: ...

    def issue_keys(
        self, cohort_id: str, labels: list[str]
    ) -> Mapping[str, Any]: ...

    def list_keys(self, cohort_id: str) -> Mapping[str, Any]: ...

    def smoke_usage(self, plaintext_key: str) -> Mapping[str, Any]: ...

    def revoke_key(self, key_id: str) -> Mapping[str, Any]: ...

    def inactivate_cohort(self, cohort_id: str) -> Mapping[str, Any]: ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True, slots=True)
class IssuedKey:
    key_id: str
    label: str
    plaintext: str


class HttpPilotClient:
    """Dependency-free HTTP client that never includes credentials in errors."""

    def __init__(self, base_url: str, admin_key: str, *, timeout: float = 15.0) -> None:
        if not base_url.strip():
            raise ValueError("base URL is required")
        if not admin_key.strip():
            raise ValueError("FNX_API_KEY is required")
        self._base_url = base_url.rstrip("/")
        self._admin_key = admin_key
        self._timeout = timeout

    def create_cohort(
        self, name: str, *, default_daily_quota: int, expires_at: str
    ) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/admin/cohorts",
            self._admin_key,
            {
                "name": name,
                "defaultDailyQuota": default_daily_quota,
                "expiresAt": expires_at,
            },
        )

    def issue_keys(self, cohort_id: str, labels: list[str]) -> Mapping[str, Any]:
        return self._request(
            "POST",
            "/admin/keys",
            self._admin_key,
            {"cohortId": cohort_id, "labels": labels},
        )

    def list_keys(self, cohort_id: str) -> Mapping[str, Any]:
        query_string = urlencode({"cohortId": cohort_id, "limit": KEY_COUNT})
        return self._request(
            "GET", f"/admin/keys?{query_string}", self._admin_key
        )

    def smoke_usage(self, plaintext_key: str) -> Mapping[str, Any]:
        return self._request("GET", "/usage", plaintext_key)

    def revoke_key(self, key_id: str) -> Mapping[str, Any]:
        return self._request(
            "DELETE", f"/admin/keys/{quote(key_id, safe='')}", self._admin_key
        )

    def inactivate_cohort(self, cohort_id: str) -> Mapping[str, Any]:
        return self._request(
            "PATCH",
            f"/admin/cohorts/{quote(cohort_id, safe='')}",
            self._admin_key,
            {"status": "inactive"},
        )

    def _request(
        self,
        method: str,
        path: str,
        api_key: str,
        body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        encoded = (
            json.dumps(body, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        request = Request(
            f"{self._base_url}{path}",
            data=encoded,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Api-Key": api_key,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                status = int(response.status)
                raw = response.read()
        except HTTPError as exc:
            status = int(exc.code)
            raw = exc.read()
        except (URLError, TimeoutError, OSError):
            raise PilotError("API request failed before receiving a response") from None

        if not 200 <= status < 300:
            raise PilotError(f"API request failed with HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PilotError("API response was not valid JSON") from None
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise PilotError("API response did not contain an object data envelope")
        return data


def run_pilot(
    client: PilotClient,
    output_path: Path,
    *,
    clock: Clock | None = None,
    cohort_name: str = "Fauxnance Phase 4 pilot dry run",
    label_prefix: str = "phase4-pilot",
    daily_quota: int = 100,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    smoke_count: int = 3,
) -> dict[str, Any]:
    """Exercise cohort issuance and always clean up the disposable records."""

    active_clock = clock or SystemClock()
    _validate_options(
        output_path,
        label_prefix=label_prefix,
        daily_quota=daily_quota,
        chunk_size=chunk_size,
        max_seconds=max_seconds,
        smoke_count=smoke_count,
    )
    labels = [f"{label_prefix}-{index:03d}" for index in range(1, KEY_COUNT + 1)]
    cohort_id: str | None = None
    issued: list[IssuedKey] = []
    known_key_ids: set[str] = set()
    issuance_seconds: float | None = None
    smoke_passed = 0
    primary_failed = False
    cleanup_errors = 0

    try:
        expires_at = (active_clock.now().date() + timedelta(days=7)).isoformat()
        cohort = client.create_cohort(
            cohort_name,
            default_daily_quota=daily_quota,
            expires_at=expires_at,
        )
        cohort_id = _required_string(cohort, "cohortId")

        started = active_clock.monotonic()
        for offset in range(0, KEY_COUNT, chunk_size):
            response = client.issue_keys(cohort_id, labels[offset : offset + chunk_size])
            batch = _issued_keys(response)
            issued.extend(batch)
            known_key_ids.update(item.key_id for item in batch)

        _assert_complete_issuance(issued, labels)
        listed = client.list_keys(cohort_id)
        _assert_no_secret_fields(listed)
        listed_ids = _listed_key_ids(listed)
        known_key_ids.update(listed_ids)
        if listed_ids != known_key_ids or len(listed_ids) != KEY_COUNT:
            raise PilotError("listed keys did not match the issued key set")

        issuance_seconds = active_clock.monotonic() - started
        if issuance_seconds > max_seconds:
            raise PilotError("key issuance exceeded the configured time threshold")

        _write_credentials(output_path, issued)
        for item in _sample(issued, smoke_count):
            usage = client.smoke_usage(item.plaintext)
            if not isinstance(usage.get("usedToday"), (int, float)):
                raise PilotError("authenticated usage smoke response was invalid")
            smoke_passed += 1
    except Exception:
        primary_failed = True
    finally:
        if cohort_id is not None:
            try:
                cleanup_listing = client.list_keys(cohort_id)
                known_key_ids.update(_listed_key_ids(cleanup_listing))
                try:
                    _assert_no_secret_fields(cleanup_listing)
                except Exception:
                    cleanup_errors += 1
            except Exception:
                cleanup_errors += 1
            for key_id in sorted(known_key_ids):
                try:
                    client.revoke_key(key_id)
                except Exception:
                    cleanup_errors += 1
            try:
                client.inactivate_cohort(cohort_id)
            except Exception:
                cleanup_errors += 1

    summary = {
        "success": not primary_failed and cleanup_errors == 0,
        "cohortId": cohort_id,
        "keysRequested": KEY_COUNT,
        "keysIssued": len(issued),
        "issuanceSeconds": (
            round(issuance_seconds, 3) if issuance_seconds is not None else None
        ),
        "maxIssuanceSeconds": max_seconds,
        "smokePassed": smoke_passed,
        "keysRevocationAttempted": len(known_key_ids),
        "cohortInactivationAttempted": cohort_id is not None,
        "cleanupErrors": cleanup_errors,
        "credentialFile": str(output_path) if output_path.exists() else None,
    }
    if not summary["success"]:
        raise PilotRunError(summary)
    return summary


def _validate_options(
    output_path: Path,
    *,
    label_prefix: str,
    daily_quota: int,
    chunk_size: int,
    max_seconds: float,
    smoke_count: int,
) -> None:
    if output_path.exists():
        raise PilotError("credential output already exists")
    if not label_prefix or len(label_prefix) > 80:
        raise PilotError("label prefix must contain 1 to 80 characters")
    if daily_quota < 1:
        raise PilotError("daily quota must be positive")
    if not 1 <= chunk_size <= 25:
        raise PilotError("chunk size must be from 1 to 25")
    if max_seconds <= 0:
        raise PilotError("maximum issuance time must be positive")
    if not 1 <= smoke_count <= KEY_COUNT:
        raise PilotError(f"smoke count must be from 1 to {KEY_COUNT}")


def _issued_keys(response: Mapping[str, Any]) -> list[IssuedKey]:
    raw = response.get("keys")
    if not isinstance(raw, list):
        raise PilotError("key issuance response was invalid")
    values: list[IssuedKey] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PilotError("key issuance response was invalid")
        values.append(
            IssuedKey(
                key_id=_required_string(item, "keyId"),
                label=_required_string(item, "label"),
                plaintext=_required_string(item, "key"),
            )
        )
    return values


def _assert_complete_issuance(issued: Sequence[IssuedKey], labels: list[str]) -> None:
    if len(issued) != KEY_COUNT:
        raise PilotError(f"expected exactly {KEY_COUNT} issued keys")
    if {item.label for item in issued} != set(labels):
        raise PilotError("issued labels did not match requested labels")
    if len({item.label.casefold() for item in issued}) != KEY_COUNT:
        raise PilotError("issued labels were not unique")
    if len({item.key_id for item in issued}) != KEY_COUNT:
        raise PilotError("issued key IDs were not unique")
    if len({item.plaintext for item in issued}) != KEY_COUNT:
        raise PilotError("issued plaintext keys were not unique")


def _assert_no_secret_fields(value: Any) -> None:
    forbidden = {"key", "keyhash", "hash", "plaintext", "secret", "digest"}
    if isinstance(value, Mapping):
        for name, child in value.items():
            normalized = "".join(character for character in str(name).lower() if character.isalnum())
            if normalized in forbidden:
                raise PilotError("key list response exposed credential material")
            _assert_no_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_secret_fields(child)


def _listed_key_ids(response: Mapping[str, Any]) -> set[str]:
    raw = response.get("keys")
    if not isinstance(raw, list):
        raise PilotError("key list response was invalid")
    values: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise PilotError("key list response was invalid")
        values.add(_required_string(item, "keyId"))
    return values


def _required_string(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value:
        raise PilotError(f"API response omitted {name}")
    return value


def _sample(values: Sequence[IssuedKey], count: int) -> list[IssuedKey]:
    if count == 1:
        return [values[0]]
    indexes = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)]
    return [values[index] for index in indexes]


def _write_credentials(path: Path, issued: Sequence[IssuedKey]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["label", "key"])
            for item in sorted(issued, key=lambda value: value.label):
                writer.writerow([item.label, item.plaintext])
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required acknowledgement that the command creates and revokes live keys",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--cohort-name", default="Fauxnance Phase 4 pilot dry run")
    parser.add_argument("--label-prefix", default="phase4-pilot")
    parser.add_argument("--daily-quota", type=int, default=100)
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--smoke-count", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


ClientFactory = Callable[[str, str, float], PilotClient]


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    client_factory: ClientFactory | None = None,
    clock: Clock | None = None,
) -> int:
    args = _parser().parse_args(argv)
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    environment = os.environ if environ is None else environ
    if not args.execute:
        errors.write("pilot-dry-run: --execute is required; no API calls were made\n")
        return 2
    admin_key = environment.get("FNX_API_KEY", "").strip()
    if not admin_key:
        errors.write("pilot-dry-run: FNX_API_KEY is required\n")
        return 2
    base_url = args.base_url or environment.get("FNX_BASE_URL") or DEFAULT_BASE_URL
    factory = client_factory or (
        lambda url, key, timeout: HttpPilotClient(url, key, timeout=timeout)
    )
    try:
        client = factory(base_url, admin_key, args.timeout)
        summary = run_pilot(
            client,
            args.output,
            clock=clock,
            cohort_name=args.cohort_name,
            label_prefix=args.label_prefix,
            daily_quota=args.daily_quota,
            chunk_size=args.chunk_size,
            max_seconds=args.max_seconds,
            smoke_count=args.smoke_count,
        )
    except PilotRunError as exc:
        output.write(json.dumps(exc.summary, separators=(",", ":")) + "\n")
        errors.write("pilot-dry-run: pilot failed; cleanup was attempted\n")
        return 1
    except (PilotError, ValueError, OSError):
        errors.write("pilot-dry-run: validation failed; no credentials were displayed\n")
        return 1
    output.write(json.dumps(summary, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

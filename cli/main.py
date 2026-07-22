"""Command-line entry point for Fauxnance cohort operations."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import csv
from datetime import date
import json
import os
from pathlib import Path
import sys
from typing import IO, Any

from cli.client import DEFAULT_BASE_URL, FnxApiClient, FnxClientError, Transport


_MAX_LABELS_PER_REQUEST = 25
_UNSAFE_SPREADSHEET_PREFIXES = ("=", "+", "-", "@")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fnx", description=__doc__)
    groups = parser.add_subparsers(dest="group", required=True)

    cohorts = groups.add_parser("cohorts", help="Manage cohorts")
    cohort_commands = cohorts.add_subparsers(dest="command", required=True)
    create = cohort_commands.add_parser("create", help="Create a cohort")
    create.add_argument("name")
    create.add_argument("--quota", type=_positive_int, required=True)
    create.add_argument("--expires", type=_iso_date, required=True)
    cohort_commands.add_parser("list", help="List cohorts")

    keys = groups.add_parser("keys", help="Manage student API keys")
    key_commands = keys.add_subparsers(dest="command", required=True)
    issue = key_commands.add_parser("issue", help="Issue keys from a label file")
    issue.add_argument("--cohort", required=True)
    issue.add_argument("--labels-file", type=Path, required=True)
    issue.add_argument("--quota", type=_positive_int)
    issue.add_argument("--output", type=Path)
    list_keys = key_commands.add_parser("list", help="List cohort keys")
    list_keys.add_argument("--cohort", required=True)
    revoke = key_commands.add_parser("revoke", help="Revoke a key")
    revoke.add_argument("key_id")

    backfill = groups.add_parser("backfill", help="Start a historical backfill")
    backfill.add_argument("--universe", required=True)
    backfill.add_argument("--from", dest="from_date", type=_iso_date, required=True)
    backfill.add_argument("--to", dest="to_date", type=_iso_date, required=True)

    jobs = groups.add_parser("jobs", help="Inspect ingest jobs")
    job_commands = jobs.add_subparsers(dest="command", required=True)
    status = job_commands.add_parser("status", help="Get a job's status")
    status.add_argument("job_id")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    transport: Transport | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    api_key = env.get("FNX_API_KEY", "").strip()
    if not api_key:
        print("fnx: FNX_API_KEY is required", file=err)
        return 2
    base_url = env.get("FNX_BASE_URL", DEFAULT_BASE_URL).strip()
    if not base_url:
        print("fnx: FNX_BASE_URL must not be empty", file=err)
        return 2

    client = FnxApiClient(base_url, api_key, transport=transport)
    try:
        return _dispatch(args, client, out, err)
    except (FnxClientError, OSError, ValueError) as exc:
        print(f"fnx: {exc}", file=err)
        return 1


def _dispatch(
    args: argparse.Namespace,
    client: FnxApiClient,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    if args.group == "cohorts" and args.command == "create":
        data = client.create_cohort(
            args.name,
            default_daily_quota=args.quota,
            expires_at=args.expires,
        )
        _print_json(data, stdout)
        return 0
    if args.group == "cohorts" and args.command == "list":
        _print_json(client.list_cohorts(), stdout)
        return 0
    if args.group == "keys" and args.command == "issue":
        labels = _read_labels(args.labels_file)
        return _issue_keys(args, labels, client, stdout, stderr)
    if args.group == "keys" and args.command == "list":
        _print_json(client.list_keys(args.cohort), stdout)
        return 0
    if args.group == "keys" and args.command == "revoke":
        _print_json(client.revoke_key(args.key_id), stdout)
        return 0
    if args.group == "backfill":
        if args.from_date > args.to_date:
            raise ValueError("--from must not be after --to")
        _print_json(
            client.start_backfill(
                args.universe,
                from_date=args.from_date,
                to_date=args.to_date,
            ),
            stdout,
        )
        return 0
    if args.group == "jobs" and args.command == "status":
        _print_json(client.job_status(args.job_id), stdout)
        return 0
    raise ValueError("unsupported command")


def _issue_keys(
    args: argparse.Namespace,
    labels: list[str],
    client: FnxApiClient,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    output: IO[str]
    close_output = False
    if args.output is None:
        output = stdout
    else:
        descriptor = os.open(
            args.output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        output = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        close_output = True

    try:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["label", "key"])
        output.flush()
        issued_count = 0
        for offset in range(0, len(labels), _MAX_LABELS_PER_REQUEST):
            chunk = labels[offset : offset + _MAX_LABELS_PER_REQUEST]
            data = client.issue_keys(args.cohort, chunk, daily_quota=args.quota)
            issued = _issued_credentials(data, chunk)
            for label in chunk:
                writer.writerow([label, issued[label]])
                issued_count += 1
            output.flush()
    finally:
        if close_output:
            output.close()
    print(f"Issued {issued_count} key(s).", file=stderr)
    return 0


def _issued_credentials(data: Any, expected_labels: list[str]) -> dict[str, str]:
    values = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(values, list):
        raise FnxClientError("key issuance response did not contain keys")
    credentials: dict[str, str] = {}
    for value in values:
        if not isinstance(value, dict):
            raise FnxClientError("key issuance response contained an invalid entry")
        label = value.get("label")
        key = value.get("key")
        if not isinstance(label, str) or not isinstance(key, str):
            raise FnxClientError("key issuance response contained an invalid entry")
        if label in credentials:
            raise FnxClientError("key issuance response contained duplicate labels")
        credentials[label] = key
    if set(credentials) != set(expected_labels):
        raise FnxClientError("key issuance response did not match requested labels")
    return credentials


def _read_labels(path: Path) -> list[str]:
    labels = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    if not labels or any(not label for label in labels):
        raise ValueError("labels file must contain one non-empty label per line")
    seen: set[str] = set()
    for label in labels:
        if len(label) > 120:
            raise ValueError("labels must be at most 120 characters")
        if label.startswith(_UNSAFE_SPREADSHEET_PREFIXES):
            raise ValueError("labels must not begin with =, +, -, or @")
        if any(ord(character) < 32 for character in label):
            raise ValueError("labels must not contain control characters")
        if label in seen:
            raise ValueError(f"duplicate label: {label}")
        seen.add(label)
    return labels


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("must be a YYYY-MM-DD date") from None


def _print_json(value: Any, stream: IO[str]) -> None:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

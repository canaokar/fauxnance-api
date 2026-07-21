#!/usr/bin/env python3
"""Bootstrap the first admin key and, optionally, a Phase 1 dev student key.

This is intentionally the only operator utility that writes authentication
records directly to AWS. Plaintext keys are returned to the caller once and are
never persisted.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import re
import secrets
import string
from typing import Any, Callable, Sequence
import uuid


BASE62 = string.ascii_letters + string.digits
_STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    key_id: str
    plaintext: str
    label: str
    key_type: str


def generate_plaintext_key(
    stage: str, *, chooser: Callable[[str], str] = secrets.choice
) -> str:
    if not _STAGE_PATTERN.fullmatch(stage):
        raise ValueError("stage must contain lowercase letters, numbers, or '-'")
    token = "".join(chooser(BASE62) for _ in range(32))
    return f"fnx_{stage}_{token}"


def bootstrap_admin(
    dynamodb: Any,
    ssm: Any,
    *,
    table_name: str,
    parameter_name: str,
    stage: str,
    label: str,
    now: datetime | None = None,
    key_id: str | None = None,
    plaintext: str | None = None,
) -> IssuedCredential:
    """Create an admin key pair in DynamoDB and add its ID to SSM."""

    timestamp = _utc_timestamp(now)
    credential = _credential(
        stage=stage,
        label=label,
        key_type="admin",
        key_id=key_id,
        plaintext=plaintext,
    )
    digest = _hash_key(credential.plaintext)
    dynamodb.transact_write_items(
        TransactItems=[
            _put(
                table_name,
                {
                    "PK": _s(f"KEY#{digest}"),
                    "SK": _s("META"),
                    "keyId": _s(credential.key_id),
                    "label": _s(label),
                    "type": _s("admin"),
                    "status": _s("active"),
                    "createdAt": _s(timestamp),
                },
            ),
            _put(
                table_name,
                {
                    "PK": _s(f"KEYID#{credential.key_id}"),
                    "SK": _s("META"),
                    "keyHash": _s(digest),
                    "createdAt": _s(timestamp),
                },
            ),
        ]
    )
    _add_admin_to_allowlist(ssm, parameter_name, credential.key_id)
    return credential


def seed_dev_student(
    dynamodb: Any,
    *,
    table_name: str,
    stage: str,
    label: str,
    daily_quota: int = 2_000,
    expires_at: date,
    now: datetime | None = None,
    cohort_id: str | None = None,
    key_id: str | None = None,
    plaintext: str | None = None,
) -> IssuedCredential:
    """Create the disposable Phase 1 cohort and its first student atomically."""

    if daily_quota < 1:
        raise ValueError("daily_quota must be positive")
    timestamp = _utc_timestamp(now)
    cohort_id = cohort_id or f"cohort_{uuid.uuid4().hex[:16]}"
    credential = _credential(
        stage=stage,
        label=label,
        key_type="student",
        key_id=key_id,
        plaintext=plaintext,
    )
    digest = _hash_key(credential.plaintext)
    cohort_pk = f"COHORT#{cohort_id}"
    common_key_fields = {
        "keyId": _s(credential.key_id),
        "label": _s(label),
        "cohortId": _s(cohort_id),
        "dailyQuota": _n(daily_quota),
        "status": _s("active"),
        "createdAt": _s(timestamp),
        "expiresAt": _s(expires_at.isoformat()),
    }
    dynamodb.transact_write_items(
        TransactItems=[
            _put(
                table_name,
                {
                    "PK": _s(cohort_pk),
                    "SK": _s("META"),
                    "name": _s("Phase 1 dev cohort"),
                    "defaultDailyQuota": _n(daily_quota),
                    "expiresAt": _s(expires_at.isoformat()),
                    "status": _s("active"),
                    "createdAt": _s(timestamp),
                },
            ),
            _put(
                table_name,
                {
                    "PK": _s(f"KEY#{digest}"),
                    "SK": _s("META"),
                    "type": _s("student"),
                    **common_key_fields,
                },
            ),
            _put(
                table_name,
                {
                    "PK": _s(f"KEYID#{credential.key_id}"),
                    "SK": _s("META"),
                    "keyHash": _s(digest),
                    "cohortId": _s(cohort_id),
                    "createdAt": _s(timestamp),
                },
            ),
            _put(
                table_name,
                {
                    "PK": _s(cohort_pk),
                    "SK": _s(f"KEY#{credential.key_id}"),
                    **common_key_fields,
                },
            ),
        ]
    )
    return credential


def _credential(
    *,
    stage: str,
    label: str,
    key_type: str,
    key_id: str | None,
    plaintext: str | None,
) -> IssuedCredential:
    prefix = "admin" if key_type == "admin" else "student"
    return IssuedCredential(
        key_id=key_id or f"{prefix}_{uuid.uuid4().hex[:16]}",
        plaintext=plaintext or generate_plaintext_key(stage),
        label=label,
        key_type=key_type,
    )


def _put(table_name: str, item: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(PK)",
        }
    }


def _add_admin_to_allowlist(ssm: Any, parameter_name: str, key_id: str) -> None:
    response = ssm.get_parameter(Name=parameter_name)
    raw = response["Parameter"]["Value"]
    existing = {
        entry.strip()
        for entry in raw.split(",")
        if entry.strip() and entry.strip() != "REPLACE_ME"
    }
    existing.add(key_id)
    ssm.put_parameter(
        Name=parameter_name,
        Type="String",
        Value=",".join(sorted(existing)),
        Overwrite=True,
    )


def _utc_timestamp(now: datetime | None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default="dev")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--profile", default="megh.io")
    parser.add_argument("--control-table")
    parser.add_argument("--admin-allowlist-parameter")
    parser.add_argument("--label", default="bootstrap-admin")
    parser.add_argument("--seed-dev-student", action="store_true")
    parser.add_argument("--student-label", default="phase1-student")
    parser.add_argument("--student-quota", type=int, default=2_000)
    parser.add_argument(
        "--student-expires-at",
        type=date.fromisoformat,
        help="Cohort expiry date (default: 365 days from today)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - operator environment guard
        raise SystemExit("boto3 is required; install requirements-dev.txt") from exc

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    dynamodb = session.client("dynamodb")
    ssm = session.client("ssm")
    table_name = args.control_table or f"fauxnance-{args.stage}-control"
    parameter_name = (
        args.admin_allowlist_parameter
        or f"/fauxnance/{args.stage}/admin/key_ids"
    )

    admin = bootstrap_admin(
        dynamodb,
        ssm,
        table_name=table_name,
        parameter_name=parameter_name,
        stage=args.stage,
        label=args.label,
    )
    print(f"Admin key ID: {admin.key_id}")
    print(f"Admin API key (shown once): {admin.plaintext}")

    if args.seed_dev_student:
        expiry = args.student_expires_at or (datetime.now(UTC).date() + timedelta(days=365))
        student = seed_dev_student(
            dynamodb,
            table_name=table_name,
            stage=args.stage,
            label=args.student_label,
            daily_quota=args.student_quota,
            expires_at=expiry,
        )
        print(f"Student key ID: {student.key_id}")
        print(f"Student API key (shown once): {student.plaintext}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

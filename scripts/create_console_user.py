#!/usr/bin/env python3
"""Create the first instructor-console login.

The console API has no way to bootstrap its own first admin account, so this
is an operator utility that writes a console user record directly to AWS.
The password is never accepted as a command-line argument: an argv password
would land in shell history and be visible to any other process on the host
via `ps`. Set FAUXNANCE_CONSOLE_PASSWORD in the environment instead, or leave
it unset to have a strong random password generated and printed once.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import os
import secrets
import string
import sys
from typing import Any, Sequence
import uuid

from src.admin.repository import RepositoryConflict
from src.console.passwords import hash_password, validate_password_strength
from src.console.repository import DynamoConsoleRepository


_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
_GENERATED_PASSWORD_LENGTH = 24


class InvalidEmail(ValueError):
    """Raised when an email does not have the exact shape ConsoleService requires."""


def validate_email(email: str) -> str:
    """Apply the same email-shape rule as ConsoleService, so accounts created here can log in."""

    normalized = email.strip().lower()
    if not 1 <= len(normalized) <= 254 or normalized.count("@") != 1:
        raise InvalidEmail(f"{email!r} is not a valid email address.")
    local, _, domain = normalized.partition("@")
    if not local or not domain:
        raise InvalidEmail(f"{email!r} is not a valid email address.")
    return normalized


def generate_password(
    *, chooser: Any = secrets.choice, length: int = _GENERATED_PASSWORD_LENGTH
) -> str:
    return "".join(chooser(_PASSWORD_ALPHABET) for _ in range(length))


def create_console_user(
    repository: DynamoConsoleRepository,
    *,
    email: str,
    name: str,
    role: str,
    password: str,
    now: datetime | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Validate, hash, and persist a new console user. Raises RepositoryConflict on duplicate email."""

    validate_password_strength(password)
    normalized_email = validate_email(email)
    resolved_user_id = user_id or f"user_{uuid.uuid4().hex[:16]}"
    timestamp = _utc_timestamp(now or datetime.now(UTC))
    item = {
        "PK": f"USER#{resolved_user_id}",
        "SK": "META",
        "userId": resolved_user_id,
        "email": normalized_email,
        "name": name,
        "passwordHash": hash_password(password),
        "role": role,
        "status": "active",
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }
    repository.create_user(item)
    return item


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Password handling:\n"
            "  Set FAUXNANCE_CONSOLE_PASSWORD in the environment to choose the password.\n"
            "  Leave it unset to have a strong random password generated and printed once.\n"
            "  There is intentionally no --password flag: a command-line argument would be\n"
            "  recorded in shell history and visible to other processes via `ps`.\n"
        ),
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--role", choices=["admin", "instructor"], default="admin"
    )
    parser.add_argument("--stage", default="dev")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--profile", default="megh.io")
    parser.add_argument("--table")
    return parser


def _run(
    args: argparse.Namespace,
    repository: DynamoConsoleRepository,
    *,
    password: str,
    generated: bool,
) -> int:
    try:
        item = create_console_user(
            repository,
            email=args.email,
            name=args.name,
            role=args.role,
            password=password,
        )
    except RepositoryConflict:
        print(f"A console user with email {args.email.strip().lower()!r} already exists.", file=sys.stderr)
        return 1
    except InvalidEmail as exc:
        print(f"Invalid email: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Invalid password: {exc}", file=sys.stderr)
        return 1

    print(f"User ID: {item['userId']}")
    print(f"Email: {item['email']}")
    print(f"Role: {item['role']}")
    if generated:
        print(f"Password (shown once, store this now): {password}")
        print("This password will not be shown again.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - operator environment guard
        raise SystemExit("boto3 is required; install requirements-dev.txt") from exc

    env_password = os.environ.get("FAUXNANCE_CONSOLE_PASSWORD")
    generated = env_password is None
    password = env_password or generate_password()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    dynamodb = session.resource("dynamodb")
    table_name = args.table or f"fauxnance-{args.stage}-control"
    table = dynamodb.Table(table_name)
    repository = DynamoConsoleRepository(table, dynamodb)

    return _run(args, repository, password=password, generated=generated)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

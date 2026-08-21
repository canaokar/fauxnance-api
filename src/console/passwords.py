"""PBKDF2-HMAC-SHA256 password hashing for console user accounts."""

from __future__ import annotations

from base64 import b64decode, b64encode
import hashlib
import hmac
import secrets


_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 210_000
_SALT_BYTES = 16
_MIN_ITERATIONS = 1
_MAX_ITERATIONS = 10_000_000
_MIN_LENGTH = 12
_MAX_LENGTH = 200


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join(
        [
            _ALGORITHM,
            str(_ITERATIONS),
            b64encode(salt).decode("ascii"),
            b64encode(derived).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, derived_b64 = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations_int = int(iterations)
        if not _MIN_ITERATIONS <= iterations_int <= _MAX_ITERATIONS:
            return False
        salt = b64decode(salt_b64, validate=True)
        expected = b64decode(derived_b64, validate=True)
    except Exception:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations_int
    )
    return hmac.compare_digest(candidate, expected)


def validate_password_strength(password: str) -> None:
    if len(password) < _MIN_LENGTH or len(password) > _MAX_LENGTH:
        raise ValueError(
            f"password must be between {_MIN_LENGTH} and {_MAX_LENGTH} characters"
        )

"""Shared test helper: make fake DynamoDB tables round-trip numbers faithfully.

boto3's DynamoDB resource always returns stored numbers as ``decimal.Decimal``,
never a plain ``int`` or ``float``. A fake table that hands back whatever
Python value it was given (typically a plain ``int``) hides any production
bug that assumes ``isinstance(value, int)`` or otherwise fails on ``Decimal``.

Call ``dynamo_roundtrip`` on an item (or a container of items) wherever a fake
table returns data, so fakes are faithful to what real DynamoDB reads produce.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def dynamo_roundtrip(value: Any) -> Any:
    """Recursively convert ``int``/``float`` to ``Decimal``, as boto3 does on read."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: dynamo_roundtrip(item) for key, item in value.items()}
    if isinstance(value, list):
        return [dynamo_roundtrip(item) for item in value]
    if isinstance(value, tuple):
        return tuple(dynamo_roundtrip(item) for item in value)
    return value

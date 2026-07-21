"""Intrinsic checks applied before an upstream candle is persisted."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterable

from src.shared.market_data import Candle


class InvalidCandle(ValueError):
    """Raised when an upstream candle violates the stored-data contract."""


def validate_candles(
    candles: Iterable[Candle], *, start: date, end: date
) -> list[Candle]:
    validated: list[Candle] = []
    seen: set[date] = set()
    for candle in candles:
        prices = (candle.open, candle.high, candle.low, candle.close)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in prices):
            raise InvalidCandle("prices must be finite decimals")
        if candle.high <= 0 or candle.low <= 0:
            raise InvalidCandle("prices must be positive")
        if not candle.low <= candle.open <= candle.high:
            raise InvalidCandle("open must be between low and high")
        if not candle.low <= candle.close <= candle.high:
            raise InvalidCandle("close must be between low and high")
        if candle.volume is not None and candle.volume < 0:
            raise InvalidCandle("volume must be non-negative or null")
        if not isinstance(candle.source, str) or not candle.source:
            raise InvalidCandle("source must not be empty")
        if not start <= candle.date <= end:
            raise InvalidCandle("candle date is outside the requested range")
        if candle.date in seen:
            raise InvalidCandle("candle dates must be unique")
        seen.add(candle.date)
        validated.append(candle)
    validated.sort(key=lambda candle: candle.date)
    return validated

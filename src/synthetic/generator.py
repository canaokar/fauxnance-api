"""Deterministic anchored synthetic candles and quotes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import hashlib
import math
from typing import Iterable, Sequence

from src.shared.market_data import Candle, Quote


_DEFAULT_SIGMA = {
    "equity": 0.25,
    "etf": 0.25,
    "fx": 0.10,
    "crypto": 0.70,
}
_MU = 0.05


def fill_candle_gaps(
    symbol: str,
    asset_type: str,
    start: date,
    end: date,
    real_candles: Sequence[Candle],
    *,
    history: Sequence[Candle] = (),
) -> list[Candle]:
    """Return real candles plus forward-only deterministic gap fills."""

    real_by_day = {candle.date: candle for candle in real_candles}
    calibration = list(history) or sorted(real_candles, key=lambda candle: candle.date)
    sigma = _annualized_volatility(calibration, asset_type)
    result: list[Candle] = []
    prior = [candle for candle in history if candle.date < start]
    anchor: Decimal | None = prior[-1].close if prior else None
    day = start
    while day <= end:
        real = real_by_day.get(day)
        if real is not None:
            result.append(real)
            anchor = real.close
        elif _eligible(day, asset_type) and anchor is not None:
            generated = _synthetic_candle(symbol, day, anchor, sigma)
            result.append(generated)
            anchor = generated.close
        day += timedelta(days=1)
    return result


def synthetic_quote(
    symbol: str,
    asset_type: str,
    currency: str | None,
    anchor: Candle,
    *,
    now: datetime,
    history: Sequence[Candle] = (),
) -> Quote:
    now = _as_utc(now)
    bucket_minute = now.minute - (now.minute % 5)
    bucket = now.replace(minute=bucket_minute, second=0, microsecond=0)
    sigma = _annualized_volatility(history or (anchor,), asset_type)
    periods = 365 * 24 * 12 if asset_type == "crypto" else 252 * 24 * 12
    shock = _normal(f"quote|{symbol}|{bucket.isoformat()}")
    move = math.exp((_MU - sigma * sigma / 2) / periods + sigma * shock / math.sqrt(periods))
    price = _price(float(anchor.close) * move)
    change = price - anchor.close
    percent = change / anchor.close * Decimal("100")
    return Quote(
        price=price,
        currency=currency,
        change=change,
        change_percent=percent,
        previous_close=anchor.close,
        as_of=bucket,
        market_state="unknown",
        source="synthetic",
    )


def _synthetic_candle(
    symbol: str, day: date, previous_close: Decimal, sigma: float
) -> Candle:
    periods = 365
    close_shock = _normal(f"candle-close|{symbol}|{day.isoformat()}")
    close_move = math.exp(
        (_MU - sigma * sigma / 2) / periods + sigma * close_shock / math.sqrt(periods)
    )
    close = _price(float(previous_close) * close_move)
    open_move = math.exp(
        sigma * _normal(f"candle-open|{symbol}|{day.isoformat()}") / math.sqrt(periods * 4)
    )
    open_ = _price(float(previous_close) * open_move)
    spread = abs(_normal(f"candle-range|{symbol}|{day.isoformat()}"))
    spread = min(0.20, 0.0025 + spread * sigma / math.sqrt(periods * 4))
    high = _price(max(float(open_), float(close)) * (1 + spread))
    low = _price(min(float(open_), float(close)) * (1 - spread))
    if low <= 0:
        low = min(open_, close)
    return Candle(
        date=day,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=None,
        source="synthetic",
    )


def _annualized_volatility(candles: Iterable[Candle], asset_type: str) -> float:
    closes = [float(candle.close) for candle in sorted(candles, key=lambda row: row.date)][-91:]
    returns = []
    for previous, current in zip(closes, closes[1:]):
        if previous <= 0 or current <= 0:
            continue
        value = math.log(current / previous)
        # Ignore split-sized discontinuities when calibration receives raw closes.
        if abs(value) <= 0.50:
            returns.append(value)
    if len(returns) < 20:
        return _DEFAULT_SIGMA.get(asset_type, 0.25)
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    periods = 365 if asset_type == "crypto" else 252
    return max(0.01, min(2.0, math.sqrt(variance * periods)))


def _normal(seed: str) -> float:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    first = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)
    second = (int.from_bytes(digest[8:16], "big") + 1) / (2**64 + 1)
    return math.sqrt(-2 * math.log(first)) * math.cos(2 * math.pi * second)


def _eligible(day: date, asset_type: str) -> bool:
    return asset_type == "crypto" or day.weekday() < 5


def _price(value: float) -> Decimal:
    return Decimal(str(round(value, 8)))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)

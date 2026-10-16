"""Versioned, lossless decimal serialization of the exact Strategy v1 input.

v1: UTF-8 compact JSON array [schema, [[timeframe, rows], ...]]. Timeframes
are 15m, 1h, 4h; rows are chronological arrays of time, open, high, low,
close, volume, close_time. Times are integer UTC milliseconds; numeric values
are finite, plain decimal strings without insignificant zeros (negative zero
is zero). No rounding, locale, binary-float formatting or wall clock is used.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import subprocess


INPUT_SCHEMA = "krypta-ohlcv-decimal-v1"
TIMEFRAMES = ("15m", "1h", "4h")
NUMERIC_FIELDS = ("open", "high", "low", "close", "volume")
CANDLE_FIELDS = ("time", *NUMERIC_FIELDS, "close_time")


def decimal_text(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not an OHLCV number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid OHLCV number") from exc
    if not number.is_finite():
        raise ValueError("OHLCV numbers must be finite")
    if not number:
        return "0"
    # Decimal.normalize() can round using the ambient Decimal context.
    plain = format(number, "f")
    return plain.rstrip("0").rstrip(".") if "." in plain else plain


def numeric_values(candle):
    return {field: decimal_text(candle[field]) for field in NUMERIC_FIELDS}


def input_hash(candles):
    frames = []
    if set(candles) != set(TIMEFRAMES):
        raise ValueError("Provide exactly 15m, 1h and 4h inputs")
    for interval in TIMEFRAMES:
        rows = candles[interval]
        times = [row["time"] for row in rows]
        if any(a >= b for a, b in zip(times, times[1:])):
            raise ValueError("Hash input must be strictly chronological")
        encoded = []
        for row in rows:
            if any(type(row[key]) is not int for key in ("time", "close_time")):
                raise ValueError("Candle times must be integer UTC milliseconds")
            encoded.append([row["time"], *[decimal_text(row[f]) for f in NUMERIC_FIELDS],
                            row["close_time"]])
        frames.append([interval, encoded])
    payload = json.dumps([INPUT_SCHEMA, frames], ensure_ascii=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def strategy_provenance(strategy):
    """Render supplies its deployed SHA; local executions also report dirtiness."""
    sha = os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT")
    dirty = None
    if not sha:
        root = Path(__file__).resolve().parent.parent
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=3,
                stderr=subprocess.DEVNULL,
            ).strip()
            dirty = bool(subprocess.check_output(
                ["git", "status", "--porcelain", "--", "app"], cwd=root,
                text=True, timeout=3, stderr=subprocess.DEVNULL,
            ).strip())
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("Cannot identify strategy code commit") from exc
    if len(sha) != 40 or any(c not in "0123456789abcdefABCDEF" for c in sha):
        raise ValueError("Strategy code commit must be a full Git SHA")
    return dict(
        mode="LIVE", git_commit_sha=sha.lower(), working_tree_dirty=dirty,
        strategy_id=strategy.STRATEGY_ID, strategy_version=strategy.STRATEGY_VERSION,
        input_schema=INPUT_SCHEMA, hash_algorithm="SHA-256",
        parameters=dict(window_bars=dict(strategy.WINDOW_BARS),
                        trend_threshold=strategy.TREND_THRESHOLD,
                        momentum_threshold=strategy.MOMENTUM_THRESHOLD,
                        ema_periods=[20, 50], ema_seed="SMA",
                        atr_period=14, atr_method="mean_true_range",
                        momentum_lag=4, timezone="UTC"),
    )

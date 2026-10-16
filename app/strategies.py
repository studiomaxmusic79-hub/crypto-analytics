"""Pure hourly strategy calculations; no I/O, LIVE issuance or backfilling.

Input rows use Store.get_candles() dictionaries. ``confirmed_at`` is Binance
time before a fresh successful collection, recorded after saving its response.
The caller must supply that confirmation; passing elapsed wall time is not proof
that a previously forming candle has been updated.

All times are UTC milliseconds. input_close_at is the closing boundary
(Binance close_time + 1), and horizons end at hourly closing boundaries.
"""
import math
from decimal import Decimal

from .analytics import ema


HOUR_MS = 3_600_000
MIN_BARS = 500
MOMENTUM_THRESHOLD_PCT = 0.20
STRATEGY_A = "trend_breakout"
STRATEGY_B = "trend_momentum"
VERSION = "v1"
DIRECTIONS = ("UP", "DOWN", "WAIT")
CONTEXT_FIELDS = ("input_close_at", "base_price", "due_at", "thresholds_pct")


class _DataUnavailable(ValueError):
    pass


def _finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _timestamp(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _change_pct(price, base):
    # Decimal price strings preserve exact percentage boundaries (e.g. 30.06/30)
    # without introducing an extra tolerance or a rounded trading threshold.
    return float(100 * (Decimal(str(price)) / Decimal(str(base)) - 1))


def _candle(row):
    start, end = row["time"], row["close_time"]
    if (
        not _timestamp(start) or not _timestamp(end)
        or start % HOUR_MS or end != start + HOUR_MS - 1
    ):
        raise _DataUnavailable("INVALID_HOURLY_CANDLE")
    values = tuple(float(row[k]) for k in ("open", "high", "low", "close", "volume"))
    o, h, low, c, volume = values
    if (
        not all(math.isfinite(v) for v in values)
        or min(o, h, low, c) <= 0 or volume < 0
        or h < max(o, low, c) or low > min(o, h, c)
    ):
        raise _DataUnavailable("INVALID_OHLCV")
    return (start, o, h, low, c, volume, end)


def evaluation_thresholds(base_price, atr14):
    """Return frozen percentage thresholds for the 1h and 4h evaluations."""
    if not _finite(base_price) or base_price <= 0 or not _finite(atr14) or atr14 < 0:
        raise ValueError("base_price must be positive and atr14 nonnegative")
    atr_pct = 100 * (atr14 / base_price)
    if not math.isfinite(atr_pct):
        raise ValueError("Non-finite ATR percentage")
    return {1: max(0.10, 0.25 * atr_pct), 4: max(0.10, 0.50 * atr_pct)}


def _prepare(candles, now, confirmed_at):
    if not _timestamp(now):
        raise _DataUnavailable("INVALID_TIME")
    closed = []
    for row in candles:
        end = row["close_time"]
        if not _timestamp(end):
            raise _DataUnavailable("INVALID_HOURLY_CANDLE")
        if end < now:
            closed.append(row)
    if len(closed) < MIN_BARS:
        raise _DataUnavailable("INSUFFICIENT_HISTORY")
    window = tuple(_candle(row) for row in closed[-MIN_BARS:])
    if any(b[0] - a[0] != HOUR_MS for a, b in zip(window, window[1:])):
        raise _DataUnavailable("NON_CONTIGUOUS_HISTORY")
    input_close_at = window[-1][6] + 1
    if input_close_at != now // HOUR_MS * HOUR_MS:
        raise _DataUnavailable("STALE_LAST_CANDLE")
    if (
        not _timestamp(confirmed_at)
        or not input_close_at <= confirmed_at <= now
    ):
        raise _DataUnavailable("CLOSE_NOT_CONFIRMED")

    closes = [c[4] for c in window]
    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    atr14 = sum(
        max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4]))
        for a, b in zip(window[-15:-1], window[-14:])
    ) / 14
    if not all(math.isfinite(v) for v in (ema20, ema50, atr14)):
        raise _DataUnavailable("NON_FINITE_INDICATORS")
    context = {
        "input_close_at": input_close_at,
        "base_price": closes[-1],
        "due_at": {h: input_close_at + h * HOUR_MS for h in (1, 4)},
        "thresholds_pct": evaluation_thresholds(closes[-1], atr14),
    }
    return window, context, (ema20, ema50, atr14)


def _result(strategy_id, context=None, *, status="OK", direction=None,
            score=None, reasons=(), indicators=None):
    return {
        "strategy_id": strategy_id,
        "strategy_version": VERSION,
        "status": status,
        "direction": direction,
        "score": score,
        "reason_codes": list(reasons),
        "indicators": dict(indicators or {}),
        **(context or {"input_close_at": None, "base_price": None,
                       "due_at": {}, "thresholds_pct": {}}),
    }


def _breakout(window, context, features):
    ema20, ema50, atr14 = features
    previous = window[-21:-1]
    previous_high = max(c[2] for c in previous)
    previous_low = min(c[3] for c in previous)
    indicators = {"ema20": ema20, "ema50": ema50, "atr14": atr14,
                  "previous_high20": previous_high, "previous_low20": previous_low}
    if atr14 == 0:
        return _result(STRATEGY_A, context, status="SKIPPED",
                       reasons=("ZERO_ATR",), indicators=indicators)
    d = (ema20 - ema50) / atr14
    if not math.isfinite(d):
        return _result(STRATEGY_A, context, status="SKIPPED",
                       reasons=("NON_FINITE_SCORE",), indicators=indicators)
    trend = 1 if d >= 0.25 else -1 if d <= -0.25 else 0
    close = window[-1][4]
    breakout = 1 if close > previous_high else -1 if close < previous_low else 0
    direction = "UP" if trend == breakout == 1 else "DOWN" if trend == breakout == -1 else "WAIT"
    indicators.update(d=d, trend=trend, breakout=breakout)
    return _result(
        STRATEGY_A, context, direction=direction, score=d,
        reasons=({1: "TREND_UP", -1: "TREND_DOWN", 0: "TREND_NEUTRAL"}[trend],
                 {1: "BREAKOUT_UP", -1: "BREAKOUT_DOWN", 0: "NO_BREAKOUT"}[breakout]),
        indicators=indicators,
    )


def _momentum(window, context, features):
    ema20, ema50, _ = features
    m = _change_pct(window[-1][4], window[-5][4])
    indicators = {"ema20": ema20, "ema50": ema50, "momentum_pct": m}
    if not math.isfinite(m):
        return _result(STRATEGY_B, context, status="SKIPPED",
                       reasons=("NON_FINITE_SCORE",),
                       indicators={"ema20": ema20, "ema50": ema50})
    trend = 1 if ema20 > ema50 else -1 if ema20 < ema50 else 0
    momentum = 1 if m >= MOMENTUM_THRESHOLD_PCT else -1 if m <= -MOMENTUM_THRESHOLD_PCT else 0
    direction = "UP" if trend == momentum == 1 else "DOWN" if trend == momentum == -1 else "WAIT"
    return _result(
        STRATEGY_B, context, direction=direction, score=m,
        reasons=({1: "EMA_UP", -1: "EMA_DOWN", 0: "EMA_EQUAL"}[trend],
                 {1: "MOMENTUM_UP", -1: "MOMENTUM_DOWN", 0: "MOMENTUM_NEUTRAL"}[momentum]),
        indicators=indicators,
    )


def _prepared_or_error(candles, now, confirmed_at):
    try:
        return _prepare(candles, now, confirmed_at), None
    except _DataUnavailable as exc:
        return None, str(exc)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "INVALID_DATA"


def analyze_a(candles, *, now, confirmed_at):
    """Trend Breakout v1 using the latest 500 confirmed closed 1h candles."""
    prepared, error = _prepared_or_error(candles, now, confirmed_at)
    return (_result(STRATEGY_A, status="DATA_UNAVAILABLE", reasons=(error,))
            if error else _breakout(*prepared))


def analyze_b(candles, *, now, confirmed_at):
    """Trend Momentum v1: EMA20/EMA50 and the fixed +/-0.20% four-hour momentum."""
    prepared, error = _prepared_or_error(candles, now, confirmed_at)
    return (_result(STRATEGY_B, status="DATA_UNAVAILABLE", reasons=(error,))
            if error else _momentum(*prepared))


def consensus(results):
    """Strict majority among valid votes; agreement is not success probability."""
    results = list(results)
    identities = [r["strategy_id"] for r in results]
    if len(set(identities)) != len(identities) or "consensus" in identities:
        raise ValueError("Consensus requires distinct strategy votes")
    valid = [r for r in results if r.get("status") == "OK"
             and r.get("direction") in DIRECTIONS and _finite(r.get("score"))]
    context = {k: valid[0][k] for k in CONTEXT_FIELDS} if valid else None
    if any(any(r[k] != context[k] for k in CONTEXT_FIELDS) for r in valid):
        raise ValueError("Strategy votes must use the same hourly forecast context")
    votes = {d: sum(r["direction"] == d for r in valid) for d in DIRECTIONS}
    n = len(valid)
    indicators = {"votes": votes, "valid_count": n,
                  "skipped_count": len(results) - n, "total_count": len(results),
                  "agreement_pct": round(100 * max(votes.values()) / n) if n else None,
                  "participants": [{"strategy_id": r["strategy_id"],
                                    "strategy_version": r["strategy_version"],
                                    "status": r["status"]} for r in results]}
    if not n:
        return _result("consensus", status="DATA_UNAVAILABLE",
                       reasons=("NO_VALID_STRATEGIES",), indicators=indicators)
    direction = "UP" if votes["UP"] > n / 2 else "DOWN" if votes["DOWN"] > n / 2 else "WAIT"
    return _result("consensus", context, direction=direction,
                   score=(votes["UP"] - votes["DOWN"]) / n,
                   reasons=("MAJORITY_" + direction if direction != "WAIT" else "NO_DIRECTIONAL_MAJORITY",),
                   indicators=indicators)


def analyze_strategies(candles, *, now, confirmed_at):
    """Prepare one shared snapshot, calculate independent A/B, then consensus."""
    prepared, error = _prepared_or_error(candles, now, confirmed_at)
    if error:
        a, b = (_result(s, status="DATA_UNAVAILABLE", reasons=(error,))
                for s in (STRATEGY_A, STRATEGY_B))
    else:
        a, b = _breakout(*prepared), _momentum(*prepared)
    return {"A": a, "B": b, "consensus": consensus((a, b))}


def evaluate_result(result, horizon, target_candle, *, now, confirmed_at):
    """Evaluate an exact closed 1h target using the threshold frozen at calculation.

    WAIT and technical skips never count as wins. No current ticker, minute
    candle, recalculated ATR or replacement target is accepted here.
    """
    if horizon not in (1, 4):
        raise ValueError("Only 1h and 4h horizons are supported")
    if result["status"] != "OK":
        return {"status": "SKIPPED", "reason_codes": ["NO_VALID_FORECAST"], "correct": None}
    target = _candle(target_candle)
    due_at = result["due_at"][horizon]
    if target[6] + 1 != due_at:
        raise ValueError("Target candle must close at the exact forecast due_at")
    if not _timestamp(now) or now < due_at:
        return {"status": "DATA_UNAVAILABLE", "reason_codes": ["TARGET_NOT_CLOSED"], "correct": None}
    if not _timestamp(confirmed_at) or not due_at <= confirmed_at <= now:
        return {"status": "DATA_UNAVAILABLE", "reason_codes": ["TARGET_CLOSE_NOT_CONFIRMED"], "correct": None}
    base, threshold = result["base_price"], result["thresholds_pct"][horizon]
    if not _finite(base) or base <= 0 or not _finite(threshold) or threshold < 0:
        raise ValueError("Invalid frozen evaluation parameters")
    if result["direction"] not in DIRECTIONS:
        raise ValueError("Invalid forecast direction")
    change = _change_pct(target[4], base)
    if not math.isfinite(change):
        raise ValueError("Non-finite price change")
    observed = "UP" if change > threshold else "DOWN" if change < -threshold else "FLAT"
    return {"status": "OK", "actual_price": target[4], "change_pct": change,
            "observed_direction": observed,
            "correct": None if result["direction"] == "WAIT" else result["direction"] == observed,
            "reason_codes": ["WAIT_NOT_SCORED"] if result["direction"] == "WAIT" else []}

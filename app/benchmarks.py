"""Three fixed, pure benchmarks over caller-supplied point-in-time 1h OHLCV.

No future target price is accepted by this API. Callers supply the same snapshot
used by the strategy; close_time filtering does not reconstruct old revisions.
EMA uses all supplied closed history, SMA seed and the existing EMA recurrence.
For reproducible comparisons, keep the supplied history window identical across
participants/timestamps according to the calling experiment's data protocol.
"""
import math

from .analytics import ema
from .krypta_strategy import _candle
from .strategies import HOUR_MS

ALWAYS_UP = "ALWAYS_UP"
PREVIOUS_HOUR_MOMENTUM = "PREVIOUS_HOUR_MOMENTUM"
EMA20_50_1H = "EMA20_50_1H"
BENCHMARKS = (ALWAYS_UP, PREVIOUS_HOUR_MOMENTUM, EMA20_50_1H)


def benchmark_decision(benchmark_id, candles, *, decision_at):
    """Return status, decision and input reference; technical failure is not WAIT.

    Times are UTC milliseconds. Only bars with close_time < decision_at are
    inspected for OHLCV. Closed input must be chronological, contiguous and end
    at the latest fully closed hour. Minimum bars: 1 / 2 / 50 respectively.
    There is no warm-up fetch, threshold, epsilon or future-outcome dependency.
    """
    if benchmark_id not in BENCHMARKS:
        raise ValueError("Unknown benchmark")
    if type(decision_at) is not int or decision_at < 0:
        raise ValueError("decision_at must be nonnegative UTC milliseconds")
    result = dict(benchmark_id=benchmark_id, decision_at=decision_at,
                  status="DATA_UNAVAILABLE", decision=None, reference_at=None,
                  base_price=None, reason_codes=[])
    try:
        closed = []
        for row in candles:
            end = row["close_time"]
            if type(end) is not int:
                raise ValueError("INVALID_CANDLE_TIME")
            if end < decision_at:
                closed.append(_candle(row, "1h"))
        count = {ALWAYS_UP: 1, PREVIOUS_HOUR_MOMENTUM: 2, EMA20_50_1H: 50}[benchmark_id]
        if len(closed) < count:
            raise ValueError("INSUFFICIENT_HISTORY")
        if any(b[0]-a[0] != HOUR_MS for a, b in zip(closed, closed[1:])):
            raise ValueError("NON_CONTIGUOUS_HISTORY")
        reference = decision_at // HOUR_MS * HOUR_MS
        if closed[-1][6]+1 != reference:
            raise ValueError("STALE_LAST_CANDLE")
        closes = [c[4] for c in closed]
        if benchmark_id == ALWAYS_UP:
            decision = "UP"
        else:
            a, b = ((closes[-1], closes[-2]) if benchmark_id == PREVIOUS_HOUR_MOMENTUM
                    else (ema(closes, 20), ema(closes, 50)))
            if not math.isfinite(a) or not math.isfinite(b):
                raise ValueError("NON_FINITE_INDICATOR")
            decision = "UP" if a > b else "DOWN" if a < b else "WAIT"
        result.update(status="OK", decision=decision, reference_at=reference, base_price=closes[-1])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result["reason_codes"] = [str(exc) if isinstance(exc, ValueError) else "INVALID_CANDLE_DATA"]
    return result

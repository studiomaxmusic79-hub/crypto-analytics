"""Pure Krypta MTF Strategy v1; no I/O, LIVE issuance, evaluation or backtest.

Input: Store-format OHLCV dictionaries, oldest first, keyed by 15m/1h/4h.
All times are UTC Unix milliseconds. A candle is closed at close_time + 1.
Only candles closed by reference_time are used; each timeframe must end at
its latest UTC boundary at or before that reference. No wall clock is read.
LIVE will supply the issuing 1h closing boundary, not the later arrival time.
Closure here is temporal only: LIVE must separately verify fresh collection.

Fixed windows: 500 closed 1h, 200 closed 4h, 15 closed 15m. EMA reuses the
project's SMA seed and recurrence. ATR14 is the arithmetic mean of 14 True
Ranges from the existing pure TR helper (no Wilder smoothing or epsilon).
M15m uses closes[-1] - closes[-5], exactly four 15m intervals, divided by ATR14.

OK results contain UP/DOWN/WAIT. Missing/invalid data are DATA_UNAVAILABLE;
zero ATR is SKIPPED. Technical results have signal=None, never a fake WAIT.
There is no confidence, probability or blended score.
"""
from collections.abc import Mapping, Sequence
import math

from .analytics import ema
from .strategies_v2 import _true_ranges


STRATEGY_ID = "krypta_mtf"
STRATEGY_VERSION = "v1"
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
WINDOW_BARS = {"15m": 15, "1h": 500, "4h": 200}
TREND_THRESHOLD = 0.5
MOMENTUM_THRESHOLD = 1.0


class _DataError(ValueError):
    def __init__(self, code, interval=None, status="DATA_UNAVAILABLE"):
        super().__init__(code)
        self.code, self.interval, self.status = code, interval, status


def _timestamp(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _candle(row, interval):
    try:
        start, end = row["time"], row["close_time"]
        delta = INTERVAL_MS[interval]
        if (not _timestamp(start) or not _timestamp(end) or start % delta
                or end != start + delta - 1):
            raise _DataError("INVALID_CANDLE_TIME", interval)
        raw = [row[k] for k in ("open", "high", "low", "close", "volume")]
        if any(isinstance(value, bool) for value in raw):
            raise _DataError("INVALID_OHLCV", interval)
        opening, high, low, close, volume = map(float, raw)
        if (not all(math.isfinite(value) for value in (opening, high, low, close, volume))
                or min(opening, high, low, close) <= 0 or volume < 0
                or high < max(opening, low, close) or low > min(opening, high, close)):
            raise _DataError("INVALID_OHLCV", interval)
        return (start, opening, high, low, close, volume, end)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, _DataError):
            raise
        raise _DataError("INVALID_OHLCV", interval) from exc


def _window(rows, interval, reference_time):
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise _DataError("INVALID_CANDLE_LIST", interval)
    closed = []
    for row in rows:
        if not isinstance(row, Mapping) or not _timestamp(row.get("close_time")):
            raise _DataError("INVALID_CANDLE_TIME", interval)
        if row["close_time"] < reference_time:
            closed.append(row)
    count = WINDOW_BARS[interval]
    if len(closed) < count:
        raise _DataError("INSUFFICIENT_HISTORY", interval)
    window = tuple(_candle(row, interval) for row in closed[-count:])
    delta = INTERVAL_MS[interval]
    if any(b[0]-a[0] != delta for a, b in zip(window, window[1:])):
        raise _DataError("NON_CONTIGUOUS_HISTORY", interval)
    expected_close = reference_time // delta * delta
    if window[-1][6]+1 != expected_close:
        raise _DataError("STALE_LAST_CANDLE", interval)
    return window


def analyze(candles, *, reference_time):
    """Return a JSON-safe decision from a single historical snapshot.

    ``candles`` maps 15m/1h/4h to chronological Store OHLCV dictionaries:
    time, open, high, low, close, volume, close_time. Extra older/future rows
    do not extend the fixed calculation windows. Invalid reference times,
    gaps, stale tails and invalid OHLCV yield a technical result, not WAIT.

    Result: strategy_id/version, status, signal, reference_time, base_price,
    T1h/T4h/M15m, indicators per timeframe, last_close_at (closing boundaries),
    last_candle_times (original Binance times), conditions, reason_codes and
    human-readable reasons. A technical result may retain diagnostics for
    timeframes already processed, but cannot carry a directional signal.
    """
    result = dict(strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
                  status="DATA_UNAVAILABLE", signal=None,
                  reference_time=reference_time if _timestamp(reference_time) else None,
                  base_price=None, T1h=None, T4h=None, M15m=None,
                  indicators={}, last_close_at={}, last_candle_times={},
                  conditions={}, reason_codes=[], reasons=[])
    try:
        if not _timestamp(reference_time) or reference_time < 0:
            raise _DataError("INVALID_REFERENCE_TIME")
        if not isinstance(candles, Mapping):
            raise _DataError("INVALID_TIMEFRAME_DATA")
        windows = {}
        for interval in ("1h", "4h", "15m"):
            if interval not in candles:
                raise _DataError("MISSING_TIMEFRAME", interval)
            window = _window(candles[interval], interval, reference_time)
            windows[interval] = window
            atr = sum(_true_ranges(window)[-14:])/14
            if not math.isfinite(atr) or atr < 0:
                raise _DataError("INVALID_ATR", interval)
            result["last_close_at"][interval] = window[-1][6]+1
            result["last_candle_times"][interval] = dict(time=window[-1][0], close_time=window[-1][6])
            indicators = dict(atr14=atr, bars_used=len(window))
            result["indicators"][interval] = indicators
            if atr == 0:
                raise _DataError("ZERO_ATR", interval, status="SKIPPED")
            if interval != "15m":
                closes = [row[4] for row in window]
                fast, slow = ema(closes, 20), ema(closes, 50)
                if not all(math.isfinite(value) for value in (fast, slow)):
                    raise _DataError("INVALID_EMA", interval)
                trend = (fast-slow)/atr
                if not math.isfinite(trend):
                    raise _DataError("NON_FINITE_FACTOR", interval)
                indicators.update(ema20=fast, ema50=slow)
                result["T1h" if interval == "1h" else "T4h"] = trend
            else:
                current, previous = window[-1][4], window[-5][4]
                momentum = (current-previous)/atr
                if not math.isfinite(momentum):
                    raise _DataError("NON_FINITE_FACTOR", interval)
                indicators.update(close=current, reference_close=previous,
                                  momentum_reference_close_at=window[-5][6]+1)
                result["M15m"] = momentum
        result["base_price"] = windows["1h"][-1][4]
        t1, t4, momentum = result["T1h"], result["T4h"], result["M15m"]
        up = dict(trend_1h=t1 >= TREND_THRESHOLD,
                  momentum_15m=momentum >= MOMENTUM_THRESHOLD,
                  context_4h=t4 > -TREND_THRESHOLD)
        down = dict(trend_1h=t1 <= -TREND_THRESHOLD,
                    momentum_15m=momentum <= -MOMENTUM_THRESHOLD,
                    context_4h=t4 < TREND_THRESHOLD)
        signal = "UP" if all(up.values()) else "DOWN" if all(down.values()) else "WAIT"
        result.update(status="OK", signal=signal, conditions=dict(up=up, down=down))
        trend_label = "UP" if up["trend_1h"] else "DOWN" if down["trend_1h"] else "NEUTRAL"
        context_label = "UP" if t4 >= TREND_THRESHOLD else "DOWN" if t4 <= -TREND_THRESHOLD else "NEUTRAL"
        momentum_label = "UP" if up["momentum_15m"] else "DOWN" if down["momentum_15m"] else "NEUTRAL"
        result["reason_codes"] = ["TREND_1H_"+trend_label, "CONTEXT_4H_"+context_label,
                                  "MOMENTUM_15M_"+momentum_label, "SIGNAL_"+signal]
        result["reasons"] = [f"Основное направление 1h: {trend_label}.",
                             f"Контекст 4h: {context_label}; используется только как фильтр.",
                             f"Часовой импульс по 15m: {momentum_label}.",
                             "Все условия направления выполнены." if signal != "WAIT" else
                             "Условия UP/DOWN не выполнены одновременно; нормальный WAIT."]
    except _DataError as exc:
        result.update(status=exc.status, signal=None, error_interval=exc.interval,
                      reason_codes=[exc.code],
                      reasons=[f"Расчёт невозможен: {exc.code}" +
                               (f" ({exc.interval})." if exc.interval else ".")])
    return result

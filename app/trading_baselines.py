"""Research trading intents only; no orders, fills, sizing, fees or LIVE changes."""
from collections.abc import Mapping, Sequence

from .krypta_strategy import INTERVAL_MS, _candle

ETH_MOMENTUM_LONG_ONLY_V1 = "ETH_MOMENTUM_LONG_ONLY_V1"


def eth_momentum_long_only(candles, *, lookback_bars, has_position, decision_at):
    """Read ETH-only Store-format 15m OHLCV, oldest first; emit a trading intent.

    Caller supplies reconciled ETH LONG presence (bool), never a previous signal.
    N=lookback_bars means close[-1] - close[-N-1], requiring N+1 closed bars.
    Candle parsing/float signal arithmetic reuses Strategy v1's input semantics;
    these prices are indicators, not monetary execution facts for the evaluator.
    Only close_time < decision_at is eligible. The selected window must be
    contiguous and finish at the latest closed UTC 15m boundary. Older unused
    rows and forming/future OHLCV cannot affect the calculation. Temporal closure
    alone cannot authenticate historical revisions or when data were observed.

    status=OK has BUY/WAIT when flat, HOLD/SELL when long. DATA_UNAVAILABLE has
    intent=None, never WAIT/HOLD. No BTC input, execution inference or wall clock.
    """
    if type(lookback_bars) is not int or lookback_bars < 1:
        raise ValueError("lookback_bars must be a positive integer")
    if type(has_position) is not bool:
        raise ValueError("has_position must be a reconciled LONG-position boolean")
    if type(decision_at) is not int or decision_at < 0:
        raise ValueError("decision_at must be nonnegative UTC Unix milliseconds")
    result = dict(baseline_id=ETH_MOMENTUM_LONG_ONLY_V1, status="DATA_UNAVAILABLE",
                  intent=None, decision_at=decision_at, reference_at=None,
                  momentum=None, lookback_bars=lookback_bars, reason_codes=[])
    try:
        if not isinstance(candles, Sequence) or isinstance(candles, (str, bytes)):
            raise ValueError("INVALID_CANDLE_LIST")
        closed = []
        for row in candles:
            if not isinstance(row, Mapping) or type(row.get("close_time")) is not int:
                raise ValueError("INVALID_CANDLE_TIME")
            if row["close_time"] < decision_at:
                closed.append(row)
        if len(closed) < lookback_bars + 1:
            raise ValueError("INSUFFICIENT_HISTORY")
        window = [_candle(row, "15m") for row in closed[-lookback_bars-1:]]
        delta = INTERVAL_MS["15m"]
        if any(row[0] < 0 for row in window):
            raise ValueError("INVALID_CANDLE_TIME")
        if any(b[0] - a[0] != delta for a, b in zip(window, window[1:])):
            raise ValueError("NON_CONTIGUOUS_HISTORY")
        reference = decision_at // delta * delta
        if window[-1][6] + 1 != reference:
            raise ValueError("STALE_LAST_CANDLE")
        momentum = window[-1][4] - window[0][4]
        intent = ("HOLD" if has_position else "BUY") if momentum > 0 else ("SELL" if has_position else "WAIT")
        result.update(status="OK", intent=intent, reference_at=reference, momentum=momentum)
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        result["reason_codes"] = [getattr(exc, "code", str(exc))]
    return result

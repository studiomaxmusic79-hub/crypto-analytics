"""Frozen Krypta v2 specification: pure calculations on confirmed 1h OHLCV.

No I/O, historical runs, parameter fitting, LIVE integration or execution.
A/B and consensus v1 remain in strategies.py, unchanged. EMA seeding, the
rolling 500-bar input, SMA-of-TR ATR14 and frozen evaluation thresholds are
inherited from that module. EMA20[t-1] uses the same window's 499-bar prefix.

Regime precedence: V >= 2 -> HIGH_VOLATILITY; otherwise D >= .25 -> TREND_UP,
D <= -.25 -> TREND_DOWN, otherwise RANGE. V compares current ATR14/Close
with the median of the previous 168 values (current value excluded).

C uses mu=EMA20[t-1], a=ATR14[t-1], bands mu +/- 2*a. In RANGE only:
UP: Low <= lower < Close <= mu-a, Close > Open, High < upper.
DOWN: High >= upper > Close >= mu+a, Close < Open, Low > lower.
Both bands touched -> WAIT. Score=(mu-Close)/a is not a probability.

Consensus v2 requires A+B aligned with the trend, selects C alone in RANGE,
and waits in HIGH_VOLATILITY. Missing required calculations never reduce the
quorum. Its score is +1/-1 for UP/DOWN, 0 for WAIT, not a weighted score.
"""
from copy import deepcopy
import math
from statistics import median

from . import strategies as v1


REGIME_ID = "market_regime"
REGIME_VERSION = "v1"
STRATEGY_C = "range_reversion"
C_VERSION = "v1"
CONSENSUS_VERSION = "v2"
REGIMES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY")
TREND_THRESHOLD = 0.25
VOLATILITY_LOOKBACK = 168
VOLATILITY_MULTIPLIER = 2.0
BAND_ATR_MULTIPLIER = 2.0
MIN_RETURN_ATR = 1.0


def _result(identity, version, context=None, **kwargs):
    result = v1._result(identity, context, **kwargs)
    result["strategy_version"] = version
    return deepcopy(result)


def _regime_error(context, reason, indicators=None):
    return {**_result(REGIME_ID, REGIME_VERSION, context,
                      status="DATA_UNAVAILABLE", reasons=(reason,),
                      indicators=indicators), "regime": None}


def _true_ranges(window):
    return [max(b[2]-b[3], abs(b[2]-a[4]), abs(b[3]-a[4]))
            for a, b in zip(window, window[1:])]


def _market_regime(window, context, features):
    ema20, ema50, atr14 = features
    indicators = dict(ema20=ema20, ema50=ema50, atr14=atr14)
    if atr14 <= 0:
        return _regime_error(context, "ZERO_ATR", indicators)
    tr = _true_ranges(window)
    # tr[i-1] is TR for window[i]. Each historical ATR uses its own past.
    previous_v = [sum(tr[i-14:i])/14/window[i][4]
                  for i in range(len(window)-VOLATILITY_LOOKBACK-1, len(window)-1)]
    baseline = median(previous_v)
    if not all(math.isfinite(value) for value in previous_v):
        return _regime_error(context, "NON_FINITE_VOLATILITY", indicators)
    if baseline <= 0:
        return _regime_error(context, "ZERO_VOLATILITY_BASELINE", indicators)
    d = (ema20-ema50)/atr14
    current_v = atr14/window[-1][4]
    ratio = current_v/baseline
    if not all(math.isfinite(value) for value in (d, current_v, ratio)):
        return _regime_error(context, "NON_FINITE_REGIME", indicators)
    name = ("HIGH_VOLATILITY" if ratio >= VOLATILITY_MULTIPLIER else
            "TREND_UP" if d >= TREND_THRESHOLD else
            "TREND_DOWN" if d <= -TREND_THRESHOLD else "RANGE")
    indicators.update(d=d, normalized_atr=current_v, volatility_baseline=baseline,
                      volatility_ratio=ratio, baseline_bars=VOLATILITY_LOOKBACK)
    return {**_result(REGIME_ID, REGIME_VERSION, context, score=d,
                      reasons=(name,), indicators=indicators), "regime": name}


def _range_reversion(window, context, regime):
    stamp = deepcopy(regime)
    if regime["status"] != "OK":
        return _result(STRATEGY_C, C_VERSION, context, status="DATA_UNAVAILABLE",
                       reasons=("REGIME_UNAVAILABLE",), indicators={"regime": stamp})
    mu = v1.ema([row[4] for row in window[:-1]], 20)
    a = sum(_true_ranges(window)[-15:-1])/14
    if not math.isfinite(mu) or not math.isfinite(a):
        return _result(STRATEGY_C, C_VERSION, context, status="DATA_UNAVAILABLE",
                       reasons=("NON_FINITE_BANDS",), indicators={"regime": stamp})
    if a <= 0:
        return _result(STRATEGY_C, C_VERSION, context, status="SKIPPED",
                       reasons=("ZERO_PREVIOUS_ATR",), indicators={"regime": stamp})
    lower, upper = mu-BAND_ATR_MULTIPLIER*a, mu+BAND_ATR_MULTIPLIER*a
    _, opening, high, low, close, _, _ = window[-1]
    score = (mu-close)/a
    if not all(math.isfinite(value) for value in (lower, upper, score)):
        return _result(STRATEGY_C, C_VERSION, context, status="DATA_UNAVAILABLE",
                       reasons=("NON_FINITE_BANDS",), indicators={"regime": stamp})
    indicators = dict(regime=stamp, previous_ema20=mu, previous_atr14=a,
                      lower_band=lower, upper_band=upper, distance_atr=score)
    if regime["regime"] != "RANGE":
        direction, reason = "WAIT", "NOT_RANGE"
    elif low <= lower and high >= upper:
        direction, reason = "WAIT", "TWO_SIDED_EXTREME"
    elif low <= lower < close <= mu-MIN_RETURN_ATR*a and close > opening and high < upper:
        direction, reason = "UP", "LOWER_BAND_REENTRY"
    elif high >= upper > close >= mu+MIN_RETURN_ATR*a and close < opening and low > lower:
        direction, reason = "DOWN", "UPPER_BAND_REENTRY"
    else:
        direction, reason = "WAIT", "NO_REENTRY"
    return _result(STRATEGY_C, C_VERSION, context, direction=direction,
                   score=score, reasons=(reason,), indicators=indicators)


def analyze_market_regime(candles, *, now, confirmed_at):
    """Classify one confirmed hourly snapshot; relative volatility, not risk probability."""
    prepared, error = v1._prepared_or_error(candles, now, confirmed_at)
    return _regime_error(None, error) if error else _market_regime(*prepared)


def analyze_c(candles, *, now, confirmed_at):
    """Range Reversion v1 with bands frozen before the current closed candle."""
    prepared, error = v1._prepared_or_error(candles, now, confirmed_at)
    if error:
        return _result(STRATEGY_C, C_VERSION, status="DATA_UNAVAILABLE", reasons=(error,))
    window, context, _ = prepared
    return _range_reversion(window, context, _market_regime(*prepared))


def _valid_context(result):
    """Refuse malformed forecast contexts even when the routing branch is WAIT."""
    try:
        closing, base = result["input_close_at"], result["base_price"]
        return (v1._timestamp(closing) and closing % v1.HOUR_MS == 0
                and v1._finite(base) and base > 0
                and all(v1._timestamp(result["due_at"][h])
                        and result["due_at"][h] == closing+h*v1.HOUR_MS
                        and v1._finite(result["thresholds_pct"][h])
                        and result["thresholds_pct"][h] >= 0 for h in (1, 4)))
    except (KeyError, TypeError):
        return False


def consensus_v2(regime, results):
    """Route distinct versioned A/B/C results without weights or fallback votes.

    Only selected strategies participate. Mixed selected contexts raise
    ValueError; missing/invalid required results produce DATA_UNAVAILABLE.
    In RANGE selected_count=1 and agreement_pct=None, never a misleading 100%.
    """
    versions = {v1.STRATEGY_A: v1.VERSION, v1.STRATEGY_B: v1.VERSION, STRATEGY_C: C_VERSION}
    results = list(results)
    identities = [r.get("strategy_id") for r in results]
    if len(set(identities)) != len(identities) or any(i not in versions for i in identities):
        raise ValueError("Consensus v2 requires distinct A/B/C results")
    if (regime.get("strategy_id") != REGIME_ID
            or regime.get("strategy_version") != REGIME_VERSION
            or regime.get("status") != "OK" or regime.get("regime") not in REGIMES
            or not v1._finite(regime.get("score")) or not _valid_context(regime)):
        return _result("consensus", CONSENSUS_VERSION, status="DATA_UNAVAILABLE",
                       reasons=("REGIME_UNAVAILABLE",))
    context = {k: regime[k] for k in v1.CONTEXT_FIELDS}
    name = regime["regime"]
    selected = ([v1.STRATEGY_A, v1.STRATEGY_B] if name in ("TREND_UP", "TREND_DOWN")
                else [STRATEGY_C] if name == "RANGE" else [])
    by_id = {r["strategy_id"]: r for r in results}
    valid = []
    for identity in selected:
        row = by_id.get(identity, {})
        if (row.get("strategy_version") != versions[identity] or row.get("status") != "OK"
                or row.get("direction") not in v1.DIRECTIONS
                or not v1._finite(row.get("score")) or not _valid_context(row)):
            continue
        if any(row[k] != context[k] for k in v1.CONTEXT_FIELDS):
            raise ValueError("Selected strategies and regime must share the same hourly context")
        valid.append(row)
    votes = {d: sum(row["direction"] == d for row in valid) for d in v1.DIRECTIONS}
    indicators = dict(regime=deepcopy(regime), selected_strategies=selected,
                      selected_count=len(selected), valid_count=len(valid), votes=votes,
                      agreement_pct=(round(100*max(votes.values())/len(valid))
                                     if len(valid) == len(selected) and len(valid) >= 2 else None),
                      selection_type=("TREND_CONFIRMATION" if len(selected) == 2 else
                                      "REGIME_ROUTING" if selected else "VOLATILITY_GUARD"))
    if len(valid) != len(selected):
        return _result("consensus", CONSENSUS_VERSION, context, status="DATA_UNAVAILABLE",
                       reasons=("REQUIRED_STRATEGY_UNAVAILABLE",), indicators=indicators)
    if name == "HIGH_VOLATILITY":
        direction, reason = "WAIT", "VOLATILITY_GUARD"
    elif name == "RANGE":
        direction, reason = valid[0]["direction"], "RANGE_REVERSION_SELECTED"
    else:
        expected = "UP" if name == "TREND_UP" else "DOWN"
        direction = expected if votes[expected] == 2 else "WAIT"
        reason = "TREND_CONFIRMED" if direction != "WAIT" else "NO_ALIGNED_CONFIRMATION"
    return _result("consensus", CONSENSUS_VERSION, context, direction=direction,
                   score={"UP": 1., "DOWN": -1., "WAIT": 0.}[direction],
                   reasons=(reason,), indicators=indicators)


def analyze_strategies_v2(candles, *, now, confirmed_at):
    """One shared preparation; original v1 outputs plus regime, C and consensus_v2.

    Existing LIVE/backtest callers keep using v1.analyze_strategies; this new
    entry point has no callers outside its calculation tests at this stage.
    """
    prepared, error = v1._prepared_or_error(candles, now, confirmed_at)
    if error:
        a, b = (v1._result(identity, status="DATA_UNAVAILABLE", reasons=(error,))
                for identity in (v1.STRATEGY_A, v1.STRATEGY_B))
        regime = _regime_error(None, error)
        c = _result(STRATEGY_C, C_VERSION, status="DATA_UNAVAILABLE", reasons=(error,))
    else:
        window, context, _ = prepared
        a, b = v1._breakout(*prepared), v1._momentum(*prepared)
        regime = _market_regime(*prepared)
        c = _range_reversion(window, context, regime)
    return dict(A=a, B=b, consensus=v1.consensus((a, b)), regime=regime, C=c,
                consensus_v2=consensus_v2(regime, (a, b, c)))

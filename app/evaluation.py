"""Pure shared evaluation; no storage, publication, issuance or history updates.

    row = dict(symbol="BTCUSDT", horizon=1, strategy_id="ALWAYS_UP",
               decision_at=3600000, **evaluate_decision("UP", 100, 101, .2))
    metrics = aggregate_results([row])

Aggregate/compare rows must identify one symbol, horizon (hours: 1 or 4),
strategy_id and unique UTC-millisecond decision_at. No implicit cohort filtering.
"""
import math

from .strategies import _change_pct


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def evaluate_decision(decision, base_price, actual_price, neutral_pct):
    """Use the accepted LIVE percentage arithmetic and strict FLAT boundaries.

    Prices must be finite and positive; neutral_pct is a nonnegative percentage
    (e.g. .2 means 0.2%). Invalid data raise ValueError, never a fake WAIT result.
    """
    if decision not in ("UP", "DOWN", "WAIT"):
        raise ValueError("decision must be UP, DOWN or WAIT")
    if any(not _number(v) or v <= 0 for v in (base_price, actual_price)):
        raise ValueError("Prices must be finite positive numbers")
    if not _number(neutral_pct) or neutral_pct < 0:
        raise ValueError("neutral_pct must be a finite nonnegative percentage")
    change = _change_pct(actual_price, base_price)
    if not math.isfinite(change):
        raise ValueError("Non-finite percentage change")
    actual = "UP" if change > neutral_pct else "DOWN" if change < -neutral_pct else "FLAT"
    participated = decision != "WAIT"
    return dict(decision=decision, base_price=base_price, actual_price=actual_price,
                neutral_pct=neutral_pct, actual_direction=actual, change_pct=change,
                participated=participated, correct=decision == actual if participated else None)


def _index_results(results):
    scope = None
    indexed = {}
    for row in results:
        try:
            current = (row["symbol"], row["horizon"], row["strategy_id"])
            stamp = row["decision_at"]
            decision, actual = row["decision"], row["actual_direction"]
            participated, correct = row["participated"], row["correct"]
            change = row["change_pct"]
        except (KeyError, TypeError) as exc:
            raise ValueError("Provide a calculated result with cohort and decision_at") from exc
        if (not isinstance(current[0], str) or not current[0].strip()
            or type(current[1]) is not int or current[1] not in (1, 4)
            or not isinstance(current[2], str) or not current[2].strip()
            or type(stamp) is not int or stamp < 0):
            raise ValueError("Invalid symbol, horizon, strategy_id or decision_at")
        if scope is not None and scope != current:
            raise ValueError("Do not merge symbols, horizons or strategies")
        if (decision not in ("UP", "DOWN", "WAIT") or actual not in ("UP", "DOWN", "FLAT")
            or participated is not (decision != "WAIT") or not _number(change)
            or correct is not (None if decision == "WAIT" else decision == actual)):
            raise ValueError("Inconsistent evaluation result; WAIT cannot be scored")
        if stamp in indexed:
            raise ValueError("Duplicate decision_at would double-count a decision")
        scope = current
        indexed[stamp] = row
    return scope, indexed


def _metrics(scope, indexed):
    rows = list(indexed.values())
    total = len(rows)
    directional = sum(r["participated"] for r in rows)
    correct = sum(r["correct"] is True for r in rows)
    return dict(symbol=scope[0] if scope else None, horizon=scope[1] if scope else None,
                strategy_id=scope[2] if scope else None, total=total,
                directional_count=directional, wait_count=total-directional,
                coverage_pct=100*directional/total if total else 0.,
                correct_count=correct, incorrect_count=directional-correct,
                directional_accuracy_pct=100*correct/directional if directional else None,
                actual_up_count=sum(r["actual_direction"] == "UP" for r in rows),
                actual_down_count=sum(r["actual_direction"] == "DOWN" for r in rows),
                actual_flat_count=sum(r["actual_direction"] == "FLAT" for r in rows))


def aggregate_results(results):
    """Aggregate already calculated rows for exactly one cohort; no rescoring.

    WAIT contributes to total, coverage denominator and actual-direction counts,
    but never correct/incorrect or the directional-accuracy denominator.
    Empty input: zero coverage, null accuracy and null cohort identity.
    """
    return _metrics(*_index_results(results))


def compare_results(strategy_results, benchmark_results):
    """Compare complete, identical timestamp sets, independent of row order.

    Both cohorts must have the same symbol/horizon and each pair the same base,
    actual and neutral_pct. Inputs must already refer to the same PIT data;
    this pure function neither fetches nor authenticates candle observations.
    Empty/no-directional accuracy yields null excess, not a synthetic zero.
    """
    strategy_scope, strategy = _index_results(strategy_results)
    benchmark_scope, benchmark = _index_results(benchmark_results)
    if strategy.keys() != benchmark.keys():
        raise ValueError("Strategy and benchmark must have exactly the same decision timestamps")
    if strategy_scope and strategy_scope[:2] != benchmark_scope[:2]:
        raise ValueError("Comparison requires the same symbol and horizon")
    for stamp in strategy:
        for field in ("base_price", "actual_price", "neutral_pct"):
            a, b = strategy[stamp].get(field), benchmark[stamp].get(field)
            if (not _number(a) or not _number(b) or a != b
                or (a < 0 if field == "neutral_pct" else a <= 0)):
                raise ValueError("Paired results require identical prices and neutral_pct")
    a, b = _metrics(strategy_scope, strategy), _metrics(benchmark_scope, benchmark)
    aa, ba = a["directional_accuracy_pct"], b["directional_accuracy_pct"]
    return dict(symbol=a["symbol"], horizon=a["horizon"], strategy=a, benchmark=b,
                strategy_directional_accuracy_pct=aa, benchmark_directional_accuracy_pct=ba,
                strategy_coverage_pct=a["coverage_pct"], benchmark_coverage_pct=b["coverage_pct"],
                accuracy_excess_pp=aa-ba if aa is not None and ba is not None else None)

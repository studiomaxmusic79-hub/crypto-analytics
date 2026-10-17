"""Shared evaluation/benchmarks never write or rescore LIVE history."""
import copy

import pytest

from app.evaluation import evaluate_decision, aggregate_results, compare_results
from app.strategies import evaluate_result, HOUR_MS


def row(decision="UP", actual=101., *, stamp=HOUR_MS, symbol="BTCUSDT", horizon=1,
        strategy_id="strategy", base=100., neutral=.2):
    return dict(symbol=symbol, horizon=horizon, strategy_id=strategy_id, decision_at=stamp,
                **evaluate_decision(decision, base, actual, neutral))


@pytest.mark.parametrize("decision,actual,direction,correct,change", [
    ("UP", 101., "UP", True, 1.), ("UP", 99., "DOWN", False, -1.),
    ("DOWN", 99., "DOWN", True, -1.), ("DOWN", 101., "UP", False, 1.),
    ("UP", 100., "FLAT", False, 0.), ("DOWN", 100., "FLAT", False, 0.),
    ("WAIT", 101., "UP", None, 1.), ("WAIT", 99., "DOWN", None, -1.),
    ("WAIT", 100., "FLAT", None, 0.),
])
def test_evaluator_decisions_and_percentage_units(decision, actual, direction, correct, change):
    result = evaluate_decision(decision, 100., actual, .2)
    assert result["actual_direction"] == direction
    assert result["correct"] is correct
    assert result["change_pct"] == change
    assert result["participated"] is (decision != "WAIT")


@pytest.mark.parametrize("actual,expected", [(30.06, "FLAT"), (29.94, "FLAT"),
    (30.0600001, "UP"), (29.9399999, "DOWN"), (30.0599999, "FLAT"), (29.9400001, "FLAT")])
def test_decimal_threshold_boundaries_have_no_float_epsilon(actual, expected):
    assert evaluate_decision("UP", 30., actual, .2)["actual_direction"] == expected


def test_zero_threshold_and_tiny_directional_changes():
    assert evaluate_decision("UP", 100, 100, 0)["actual_direction"] == "FLAT"
    assert evaluate_decision("UP", 100, 100.000000001, 0)["correct"] is True
    assert evaluate_decision("DOWN", 100, 99.999999999, 0)["correct"] is True


@pytest.mark.parametrize("changes", [dict(decision="FLAT"), dict(decision=None),
    dict(base_price=0), dict(actual_price=-1), dict(base_price=True), dict(actual_price="101"),
    dict(base_price=float("inf")), dict(actual_price=float("nan")), dict(neutral_pct=-.1),
    dict(neutral_pct=True), dict(neutral_pct=float("inf")), dict(base_price=10**1000),
    dict(base_price=1e-300, actual_price=1e300)])
def test_invalid_data_is_rejected_instead_of_becoming_wait(changes):
    args = dict(decision="UP", base_price=100., actual_price=101., neutral_pct=.2)
    with pytest.raises(ValueError):
        evaluate_decision(**{**args, **changes})


def test_shared_result_matches_existing_live_evaluation_for_each_horizon():
    # Synthetic exact candles only; no service, storage or backtest invocation.
    for horizon in (1, 4):
        due = horizon*HOUR_MS
        for decision in ("UP", "DOWN", "WAIT"):
            for threshold in (.1, .2, .5):
                for actual in (29., 29.94, 30., 30.06, 31.):
                    candle = dict(time=due-HOUR_MS, close_time=due-1, open=30., high=32.,
                                  low=28., close=actual, volume=10.)
                    live = evaluate_result(dict(status="OK", direction=decision, base_price=30.,
                        due_at={horizon: due}, thresholds_pct={horizon: threshold}), horizon,
                        candle, now=due, confirmed_at=due)
                    common = evaluate_decision(decision, 30., actual, threshold)
                    assert common["actual_direction"] == live["observed_direction"]
                    assert common["change_pct"] == live["change_pct"]
                    assert common["correct"] is live["correct"]


def test_aggregate_mixed_results_counts_wait_without_scoring_it():
    rows = [row("UP", 101, stamp=1), row("DOWN", 99, stamp=2), row("UP", 99, stamp=3),
            row("DOWN", 100, stamp=4), row("WAIT", 101, stamp=5), row("WAIT", 100, stamp=6)]
    before = copy.deepcopy(rows)
    metrics = aggregate_results(iter(rows))
    assert metrics == dict(symbol="BTCUSDT", horizon=1, strategy_id="strategy", total=6,
        directional_count=4, wait_count=2, coverage_pct=100*4/6,
        correct_count=2, incorrect_count=2, directional_accuracy_pct=50.,
        actual_up_count=2, actual_down_count=2, actual_flat_count=2)
    assert rows == before


def test_all_wait_accuracy_is_null_even_when_actual_is_flat():
    metrics = aggregate_results([row("WAIT", 100, stamp=1), row("WAIT", 101, stamp=2)])
    assert metrics["total"] == metrics["wait_count"] == 2
    assert metrics["correct_count"] == metrics["incorrect_count"] == metrics["directional_count"] == 0
    assert metrics["coverage_pct"] == 0.
    assert metrics["directional_accuracy_pct"] is None


def test_empty_results_are_zero_coverage_and_null_accuracy():
    metrics = aggregate_results([])
    assert metrics["total"] == 0 and metrics["coverage_pct"] == 0
    assert metrics["directional_accuracy_pct"] is None and metrics["symbol"] is None
    comparison = compare_results([], [])
    assert comparison["accuracy_excess_pp"] is None


@pytest.mark.parametrize("change", [dict(symbol="ETHUSDT"), dict(horizon=4), dict(strategy_id="other")])
def test_aggregate_does_not_merge_cohorts(change):
    with pytest.raises(ValueError, match="merge"):
        aggregate_results([row(stamp=1), {**row(stamp=2), **change}])


def test_duplicate_timestamps_are_rejected_instead_of_double_counted():
    with pytest.raises(ValueError, match="Duplicate"):
        aggregate_results([row(), row()])


@pytest.mark.parametrize("change", [dict(correct=True, decision="WAIT", participated=False),
    dict(participated=False), dict(correct=None), dict(correct=1), dict(actual_direction="WAIT"),
    dict(change_pct=float("nan")), dict(horizon=True), dict(decision_at=True)])
def test_inconsistent_rows_cannot_inflate_metrics(change):
    with pytest.raises(ValueError):
        aggregate_results([{**row(), **change}])


def test_comparison_uses_same_timestamps_but_independent_directional_denominators():
    strategy = [row("UP", 101, stamp=1), row("DOWN", 99, stamp=2), row("WAIT", 100, stamp=3)]
    benchmark = [row("UP", actual, stamp=i, strategy_id="ALWAYS_UP")
                 for i, actual in ((3, 100), (1, 101), (2, 99))]
    before = copy.deepcopy((strategy, benchmark))
    comparison = compare_results(iter(strategy), iter(benchmark))
    assert comparison["strategy_directional_accuracy_pct"] == 100.
    assert comparison["benchmark_directional_accuracy_pct"] == 100/3
    assert comparison["accuracy_excess_pp"] == 100.-100/3
    assert comparison["strategy_coverage_pct"] == 200/3
    assert comparison["benchmark_coverage_pct"] == 100.
    assert (strategy, benchmark) == before


def test_comparison_excess_is_null_without_directional_accuracy():
    result = compare_results([row("WAIT")], [row(strategy_id="benchmark")])
    assert result["strategy_directional_accuracy_pct"] is None
    assert result["benchmark_directional_accuracy_pct"] == 100
    assert result["accuracy_excess_pp"] is None


@pytest.mark.parametrize("benchmark", [[], [row(stamp=2)], [row(), row(stamp=2)]])
def test_comparison_rejects_timestamp_mismatch_instead_of_silent_intersection(benchmark):
    with pytest.raises(ValueError, match="timestamps"):
        compare_results([row()], benchmark)


@pytest.mark.parametrize("change", [dict(symbol="ETHUSDT"), dict(horizon=4)])
def test_comparison_rejects_cross_symbol_or_horizon_pairs(change):
    with pytest.raises(ValueError, match="same symbol and horizon"):
        compare_results([row()], [{**row(strategy_id="benchmark"), **change}])


@pytest.mark.parametrize("change", [dict(base_price=99), dict(actual_price=102), dict(neutral_pct=.5)])
def test_comparison_requires_the_same_prices_and_flat_threshold(change):
    with pytest.raises(ValueError, match="identical prices"):
        compare_results([row()], [{**row(strategy_id="benchmark"), **change}])

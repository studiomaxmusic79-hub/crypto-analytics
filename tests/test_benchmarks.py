"""No network, storage or future outcome is needed to produce benchmark votes."""
import copy

import pytest

from app.analytics import ema
from app.benchmarks import (benchmark_decision, ALWAYS_UP, PREVIOUS_HOUR_MOMENTUM,
                            EMA20_50_1H, BENCHMARKS)
from app.evaluation import evaluate_decision, compare_results
from app.strategies import HOUR_MS


def candles(closes):
    return [dict(time=i*HOUR_MS, close_time=(i+1)*HOUR_MS-1,
                 open=c, high=c+1, low=c-1, close=c, volume=10.) for i, c in enumerate(closes)]


def decide(name, closes):
    return benchmark_decision(name, candles(closes), decision_at=len(closes)*HOUR_MS)


@pytest.mark.parametrize("closes", [[100], [101, 100], [100]*60])
def test_always_up_on_valid_data_regardless_of_trend(closes):
    result = decide(ALWAYS_UP, closes)
    assert result["status"] == "OK" and result["decision"] == "UP"


@pytest.mark.parametrize("closes,decision", [([100, 101], "UP"), ([101, 100], "DOWN"), ([100, 100], "WAIT")])
def test_momentum_compares_last_close_with_previous_close(closes, decision):
    rows = candles(closes)
    # Last candle's open is irrelevant: this is close-to-close momentum.
    rows[-1]["open"] = rows[-1]["close"]+.5
    result = benchmark_decision(PREVIOUS_HOUR_MOMENTUM, rows, decision_at=2*HOUR_MS)
    assert result["decision"] == decision and result["status"] == "OK"
    assert result["base_price"] == closes[-1]


@pytest.mark.parametrize("closes,decision", [(list(range(100, 160)), "UP"),
    (list(range(160, 100, -1)), "DOWN"), ([100]*60, "WAIT")])
def test_ema20_50_up_down_wait_uses_existing_sma_seed(closes, decision):
    result = decide(EMA20_50_1H, closes)
    fast, slow = ema(closes, 20), ema(closes, 50)
    assert decision == ("UP" if fast > slow else "DOWN" if fast < slow else "WAIT")
    assert result["decision"] == decision and result["status"] == "OK"


def test_fifty_bars_are_sufficient_for_ema_benchmark():
    assert decide(EMA20_50_1H, list(range(100, 150)))["decision"] == "UP"


@pytest.mark.parametrize("name,closes", [(ALWAYS_UP, []), (PREVIOUS_HOUR_MOMENTUM, [100]),
                                      (EMA20_50_1H, [100]*49)])
def test_insufficient_data_is_technical_and_never_a_wait(name, closes):
    result = decide(name, closes)
    assert result["status"] == "DATA_UNAVAILABLE" and result["decision"] is None
    assert result["reason_codes"] == ["INSUFFICIENT_HISTORY"]


@pytest.mark.parametrize("name", BENCHMARKS)
def test_closed_only_and_future_prices_are_not_even_read(name):
    rows = candles(list(range(100, 160)))
    before = copy.deepcopy(rows)
    decision_at = 60*HOUR_MS+120_000
    expected = benchmark_decision(name, rows, decision_at=decision_at)
    # No OHLCV in these rows: touching future/forming prices would fail parsing.
    future = [dict(time=60*HOUR_MS, close_time=61*HOUR_MS-1),
              dict(time=61*HOUR_MS, close_time=62*HOUR_MS-1)]
    assert benchmark_decision(name, rows+future, decision_at=decision_at) == expected
    assert expected["reference_at"] == 60*HOUR_MS
    assert expected["base_price"] == 159
    assert rows == before


def test_boundary_includes_just_closed_hour_and_excludes_forming_hour():
    rows = candles([100, 101])
    assert benchmark_decision(PREVIOUS_HOUR_MOMENTUM, rows, decision_at=2*HOUR_MS)["decision"] == "UP"
    result = benchmark_decision(PREVIOUS_HOUR_MOMENTUM, rows, decision_at=2*HOUR_MS-1)
    assert result["decision"] is None  # Second bar has not fully closed yet.


@pytest.mark.parametrize("kind", ["gap", "reverse", "duplicate", "stale", "invalid_price", "bool_price", "wrong_interval"])
def test_invalid_or_stale_closed_history_is_unavailable(kind):
    rows = candles([100]*60)
    if kind == "gap":
        rows.pop(20)
    elif kind == "reverse":
        rows.reverse()
    elif kind == "duplicate":
        rows.insert(20, rows[20])
    elif kind == "stale":
        rows.pop()
    elif kind == "invalid_price":
        rows[-1]["close"] = float("nan")
    elif kind == "bool_price":
        rows[-1]["close"] = True
    else:
        rows[-1]["close_time"] -= 1
    result = benchmark_decision(EMA20_50_1H, rows, decision_at=60*HOUR_MS)
    assert result["status"] == "DATA_UNAVAILABLE" and result["decision"] is None


@pytest.mark.parametrize("data", [None, "not candles", [{}]])
def test_malformed_inputs_do_not_produce_a_vote(data):
    result = benchmark_decision(ALWAYS_UP, data, decision_at=HOUR_MS)
    assert result["status"] == "DATA_UNAVAILABLE" and result["decision"] is None


def test_invalid_benchmark_identity_or_decision_time_is_a_caller_error():
    for name, stamp in (("UNKNOWN", HOUR_MS), (ALWAYS_UP, True), (ALWAYS_UP, -1)):
        with pytest.raises(ValueError):
            benchmark_decision(name, [], decision_at=stamp)


def test_three_benchmarks_compare_on_one_shared_snapshot_and_outcome():
    snapshot = candles(list(range(100, 160)))
    stamp = 60*HOUR_MS
    evaluated = []
    for name in BENCHMARKS:
        decision = benchmark_decision(name, snapshot, decision_at=stamp)
        evaluated.append(dict(symbol="BTCUSDT", horizon=4, decision_at=stamp, strategy_id=name,
            **evaluate_decision(decision["decision"], decision["base_price"], 165., .2)))
    for challenger in evaluated[1:]:
        result = compare_results([challenger], [evaluated[0]])
        assert result["horizon"] == 4 and result["symbol"] == "BTCUSDT"
        assert result["accuracy_excess_pp"] == 0

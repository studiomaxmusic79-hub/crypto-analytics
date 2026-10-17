"""Research intents use closed ETH candles, never future execution facts."""
import copy

import pytest

from app.trading_baselines import ETH_MOMENTUM_LONG_ONLY_V1, eth_momentum_long_only

BAR = 900_000


def candles(closes):
    return [dict(time=i * BAR, close_time=(i + 1) * BAR - 1,
                 open=c, high=c + 1, low=c - 1, close=c, volume=10)
            for i, c in enumerate(closes)]


def decide(closes, long=False, lookback=4):
    return eth_momentum_long_only(candles(closes), lookback_bars=lookback,
                                 has_position=long, decision_at=len(closes) * BAR)


@pytest.mark.parametrize("last,long,intent", [(105, False, "BUY"), (95, False, "WAIT"),
    (100, False, "WAIT"), (105, True, "HOLD"), (95, True, "SELL"), (100, True, "SELL")])
def test_position_intents(last, long, intent):
    result = decide([100, 101, 102, 103, last], long)
    assert result["status"] == "OK" and result["intent"] == intent
    assert result["baseline_id"] == ETH_MOMENTUM_LONG_ONLY_V1
    assert result["momentum"] == last - 100


def test_exact_lookback_uses_n_plus_one_bars_not_last_n_bars():
    closes = [110, 90, 95, 98, 100]
    assert decide(closes)["intent"] == "WAIT"  # 100 - 110, not 100 - 90.
    assert decide(closes, lookback=3)["intent"] == "BUY"
    assert decide(closes, lookback=1)["momentum"] == 2


@pytest.mark.parametrize("long", [False, True])
def test_insufficient_history_is_technical_never_wait_or_hold(long):
    result = decide([100] * 4, long)
    assert result["status"] == "DATA_UNAVAILABLE" and result["intent"] is None
    assert result["reason_codes"] == ["INSUFFICIENT_HISTORY"]


def test_forming_future_prices_and_unused_old_history_are_not_read():
    rows = candles([100, 101, 102, 103, 104])
    before = copy.deepcopy(rows)
    args = dict(lookback_bars=4, has_position=False, decision_at=5 * BAR + 1000)
    expected = eth_momentum_long_only(rows, **args)
    # Intentionally absent OHLCV: touching forming/future prices would fail.
    ignored = [dict(close_time=6 * BAR - 1), dict(close_time=7 * BAR - 1)]
    assert eth_momentum_long_only(rows + ignored, **args) == expected
    assert eth_momentum_long_only([dict(close_time=-1)] + rows, **args) == expected
    assert rows == before and expected["reference_at"] == 5 * BAR


def test_boundary_just_before_close_excludes_bar():
    rows = candles([100, 101, 102, 103, 104])
    result = eth_momentum_long_only(rows, lookback_bars=4, has_position=False, decision_at=5 * BAR - 1)
    assert result["intent"] is None
    assert result["reason_codes"] == ["INSUFFICIENT_HISTORY"]


def test_eth_only_deterministic_and_does_not_mutate_input():
    rows = candles([100, 101, 102, 103, 104])
    before = copy.deepcopy(rows)
    args = dict(lookback_bars=4, has_position=True, decision_at=5 * BAR)
    assert eth_momentum_long_only(rows, **args) == eth_momentum_long_only(rows, **args)
    assert rows == before


@pytest.mark.parametrize("kind", ["gap", "reverse", "duplicate", "stale", "invalid_price", "wrong_interval"])
def test_invalid_closed_history_is_technical(kind):
    rows = candles([100] * 6)
    if kind == "gap":
        rows.pop(3)
    elif kind == "reverse":
        rows.reverse()
    elif kind == "duplicate":
        rows[-2] = rows[-1]
    elif kind == "stale":
        rows.pop()
    elif kind == "invalid_price":
        rows[-1]["close"] = float("nan")
    else:
        rows[-1]["close_time"] -= 1
    result = eth_momentum_long_only(rows, lookback_bars=4, has_position=False, decision_at=6 * BAR)
    assert result["status"] == "DATA_UNAVAILABLE" and result["intent"] is None


@pytest.mark.parametrize("rows", [None, "candles", [{}]])
def test_invalid_candle_collection(rows):
    assert eth_momentum_long_only(rows, lookback_bars=4, has_position=False, decision_at=5 * BAR)["intent"] is None


@pytest.mark.parametrize("change", [dict(lookback_bars=0), dict(lookback_bars=True), dict(lookback_bars=1.5),
    dict(has_position="LONG"), dict(has_position=1), dict(decision_at=-1), dict(decision_at=True)])
def test_configuration_errors_are_explicit(change):
    args = dict(lookback_bars=4, has_position=False, decision_at=5 * BAR)
    with pytest.raises(ValueError):
        eth_momentum_long_only([], **{**args, **change})

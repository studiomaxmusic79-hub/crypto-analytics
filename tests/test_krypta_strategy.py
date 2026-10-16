"""Synthetic deterministic cases only; no historical backtest or network."""
import copy
from datetime import datetime, timezone
import json

import pytest

from app import analytics
from app import krypta_strategy as k
from app import strategies as old


T = int(datetime(2026, 10, 16, 13, tzinfo=timezone.utc).timestamp()*1000)


def history(interval, count=None, *, slope=0., base=1000., width=4., reference=T):
    count = k.WINDOW_BARS[interval] if count is None else count
    delta = k.INTERVAL_MS[interval]
    end = reference // delta * delta
    rows = []
    for i in range(count):
        start, price = end-(count-i)*delta, base+slope*i
        rows.append(dict(time=start, open=price, high=price+width, low=price-width,
                         close=price, volume=10., close_time=start+delta-1))
    return rows


def snapshot(direction="WAIT"):
    rows = {interval:history(interval) for interval in k.INTERVAL_MS}
    if direction != "WAIT":
        slope = 2. if direction == "UP" else -2.
        rows["1h"] = history("1h", slope=slope, base=1000. if slope > 0 else 3000., width=.25)
        rows["15m"] = history("15m", slope=slope, width=.25)
    return rows


def analyze(rows=None, reference=T):
    return k.analyze(snapshot() if rows is None else rows, reference_time=reference)


def fixed_trends(monkeypatch, t1=.5, t4=0.):
    def ema(values, period):
        trend = t1 if len(values) == 500 else t4
        return 1000.+trend*8 if period == 20 else 1000.
    monkeypatch.setattr(k, "ema", ema)


def fixed_momentum(rows, momentum):
    # Four increments, five closes; keep all 14 true ranges equal to 8.
    for i in range(1, 5):
        price = 1000.+momentum*8*i/4
        rows["15m"][-5+i].update(open=price, high=price+4, low=price-4, close=price)


def test_identity_and_frozen_specification():
    assert (k.STRATEGY_ID, k.STRATEGY_VERSION) == ("krypta_mtf", "v1")
    assert (k.TREND_THRESHOLD, k.MOMENTUM_THRESHOLD) == (.5, 1.)
    assert k.WINDOW_BARS == {"15m":15, "1h":500, "4h":200}
    assert k.ema is analytics.ema


@pytest.mark.parametrize("direction", ["UP", "DOWN", "WAIT"])
def test_real_ohlcv_up_down_wait(direction):
    result = analyze(snapshot(direction))
    assert result["status"] == "OK"
    assert result["signal"] == direction
    assert result["reason_codes"][-1] == "SIGNAL_"+direction
    assert result["reasons"]
    assert "confidence" not in result and "probability" not in result
    assert "score" not in result
    assert all(isinstance(value, bool) for branch in result["conditions"].values() for value in branch.values())


@pytest.mark.parametrize("trend,momentum,expected", [
    (.5,1.,"UP"), (.5-1e-6,1.,"WAIT"), (.5+1e-6,1.,"UP"),
    (-.5,-1.,"DOWN"), (-.5+1e-6,-1.,"WAIT"), (-.5-1e-6,-1.,"DOWN"),
])
def test_exact_1h_trend_thresholds_and_both_sides(monkeypatch, trend, momentum, expected):
    fixed_trends(monkeypatch, t1=trend)
    rows = snapshot()
    fixed_momentum(rows,momentum)
    result = analyze(rows)
    assert result["T1h"] == pytest.approx(trend)
    assert result["signal"] == expected


@pytest.mark.parametrize("momentum,expected", [
    (1.,"UP"), (1.-1e-6,"WAIT"), (1.+1e-6,"UP"),
    (-1.,"DOWN"), (-1.+1e-6,"WAIT"), (-1.-1e-6,"DOWN"),
])
def test_exact_momentum_thresholds_and_both_sides(monkeypatch,momentum,expected):
    fixed_trends(monkeypatch,t1=.5 if momentum > 0 else -.5)
    rows = snapshot()
    fixed_momentum(rows,momentum)
    result = analyze(rows)
    assert result["M15m"] == pytest.approx(momentum)
    if abs(momentum) == 1.:
        assert result["M15m"] == momentum
    assert result["signal"] == expected


@pytest.mark.parametrize("trend,context,momentum,expected", [
    (.5,-.5,1.,"WAIT"), (.5,-.5-1e-6,1.,"WAIT"), (.5,-.5+1e-6,1.,"UP"),
    (-.5,.5,-1.,"WAIT"), (-.5,.5+1e-6,-1.,"WAIT"), (-.5,.5-1e-6,-1.,"DOWN"),
    (.5,0.,1.,"UP"), (-.5,0.,-1.,"DOWN"),
    (.5,.5,1.,"UP"), (-.5,-.5,-1.,"DOWN"),
    (0.,.5,1.,"WAIT"), (.5,.5,0.,"WAIT"), (.5,.5,-1.,"WAIT"),
])
def test_4h_filter_boundaries_neutrality_and_no_independent_vote(monkeypatch,trend,context,momentum,expected):
    fixed_trends(monkeypatch,t1=trend,t4=context)
    rows = snapshot()
    fixed_momentum(rows,momentum)
    result = analyze(rows)
    assert result["signal"] == expected
    if context in (-.5,.5):
        assert result["T4h"] == context


def test_momentum_uses_five_closes_for_exactly_one_hour(monkeypatch):
    fixed_trends(monkeypatch)
    rows = snapshot()
    fixed_momentum(rows,1.)
    result = analyze(rows)
    values = rows["15m"]
    assert len(values[-5:]) == 5
    assert values[-1]["close"]-values[-5]["close"] == 8.
    assert values[-1]["close"]-values[-4]["close"] == 6.
    assert result["M15m"] == 1.  # Four closes would incorrectly produce .75.
    indicators = result["indicators"]["15m"]
    assert indicators["reference_close"] == values[-5]["close"]
    assert indicators["momentum_reference_close_at"] == T-k.INTERVAL_MS["1h"]
    assert result["signal"] == "UP"


def test_indicators_match_existing_ema_and_sma_true_range_atr():
    rows = snapshot("UP")
    # Add a price gap that must contribute to true range.
    rows["4h"][-1].update(open=1100., high=1102., low=1098., close=1100.)
    result = analyze(rows)
    for interval in k.INTERVAL_MS:
        window = rows[interval]
        expected_atr = sum(max(b["high"]-b["low"], abs(b["high"]-a["close"]),
                               abs(b["low"]-a["close"]))
                           for a,b in zip(window[-15:-1],window[-14:]))/14
        assert result["indicators"][interval]["atr14"] == expected_atr
        if interval != "15m":
            closes = [row["close"] for row in window]
            assert result["indicators"][interval]["ema20"] == analytics.ema(closes,20)
            assert result["indicators"][interval]["ema50"] == analytics.ema(closes,50)
    assert result["indicators"]["4h"]["atr14"] == (13*8.+102.)/14


@pytest.mark.parametrize("interval", k.INTERVAL_MS)
def test_one_missing_warmup_candle_is_not_wait(interval):
    rows = snapshot()
    rows[interval].pop(0)
    result = analyze(rows)
    assert result["status"] == "DATA_UNAVAILABLE"
    assert result["signal"] is None
    assert result["error_interval"] == interval
    assert result["reason_codes"] == ["INSUFFICIENT_HISTORY"]


@pytest.mark.parametrize("interval", k.INTERVAL_MS)
def test_zero_atr_is_technical_skip_even_for_otherwise_normal_wait(interval):
    rows = snapshot()
    rows[interval] = history(interval,width=0.)
    result = analyze(rows)
    assert result["status"] == "SKIPPED" and result["signal"] is None
    assert result["error_interval"] == interval
    assert result["reason_codes"] == ["ZERO_ATR"]


@pytest.mark.parametrize("interval", k.INTERVAL_MS)
def test_nonfinite_atr_is_unavailable_and_result_stays_json_safe(interval):
    rows = snapshot()
    for row in rows[interval][-14:]:
        row.update(high=1e308,low=1.)
    result = analyze(rows)
    assert result["status"] == "DATA_UNAVAILABLE" and result["signal"] is None
    assert result["reason_codes"] == ["INVALID_ATR"]
    assert result["error_interval"] == interval
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize("change", [{"close":float("nan")},{"high":float("inf")},
    {"low":0.},{"volume":-1.},{"open":True},{"high":999.},{"close":"bad"}])
def test_invalid_ohlcv_cannot_turn_into_wait(change):
    rows = snapshot()
    rows["1h"][-1].update(change)
    result = analyze(rows)
    assert result["status"] == "DATA_UNAVAILABLE" and result["signal"] is None
    assert result["reason_codes"] == ["INVALID_OHLCV"]
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize("interval", k.INTERVAL_MS)
@pytest.mark.parametrize("kind", ["gap","duplicate","order","stale","wrong_interval"])
def test_incomplete_or_wrong_timeframe_history_is_rejected(interval,kind):
    rows = snapshot()
    rows[interval] = history(interval,k.WINDOW_BARS[interval]+1)
    if kind == "gap": del rows[interval][-5]
    elif kind == "duplicate": rows[interval][-5] = copy.deepcopy(rows[interval][-6])
    elif kind == "order": rows[interval][-5],rows[interval][-6] = rows[interval][-6],rows[interval][-5]
    elif kind == "stale": rows[interval].pop()
    else: rows[interval][-1]["close_time"] -= 1
    result = analyze(rows)
    assert result["status"] == "DATA_UNAVAILABLE" and result["signal"] is None
    assert result["error_interval"] == interval


def test_future_and_forming_rows_are_ignored_in_every_timeframe():
    rows = snapshot("UP")
    expected = analyze(rows)
    for interval,delta in k.INTERVAL_MS.items():
        start = T//delta*delta
        for future_start in (start,start+delta):
            # Future OHLCV must not be read, even if nonsensical.
            rows[interval].append(dict(time=future_start,close_time=future_start+delta-1,
                                       open=1e100,high=float("inf"),low=-1.,close=float("nan")))
    assert analyze(rows) == expected


def test_13_utc_uses_4h_close_at_12_not_forming_12_to_16():
    rows = snapshot("UP")
    twelve = T-3_600_000
    latest = rows["4h"][-1]
    assert latest["time"] == twelve-14_400_000
    assert latest["close_time"] == twelve-1
    rows["4h"].append(dict(time=twelve,close_time=twelve+14_400_000-1,
                           open=1000.,high=1e100,low=1.,close=1.))
    result = analyze(rows)
    assert result["T4h"] == 0.
    assert result["signal"] == "UP"
    assert result["last_close_at"] == {"1h":T,"4h":twelve,"15m":T}
    assert result["last_candle_times"]["4h"] == {"time":twelve-14_400_000,"close_time":twelve-1}


def test_exact_4h_closing_boundary_includes_newly_closed_bar():
    twelve = T-3_600_000
    before = twelve-1
    rows = {interval:history(interval,reference=twelve) for interval in k.INTERVAL_MS}
    at_boundary = analyze(rows,reference=twelve)
    assert at_boundary["last_close_at"]["4h"] == twelve
    assert at_boundary["last_close_at"]["1h"] == twelve
    earlier = analyze({interval:history(interval,reference=before) for interval in k.INTERVAL_MS},reference=before)
    assert earlier["last_close_at"]["4h"] == twelve-14_400_000


def test_more_history_does_not_change_fixed_windows_or_mutate_input():
    rows = {interval:history(interval,k.WINDOW_BARS[interval]+10,slope=.1)
            for interval in k.INTERVAL_MS}
    before = copy.deepcopy(rows)
    expected = analyze({interval:values[-k.WINDOW_BARS[interval]:] for interval,values in rows.items()})
    assert analyze(rows) == expected
    assert rows == before
    first, second = analyze(rows), analyze(rows)
    assert first == second
    first["indicators"]["1h"]["ema20"] = -999.
    assert analyze(rows) == second
    json.dumps(second,allow_nan=False)


@pytest.mark.parametrize("reference", [None,True,13.0,"13:00",-1])
def test_invalid_reference_time_has_no_signal(reference):
    result = analyze(reference=reference)
    assert result["status"] == "DATA_UNAVAILABLE" and result["signal"] is None
    assert result["reason_codes"] == ["INVALID_REFERENCE_TIME"]


@pytest.mark.parametrize("interval", k.INTERVAL_MS)
def test_missing_timeframe_is_not_normal_wait(interval):
    rows = snapshot()
    del rows[interval]
    result = analyze(rows)
    assert result["status"] == "DATA_UNAVAILABLE" and result["signal"] is None
    assert result["reason_codes"] == ["MISSING_TIMEFRAME"]


def test_nonfinite_ema_is_not_wait(monkeypatch):
    monkeypatch.setattr(k,"ema",lambda values,period: float("nan"))
    result = analyze()
    assert result["status"] == "DATA_UNAVAILABLE" and result["signal"] is None
    assert result["reason_codes"] == ["INVALID_EMA"]
    json.dumps(result,allow_nan=False)


def test_new_thresholds_do_not_modify_existing_strategy_parameters():
    from app import strategies_v2
    assert old.VERSION == "v1"
    assert old.MOMENTUM_THRESHOLD_PCT == .20
    assert strategies_v2.TREND_THRESHOLD == .25

"""Frozen synthetic v2 specifications; no Binance I/O or historical backtest."""
import copy
import itertools
import json

import pytest

from app import strategies as v1
from app import strategies_v2 as v2
from test_strategies import END, NOW, candle, history, vote


def analyze(rows=None, **kwargs):
    return v2.analyze_strategies_v2(history() if rows is None else rows,
                                   now=kwargs.get("now", NOW),
                                   confirmed_at=kwargs.get("confirmed_at", NOW))


def reentry(direction="UP", **changes):
    rows = history()
    values = (dict(open=986., high=996., low=984., close=988.) if direction == "UP"
              else dict(open=1014., high=1016., low=1004., close=1012.))
    rows[-1].update(values)
    rows[-1].update(changes)
    return rows


def regime(name="RANGE"):
    return {**vote(v2.REGIME_ID, direction=None, score=0.), "regime": name}


def votes(a="UP", b="UP", c="WAIT"):
    return [vote(v1.STRATEGY_A, a), vote(v1.STRATEGY_B, b), vote(v2.STRATEGY_C, c)]


def test_frozen_constants_and_versions():
    assert (v2.TREND_THRESHOLD, v2.VOLATILITY_LOOKBACK, v2.VOLATILITY_MULTIPLIER,
            v2.BAND_ATR_MULTIPLIER, v2.MIN_RETURN_ATR) == (.25, 168, 2., 2., 1.)
    assert v1.VERSION == v2.REGIME_VERSION == v2.C_VERSION == "v1"
    assert v2.CONSENSUS_VERSION == "v2"
    assert v1.MOMENTUM_THRESHOLD_PCT == .20


@pytest.mark.parametrize("fast,expected", [
    (1002., "TREND_UP"), (1002.-1e-6, "RANGE"), (1002.+1e-6, "TREND_UP"),
    (998., "TREND_DOWN"), (998.+1e-6, "RANGE"), (998.-1e-6, "TREND_DOWN"),
    (1000., "RANGE"),
])
def test_regime_trend_threshold_boundaries(monkeypatch, fast, expected):
    monkeypatch.setattr(v1, "ema", lambda values, period: fast if period == 20 else 1000.)
    result = v2.analyze_market_regime(history(), now=NOW, confirmed_at=NOW)
    assert result["status"] == "OK"
    assert result["regime"] == expected
    assert result["indicators"]["atr14"] == 8.
    assert result["score"] == (fast-1000.)/8.


@pytest.mark.parametrize("tr,expected", [(120.-1e-6,"RANGE"), (120.,"HIGH_VOLATILITY"),
                                        (120.+1e-6,"HIGH_VOLATILITY")])
def test_volatility_threshold_and_exclusion_of_current_from_baseline(tr, expected):
    rows = history()
    rows[-1].update(high=1000.+tr/2, low=1000.-tr/2)
    result = analyze(rows)["regime"]
    assert result["regime"] == expected
    assert result["indicators"]["volatility_baseline"] == 8./1000.
    assert result["indicators"]["atr14"] == pytest.approx((13*8.+tr)/14)
    if tr == 120.:
        assert result["indicators"]["volatility_ratio"] == 2.


@pytest.mark.parametrize("fast", [1004., 996.])
def test_high_volatility_precedes_both_trends(monkeypatch, fast):
    rows = history()
    rows[-1].update(high=1060., low=940.)
    monkeypatch.setattr(v1, "ema", lambda values, period: fast if period == 20 else 1000.)
    result = analyze(rows)
    assert abs(result["regime"]["score"]) == .25
    assert result["regime"]["regime"] == "HIGH_VOLATILITY"
    assert result["C"]["reason_codes"] == ["NOT_RANGE"]
    assert result["consensus_v2"]["reason_codes"] == ["VOLATILITY_GUARD"]


def test_volatility_uses_168_past_rolling_atrs_and_median(monkeypatch):
    rows = history()
    rows[-60].update(high=1100., low=900.)
    seen = []
    original = v2.median

    def median(values):
        seen.extend(values)
        return original(values)

    monkeypatch.setattr(v2, "median", median)
    result = v2.analyze_market_regime(rows, now=NOW, confirmed_at=NOW)
    expected = []
    for i in range(331, 499):
        true_ranges = [max(rows[j]["high"]-rows[j]["low"],
                           abs(rows[j]["high"]-rows[j-1]["close"]),
                           abs(rows[j]["low"]-rows[j-1]["close"])) for j in range(i-13,i+1)]
        expected.append(sum(true_ranges)/14/rows[i]["close"])
    assert seen == expected
    assert len(seen) == 168
    assert result["indicators"]["volatility_baseline"] == .008
    assert sum(seen)/len(seen) > .008  # A mean would give another result.


def test_real_ohlcv_produces_each_regime():
    cases = [(history(slope=2.), "TREND_UP"), (history(slope=-2., base=3000.), "TREND_DOWN"),
             (history(), "RANGE")]
    shock = history()
    shock[-1].update(high=1060., low=940.)
    cases.append((shock,"HIGH_VOLATILITY"))
    for rows, expected in cases:
        assert analyze(rows)["regime"]["regime"] == expected


@pytest.mark.parametrize("direction", ["UP", "DOWN"])
def test_c_real_reentry_and_previous_bands(direction):
    result = analyze(reentry(direction))
    c = result["C"]
    assert result["regime"]["regime"] == "RANGE"
    assert c["direction"] == direction
    assert c["strategy_id"] == v2.STRATEGY_C
    assert c["indicators"]["previous_ema20"] == 1000.
    assert c["indicators"]["previous_atr14"] == 8.
    assert c["indicators"]["lower_band"] == 984.
    assert c["indicators"]["upper_band"] == 1016.
    assert c["score"] == (1000.-c["base_price"])/8.
    assert result["consensus_v2"]["direction"] == direction
    assert result["consensus_v2"]["indicators"]["agreement_pct"] is None


@pytest.mark.parametrize("changes,expected", [
    ({"low":984.}, "UP"), ({"low":984.+1e-6}, "WAIT"),
    ({"low":984.-1e-6}, "UP"),
    ({"close":984.}, "WAIT"),
    ({"close":984.+1e-6,"open":984.}, "UP"),
    ({"close":992.}, "UP"), ({"close":992.+1e-6}, "WAIT"),
    ({"close":992.-1e-6}, "UP"),
    ({"open":988.}, "WAIT"), ({"open":988.-1e-6}, "UP"),
    ({"open":988.+1e-6}, "WAIT"),
    ({"high":1016.}, "WAIT"), ({"high":1016.-1e-6}, "UP"),
    ({"high":1016.+1e-6}, "WAIT"),
])
def test_c_up_all_strict_and_inclusive_boundaries(changes, expected):
    assert analyze(reentry(**changes))["C"]["direction"] == expected


@pytest.mark.parametrize("changes,expected", [
    ({"high":1016.}, "DOWN"), ({"high":1016.-1e-6}, "WAIT"),
    ({"high":1016.+1e-6}, "DOWN"),
    ({"close":1016.}, "WAIT"),
    ({"close":1016.-1e-6,"open":1016.}, "DOWN"),
    ({"close":1008.}, "DOWN"), ({"close":1008.-1e-6}, "WAIT"),
    ({"close":1008.+1e-6}, "DOWN"),
    ({"open":1012.}, "WAIT"), ({"open":1012.+1e-6}, "DOWN"),
    ({"open":1012.-1e-6}, "WAIT"),
    ({"low":984.}, "WAIT"), ({"low":984.+1e-6}, "DOWN"),
    ({"low":984.-1e-6}, "WAIT"),
])
def test_c_down_all_strict_and_inclusive_boundaries(changes, expected):
    assert analyze(reentry("DOWN", **changes))["C"]["direction"] == expected


def test_c_wait_reason_codes_and_no_trade_in_other_regimes():
    assert analyze()["C"]["reason_codes"] == ["NO_REENTRY"]
    assert analyze(reentry(high=1016.))["C"]["reason_codes"] == ["TWO_SIDED_EXTREME"]
    for rows in (history(slope=2.), history(slope=-2., base=3000.)):
        c = analyze(rows)["C"]
        assert c["status"] == "OK" and c["direction"] == "WAIT"
        assert c["reason_codes"] == ["NOT_RANGE"]


@pytest.mark.parametrize("name", v2.REGIMES)
def test_consensus_exhaustive_routing_table(name):
    for a, b, c in itertools.product(v1.DIRECTIONS, repeat=3):
        result = v2.consensus_v2(regime(name), votes(a,b,c))
        expected = ("UP" if name == "TREND_UP" and a == b == "UP" else
                    "DOWN" if name == "TREND_DOWN" and a == b == "DOWN" else
                    c if name == "RANGE" else "WAIT")
        assert result["status"] == "OK"
        assert result["direction"] == expected
        assert result["strategy_id"] == "consensus"
        assert result["strategy_version"] == "v2"
        assert result["score"] == {"UP":1.,"DOWN":-1.,"WAIT":0.}[expected]
        selected = result["indicators"]["selected_count"]
        assert selected == (2 if name.startswith("TREND_") else 1 if name == "RANGE" else 0)
        if selected < 2:
            assert result["indicators"]["agreement_pct"] is None


@pytest.mark.parametrize("name,index", [("TREND_UP",0),("TREND_UP",1),
                                        ("TREND_DOWN",0),("TREND_DOWN",1),("RANGE",2)])
@pytest.mark.parametrize("invalid", ["missing","SKIPPED","DATA_UNAVAILABLE","score","direction","version"])
def test_consensus_never_reduces_required_quorum(name, index, invalid):
    rows = votes("DOWN","DOWN","UP")
    if invalid == "missing":
        del rows[index]
    elif invalid in ("SKIPPED","DATA_UNAVAILABLE"):
        rows[index].update(status=invalid, direction=None, score=None)
    elif invalid == "score":
        rows[index]["score"] = float("nan")
    elif invalid == "direction":
        rows[index]["direction"] = "BUY"
    else:
        rows[index]["strategy_version"] = "unknown"
    result = v2.consensus_v2(regime(name), rows)
    assert result["status"] == "DATA_UNAVAILABLE"
    assert result["direction"] is result["score"] is None
    assert result["indicators"]["agreement_pct"] is None


def test_consensus_ignores_unselected_unavailable_strategies():
    broken = votes()
    broken[2].update(status="SKIPPED", direction=None, score=None)
    assert v2.consensus_v2(regime("TREND_UP"),broken)["direction"] == "UP"
    assert v2.consensus_v2(regime("RANGE"),[vote(v2.STRATEGY_C,"DOWN")])["direction"] == "DOWN"
    result = v2.consensus_v2(regime("HIGH_VOLATILITY"),[])
    assert result["status"] == "OK" and result["direction"] == "WAIT"
    assert result["reason_codes"] == ["VOLATILITY_GUARD"]


def test_consensus_agreement_is_not_success_probability():
    assert v2.consensus_v2(regime("TREND_UP"),votes())["indicators"]["agreement_pct"] == 100
    result = v2.consensus_v2(regime("TREND_UP"),votes(b="WAIT"))
    assert result["indicators"]["agreement_pct"] == 50
    assert "probability" not in json.dumps(result)
    assert "confidence" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [("base_price",900.),
    ("input_close_at", END-v1.HOUR_MS),
    ("due_at", {1:END+2*v1.HOUR_MS,4:END+5*v1.HOUR_MS}),
    ("thresholds_pct",{1:.2,4:.4})])
def test_consensus_rejects_mixed_selected_contexts(field,value):
    rows = votes()
    rows[0][field] = value
    if field == "input_close_at":
        rows[0]["due_at"] = {h:value+h*v1.HOUR_MS for h in (1,4)}
    if field == "due_at":
        rows[0]["input_close_at"] += v1.HOUR_MS
    with pytest.raises(ValueError,match="same hourly context"):
        v2.consensus_v2(regime("TREND_UP"),rows)


def test_consensus_duplicate_and_foreign_votes_rejected():
    a = vote(v1.STRATEGY_A)
    for rows in ([a,a],[vote("consensus")],[vote("unknown")]):
        with pytest.raises(ValueError,match="distinct A/B/C"):
            v2.consensus_v2(regime(),rows)


@pytest.mark.parametrize("change", [{"status":"DATA_UNAVAILABLE"},{"regime":"UNKNOWN"},
    {"strategy_version":"future"},{"base_price":0.},{"score":float("nan")},
    {"input_close_at":END+1}])
def test_bad_regime_never_becomes_a_normal_wait(change):
    result = v2.consensus_v2({**regime("HIGH_VOLATILITY"),**change},[])
    assert result["status"] == "DATA_UNAVAILABLE"
    assert result["direction"] is None


@pytest.mark.parametrize("case", ["short","gap","duplicate","order","stale","unconfirmed","invalid"])
def test_invalid_snapshot_cannot_produce_v2_signal(case):
    rows = history()
    kwargs = {}
    if case == "short": rows.pop(0)
    elif case == "gap": rows[10]["time"] -= v1.HOUR_MS
    elif case == "duplicate": rows[10] = copy.deepcopy(rows[9])
    elif case == "order": rows[9],rows[10] = rows[10],rows[9]
    elif case == "stale": kwargs["now"] = NOW+v1.HOUR_MS
    elif case == "unconfirmed": kwargs["confirmed_at"] = END-1
    else: rows[10]["high"] = float("nan")
    result = analyze(rows,**kwargs)
    for key in ("regime","C","consensus_v2"):
        assert result[key]["status"] == "DATA_UNAVAILABLE"
        assert result[key]["direction"] is None


def test_zero_atr_and_zero_historical_volatility_are_unavailable():
    result = analyze(history(width=0.))
    assert result["regime"]["reason_codes"] == ["ZERO_ATR"]
    assert result["C"]["status"] == result["consensus_v2"]["status"] == "DATA_UNAVAILABLE"
    rows = history(width=0.)
    rows[-1].update(high=1001., low=999.)
    assert analyze(rows)["regime"]["reason_codes"] == ["ZERO_VOLATILITY_BASELINE"]


def test_c_zero_previous_atr_is_technical_skip():
    rows = history()
    for row in rows[-15:-1]:
        row.update(open=1000.,high=1000.,low=1000.,close=1000.)
    result = analyze(rows)
    assert result["regime"]["status"] == "OK"
    assert result["C"]["status"] == "SKIPPED"
    assert result["C"]["reason_codes"] == ["ZERO_PREVIOUS_ATR"]
    assert result["consensus_v2"]["status"] == "DATA_UNAVAILABLE"


def test_forming_future_bars_are_excluded_without_lookahead():
    rows = reentry()
    expected = analyze(rows)
    forming, future = candle(END,1e100), candle(END+v1.HOUR_MS,1e200)
    assert analyze(rows+[forming,future]) == expected
    forming["close"] = future["close"] = float("nan")
    assert analyze(rows+[forming,future]) == expected
    assert analyze(history(499)+[forming])["regime"]["status"] == "DATA_UNAVAILABLE"


def test_current_candle_cannot_change_frozen_c_bands():
    first = analyze(reentry())["C"]["indicators"]
    changed = analyze(reentry(open=987.,close=990.,low=980.,high=1003.))["C"]["indicators"]
    for key in ("previous_ema20","previous_atr14","lower_band","upper_band"):
        assert changed[key] == first[key]


def test_shared_context_existing_evaluation_and_wait_never_wins():
    for rows in (history(),reentry(),reentry("DOWN")):
        result = analyze(rows)
        for key in ("C","consensus_v2"):
            for field in v1.CONTEXT_FIELDS:
                assert result[key][field] == result["A"][field] == result["regime"][field]
            for h in (1,4):
                due = END+h*v1.HOUR_MS
                evaluated = v1.evaluate_result(result[key],h,candle(due-v1.HOUR_MS,1000.),
                                               now=due,confirmed_at=due)
                assert evaluated["status"] == "OK"
                if result[key]["direction"] == "WAIT":
                    assert evaluated["correct"] is None


def test_one_preparation_and_standalone_entry_points_match(monkeypatch):
    original = v1._prepare
    calls = []
    def prepare(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(v1,"_prepare",prepare)
    result = analyze(reentry())
    assert len(calls) == 1
    assert v2.analyze_c(reentry(),now=NOW,confirmed_at=NOW) == result["C"]
    assert v2.analyze_market_regime(reentry(),now=NOW,confirmed_at=NOW) == result["regime"]


def test_v1_results_unchanged_inputs_unmodified_and_outputs_json_safe():
    for rows in (history(),history(slope=2.),history(slope=-2.,base=3000.),
                 history(width=0.),history(499),reentry(),reentry("DOWN")):
        before = copy.deepcopy(rows)
        old = v1.analyze_strategies(rows,now=NOW,confirmed_at=NOW)
        result = analyze(rows)
        assert {key:result[key] for key in old} == old
        assert rows == before
        json.dumps(result,allow_nan=False)
    rows = history(510,slope=2.)
    assert analyze(rows) == analyze(rows[-500:])
    result = analyze(reentry())
    result["regime"]["indicators"]["volatility_ratio"] = 999.
    assert result["C"]["indicators"]["regime"]["indicators"]["volatility_ratio"] != 999.

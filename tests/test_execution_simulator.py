"""Small synthetic MARKET execution cases; no historical backtest or network."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact, ROUND_UP, localcontext

import pytest

from app.economic_evaluation import FillFact, InstrumentContext, evaluate_campaign
from app.execution_simulator import (ExecutionMetadata, ExecutionPolicy, ExecutionResult,
    ExecutionStatus, MinuteCandle, OrderIntent, simulate_market_order,
    ExitPolicy, ExitResult, ExitStatus, ExitTrigger, IntrabarPolicy, simulate_long_exit)

D = Decimal
INSTRUMENT = InstrumentContext("ETH", "USDT")
POLICY = ExecutionPolicy(1, 2, D("0.01"), D("0.02"), D("0.001"), "QUOTE", "market-v1")


def candle(start=60_000, price="100", closed=True):
    value = D(price)
    return MinuteCandle(start, start + 59_999, value, value, value, value, closed)


def intent(side="BUY", quantity="1", decision=0, identity="example-intent-1"):
    return OrderIntent(side, D(quantity), decision, identity)


def run(order=None, policy=POLICY, rows=None, instrument=INSTRUMENT):
    return simulate_market_order(intent() if order is None else order, policy, instrument,
                                 [candle()] if rows is None else rows)


@pytest.mark.parametrize("side,price", [("BUY", "103"), ("SELL", "97")])
def test_market_full_fill_has_adverse_spread_and_impact_price(side, price):
    result = run(intent(side, quantity="2"))
    assert isinstance(result, ExecutionResult) and result.status is ExecutionStatus.FILLED
    assert len(result.fills) == 1 and isinstance(result.fills[0], FillFact)
    fact = result.fills[0]
    assert fact.side == side and fact.executed_base_qty == D(2)
    assert fact.execution_price == D(price)
    assert fact.executed_quote_qty == D(price) * 2
    assert fact.execution_price > D(100) if side == "BUY" else fact.execution_price < D(100)
    assert fact.executed_at == 60_000


def test_both_delays_sum_before_selecting_minute():
    result = run(intent(decision=59_000), rows=[candle(), candle(120_000)])
    assert result.metadata.order_eligible_at == 62_000
    assert result.fills[0].executed_at == 120_000
    assert result.fills[0].executed_at >= result.metadata.order_eligible_at


@pytest.mark.parametrize("ready,expected", [(0, 60_000), (59_999, 60_000),
                                         (60_000, 120_000), (63_000, 120_000), (120_000, 180_000)])
def test_strictly_after_boundary_and_three_seconds_after_open(ready, expected):
    policy = replace(POLICY, processing_delay_seconds=0, submission_delay_seconds=0)
    result = run(intent(decision=ready), policy, [candle(i * 60_000) for i in range(4)])
    assert result.fills[0].executed_at == expected > ready


def test_delay_that_lands_exactly_on_boundary_does_not_use_that_open():
    result = run(intent(decision=57_000), rows=[candle(), candle(120_000)])
    assert result.metadata.order_eligible_at == 60_000
    assert result.fills[0].executed_at == 120_000


def test_earlier_and_later_candles_do_not_affect_execution_result():
    order = intent(decision=60_000)
    selected = candle(120_000)
    expected = run(order, rows=[selected])
    rows = [candle(0, "1000"), candle(60_000, "0.001"), selected, candle(180_000, "9999")]
    assert run(order, rows=rows) == expected
    rows[-1] = candle(180_000, "0.00001", closed=False)
    assert run(order, rows=rows) == expected


def test_selected_minute_high_low_close_are_not_execution_prices():
    expected = run()
    changed = replace(candle(), high=D(1000), low=D(1), close=D(900))
    assert run(rows=[changed]) == expected


@pytest.mark.parametrize("rows", [[], [candle(0)], [candle(120_000)],
                                  [candle(closed=False)], [candle(closed=False), candle(120_000)]])
def test_missing_or_forming_required_minute_never_falls_forward(rows):
    result = run(rows=rows)
    assert result.status is ExecutionStatus.DATA_UNAVAILABLE
    assert result.fills == () and result.metadata is None
    assert result.reason == "REQUIRED_CLOSED_MINUTE_UNAVAILABLE"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("asset,expected_fee", [("QUOTE", "0.206"), ("BASE", "0.002")])
def test_quote_and_base_commissions_are_per_fill_canonical_facts(side, asset, expected_fee):
    fact = run(intent(side, "2"), replace(POLICY, fee_asset_policy=asset)).fills[0]
    expected = (fact.executed_quote_qty if asset == "QUOTE" else fact.executed_base_qty) * POLICY.taker_fee_fraction
    assert fact.commission_amount == expected
    assert fact.commission_asset == ("USDT" if asset == "QUOTE" else "ETH")
    if side == "BUY":
        assert fact.commission_amount == D(expected_fee)


def test_zero_spread_impact_and_fee_preserves_reference_price():
    policy = replace(POLICY, half_spread_fraction=D(0), impact_fraction=D(0), taker_fee_fraction=D(0))
    fact = run(policy=policy).fills[0]
    assert fact.execution_price == D(100) and fact.commission_amount == D(0)


def test_economic_evaluator_consumes_fills_without_double_deducting_spread_impact():
    buy = run().fills[0]
    sell = run(intent("SELL", decision=60_000, identity="exit-intent"), rows=[candle(120_000, "110")]).fills[0]
    result = evaluate_campaign([buy, sell], instrument=INSTRUMENT, allocated_capital_quote=D(1000))
    assert result["status"] == "OK"
    assert buy.execution_price == D(103) and sell.execution_price == D("106.7")
    assert result["gross_pnl_quote"] == D("3.7")
    assert result["fees_quote_total"] == D("0.2097")
    assert result["net_pnl_quote"] == D("3.4903")
    assert result["return_pct_on_allocated_capital"] == D("0.34903")


def test_base_fee_fills_reconcile_using_inventory_not_gross_buy_sell_equality():
    policy = replace(POLICY, fee_asset_policy="BASE", taker_fee_fraction=D("0.01"))
    buy = run(intent(quantity="1.01"), policy).fills[0]
    sell = run(intent("SELL", "0.99", 60_000, "base-fee-exit"), policy, [candle(120_000, "110")]).fills[0]
    result = evaluate_campaign([buy, sell], instrument=INSTRUMENT, allocated_capital_quote=D(1000))
    assert result["remaining_base"] == D(0)
    assert result["position_closed"] and result["economic_evaluation_complete"]
    assert buy.executed_base_qty != sell.executed_base_qty
    assert result["net_pnl_quote"] == sell.executed_quote_qty - buy.executed_quote_qty


def test_decimal_products_are_exact_and_independent_of_global_context():
    order = intent(quantity="0.12345678901234567890123456789")
    policy = replace(POLICY, half_spread_fraction=D("0.00000000000000000000000000001"),
                     impact_fraction=D("0.00000000000000000000000000002"),
                     taker_fee_fraction=D("0.0001234567890123456789"))
    rows = [candle(price="2000.12345678901234567890123456789")]
    expected = run(order, policy, rows)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert run(order, policy, rows) == expected
        assert context.prec == 3 and context.traps[Inexact]
    with localcontext() as context:
        context.prec = 250
        fact = expected.fills[0]
        assert fact.execution_price == rows[0].open * (1 + policy.half_spread_fraction + policy.impact_fraction)
        assert fact.executed_quote_qty == order.quantity_base * fact.execution_price
        assert fact.commission_amount == fact.executed_quote_qty * policy.taker_fee_fraction


def test_deterministic_ids_logical_result_and_decimal_scale_normalization():
    first = run()
    assert run() == first
    assert run(intent(quantity="1.000"), replace(POLICY, impact_fraction=D("0.02000"))) == first
    fact = first.fills[0]
    assert fact.order_id.startswith("sim-order-") and fact.trade_id.startswith("sim-trade-")
    assert run(intent(identity="another-intent")).fills[0].identity != fact.identity
    assert run(policy=replace(POLICY, execution_model_version="market-v2")).fills[0].identity != fact.identity
    assert run(instrument=InstrumentContext("SOL", "EUR")).fills[0].identity != fact.identity


def test_audit_metadata_is_complete_and_inputs_results_are_immutable():
    result = run()
    metadata = result.metadata
    assert isinstance(metadata, ExecutionMetadata)
    assert metadata.intent == intent() and metadata.instrument == INSTRUMENT and metadata.policy == POLICY
    assert metadata.decision_at == 0 and metadata.order_eligible_at == 3000
    assert metadata.chosen_open_time == 60_000 and metadata.chosen_close_time == 119_999
    assert metadata.reference_price == D(100)
    for obj, field in [(result, "status"), (metadata, "reference_price"), (POLICY, "impact_fraction"),
                       (intent(), "side"), (candle(), "open"), (result.fills[0], "execution_price")]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)


@pytest.mark.parametrize("value", [D(0), D(-1), D("NaN"), D("Infinity"), 1.0, "1", True])
def test_invalid_quantity_returns_rejected(value):
    result = run(replace(intent(), quantity_base=value))
    assert result.status is ExecutionStatus.REJECTED and result.fills == ()


@pytest.mark.parametrize("change", [dict(processing_delay_seconds=-1), dict(submission_delay_seconds=1.5),
    dict(processing_delay_seconds=True), dict(half_spread_fraction=D(-1)), dict(impact_fraction=D(-1)),
    dict(taker_fee_fraction=D(-1)), dict(taker_fee_fraction=0.001), dict(impact_fraction=D("NaN")),
    dict(half_spread_fraction=D("Infinity")), dict(fee_asset_policy="BNB"), dict(execution_model_version=" ")])
def test_invalid_policy_returns_rejected(change):
    assert run(policy=replace(POLICY, **change)).status is ExecutionStatus.REJECTED


@pytest.mark.parametrize("spread", ["0.98", "1", "2"])
def test_nonpositive_synthetic_sell_price_rejected(spread):
    result = run(intent("SELL"), replace(POLICY, half_spread_fraction=D(spread)))
    assert result.status is ExecutionStatus.REJECTED and result.fills == ()


@pytest.mark.parametrize("change", [dict(side="WAIT"), dict(side="HOLD"), dict(decision_at=-1),
                                    dict(decision_at=True), dict(intent_id="")])
def test_invalid_intent_returns_rejected(change):
    assert run(replace(intent(), **change)).status is ExecutionStatus.REJECTED


@pytest.mark.parametrize("rows", [[candle(), candle()], [candle(120_000), candle()], None, "candles", [{}]])
def test_duplicate_unordered_or_invalid_candle_collection_rejected(rows):
    result = simulate_market_order(intent(), POLICY, INSTRUMENT, rows)
    assert result.status is ExecutionStatus.REJECTED


@pytest.mark.parametrize("change", [dict(open_time=60_001), dict(close_time=120_000), dict(open_time=True),
    dict(closed="true"), dict(open=D(0)), dict(high=D(-1)), dict(low=D(0)), dict(close=D(0)),
    dict(open=100.0), dict(close=D("NaN")), dict(high=D(99)), dict(low=D(101))])
def test_invalid_candle_data_rejected(change):
    assert run(rows=[replace(candle(), **change)]).status is ExecutionStatus.REJECTED


def test_invalid_instrument_or_dto_types_rejected_and_existing_context_invariant_preserved():
    assert run(instrument=None).status is ExecutionStatus.REJECTED
    assert simulate_market_order(None, POLICY, INSTRUMENT, []).status is ExecutionStatus.REJECTED
    assert simulate_market_order(intent(), None, INSTRUMENT, []).status is ExecutionStatus.REJECTED
    with pytest.raises(ValueError):
        InstrumentContext("ETH", "ETH")


EXITS = ExitPolicy(D(2000), D(2200))


def exit_candle(start=120_000, opening="2100", high="2150", low="2050", close="2100", closed=True):
    return MinuteCandle(start, start + 59_999, D(opening), D(high), D(low), D(close), closed)


def exit_run(rows=None, exits=EXITS, policy=POLICY, order=None):
    return simulate_long_exit(intent("SELL", decision=60_000, identity="protect-position") if order is None else order,
                              exits, policy, INSTRUMENT, [exit_candle()] if rows is None else rows)


@pytest.mark.parametrize("row,trigger,reference", [
    (exit_candle(low="1990"), ExitTrigger.STOP, "2000"),
    (exit_candle(high="2210"), ExitTrigger.TARGET, "2200"),
    (exit_candle(low="2000"), ExitTrigger.STOP, "2000"),
    (exit_candle(high="2200"), ExitTrigger.TARGET, "2200")])
def test_single_level_touch_including_equality(row, trigger, reference):
    result = exit_run([row])
    assert isinstance(result, ExitResult) and result.status is ExitStatus.EXIT_FILLED
    assert len(result.fills) == 1 and isinstance(result.fills[0], FillFact)
    assert result.metadata.trigger_type is trigger and result.metadata.reference_price == D(reference)
    assert not result.metadata.ambiguity and not result.metadata.gap_through_stop
    assert result.fills[0].side == "SELL" and result.fills[0].executed_base_qty == D(1)
    assert result.fills[0].execution_price == D(reference) * D("0.97")
    assert result.metadata.final_execution_price == result.fills[0].execution_price
    assert result.fills[0].executed_at == row.close_time + 1
    assert result.metadata.fill_time_basis == "MINUTE_CLOSE_PROXY"


def test_no_trigger_keeps_position_open_without_an_error_or_fill():
    result = exit_run()
    assert result.status is ExitStatus.NO_TRIGGER and result.reason is None and result.fills == ()
    assert result.metadata.trigger_type is None
    assert result.metadata.exit_policy == EXITS


def test_stop_first_preserves_both_touches_and_explicit_ambiguity():
    row = exit_candle(low="1990", high="2210")
    result = exit_run([row])
    metadata = result.metadata
    assert result.status is ExitStatus.EXIT_FILLED
    assert metadata.trigger_type is ExitTrigger.STOP and metadata.reference_price == D(2000)
    assert metadata.ambiguity and metadata.stop_touched and metadata.target_touched
    assert metadata.intrabar_policy is IntrabarPolicy.STOP_FIRST
    assert metadata.stop_price == D(2000) and metadata.target_price == D(2200)
    assert metadata.trigger_candle == row and metadata.trigger_candle_open_time == 120_000
    assert metadata.execution_model_version == POLICY.execution_model_version


def test_target_first_is_explicit_diagnostic_not_default_or_another_contract():
    rows = [exit_candle(low="1990", high="2210")]
    conservative = exit_run(rows)
    alternative = exit_run(rows, replace(EXITS, intrabar_policy=IntrabarPolicy.TARGET_FIRST))
    assert alternative.metadata.trigger_type is ExitTrigger.TARGET
    assert alternative.metadata.ambiguity and alternative.metadata.reference_price == D(2200)
    assert alternative.fills[0].identity != conservative.fills[0].identity
    assert EXITS.intrabar_policy is IntrabarPolicy.STOP_FIRST


def test_gap_stop_uses_open_1950_not_stop_2000_then_sell_costs():
    result = exit_run([exit_candle(opening="1950", high="1980", low="1930", close="1960")])
    assert result.metadata.gap_through_stop and not result.metadata.ambiguity
    assert result.metadata.reference_price == D(1950)
    assert result.fills[0].execution_price == D("1891.5")
    assert result.fills[0].executed_at == 120_000 and result.metadata.fill_time_basis == "OPEN"


@pytest.mark.parametrize("opening,high,low,close,trigger,reference,gap", [
    ("1950", "2250", "1900", "2100", ExitTrigger.STOP, "1950", True),
    ("2250", "2300", "1900", "2100", ExitTrigger.TARGET, "2250", False),
    ("2000", "2250", "1900", "2100", ExitTrigger.STOP, "2000", False),
    ("2200", "2250", "1900", "2100", ExitTrigger.TARGET, "2200", False)])
def test_open_resolves_order_before_later_intrabar_touches(opening, high, low, close, trigger, reference, gap):
    row = exit_candle(opening=opening, high=high, low=low, close=close)
    result = exit_run([row])
    assert result.metadata.trigger_type is trigger and result.metadata.reference_price == D(reference)
    assert result.metadata.stop_touched and result.metadata.target_touched
    assert not result.metadata.ambiguity and result.metadata.gap_through_stop is gap
    assert result.fills[0].executed_at == row.open_time


@pytest.mark.parametrize("fee_asset,commission", [("QUOTE", "1.94"), ("BASE", "0.001")])
def test_exit_quote_and_base_fee_keep_gross_sell_quantity(fee_asset, commission):
    result = exit_run([exit_candle(low="1990")], policy=replace(POLICY, fee_asset_policy=fee_asset))
    fact = result.fills[0]
    assert fact.executed_base_qty == D(1) and fact.executed_quote_qty == D(1940)
    assert fact.commission_amount == D(commission)
    assert fact.commission_asset == ("USDT" if fee_asset == "QUOTE" else "ETH")


def test_base_fee_oversell_is_not_hidden_and_evaluator_rejects_negative_inventory():
    buy = run(policy=replace(POLICY, taker_fee_fraction=D(0))).fills[0]
    sell = exit_run([exit_candle(low="1990")], policy=replace(POLICY, fee_asset_policy="BASE")).fills[0]
    assert sell.executed_base_qty == D(1)  # No automatic down-sizing.
    with pytest.raises(ValueError, match="inventory"):
        evaluate_campaign([buy, sell], instrument=INSTRUMENT, allocated_capital_quote=D(3000))


@pytest.mark.parametrize("decision,expected", [(60_000, 120_000), (63_000, 120_000), (120_000, 180_000)])
def test_exit_activation_strict_next_minute_discards_elapsed_extrema(decision, expected):
    policy = replace(POLICY, processing_delay_seconds=0, submission_delay_seconds=0)
    # A touched level earlier in the activation minute cannot trigger an exit.
    elapsed = exit_candle(start=decision // 60_000 * 60_000, low="1900")
    fresh = exit_candle(start=expected)
    result = exit_run([elapsed, fresh], policy=policy, order=intent("SELL", decision=decision))
    assert result.status is ExitStatus.NO_TRIGGER
    triggered = exit_run([elapsed, replace(fresh, low=D(1900))], policy=policy,
                         order=intent("SELL", decision=decision))
    assert triggered.metadata.trigger_candle_open_time == expected
    assert triggered.fills[0].executed_at >= decision


def test_exit_delays_apply_before_protection_is_eligible():
    result = exit_run([exit_candle(low="1900"), exit_candle(180_000)],
                      order=intent("SELL", decision=119_000))
    assert result.metadata.order_eligible_at == 122_000
    assert result.status is ExitStatus.NO_TRIGGER  # 120000 candle is ineligible.


@pytest.mark.parametrize("rows", [[], [exit_candle(start=60_000, low="1900")],
    [exit_candle(start=180_000, low="1900")], [exit_candle(low="1900", closed=False)],
    [exit_candle(), exit_candle(240_000, low="1900")],
    [exit_candle(), exit_candle(180_000, low="1900", closed=False)]])
def test_missing_or_unclosed_future_minutes_do_not_invent_exit_or_no_trigger(rows):
    result = exit_run(rows)
    assert result.status is ExitStatus.DATA_UNAVAILABLE and result.fills == ()


def test_scan_uses_first_trigger_only_and_later_valid_data_cannot_change_it():
    first = exit_candle(low="1990")
    expected = exit_run([first])
    assert exit_run([first, exit_candle(180_000, high="2300")]) == expected
    result = exit_run([exit_candle(), exit_candle(180_000, low="1990")])
    assert result.metadata.trigger_candle_open_time == 180_000


@pytest.mark.parametrize("policy,row,trigger", [
    (ExitPolicy(stop_price=D(2000)), exit_candle(high="2300"), None),
    (ExitPolicy(target_price=D(2200)), exit_candle(low="1900"), None),
    (ExitPolicy(), exit_candle(low="1900", high="2300"), None),
    (ExitPolicy(stop_price=D(2000)), exit_candle(low="1900"), ExitTrigger.STOP),
    (ExitPolicy(target_price=D(2200)), exit_candle(high="2300"), ExitTrigger.TARGET)])
def test_optional_levels(policy, row, trigger):
    result = exit_run([row], exits=policy)
    assert result.metadata.trigger_type is trigger
    assert result.status is (ExitStatus.NO_TRIGGER if trigger is None else ExitStatus.EXIT_FILLED)


@pytest.mark.parametrize("change", [dict(stop_price=D(0)), dict(stop_price=D(-1)), dict(stop_price=2000.0),
    dict(target_price=D(0)), dict(target_price=D("NaN")), dict(stop_price=D("Infinity")),
    dict(stop_price=D(2200)), dict(stop_price=D(2300)), dict(intrabar_policy="STOP_FIRST"),
    dict(policy_version="")])
def test_invalid_exit_policy_is_rejected(change):
    assert exit_run(exits=replace(EXITS, **change)).status is ExitStatus.REJECTED


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN"])
def test_exit_nonpositive_or_invalid_quantity_rejected(quantity):
    result = exit_run(order=intent("SELL", quantity, 60_000))
    assert result.status is ExitStatus.REJECTED and result.fills == ()


def test_exit_reuses_request_and_candle_validation():
    assert exit_run(order=intent("BUY")).status is ExitStatus.REJECTED
    assert exit_run(exits=None).status is ExitStatus.REJECTED
    assert exit_run(policy=replace(POLICY, impact_fraction=D(-1))).status is ExitStatus.REJECTED
    assert exit_run([exit_candle(), exit_candle()]).status is ExitStatus.REJECTED
    assert exit_run([replace(exit_candle(), low=D(0))]).status is ExitStatus.REJECTED


def test_exit_deterministic_identity_metadata_and_decimal_context_independence():
    rows = [exit_candle(low="1900", high="2300")]
    expected = exit_run(rows)
    assert exit_run(rows) == expected
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        assert exit_run(rows) == expected
    assert exit_run(rows, replace(EXITS, stop_price=D("2000.00"))).fills == expected.fills
    assert exit_run(rows, replace(EXITS, policy_version="exit-v2")).fills[0].identity != expected.fills[0].identity
    for obj, field in [(EXITS, "stop_price"), (expected, "status"), (expected.metadata, "ambiguity")]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)


@pytest.mark.parametrize("trigger,expected_gross,expected_net", [
    (ExitTrigger.TARGET, "74", "69.806"), (ExitTrigger.STOP, "-120", "-124.0")])
def test_simulated_entry_and_exit_campaign_pnl_and_costs_once(trigger, expected_gross, expected_net):
    buy = run(rows=[candle(price="2000")]).fills[0]  # 2060 + 2.06 quote fee.
    row = exit_candle(high="2210") if trigger is ExitTrigger.TARGET else exit_candle(low="1990")
    sell = exit_run([row]).fills[0]  # TARGET=2134, STOP=1940, already net of spread/impact.
    result = evaluate_campaign([buy, sell], instrument=INSTRUMENT, allocated_capital_quote=D(3000))
    assert result["status"] == "OK"
    assert result["gross_pnl_quote"] == D(expected_gross)
    assert result["net_pnl_quote"] == D(expected_net)
    assert result["net_pnl_quote"] == sell.executed_quote_qty - buy.executed_quote_qty - buy.commission_amount - sell.commission_amount


def test_gap_campaign_has_worse_pnl_than_idealized_stop_level_fill():
    buy = run(rows=[candle(price="2000")]).fills[0]
    normal = exit_run([exit_candle(low="1990")]).fills[0]
    gap = exit_run([exit_candle(opening="1950", high="1980", low="1930", close="1960")]).fills[0]
    normal_result = evaluate_campaign([buy, normal], instrument=INSTRUMENT, allocated_capital_quote=D(3000))
    gap_result = evaluate_campaign([buy, gap], instrument=INSTRUMENT, allocated_capital_quote=D(3000))
    assert gap_result["net_pnl_quote"] < normal_result["net_pnl_quote"]
    assert gap.executed_quote_qty == D("1891.5") < normal.executed_quote_qty == D(1940)

"""Small synthetic MARKET execution cases; no historical backtest or network."""
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact, ROUND_UP, localcontext

import pytest

from app.economic_evaluation import FillFact, InstrumentContext, evaluate_campaign
from app.execution_simulator import (ExecutionMetadata, ExecutionPolicy, ExecutionResult,
    ExecutionStatus, MinuteCandle, OrderIntent, simulate_market_order)

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

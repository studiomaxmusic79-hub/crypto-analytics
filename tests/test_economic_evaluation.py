"""Synthetic execution facts only; no exchange, simulator or backtest."""
from dataclasses import FrozenInstanceError, replace
from decimal import Context, Decimal, Inexact, ROUND_UP, localcontext
from itertools import count

import pytest

from app.economic_evaluation import (AssetMovements, FeeValuation, FillFact, InstrumentContext,
    derive_fill_movements, evaluate_campaign, evaluate_equity_curve)

D = Decimal
INSTRUMENT = InstrumentContext("ETH", "USDT")
IDS = count()


def fill(side="BUY", quantity="1", price="100", fee="0", stamp=1000, execution_id=None,
         asset="USDT", quote=None, order_id="order-1"):
    with localcontext(Context(prec=100)):
        notional = D(quote) if quote is not None else D(quantity) * D(price)
    return FillFact(side, D(quantity), notional, D(price), D(fee), asset, stamp,
                    order_id, execution_id if execution_id is not None else f"trade-{next(IDS)}")


def campaign(rows=None, capital="1000", valuations=(), instrument=INSTRUMENT):
    if rows is None:
        rows = [fill(), fill("SELL", price="110", stamp=2500)]
    return evaluate_campaign(rows, instrument=instrument, allocated_capital_quote=D(capital),
                             fee_valuations=valuations)


@pytest.mark.parametrize("exit_price,gross,returns", [("110", "10", "1"), ("90", "-10", "-1")])
def test_profit_and_loss(exit_price, gross, returns):
    result = campaign([fill(), fill("SELL", price=exit_price, stamp=2000)])
    assert result["gross_pnl_quote"] == result["net_pnl_quote"] == D(gross)
    assert result["return_pct_on_allocated_capital"] == D(returns)


def test_multiple_partial_entries_and_exits_weighted_prices_and_costs():
    rows = [fill(quantity="0.4", price="100", fee="0.1"),
            fill(quantity="0.6", price="120", fee="0.2", stamp=2000),
            fill("SELL", "0.25", "130", "0.3", 3000),
            fill("SELL", "0.75", "140", "0.4", 4000)]
    result = campaign(rows)
    assert result["total_buy_quantity"] == result["total_sell_quantity"] == D(1)
    assert result["weighted_average_entry_price"] == D(112)
    assert result["weighted_average_exit_price"] == D("137.5")
    assert result["gross_cost_quote"] == D(112)
    assert result["gross_proceeds_quote"] == D("137.5")
    assert result["fees_quote_total"] == D(1)
    assert result["gross_pnl_quote"] == D("25.5")
    assert result["net_pnl_quote"] == D("24.5")
    assert result["turnover_quote"] == D("249.5")
    assert result["first_entry_at"] == 1000 and result["final_exit_at"] == 4000


def test_spread_and_slippage_are_already_in_fill_prices_only_fees_deducted():
    # Actual execution bought above 100 and sold below 110. No second cost model.
    result = campaign([fill(price="101", fee="0.2"),
                       fill("SELL", price="109", fee="0.3", stamp=2000)])
    assert result["gross_pnl_quote"] == D(8)
    assert result["net_pnl_quote"] == D("7.5")


def test_fees_can_turn_gross_profit_into_net_loss():
    result = campaign([fill(fee="6"), fill("SELL", price="110", fee="5", stamp=2000)])
    assert result["gross_pnl_quote"] == D(10)
    assert result["net_pnl_quote"] == D(-1)


def test_return_denominator_includes_all_unused_allocated_cash():
    small = campaign(capital="100")
    large = campaign(capital="1000")
    assert small["net_pnl_quote"] == large["net_pnl_quote"] == D(10)
    assert small["return_pct_on_allocated_capital"] == D(10)
    assert large["return_pct_on_allocated_capital"] == D(1)
    assert large["allocated_capital_quote"] == D(1000)


def test_millisecond_holding_duration_and_same_time_fills():
    result = campaign()
    assert result["first_entry_at"] == 1000 and result["final_exit_at"] == 2500
    assert result["holding_duration_seconds"] == D("1.5")
    assert campaign([fill(), fill("SELL")])["holding_duration_seconds"] == D(0)


def test_decimal_precision_is_not_lost_to_float_or_callers_small_context():
    rows = [fill(quantity="0.1", price="100.000000000000000000001"),
            fill(quantity="0.2", price="100.000000000000000000001", stamp=2000),
            fill("SELL", "0.3", "100.000000000000000000002", stamp=3000)]
    with localcontext() as context:
        context.prec = 3
        result = campaign(rows)
        assert context.prec == 3
    assert result["total_buy_quantity"] == D("0.3")
    assert result["gross_pnl_quote"] == D("0.0000000000000000000003")
    assert result["weighted_average_entry_price"] == D("100.000000000000000000001")
    for name in ("net_pnl_quote", "gross_pnl_quote", "fees_quote_total", "remaining_base",
                 "turnover_quote", "return_pct_on_allocated_capital", "holding_duration_seconds"):
        assert isinstance(result[name], Decimal)


def test_residual_dust_is_never_rounded_away_or_presented_as_realized_pnl():
    result = campaign([fill(), fill("SELL", quantity="0.99999999999999999999999999999", stamp=2000)])
    assert result["remaining_base"] == D("0.00000000000000000000000000001")
    assert result["status"] == "RESIDUAL_BASE"
    assert not result["position_closed"] and not result["economic_evaluation_complete"]
    assert result["net_pnl_quote"] is result["gross_pnl_quote"] is None
    assert result["final_exit_at"] is result["return_pct_on_allocated_capital"] is None


def test_overselling_is_rejected():
    with pytest.raises(ValueError):
        campaign([fill(), fill("SELL", quantity="1.01", stamp=2000)])


@pytest.mark.parametrize("field", ["executed_base_qty", "executed_quote_qty", "execution_price", "commission_amount"])
@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-Infinity"), 1.0, 1, "1", True])
def test_fill_requires_finite_decimal_values(field, value):
    with pytest.raises(ValueError):
        replace(fill(), **{field: value})


@pytest.mark.parametrize("field,value", [("executed_base_qty", D(0)), ("executed_base_qty", D(-1)),
    ("executed_quote_qty", D(0)), ("executed_quote_qty", D(-1)),
    ("execution_price", D(0)), ("execution_price", D(-1)), ("commission_amount", D(-1)),
    ("executed_at", -1), ("executed_at", True), ("executed_at", 1.0),
    ("side", "UP"), ("order_id", ""), ("trade_id", 1), ("commission_asset", " ")])
def test_invalid_fill_rejected(field, value):
    with pytest.raises(ValueError):
        replace(fill(), **{field: value})


@pytest.mark.parametrize("capital", [D(0), D(-1), D("NaN"), D("Infinity"), 1000.0, "1000", True])
def test_invalid_allocated_capital(capital):
    with pytest.raises(ValueError):
        evaluate_campaign([fill(), fill("SELL")], instrument=INSTRUMENT, allocated_capital_quote=capital)


@pytest.mark.parametrize("rows", [[], ["BUY", "SELL"], [fill("SELL"), fill()],
    [fill(stamp=2000), fill("SELL", stamp=1000)],
    [fill(), fill("SELL"), fill(), fill("SELL")],
    [fill(execution_id="same"), fill("SELL", execution_id="same")],
    [fill(quantity="1"), fill("SELL", quantity="2"), fill(quantity="1")]])
def test_invalid_or_multiple_campaigns_rejected(rows):
    with pytest.raises(ValueError):
        campaign(rows)


def test_fill_is_immutable_and_iterable_input_supported():
    fact = fill()
    with pytest.raises(FrozenInstanceError):
        fact.execution_price = D(200)
    assert campaign(iter([fact, fill("SELL", price="110", stamp=2500)])) == campaign()


def test_repeating_ratios_do_not_depend_on_callers_rounding_or_traps():
    rows = [fill(quantity="1", price="100"), fill(quantity="2", price="101", stamp=2000),
            fill("SELL", "3", "103", stamp=3000)]
    expected = campaign(rows, capital="7")
    points = [(0, D(3)), (1000, D(2))]
    expected_equity = evaluate_equity_curve(points)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert campaign(rows, capital="7") == expected
        assert evaluate_equity_curve(points) == expected_equity
        assert context.prec == 3 and context.traps[Inexact]


def test_partial_exit_does_not_mark_campaign_closed_while_inventory_remains():
    rows = [fill(quantity="2"), fill("SELL", "0.5", "105", stamp=2000),
            fill("SELL", "1.5", "110", stamp=5000)]
    result = campaign(rows)
    assert result["net_pnl_quote"] == D("17.5")
    assert result["final_exit_at"] == 5000
    assert result["holding_duration_seconds"] == D(4)


def valuation(fact, rate="300", value="3", quote="USDT"):
    return FeeValuation(fact.order_id, fact.trade_id, quote, D(rate), fact.executed_at,
                        "synthetic-rate-source", "EXAMPLE_AS_OF_RATE_V1", D(value))


def test_quote_commission_is_cashflow_not_a_second_net_deduction():
    buy = fill(price="2000", fee="2")
    sell = fill("SELL", price="2100", fee="3", stamp=2000)
    assert derive_fill_movements(buy, INSTRUMENT) == AssetMovements(D(1), D(-2002), None, D(0))
    assert derive_fill_movements(sell, INSTRUMENT).quote_cash_delta == D(2097)
    result = campaign([buy, sell])
    assert result["quote_cash_pnl"] == result["net_pnl_quote"] == D(95)
    assert result["fees_quote_total"] == D(5)
    assert result["gross_pnl_quote"] == D(100)


def test_control_buy_base_fee_exact_inventory_close_and_no_double_deduction():
    buy = fill(price="2000", fee="0.001", asset="ETH")
    sell = fill("SELL", quantity="0.999", price="2100", quote="2097.90", stamp=2000)
    assert derive_fill_movements(buy, INSTRUMENT) == AssetMovements(D("0.999"), D(-2000), None, D(0))
    before = (buy, sell)
    result = campaign([buy, sell])
    assert (buy, sell) == before
    assert result["status"] == "OK"
    assert result["position_closed"] and result["economic_evaluation_complete"]
    assert result["remaining_base"] == D(0)
    assert result["total_buy_quantity"] == D(1)
    assert result["total_sell_quantity"] == D("0.999")
    assert result["net_pnl_quote"] == result["quote_cash_pnl"] == D("97.90")
    assert result["fees_quote_total"] == D(2)
    assert result["gross_pnl_quote"] == D("99.90")  # Attribution, not no-fee profit 100.
    assert result["weighted_average_entry_price"] == D(2000)
    assert result["weighted_average_exit_price"] == D(2100)
    assert result["turnover_quote"] == D("4097.90")  # Base commission is not a trade.


def test_sell_base_fee_also_reduces_inventory_without_second_cash_deduction():
    sell = fill("SELL", quantity="0.999", price="2100", fee="0.001", asset="ETH", stamp=2000)
    assert derive_fill_movements(sell, INSTRUMENT).base_inventory_delta == D(-1)
    result = campaign([fill(price="2000"), sell])
    assert result["position_closed"]
    assert result["net_pnl_quote"] == D("97.90")
    assert result["fees_quote_total"] == D("2.100")
    assert result["gross_pnl_quote"] == D(100)


def test_third_asset_movements_and_explicit_valuation():
    buy = fill(price="2000", fee="0.01", asset="BNB")
    sell = fill("SELL", price="2100", stamp=2000)
    assert derive_fill_movements(buy, INSTRUMENT) == AssetMovements(D(1), D(-2000), "BNB", D("-0.01"))
    result = campaign([buy, sell], valuations=[valuation(buy)])
    assert result["net_pnl_quote"] == D(97)
    assert result["quote_cash_pnl"] == D(100)
    assert result["fees_quote_total"] == D(3)
    assert result["gross_pnl_quote"] == D(100)


def test_closed_position_is_independent_of_missing_economic_valuation():
    buy = fill(fee="0.01", asset="BNB")
    rows = [buy, fill("SELL", price="110", stamp=2000)]
    result = campaign(rows)
    assert result["status"] == "MISSING_FEE_VALUATION"
    assert result["remaining_base"] == D(0) and result["position_closed"] is True
    assert result["economic_evaluation_complete"] is False
    assert result["missing_fee_valuation_ids"] == [buy.identity]
    for field in ("net_pnl_quote", "gross_pnl_quote", "fees_quote_total", "return_pct_on_allocated_capital"):
        assert result[field] is None
    assert result["final_exit_at"] == 2000
    assert campaign(rows, valuations=[valuation(buy)])["economic_evaluation_complete"] is True
    assert rows[0] == buy  # Adding a derived valuation never rewrites facts.


def test_partial_fills_with_mixed_commission_assets():
    rows = [fill(quantity="0.6", price="2000", fee="0.001", asset="ETH"),
            fill(quantity="0.4", price="2000", fee="1", stamp=2000),
            fill("SELL", "0.5", "2100", "0.01", 3000, asset="BNB"),
            fill("SELL", "0.499", "2100", "1", 4000)]
    result = campaign(rows, valuations=[valuation(rows[2])])
    assert result["remaining_base"] == 0
    assert result["net_pnl_quote"] == D("92.90")
    assert result["fees_quote_total"] == D(7)
    assert result["gross_pnl_quote"] == D("99.90")


def test_explicit_quote_notional_is_authoritative_for_pnl_vwap_and_turnover():
    rows = [fill(price="100", quote="100.01"),
            fill("SELL", price="110", quote="109.99", stamp=2000)]
    result = campaign(rows)
    assert rows[0].execution_price == D(100)
    assert result["weighted_average_entry_price"] == D("100.01")
    assert result["weighted_average_exit_price"] == D("109.99")
    assert result["net_pnl_quote"] == D("9.98")
    assert result["turnover_quote"] == D(210)


def test_duplicate_rest_websocket_fill_is_rejected_even_if_not_adjacent():
    buy = fill(quantity="1")
    rows = [buy, fill(quantity="1", stamp=2000), replace(buy, executed_at=3000),
            fill("SELL", quantity="3", stamp=4000)]
    with pytest.raises(ValueError, match="Duplicate execution identity"):
        campaign(rows)


def test_identity_uses_order_and_trade_together_within_upstream_scope():
    rows = [fill(quantity="0.5", execution_id="trade", order_id="a"),
            fill(quantity="0.5", execution_id="trade", order_id="b"),
            fill("SELL", stamp=2000)]
    assert campaign(rows)["status"] == "OK"


@pytest.mark.parametrize("rows", [
    [fill(fee="1.001", asset="ETH")],
    [fill(), fill("SELL", quantity="1", fee="0.001", asset="ETH", stamp=2000)],
    [fill(), fill("SELL", quantity="2", stamp=2000), fill(stamp=3000)]])
def test_inventory_cannot_go_negative_even_temporarily_or_due_to_commission(rows):
    with pytest.raises(ValueError, match="inventory"):
        campaign(rows)


def test_residual_and_missing_valuation_are_both_reported_without_open_position_pricing():
    buy = fill(fee="0.01", asset="BNB")
    result = campaign([buy])
    assert result["status"] == "RESIDUAL_BASE" and result["remaining_base"] == D(1)
    assert result["missing_fee_valuation_ids"] == [buy.identity]
    assert not result["position_closed"] and not result["economic_evaluation_complete"]
    assert result["net_pnl_quote"] is None


def test_zero_third_asset_commission_does_not_require_valuation():
    assert campaign([fill(asset="BNB"), fill("SELL", stamp=2000)])["status"] == "OK"


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "wrong_quote", "wrong_value", "quote_fee", "base_fee"])
def test_invalid_or_extra_fee_valuations_are_rejected(kind):
    buy = fill(fee="0.01", asset="BNB")
    valid = valuation(buy)
    supplied = [valid]
    if kind == "unknown":
        supplied = [replace(valid, order_id="another")]
    elif kind == "duplicate":
        supplied = [valid, valid]
    elif kind == "wrong_quote":
        supplied = [replace(valid, quote_asset="EUR")]
    elif kind == "wrong_value":
        supplied = [replace(valid, value_quote=D("2.99"))]
    elif kind in ("quote_fee", "base_fee"):
        buy = replace(buy, commission_asset="USDT" if kind == "quote_fee" else "ETH")
    with pytest.raises(ValueError):
        campaign([buy, fill("SELL", stamp=2000)], valuations=supplied)


@pytest.mark.parametrize("field,value", [
    ("order_id", ""), ("trade_id", None), ("quote_asset", ""), ("valuation_source", " "),
    ("valuation_policy_version", ""), ("rate_as_of", -1), ("rate_as_of", True),
    ("rate_quote_per_fee_asset", D(0)), ("rate_quote_per_fee_asset", 300.0),
    ("rate_quote_per_fee_asset", D("NaN")), ("value_quote", D(-1)),
    ("value_quote", D("Infinity")), ("value_quote", 3.0)])
def test_fee_valuation_contract_validated(field, value):
    with pytest.raises(ValueError):
        replace(valuation(fill(fee="0.01", asset="BNB")), **{field: value})


def test_instrument_valuation_and_projections_are_immutable_and_asset_generic():
    fact = fill(fee="0.01", asset="BNB")
    rate = valuation(fact)
    movements = derive_fill_movements(fact, INSTRUMENT)
    for instance, field, value in [(INSTRUMENT, "base_asset", "BTC"),
                                   (rate, "value_quote", D(0)),
                                   (movements, "base_inventory_delta", D(0))]:
        with pytest.raises(FrozenInstanceError):
            setattr(instance, field, value)
    other = InstrumentContext("SOL", "EUR")
    rows = [fill(fee="0.001", asset="SOL"),
            fill("SELL", quantity="0.999", price="110", asset="EUR", stamp=2000)]
    result = campaign(rows, instrument=other)
    assert result["position_closed"] and result["net_pnl_quote"] == D("9.89")


@pytest.mark.parametrize("base,quote", [("", "USDT"), ("ETH", " "), ("ETH", "ETH"), (None, "USDT")])
def test_invalid_instrument_context(base, quote):
    with pytest.raises(ValueError):
        InstrumentContext(base, quote)


@pytest.mark.parametrize("values,change,drawdown", [
    (["100", "110", "120"], "20", "0"),
    (["100", "80"], "-20", "20"),
    (["100", "80", "120", "108", "130"], "30", "20"),
    (["100", "100"], "0", "0"), (["100"], "0", "0"),
    (["200", "150", "100"], "-100", "50")])
def test_equity_change_and_peak_to_trough_drawdown(values, change, drawdown):
    result = evaluate_equity_curve([(i * 1000, D(v)) for i, v in enumerate(values)])
    assert result == dict(start_equity=D(values[0]), end_equity=D(values[-1]),
                          net_change=D(change), max_drawdown_pct=D(drawdown))
    assert all(isinstance(v, Decimal) for v in result.values())


@pytest.mark.parametrize("points", [[], [(1, D(1)), (1, D(2))], [(2, D(1)), (1, D(2))],
    [(-1, D(1))], [(True, D(1))], [(1.0, D(1))], [(0, D(0))], [(0, D(-1))],
    [(0, D("NaN"))], [(0, D("Infinity"))], [(0, 100.0)]])
def test_invalid_equity_series(points):
    with pytest.raises(ValueError):
        evaluate_equity_curve(points)

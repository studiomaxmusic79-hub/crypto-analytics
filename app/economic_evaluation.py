"""Pure economics of one isolated LONG Spot campaign, independent of source/mode.

Decision -> Order Intent -> Exchange Facts -> Inventory Movements -> Economics.
Frozen FillFact contains gross exchange facts, before commission. Supplied quote
notional is authoritative, NEVER silently replaced with base quantity * price.
InstrumentContext scopes assets; upstream must additionally scope venue/account/
instrument/campaign and normalize IDs to strings and timestamps to UTC Unix ms.
Identity (order_id, trade_id) is unique WITHIN that scope, not globally.

Quote commission reduces cash, base commission reduces inventory, third-asset
commission requires a separate FeeValuation. Spread/slippage are already in
execution facts; none is deducted twice. Base fee attribution uses the declared
execution_price (BASE_AT_EXECUTION_PRICE_V1); quote fees are valued at par.
Third-asset valuation carries its own explicit source/as-of/policy; this module
validates multiplication and identity, not market-rate provenance or timeliness.
gross_pnl = net_pnl + fees_quote_total is fee attribution, NOT a no-fee backtest.

Cash/inventory arithmetic is Decimal with input-sized local precision; ratios use
ROUND_HALF_EVEN with at least 50 significant digits. No exchange-step rounding.
Residual inventory is reported, never valued as a closed campaign. No portfolio,
funding validation, fee-asset cost basis, external flows or OHLC reconstruction.
"""
from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext


def _decimal(value, name, *, zero=False):
    if (not isinstance(value, Decimal) or not value.is_finite()
        or (value < 0 if zero else value <= 0)):
        raise ValueError(f"{name} must be a finite {'nonnegative' if zero else 'positive'} Decimal")


def _timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError("timestamp must be nonnegative UTC Unix milliseconds")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _precision(values):
    # Enough for exact quantity sums and price*quantity products, even for widely
    # separated decimal exponents; independent of caller's global precision.
    low = min(v.as_tuple().exponent for v in values)
    high = max(v.adjusted() for v in values)
    return max(50, 2 * (max(high, 0) - min(low, 0) + 1) + len(str(len(values))) + 10)


@dataclass(frozen=True)
class InstrumentContext:
    base_asset: str
    quote_asset: str

    def __post_init__(self):
        _text(self.base_asset, "base_asset")
        _text(self.quote_asset, "quote_asset")
        if self.base_asset == self.quote_asset:
            raise ValueError("base_asset and quote_asset must differ")


@dataclass(frozen=True)
class FillFact:
    side: str
    executed_base_qty: Decimal
    executed_quote_qty: Decimal
    execution_price: Decimal
    commission_amount: Decimal
    commission_asset: str
    executed_at: int
    order_id: str
    trade_id: str

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError("fill side must be BUY or SELL")
        for field in ("executed_base_qty", "executed_quote_qty", "execution_price"):
            _decimal(getattr(self, field), field)
        _decimal(self.commission_amount, "commission_amount", zero=True)
        for field in ("commission_asset", "order_id", "trade_id"):
            _text(getattr(self, field), field)
        _timestamp(self.executed_at)

    @property
    def identity(self):
        return self.order_id, self.trade_id


@dataclass(frozen=True)
class FeeValuation:
    order_id: str
    trade_id: str
    quote_asset: str
    rate_quote_per_fee_asset: Decimal
    rate_as_of: int
    valuation_source: str
    valuation_policy_version: str
    value_quote: Decimal

    def __post_init__(self):
        for field in ("order_id", "trade_id", "quote_asset", "valuation_source", "valuation_policy_version"):
            _text(getattr(self, field), field)
        _decimal(self.rate_quote_per_fee_asset, "rate_quote_per_fee_asset")
        _decimal(self.value_quote, "value_quote", zero=True)
        _timestamp(self.rate_as_of)

    @property
    def identity(self):
        return self.order_id, self.trade_id


@dataclass(frozen=True)
class AssetMovements:
    base_inventory_delta: Decimal
    quote_cash_delta: Decimal
    third_asset: str | None
    third_asset_delta: Decimal


def derive_fill_movements(fill, instrument):
    """Project immutable gross facts into asset movements; never value a fee here."""
    if not isinstance(fill, FillFact) or not isinstance(instrument, InstrumentContext):
        raise ValueError("Provide FillFact and InstrumentContext")
    values = [fill.executed_base_qty, fill.executed_quote_qty, fill.commission_amount]
    with localcontext(Context(prec=_precision(values), rounding=ROUND_HALF_EVEN)):
        sign = 1 if fill.side == "BUY" else -1
        base = sign * fill.executed_base_qty
        quote = -sign * fill.executed_quote_qty
        third_asset, third_delta = None, Decimal(0)
        if fill.commission_asset == instrument.base_asset:
            base -= fill.commission_amount
        elif fill.commission_asset == instrument.quote_asset:
            quote -= fill.commission_amount
        elif fill.commission_amount:
            third_asset, third_delta = fill.commission_asset, -fill.commission_amount
        return AssetMovements(base, quote, third_asset, third_delta)


def evaluate_campaign(fills, *, instrument, allocated_capital_quote, fee_valuations=()):
    """Evaluate one campaign with Decimal metrics and independent closure/completeness.

    Fills must be chronological (ties allowed in supplied exchange order), with
    no intermediate short position. Closure is EXACT sum(base_inventory_delta)==0.
    Duplicate (order_id, trade_id) pairs are rejected, not silently deduplicated.
    A flat position followed by another fill is another campaign, not this one.
    All unused allocated cash remains in the return denominator. Funding/capital
    availability is an upstream responsibility, not inferred from these fills.

    Status precedence: RESIDUAL_BASE, then MISSING_FEE_VALUATION, otherwise OK.
    All missing third-fee identities are returned even when residual base exists.
    Incomplete evaluation has net/gross PnL and return=None; quote_cash_pnl is
    still the factual cashflow sum, NOT realized profit for a residual position.
    Fees total is None if any valuation is missing. No partial economic estimate
    is presented as complete. Valuations are supplied projections, never stored
    back into a FillFact. Extra, duplicate or inconsistent valuations are errors.
    """
    _decimal(allocated_capital_quote, "allocated_capital_quote")
    if not isinstance(instrument, InstrumentContext):
        raise ValueError("Provide InstrumentContext")
    rows = tuple(fills)
    if not rows or any(not isinstance(f, FillFact) for f in rows):
        raise ValueError("Provide nonempty FillFact execution facts")
    ids = [f.identity for f in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate execution identity (order_id, trade_id)")
    if any(b.executed_at < a.executed_at for a, b in zip(rows, rows[1:])):
        raise ValueError("Fills must be chronological")
    valuations = tuple(fee_valuations)
    if any(not isinstance(v, FeeValuation) for v in valuations):
        raise ValueError("Provide FeeValuation projections")
    by_id = {v.identity: v for v in valuations}
    if len(by_id) != len(valuations):
        raise ValueError("Duplicate fee valuation identity")
    third_ids = {f.identity for f in rows if f.commission_amount
                 and f.commission_asset not in (instrument.base_asset, instrument.quote_asset)}
    if by_id.keys() - third_ids:
        raise ValueError("FeeValuation must refer to a nonzero third-asset fee in this campaign")
    values = [allocated_capital_quote] + [v for f in rows for v in
              (f.executed_base_qty, f.executed_quote_qty, f.execution_price, f.commission_amount)]
    values += [number for v in valuations for number in (v.rate_quote_per_fee_asset, v.value_quote)]
    with localcontext(Context(prec=_precision(values), rounding=ROUND_HALF_EVEN)):
        buy_quantity = sell_quantity = cost = proceeds = fees = position = Decimal(0)
        quote_cash = third_fees = Decimal(0)
        missing = []
        for index, fill in enumerate(rows):
            movements = derive_fill_movements(fill, instrument)
            position += movements.base_inventory_delta
            quote_cash += movements.quote_cash_delta
            if fill.side == "BUY":
                buy_quantity += fill.executed_base_qty
                cost += fill.executed_quote_qty
            else:
                sell_quantity += fill.executed_base_qty
                proceeds += fill.executed_quote_qty
            if fill.commission_asset == instrument.quote_asset:
                fees += fill.commission_amount
            elif fill.commission_asset == instrument.base_asset:
                fees += fill.commission_amount * fill.execution_price
            elif fill.commission_amount:
                valuation = by_id.get(fill.identity)
                if valuation is None:
                    missing.append(fill.identity)
                else:
                    if (valuation.quote_asset != instrument.quote_asset
                        or valuation.value_quote != fill.commission_amount * valuation.rate_quote_per_fee_asset):
                        raise ValueError("Fee valuation quote asset or value does not match execution commission")
                    fees += valuation.value_quote
                    third_fees += valuation.value_quote
            if position < 0:
                raise ValueError("Base inventory cannot become negative, including commissions")
            if position == 0 and index != len(rows) - 1:
                raise ValueError("Multiple campaigns are not supported")
        closed = position == 0
        complete = closed and not missing
        net = quote_cash - third_fees if complete else None
        return dict(status="RESIDUAL_BASE" if not closed else "MISSING_FEE_VALUATION" if missing else "OK",
                    position_closed=closed, economic_evaluation_complete=complete,
                    remaining_base=position, missing_fee_valuation_ids=missing,
                    total_buy_quantity=buy_quantity, total_sell_quantity=sell_quantity,
                    weighted_average_entry_price=cost / buy_quantity,
                    weighted_average_exit_price=proceeds / sell_quantity if sell_quantity else None,
                    gross_cost_quote=cost, gross_proceeds_quote=proceeds,
                    quote_cash_pnl=quote_cash, fees_quote_total=fees if not missing else None,
                    gross_pnl_quote=net + fees if complete else None, net_pnl_quote=net,
                    allocated_capital_quote=allocated_capital_quote,
                    return_pct_on_allocated_capital=net / allocated_capital_quote * 100 if complete else None,
                    turnover_quote=cost + proceeds,
                    first_entry_at=rows[0].executed_at,
                    final_exit_at=rows[-1].executed_at if closed else None,
                    holding_duration_seconds=Decimal(rows[-1].executed_at - rows[0].executed_at) / 1000 if closed else None)


def evaluate_equity_curve(points):
    """Summarize (UTC-ms timestamp, Decimal account_equity_quote) observations.

    Equity must be positive and timestamps strictly increasing. Drawdown is the
    greatest peak-to-subsequent-trough percentage on supplied observations only;
    it cannot detect unobserved intraperiod losses. No OHLC, cash-flow adjustment
    or campaign-level equity reconstruction is performed.
    """
    rows = tuple(points)
    if not rows:
        raise ValueError("Equity series must not be empty")
    previous = None
    values = []
    for stamp, equity in rows:
        _timestamp(stamp)
        _decimal(equity, "account_equity_quote")
        if previous is not None and stamp <= previous:
            raise ValueError("Equity timestamps must be strictly increasing")
        previous = stamp
        values.append(equity)
    with localcontext(Context(prec=_precision(values), rounding=ROUND_HALF_EVEN)):
        peak = values[0]
        drawdown = Decimal(0)
        for equity in values:
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak * 100)
        return dict(start_equity=values[0], end_equity=values[-1],
                    net_change=values[-1] - values[0], max_drawdown_pct=drawdown)

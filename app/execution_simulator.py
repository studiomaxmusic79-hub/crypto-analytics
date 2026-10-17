"""Deterministic research MARKET execution and LONG exits; no PnL/I/O/position book.

Times are UTC Unix milliseconds; delays are nonnegative integer seconds. Reference
is the open of the minute STRICTLY AFTER eligibility, including exact boundaries:
12:00:00 or 12:00:03 eligible -> 12:01:00 fill. The exact required minute must be
present and closed in the historical input; never substitute a later open across
a missing/forming minute. closed is an archive-completeness flag, NOT a claim
that the minute's closing price was known at fill time. Part 1 MARKET fills use
only open prices. Protective exit OHLC assumptions are documented separately.

Synthetic price, full fill, fixed latency and commissions are model assumptions,
not observed exchange facts. Fee policy BASE means gross base qty * fee fraction
on either side; QUOTE means gross quote qty * fee fraction. SELL with a base fee
also consumes base inventory beyond the sold qty. Sizing/funding/reconciliation
remain upstream. Spread/impact are embedded once in price, never extra fees.

One successful intent yields a tuple with one existing FillFact. Tuples permit
future partial fills without assuming one order == one trade. Identity includes
intent, instrument, policy and chosen reference, with decimal scale normalized;
unused valid candles cannot change identity. These are simulation IDs, scoped to
the upstream scenario/account/campaign, never real exchange execution IDs.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
from enum import Enum
import hashlib
import json

from .economic_evaluation import FillFact, InstrumentContext

MINUTE_MS = 60_000


class ExecutionStatus(str, Enum):
    FILLED = "FILLED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class OrderIntent:
    side: str
    quantity_base: Decimal
    decision_at: int
    intent_id: str


@dataclass(frozen=True)
class ExecutionPolicy:
    processing_delay_seconds: int
    submission_delay_seconds: int
    half_spread_fraction: Decimal
    impact_fraction: Decimal
    taker_fee_fraction: Decimal
    fee_asset_policy: str  # BASE or QUOTE; third-asset fees are not supported.
    execution_model_version: str


@dataclass(frozen=True)
class MinuteCandle:
    open_time: int
    close_time: int  # Inclusive: open_time + 59_999, like project OHLCV.
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    closed: bool


@dataclass(frozen=True)
class ExecutionMetadata:
    intent: OrderIntent
    instrument: InstrumentContext
    policy: ExecutionPolicy
    decision_at: int
    order_eligible_at: int
    chosen_open_time: int
    chosen_close_time: int
    reference_price: Decimal


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    fills: tuple[FillFact, ...] = ()
    metadata: ExecutionMetadata | None = None
    reason: str | None = None


class IntrabarPolicy(str, Enum):
    STOP_FIRST = "STOP_FIRST"
    TARGET_FIRST = "TARGET_FIRST"  # Diagnostic alternative; never the default.


class ExitStatus(str, Enum):
    EXIT_FILLED = "EXIT_FILLED"
    NO_TRIGGER = "NO_TRIGGER"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    REJECTED = "REJECTED"


class ExitTrigger(str, Enum):
    STOP = "STOP"
    TARGET = "TARGET"


@dataclass(frozen=True)
class ExitPolicy:
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    intrabar_policy: IntrabarPolicy = IntrabarPolicy.STOP_FIRST
    policy_version: str = "long-exit-v1"


@dataclass(frozen=True)
class ExitMetadata:
    intent: OrderIntent
    instrument: InstrumentContext
    execution_policy: ExecutionPolicy
    exit_policy: ExitPolicy
    order_eligible_at: int
    trigger_type: ExitTrigger | None = None
    trigger_candle: MinuteCandle | None = None
    gap_through_stop: bool = False
    ambiguity: bool = False
    stop_touched: bool = False
    target_touched: bool = False
    reference_price: Decimal | None = None
    final_execution_price: Decimal | None = None
    fill_time_basis: str | None = None

    @property
    def trigger_candle_open_time(self):
        return self.trigger_candle.open_time if self.trigger_candle else None

    @property
    def stop_price(self):
        return self.exit_policy.stop_price

    @property
    def target_price(self):
        return self.exit_policy.target_price

    @property
    def intrabar_policy(self):
        return self.exit_policy.intrabar_policy

    @property
    def execution_model_version(self):
        return self.execution_policy.execution_model_version


@dataclass(frozen=True)
class ExitResult:
    status: ExitStatus
    fills: tuple[FillFact, ...] = ()
    metadata: ExitMetadata | None = None
    reason: str | None = None


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _number(value, name, *, zero=False):
    if (not isinstance(value, Decimal) or not value.is_finite()
        or (value < 0 if zero else value <= 0)):
        raise ValueError(f"{name} must be a finite {'nonnegative' if zero else 'positive'} Decimal")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _decimal_text(value):
    if not value:
        return "0"
    plain = format(value, "f")
    return plain.rstrip("0").rstrip(".") if "." in plain else plain


def _validate_request(intent, policy, instrument):
    if not isinstance(intent, OrderIntent) or not isinstance(policy, ExecutionPolicy):
        raise ValueError("Provide OrderIntent and ExecutionPolicy")
    if not isinstance(instrument, InstrumentContext):
        raise ValueError("Provide InstrumentContext")
    _text(instrument.base_asset, "base_asset")
    _text(instrument.quote_asset, "quote_asset")
    if instrument.base_asset == instrument.quote_asset:
        raise ValueError("Instrument base and quote must differ")
    if intent.side not in ("BUY", "SELL"):
        raise ValueError("Only MARKET BUY and SELL are supported")
    _number(intent.quantity_base, "quantity_base")
    _integer(intent.decision_at, "decision_at")
    _text(intent.intent_id, "intent_id")
    for name in ("processing_delay_seconds", "submission_delay_seconds"):
        _integer(getattr(policy, name), name)
    for name in ("half_spread_fraction", "impact_fraction", "taker_fee_fraction"):
        _number(getattr(policy, name), name, zero=True)
    if policy.fee_asset_policy not in ("BASE", "QUOTE"):
        raise ValueError("fee_asset_policy must be BASE or QUOTE")
    _text(policy.execution_model_version, "execution_model_version")
    return intent.decision_at + 1000 * (policy.processing_delay_seconds + policy.submission_delay_seconds)


def _validate_candles(candles):
    if not isinstance(candles, Sequence) or isinstance(candles, (str, bytes)):
        raise ValueError("Provide a sequence of MinuteCandle")
    rows = tuple(candles)
    previous = None
    for candle in rows:
        if not isinstance(candle, MinuteCandle):
            raise ValueError("Provide MinuteCandle inputs")
        _integer(candle.open_time, "open_time")
        _integer(candle.close_time, "close_time")
        if candle.open_time % MINUTE_MS or candle.close_time != candle.open_time + MINUTE_MS - 1:
            raise ValueError("Invalid 1m candle timestamps")
        if previous is not None and candle.open_time <= previous:
            raise ValueError("Candles must be chronological without duplicates")
        previous = candle.open_time
        if type(candle.closed) is not bool:
            raise ValueError("closed must be a boolean")
        for name in ("open", "high", "low", "close"):
            _number(getattr(candle, name), name)
        if candle.high < max(candle.open, candle.low, candle.close) or candle.low > min(candle.open, candle.high, candle.close):
            raise ValueError("Inconsistent candle OHLC")
    return rows


def _market_fill(intent, policy, instrument, reference, executed_at, reference_time, identity_extension=()):
    values = (intent.quantity_base, reference, policy.half_spread_fraction,
              policy.impact_fraction, policy.taker_fee_fraction)
    # Budget covers exact finite products and alignment, without tick rounding.
    precision = max(50, 20 + sum(len(v.as_tuple().digits) + abs(v.as_tuple().exponent)
                                + max(v.adjusted(), 0) for v in values))
    with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN)):
        adjustment = policy.half_spread_fraction + policy.impact_fraction
        price = reference * (1 + adjustment if intent.side == "BUY" else 1 - adjustment)
        _number(price, "synthetic execution_price")
        notional = intent.quantity_base * price
        base_fee = policy.fee_asset_policy == "BASE"
        commission = (intent.quantity_base if base_fee else notional) * policy.taker_fee_fraction
    # Keep the Part 1 identity projection unchanged. Exit extensions additionally
    # bind trigger facts/policy so a protective exit cannot alias a MARKET intent.
    identity = ["krypta-market-simulation-v1", intent.intent_id, intent.side,
                _decimal_text(intent.quantity_base), intent.decision_at,
                instrument.base_asset, instrument.quote_asset,
                policy.execution_model_version, policy.processing_delay_seconds,
                policy.submission_delay_seconds, policy.fee_asset_policy,
                *[_decimal_text(v) for v in values[2:]], reference_time,
                _decimal_text(reference), *identity_extension]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return FillFact(intent.side, intent.quantity_base, notional, price, commission,
                    instrument.base_asset if base_fee else instrument.quote_asset,
                    executed_at, "sim-order-" + digest, "sim-trade-" + digest + "-0")


def simulate_market_order(intent, policy, instrument, candles):
    """Return FILLED, DATA_UNAVAILABLE or REJECTED, never WAIT or an implied PnL.

    Invalid DTO values are reported here as REJECTED; constructing InstrumentContext
    also enforces its existing contract. All supplied candles are validated for
    order, duplicates, minute geometry and positive Decimal OHLC; gaps are allowed
    but the exact chosen minute must exist. Unclosed candles are never filled.
    Caller supplies a scoped historical snapshot; no data availability is fetched
    or authenticated. Decimal arithmetic does not inherit caller rounding/traps.
    """
    try:
        eligible = _validate_request(intent, policy, instrument)
        chosen_time = (eligible // MINUTE_MS + 1) * MINUTE_MS
        rows = _validate_candles(candles)
        chosen = next((c for c in rows if c.open_time == chosen_time and c.closed), None)
        if chosen is None:
            return ExecutionResult(ExecutionStatus.DATA_UNAVAILABLE, reason="REQUIRED_CLOSED_MINUTE_UNAVAILABLE")
        fact = _market_fill(intent, policy, instrument, chosen.open, chosen.open_time, chosen.open_time)
        metadata = ExecutionMetadata(intent, instrument, policy, intent.decision_at, eligible,
                                     chosen.open_time, chosen.close_time, chosen.open)
        return ExecutionResult(ExecutionStatus.FILLED, (fact,), metadata)
    except (ValueError, TypeError, DecimalException) as exc:
        return ExecutionResult(ExecutionStatus.REJECTED, reason=str(exc))


def simulate_long_exit(intent, exit_policy, policy, instrument, candles):
    """Apply an already specified LONG protection to a finite historical scan.

    intent.side must be SELL, quantity is confirmed inventory supplied upstream,
    decision_at is protection/position activation instruction time. Processing +
    submission delays arm the protection; scanning starts at the next STRICT
    minute boundary, like Part 1. Elapsed intraminute high/low are never reused.

    STOP and TARGET both trigger synthetic full MARKET SELL, not limit orders.
    Open <= stop resolves STOP first (gap reference=open); open >= target resolves
    TARGET first (reference=open). Only an interior open with both extrema touching
    is ambiguous: STOP_FIRST by default, optional TARGET_FIRST for diagnostics.
    A normal touch uses the level, never the favorable high/low excursion.

    An open trigger is timestamped at open. Intrabar timing/order is unknown:
    execution uses a synthetic minute-close boundary timestamp and level price,
    explicitly MINUTE_CLOSE_PROXY, NOT an observed fill at that time/price.
    There is no additional post-trigger latency model: delays above govern arming.

    Every minute from eligibility through the first trigger must be closed and
    contiguous; a missing/forming minute yields DATA_UNAVAILABLE, not a later
    favorable exit. NO_TRIGGER means no touch in the supplied complete scan only,
    never a forced closure or a claim about later data. Both levels None disables
    triggers on that scan. BASE SELL fees do NOT reduce requested sell quantity:
    Economic Evaluator must still reject any resulting negative inventory.
    """
    try:
        eligible = _validate_request(intent, policy, instrument)
        if intent.side != "SELL":
            raise ValueError("LONG exit requires a SELL intent")
        if not isinstance(exit_policy, ExitPolicy):
            raise ValueError("Provide ExitPolicy")
        for name in ("stop_price", "target_price"):
            value = getattr(exit_policy, name)
            if value is not None:
                _number(value, name)
        if (exit_policy.stop_price is not None and exit_policy.target_price is not None
            and exit_policy.stop_price >= exit_policy.target_price):
            raise ValueError("LONG stop_price must be below target_price")
        if not isinstance(exit_policy.intrabar_policy, IntrabarPolicy):
            raise ValueError("Provide an explicit IntrabarPolicy")
        _text(exit_policy.policy_version, "exit policy_version")
        rows = _validate_candles(candles)
        expected = (eligible // MINUTE_MS + 1) * MINUTE_MS
        metadata = ExitMetadata(intent, instrument, policy, exit_policy, eligible)
        seen = False
        for candle in rows:
            if candle.open_time < expected:
                continue
            if candle.open_time != expected or not candle.closed:
                return ExitResult(ExitStatus.DATA_UNAVAILABLE, metadata=metadata,
                                  reason="EXIT_SCAN_MISSING_CLOSED_MINUTE")
            seen = True
            stop, target = exit_policy.stop_price, exit_policy.target_price
            stop_touch = stop is not None and candle.low <= stop
            target_touch = target is not None and candle.high >= target
            open_stop = stop is not None and candle.open <= stop
            open_target = target is not None and candle.open >= target
            ambiguity = stop_touch and target_touch and not (open_stop or open_target)
            if not stop_touch and not target_touch:
                expected += MINUTE_MS
                continue
            if open_stop:
                trigger = ExitTrigger.STOP
            elif open_target:
                trigger = ExitTrigger.TARGET
            elif ambiguity:
                trigger = ExitTrigger.STOP if exit_policy.intrabar_policy is IntrabarPolicy.STOP_FIRST else ExitTrigger.TARGET
            else:
                trigger = ExitTrigger.STOP if stop_touch else ExitTrigger.TARGET
            at_open = open_stop or open_target
            reference = candle.open if at_open else stop if trigger is ExitTrigger.STOP else target
            fill_at = candle.open_time if at_open else candle.close_time + 1
            gap = trigger is ExitTrigger.STOP and candle.open < stop
            # Bind the full trigger candle and both policies for auditable replay.
            extension = ["krypta-long-exit-v1", exit_policy.policy_version,
                         _decimal_text(stop) if stop is not None else None,
                         _decimal_text(target) if target is not None else None,
                         exit_policy.intrabar_policy.value, trigger.value,
                         *[_decimal_text(v) for v in (candle.open, candle.high, candle.low, candle.close)],
                         gap, ambiguity, fill_at]
            fact = _market_fill(intent, policy, instrument, reference, fill_at, candle.open_time, extension)
            metadata = ExitMetadata(intent, instrument, policy, exit_policy, eligible,
                                    trigger, candle, gap, ambiguity, stop_touch, target_touch,
                                    reference, fact.execution_price, "OPEN" if at_open else "MINUTE_CLOSE_PROXY")
            return ExitResult(ExitStatus.EXIT_FILLED, (fact,), metadata)
        if not seen:
            return ExitResult(ExitStatus.DATA_UNAVAILABLE, metadata=metadata,
                              reason="EXIT_SCAN_MISSING_CLOSED_MINUTE")
        return ExitResult(ExitStatus.NO_TRIGGER, metadata=metadata)
    except (ValueError, TypeError, DecimalException) as exc:
        return ExitResult(ExitStatus.REJECTED, reason=str(exc))

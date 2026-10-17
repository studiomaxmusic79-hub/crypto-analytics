"""Deterministic research MARKET execution only; no PnL, I/O or position state.

Times are UTC Unix milliseconds; delays are nonnegative integer seconds. Reference
is the open of the minute STRICTLY AFTER eligibility, including exact boundaries:
12:00:00 or 12:00:03 eligible -> 12:01:00 fill. The exact required minute must be
present and closed in the historical input; never substitute a later open across
a missing/forming minute. closed is an archive-completeness flag, NOT a claim
that the minute's closing price was known at fill time. Only open prices fills.

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
        eligible = intent.decision_at + 1000 * (policy.processing_delay_seconds + policy.submission_delay_seconds)
        chosen_time = (eligible // MINUTE_MS + 1) * MINUTE_MS
        if not isinstance(candles, Sequence) or isinstance(candles, (str, bytes)):
            raise ValueError("Provide a sequence of MinuteCandle")
        chosen = None
        previous = None
        for candle in candles:
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
            if candle.open_time == chosen_time and candle.closed:
                chosen = candle
        if chosen is None:
            return ExecutionResult(ExecutionStatus.DATA_UNAVAILABLE, reason="REQUIRED_CLOSED_MINUTE_UNAVAILABLE")
        values = (intent.quantity_base, chosen.open, policy.half_spread_fraction,
                  policy.impact_fraction, policy.taker_fee_fraction)
        # Budget covers all finite products (qty * price * fee, including price
        # adjustments) and exponent alignment without exchange-step rounding.
        precision = max(50, 20 + sum(len(v.as_tuple().digits) + abs(v.as_tuple().exponent)
                                    + max(v.adjusted(), 0) for v in values))
        with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN)):
            adjustment = policy.half_spread_fraction + policy.impact_fraction
            price = chosen.open * (1 + adjustment if intent.side == "BUY" else 1 - adjustment)
            _number(price, "synthetic execution_price")
            notional = intent.quantity_base * price
            base_fee = policy.fee_asset_policy == "BASE"
            commission = (intent.quantity_base if base_fee else notional) * policy.taker_fee_fraction
        # Canonical projection includes only execution-relevant inputs, not future
        # high/low/close outcomes or unrelated history. No floats or random IDs.
        identity = ["krypta-market-simulation-v1", intent.intent_id, intent.side,
                    _decimal_text(intent.quantity_base), intent.decision_at,
                    instrument.base_asset, instrument.quote_asset,
                    policy.execution_model_version, policy.processing_delay_seconds,
                    policy.submission_delay_seconds, policy.fee_asset_policy,
                    *[_decimal_text(v) for v in values[2:]], chosen.open_time,
                    _decimal_text(chosen.open)]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        fact = FillFact(intent.side, intent.quantity_base, notional, price, commission,
                        instrument.base_asset if base_fee else instrument.quote_asset,
                        chosen.open_time, "sim-order-" + digest, "sim-trade-" + digest + "-0")
        metadata = ExecutionMetadata(intent, instrument, policy, intent.decision_at, eligible,
                                     chosen.open_time, chosen.close_time, chosen.open)
        return ExecutionResult(ExecutionStatus.FILLED, (fact,), metadata)
    except (ValueError, TypeError, DecimalException) as exc:
        return ExecutionResult(ExecutionStatus.REJECTED, reason=str(exc))

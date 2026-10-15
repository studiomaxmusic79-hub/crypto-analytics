"""Deterministic experimental indicators; no ML, calibration or trading."""
import math

MODEL_VERSION = "technical-v1"
MIN_BARS = 100


def ema(values, period):
    value = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for price in values[period:]:
        value += alpha * (price - value)
    return value


def rsi(values, period=14):
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(x, 0) for x in changes[:period]) / period
    loss = sum(max(-x, 0) for x in changes[:period]) / period

    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period

    if gain == loss == 0:
        return 50.0

    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def analyze(candles, horizon):
    if horizon not in (30, 1, 4) or len(candles) < MIN_BARS:
        raise ValueError("Недостаточно закрытых свечей для анализа")

    candles = candles[-MIN_BARS:]

    # 30 минут и 1 час используют свечи 15m.
    # 4 часа используют свечи 1h.
    interval = 900_000 if horizon in (30, 1) else 3_600_000

    if any(
        b[0] - a[0] != interval
        for a, b in zip(candles, candles[1:])
    ):
        raise ValueError("Пропуск свечей: прогноз не сформирован")

    closes = [float(c[4]) for c in candles]

    tr = [
        max(
            float(b[2]) - float(b[3]),
            abs(float(b[2]) - float(a[4])),
            abs(float(b[3]) - float(a[4]))
        )
        for a, b in zip(candles, candles[1:])
    ]

    atr = sum(tr[-14:]) / 14
    last = closes[-1]
    atr_pct = atr / last * 100

    fast = ema(closes, 12)
    slow = ema(closes, 26)
    strength = rsi(closes)

    momentum = (last / closes[-5] - 1) * 100

    trend = math.tanh(
        (fast - slow) / max(atr, last * 0.00001)
    )

    impulse = math.tanh(
        momentum / max(atr_pct, 0.001)
    )

    oscillator = max(
        -1.0,
        min(1.0, (strength - 50) / 25)
    )

    score = (
        0.5 * trend
        + 0.3 * impulse
        + 0.2 * oscillator
    )

    # 30m = 2 свечи по 15m.
    # 1h = 4 свечи по 15m.
    # 4h = 4 свечи по 1h.
    bars_ahead = 2 if horizon == 30 else 4

    move_proxy = (
        score
        * atr_pct
        * math.sqrt(bars_ahead)
    )

    neutral_pct = max(
        0.12,
        0.25 * atr_pct * math.sqrt(bars_ahead)
    )

    direction = (
        "WAIT"
        if abs(score) < 0.25
        or abs(move_proxy) <= neutral_pct
        else ("UP" if score > 0 else "DOWN")
    )

    return {
        "direction": direction,
        "confidence": round(abs(score) * 100),
        "score": round(score, 6),
        "neutral_pct": round(neutral_pct, 6),
        "model_version": MODEL_VERSION,

        "indicators": {
            "ema12": fast,
            "ema26": slow,
            "rsi14": strength,
            "atr_pct": atr_pct,
            "momentum_pct": momentum,
            "move_proxy_pct": move_proxy,
        },

        "reasons": [
            f"EMA12 {'выше' if fast > slow else 'ниже' if fast < slow else 'равна'} EMA26",
            f"RSI(14): {strength:.1f}; импульс за 4 свечи: {momentum:+.2f}%",
            f"ATR(14): {atr_pct:.2f}%; нейтральная зона: ±{neutral_pct:.2f}%",
            (
                "Сигнал слабый или ожидаемое движение "
                "внутри нейтральной зоны"
                if direction == "WAIT"
                else "Тренд и импульс дают направленный сигнал"
            ),
        ],
    }


def evaluate(direction, base_price, actual_price, neutral_pct):
    change = (actual_price / base_price - 1) * 100

    observed = (
        "UP"
        if change > neutral_pct
        else "DOWN"
        if change < -neutral_pct
        else "WAIT"
    )

    return {
        "actual_price": actual_price,
        "change_pct": change,
        "observed_direction": observed,
        "correct": observed == direction,
    }

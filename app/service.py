import asyncio
import logging
import time

from sqlalchemy.exc import SQLAlchemyError

from . import krypta_strategy
from .analytics import analyze, evaluate
from .market import INTERVALS, MarketError
from .strategies import (
    HOUR_MS, MIN_BARS, STRATEGY_A, STRATEGY_B, VERSION,
    analyze_strategies, evaluate_result, evaluation_thresholds,
)


SYMBOLS = ("BTCUSDT", "ETHUSDT")
CANDLE_REFRESH_MS = {
    "15m": 5 * 60_000,
    "1h": 15 * 60_000,
    "4h": 30 * 60_000,
}
HISTORY_BARS = 200
STRATEGY_HISTORY_BARS = MIN_BARS + 1
LIVE_WINDOW_MS = 5 * 60_000
EVALUATION_VERSION = "hourly-flat-v1"
logger = logging.getLogger(__name__)


class ForecastService:
    def __init__(self, market, store, *, enable_krypta=False):
        self.market = market
        self.store = store
        # Only the standalone market_tick opts in; FastAPI keeps its old cycle.
        self.enable_krypta = enable_krypta
        self.last_cycle_at = None
        self.errors = {}

        self.collected_slots = {}
        # Время Binance до запроса; отмечаем только после успешной записи.
        # После перезапуска свечи должны быть подтверждены заново.
        self.candle_updates = {}
        self.strategy_issued = {}

        # Совместимость с main.py. На фоновую работу не влияет.
        self.active_symbol = "BTCUSDT"

    def set_active_symbol(self, symbol):
        symbol = symbol.upper().strip()

        if not symbol.endswith("USDT"):
            raise ValueError(
                "Поддерживаются пары к USDT"
            )

        self.active_symbol = symbol

    @staticmethod
    def candle_values(candles):
        return [
            {
                "time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "close_time": int(c[6]),
            }
            for c in candles
        ]

    @staticmethod
    def analysis_values(candles):
        return [
            [c["time"], c["open"], c["high"], c["low"],
             c["close"], c["volume"], c["close_time"]]
            for c in candles
        ]

    async def collect_quote(self, symbol):
        quote = await self.market.ticker(symbol)
        await asyncio.to_thread(
            self.store.save_quote,
            {**quote, "fetched_at": int(time.time() * 1000)},
        )

    async def collect_candles(self, symbol, interval, now):
        key = (symbol, interval)
        refresh_slot = now // CANDLE_REFRESH_MS[interval]
        if self.collected_slots.get(key) == refresh_slot:
            return

        history_bars = STRATEGY_HISTORY_BARS if interval == "1h" else HISTORY_BARS
        if self.enable_krypta:
            history_bars = max(history_bars, krypta_strategy.WINDOW_BARS[interval] + 1)
        stored = await asyncio.to_thread(
            self.store.get_candles, symbol, interval, history_bars
        )
        delta = INTERVALS[interval]
        expected_open = now // delta * delta
        complete = (
            len(stored) == history_bars
            and all(
                b["time"] - a["time"] == delta
                for a, b in zip(stored, stored[1:])
            )
        )
        # При запуске загружаем окно; далее обновляем последнюю свечу
        # и все появившиеся после неё, включая пропущенные при остановке.
        limit = history_bars
        if key in self.collected_slots and complete:
            limit = min(
                history_bars,
                max(2, (expected_open - stored[-1]["time"]) // delta + 1),
            )

        async def fetch(count):
            # endTime также отключает короткий кэш клиента: подтверждение
            # закрытия должно основываться на новом ответе Binance.
            rows = await self.market.klines(
                symbol, interval, limit=count, endTime=now
            )
            return self.candle_values(rows)

        def usable(values, count):
            merged = {c["time"]: c for c in stored}
            merged.update({c["time"]: c for c in values})
            window = sorted(merged.values(), key=lambda c: c["time"])[-history_bars:]
            return (
                len(values) == count
                and len(window) == history_bars
                and values[-1]["time"] == expected_open
                and all(
                    b["time"] - a["time"] == delta
                    for a, b in zip(values, values[1:])
                )
                and all(
                    b["time"] - a["time"] == delta
                    for a, b in zip(window, window[1:])
                )
            )

        values = await fetch(limit)
        if not usable(values, limit) and limit != history_bars:
            values = await fetch(history_bars)
            limit = history_bars
        if not usable(values, limit):
            raise MarketError("Неполная или устаревшая история свечей")

        await asyncio.to_thread(
            self.store.save_candles, symbol, interval, values, confirmed_at=now
        )
        self.collected_slots[key] = refresh_slot
        self.candle_updates[key] = now

    async def generate_krypta(self, symbol, now):
        """Publish one immutable MTF decision, with the existing 1h/4h checks."""
        reference = now // HOUR_MS * HOUR_MS

        def in_live_window():
            # The actual issue time is exclusive of HH:05, unlike old engines.
            return 0 <= int(time.time() * 1000) - reference < LIVE_WINDOW_MS

        if not in_live_window():
            logger.info("Krypta %s: outside LIVE window for %s", symbol, reference)
            return

        existing = await asyncio.to_thread(lambda: [
            self.store.has_strategy_forecast(
                symbol, reference, krypta_strategy.STRATEGY_ID,
                krypta_strategy.STRATEGY_VERSION, horizon,
            ) for horizon in (1, 4)
        ])
        if all(existing):
            return
        if any(existing):
            raise MarketError("Krypta: incomplete published release; overwrite forbidden")

        inputs = {}
        confirmations = {}
        for interval, count in krypta_strategy.WINDOW_BARS.items():
            expected_close = reference // INTERVALS[interval] * INTERVALS[interval]
            confirmed_at = self.candle_updates.get((symbol, interval))
            if confirmed_at is None or not expected_close <= confirmed_at <= now:
                raise MarketError(f"Krypta: {symbol} {interval} close is not confirmed")
            saved = await asyncio.to_thread(
                self.store.get_candles, symbol, interval, max(HISTORY_BARS, count + 1)
            )
            inputs[interval] = [c for c in saved if c["close_time"] < reference]
            confirmations[interval] = confirmed_at

        # Identify the actual hourly input rather than backfilling an older hour.
        hourly = inputs["1h"]
        if not hourly or hourly[-1]["close_time"] + 1 != reference:
            raise MarketError("Krypta: latest closed hourly candle is unavailable")
        try:
            result = krypta_strategy.analyze(inputs, reference_time=reference)
        except Exception as exc:
            logger.exception("Krypta calculation failed for %s", symbol)
            raise MarketError(f"Krypta calculation failed: {type(exc).__name__}") from exc
        if result["status"] != "OK" or result["signal"] not in ("UP", "DOWN", "WAIT"):
            logger.warning(
                "Krypta %s: %s; %s", symbol, result["status"], result["reason_codes"]
            )
            raise MarketError(
                f"Krypta: {result['status']}: " + ", ".join(result["reason_codes"])
            )

        # Reuse the project's frozen FLAT rules, not a second evaluation formula.
        thresholds = evaluation_thresholds(
            result["base_price"], result["indicators"]["1h"]["atr14"]
        )
        diagnostics = {
            **result, "score_source": "T1h", "confirmed_at": confirmations,
        }
        def publish():
            # Sample in the writing thread, after reads, analysis and queueing.
            issued_at = int(time.time() * 1000)
            if not 0 <= issued_at - reference < LIVE_WINDOW_MS:
                logger.info("Krypta %s: LIVE window expired before publication", symbol)
                return
            if abs(issued_at - now) > 60_000:
                raise MarketError("Krypta: server and Binance clocks are not synchronized")
            records = [{
                "symbol": symbol,
                "strategy_id": result["strategy_id"],
                "strategy_version": result["strategy_version"],
                "input_close_at": reference,
                "horizon": horizon,
                "issued_at": issued_at,
                "base_price": result["base_price"],
                "due_at": reference + horizon * HOUR_MS,
                "status": "OK",
                "direction": result["signal"],
                # Required by the schema: raw trend factor, not confidence.
                "score": result["T1h"],
                "reason_codes": result["reason_codes"],
                "indicators": diagnostics,
                "evaluation_version": EVALUATION_VERSION,
                "neutral_pct": thresholds[horizon],
            } for horizon in (1, 4)]
            self.store.save_strategy_forecasts(records)
            logger.info("Krypta %s: LIVE %s at %s", symbol, result["signal"], issued_at)

        await asyncio.to_thread(publish)

    async def generate_strategies(self, symbol, now):
        input_close_at = now // HOUR_MS * HOUR_MS
        if not 0 <= now - input_close_at <= LIVE_WINDOW_MS:
            return
        if self.strategy_issued.get(symbol) == input_close_at:
            return

        def existing_release():
            return [
                self.store.has_strategy_forecast(
                    symbol, input_close_at, strategy_id, VERSION, horizon
                )
                for strategy_id in (STRATEGY_A, STRATEGY_B, "consensus")
                for horizon in (1, 4)
            ]

        existing = await asyncio.to_thread(existing_release)
        if all(existing):
            self.strategy_issued[symbol] = input_close_at
            return
        if any(existing):
            raise MarketError("Неполный опубликованный выпуск стратегий; перезапись запрещена")

        confirmed_at = self.candle_updates.get((symbol, "1h"))
        if confirmed_at is None or confirmed_at < input_close_at:
            raise MarketError("Часовые свечи для стратегий ещё не подтверждены")
        candles = await asyncio.to_thread(
            self.store.get_candles, symbol, "1h", STRATEGY_HISTORY_BARS
        )
        results = analyze_strategies(candles, now=now, confirmed_at=confirmed_at)
        context = results["A"]
        if context["input_close_at"] != input_close_at:
            # Temporary missing data must not occupy the unique LIVE release key.
            raise MarketError("Стратегии: " + ", ".join(context["reason_codes"]))

        issued_at = int(time.time() * 1000)
        if not 0 <= issued_at - input_close_at <= LIVE_WINDOW_MS:
            return
        if abs(issued_at - now) > 60_000:
            raise MarketError("Часы сервера не синхронизированы для LIVE-выпуска")
        records = []
        for result in results.values():
            for horizon in (1, 4):
                records.append({
                    "symbol": symbol,
                    "strategy_id": result["strategy_id"],
                    "strategy_version": result["strategy_version"],
                    "input_close_at": input_close_at,
                    "horizon": horizon,
                    "issued_at": issued_at,
                    "base_price": context["base_price"],
                    "due_at": context["due_at"][horizon],
                    "status": result["status"],
                    "direction": result["direction"],
                    "score": result["score"],
                    "reason_codes": result["reason_codes"],
                    "indicators": result["indicators"],
                    "evaluation_version": EVALUATION_VERSION,
                    "neutral_pct": context["thresholds_pct"][horizon],
                })
        await asyncio.to_thread(self.store.save_strategy_forecasts, records)
        self.strategy_issued[symbol] = input_close_at

    async def settle_strategies(self, now):
        rows = await asyncio.to_thread(self.store.pending_strategy_forecasts, now - 3000)
        hourly = {}
        targets = {}
        for row in rows:
            try:
                if row["evaluation_version"] != EVALUATION_VERSION:
                    await asyncio.to_thread(
                        self.store.note_strategy_forecast, row["id"],
                        "Неизвестная версия правил проверки стратегий", now,
                    )
                    self.errors["strategy_evaluation"] = "Неизвестная версия правил проверки"
                    continue
                symbol, due_at = row["symbol"], row["due_at"]
                key = (symbol, due_at)
                if key not in targets:
                    if symbol not in hourly:
                        saved = await asyncio.to_thread(
                            self.store.get_candles, symbol, "1h", STRATEGY_HISTORY_BARS
                        )
                        hourly[symbol] = {c["time"]: c for c in saved}
                    candle = hourly[symbol].get(due_at - HOUR_MS)
                    confirmed_at = self.candle_updates.get((symbol, "1h"), -1)
                    if (
                        candle is None or candle["close_time"] != due_at - 1
                        or confirmed_at < due_at
                    ):
                        # One exact request per pair/target, shared by all votes
                        # and both horizons. Never substitute a later candle.
                        raw = await self.market.klines(
                            symbol, "1h", limit=1,
                            startTime=due_at - HOUR_MS, endTime=due_at - 1,
                        )
                        candles = self.candle_values(raw)
                        candle = candles[0] if len(candles) == 1 else None
                        if candle and (
                            candle["time"] != due_at - HOUR_MS
                            or candle["close_time"] != due_at - 1
                        ):
                            candle = None
                        if candle is not None:
                            await asyncio.to_thread(
                                self.store.save_candles, symbol, "1h", [candle], confirmed_at=now
                            )
                        # This confirms only the exact target, not the whole
                        # hourly window or any currently forming input candle.
                        confirmed_at = now
                    targets[key] = (candle, confirmed_at)

                candle, confirmed_at = targets[key]
                if candle is None:
                    await asyncio.to_thread(
                        self.store.note_strategy_forecast, row["id"],
                        "Целевая часовая свеча отсутствует. Проверка будет повторена.", now,
                    )
                    continue
                horizon = row["horizon"]
                forecast = {
                    **row, "due_at": {horizon: due_at},
                    "thresholds_pct": {horizon: row["neutral_pct"]},
                }
                result = evaluate_result(
                    forecast, horizon, candle, now=now, confirmed_at=confirmed_at
                )
                if result["status"] != "OK":
                    await asyncio.to_thread(
                        self.store.note_strategy_forecast, row["id"],
                        ", ".join(result["reason_codes"]), now,
                    )
                    continue
                await asyncio.to_thread(
                    self.store.resolve_strategy_forecast, row["id"], result,
                    int(time.time() * 1000),
                )
            except MarketError as exc:
                await asyncio.to_thread(
                    self.store.note_strategy_forecast, row["id"], str(exc), now
                )
                self.errors["strategy_evaluation"] = str(exc)
                break

    async def generate(self, symbol, now):
        slot = now // 900_000

        # В базе горизонт хранится целым числом:
        # 30 = 30 минут, 1 = 1 час, 4 = 4 часа.
        missing = [
            h
            for h in (30, 1, 4)
            if not await asyncio.to_thread(
                self.store.has_slot,
                symbol,
                h,
                slot
            )
        ]

        # Если прогнозы для этого 15-минутного
        # интервала уже есть, Binance не запрашиваем.
        if not missing:
            return

        for interval in {"15m" if h in (30, 1) else "1h" for h in missing}:
            expected_close = now // INTERVALS[interval] * INTERVALS[interval]
            if self.candle_updates.get((symbol, interval), -1) < expected_close:
                raise MarketError(
                    f"Закрытые свечи {symbol} ({interval}) ещё не подтверждены "
                    "обновлением Binance. Прогноз будет рассчитан позже."
                )

        recent = await self.market.klines(
            symbol,
            "1m",
            limit=2
        )

        closed = [
            c
            for c in recent
            if int(c[6]) < now
        ]

        if not closed:
            raise MarketError(
                "Нет закрытой минутной свечи"
            )

        base = closed[-1]
        reference_at = int(base[6]) + 1

        if not 0 <= now - reference_at <= 90_000:
            raise MarketError(
                "Минутные данные устарели; "
                "прогноз не сформирован"
            )

        # Формирующуюся минутную свечу не сохраняем: её незавершённая
        # цена не должна позже использоваться как результат прогноза.
        await asyncio.to_thread(
            self.store.save_candles, symbol, "1m", self.candle_values(closed), confirmed_at=now
        )

        inputs = {}
        for horizon in missing:
            # 30 минут и 1 час анализируем
            # по 15-минутным свечам.
            # 4 часа — по часовым.
            interval = (
                "15m"
                if horizon in (30, 1)
                else "1h"
            )

            if interval not in inputs:
                saved = await asyncio.to_thread(
                    self.store.get_candles, symbol, interval, HISTORY_BARS
                )
                inputs[interval] = self.analysis_values(saved)

            candles = inputs[interval]

            candles = [
                c
                for c in candles
                if int(c[6]) < reference_at
            ]

            expected_close = (
                reference_at
                // INTERVALS[interval]
                * INTERVALS[interval]
            )

            if (
                not candles
                or int(candles[-1][6]) + 1
                != expected_close
            ):
                raise MarketError(
                    "Последняя закрытая свеча отсутствует"
                )

            prediction = analyze(
                candles,
                horizon
            )

            issued_at = int(
                time.time() * 1000
            )

            if (
                abs(issued_at - now) > 60_000
                or issued_at - reference_at > 120_000
            ):
                raise MarketError(
                    "Данные устарели при расчёте "
                    "или часы сервера "
                    "не синхронизированы"
                )

            if horizon == 30:
                due_at = (
                    reference_at
                    + 30 * 60_000
                )
            else:
                due_at = (
                    reference_at
                    + horizon * 3_600_000
                )

            await asyncio.to_thread(
                self.store.save,
                {
                    **prediction,
                    "symbol": symbol,
                    "horizon": horizon,
                    "slot": slot,
                    "issued_at": issued_at,
                    "reference_at": reference_at,
                    "due_at": due_at,
                    "base_price": float(base[4]),
                }
            )

    async def settle(self, now):
        rows = await asyncio.to_thread(
            self.store.pending,
            now - 3000
        )

        prices = {}
        minute_candles = {}
        for row in rows:
            try:
                symbol = row["symbol"]
                key = (symbol, row["due_at"])
                if symbol not in minute_candles:
                    saved = await asyncio.to_thread(
                        self.store.get_candles, symbol, "1m", 1000
                    )
                    minute_candles[symbol] = {
                        c["time"]: c for c in saved
                    }
                if key not in prices:
                    candle = minute_candles[symbol].get(row["due_at"] - 60_000)
                    if candle and candle["close_time"] == row["due_at"] - 1:
                        prices[key] = candle["close"]
                    else:
                        prices[key] = await self.market.target_price(
                            symbol, row["due_at"], now
                        )
                price = prices[key]

                if price is None:
                    await asyncio.to_thread(
                        self.store.note,
                        row["id"],
                        "Целевая свеча отсутствует. "
                        "Проверка будет повторена.",
                        now
                    )
                    continue

                result = evaluate(
                    row["direction"],
                    row["base_price"],
                    price,
                    row["neutral_pct"]
                )

                await asyncio.to_thread(
                    self.store.resolve,
                    row["id"],
                    result,
                    int(time.time() * 1000)
                )

            except MarketError as exc:
                await asyncio.to_thread(
                    self.store.note,
                    row["id"],
                    str(exc),
                    now
                )

                self.errors["evaluation"] = str(exc)
                break

    async def cycle(self):
        # Время Binance получаем только один раз
        # за весь цикл.
        now = await self.market.time_ms()

        self.errors.pop(
            "connection",
            None
        )

        for symbol in SYMBOLS:
            jobs = [
                (f"{symbol}:quote", self.collect_quote, (symbol,)),
                *[
                    (f"{symbol}:{interval}", self.collect_candles,
                     (symbol, interval, now))
                    for interval in CANDLE_REFRESH_MS
                ],
                (f"{symbol}:strategies", self.generate_strategies, (symbol, now)),
                (symbol, self.generate, (symbol, now)),
            ]
            if self.enable_krypta:
                # Inputs have been saved; issue before slower legacy generation.
                jobs.insert(-2, (f"{symbol}:krypta", self.generate_krypta, (symbol, now)))
            for key, operation, args in jobs:
                try:
                    await operation(*args)
                    self.errors.pop(key, None)
                except (MarketError, ValueError) as exc:
                    self.errors[key] = str(exc)
                except SQLAlchemyError as exc:
                    # Store's transaction context has already rolled back.
                    self.errors[key] = f"{type(exc).__name__}: {exc}"
                    logger.exception("Database operation failed: %s", key)

        self.errors.pop(
            "evaluation",
            None
        )

        # Проверяем ранее сохранённые прогнозы.
        await self.settle(now)
        self.errors.pop("strategy_evaluation", None)
        await self.settle_strategies(now)

        self.last_cycle_at = int(
            time.time() * 1000
        )

    async def run(self):
        while True:
            try:
                await self.cycle()

            except asyncio.CancelledError:
                raise

            except MarketError as exc:
                self.errors["connection"] = str(exc)

            except Exception:
                self.errors["connection"] = (
                    "Не удалось завершить цикл. "
                    "Проверьте подключение к базе "
                    "и журнал сервера."
                )

            # Полный фоновый цикл раз в 2 минуты.
            await asyncio.sleep(120)

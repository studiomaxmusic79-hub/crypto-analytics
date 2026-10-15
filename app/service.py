import asyncio
import time
from .analytics import analyze, evaluate
from .market import SYMBOLS, INTERVALS, MarketError


class ForecastService:
    def __init__(self, market, store):
        self.market, self.store = market, store
        self.last_cycle_at = None
        self.errors = {}

    async def generate(self, symbol, now):
        slot = now // 900_000
        missing = [h for h in (1, 4) if not await asyncio.to_thread(self.store.has_slot, symbol, h, slot)]
        if not missing:
            return
        recent = await self.market.klines(symbol, "1m", limit=3)
        closed = [c for c in recent if int(c[6]) < now]
        if not closed:
            raise MarketError("Нет закрытой минутной свечи")
        base = closed[-1]
        reference_at = int(base[6]) + 1
        if not 0 <= now - reference_at <= 90_000:
            raise MarketError("Минутные данные устарели; прогноз не сформирован")
        for horizon in missing:
            interval = "15m" if horizon == 1 else "1h"
            candles = await self.market.klines(symbol, interval, limit=120, endTime=reference_at - 1)
            candles = [c for c in candles if int(c[6]) < reference_at]
            if not candles or int(candles[-1][6]) + 1 != reference_at // INTERVALS[interval] * INTERVALS[interval]:
                raise MarketError("Последняя закрытая свеча отсутствует")
            prediction = analyze(candles, horizon)
            issued_at = int(time.time() * 1000)
            if abs(issued_at - now) > 60_000 or issued_at - reference_at > 120_000:
                raise MarketError("Данные устарели при расчёте или часы сервера не синхронизированы")
            await asyncio.to_thread(self.store.save, {
                **prediction, "symbol": symbol, "horizon": horizon, "slot": slot,
                "issued_at": issued_at, "reference_at": reference_at,
                "due_at": reference_at + horizon * 3_600_000, "base_price": float(base[4])})

    async def settle(self, now):
        rows = await asyncio.to_thread(self.store.pending, now - 3000)
        for row in rows:
            try:
                price = await self.market.target_price(row["symbol"], row["due_at"], now)
                if price is None:
                    await asyncio.to_thread(self.store.note, row["id"], "Целевая свеча отсутствует. Проверка будет повторена.", now)
                    continue
                result = evaluate(row["direction"], row["base_price"], price, row["neutral_pct"])
                await asyncio.to_thread(self.store.resolve, row["id"], result, int(time.time() * 1000))
            except MarketError as exc:
                await asyncio.to_thread(self.store.note, row["id"], str(exc), now)
                # A rate-limit/network outage should not result in 100 requests.
                self.errors["evaluation"] = str(exc)
                break

    async def cycle(self):
        now = await self.market.time_ms()
        self.errors.pop("connection", None)
        for symbol in SYMBOLS:
            try:
                await self.generate(symbol, now)
                self.errors.pop(symbol, None)
            except (MarketError, ValueError) as exc:
                self.errors[symbol] = str(exc)
        self.errors.pop("evaluation", None)
        await self.settle(await self.market.time_ms())
        self.last_cycle_at = int(time.time() * 1000)

    async def run(self):
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except MarketError as exc:
                self.errors["connection"] = str(exc)
            except Exception:
                # Don't leak connection strings or database parameters into logs/API.
                self.errors["connection"] = "Не удалось завершить цикл. Проверьте подключение к базе и журнал сервера."
            await asyncio.sleep(30)

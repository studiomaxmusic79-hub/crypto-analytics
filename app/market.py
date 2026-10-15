import asyncio
import math
import time
import httpx

INTERVALS = {
    "1m": 60_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}

BASE_URL = "https://data-api.binance.vision"
SYMBOLS = ("BTCUSDT", "ETHUSDT")

class MarketError(Exception):
    pass


class BinanceMarket:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=12,
            follow_redirects=False
        )
        self.cache = {}
        self.lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(3)
        self.cooldown_until = 0

    async def close(self):
        await self.client.aclose()

    async def get(self, path, params=None, ttl=0):
        # Только публичные рыночные данные Binance. API-ключи не используются.
        if path not in ("time", "ticker/24hr", "klines"):
            raise ValueError("Endpoint not allowed")

        key = (path, tuple(sorted((params or {}).items())))

        async with self.lock:
            cached = self.cache.get(key)

            if cached and time.monotonic() - cached[0] < ttl:
                return cached[1]

            if time.monotonic() < self.cooldown_until:
                raise MarketError(
                    "Binance ограничил частоту запросов. Повторим позже."
                )

            async with self.semaphore:
                try:
                    response = await self.client.get(
                        f"/api/v3/{path}",
                        params=params
                    )

                    if response.status_code in (418, 429):
                        try:
                            delay = max(
                                60,
                                min(
                                    86400,
                                    int(response.headers.get("Retry-After", "60"))
                                )
                            )
                        except ValueError:
                            delay = 60

                        self.cooldown_until = time.monotonic() + delay

                        raise MarketError(
                            "Binance ограничил частоту запросов. Повторим позже."
                        )

                    response.raise_for_status()
                    data = response.json()

                except (httpx.HTTPError, ValueError) as exc:
                    raise MarketError(
                        "Нет доступа к публичным данным Binance. "
                        "Проверьте сеть или регион сервера."
                    ) from exc

            if ttl:
                if len(self.cache) > 64:
                    self.cache.clear()

                self.cache[key] = (time.monotonic(), data)

            return data

    async def time_ms(self):
    data = await self.get("time", ttl=60)

        try:
            return int(data["serverTime"])

        except (KeyError, TypeError, ValueError) as exc:
            raise MarketError(
                "Некорректное время Binance"
            ) from exc

    async def ticker(self, symbol):
        symbol = symbol.upper().strip()

        data = await self.get(
            "ticker/24hr",
            {"symbol": symbol},
            ttl=15
        )

        try:
            result = {
                "symbol": symbol,
                "price": float(data["lastPrice"]),
                "change_pct": float(data["priceChangePercent"]),
                "high": float(data["highPrice"]),
                "low": float(data["lowPrice"]),
                "volume": float(data["quoteVolume"]),
                "updated_at": int(data["closeTime"]),
            }

            if any(
                not math.isfinite(result[k])
                for k in ("price", "change_pct", "high", "low", "volume")
            ):
                raise ValueError("Invalid quote")

            if result["price"] <= 0:
                raise ValueError("Invalid quote")

            if abs(
                int(time.time() * 1000) - result["updated_at"]
            ) > 120_000:
                raise MarketError(
                    "Binance вернул устаревшую котировку"
                )

            return result

        except (KeyError, TypeError, ValueError) as exc:
            raise MarketError(
                "Некорректная котировка Binance"
            ) from exc

    async def klines(self, symbol, interval, limit=200, **params):
        symbol = symbol.upper().strip()

        if interval not in INTERVALS or not 1 <= limit <= 1000:
            raise ValueError("Invalid market parameters")

        data = await self.get(
            "klines",
            {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                **params,
            },
            ttl=20 if not params else 0
        )

        try:
            if not isinstance(data, list):
                raise ValueError("Not a list")

            previous = -1

            for row in data:
                start = int(row[0])
                end = int(row[6])

                o, h, l, c, v = map(float, row[1:6])

                if (
                    start <= previous
                    or start % INTERVALS[interval]
                    or end != start + INTERVALS[interval] - 1
                    or any(
                        not math.isfinite(x)
                        for x in (o, h, l, c, v)
                    )
                    or min(o, h, l, c) <= 0
                    or v < 0
                    or h < max(o, c, l)
                    or l > min(o, c, h)
                ):
                    raise ValueError("Invalid candle")

                previous = start

            return data

        except (ValueError, TypeError, IndexError) as exc:
            raise MarketError(
                "Некорректные свечи Binance"
            ) from exc

    async def target_price(self, symbol, due_at, now):
        if due_at > now:
            return None

        candles = await self.klines(
            symbol,
            "1m",
            limit=1,
            startTime=due_at - 60_000,
            endTime=due_at - 1
        )

        if (
            not candles
            or int(candles[0][0]) != due_at - 60_000
            or int(candles[0][6]) != due_at - 1
        ):
            return None

        return float(candles[0][4])

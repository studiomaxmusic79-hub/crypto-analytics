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
        # Только публичные данные Binance.
        # API-ключи не используются.
        if path not in (
            "time",
            "ticker/24hr",
            "ticker/bookTicker",
            "klines"
        ):
            raise ValueError(
                "Endpoint not allowed"
            )

        key = (
            path,
            tuple(
                sorted(
                    (params or {}).items()
                )
            )
        )

        async with self.lock:
            cached = self.cache.get(key)

            if (
                cached
                and time.monotonic() - cached[0] < ttl
            ):
                return cached[1]

            if time.monotonic() < self.cooldown_until:
                remaining = max(
                    1,
                    int(
                        self.cooldown_until
                        - time.monotonic()
                    )
                )

                raise MarketError(
                    "Binance cooldown активен. "
                    f"Осталось примерно {remaining} сек."
                )

            async with self.semaphore:
                try:
                    response = await self.client.get(
                        f"/api/v3/{path}",
                        params=params
                    )

                except httpx.HTTPError as exc:
                    raise MarketError(
                        "Ошибка соединения с Binance: "
                        f"{type(exc).__name__}"
                    ) from exc

            status = response.status_code

            retry_after = response.headers.get(
                "Retry-After"
            )

            used_weight = response.headers.get(
                "X-MBX-USED-WEIGHT-1M"
            )

            try:
                payload = response.json()

            except ValueError:
                payload = None

            if isinstance(payload, dict):
                binance_code = payload.get("code")
                binance_msg = payload.get("msg")
            else:
                binance_code = None
                binance_msg = None

            # Показываем настоящий ответ Binance,
            # чтобы можно было установить причину.
            if status in (418, 429):
                try:
                    delay = int(
                        retry_after or "60"
                    )
                except ValueError:
                    delay = 60

                delay = max(
                    60,
                    min(
                        86400,
                        delay
                    )
                )

                self.cooldown_until = (
                    time.monotonic() + delay
                )

                details = [
                    f"HTTP {status}"
                ]

                if binance_code is not None:
                    details.append(
                        f"code={binance_code}"
                    )

                if binance_msg:
                    details.append(
                        f"msg={binance_msg}"
                    )

                if retry_after:
                    details.append(
                        f"Retry-After={retry_after}"
                    )

                if used_weight:
                    details.append(
                        f"weight={used_weight}"
                    )

                raise MarketError(
                    "Binance rate limit: "
                    + " | ".join(details)
                )

            if status >= 400:
                details = [
                    f"HTTP {status}"
                ]

                if binance_code is not None:
                    details.append(
                        f"code={binance_code}"
                    )

                if binance_msg:
                    details.append(
                        f"msg={binance_msg}"
                    )

                if used_weight:
                    details.append(
                        f"weight={used_weight}"
                    )

                raise MarketError(
                    "Binance вернул ошибку: "
                    + " | ".join(details)
                )

            if payload is None:
                raise MarketError(
                    "Binance вернул ответ "
                    "в неизвестном формате"
                )

            data = payload

            if ttl:
                if len(self.cache) > 64:
                    self.cache.clear()

                self.cache[key] = (
                    time.monotonic(),
                    data
                )

            return data

    async def time_ms(self):
        data = await self.get(
            "time",
            ttl=60
        )

        try:
            return int(
                data["serverTime"]
            )

        except (
            KeyError,
            TypeError,
            ValueError
        ) as exc:
            raise MarketError(
                "Некорректное время Binance"
            ) from exc

    async def ticker(self, symbol):
        symbol = (
            symbol
            .upper()
            .strip()
        )

        data = await self.get(
            "ticker/24hr",
            {
                "symbol": symbol
            },
            ttl=60
        )

        try:
            result = {
                "symbol":
                    symbol,

                "price":
                    float(
                        data["lastPrice"]
                    ),

                "change_pct":
                    float(
                        data[
                            "priceChangePercent"
                        ]
                    ),

                "high":
                    float(
                        data["highPrice"]
                    ),

                "low":
                    float(
                        data["lowPrice"]
                    ),

                "volume":
                    float(
                        data["quoteVolume"]
                    ),

                "updated_at":
                    int(
                        data["closeTime"]
                    ),
            }

            if any(
                not math.isfinite(
                    result[k]
                )
                for k in (
                    "price",
                    "change_pct",
                    "high",
                    "low",
                    "volume"
                )
            ):
                raise ValueError(
                    "Invalid quote"
                )

            if result["price"] <= 0:
                raise ValueError(
                    "Invalid quote"
                )

            if abs(
                int(time.time() * 1000)
                - result["updated_at"]
            ) > 120_000:
                raise MarketError(
                    "Binance вернул "
                    "устаревшую котировку"
                )

            return result

        except (
            KeyError,
            TypeError,
            ValueError
        ) as exc:
            raise MarketError(
                "Некорректная котировка Binance"
            ) from exc

    async def book_ticker(self, symbol):
        # Uncached, one attempt. Validation and persistence belong to protocol.
        return await self.get("ticker/bookTicker", {"symbol": symbol})

    async def klines(
        self,
        symbol,
        interval,
        limit=200,
        **params
    ):
        symbol = (
            symbol
            .upper()
            .strip()
        )

        if (
            interval not in INTERVALS
            or not 1 <= limit <= 1000
        ):
            raise ValueError(
                "Invalid market parameters"
            )

        # Обычные свечи кэшируются.
        # Исторические запросы пока
        # оставляем без кэша.
        data = await self.get(
            "klines",
            {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                **params,
            },
            ttl=60 if not params else 0
        )

        try:
            if not isinstance(
                data,
                list
            ):
                raise ValueError(
                    "Not a list"
                )

            previous = -1

            for row in data:
                start = int(
                    row[0]
                )

                end = int(
                    row[6]
                )

                o, h, l, c, v = map(
                    float,
                    row[1:6]
                )

                if (
                    start <= previous
                    or start
                    % INTERVALS[interval]
                    or end
                    != start
                    + INTERVALS[interval]
                    - 1
                    or any(
                        not math.isfinite(x)
                        for x in (
                            o,
                            h,
                            l,
                            c,
                            v
                        )
                    )
                    or min(
                        o,
                        h,
                        l,
                        c
                    ) <= 0
                    or v < 0
                    or h < max(
                        o,
                        c,
                        l
                    )
                    or l > min(
                        o,
                        c,
                        h
                    )
                ):
                    raise ValueError(
                        "Invalid candle"
                    )

                previous = start

            return data

        except (
            ValueError,
            TypeError,
            IndexError
        ) as exc:
            raise MarketError(
                "Некорректные свечи Binance"
            ) from exc

    async def target_price(
        self,
        symbol,
        due_at,
        now
    ):
        if due_at > now:
            return None

        candles = await self.klines(
            symbol,
            "1m",
            limit=1,
            startTime=(
                due_at - 60_000
            ),
            endTime=(
                due_at - 1
            )
        )

        if (
            not candles
            or int(
                candles[0][0]
            ) != due_at - 60_000
            or int(
                candles[0][6]
            ) != due_at - 1
        ):
            return None

        return float(
            candles[0][4]
        )

import asyncio
import csv
import io
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .market import BinanceMarket, MarketError
from .service import CANDLE_REFRESH_MS, ForecastService
from .storage import Store


STATIC = Path(__file__).parent / "static"
Symbol = str
QUOTE_MAX_AGE_MS = 6 * 60_000
CANDLE_GRACE_MS = 4 * 60_000


def normalize_symbol(symbol: str) -> str:
    symbol = (
        symbol
        .upper()
        .strip()
        .replace("/", "")
        .replace("-", "")
        .replace(" ", "")
    )

    if not symbol.endswith("USDT"):
        raise HTTPException(
            400,
            "Поддерживаются пары к USDT"
        )

    if len(symbol) <= 4:
        raise HTTPException(
            400,
            "Некорректная валютная пара"
        )

    return symbol


def stored_quote(store: Store, symbol: str):
    ticker = store.get_quote(symbol)

    if ticker is None:
        raise HTTPException(
            503,
            f"В базе пока нет котировки {symbol}. "
            "Дождитесь фонового обновления данных."
        )

    now = int(time.time() * 1000)
    if (
        now - ticker["fetched_at"] > QUOTE_MAX_AGE_MS
        or now - ticker["updated_at"] > QUOTE_MAX_AGE_MS + 120_000
    ):
        raise HTTPException(
            503,
            f"Сохранённая котировка {symbol} устарела. "
            "Ожидается фоновое обновление данных."
        )

    # Сохраняем прежний формат ticker(), без служебного fetched_at.
    return {
        key: ticker[key]
        for key in (
            "symbol", "price", "change_pct", "high", "low", "volume", "updated_at"
        )
    }


@asynccontextmanager
async def lifespan(app):
    store = Store()
    await asyncio.to_thread(store.init)

    market = BinanceMarket()
    service = ForecastService(
        market,
        store
    )

    app.state.store = store
    app.state.market = market
    app.state.service = service

    task = asyncio.create_task(
        service.run()
    )

    try:
        yield

    finally:
        task.cancel()

        with suppress(
            asyncio.CancelledError
        ):
            await task

        await market.close()
        store.close()


app = FastAPI(
    title="Krypta — экспериментальная криптоаналитика",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None
)


@app.middleware("http")
async def security_headers(
    request: Request,
    call_next
):
    response = await call_next(
        request
    )

    response.headers[
        "X-Content-Type-Options"
    ] = "nosniff"

    response.headers[
        "Referrer-Policy"
    ] = "same-origin"

    response.headers[
        "X-Frame-Options"
    ] = "DENY"

    response.headers[
        "Content-Security-Policy"
    ] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )

    if request.url.path.startswith(
        "/api/"
    ):
        response.headers[
            "Cache-Control"
        ] = "no-store"

    return response


@app.exception_handler(MarketError)
async def market_exception(
    request,
    exc
):
    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc)
        }
    )


@app.exception_handler(Exception)
async def server_exception(
    request,
    exc
):
    return JSONResponse(
        status_code=503,
        content={
            "detail":
                "Сервис временно недоступен. "
                "Повторите запрос позже."
        }
    )


@app.get("/healthz")
def health(
    request: Request
):
    try:
        request.app.state.store.ready()

    except Exception:
        raise HTTPException(
            503,
            "Database unavailable"
        ) from None

    return {
        "status": "ok"
    }


@app.get("/api/status")
def status(
    request: Request
):
    service = request.app.state.service

    return {
        "now":
            int(time.time() * 1000),

        "last_cycle_at":
            service.last_cycle_at,

        "active_symbol":
            service.active_symbol,

        "errors":
            dict(service.errors),

        "storage":
            request.app.state
            .store
            .engine
            .dialect
            .name,

        "render_free":
            bool(os.getenv("RENDER")),

        "source":
            "Binance public market data",

        "generation_interval_minutes":
            15,

        "calibrated_probability":
            False
    }


@app.get("/api/market")
def market(
    request: Request,
    symbol: Symbol = "BTCUSDT",
    interval: Literal[
        "15m",
        "1h",
        "4h"
    ] = "1h"
):
    symbol = normalize_symbol(symbol)
    store = request.app.state.store
    ticker = stored_quote(store, symbol)
    candles = store.get_candles(
        symbol,
        interval,
        limit=200
    )

    if not candles:
        raise HTTPException(
            503,
            f"В базе пока нет свечей {symbol} ({interval}) для графика. "
            "Дождитесь фонового обновления данных."
        )

    now = int(time.time() * 1000)
    updated_at = request.app.state.service.candle_updates.get((symbol, interval))
    max_age = CANDLE_REFRESH_MS[interval] + CANDLE_GRACE_MS
    if (
        updated_at is None
        or now - updated_at > max_age
        or now - candles[-1]["close_time"] > max_age
    ):
        raise HTTPException(
            503,
            f"Свечи {symbol} ({interval}) устарели или ещё не подтверждены "
            "после запуска. Ожидается фоновое обновление данных."
        )

    return {
        "ticker": ticker,
        "interval": interval,
        "source": "Binance",
        "candles": candles
    }


@app.get("/api/quote")
def quote(
    request: Request,
    symbol: Symbol = "BTCUSDT"
):
    symbol = normalize_symbol(
        symbol
    )

    return stored_quote(request.app.state.store, symbol)


@app.get("/api/forecasts")
def forecasts(
    request: Request,
    symbol: Symbol = "BTCUSDT"
):
    symbol = normalize_symbol(symbol)

    # Здесь Binance не вызывается.
    # Браузер читает уже готовые
    # прогнозы из базы.
    return {
        "items":
            request.app.state
            .store
            .latest(symbol)
    }


@app.get("/api/strategy-forecasts")
def strategy_forecasts(
    request: Request,
    symbol: Symbol = "BTCUSDT"
):
    symbol = normalize_symbol(symbol)
    return request.app.state.store.strategy_history(symbol, limit=12)


@app.get("/api/history")
def history(
    request: Request,
    symbol: Symbol = "BTCUSDT",
    horizon: Literal[
        "0",
        "30",
        "1",
        "4"
    ] = "0",
    offset: int = Query(
        0,
        ge=0,
        le=1_000_000
    ),
    limit: int = Query(
        20,
        ge=1,
        le=100
    )
):
    symbol = normalize_symbol(
        symbol
    )

    return (
        request.app.state
        .store
        .history(
            symbol,
            int(horizon),
            offset,
            limit
        )
    )


@app.get("/api/stats")
def stats(
    request: Request,
    symbol: Symbol = "BTCUSDT"
):
    symbol = normalize_symbol(
        symbol
    )

    return {
        "items":
            request.app.state
            .store
            .stats(symbol)
    }


@app.get("/api/export.csv")
def export(
    request: Request,
    symbol: Symbol = "BTCUSDT"
):
    symbol = normalize_symbol(
        symbol
    )

    store = request.app.state.store

    def generate():
        cursor = 0

        fields = [
            "id",
            "symbol",
            "horizon",
            "issued_at",
            "reference_at",
            "due_at",
            "base_price",
            "direction",
            "confidence",
            "score",
            "neutral_pct",
            "model_version",
            "actual_price",
            "change_pct",
            "observed_direction",
            "correct",
            "evaluated_at"
        ]

        output = io.StringIO()

        writer = csv.DictWriter(
            output,
            fields,
            extrasaction="ignore"
        )

        writer.writeheader()

        yield (
            "\ufeff" +
            output.getvalue()
        )

        from sqlalchemy import select
        from .storage import (
            forecasts as table
        )

        with store.engine.connect() as conn:
            max_id = conn.execute(
                select(table.c.id)
                .where(
                    table.c.symbol ==
                    symbol
                )
                .order_by(
                    table.c.id.desc()
                )
                .limit(1)
            ).scalar()

        if max_id is None:
            return

        while True:
            with store.engine.connect() as conn:
                rows = conn.execute(
                    select(table)
                    .where(
                        table.c.symbol ==
                        symbol,

                        table.c.id >
                        cursor,

                        table.c.id <=
                        max_id
                    )
                    .order_by(
                        table.c.id
                    )
                    .limit(500)
                ).all()

            if not rows:
                return

            output.seek(0)
            output.truncate(0)

            for row in rows:
                writer.writerow(
                    dict(row._mapping)
                )

            cursor = (
                rows[-1]
                ._mapping["id"]
            )

            yield output.getvalue()

    return StreamingResponse(
        generate(),
        media_type=(
            "text/csv; charset=utf-8"
        ),
        headers={
            "Content-Disposition":
                f'attachment; '
                f'filename="krypta-{symbol}.csv"'
        }
    )


@app.get(
    "/en/",
    include_in_schema=False
)
def index_en():
    return FileResponse(
        STATIC / "index-en.html",
        headers={
            "Cache-Control":
                "no-cache"
        }
    )


@app.get(
    "/robots.txt",
    include_in_schema=False
)
def robots():
    return FileResponse(
        STATIC / "robots.txt",
        media_type="text/plain"
    )


@app.get(
    "/sitemap.xml",
    include_in_schema=False
)
def sitemap():
    return FileResponse(
        STATIC / "sitemap.xml",
        media_type="application/xml"
    )


@app.get(
    "/googlea35b0bd688671f7e.html",
    include_in_schema=False
)
def google_verification():
    return FileResponse(
        Path(
            "googlea35b0bd688671f7e.html"
        ),
        media_type="text/html"
    )


@app.get(
    "/",
    include_in_schema=False
)
def index():
    return FileResponse(
        STATIC / "index.html",
        headers={
            "Cache-Control":
                "no-cache"
        }
    )


app.mount(
    "/static",
    StaticFiles(
        directory=STATIC
    ),
    name="static"
)

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
from .service import ForecastService
from .storage import Store


STATIC = Path(__file__).parent / "static"
Symbol = str


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


def activate_symbol(
    request: Request,
    symbol: str
) -> str:
    symbol = normalize_symbol(symbol)

    request.app.state.service.set_active_symbol(
        symbol
    )

    return symbol


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
async def market(
    request: Request,
    symbol: Symbol = "BTCUSDT",
    interval: Literal[
        "15m",
        "1h",
        "4h"
    ] = "1h"
):
    # Выбранная пользователем монета
    # становится активной для фонового
    # расчёта прогнозов.
    symbol = activate_symbol(
        request,
        symbol
    )

    market = request.app.state.market

    # Эти данные кэшируются
    # в market.py.
    ticker = await market.ticker(
        symbol
    )

    candles = await market.klines(
        symbol,
        interval,
        limit=200
    )

    if not candles:
        raise MarketError(
            "Binance не вернул свечи "
            "для графика. "
            "Повторим запрос позже."
        )

    return {
        "ticker": ticker,
        "interval": interval,
        "source": "Binance",
        "candles": [
            {
                "time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "close_time": int(c[6])
            }
            for c in candles
        ]
    }


@app.get("/api/quote")
async def quote(
    request: Request,
    symbol: Symbol = "BTCUSDT"
):
    symbol = normalize_symbol(
        symbol
    )

    # Только цена выбранной валюты.
    # ticker() кэшируется в market.py.
    return await (
        request.app.state
        .market
        .ticker(symbol)
    )


@app.get("/api/forecasts")
def forecasts(
    request: Request,
    symbol: Symbol = "BTCUSDT"
):
    symbol = activate_symbol(
        request,
        symbol
    )

    # Здесь Binance не вызывается.
    # Браузер читает уже готовые
    # прогнозы из базы.
    return {
        "items":
            request.app.state
            .store
            .latest(symbol)
    }


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

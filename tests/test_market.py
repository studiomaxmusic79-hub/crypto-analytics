import asyncio
import httpx
import pytest
from app.market import BinanceMarket, MarketError


def test_evaluation_uses_exact_target_candle():
    async def run():
        market = BinanceMarket()
        await market.client.aclose()
        def handler(request):
            assert request.url.path == '/api/v3/klines'
            assert request.url.params['startTime'] == '60000'
            assert request.url.params['endTime'] == '119999'
            assert not any('key' in k.lower() for k in request.headers)
            return httpx.Response(200,json=[[60000,'100','102','99','101','10',119999]])
        market.client = httpx.AsyncClient(base_url='https://data-api.binance.vision', transport=httpx.MockTransport(handler))
        assert await market.target_price('BTCUSDT',120000,180000) == 101
        assert await market.target_price('BTCUSDT',240000,180000) is None
        await market.close()
    asyncio.run(run())


def test_rate_limit_stops_followup_requests():
    async def run():
        calls = []
        market = BinanceMarket(); await market.client.aclose()
        def handler(request):
            calls.append(request)
            return httpx.Response(429,headers={'Retry-After':'60'})
        market.client = httpx.AsyncClient(base_url='https://data-api.binance.vision',transport=httpx.MockTransport(handler))
        for _ in range(2):
            with pytest.raises(MarketError,match='частоту'):
                await market.time_ms()
        assert len(calls) == 1
        await market.close()
    asyncio.run(run())


def test_corrupt_ohlc_is_rejected():
    async def run():
        market = BinanceMarket(); await market.client.aclose()
        market.client = httpx.AsyncClient(base_url='https://data-api.binance.vision',transport=httpx.MockTransport(
            lambda request: httpx.Response(200,json=[[0,'100','98','99','101','10',59999]])))
        with pytest.raises(MarketError,match='Некорректные'):
            await market.klines('BTCUSDT','1m',1)
        await market.close()
    asyncio.run(run())

import asyncio
import time
import pytest
from app.market import MarketError
from app.service import ForecastService
from app.storage import Store
from test_storage import record
from test_analytics import candles


def test_no_future_candles_and_no_retrospective_generation(tmp_path, monkeypatch):
    now = 180_010_000
    monkeypatch.setattr(time, 'time', lambda: now/1000)
    class FakeMarket:
        async def klines(self, symbol, interval, limit, **params):
            if interval == '1m':
                return [[179_940_000,'100','101','99','100','10',179_999_999]]
            delta = 900_000 if interval == '15m' else 3_600_000
            # Deliberately return an additional open/future candle. Service must filter it.
            data = candles(1, delta, 180_000_000)
            data.append([180_000_000,'200','201','199','200','10',180_000_000+delta-1])
            return data
    store = Store('sqlite:///' + str(tmp_path/'test.db')); store.init()
    service = ForecastService(FakeMarket(), store)
    asyncio.run(service.generate('BTCUSDT', now))
    rows = store.latest('BTCUSDT')
    assert len(rows) == 2
    assert all(r['issued_at'] == now and r['slot'] == now//900_000 for r in rows)
    assert all(r['reference_at'] == 180_000_000 for r in rows)
    assert rows[0]['indicators'] == __import__('app.analytics',fromlist=['analyze']).analyze(candles(1),1)['indicators']
    asyncio.run(service.generate('BTCUSDT', now))
    assert store.history('BTCUSDT')['total'] == 2
    store.close()


def test_missing_target_is_retried_after_restart(tmp_path):
    class FakeMarket:
        price = None
        async def target_price(self, symbol, due_at, now):
            assert due_at == 183_600_000
            return self.price
    market = FakeMarket()
    url = 'sqlite:///' + str(tmp_path/'test.db')
    store = Store(url); store.init(); store.save(record(neutral_pct=.12))
    service = ForecastService(market,store)
    asyncio.run(service.settle(200_000_000))
    row = store.latest('BTCUSDT')[0]
    assert row['evaluated_at'] is None and 'отсутствует' in row['evaluation_note']
    store.close()
    store = Store(url); service = ForecastService(market,store); market.price = 101
    asyncio.run(service.settle(300_000_000))
    assert store.latest('BTCUSDT')[0]['correct']
    store.close()


def test_stale_market_does_not_create_predictions(tmp_path):
    class FakeMarket:
        async def klines(self, *args, **kwargs):
            return [[0,'100','101','99','100','10',59_999]]
    store = Store('sqlite:///' + str(tmp_path/'test.db')); store.init()
    with pytest.raises(MarketError, match='устарели'):
        asyncio.run(ForecastService(FakeMarket(),store).generate('BTCUSDT',180_000_000))
    assert store.history('BTCUSDT')['total'] == 0
    store.close()

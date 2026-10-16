from fastapi.testclient import TestClient
from app.main import app
from app.service import ForecastService
from app.market import BinanceMarket
from test_storage import record
import time


def test_api_validation_history_and_static_app(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(tmp_path/'test.db'))
    monkeypatch.delenv('RENDER',raising=False)
    async def idle(self):
        import asyncio
        await asyncio.Event().wait()
    monkeypatch.setattr(ForecastService,'run',idle)
    with TestClient(app) as client:
        assert client.get('/healthz').json() == {'status':'ok'}
        page = client.get('/')
        assert page.status_code == 200 and 'Журнал прогнозов' in page.text
        assert page.headers['x-content-type-options'] == 'nosniff'
        assert client.get('/static/app.js').status_code == 200
        for url, code in [('/api/market?symbol=BAD',400),('/api/market?interval=1s',422),('/api/history?limit=10000',422),('/api/history?offset=-1',422)]:
            assert client.get(url).status_code == code
        assert client.get('/api/forecasts').json()['items'] == []
        client.app.state.store.save(record())
        assert client.get('/api/history').json()['total'] == 1
        assert client.get('/api/history?symbol=ETHUSDT').json()['total'] == 0
        assert client.get('/api/stats').json()['items'][0]['directional_accuracy'] is None
        assert 'BTCUSDT' in client.get('/api/export.csv').text
        assert client.post('/api/forecasts', json={}).status_code == 405


def test_visitor_market_apis_only_read_shared_data_and_never_switch_collector(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(tmp_path/'shared.db'))
    monkeypatch.delenv('RENDER',raising=False)
    async def idle(self):
        import asyncio
        await asyncio.Event().wait()
    monkeypatch.setattr(ForecastService,'run',idle)
    with TestClient(app) as client:
        calls = []
        async def forbidden(*args, **kwargs):
            calls.append((args,kwargs))
            raise AssertionError('visitor must not call Binance')
        monkeypatch.setattr(BinanceMarket,'ticker',forbidden)
        monkeypatch.setattr(BinanceMarket,'klines',forbidden)
        assert not hasattr(client.app.state, 'market')
        assert not hasattr(client.app.state, 'service')
        assert client.get('/api/quote').status_code == 503
        assert client.get('/api/market').status_code == 503
        now = int(time.time()*1000)
        start = now//3_600_000*3_600_000
        quote = dict(symbol='BTCUSDT',price=100.,change_pct=1.,high=101.,low=99.,
                     volume=100.,updated_at=now,fetched_at=now)
        candle = dict(time=start-3_600_000,open=100.,high=101.,low=99.,close=100.,volume=10.,
                      close_time=start-1)
        client.app.state.store.save_quote(quote)
        client.app.state.store.save_candles('BTCUSDT','1h',[candle], confirmed_at=now)
        expected = {**quote, 'stale':False}
        assert client.get('/api/quote').json() == expected
        assert client.get('/api/market').json() == dict(
            ticker=expected,interval='1h',source='Binance',candles=[candle], stale=False,
            candles_stale=False,candles_confirmed_at=now,last_closed_at=start,
        )
        assert client.get('/api/forecasts').status_code == 200
        assert not hasattr(client.app.state, 'service')
        assert calls == []


def test_strategy_forecasts_reads_latest_database_rows_without_binance(tmp_path, monkeypatch):
    from test_strategy_storage import CLOSE, HOUR, record as strategy_record
    monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(tmp_path/'strategies.db'))
    monkeypatch.delenv('RENDER',raising=False)
    async def idle(self):
        import asyncio
        await asyncio.Event().wait()
    monkeypatch.setattr(ForecastService,'run',idle)
    with TestClient(app) as client:
        calls = []
        async def forbidden(*args, **kwargs):
            calls.append((args,kwargs))
            raise AssertionError('strategy API must never call Binance')
        for method in ('ticker','klines','time_ms','target_price'):
            monkeypatch.setattr(BinanceMarket,method,forbidden)
        store = client.app.state.store
        history_calls = []
        original = store.strategy_history
        def history(symbol, **kwargs):
            history_calls.append((symbol,kwargs))
            return original(symbol,**kwargs)
        monkeypatch.setattr(store,'strategy_history',history)

        empty = client.get('/api/strategy-forecasts')
        assert empty.status_code == 200
        assert empty.json() == {'total':0,'items':[]}
        records = []
        for symbol, count in (('BTCUSDT',3),('ETHUSDT',1)):
            for release in range(count):
                closed = CLOSE+release*HOUR
                for strategy_id in ('trend_breakout','trend_momentum','consensus'):
                    for horizon in (1,4):
                        records.append(strategy_record(
                            symbol=symbol,strategy_id=strategy_id,horizon=horizon,
                            input_close_at=closed,issued_at=closed+10_000,
                            due_at=closed+horizon*HOUR,
                        ))
        store.save_strategy_forecasts(records)
        response = client.get('/api/strategy-forecasts?symbol=BTCUSDT')
        assert response.status_code == 200
        data = response.json()
        assert data['total'] == 18 and len(data['items']) == 12
        assert {row['symbol'] for row in data['items']} == {'BTCUSDT'}
        assert {row['strategy_id'] for row in data['items']} == {'trend_breakout','trend_momentum','consensus'}
        assert {row['horizon'] for row in data['items']} == {1,4}
        assert {row['input_close_at'] for row in data['items']} == {CLOSE+HOUR,CLOSE+2*HOUR}
        assert data['items'][0]['input_close_at'] == CLOSE+2*HOUR
        assert all(isinstance(row['reason_codes'],list) and isinstance(row['indicators'],dict)
                   and row['evaluated_at'] is None for row in data['items'])
        assert response.headers['cache-control'] == 'no-store'
        eth = client.get('/api/strategy-forecasts?symbol=eth%2Fusdt').json()
        assert eth['total'] == 6
        assert {row['symbol'] for row in eth['items']} == {'ETHUSDT'}
        assert history_calls[-1] == ('ETHUSDT',{'limit':12})
        assert client.get('/api/strategy-forecasts?symbol=BAD').status_code == 400
        assert client.post('/api/strategy-forecasts',json={}).status_code == 405
        assert client.get('/api/forecasts').json() == {'items':[]}
        assert not hasattr(client.app.state, 'service')
        assert calls == []
        assert store.strategy_history('BTCUSDT')['total'] == 18

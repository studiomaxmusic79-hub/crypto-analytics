from fastapi.testclient import TestClient
from app.main import app
from app.service import ForecastService
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
        monkeypatch.setattr(client.app.state.market,'ticker',forbidden)
        monkeypatch.setattr(client.app.state.market,'klines',forbidden)
        client.app.state.service.active_symbol = 'ETHUSDT'
        assert client.get('/api/quote').status_code == 503
        assert client.get('/api/market').status_code == 503
        now = int(time.time()*1000)
        start = now//3_600_000*3_600_000
        quote = dict(symbol='BTCUSDT',price=100.,change_pct=1.,high=101.,low=99.,
                     volume=100.,updated_at=now,fetched_at=now)
        candle = dict(time=start,open=100.,high=101.,low=99.,close=100.,volume=10.,
                      close_time=start+3_600_000-1)
        client.app.state.store.save_quote(quote)
        client.app.state.store.save_candles('BTCUSDT','1h',[candle])
        client.app.state.service.candle_updates[('BTCUSDT','1h')] = now
        expected = {key:value for key,value in quote.items() if key != 'fetched_at'}
        assert client.get('/api/quote').json() == expected
        assert client.get('/api/market').json() == dict(ticker=expected,interval='1h',source='Binance',candles=[candle])
        assert client.get('/api/forecasts').status_code == 200
        assert client.app.state.service.active_symbol == 'ETHUSDT'
        assert calls == []

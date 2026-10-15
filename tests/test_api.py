from fastapi.testclient import TestClient
from app.main import app
from app.service import ForecastService
from test_storage import record


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
        for url in ['/api/market?symbol=BAD','/api/market?interval=1s','/api/history?limit=10000','/api/history?offset=-1']:
            assert client.get(url).status_code == 422
        assert client.get('/api/forecasts').json()['items'] == []
        client.app.state.store.save(record())
        assert client.get('/api/history').json()['total'] == 1
        assert client.get('/api/history?symbol=ETHUSDT').json()['total'] == 0
        assert client.get('/api/stats').json()['items'][0]['directional_accuracy'] is None
        assert 'BTCUSDT' in client.get('/api/export.csv').text
        assert client.post('/api/forecasts', json={}).status_code == 405

from concurrent.futures import ThreadPoolExecutor
from app.analytics import analyze, evaluate
from app.storage import Store, database_url
from test_analytics import candles
import pytest


def record(**changes):
    return {**analyze(candles(1), 1), 'symbol':'BTCUSDT', 'horizon':1, 'slot':200,
        'issued_at':180_000_000, 'reference_at':180_000_000, 'due_at':183_600_000,
        'base_price':100, **changes}


@pytest.fixture
def store(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'test.db')); s.init()
    yield s
    s.close()


def test_duplicate_forecasts_do_not_rewrite_original(store):
    original = record()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.save(original), range(8)))
    assert sum(results) == 1
    assert not store.save(record(direction='DOWN', base_price=123))
    row = store.latest('BTCUSDT')[0]
    assert row['direction'] == 'UP' and row['base_price'] == 100


def test_restart_persists_and_evaluation_is_immutable(store):
    store.save(record())
    row = store.pending(200_000_000)[0]
    store.resolve(row['id'], evaluate('UP', 100, 102, .12), 200_000_000)
    store.resolve(row['id'], evaluate('UP', 100, 95, .12), 200_001_000)
    reopened = Store(store.engine.url)
    try:
        after = reopened.latest('BTCUSDT')[0]
        assert after['actual_price'] == 102 and after['correct']
        for key in ('issued_at','reference_at','base_price','direction','neutral_pct','score','indicators'):
            assert after[key] == row[key]
        assert reopened.pending(300_000_000) == []
    finally:
        reopened.close()


def test_wait_statistics_are_separate(store):
    for slot, direction, actual in [(1,'UP',101),(2,'DOWN',102),(3,'WAIT',100.01),(4,'WAIT',101)]:
        store.save(record(slot=slot, direction=direction, neutral_pct=.12))
        row = store.latest('BTCUSDT')[0]
        store.resolve(row['id'], evaluate(direction,100,actual,.12),300_000_000)
    store.save(record(slot=5))
    stats = store.stats('BTCUSDT')[0]
    assert stats['directional_accuracy'] == stats['wait_accuracy'] == 50.0
    assert stats['total'] == 5 and stats['evaluated'] == 4
    assert store.stats('ETHUSDT') == []


def test_render_refuses_ephemeral_sqlite(monkeypatch):
    monkeypatch.setenv('RENDER', 'true'); monkeypatch.delenv('DATABASE_URL', raising=False)
    with pytest.raises(RuntimeError, match='PostgreSQL'):
        database_url()

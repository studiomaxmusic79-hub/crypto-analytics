"""Optional real PostgreSQL check; creates its own disposable server.

Install pgserver==0.1.4 separately to enable; not needed by the app.
"""
import pytest
import os
from sqlalchemy.engine import make_url
from app.storage import Store
from app.analytics import evaluate
from test_storage import record

pgserver = pytest.importorskip('pgserver')


def test_real_postgres_persistence_duplicate_and_scoring(tmp_path):
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        pytest.skip('Run this optional disposable-Postgres test as a non-root user')
    pg = pgserver.get_server(tmp_path / 'pgdata', cleanup_mode='stop')
    url = make_url(pg.get_uri()).set(drivername='postgresql+psycopg')
    try:
        store = Store(url); store.init()
        assert store.save(record(neutral_pct=.12))
        assert not store.save(record(direction='DOWN'))
        row = store.pending(200_000_000)[0]
        store.resolve(row['id'], evaluate('UP',100,101,.12),200_000_000)
        store.close()
        pg.cleanup()
        pg = pgserver.get_server(tmp_path / 'pgdata', cleanup_mode='stop')
        url = make_url(pg.get_uri()).set(drivername='postgresql+psycopg')
        reopened = Store(url); reopened.init()
        assert reopened.latest('BTCUSDT')[0]['actual_price'] == 101
        assert reopened.stats('BTCUSDT')[0]['directional_accuracy'] == 100
        assert reopened.history('BTCUSDT')['total'] == 1
        reopened.close()
    finally:
        pg.cleanup()

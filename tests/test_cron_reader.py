"""Cron/web separation and durable candle confirmation; no external services."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql, sqlite

from app import main as web
from app import storage as storage_module
from app.jobs import market_tick
from app.market import BinanceMarket, INTERVALS
from app.service import ForecastService, SYMBOLS
from app.storage import Store
from test_storage import record
from test_strategy_service import CLOSE, HOUR, FakeMarket


NOW = CLOSE + 60_000


def candle(interval="1h", at=NOW):
    delta = INTERVALS[interval]
    close = at // delta * delta
    return dict(time=close-delta, open=100., high=101., low=99., close=100.,
                volume=10., close_time=close-1)


def seed(store, at=NOW, symbol="BTCUSDT"):
    store.save_quote(dict(symbol=symbol, price=100., change_pct=1., high=101., low=99.,
                          volume=10., updated_at=at, fetched_at=at))
    for interval in ("15m", "1h", "4h"):
        closed = candle(interval, at)
        delta = INTERVALS[interval]
        forming = {**closed, "time": closed["time"]+delta,
                   "close_time": closed["close_time"]+delta, "close": 100.5}
        store.save_candles(symbol, interval, [closed, forming], confirmed_at=at)


@pytest.fixture
def reader(tmp_path, monkeypatch):
    url = "sqlite:///" + str(tmp_path / "reader.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("RENDER", raising=False)
    clock = {"now": NOW}
    monkeypatch.setattr(web.time, "time", lambda: clock["now"] / 1000)
    store = Store(url)
    store.init()
    seed(store)
    store.close()

    def forbidden(*args, **kwargs):
        pytest.fail("A web visitor must not construct a collector or call Binance")

    monkeypatch.setattr(BinanceMarket, "__init__", forbidden)
    monkeypatch.setattr(ForecastService, "__init__", forbidden)
    yield clock, url


def test_waking_web_reads_neon_snapshots_without_collector_and_preserves_decisions(reader, monkeypatch):
    clock, url = reader
    seeded = Store(url)
    seeded.save(record())
    original = seeded.history("BTCUSDT")
    seeded.close()

    def forbidden(*args, **kwargs):
        pytest.fail("The reader must not write market data or forecasts")

    for method in ("save", "save_quote", "save_candles", "save_strategy_forecasts",
                   "resolve", "resolve_strategy_forecast"):
        monkeypatch.setattr(Store, method, forbidden)
    for _ in range(2):  # Cold web lifespan starts with no collector state.
        with TestClient(web.app) as client:
            assert not hasattr(client.app.state, "service")
            assert not hasattr(client.app.state, "market")
            quote = client.get("/api/quote").json()
            assert quote["price"] == 100. and quote["stale"] is False
            for interval in ("15m", "1h", "4h"):
                response = client.get("/api/market", params={"interval": interval})
                assert response.status_code == 200
                data = response.json()
                assert data["stale"] is False
                assert data["candles"] == [candle(interval)]
                assert data["candles_confirmed_at"] == NOW
            for path in ("/healthz", "/api/forecasts", "/api/history", "/api/stats",
                         "/api/strategy-forecasts", "/api/export.csv"):
                assert client.get(path).status_code == 200
            status = client.get("/api/status").json()
            assert status["collector"] == "render-cron"
            assert status["generation_interval_minutes"] == 60
            assert status["last_market_update_at"] == NOW
            assert status["last_cycle_at"] is None and status["active_symbol"] is None
            assert client.app.state.store.history("BTCUSDT") == original


@pytest.mark.parametrize("age,stale", [(59*60_000, False), (70*60_000, False),
                                      (70*60_000+1, True), (4*HOUR, True)])
def test_missed_cron_returns_saved_data_with_explicit_age_flag(reader, age, stale):
    clock, url = reader
    clock["now"] += age
    with TestClient(web.app) as client:
        quote = client.get("/api/quote")
        assert quote.status_code == 200
        assert quote.json()["stale"] is stale
        assert quote.json()["fetched_at"] == NOW
        for interval in ("15m", "1h", "4h"):
            response = client.get("/api/market", params={"interval": interval})
            assert response.status_code == 200
            assert response.json()["stale"] is stale
            assert response.json()["candles"] == [candle(interval)]


def test_fresh_quote_cannot_hide_failed_candle_refresh_and_pairs_are_independent(reader):
    clock, url = reader
    clock["now"] += 2 * HOUR
    store = Store(url)
    seed(store, clock["now"], "ETHUSDT")
    quote = store.get_quote("BTCUSDT")
    store.save_quote({**quote, "updated_at": clock["now"], "fetched_at": clock["now"]})
    store.close()
    with TestClient(web.app) as client:
        btc = client.get("/api/market?symbol=BTCUSDT").json()
        eth = client.get("/api/market?symbol=ETHUSDT").json()
        assert btc["ticker"]["stale"] is False and btc["candles_stale"] is True
        assert btc["stale"] is True and eth["stale"] is False


def test_legacy_unknown_snapshot_is_shown_as_unconfirmed_not_certified_by_elapsed_time(reader):
    clock, url = reader
    store = Store(url)
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE market_candles SET confirmed_at = NULL"))
    store.close()
    clock["now"] += 2 * HOUR
    with TestClient(web.app) as client:
        response = client.get("/api/market")
        assert response.status_code == 200
        data = response.json()
        assert data["stale"] is True and data["candles_stale"] is True
        assert data["candles_confirmed_at"] is None
        assert len(data["candles"]) == 2


def test_initially_empty_database_returns_clear_503_without_starting_collection(reader):
    with TestClient(web.app) as client:
        for path in ("/api/quote?symbol=SOLUSDT", "/api/market?symbol=SOLUSDT"):
            response = client.get(path)
            assert response.status_code == 503
            assert "В базе пока нет" in response.json()["detail"]


def test_init_migrates_existing_candles_without_certifying_or_losing_them(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "old.db"))
    old = candle()
    with store.engine.begin() as conn:
        conn.execute(text("""CREATE TABLE market_candles (
            symbol VARCHAR(12), interval VARCHAR(4), time BIGINT, open FLOAT,
            high FLOAT, low FLOAT, close FLOAT, volume FLOAT, close_time BIGINT,
            PRIMARY KEY(symbol, interval, time))"""))
        conn.execute(text("""INSERT INTO market_candles VALUES
            ('BTCUSDT', '1h', :time, :open, :high, :low, :close, :volume, :close_time)"""), old)
    store.init()
    store.save(record())
    store.init()
    assert store.get_candles("BTCUSDT", "1h") == [old]
    assert store.get_candles("BTCUSDT", "1h", confirmed_only=True) == []
    assert "confirmed_at" in {c["name"] for c in inspect(store.engine).get_columns("market_candles")}
    assert len(store.latest("BTCUSDT")) == 1
    assert set(inspect(store.engine).get_table_names()) == {
        "forecasts", "strategy_forecasts", "market_quotes", "market_candles",
        "market_candle_revisions", "market_data_state",
        "strategy_publish_observations",
    }
    store.close()


def test_confirmation_requires_post_close_fetch_and_cannot_be_downgraded(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "confirmed.db"))
    store.init()
    row = candle()
    cutoff = row["close_time"] + 1
    store.save_candles("BTCUSDT", "1h", [row], confirmed_at=cutoff-1)
    assert store.get_candles("BTCUSDT", "1h", confirmed_only=True) == []
    final = {**row, "close": 100.5}
    store.save_candles("BTCUSDT", "1h", [final], confirmed_at=cutoff)
    for timestamp in (None, cutoff-1):
        store.save_candles("BTCUSDT", "1h", [row], confirmed_at=timestamp)
    assert store.get_candles("BTCUSDT", "1h") == [final]
    assert store.get_candles("BTCUSDT", "1h", confirmed_only=True) == [
        {**final, "confirmed_at": cutoff},
    ]
    store.close()


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_confirmation_upsert_compiles_for_both_databases(dialect):
    store = object.__new__(Store)
    store.engine = MagicMock()
    store.engine.dialect.name = dialect.name
    conn = store.engine.connect.return_value.__enter__.return_value
    conn.dialect.name = dialect.name
    conn.execute.return_value.mappings.return_value = []
    conn.execute.return_value.scalar_one.return_value = 1
    store.save_candles("BTCUSDT", "1h", [candle()], confirmed_at=NOW)
    statement, values = conn.execute.call_args.args
    sql = str(statement.compile(dialect=dialect))
    assert "ON CONFLICT" in sql and "confirmed_at" in sql and "observation_id" in sql
    assert values[0]["confirmed_at"] == NOW
    if dialect.name == "postgresql":
        assert "pg_advisory_xact_lock" in str(conn.execute.call_args_list[0].args[0])
    else:
        conn.exec_driver_sql.assert_called_once_with("BEGIN IMMEDIATE")


def test_postgres_column_migration_is_repeatable(monkeypatch):
    store = object.__new__(Store)
    store.engine = MagicMock()
    conn = store.engine.connect.return_value.__enter__.return_value
    conn.dialect.name = "postgresql"
    monkeypatch.setattr(storage_module.metadata, "create_all", lambda engine: None)
    mocked_inspector = MagicMock()
    mocked_inspector.get_columns.return_value = [{"name": "close_time"}]
    monkeypatch.setattr(storage_module, "inspect", lambda connection: mocked_inspector)
    store.init()
    statements = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert "ALTER TABLE market_candles ADD COLUMN IF NOT EXISTS confirmed_at BIGINT" in statements
    assert "ALTER TABLE market_candles ADD COLUMN IF NOT EXISTS observation_id BIGINT" in statements
    assert "ALTER TABLE strategy_forecasts ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMP WITH TIME ZONE" in statements
    assert "pg_advisory_xact_lock" in statements[0]


def test_next_successful_hour_restores_data_and_checks_only_real_forecasts(tmp_path, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(web.time, "time", lambda: clock["now"] / 1000)
    url = "sqlite:///" + str(tmp_path / "recovery.db")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("RENDER", raising=False)
    markets = []

    def make_market():
        market = FakeMarket(clock)
        async def close():
            pass
        market.close = close
        markets.append(market)
        return market

    monkeypatch.setattr(market_tick, "BinanceMarket", make_market)
    assert market_tick.main() == 0
    clock["now"] = CLOSE + 3*HOUR + 300_000  # Late cron: recover, never issue LIVE.
    assert market_tick.main() == 0
    store = Store(url)
    for symbol in SYMBOLS:
        rows = store.strategy_history(symbol, strategy_id="krypta_mtf")["items"]
        assert len(rows) == 2 and {r["input_close_at"] for r in rows} == {CLOSE}
        assert next(r for r in rows if r["horizon"] == 1)["evaluated_at"] is not None
        assert store.get_candles(symbol, "1h", confirmed_only=True)[-1]["close_time"]+1 == CLOSE+3*HOUR
    store.close()
    with TestClient(web.app) as client:
        assert client.get("/api/market").json()["stale"] is False
    clock["now"] = CLOSE + 4*HOUR + 60_000
    assert market_tick.main() == 0
    store = Store(url)
    for symbol in SYMBOLS:
        rows = store.strategy_history(symbol, strategy_id="krypta_mtf")["items"]
        assert {r["input_close_at"] for r in rows} == {CLOSE, CLOSE+4*HOUR}
        assert all(r["evaluated_at"] is not None for r in rows if r["input_close_at"] == CLOSE)
    store.close()

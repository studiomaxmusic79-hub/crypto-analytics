"""Point-in-time inputs: disposable SQLite only, synthetic candles, no network."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import copy
import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app import krypta_strategy as krypta
from app.market_inputs import INPUT_SCHEMA, decimal_text, input_hash, strategy_provenance
from app.storage import (Store, market_candles, market_candle_revisions,
                         market_data_state, strategy_forecasts)
from test_krypta_live import live, run, releases
from test_strategy_service import CLOSE, HOUR
from test_strategy_storage import record


@pytest.fixture
def store(tmp_path):
    instance = Store("sqlite:///" + str(tmp_path / "versions.db"))
    instance.init()
    yield instance
    instance.close()


def bar(price="100", start=0):
    return dict(time=start, open="100", high="200", low="50", close=price,
                volume="123.456789012345678901", close_time=start+HOUR-1)


def current(store):
    with store.engine.connect() as conn:
        c = market_candles.c
        return dict(conn.execute(select(market_candles).where(
            c.symbol == "BTCUSDT", c.interval == "1h", c.time == 0,
        )).mappings().one())


def revisions(store):
    with store.engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(market_candle_revisions)
                                              .order_by(market_candle_revisions.c.observation_id)).mappings()]


def save(store, price="100", now=HOUR, start=0):
    store.save_candles("BTCUSDT", "1h", [bar(price, start)], confirmed_at=now)
    return current(store) if start == 0 else None


def test_identical_normalized_confirmation_is_not_a_new_version(store):
    first = save(store)
    second = save(store, "100.000", HOUR+1)
    assert second["observation_id"] == first["observation_id"]
    assert second["observed_at"] == first["observed_at"]
    assert second["confirmed_at"] == HOUR+1
    assert revisions(store) == []


def test_correction_and_reversion_are_three_distinct_versions(store):
    first = save(store)
    second = save(store, "101", HOUR+1)
    third = save(store, "100", HOUR+2)
    assert first["observation_id"] < second["observation_id"] < third["observation_id"]
    assert revisions(store) == [first, second]
    for snapshot, expected in ((first, "100"), (second, "101"), (third, "100")):
        rows = store.get_candles_at("BTCUSDT", "1h", HOUR, snapshot["observation_id"])
        assert len(rows) == 1 and rows[0]["close"] == expected
        assert rows[0]["volume"] == "123.456789012345678901"


def test_unconfirmed_and_stale_response_cannot_destroy_confirmed_history(store):
    first = save(store)
    for now in (None, HOUR-1):
        store.save_candles("BTCUSDT", "1h", [bar("99")], confirmed_at=now)
    assert current(store) == first and revisions(store) == []
    second = save(store, "101", HOUR+2)
    save(store, "102", HOUR+1)
    assert current(store) == second and revisions(store) == [first]


def test_forming_then_confirmed_gets_first_observation_only_after_close(store):
    store.save_candles("BTCUSDT", "1h", [bar()], confirmed_at=HOUR-1)
    assert current(store)["observation_id"] is None
    assert current(store)["observed_at"] is None
    assert store.get_candles_at("BTCUSDT", "1h", HOUR, 0) == []
    first = save(store)
    assert first["observation_id"] > 0 and first["observed_at"] is not None
    assert revisions(store) == []


def test_no_precision_loss_even_when_float_projection_is_identical(store):
    first = save(store, "100.000000000000000001")
    second = save(store, "100.000000000000000002", HOUR+1)
    assert first["close"] == second["close"]  # Existing Float/API representation.
    assert first["observation_id"] != second["observation_id"]
    assert store.get_candles_at("BTCUSDT", "1h", HOUR, first["observation_id"])[0]["close"] == "100.000000000000000001"
    assert revisions(store) == [first]


def test_later_first_observation_and_future_candle_excluded_from_replay(store):
    save(store, start=HOUR, now=3*HOUR)
    snapshot = store.strategy_inputs("BTCUSDT", 2*HOUR, {"1h": 5})
    save(store, "99", now=3*HOUR)  # Older candle, discovered only afterwards.
    save(store, "101", start=2*HOUR, now=3*HOUR)
    replay = store.strategy_inputs("BTCUSDT", 2*HOUR, {"1h": 5},
                                  cutoff_id=snapshot["input_data_cutoff_id"])
    assert replay == snapshot
    assert [r["time"] for r in replay["candles"]["1h"]] == [HOUR]
    latest = store.strategy_inputs("BTCUSDT", 2*HOUR, {"1h": 5})
    assert [r["time"] for r in latest["candles"]["1h"]] == [0, HOUR]


def test_archive_current_and_allocator_rollback_together(store):
    first = save(store)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO market_candles"):
            raise SQLAlchemyError("simulated write failure after archival")

    event.listen(store.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(SQLAlchemyError):
            save(store, "101", HOUR+1)
    finally:
        event.remove(store.engine, "before_cursor_execute", fail)
    assert current(store) == first and revisions(store) == []
    assert store.strategy_inputs("BTCUSDT", HOUR, {"1h": 1})["input_data_cutoff_id"] == first["observation_id"]


def test_global_allocator_and_concurrent_writers_have_no_lost_versions(store):
    save(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda price: save(store, price, HOUR+1), ("101", "102")))
    old = revisions(store)
    assert len(old) == 2
    ids = [r["observation_id"] for r in old] + [current(store)["observation_id"]]
    assert ids == sorted(set(ids))
    store.save_candles("ETHUSDT", "15m", [bar()], confirmed_at=HOUR+1)
    snapshot = store.strategy_inputs("ETHUSDT", HOUR, {"15m": 1})
    assert snapshot["input_data_cutoff_id"] > max(ids)


def test_snapshot_never_captures_uncommitted_allocated_id(store):
    first = save(store)
    writing, release, reading = Event(), Event(), Event()

    def pause(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO market_candles"):
            writing.set()
            assert release.wait(10)

    def snapshot():
        reading.set()
        return store.strategy_inputs("BTCUSDT", HOUR, {"1h": 1})

    event.listen(store.engine, "before_cursor_execute", pause)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writer = pool.submit(save, store, "101", HOUR+1)
            assert writing.wait(10)
            reader = pool.submit(snapshot)
            assert reading.wait(10)
            assert not reader.done()
            # A normal reader still sees only the previously committed version.
            assert current(store)["observation_id"] == first["observation_id"]
            release.set()
            writer.result(timeout=10)
            result = reader.result(timeout=10)
    finally:
        release.set()
        event.remove(store.engine, "before_cursor_execute", pause)
    assert result["candles"]["1h"][0]["close"] == "101"
    assert result["input_data_cutoff_id"] == current(store)["observation_id"]


def test_existing_rows_are_unknown_until_reobserved_and_boundary_is_stable(tmp_path):
    instance = Store("sqlite:///" + str(tmp_path / "legacy.db"))
    with instance.engine.begin() as conn:
        conn.execute(text("""CREATE TABLE market_candles (
            symbol VARCHAR(12), interval VARCHAR(4), time BIGINT, open FLOAT,
            high FLOAT, low FLOAT, close FLOAT, volume FLOAT, close_time BIGINT,
            confirmed_at BIGINT, PRIMARY KEY(symbol, interval, time))"""))
        conn.execute(text("INSERT INTO market_candles VALUES ('BTCUSDT','1h',0,100,200,50,100,1,3599999,3600000)"))
    try:
        instance.init()
        original = current(instance)
        assert original["observation_id"] is original["observed_at"] is None
        before = instance.strategy_inputs("BTCUSDT", HOUR, {"1h": 1})
        assert before["candles"]["1h"] == []
        instance.init()
        assert before == instance.strategy_inputs("BTCUSDT", HOUR, {"1h": 1})
        save(instance)
        assert current(instance)["observed_at"] is not None
        assert revisions(instance) == []  # No fabricated pre-migration version.
    finally:
        instance.close()


@pytest.mark.parametrize("number,expected", [
    ("100.000", "100"), ("-0.00", "0"), ("1e-8", "0.00000001"),
    ("123456789012345678901234567890.00001", "123456789012345678901234567890.00001"),
])
def test_decimal_normalization_is_exact(number, expected):
    assert decimal_text(number) == expected


def test_hash_is_deterministic_versioned_and_covers_exact_fields():
    data = {tf: [bar()] for tf in ("15m", "1h", "4h")}
    original = copy.deepcopy(data)
    digest = input_hash(data)
    assert len(digest) == 64 and data == original
    reordered = {tf: [{**bar("100.000"), "ignored": 42}] for tf in reversed(data)}
    assert input_hash(reordered) == digest
    for field in ("open", "high", "low", "close", "volume", "time", "close_time"):
        changed = copy.deepcopy(data)
        changed["1h"][0][field] = changed["1h"][0][field]+1 if field in ("time", "close_time") else "100.000000000000000001"
        assert input_hash(changed) != digest


def test_provenance_records_deployed_commit_and_fixed_parameters(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a"*40)
    provenance = strategy_provenance(krypta)
    assert provenance["mode"] == "LIVE" and provenance["git_commit_sha"] == "a"*40
    assert provenance["input_schema"] == INPUT_SCHEMA
    assert provenance["parameters"]["window_bars"] == krypta.WINDOW_BARS
    assert provenance["parameters"]["trend_threshold"] == .5
    assert provenance["parameters"]["momentum_threshold"] == 1.


def test_live_hash_replay_and_duplicate_tick_keep_original_snapshot(live, monkeypatch):
    clock, instance, market, service = live
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a"*40)
    run(service.cycle())
    first = releases(instance)
    assert len(first) == 2
    for row in first:
        assert row["recorded_at"] is not None
        snapshot = instance.strategy_inputs("BTCUSDT", CLOSE, krypta.WINDOW_BARS,
                                            cutoff_id=row["input_data_cutoff_id"])
        assert input_hash(snapshot["candles"]) == row["input_hash"]
        assert krypta.analyze(snapshot["candles"], reference_time=CLOSE)["signal"] == row["direction"]
        assert row["indicators"]["provenance"]["mode"] == "LIVE"
    latest = instance.get_candles("BTCUSDT", "1h", 2)[0]
    instance.save_candles("BTCUSDT", "1h", [{**latest, "volume": "999999.00001"}],
                          confirmed_at=clock["now"]+1)
    run(service.cycle())
    assert releases(instance) == first
    replay = instance.strategy_inputs("BTCUSDT", CLOSE, krypta.WINDOW_BARS,
                                      cutoff_id=first[0]["input_data_cutoff_id"])
    assert input_hash(replay["candles"]) == first[0]["input_hash"]
    assert all(c["close_time"] < CLOSE for bars in replay["candles"].values() for c in bars)
    assert replay["candles"]["4h"][-1]["close_time"] < CLOSE


def test_recorded_at_is_database_generated_and_cannot_be_supplied(store):
    store.save_strategy_forecasts([record()])
    row = store.strategy_history("BTCUSDT")["items"][0]
    assert row["recorded_at"] is not None
    assert row["input_data_cutoff_id"] is row["input_hash"] is None
    with pytest.raises(ValueError):
        store.save_strategy_forecasts([record(recorded_at=row["recorded_at"])])
    store.save_strategy_forecasts([record()])
    assert store.strategy_history("BTCUSDT")["items"][0] == row


def test_existing_forecast_is_not_backdated_and_new_record_uses_sqlite_db_time(tmp_path):
    instance = Store("sqlite:///" + str(tmp_path / "old_forecast.db"))
    original = record()
    original["reason_codes"] = json.dumps(original["reason_codes"])
    original["indicators"] = json.dumps(original["indicators"])
    ddl = str(CreateTable(strategy_forecasts).compile(dialect=sqlite.dialect()))
    ddl = "\n".join(line for line in ddl.splitlines() if not any(
        field in line for field in ("input_data_cutoff_id", "input_hash", "recorded_at")
    ))
    with instance.engine.begin() as conn:
        conn.exec_driver_sql(ddl)
        conn.execute(strategy_forecasts.insert().values(**original))
    try:
        instance.init()
        instance.init()
        old = instance.strategy_history("BTCUSDT")["items"][0]
        assert old["recorded_at"] is old["input_hash"] is old["input_data_cutoff_id"] is None
        instance.save_strategy_forecasts([record(strategy_id="trend_momentum")])
        new = instance.strategy_history("BTCUSDT", strategy_id="trend_momentum")["items"][0]
        assert new["recorded_at"] is not None
        with instance.engine.connect() as conn:
            db_date = conn.scalar(text("SELECT date(CURRENT_TIMESTAMP)"))
        assert new["recorded_at"].date().isoformat() == db_date
        assert instance.strategy_history("BTCUSDT", strategy_id="trend_breakout")["items"][0] == old
    finally:
        instance.close()


def test_hash_is_of_actual_analyze_arguments_even_if_correction_arrives_during_calculation(live, monkeypatch):
    clock, instance, market, service = live
    monkeypatch.setenv("RENDER_GIT_COMMIT", "b"*40)
    for interval in krypta.WINDOW_BARS:
        run(service.collect_candles("BTCUSDT", interval, clock["now"]))
    analyze = krypta.analyze
    captured = {}

    def calculate(candles, *, reference_time):
        captured["hash"] = input_hash(candles)
        captured["input"] = copy.deepcopy(candles)
        # Simulate another writer AFTER the captured input was committed/read.
        latest = instance.get_candles("BTCUSDT", "1h", 2)[0]
        instance.save_candles("BTCUSDT", "1h", [{**latest, "volume": "987654321.0123456789"}],
                              confirmed_at=clock["now"]+1)
        return analyze(candles, reference_time=reference_time)

    monkeypatch.setattr(krypta, "analyze", calculate)
    run(service.generate_krypta("BTCUSDT", clock["now"]))
    for row in releases(instance):
        assert row["input_hash"] == captured["hash"]
        replay = instance.strategy_inputs("BTCUSDT", CLOSE, krypta.WINDOW_BARS,
                                          cutoff_id=row["input_data_cutoff_id"])
        assert replay["candles"] == captured["input"]
        assert input_hash(replay["candles"]) == row["input_hash"]
        assert instance.strategy_inputs("BTCUSDT", CLOSE, krypta.WINDOW_BARS)["input_data_cutoff_id"] > row["input_data_cutoff_id"]


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
def test_version_schema_and_point_in_time_sql_compile_for_both_dialects(dialect):
    for table in (market_candles, market_candle_revisions, market_data_state, strategy_forecasts):
        assert str(CreateTable(table).compile(dialect=dialect))
    conn = MagicMock()
    conn.execute.return_value.mappings.return_value.all.return_value = []
    assert Store._candles_at(conn, "BTCUSDT", "1h", HOUR, 7, 500) == []
    sql = str(conn.execute.call_args.args[0].compile(dialect=dialect))
    assert "UNION ALL" in sql and "row_number() OVER" in sql
    assert "observation_id <=" in sql and "close_time <" in sql


@pytest.mark.parametrize("cutoff,digest", [(1, None), (None, "a"*64),
                                          (0, "a"*64), (1, "not-a-hash")])
def test_cutoff_and_hash_must_be_a_valid_pair(store, cutoff, digest):
    with pytest.raises(ValueError, match="cutoff"):
        store.save_strategy_forecasts([record(input_data_cutoff_id=cutoff, input_hash=digest)])

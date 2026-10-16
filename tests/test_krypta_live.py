"""Krypta LIVE integration: synthetic Binance, disposable SQLite, no network."""
import asyncio

import pytest
from sqlalchemy import delete, event
from sqlalchemy.exc import SQLAlchemyError

from app import krypta_strategy as krypta
from app import service as service_module
from app.market import MarketError
from app.service import ForecastService, SYMBOLS
from app.storage import Store, strategy_forecasts
from test_strategy_service import CLOSE, HOUR, FakeMarket


@pytest.fixture
def live(tmp_path, monkeypatch):
    clock = {"now": CLOSE + 60_000}
    monkeypatch.setattr(service_module.time, "time", lambda: clock["now"] / 1000)
    store = Store("sqlite:///" + str(tmp_path / "krypta_live.db"))
    store.init()
    market = FakeMarket(clock)
    service = ForecastService(market, store, enable_krypta=True)
    try:
        yield clock, store, market, service
    finally:
        store.close()


def run(coroutine):
    return asyncio.run(coroutine)


def releases(store, symbol="BTCUSDT", reference=CLOSE):
    return [row for row in store.strategy_history(
        symbol, strategy_id=krypta.STRATEGY_ID,
        strategy_version=krypta.STRATEGY_VERSION,
    )["items"] if row["input_close_at"] == reference]


def set_direction(market, direction):
    original = market.bar

    def bar(symbol, interval, start):
        row = original(symbol, interval, start)
        if direction == "DOWN":
            price = 5000. - start / HOUR
        elif direction == "WAIT":
            price = 2000.
        else:
            return row
        row[1:5] = [price, price + .1, price - .1, price]
        return row

    market.bar = bar


@pytest.mark.parametrize("direction", ["UP", "DOWN", "WAIT"])
def test_real_calculation_persists_one_release_with_complete_diagnostics(live, direction):
    clock, store, market, service = live
    set_direction(market, direction)
    run(service.cycle())
    for symbol in SYMBOLS:
        rows = releases(store, symbol)
        assert len(rows) == 2  # One decision, two existing evaluation horizons.
        assert {r["horizon"] for r in rows} == {1, 4}
        assert {r["direction"] for r in rows} == {direction}
        assert {r["issued_at"] for r in rows} == {clock["now"]}
        closed = {
            interval: [c for c in store.get_candles(symbol, interval, count + 1)
                       if c["close_time"] < CLOSE]
            for interval, count in krypta.WINDOW_BARS.items()
        }
        expected = krypta.analyze(closed, reference_time=CLOSE)
        assert expected["signal"] == direction
        for row in rows:
            assert row["base_price"] == closed["1h"][-1]["close"]
            assert row["due_at"] == CLOSE + row["horizon"] * HOUR
            assert row["score"] == expected["T1h"]
            assert row["reason_codes"] == expected["reason_codes"]
            assert all(row["indicators"][k] == v for k, v in expected.items())
            assert row["indicators"]["score_source"] == "T1h"
            assert row["indicators"]["confirmed_at"] == {
                interval: clock["now"] for interval in krypta.INTERVAL_MS
            }
            assert row["correct"] is None and row["evaluated_at"] is None
            assert "confidence" not in row["indicators"]
            assert "probability" not in row["indicators"]
    assert len(store.get_candles("BTCUSDT", "4h", 201)) == 201
    calls = [c for c in market.calls if c[0] == "klines" and c[2] == "4h"]
    assert len(calls) == 2 and all(c[3] == 201 for c in calls)


@pytest.mark.parametrize("offset,expected", [
    (-1, 0), (0, 2), (299_999, 2), (300_000, 0), (300_001, 0), (600_000, 0),
])
def test_live_deadline_is_exclusive_of_fifth_minute(live, offset, expected):
    clock, store, market, service = live
    clock["now"] = CLOSE + offset
    run(service.cycle())
    assert len(releases(store)) == expected
    assert len(releases(store, "ETHUSDT")) == expected


def test_slow_calculation_cannot_publish_after_deadline(live, monkeypatch):
    clock, store, market, service = live
    clock["now"] = CLOSE + 299_999
    original = krypta.analyze

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        clock["now"] += 1
        return result

    monkeypatch.setattr(krypta, "analyze", slow)
    run(service.cycle())
    assert releases(store) == releases(store, "ETHUSDT") == []


def test_write_thread_queue_delay_cannot_backdate_publication(live, monkeypatch):
    clock, store, market, service = live
    original = asyncio.to_thread

    async def delayed(function, *args, **kwargs):
        if function.__name__ == "publish":
            clock["now"] = CLOSE + 300_000
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", delayed)
    run(service.cycle())
    assert releases(store) == releases(store, "ETHUSDT") == []


def test_unsynchronized_publication_clock_does_not_issue(live, monkeypatch):
    clock, store, market, service = live
    monkeypatch.setattr(service_module.time, "time", lambda: (clock["now"] + 61_000) / 1000)
    run(service.cycle())
    assert releases(store) == releases(store, "ETHUSDT") == []
    assert "clocks are not synchronized" in service.errors["BTCUSDT:krypta"]


def test_retries_and_restart_preserve_original_decision_and_issue_time(live, monkeypatch):
    clock, store, market, service = live
    run(service.cycle())
    before = {symbol: releases(store, symbol) for symbol in SYMBOLS}

    def forbidden(*args, **kwargs):
        pytest.fail("An existing decision must not be recalculated")

    monkeypatch.setattr(krypta, "analyze", forbidden)
    clock["now"] += 60_000
    run(service.cycle())
    set_direction(market, "DOWN")  # A later input must not replace publication.
    restarted = ForecastService(market, store, enable_krypta=True)
    clock["now"] += 60_000
    run(restarted.cycle())
    assert {symbol: releases(store, symbol) for symbol in SYMBOLS} == before


@pytest.mark.parametrize("interval", ["15m", "1h", "4h"])
def test_saved_forming_candle_without_post_close_refresh_cannot_issue(live, interval):
    clock, store, market, service = live
    clock["now"] = CLOSE - 60_000
    for timeframe in krypta.INTERVAL_MS:
        run(service.collect_candles("BTCUSDT", timeframe, clock["now"]))
    market.fail.add(("BTCUSDT", interval))
    clock["now"] = CLOSE + 60_000
    run(service.cycle())
    assert releases(store) == []
    assert len(releases(store, "ETHUSDT")) == 2
    assert "not confirmed" in service.errors["BTCUSDT:krypta"]
    market.fail.clear()
    clock["now"] += 60_000
    run(service.cycle())
    assert len(releases(store)) == 2


def test_hourly_cutoff_excludes_forming_4h_at_13_utc_and_all_future_inputs(live, monkeypatch):
    clock, store, market, service = live
    # CLOSE is midnight UTC; analyze at 13:00 without the open 12:00-16:00 bar.
    clock["now"] = CLOSE + 13 * HOUR + 60_000
    reference = CLOSE + 13 * HOUR
    original = krypta.analyze
    snapshots = []

    def capture(inputs, *, reference_time):
        snapshots.append(inputs)
        assert reference_time == reference
        assert all(c["close_time"] < reference for rows in inputs.values() for c in rows)
        assert inputs["4h"][-1]["close_time"] + 1 == CLOSE + 12 * HOUR
        return original(inputs, reference_time=reference_time)

    monkeypatch.setattr(krypta, "analyze", capture)
    run(service.cycle())
    assert len(snapshots) == 2
    assert len(releases(store, reference=reference)) == 2


def test_no_closed_hourly_input_does_not_fall_back_to_an_older_release(live):
    clock, store, market, service = live
    old = CLOSE - HOUR - 1
    for interval in krypta.INTERVAL_MS:
        raw = run(market.klines("BTCUSDT", interval, 501, endTime=old))
        store.save_candles("BTCUSDT", interval, service.candle_values(raw))
        service.candle_updates[("BTCUSDT", interval)] = clock["now"]
    with pytest.raises(MarketError, match="latest closed hourly candle"):
        run(service.generate_krypta("BTCUSDT", clock["now"]))
    assert store.strategy_history("BTCUSDT", strategy_id=krypta.STRATEGY_ID)["total"] == 0


@pytest.mark.parametrize("kind", ["missing", "zero_atr"])
def test_technical_results_are_not_persisted_as_wait(live, monkeypatch, kind, caplog):
    clock, store, market, service = live
    for interval in krypta.INTERVAL_MS:
        run(service.collect_candles("BTCUSDT", interval, clock["now"]))
    if kind == "missing":
        # Keep valid latest candles/confirmation but remove a warm-up candle.
        original = store.get_candles

        def missing(symbol, interval, limit=200):
            saved = original(symbol, interval, limit)
            return saved[1:] if interval == "4h" else saved

        monkeypatch.setattr(store, "get_candles", missing)
        expected = "DATA_UNAVAILABLE: INSUFFICIENT_HISTORY"
    else:
        saved = store.get_candles("BTCUSDT", "15m", 200)
        for c in saved:
            c.update(open=1000., high=1000., low=1000., close=1000.)
        store.save_candles("BTCUSDT", "15m", saved, confirmed_at=clock["now"])
        expected = "SKIPPED: ZERO_ATR"
    with pytest.raises(MarketError, match=expected):
        run(service.generate_krypta("BTCUSDT", clock["now"]))
    assert releases(store) == []
    assert expected.split(": ")[0] in caplog.text


def test_calculation_exception_is_visible_and_does_not_block_eth(live, monkeypatch, caplog):
    clock, store, market, service = live
    original = krypta.analyze
    attempts = []

    def failing_once(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("simulated calculation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(krypta, "analyze", failing_once)
    run(service.cycle())
    assert releases(store) == []
    assert len(releases(store, "ETHUSDT")) == 2
    assert "RuntimeError" in service.errors["BTCUSDT:krypta"]
    assert "simulated calculation failure" in caplog.text
    clock["now"] += 60_000
    run(service.cycle())
    assert len(releases(store)) == 2


def test_atomic_database_failure_does_not_block_eth_or_occupy_live_key(live):
    clock, store, market, service = live

    def fail_after_write(conn, cursor, statement, parameters, context, executemany):
        if context.isinsert and context.compiled.statement.table is strategy_forecasts:
            values = context.compiled_parameters[0]
            if (values["strategy_id"] == krypta.STRATEGY_ID
                    and values["symbol"] == "BTCUSDT" and values["horizon"] == 4):
                raise SQLAlchemyError("simulated Krypta write failure")

    event.listen(store.engine, "after_cursor_execute", fail_after_write)
    try:
        run(service.cycle())
    finally:
        event.remove(store.engine, "after_cursor_execute", fail_after_write)
    assert releases(store) == []  # The first horizon was rolled back as well.
    assert len(releases(store, "ETHUSDT")) == 2
    assert "SQLAlchemyError" in service.errors["BTCUSDT:krypta"]
    clock["now"] += 60_000
    run(service.cycle())
    assert len(releases(store)) == 2


def test_partial_existing_release_is_not_rewritten_or_completed(live):
    clock, store, market, service = live
    run(service.cycle())
    with store.engine.begin() as conn:
        conn.execute(delete(strategy_forecasts).where(
            strategy_forecasts.c.strategy_id == krypta.STRATEGY_ID,
            strategy_forecasts.c.symbol == "BTCUSDT",
            strategy_forecasts.c.horizon == 4,
        ))
    before = releases(store)
    clock["now"] += 60_000
    run(service.cycle())
    assert releases(store) == before
    assert "incomplete published release" in service.errors["BTCUSDT:krypta"]


@pytest.mark.parametrize("direction", ["UP", "WAIT"])
def test_existing_settlement_checks_exact_hourly_targets_after_restart(live, direction):
    clock, store, market, service = live
    set_direction(market, direction)
    run(service.cycle())
    original = releases(store)
    for horizon in (1, 4):
        clock["now"] = CLOSE + horizon * HOUR + 60_000
        restarted = ForecastService(market, store, enable_krypta=True)
        run(restarted.cycle())
        row = next(r for r in releases(store) if r["horizon"] == horizon)
        expected_price = market.bar("BTCUSDT", "1h", CLOSE + (horizon - 1) * HOUR)[4]
        assert row["actual_price"] == expected_price
        assert row["evaluated_at"] == clock["now"]
        assert row["issued_at"] == original[0]["issued_at"]
        # One synthetic hourly step is below the frozen 0.10% FLAT minimum.
        expected_observed = "UP" if direction == "UP" and horizon == 4 else "FLAT"
        assert row["observed_direction"] == expected_observed
        assert row["correct"] is (None if direction == "WAIT" else horizon == 4)


def test_sleep_does_not_backfill_missed_live_hours(live):
    clock, store, market, service = live
    clock["now"] = CLOSE + 3 * HOUR + 600_000
    run(service.cycle())
    assert store.strategy_history("BTCUSDT", strategy_id=krypta.STRATEGY_ID)["total"] == 0
    clock["now"] = CLOSE + 4 * HOUR + 60_000
    run(service.cycle())
    assert len(releases(store, reference=CLOSE + 4 * HOUR)) == 2
    assert store.strategy_history("BTCUSDT", strategy_id=krypta.STRATEGY_ID)["total"] == 2


def test_default_fastapi_service_does_not_enable_krypta(live):
    clock, store, market, service = live
    old_service = ForecastService(market, store)
    run(old_service.cycle())
    assert releases(store) == []
    assert store.strategy_history("BTCUSDT")["total"] == 6
    assert len(store.get_candles("BTCUSDT", "4h", 201)) == 200


def test_enabling_krypta_adds_no_binance_requests(live, tmp_path):
    clock, store, market, service = live
    run(service.cycle())
    other = Store("sqlite:///" + str(tmp_path / "legacy.db"))
    other.init()
    old_market = FakeMarket(clock)
    try:
        run(ForecastService(old_market, other).cycle())
    finally:
        other.close()
    assert len(market.calls) == len(old_market.calls)
    for actual, old in zip(market.calls, old_market.calls):
        if actual[0] == "klines" and actual[2] == "4h":
            assert actual[3] == 201 and old[3] == 200
            assert actual[:3] == old[:3] and actual[4] == old[4]
        else:
            assert actual == old

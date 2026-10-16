"""Protocol tests use synthetic quotes/candles and disposable SQLite only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import event, select, update, text

from app import evaluation_protocol as protocol
from app.market import BinanceMarket, MarketError
from app.service import ForecastService
from app.storage import (Store, market_candles, market_data_state, publish_observations)
from app.strategies import evaluate_result
from test_krypta_live import live, run, releases
from test_strategy_storage import store, record, first, evaluation, CLOSE, HOUR


def quote(symbol="BTCUSDT", bid="999", ask="1001"):
    return dict(symbol=symbol, bidPrice=bid, askPrice=ask, bidQty="2", askQty="3")


def key(row):
    return {k: row[k] for k in ("symbol", "input_close_at", "strategy_id", "strategy_version")}


def publish(store, **changes):
    rows = [record(evaluation_version=protocol.VERSION, horizon=h,
                   due_at=CLOSE+h*HOUR, **changes) for h in (1, 4)]
    assert store.save_strategy_forecasts(rows) == 2
    return rows


def observe(store, row):
    k = key(row)
    assert store.claim_publish_observation(k, row["issued_at"]+1)
    assert store.finish_publish_observation(k, row["issued_at"]+2, data=quote())
    return store.publish_observation(**k)


def target(store, row, *, price=1010., delay=300_000):
    due = row["due_at"]
    candle = dict(time=due-HOUR, close_time=due-1, open=1000., high=2000.,
                  low=500., close=price, volume=10.)
    store.save_candles(row["symbol"], "1h", [candle], confirmed_at=due+delay)
    # Control DB acceptance time, independently of the collection's clock.
    with store.engine.begin() as conn:
        where = (market_candles.c.symbol == row["symbol"], market_candles.c.interval == "1h",
                 market_candles.c.time == due-HOUR)
        conn.execute(update(market_candles).where(*where)
                     .values(observed_at=datetime.fromtimestamp((due+delay)/1000, timezone.utc)))
        return dict(conn.execute(select(market_candles).where(*where)).mappings().one())


def test_midpoint_and_first_snapshot_are_immutable(store):
    row = publish(store)[0]
    saved = observe(store, row)
    assert saved["mid_price"] == "1000"
    assert saved["bid_price"] == "999" and saved["ask_price"] == "1001"
    assert saved["bid_qty"] == "2" and saved["ask_qty"] == "3"
    assert saved["request_started_at"] > row["issued_at"]
    assert not store.claim_publish_observation(key(row), row["issued_at"]+3)
    assert not store.finish_publish_observation(key(row), row["issued_at"]+4,
                                                 data=quote(bid="100", ask="102"))
    assert store.publish_observation(**key(row)) == saved
    assert store.save_strategy_forecasts(publish_records(row)) == 0
    assert store.publish_observation(**key(row)) == saved


def publish_records(row):
    return [{**row, "horizon": h, "due_at": CLOSE+h*HOUR} for h in (1, 4)]


def test_atomic_claim_has_one_owner(store):
    row = publish(store)[0]
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda _: store.claim_publish_observation(key(row), CLOSE+20_000), range(2)))
    assert sorted(outcomes) == [False, True]
    with store.engine.connect() as conn:
        assert len(conn.execute(select(publish_observations)).all()) == 1


@pytest.mark.parametrize("delay,timing", [(0, "OK"), (300_000, "OK"),
                                           (300_001, "LATE_TARGET_DATA")])
@pytest.mark.parametrize("direction", ["UP", "DOWN", "WAIT"])
def test_two_series_exact_target_timing_and_frozen_rules(store, delay, timing, direction):
    rows = publish(store, direction=direction)
    # Different live and reference bases make accidental mixing visible.
    k = key(rows[0])
    assert store.claim_publish_observation(k, CLOSE+20_000)
    store.finish_publish_observation(k, CLOSE+20_001, data=quote(bid="1009", ask="1011"))
    for row in rows:
        candle = target(store, row, delay=delay)
        expected = evaluate_result(
            {**row, "due_at": {row["horizon"]: row["due_at"]},
             "thresholds_pct": {row["horizon"]: row["neutral_pct"]}},
            row["horizon"], candle, now=row["due_at"]+delay, confirmed_at=row["due_at"])
        saved = first(store, horizon=row["horizon"])
        assert store.resolve_strategy_forecast(saved["id"], expected, row["due_at"]+delay)
        saved = first(store, horizon=row["horizon"])
        for name in ("actual_price", "change_pct", "observed_direction", "correct"):
            assert saved[name] == expected[name]
        assert saved["target_observation_id"] == candle["observation_id"]
        assert saved["result_status"] == saved["live_status"] == timing
        assert saved["live_change_pct"] == 0
        assert saved["live_observed_direction"] == "FLAT"
        assert saved["live_correct"] is (None if direction == "WAIT" else False)
        assert saved["due_at"] == CLOSE + row["horizon"]*HOUR


def test_first_target_revision_is_selected_and_completed_result_is_immutable(store):
    row = publish(store)[0]
    original = target(store, row, delay=1000)
    target(store, row, price=1200., delay=600_000)
    saved = first(store, horizon=1)
    assert store.resolve_strategy_forecast(saved["id"], evaluation(actual_price=9999.), row["due_at"]+700_000)
    saved = first(store, horizon=1)
    assert saved["actual_price"] == 1010.
    assert saved["target_observation_id"] == original["observation_id"]
    assert saved["result_status"] == "OK"
    target(store, row, price=1300., delay=800_000)
    assert not store.resolve_strategy_forecast(saved["id"], evaluation(), row["due_at"]+900_000)
    assert first(store, horizon=1) == saved


def test_missing_quote_and_missing_target_are_honest(store):
    row = publish(store)[0]
    saved = first(store, horizon=1)
    assert not store.resolve_strategy_forecast(saved["id"], evaluation(), row["due_at"])
    target(store, row)
    assert store.resolve_strategy_forecast(saved["id"], evaluation(), row["due_at"]+300_000)
    saved = first(store, horizon=1)
    assert saved["live_status"] == "UNAVAILABLE" and saved["live_change_pct"] is None
    assert saved["actual_price"] == 1010.


@pytest.mark.parametrize("failure", [None, "network", "invalid"])
def test_publication_is_committed_before_io_and_duplicate_tick_never_requests_again(live, failure):
    clock, store, market, service = live
    calls = []
    transactions = set()
    event.listen(store.engine, "begin", lambda conn: transactions.add(id(conn)))
    event.listen(store.engine, "commit", lambda conn: transactions.discard(id(conn)))
    event.listen(store.engine, "rollback", lambda conn: transactions.discard(id(conn)))

    async def book(symbol):
        assert not transactions
        assert len(releases(store, symbol)) == 2
        calls.append(symbol)
        if failure == "network":
            raise MarketError("synthetic outage")
        if failure == "invalid":
            return quote(symbol, bid="1002", ask="1000")
        return quote(symbol)

    market.book_ticker = book
    run(service.cycle())
    assert sorted(calls) == ["BTCUSDT", "ETHUSDT"]
    saved = store.publish_observation(**key(releases(store)[0]))
    assert saved["status"] == ("QUOTE_FAILED" if failure else "OK")
    if failure == "network":
        assert "synthetic outage" in saved["error"]
    run(service.cycle())
    restarted = ForecastService(market, store, enable_krypta=True)
    run(restarted.cycle())
    assert len(calls) == 2
    assert store.publish_observation(**key(releases(store)[0])) == saved


def test_settlement_uses_committed_first_target_without_network(live, monkeypatch):
    clock, store, market, service = live
    market.book_ticker = async_quote
    run(service.cycle())
    row = next(r for r in releases(store) if r["horizon"] == 1)
    original = target(store, row, delay=1000)
    target(store, row, price=1200., delay=600_000)
    clock["now"] = row["due_at"]+700_000
    pending = store.pending_strategy_forecasts
    monkeypatch.setattr(store, "pending_strategy_forecasts", lambda now: [
        r for r in pending(now) if r["id"] == row["id"]])

    async def forbidden(*args, **kwargs):
        pytest.fail("Accepted target must not require another Binance request")

    market.klines = forbidden
    run(service.settle_strategies(clock["now"]))
    saved = next(r for r in releases(store) if r["horizon"] == 1)
    assert saved["target_observation_id"] == original["observation_id"]
    assert saved["actual_price"] == 1010.
    assert saved["result_status"] == "OK"


async def async_quote(symbol):
    return quote(symbol)


def test_newly_fetched_target_is_versioned_before_evaluation(live):
    clock, store, market, service = live
    market.book_ticker = async_quote
    run(service.cycle())
    row = next(r for r in releases(store) if r["horizon"] == 1)
    clock["now"] = row["due_at"]+10_000
    run(service.settle_strategies(clock["now"]))
    saved = next(r for r in releases(store) if r["horizon"] == 1)
    actual = store.target_observation(row["symbol"], row["due_at"])
    assert saved["target_observation_id"] == actual["observation_id"]
    assert saved["actual_price"] == actual["close"]


def test_migrate_existing_tracking_state_without_resetting_it(tmp_path):
    instance = Store("sqlite:///"+str(tmp_path/"old_state.db"))
    try:
        with instance.engine.begin() as conn:
            conn.execute(text("""CREATE TABLE market_data_state (
                id INTEGER PRIMARY KEY, last_observation_id BIGINT NOT NULL,
                revision_tracking_started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"""))
            conn.execute(text("INSERT INTO market_data_state VALUES (1, 123, '2026-10-15 00:00:00')"))
        instance.init()
        with instance.engine.connect() as conn:
            state = conn.execute(select(market_data_state)).mappings().one()
            assert state["last_observation_id"] == 123
            assert state["revision_tracking_started_at"] == datetime(2026, 10, 15)
            assert state["evaluation_protocol_started_at"] >= datetime(2026, 10, 16)
    finally:
        instance.close()


def test_activation_marker_is_stable_and_legacy_history_has_no_invented_facts(store):
    store.save_strategy_forecasts([record()])
    with store.engine.connect() as conn:
        started = conn.scalar(select(market_data_state.c.evaluation_protocol_started_at))
        assert conn.execute(select(publish_observations)).first() is None
    store.init()
    with store.engine.connect() as conn:
        assert conn.scalar(select(market_data_state.c.evaluation_protocol_started_at)) == started
    saved = first(store)
    assert saved["target_observation_id"] is None
    assert saved["evaluation_version"] == "hourly-flat-v1"


@pytest.mark.parametrize("changes", [dict(bidPrice="NaN"), dict(askPrice="998"),
                                     dict(bidQty="0"), dict(symbol="ETHUSDT")])
def test_invalid_quote_is_rejected(changes):
    with pytest.raises(ValueError):
        protocol.normalize_quote({**quote(), **changes}, "BTCUSDT")


def test_existing_market_client_book_ticker_is_uncached():
    async def check():
        market = BinanceMarket()
        await market.client.aclose()
        calls = []
        def handler(request):
            calls.append(request)
            assert request.url.path == protocol.ENDPOINT
            assert request.url.params["symbol"] == "BTCUSDT"
            return httpx.Response(200, json=quote())
        market.client = httpx.AsyncClient(base_url="https://data-api.binance.vision",
                                         transport=httpx.MockTransport(handler))
        try:
            for _ in range(2):
                assert await market.book_ticker("BTCUSDT") == quote()
            assert len(calls) == 2
        finally:
            await market.close()
    run(check())

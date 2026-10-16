"""Publication groundwork: deterministic local batches, no external services."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, Table, MetaData, Column
from sqlalchemy.exc import IntegrityError

from app import evaluation_protocol as evaluation
from app import krypta_strategy, publication
from app.service import ForecastService
from app.storage import Store, official_slots, publication_streams, publish_observations, strategy_forecasts
from test_evaluation_protocol import quote, key, target
from test_krypta_live import live, run, releases, set_direction
from test_strategy_storage import store, record, first, CLOSE, HOUR, evaluation as result


CODE = dict(git_commit_sha="a"*40, parameters={"timezone": "UTC"}, working_tree_dirty=False)


def records(symbol="BTCUSDT", direction="UP", slot=CLOSE):
    return [record(symbol=symbol, strategy_id=publication.STRATEGY_ID,
                   strategy_version=publication.STRATEGY_VERSION, direction=direction,
                   input_close_at=slot, issued_at=slot+1000, horizon=h,
                   due_at=slot+h*HOUR, evaluation_version=publication.VERSION)
            for h in (1, 4)]


def seed(store, *, eth=True, direction="UP"):
    store.prepare_publication_slot(CLOSE+1000)
    rows = records(direction=direction) + (records("ETHUSDT", direction) if eth else [])
    store.save_strategy_forecasts(rows)
    return rows


def test_commit_is_not_external_publication_and_post_record_is_diagnostics(store):
    row = seed(store)[0]
    slot = store.publication_slot(CLOSE)
    assert slot["status"] == "PREPARING"
    for name in ("witness_accepted_at", "public_read_confirmed_at", "published_at",
                 "external_object_key", "external_object_version", "witness_id", "witness_sequence"):
        assert slot[name] is None
    observation = store.publish_observation(**key(row))
    assert observation["quote_kind"] == "POST_RECORD_QUOTE"
    assert store.claim_publish_observation(key(row), CLOSE+1001)
    store.finish_publish_observation(key(row), CLOSE+1002, data=quote())
    target(store, row, delay=1000)
    forecast = first(store, horizon=1)
    assert store.resolve_strategy_forecast(forecast["id"], result(), row["due_at"]+1000)
    forecast = first(store, horizon=1)
    assert forecast["actual_price"] == 1010.
    assert forecast["change_pct"] == 1.
    assert forecast["live_status"] == "NOT_PUBLISHED_EXTERNALLY"
    assert forecast["live_change_pct"] is None
    assert forecast["live_correct"] is None
    assert store.publication_slot(CLOSE)["published_at"] is None


def test_old_v1_still_scores_its_original_post_commit_quote(store):
    row = record(evaluation_version=evaluation.VERSION)
    store.save_strategy_forecasts([row])
    assert store.claim_publish_observation(key(row), CLOSE+20_000)
    store.finish_publish_observation(key(row), CLOSE+20_001, data=quote())
    before = first(store)
    store.init()
    assert first(store) == before
    assert store.publication_slot(CLOSE) is None
    assert store.publish_observation(**key(row))["quote_kind"] is None
    target(store, row, delay=1000)
    assert store.resolve_strategy_forecast(before["id"], result(), row["due_at"]+1000)
    assert first(store)["live_status"] == "OK"
    assert first(store)["live_change_pct"] == 1.


def test_migration_preserves_existing_v1_quote_without_relabelling(tmp_path):
    instance = Store("sqlite:///"+str(tmp_path/"old_quote.db"))
    old = Table("strategy_publish_observations", MetaData(), *[
        Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
        for c in publish_observations.c if c.name != "quote_kind"])
    original = dict(**key(record()), bid_price="999", ask_price="1001", mid_price="1000",
                    bid_qty="2", ask_qty="3", request_started_at=CLOSE+20_000,
                    response_received_at=CLOSE+20_001, provider="Binance Spot", endpoint=evaluation.ENDPOINT,
                    status="OK", error=None)
    try:
        old.create(instance.engine)
        with instance.engine.begin() as conn:
            conn.execute(old.insert().values(**original))
        instance.init()
        saved = instance.publish_observation(**key(record()))
        assert saved == {**original, "quote_kind": None}
        assert instance.publication_slot(CLOSE) is None
    finally:
        instance.close()


def test_v1_history_is_not_adopted_or_relabelled_as_v1_1_batch(store):
    old = [record(strategy_id=publication.STRATEGY_ID,
                  evaluation_version=evaluation.VERSION, horizon=h, due_at=CLOSE+h*HOUR)
           for h in (1, 4)]
    store.save_strategy_forecasts(old)
    before = store.strategy_history("BTCUSDT")["items"]
    store.prepare_publication_slot(CLOSE+1000)
    frozen = store.freeze_publication_slot(CLOSE, CLOSE+2000, CODE)
    assert all(e["forecast_ids"] == [] for e in json.loads(frozen["batch_bytes"])["entries"])
    assert store.strategy_history("BTCUSDT")["items"] == before


def test_one_slot_and_one_official_stream_per_project(store):
    with ThreadPoolExecutor(2) as pool:
        slots = list(pool.map(store.prepare_publication_slot, [CLOSE+1000, CLOSE+1001]))
    assert slots[0]["slot_utc"] == slots[1]["slot_utc"] == CLOSE
    with store.engine.connect() as conn:
        assert len(conn.execute(select(official_slots)).all()) == 1
        original = dict(conn.execute(select(publication_streams)).mappings().one())
    with pytest.raises(IntegrityError), store.engine.begin() as conn:
        conn.execute(publication_streams.insert().values(
            **{**original, "official_stream_id": "another-version", "strategy_version": "v2"}))
    with store.engine.begin() as conn:
        conn.execute(publication_streams.insert().values(
            **{**original, "official_stream_id": "experiment", "strategy_version": "v2",
               "stream_status": "EXPERIMENTAL"}))


@pytest.mark.parametrize("change", [dict(strategy_version="v2"),
                                    dict(strategy_id="other_strategy"), dict(symbol="SOLUSDT")])
def test_other_strategy_identity_cannot_enter_official_stream(store, change):
    store.prepare_publication_slot(CLOSE+1000)
    with pytest.raises(ValueError, match="official stream"):
        store.save_strategy_forecasts([{**r, **change} for r in records()])
    with store.engine.connect() as conn:
        assert conn.execute(select(strategy_forecasts)).first() is None


def test_frozen_bytes_and_hash_survive_changed_retries_and_settlement(store):
    rows = seed(store)
    frozen = store.freeze_publication_slot(CLOSE, CLOSE+2000, CODE)
    raw = frozen["batch_bytes"]
    assert hashlib.sha256(raw).hexdigest() == frozen["batch_hash"]
    assert frozen["batch_id"] == "krypta-batch-"+frozen["batch_hash"]
    payload = json.loads(raw)
    assert [e["symbol"] for e in payload["entries"]] == list(publication.SYMBOLS)
    assert payload["entries"][0]["forecasts"][0]["base_price"] == "1000"
    assert payload["entries"][0]["forecasts"][0]["issued_at"] == CLOSE+1000
    assert payload["code_config_identity"] == CODE
    assert "actual_price" not in payload["entries"][0]["forecasts"][0]
    assert store.save_strategy_forecasts(rows) == 0
    with pytest.raises(ValueError, match="closed"):
        store.save_strategy_forecasts([{**rows[0], "direction": "DOWN"}])
    target(store, rows[0], delay=1000)
    forecast = first(store, horizon=1)
    store.resolve_strategy_forecast(forecast["id"], result(), rows[0]["due_at"]+1000)
    assert store.freeze_publication_slot(CLOSE, CLOSE+HOUR, {"changed": True}) == frozen
    assert store.publication_slot(CLOSE)["batch_bytes"] == raw


def test_canonical_serialization_is_fixed_and_ignores_mapping_order():
    assert publication.canonical_bytes({"z": -0.0, "a": [1, True, None, .125]}) == (
        b'{"a":[1,true,null,"0.125"],"z":"0"}')
    assert publication.canonical_bytes({"b": "кириллица", "a": 2}) == publication.canonical_bytes(
        {"a": 2, "b": "кириллица"})
    for invalid in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            publication.canonical_bytes({"invalid": invalid})


def test_partial_batch_freezes_explicit_eth_error_and_cannot_append_it(store):
    seed(store, eth=False, direction="WAIT")
    frozen = store.freeze_publication_slot(CLOSE, CLOSE+2000, CODE, {
        "ETHUSDT": dict(status="DATA_UNAVAILABLE", reason="INSUFFICIENT_HISTORY")})
    btc, eth = json.loads(frozen["batch_bytes"])["entries"]
    assert btc["signal"] == "WAIT" and btc["status"] == "OK"
    assert eth["signal"] is None and eth["status"] == "DATA_UNAVAILABLE"
    assert eth["forecast_ids"] == [] and eth["forecasts"] == []
    assert eth["reason"] == "INSUFFICIENT_HISTORY"
    with pytest.raises(ValueError, match="closed"):
        store.save_strategy_forecasts(records("ETHUSDT"))
    assert store.freeze_publication_slot(CLOSE, CLOSE+3000, CODE) == frozen


@pytest.mark.parametrize("failure,status", [("DATA_UNAVAILABLE", "DATA_UNAVAILABLE"),
                                            ("ERROR", "ERROR")])
def test_failed_slot_still_has_exact_two_technical_entries(store, failure, status):
    store.prepare_publication_slot(CLOSE+1000)
    frozen = store.freeze_publication_slot(CLOSE, CLOSE+2000, CODE,
        {s: dict(status=failure, reason="synthetic failure") for s in publication.SYMBOLS})
    assert frozen["status"] == status
    assert len(json.loads(frozen["batch_bytes"])["entries"]) == 2
    assert frozen["publication_status"] == "BATCH_FROZEN"
    assert frozen["published_at"] is None


def test_late_slot_has_no_fake_forecasts(store):
    store.prepare_publication_slot(CLOSE+300_000)
    frozen = store.freeze_publication_slot(CLOSE, CLOSE+300_000, CODE)
    assert frozen["status"] == "LATE"
    assert all(e["forecast_ids"] == [] for e in json.loads(frozen["batch_bytes"])["entries"])


def test_missing_hours_are_permanent_without_invented_reason_or_forecasts(store):
    seed(store)
    store.freeze_publication_slot(CLOSE, CLOSE+2000, CODE)
    store.prepare_publication_slot(CLOSE+3*HOUR+1000)
    for missed in (CLOSE+HOUR, CLOSE+2*HOUR):
        slot = store.publication_slot(missed)
        assert slot["status"] == "MISSING_UNEXPLAINED"
        assert slot["error"] is None and slot["batch_bytes"] is None
        assert slot["published_at"] is None
        # A stale manual tick cannot repurpose the historical missing slot.
        assert store.prepare_publication_slot(missed+1000) == slot
        assert store.freeze_publication_slot(missed, missed+2000, CODE) == slot
        with pytest.raises(ValueError, match="closed"):
            store.save_strategy_forecasts(records(slot=missed))
    assert store.publication_slot(CLOSE-HOUR) is None
    with store.engine.connect() as conn:
        assert len(conn.execute(select(strategy_forecasts)).all()) == 4


def test_interrupted_preparation_is_visible_without_inventing_crash_cause(store):
    store.prepare_publication_slot(CLOSE+1000)
    store.prepare_publication_slot(CLOSE+HOUR+1000)
    slot = store.publication_slot(CLOSE)
    assert slot["status"] == "ERROR"
    assert slot["error"] == "PREPARATION_NOT_FROZEN_BY_DEADLINE"
    assert slot["batch_bytes"] is None


def test_concurrent_freezers_return_one_batch(store):
    seed(store)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda i: store.freeze_publication_slot(
            CLOSE, CLOSE+2000+i, {**CODE, "worker": i}), range(2)))
    assert results[0] == results[1]


def test_concurrent_eth_record_and_freeze_cannot_diverge(store):
    seed(store, eth=False)
    barrier = Barrier(2)

    def append_eth():
        barrier.wait(timeout=10)
        try:
            return store.save_strategy_forecasts(records("ETHUSDT"))
        except ValueError as exc:
            assert "closed" in str(exc)
            return 0

    def freeze():
        barrier.wait(timeout=10)
        return store.freeze_publication_slot(CLOSE, CLOSE+2000, CODE)

    with ThreadPoolExecutor(2) as pool:
        writer = pool.submit(append_eth)
        freezer = pool.submit(freeze)
        inserted, frozen = writer.result(), freezer.result()
    eth = json.loads(frozen["batch_bytes"])["entries"][1]
    assert eth["status"] == ("OK" if inserted else "DATA_UNAVAILABLE")
    assert len(eth["forecast_ids"]) == inserted


def test_cycle_reuses_frozen_partial_batch_without_calculating_missing_symbol(live, monkeypatch):
    clock, store, market, service = live
    market.fail.add(("ETHUSDT", "1h"))
    set_direction(market, "WAIT")
    run(service.cycle())
    frozen = store.publication_slot(clock["now"]//HOUR*HOUR)
    entries = json.loads(frozen["batch_bytes"])["entries"]
    assert entries[0]["status"] == "OK" and entries[0]["signal"] == "WAIT"
    assert entries[1]["status"] == "DATA_UNAVAILABLE"
    assert releases(store, "ETHUSDT") == []
    market.fail.clear()

    def forbidden(*args, **kwargs):
        pytest.fail("Frozen batch must not recalculate Strategy")

    monkeypatch.setattr(krypta_strategy, "analyze", forbidden)
    restarted = ForecastService(market, store, enable_krypta=True)
    run(restarted.cycle())
    assert store.publication_slot(frozen["slot_utc"]) == frozen
    assert releases(store, "ETHUSDT") == []


def test_v1_1_protocol_identity_and_future_timestamps_remain_null_after_cycle(live):
    clock, store, market, service = live
    run(service.cycle())
    assert all(r["evaluation_version"] == publication.VERSION for r in releases(store))
    frozen = store.publication_slot(clock["now"]//HOUR*HOUR)
    assert frozen["status"] == frozen["publication_status"] == "BATCH_FROZEN"
    assert frozen["published_at"] is None and frozen["witness_accepted_at"] is None
    btc = json.loads(frozen["batch_bytes"])["entries"][0]
    assert btc["forecast_ids"] == sorted(r["id"] for r in releases(store))
    assert all(r["recorded_at"] is not None for r in btc["forecasts"])

"""External publication orchestration uses local mocks and disposable SQLite."""
import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update, MetaData, Table, Column

from app import publication, krypta_strategy
from app.mock_publication import MockArchive, MockWitness, MockPublicReader
from app.publication_adapters import PublicationWorkflow, publication_mode, ArchiveReceipt
from app.service import ForecastService
from app.storage import official_slots, publication_streams, Store, EXTERNAL_SLOT_TYPES, PUBLICATION_LEASE_MS
from test_evaluation_protocol import quote, key, target
from test_krypta_live import live, run, releases
from test_publication import seed, CODE
from test_strategy_storage import store, first, evaluation, CLOSE, HOUR


@pytest.fixture
def rig(store):
    seed(store)
    frozen = store.freeze_publication_slot(CLOSE, CLOSE+2000, CODE)
    clock = {"now": CLOSE+3000}
    now = lambda: clock["now"]
    archive, witness = MockArchive(now), MockWitness(now)
    reader = MockPublicReader(archive, now)
    workflow = PublicationWorkflow(store, archive, witness, reader, now)
    return SimpleNamespace(store=store, frozen=frozen, archive=archive, witness=witness,
                           reader=reader, workflow=workflow, clock=clock)


def unchanged(rig):
    row = rig.store.publication_slot(CLOSE)
    for field in ("batch_id", "batch_bytes", "batch_hash"):
        assert row[field] == rig.frozen[field]
    return row


def test_exact_bytes_success_receipts_and_already_published_idempotency(rig, monkeypatch):
    # Any reconstruction of canonical JSON/strategy would make this test fail.
    def forbidden(*args, **kwargs):
        pytest.fail("Frozen publication must not reconstruct its payload")
    monkeypatch.setattr(publication, "canonical_bytes", forbidden)
    monkeypatch.setattr(krypta_strategy, "analyze", forbidden)
    row = run(rig.workflow.run(CLOSE))
    assert row["publication_status"] == row["status"] == "PUBLISHED"
    assert row["publication_mode"] == "mock"
    assert row["public_read_confirmed_at"] <= row["published_at"]
    assert row["archive_receipt"]["stored_hash"] == row["batch_hash"]
    assert row["witness_receipt"]["submitted_hash"] == row["batch_hash"]
    assert row["public_read_receipt"]["read_hash"] == row["batch_hash"]
    assert rig.archive.calls[0][2] == rig.frozen["batch_bytes"]
    assert rig.archive.calls[0][3] == rig.frozen["batch_hash"]
    calls = (list(rig.archive.calls), list(rig.witness.calls), list(rig.reader.calls))
    assert run(rig.workflow.run(CLOSE)) == row
    assert calls == (rig.archive.calls, rig.witness.calls, rig.reader.calls)
    unchanged(rig)


def test_hash_mismatch_blocks_all_provider_calls(rig):
    with rig.store.engine.begin() as conn:
        conn.execute(update(official_slots).values(batch_bytes=rig.frozen["batch_bytes"]+b" "))
    row = run(rig.workflow.run(CLOSE))
    assert row["status"] == "ERROR" and row["external_step_status"] == "HASH_MISMATCH"
    assert row["published_at"] is None
    assert not rig.archive.calls and not rig.witness.calls and not rig.reader.calls
    assert not rig.store.claim_post_publication_quote(CLOSE, "BTCUSDT", rig.clock["now"])


@pytest.mark.parametrize("provider", ["archive", "witness", "reader"])
def test_definitive_failure_retries_same_frozen_batch_and_preserves_completed_steps(rig, provider):
    adapter = getattr(rig, provider)
    adapter.behavior = "failure"
    row = run(rig.workflow.run(CLOSE))
    assert row["published_at"] is None and row["external_step_status"] == "FAILED"
    assert row["publication_claim_token"] is None
    unchanged(rig)
    archive_count = len(rig.archive.calls)
    witness_count = len(rig.witness.calls)
    adapter.behavior = "success"
    row = run(rig.workflow.run(CLOSE))
    assert row["publication_status"] == "PUBLISHED"
    if provider != "archive":
        assert len(rig.archive.calls) == archive_count
    if provider == "reader":
        assert len(rig.witness.calls) == witness_count
    unchanged(rig)


@pytest.mark.parametrize("provider", ["archive", "witness"])
@pytest.mark.parametrize("behavior", ["timeout", "timeout_after_accept", "delayed"])
def test_unknown_reconciles_before_register_and_never_changes_batch(rig, provider, behavior):
    adapter = getattr(rig, provider)
    adapter.behavior = behavior
    row = run(rig.workflow.run(CLOSE))
    assert row["external_step_status"] == "UNKNOWN" and row["published_at"] is None
    unchanged(rig)
    initial_calls = len(adapter.calls)
    adapter.behavior = "success"
    if behavior == "delayed":
        row = run(rig.workflow.run(CLOSE))
        assert row["external_step_status"] == "UNKNOWN"
        assert len(adapter.calls) == initial_calls+1
        assert adapter.calls[-1][0] == "check"
        adapter.accept_pending()
    before = len(adapter.calls)
    row = run(rig.workflow.run(CLOSE))
    assert row["publication_status"] == "PUBLISHED"
    assert adapter.calls[before][0] == "check"
    if behavior != "timeout":
        assert len(adapter.calls) == before+1  # No duplicate submission after remote acceptance.
    assert len(rig.archive.objects) == 1
    records = list(rig.witness.records.values())[0]
    assert len(records) == 1 and records[0].sequence == 1
    unchanged(rig)


def test_public_read_must_finish_before_published_at_and_io_has_no_db_transaction(rig):
    transactions = set()
    event.listen(rig.store.engine, "begin", lambda conn: transactions.add(id(conn)))
    event.listen(rig.store.engine, "commit", lambda conn: transactions.discard(id(conn)))
    event.listen(rig.store.engine, "rollback", lambda conn: transactions.discard(id(conn)))
    def guarded(original):
        async def call(*args):
            assert not transactions
            return await original(*args)
        return call
    for adapter, methods in ((rig.archive, ("store_once", "check")), (rig.witness, ("register", "check"))):
        for method in methods:
            setattr(adapter, method, guarded(getattr(adapter, method)))
    original = rig.reader.read

    async def read(reference):
        assert not transactions
        row = rig.store.publication_slot(CLOSE)
        assert row["publication_status"] == "WITNESS_ACCEPTED"
        assert row["witness_accepted_at"] is not None
        assert row["published_at"] is None and row["public_read_confirmed_at"] is None
        rig.clock["now"] += 50
        return await original(reference)

    rig.reader.read = read
    row = run(rig.workflow.run(CLOSE))
    assert row["published_at"] == row["public_read_confirmed_at"] == rig.clock["now"]


def test_corrupt_public_bytes_never_publish(rig):
    rig.reader.behavior = "conflict"
    row = run(rig.workflow.run(CLOSE))
    assert row["external_step_status"] == "HASH_MISMATCH"
    assert row["published_at"] is None and row["public_read_confirmed_at"] is None
    unchanged(rig)


def test_conflicting_witness_hash_is_visible_and_has_later_sequence(rig):
    first_receipt = run(rig.witness.register(publication.PROJECT_ID, publication.STREAM_ID,
                                           CLOSE, "0"*64, "mock://other"))
    row = run(rig.workflow.run(CLOSE))
    conflict = row["witness_receipt"]
    assert conflict["status"] == "CONFLICT" and conflict["sequence"] > first_receipt.sequence
    assert conflict["first_hash"] == "0"*64 and conflict["first_sequence"] == first_receipt.sequence
    assert row["external_step_status"] == "CONFLICT" and row["published_at"] is None
    assert row["witness_accepted_at"] is None
    calls = len(rig.witness.calls)
    run(rig.workflow.run(CLOSE))
    assert len(rig.witness.calls) == calls
    unchanged(rig)


def test_mock_duplicate_and_delayed_conflict_keep_first_record():
    witness = MockWitness()
    args = ("project", "stream", CLOSE)
    a = run(witness.register(*args, "a"*64, "archive-a"))
    assert run(witness.register(*args, "a"*64, "archive-a")) == a
    b = run(witness.register(*args, "b"*64, "archive-b"))
    assert b.status == "CONFLICT" and b.sequence > a.sequence
    witness.behavior = "delayed"
    later = ("project", "stream", CLOSE+HOUR)
    run(witness.register(*later, "a"*64, "archive-a"))
    run(witness.register(*later, "b"*64, "archive-b"))
    witness.accept_pending()
    receipts = witness.records[later]
    assert [r.status for r in receipts] == ["ACCEPTED", "CONFLICT"]
    assert receipts[0].sequence < receipts[1].sequence


def test_abandoned_attempt_uses_reconciliation_after_lease_expiry(rig):
    old = rig.store.claim_external_publication(CLOSE, "mock", rig.clock["now"])
    rig.store.external_publication_progress(CLOSE, old["publication_claim_token"], rig.clock["now"],
        publication_status="EXTERNAL_SUBMISSION", external_step="ARCHIVE", external_step_status="IN_PROGRESS")
    run(rig.archive.store_once(old["batch_id"], old["batch_bytes"], old["batch_hash"], CLOSE, publication.STREAM_ID))
    # Simulate loss of the process after acceptance but before saving its receipt.
    rig.clock["now"] += PUBLICATION_LEASE_MS+1
    row = run(rig.workflow.run(CLOSE))
    assert row["publication_status"] == "PUBLISHED"
    assert [c[0] for c in rig.archive.calls] == ["store_once", "check"]
    unchanged(rig)


def test_active_claim_prevents_concurrent_delivery(rig):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        original = rig.archive.store_once
        async def held(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        rig.archive.store_once = held
        first_task = asyncio.create_task(rig.workflow.run(CLOSE))
        await entered.wait()
        other = await rig.workflow.run(CLOSE)
        assert other["published_at"] is None
        release.set()
        return await first_task
    assert run(check())["publication_status"] == "PUBLISHED"
    assert len(rig.archive.calls) == len(rig.witness.calls) == 1


def test_disabled_and_unimplemented_external_configuration(rig, monkeypatch):
    disabled = PublicationWorkflow(rig.store, rig.archive, rig.witness, rig.reader,
                                  lambda: rig.clock["now"], mode="disabled")
    assert run(disabled.run(CLOSE))["publication_status"] == "BATCH_FROZEN"
    assert not rig.archive.calls and rig.store.publication_slot(CLOSE)["published_at"] is None
    monkeypatch.delenv("EXTERNAL_PUBLICATION_MODE", raising=False)
    assert ForecastService(None, rig.store, enable_krypta=True).external_publication_mode == "disabled"
    with pytest.raises(ValueError, match="not implemented"):
        publication_mode("external")
    with pytest.raises(ValueError):
        publication_mode("typo")


def test_mock_environment_configuration_uses_only_local_providers(live, monkeypatch):
    clock, store, market, _ = live
    monkeypatch.setenv("EXTERNAL_PUBLICATION_MODE", "mock")
    async def book(symbol):
        return quote(symbol)
    market.book_ticker = book
    service = ForecastService(market, store, enable_krypta=True)
    run(service.cycle())
    row = store.publication_slot(clock["now"]//HOUR*HOUR)
    assert row["publication_mode"] == "mock" and row["publication_status"] == "PUBLISHED"
    assert row["archive_receipt"]["provider"] == "mock-archive"
    assert row["witness_receipt"]["provider"] == "mock-witness"


def test_mock_tick_post_publish_quote_order_and_midpoint_separation(live, monkeypatch):
    clock, store, market, _ = live
    now = lambda: clock["now"]
    archive, witness = MockArchive(now), MockWitness(now)
    reader = MockPublicReader(archive, now)
    calls = []
    async def book(symbol):
        slot = store.publication_slot(clock["now"]//HOUR*HOUR)
        published = slot["published_at"] is not None
        calls.append((symbol, published))
        if published:
            assert slot["publication_status"] == "PUBLISHED"
            assert slot["public_read_confirmed_at"] <= slot["published_at"]
            return quote(symbol, bid="999", ask="1001")
        return quote(symbol, bid="4999", ask="5001")
    market.book_ticker = book
    service = ForecastService(market, store, enable_krypta=True,
        external_publication_mode="mock", publication_adapters=(archive, witness, reader))
    run(service.cycle())
    slot = clock["now"]//HOUR*HOUR
    journal = store.publication_slot(slot)
    assert calls == [("BTCUSDT", False), ("ETHUSDT", False), ("BTCUSDT", True), ("ETHUSDT", True)]
    row = next(r for r in releases(store) if r["horizon"] == 1)
    post = journal["post_publish_quotes"]["BTCUSDT"]
    assert post["request_started_at"] >= journal["published_at"]
    assert post["mid_price"] == "1000" and post["quote_kind"] == "LIVE_AFTER_PUBLISH"
    assert store.publish_observation(**key(row))["mid_price"] == "5000"
    target(store, row, delay=1000)
    assert store.resolve_strategy_forecast(row["id"], evaluation(), row["due_at"]+1000)
    saved = next(r for r in releases(store) if r["horizon"] == 1)
    assert saved["live_change_pct"] == 1. and saved["live_evidence_mode"] == "mock"
    def forbidden(*args, **kwargs):
        pytest.fail("Duplicate tick cannot recompute Strategy")
    monkeypatch.setattr(krypta_strategy, "analyze", forbidden)
    restarted = ForecastService(market, store, enable_krypta=True,
        external_publication_mode="mock", publication_adapters=(archive, witness, reader))
    run(restarted.cycle())
    assert len(calls) == 4 and len(archive.objects) == 1 and witness.sequence == 1
    assert store.publication_slot(slot) == journal


def test_post_publication_quote_cannot_start_early_or_replace_first(rig):
    assert not rig.store.claim_post_publication_quote(CLOSE, "BTCUSDT", rig.clock["now"])
    row = run(rig.workflow.run(CLOSE))
    assert not rig.store.claim_post_publication_quote(CLOSE, "BTCUSDT", row["published_at"]-1)
    assert rig.store.claim_post_publication_quote(CLOSE, "BTCUSDT", row["published_at"])
    assert rig.store.finish_post_publication_quote(CLOSE, "BTCUSDT", rig.clock["now"], data=quote())
    assert not rig.store.claim_post_publication_quote(CLOSE, "BTCUSDT", rig.clock["now"]+1)
    assert not rig.store.finish_post_publication_quote(CLOSE, "BTCUSDT", rig.clock["now"]+2,
                                                       data=quote(bid="100", ask="102"))
    assert rig.store.publication_slot(CLOSE)["post_publish_quotes"]["BTCUSDT"]["mid_price"] == "1000"


@pytest.mark.parametrize("adapter", ["archive", "witness"])
def test_wrong_receipt_hash_cannot_publish(rig, adapter):
    provider = getattr(rig, adapter)
    name = "store_once" if adapter == "archive" else "register"
    original = getattr(provider, name)
    async def forged(*args):
        receipt = await original(*args)
        return replace(receipt, **{"stored_hash" if adapter == "archive" else "submitted_hash": "f"*64})
    setattr(provider, name, forged)
    row = run(rig.workflow.run(CLOSE))
    assert row["external_step_status"] == "HASH_MISMATCH" and row["published_at"] is None
    assert not rig.reader.calls
    unchanged(rig)


def test_unknown_reconciliation_failure_never_blindly_resubmits(rig):
    rig.archive.behavior = "timeout_after_accept"
    run(rig.workflow.run(CLOSE))
    original = rig.archive.check
    async def unavailable(*args):
        return ArchiveReceipt("mock-archive", status="ERROR")
    rig.archive.check = unavailable
    for _ in range(2):
        row = run(rig.workflow.run(CLOSE))
        assert row["external_step_status"] == "UNKNOWN"
        assert len(rig.archive.calls) == 1
    rig.archive.check = original
    assert run(rig.workflow.run(CLOSE))["publication_status"] == "PUBLISHED"


def test_resume_after_read_confirmation_does_not_repeat_io(rig, monkeypatch):
    original = rig.store.complete_external_publication
    def interrupted(*args):
        raise TimeoutError()
    monkeypatch.setattr(rig.store, "complete_external_publication", interrupted)
    row = run(rig.workflow.run(CLOSE))
    assert row["publication_status"] == "PUBLIC_READ_CONFIRMED" and row["published_at"] is None
    calls = (len(rig.archive.calls), len(rig.witness.calls), len(rig.reader.calls))
    monkeypatch.setattr(rig.store, "complete_external_publication", original)
    rig.clock["now"] += 100
    row = run(rig.workflow.run(CLOSE))
    assert row["published_at"] > row["public_read_confirmed_at"]
    assert calls == (len(rig.archive.calls), len(rig.witness.calls), len(rig.reader.calls))


def test_published_without_post_publish_quote_cannot_use_post_record_midpoint(rig):
    row = first(rig.store, horizon=1)
    assert rig.store.claim_publish_observation(key(row), CLOSE+2000)
    rig.store.finish_publish_observation(key(row), CLOSE+2001, data=quote())
    run(rig.workflow.run(CLOSE))
    target(rig.store, row, delay=1000)
    assert rig.store.resolve_strategy_forecast(row["id"], evaluation(), row["due_at"]+1000)
    saved = first(rig.store, horizon=1)
    assert saved["live_status"] == "UNAVAILABLE" and saved["live_change_pct"] is None
    assert saved["actual_price"] == 1010.


def test_post_publish_quote_failure_preserves_published_batch(rig):
    row = run(rig.workflow.run(CLOSE))
    assert rig.store.claim_post_publication_quote(CLOSE, "BTCUSDT", row["published_at"])
    rig.store.finish_post_publication_quote(CLOSE, "BTCUSDT", row["published_at"], error="synthetic outage")
    saved = unchanged(rig)
    assert saved["published_at"] == row["published_at"]
    assert saved["post_publish_quotes"]["BTCUSDT"]["status"] == "QUOTE_FAILED"
    assert not rig.store.claim_post_publication_quote(CLOSE, "BTCUSDT", row["published_at"]+10)


def test_late_publication_cannot_score_backwards_against_an_earlier_target(rig):
    forecast = first(rig.store, horizon=1)
    rig.clock["now"] = forecast["due_at"]+1000
    row = run(rig.workflow.run(CLOSE))
    assert rig.store.claim_post_publication_quote(CLOSE, "BTCUSDT", row["published_at"])
    rig.store.finish_post_publication_quote(CLOSE, "BTCUSDT", row["published_at"], data=quote())
    target(rig.store, forecast, delay=2000)
    assert rig.store.resolve_strategy_forecast(forecast["id"], evaluation(), forecast["due_at"]+2000)
    saved = first(rig.store, horizon=1)
    assert saved["live_status"] == "UNAVAILABLE" and saved["live_change_pct"] is None
    assert saved["change_pct"] == 1. and saved["actual_price"] == 1010.


def test_progress_cannot_replace_frozen_bytes_or_invent_published_time(rig):
    row = rig.store.claim_external_publication(CLOSE, "mock", rig.clock["now"])
    token = row["publication_claim_token"]
    for fields in ({"batch_bytes": b"new"}, {"batch_hash": "f"*64}, {"published_at": rig.clock["now"]}):
        with pytest.raises(ValueError):
            rig.store.external_publication_progress(CLOSE, token, rig.clock["now"], **fields)
    assert not rig.store.complete_external_publication(CLOSE, token, rig.clock["now"])
    rig.store.release_external_publication(CLOSE, token)
    unchanged(rig)


def test_migration_preserves_old_frozen_bytes_and_leaves_receipts_empty(rig, tmp_path):
    instance = Store("sqlite:///"+str(tmp_path/"old_publication.db"))
    old = Table("official_slots", MetaData(), *[
        Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
        for c in official_slots.c if c.name not in EXTERNAL_SLOT_TYPES])
    try:
        publication_streams.create(instance.engine)
        old.create(instance.engine)
        with rig.store.engine.connect() as conn:
            stream = dict(conn.execute(select(publication_streams)).mappings().one())
        with instance.engine.begin() as conn:
            conn.execute(publication_streams.insert().values(**stream))
            conn.execute(old.insert().values(**{k: v for k, v in rig.frozen.items() if k not in EXTERNAL_SLOT_TYPES}))
        instance.init()
        instance.init()
        saved = instance.publication_slot(CLOSE)
        assert saved["batch_bytes"] == rig.frozen["batch_bytes"]
        assert saved["batch_hash"] == hashlib.sha256(saved["batch_bytes"]).hexdigest()
        assert saved["published_at"] is None
        assert all(saved[k] is None for k in EXTERNAL_SLOT_TYPES)
    finally:
        instance.close()

"""Strategy storage uses disposable SQLite files, never the configured Neon DB."""
import copy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from app.storage import Store, strategy_forecasts
from app.strategies import analyze_strategies, evaluate_result
from test_storage import record as legacy_record


HOUR = 3_600_000
CLOSE = 500 * HOUR


def record(**changes):
    values = dict(
        symbol="BTCUSDT", strategy_id="trend_breakout", strategy_version="v1",
        input_close_at=CLOSE, horizon=1, issued_at=CLOSE + 10_000,
        base_price=1000., due_at=CLOSE + HOUR, status="OK", direction="UP",
        score=.5, reason_codes=["TREND_UP", "BREAKOUT_UP"],
        indicators={"ema20": 1002., "ema50": 1000., "atr14": 8.},
        evaluation_version="hourly-flat-v1", neutral_pct=.2,
    )
    values.update(changes)
    return values


@pytest.fixture
def store(tmp_path):
    instance = Store("sqlite:///" + str(tmp_path / "strategies.db"))
    instance.init()
    try:
        yield instance
    finally:
        instance.close()


def first(store, **filters):
    return store.strategy_history("BTCUSDT", **filters)["items"][0]


def evaluation(**changes):
    values = dict(status="OK", actual_price=1010., change_pct=1.,
                  observed_direction="UP", correct=True)
    values.update(changes)
    return values


def test_init_adds_table_to_existing_database_without_changing_old_forecasts(tmp_path):
    url = "sqlite:///" + str(tmp_path / "existing.db")
    old = Store(url)
    # Simulate an existing deployment whose schema has only the legacy table.
    from app.storage import forecasts
    forecasts.create(old.engine)
    old.save(legacy_record())
    before = old.latest("BTCUSDT")
    old.init()
    old.init()
    assert set(inspect(old.engine).get_table_names()) == {
        "forecasts", "market_quotes", "market_candles", "strategy_forecasts",
        "market_candle_revisions", "market_data_state",
        "strategy_publish_observations",
        "publication_streams", "official_slots",
    }
    assert old.latest("BTCUSDT") == before
    old.close()


def test_exact_retry_is_idempotent_json_roundtrips_and_input_is_not_mutated(store):
    values = record(reason_codes=["ПРОБОЙ"], indicators={"b": 2, "a": {"price": 10.}})
    original = copy.deepcopy(values)
    assert store.save_strategy_forecasts([values]) == 1
    assert store.save_strategy_forecasts([values]) == 0
    # Object key ordering is immaterial to an exact retry.
    assert store.save_strategy_forecasts([{**values, "indicators": {"a": {"price": 10.}, "b": 2}}]) == 0
    saved = first(store)
    for key, value in values.items():
        assert saved[key] == value
    assert saved["correct"] is None and saved["evaluated_at"] is None
    assert values == original
    assert store.save_strategy_forecasts([]) == 0


@pytest.mark.parametrize("change", [
    {"direction": "DOWN"}, {"score": -.5}, {"base_price": 999.},
    {"issued_at": CLOSE + 20_000}, {"neutral_pct": .3},
    {"reason_codes": ["OTHER_REASON"]}, {"indicators": {"atr14": 9.}},
    {"evaluation_version": "hourly-flat-v2"},
])
def test_changed_retry_raises_and_never_overwrites_published_decision(store, change):
    store.save_strategy_forecasts([record()])
    before = first(store)
    with pytest.raises(ValueError, match="cannot be overwritten"):
        store.save_strategy_forecasts([record(**change)])
    assert first(store) == before


def test_batch_rolls_back_on_conflict_or_invalid_record(store):
    store.save_strategy_forecasts([record()])
    with pytest.raises(ValueError):
        store.save_strategy_forecasts([
            record(strategy_id="trend_momentum"), record(direction="DOWN")
        ])
    assert store.strategy_history("BTCUSDT")["total"] == 1
    with pytest.raises(IntegrityError):
        store.save_strategy_forecasts([
            record(strategy_id="consensus"), record(strategy_id="C", horizon=30)
        ])
    assert store.strategy_history("BTCUSDT")["total"] == 1


def test_batch_requires_identical_hourly_context(store):
    with pytest.raises(ValueError, match="share issue time"):
        store.save_strategy_forecasts([record(), record(strategy_id="trend_momentum", base_price=999.)])
    assert store.strategy_history("BTCUSDT")["total"] == 0


def test_concurrent_identical_inserts_produce_one_record(store):
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda _: store.save_strategy_forecasts([record()]), range(8)))
    assert sum(counts) == 1
    assert store.strategy_history("BTCUSDT")["total"] == 1


def test_concurrent_different_decisions_cannot_replace_the_winner(store):
    def save(direction):
        try:
            return store.save_strategy_forecasts([record(direction=direction)])
        except ValueError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ("UP", "DOWN")))
    assert sorted(results, key=str) == [1, "conflict"]
    assert store.strategy_history("BTCUSDT")["total"] == 1


def test_all_unique_key_dimensions_and_history_filters(store):
    rows = [record(), record(strategy_id="trend_momentum"), record(strategy_id="consensus"),
            record(strategy_version="v2"), record(horizon=4, due_at=CLOSE+4*HOUR),
            record(symbol="ETHUSDT"),
            record(input_close_at=CLOSE+HOUR, issued_at=CLOSE+HOUR+10_000, due_at=CLOSE+2*HOUR)]
    assert store.save_strategy_forecasts(rows) == len(rows)
    for row in rows:
        assert store.has_strategy_forecast(*[row[k] for k in (
            "symbol", "input_close_at", "strategy_id", "strategy_version", "horizon"
        )])
    assert not store.has_strategy_forecast("BTCUSDT", CLOSE, "missing", "v1", 1)
    assert store.strategy_history("BTCUSDT")["total"] == 6
    assert store.strategy_history("ETHUSDT")["total"] == 1
    assert store.strategy_history("BTCUSDT", strategy_id="consensus")["total"] == 1
    assert store.strategy_history("BTCUSDT", strategy_version="v2")["total"] == 1
    assert store.strategy_history("BTCUSDT", horizon=4)["total"] == 1
    page = store.strategy_history("BTCUSDT", offset=1, limit=2)
    assert page["total"] == 6 and len(page["items"]) == 2
    assert first(store)["input_close_at"] == CLOSE + HOUR


@pytest.mark.parametrize("delay,accepted", [(0, True), (300_000, True), (-1, False), (300_001, False)])
def test_live_five_minute_window_is_enforced_at_database_level(store, delay, accepted):
    # Direct inserts prove the database guard is not merely Python validation.
    values = record(issued_at=CLOSE+delay, reason_codes="[]", indicators="{}")
    if accepted:
        with store.engine.begin() as conn:
            conn.execute(strategy_forecasts.insert().values(**values))
    else:
        with pytest.raises(IntegrityError), store.engine.begin() as conn:
            conn.execute(strategy_forecasts.insert().values(**values))


@pytest.mark.parametrize("change", [
    {"horizon": 30}, {"due_at": CLOSE+HOUR-1},
    {"input_close_at": CLOSE+1, "issued_at": CLOSE+10_001, "due_at": CLOSE+HOUR+1},
    {"direction": None}, {"direction": "BUY"}, {"status": "OTHER"},
    {"score": None}, {"base_price": None}, {"neutral_pct": None}, {"neutral_pct": .09},
    {"status": "SKIPPED", "direction": "WAIT", "score": None},
])
def test_invalid_decision_shape_is_rejected(store, change):
    with pytest.raises(IntegrityError):
        store.save_strategy_forecasts([record(**change)])


@pytest.mark.parametrize("change", [
    {"score": float("nan")}, {"base_price": float("inf")}, {"horizon": True},
    {"issued_at": 1.5}, {"indicators": {"nan": float("nan")}},
    {"reason_codes": "NOT_A_LIST"}, {"reason_codes": [1]}, {"indicators": []},
    {"correct": True}, {"evaluated_at": CLOSE+HOUR}, {"symbol": ""},
])
def test_invalid_values_and_preset_evaluations_are_rejected(store, change):
    with pytest.raises(ValueError):
        store.save_strategy_forecasts([record(**change)])


def test_wait_and_technical_skips_never_become_wins(store):
    rows = [record(direction="WAIT"),
            record(strategy_id="trend_momentum", status="SKIPPED", direction=None, score=None),
            record(strategy_id="consensus", status="DATA_UNAVAILABLE", direction=None,
                   score=None, base_price=None, neutral_pct=None,
                   input_close_at=CLOSE+HOUR, issued_at=CLOSE+HOUR+10_000, due_at=CLOSE+2*HOUR)]
    store.save_strategy_forecasts(rows)
    saved = store.strategy_history("BTCUSDT")["items"]
    wait = next(r for r in saved if r["direction"] == "WAIT")
    assert store.resolve_strategy_forecast(wait["id"], evaluation(correct=True), CLOSE+HOUR)
    assert first(store, strategy_id="trend_breakout")["correct"] is None
    for row in saved:
        if row["status"] != "OK":
            assert not store.resolve_strategy_forecast(row["id"], evaluation(), CLOSE+3*HOUR)
            assert not store.note_strategy_forecast(row["id"], "retry", CLOSE+3*HOUR)
    assert all(r["correct"] is None for r in store.strategy_history("BTCUSDT")["items"])


@pytest.mark.parametrize("status,direction", [("OK", "WAIT"), ("SKIPPED", None), ("DATA_UNAVAILABLE", None)])
def test_direct_sql_cannot_mark_wait_or_technical_status_as_correct(store, status, direction):
    values = record(status=status, direction=direction, score=.5 if status=="OK" else None,
                    reason_codes="[]", indicators="{}", actual_price=1010., change_pct=1.,
                    observed_direction="UP", evaluated_at=CLOSE+HOUR, correct=True)
    with pytest.raises(IntegrityError), store.engine.begin() as conn:
        conn.execute(strategy_forecasts.insert().values(**values))


def test_pending_retry_order_and_immutable_settlement_after_restart(store):
    store.save_strategy_forecasts([record(), record(strategy_id="trend_momentum", direction="WAIT"),
                                   record(strategy_id="consensus", status="SKIPPED", direction=None, score=None)])
    assert store.pending_strategy_forecasts(CLOSE+HOUR-1) == []
    pending = store.pending_strategy_forecasts(CLOSE+HOUR)
    assert len(pending) == 2
    row = pending[0]
    before = copy.deepcopy(row)
    assert store.note_strategy_forecast(row["id"], "Target missing", CLOSE+HOUR)
    assert store.pending_strategy_forecasts(CLOSE+HOUR)[-1]["id"] == row["id"]
    assert store.resolve_strategy_forecast(row["id"], evaluation(direction="DOWN", base_price=1.), CLOSE+HOUR)
    assert not store.resolve_strategy_forecast(row["id"], evaluation(actual_price=900., observed_direction="DOWN"), CLOSE+2*HOUR)
    assert not store.note_strategy_forecast(row["id"], "Overwrite", CLOSE+2*HOUR)
    reopened = Store(store.engine.url)
    try:
        reopened.init()
        after = next(r for r in reopened.strategy_history("BTCUSDT")["items"] if r["id"] == row["id"])
        for key in record():
            assert after[key] == before[key]
        assert after["actual_price"] == 1010. and after["correct"] is True
        assert after["evaluation_note"] is None
        assert len(reopened.pending_strategy_forecasts(CLOSE+3*HOUR)) == 1
    finally:
        reopened.close()


def test_early_incomplete_invalid_evaluation_is_not_saved(store):
    store.save_strategy_forecasts([record()])
    row = first(store)
    with pytest.raises(ValueError):
        store.resolve_strategy_forecast(row["id"], evaluation(), CLOSE+HOUR-1)
    for change in ({"status": "DATA_UNAVAILABLE"}, {"actual_price": 0.},
                   {"change_pct": float("nan")}, {"observed_direction": "WAIT"}):
        with pytest.raises(ValueError):
            store.resolve_strategy_forecast(row["id"], evaluation(**change), CLOSE+HOUR)
    assert first(store)["evaluated_at"] is None
    assert not store.resolve_strategy_forecast(-1, evaluation(), CLOSE+HOUR)


def test_directional_flat_is_not_a_win_even_if_caller_says_correct(store):
    store.save_strategy_forecasts([record()])
    assert store.resolve_strategy_forecast(first(store)["id"], evaluation(
        actual_price=1000., change_pct=0., observed_direction="FLAT", correct=True
    ), CLOSE+HOUR)
    assert first(store)["correct"] is False


def test_real_stage_one_output_can_be_saved_and_evaluated_for_all_strategies(store):
    rows = [dict(time=i*HOUR, open=1000.+2*i, high=1000.25+2*i, low=999.75+2*i,
                 close=1000.+2*i, volume=10., close_time=(i+1)*HOUR-1) for i in range(500)]
    calculations = analyze_strategies(rows, now=CLOSE+10_000, confirmed_at=CLOSE+10_000)
    records = []
    for result in calculations.values():
        for horizon in (1, 4):
            records.append(record(**{k: result[k] for k in (
                "strategy_id", "strategy_version", "input_close_at", "base_price",
                "status", "direction", "score", "reason_codes", "indicators"
            )}, horizon=horizon, due_at=result["due_at"][horizon],
                neutral_pct=result["thresholds_pct"][horizon]))
    assert store.save_strategy_forecasts(records) == 6
    for result in calculations.values():
        due = result["due_at"][1]
        target = dict(time=due-HOUR, open=2100., high=2101., low=2099., close=2100.,
                      volume=10., close_time=due-1)
        checked = evaluate_result(result, 1, target, now=due, confirmed_at=due)
        saved = first(store, strategy_id=result["strategy_id"], horizon=1)
        assert store.resolve_strategy_forecast(saved["id"], checked, due)
    assert len(store.pending_strategy_forecasts(CLOSE+4*HOUR)) == 3


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 1001}, {"limit": True},
                                   {"offset": -1}, {"horizon": 30}])
def test_history_parameter_bounds(store, kwargs):
    with pytest.raises(ValueError):
        store.strategy_history("BTCUSDT", **kwargs)


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_schema_compiles_for_both_dialects(dialect):
    sql = str(CreateTable(strategy_forecasts).compile(dialect=dialect))
    assert "unique_strategy_forecast" in sql
    assert "strategy_live_window" in sql
    assert "strategy_evaluation_shape" in sql


def test_postgres_insert_path_uses_do_nothing_returning_not_update():
    # Actual PostgreSQL dialect/compiler, mocked connection; no external server.
    store = object.__new__(Store)
    store.engine = MagicMock()
    store.engine.dialect.name = "postgresql"
    conn = store.engine.begin.return_value.__enter__.return_value
    conn.execute.return_value.scalar_one_or_none.return_value = 1
    assert store.save_strategy_forecasts([record()]) == 1
    statement = conn.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "ON CONFLICT (symbol, input_close_at, strategy_id, strategy_version, horizon) DO NOTHING" in sql
    assert "RETURNING strategy_forecasts.id" in sql
    assert "DO UPDATE" not in sql

import json
import math
import os
from datetime import timezone
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import (
    create_engine, MetaData, Table, Column, Integer, BigInteger,
    String, Float, Text, Boolean, DateTime, UniqueConstraint, CheckConstraint,
    select, func, case, and_, or_, inspect, text, union_all
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from .market_inputs import NUMERIC_FIELDS, numeric_values
from . import evaluation_protocol as protocol
from .strategies import evaluate_result

metadata = MetaData()

forecasts = Table(
    "forecasts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("symbol", String(12), nullable=False),
    Column("horizon", Integer, nullable=False),
    Column("slot", BigInteger, nullable=False),
    Column("issued_at", BigInteger, nullable=False),
    Column("reference_at", BigInteger, nullable=False),
    Column("due_at", BigInteger, nullable=False, index=True),
    Column("base_price", Float, nullable=False),
    Column("direction", String(4), nullable=False),
    Column("confidence", Integer, nullable=False),
    Column("score", Float, nullable=False),
    Column("neutral_pct", Float, nullable=False),
    Column("model_version", String(32), nullable=False),
    Column("indicators", Text, nullable=False),
    Column("reasons", Text, nullable=False),
    Column("actual_price", Float),
    Column("change_pct", Float),
    Column("observed_direction", String(4)),
    Column("correct", Boolean),
    Column("evaluated_at", BigInteger),
    Column("evaluation_note", Text),
    Column("last_attempt_at", BigInteger),
    UniqueConstraint(
        "symbol",
        "horizon",
        "slot",
        name="unique_forecast_slot"
    )
)


market_quotes = Table(
    "market_quotes",
    metadata,
    Column("symbol", String(12), primary_key=True),
    Column("price", Float, nullable=False),
    Column("change_pct", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("volume", Float, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("fetched_at", BigInteger, nullable=False),
)

market_candles = Table(
    "market_candles",
    metadata,
    Column("symbol", String(12), primary_key=True),
    Column("interval", String(4), primary_key=True),
    Column("time", BigInteger, primary_key=True),
    Column("open", Float, nullable=False),
    Column("high", Float, nullable=False),
    Column("low", Float, nullable=False),
    Column("close", Float, nullable=False),
    Column("volume", Float, nullable=False),
    Column("close_time", BigInteger, nullable=False),
    # Set only by a successful collection made after this candle closed.
    Column("confirmed_at", BigInteger),
    Column("observation_id", BigInteger),
    Column("observed_at", DateTime(timezone=True)),
    # Exact normalized decimals; existing Float columns remain API-compatible.
    Column("numeric_values", Text),
)


market_candle_revisions = Table(
    "market_candle_revisions", metadata,
    Column("observation_id", BigInteger, primary_key=True, autoincrement=False),
    *[Column(c.name, c.type, nullable=c.nullable)
      for c in market_candles.c if c.name != "observation_id"],
)

# One transactional, global monotonic allocator and the honest migration boundary.
# A legacy candle is NOT an observation until accepted again after this boundary.
market_data_state = Table(
    "market_data_state", metadata,
    Column("id", Integer, primary_key=True),
    Column("last_observation_id", BigInteger, nullable=False),
    Column("revision_tracking_started_at", DateTime(timezone=True), nullable=False,
           server_default=func.current_timestamp()),
    Column("evaluation_protocol_started_at", DateTime(timezone=True),
           server_default=func.current_timestamp()),
)


STRATEGY_FORECAST_KEY = (
    "symbol", "input_close_at", "strategy_id", "strategy_version", "horizon"
)

PUBLISH_KEY = ("symbol", "input_close_at", "strategy_id", "strategy_version")
publish_observations = Table(
    "strategy_publish_observations", metadata,
    *[Column(k, BigInteger if k == "input_close_at" else String(64), primary_key=True)
      for k in PUBLISH_KEY],
    *[Column(k, Text) for k in ("bid_price", "ask_price", "bid_qty", "ask_qty", "mid_price")],
    Column("request_started_at", BigInteger),
    Column("response_received_at", BigInteger),
    Column("provider", Text, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("status", String(32), nullable=False),
    Column("error", Text),
)

PROTOCOL_RESULT_TYPES = {
    "target_observation_id": BigInteger,
    "target_observed_at": DateTime(timezone=True),
    "result_status": String(32),
    "live_status": String(32),
    "live_change_pct": Float,
    "live_observed_direction": String(4),
    "live_correct": Boolean,
}

# LIVE records only. BACKTEST issuance is intentionally not enabled by this
# schema: the actual issue time must be within five minutes of the hourly close.
strategy_forecasts = Table(
    "strategy_forecasts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("symbol", String(12), nullable=False),
    Column("strategy_id", String(64), nullable=False),
    Column("strategy_version", String(32), nullable=False),
    Column("input_close_at", BigInteger, nullable=False),
    Column("horizon", Integer, nullable=False),
    Column("issued_at", BigInteger, nullable=False),
    Column("base_price", Float),
    Column("due_at", BigInteger, nullable=False, index=True),
    Column("status", String(20), nullable=False),
    Column("direction", String(4)),
    Column("score", Float),
    Column("reason_codes", Text, nullable=False),
    Column("indicators", Text, nullable=False),
    Column("evaluation_version", String(32), nullable=False),
    Column("neutral_pct", Float),
    Column("actual_price", Float),
    Column("change_pct", Float),
    Column("observed_direction", String(4)),
    Column("correct", Boolean),
    Column("evaluated_at", BigInteger),
    Column("evaluation_note", Text),
    Column("last_attempt_at", BigInteger),
    *[Column(k, kind) for k, kind in PROTOCOL_RESULT_TYPES.items()],
    Column("input_data_cutoff_id", BigInteger),
    Column("input_hash", String(64)),
    Column("recorded_at", DateTime(timezone=True),
           server_default=func.current_timestamp()),
    UniqueConstraint(*STRATEGY_FORECAST_KEY, name="unique_strategy_forecast"),
    CheckConstraint("horizon IN (1, 4)", name="strategy_horizon"),
    CheckConstraint(
        "input_close_at % 3600000 = 0 AND "
        "due_at = input_close_at + horizon * 3600000",
        name="strategy_hourly_target",
    ),
    CheckConstraint(
        "issued_at >= input_close_at AND issued_at <= input_close_at + 300000",
        name="strategy_live_window",
    ),
    CheckConstraint(
        "(status = 'OK' AND direction IS NOT NULL "
        "AND direction IN ('UP', 'DOWN', 'WAIT') AND score IS NOT NULL "
        "AND base_price IS NOT NULL AND base_price > 0 "
        "AND neutral_pct IS NOT NULL AND neutral_pct >= 0.10) OR "
        "(status IN ('SKIPPED', 'DATA_UNAVAILABLE') "
        "AND direction IS NULL AND score IS NULL)",
        name="strategy_decision_shape",
    ),
    CheckConstraint(
        "(evaluated_at IS NULL AND actual_price IS NULL AND change_pct IS NULL "
        "AND observed_direction IS NULL AND correct IS NULL) OR "
        "(status = 'OK' AND evaluated_at IS NOT NULL AND evaluated_at >= due_at "
        "AND actual_price IS NOT NULL AND actual_price > 0 "
        "AND change_pct IS NOT NULL AND observed_direction IS NOT NULL "
        "AND observed_direction IN ('UP', 'DOWN', 'FLAT') "
        "AND ((direction = 'WAIT' AND correct IS NULL) OR "
        "(direction IN ('UP', 'DOWN') AND correct IS NOT NULL)))",
        name="strategy_evaluation_shape",
    ),
)


def database_url():
    url = os.getenv("DATABASE_URL", "")

    if os.getenv("RENDER") and not url.startswith(
        ("postgres://", "postgresql://", "postgresql+psycopg://")
    ):
        raise RuntimeError(
            "Render requires an external PostgreSQL DATABASE_URL; "
            "local SQLite is not durable."
        )

    if not url:
        Path("data").mkdir(exist_ok=True)
        url = "sqlite:///data/crypto.db"

    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]

    return url


class Store:
    def __init__(self, url=None):
        url = url or database_url()

        options = (
            {
                "connect_args": {
                    "check_same_thread": False,
                    "timeout": 20
                }
            }
            if make_url(url).get_backend_name() == "sqlite"
            else {
                "pool_size": 3,
                "max_overflow": 2,
                "connect_args": {
                    "connect_timeout": 12
                }
            }
        )

        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            hide_parameters=True,
            **options
        )

    def init(self):
        # create_all does not add columns to an existing Neon/SQLite table.
        with self._candle_transaction() as conn:
            metadata.create_all(conn)
            postgres = conn.dialect.name == "postgresql"
            optional = " IF NOT EXISTS" if postgres else ""
            for table, additions in (
                ("market_candles", {"confirmed_at": "BIGINT", "observation_id": "BIGINT",
                                    "observed_at": "TIMESTAMP WITH TIME ZONE" if postgres else "DATETIME",
                                    "numeric_values": "TEXT"}),
                ("strategy_forecasts", {"input_data_cutoff_id": "BIGINT", "input_hash": "VARCHAR(64)",
                                        "recorded_at": "TIMESTAMP WITH TIME ZONE" if postgres else "DATETIME",
                                        **{k: str(Column(k, v).type.compile(dialect=conn.dialect))
                                           for k, v in PROTOCOL_RESULT_TYPES.items()}}),
                ("market_data_state", {"evaluation_protocol_started_at":
                                      "TIMESTAMP WITH TIME ZONE" if postgres else "DATETIME"}),
            ):
                existing = {c["name"] for c in inspect(conn).get_columns(table)}
                for name, kind in additions.items():
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN{optional} {name} {kind}"))
            # No fake timestamps on old forecasts. Only subsequent inserts get DB time.
            if postgres:
                conn.execute(text("ALTER TABLE strategy_forecasts ALTER COLUMN recorded_at "
                                  "SET DEFAULT CURRENT_TIMESTAMP"))
            else:
                conn.execute(text("""CREATE TRIGGER IF NOT EXISTS strategy_recorded_at
                    AFTER INSERT ON strategy_forecasts WHEN NEW.recorded_at IS NULL
                    BEGIN UPDATE strategy_forecasts SET recorded_at = CURRENT_TIMESTAMP
                    WHERE id = NEW.id; END"""))
            insert = pg_insert if postgres else sqlite_insert
            conn.execute(insert(market_data_state).values(id=1, last_observation_id=0)
                         .on_conflict_do_nothing(index_elements=["id"]))
            conn.execute(market_data_state.update().where(
                market_data_state.c.evaluation_protocol_started_at.is_(None)
            ).values(evaluation_protocol_started_at=func.current_timestamp()))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS market_candle_observation "
                              "ON market_candles (observation_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS market_revision_key "
                              "ON market_candle_revisions (symbol, interval, time, observation_id)"))

    @contextmanager
    def _candle_transaction(self):
        """Serialize only DB candle writes/snapshots, also across manual jobs.

        Same lock on every writer; allocation, archival and commit order cannot
        invert. No network or strategy calculation is inside this section.
        SQLite BEGIN IMMEDIATE provides the equivalent single writer boundary.
        """
        with self.engine.connect() as conn:
            with conn.begin():
                if conn.dialect.name == "postgresql":
                    conn.execute(text("SELECT pg_advisory_xact_lock(634729180214)"))
                else:
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield conn

    def close(self):
        self.engine.dispose()

    def ready(self):
        with self.engine.connect() as conn:
            conn.execute(select(1))

    def save_quote(self, quote):
        """Insert or replace the latest quote; timestamps are UTC milliseconds."""
        insert = (
            sqlite_insert
            if self.engine.dialect.name == "sqlite"
            else pg_insert
        )
        statement = insert(market_quotes)

        with self.engine.begin() as conn:
            conn.execute(
                statement.on_conflict_do_update(
                    index_elements=["symbol"],
                    set_={
                        column.name: statement.excluded[column.name]
                        for column in market_quotes.c
                        if column.name != "symbol"
                    },
                ),
                dict(quote),
            )

    def get_quote(self, symbol):
        """Return the saved quote, or None when no quote has been collected."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(market_quotes)
                .where(market_quotes.c.symbol == symbol)
            ).first()

        return dict(row._mapping) if row is not None else None

    def save_candles(self, symbol, interval, candles, *, confirmed_at=None):
        """Update current bars; archive only changed accepted confirmed versions."""
        if confirmed_at is not None and (type(confirmed_at) is not int or confirmed_at < 0):
            raise ValueError("confirmed_at must be a nonnegative UTC timestamp")
        if not candles:
            return
        c = market_candles.c
        with self._candle_transaction() as conn:
            previous = {r["time"]: dict(r) for r in conn.execute(
                select(market_candles).where(c.symbol == symbol, c.interval == interval,
                                            c.time.in_([bar["time"] for bar in candles]))
            ).mappings()}
            accepted, archived, new_versions = {}, [], []
            for candle in candles:
                start, close_time = candle["time"], candle["close_time"]
                if type(start) is not int or type(close_time) is not int:
                    raise ValueError("Candle times must be integer UTC milliseconds")
                if start in accepted:
                    raise ValueError("Duplicate candle key in one response")
                old = previous.get(start)
                confirmation = confirmed_at if confirmed_at is not None and close_time < confirmed_at else None
                if old and old["confirmed_at"] is not None and (
                    confirmation is None or confirmation < old["confirmed_at"]
                ):
                    continue
                numbers = numeric_values(candle)
                encoded = json.dumps(numbers, sort_keys=True, separators=(",", ":"))
                old_numbers = (json.loads(old["numeric_values"]) if old and old["numeric_values"]
                               else numeric_values(old) if old else None)
                unchanged = old and old_numbers == numbers and old["close_time"] == close_time
                values = dict(symbol=symbol, interval=interval, time=start,
                              close_time=close_time, confirmed_at=confirmation,
                              numeric_values=encoded, observation_id=None, observed_at=None,
                              **{f: float(numbers[f]) for f in NUMERIC_FIELDS})
                if confirmation is not None:
                    if old and old["observation_id"] is not None and unchanged:
                        values["observation_id"] = old["observation_id"]
                        values["observed_at"] = old["observed_at"]
                    else:
                        if old and old["observation_id"] is not None:
                            archived.append(old)
                        new_versions.append(values)
                accepted[start] = values
            if new_versions:
                last = conn.execute(market_data_state.update().where(market_data_state.c.id == 1)
                                    .values(last_observation_id=market_data_state.c.last_observation_id + len(new_versions))
                                    .returning(market_data_state.c.last_observation_id)).scalar_one()
                observed = conn.scalar(select(func.clock_timestamp() if conn.dialect.name == "postgresql"
                                              else func.current_timestamp()))
                for offset, values in enumerate(new_versions, start=last-len(new_versions)+1):
                    values.update(observation_id=offset, observed_at=observed)
            if archived:
                conn.execute(market_candle_revisions.insert(), archived)
            if accepted:
                insert = sqlite_insert if conn.dialect.name == "sqlite" else pg_insert
                statement = insert(market_candles)
                conn.execute(statement.on_conflict_do_update(
                    index_elements=["symbol", "interval", "time"],
                    set_={f: statement.excluded[f] for f in next(iter(accepted.values()))
                          if f not in ("symbol", "interval", "time")},
                ), list(accepted.values()))

    @staticmethod
    def _candles_at(conn, symbol, interval, reference_time, cutoff_id, limit):
        versions = union_all(select(market_candles), select(*[
            market_candle_revisions.c[c.name] for c in market_candles.c
        ])).subquery()
        ranked = select(*versions.c, func.row_number().over(
            partition_by=versions.c.time, order_by=versions.c.observation_id.desc()
        ).label("version_rank")).where(
            versions.c.symbol == symbol, versions.c.interval == interval,
            versions.c.observation_id <= cutoff_id, versions.c.observed_at.is_not(None),
            versions.c.confirmed_at.is_not(None),
            versions.c.close_time < versions.c.confirmed_at,
            versions.c.close_time < reference_time,
        ).subquery()
        rows = conn.execute(select(ranked).where(ranked.c.version_rank == 1)
                            .order_by(ranked.c.time.desc()).limit(limit)).mappings().all()
        # Preserve decimal inputs exactly; analyze() retains its existing float
        # conversion/indicator semantics. Metadata is NOT part of candle input.
        return [dict(time=r["time"], **json.loads(r["numeric_values"]), close_time=r["close_time"])
                for r in reversed(rows)]

    def get_candles_at(self, symbol, interval, reference_time, cutoff_id, limit=200):
        """Latest confirmed version of each key as known at a committed cutoff."""
        if type(cutoff_id) is not int or cutoff_id < 0 or type(reference_time) is not int:
            raise ValueError("Invalid point-in-time cutoff")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.engine.connect() as conn:
            return self._candles_at(conn, symbol, interval, reference_time, cutoff_id, limit)

    def strategy_inputs(self, symbol, reference_time, windows, *, cutoff_id=None):
        """Capture committed cutoff + exact windows together, or replay a cutoff.

        The allocator is read only after acquiring the writer lock, never a
        sequence last_value that another transaction has not committed yet.
        """
        if type(reference_time) is not int or any(type(n) is not int or not 1 <= n <= 1000
                                                for n in windows.values()):
            raise ValueError("Invalid strategy input window")
        with self._candle_transaction() as conn:
            state = conn.execute(select(market_data_state).where(market_data_state.c.id == 1)).mappings().one()
            committed = state["last_observation_id"]
            cutoff = committed if cutoff_id is None else cutoff_id
            if type(cutoff) is not int or not 0 <= cutoff <= committed:
                raise ValueError("Cutoff must be a committed observation")
            candles = {interval: self._candles_at(conn, symbol, interval, reference_time, cutoff, count)
                       for interval, count in windows.items()}
            started = state["revision_tracking_started_at"]
            boundary = started.isoformat() + ("Z" if started.tzinfo is None else "")
            return dict(input_data_cutoff_id=cutoff, candles=candles,
                        revision_tracking_started_at=boundary)

    def get_candles(self, symbol, interval, limit=200, *, confirmed_only=False):
        """Return the latest candles in chronological order, or an empty list."""
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")

        c = market_candles.c
        columns = [c.time, c.open, c.high, c.low, c.close, c.volume, c.close_time]
        where = [c.symbol == symbol, c.interval == interval]
        if confirmed_only:
            columns.append(c.confirmed_at)
            where.extend([c.confirmed_at.is_not(None), c.close_time < c.confirmed_at])

        with self.engine.connect() as conn:
            rows = conn.execute(
                select(*columns)
                .where(*where)
                .order_by(c.time.desc())
                .limit(limit)
            ).all()

        return [dict(row._mapping) for row in reversed(rows)]

    def save_strategy_forecasts(self, records):
        """Atomically insert a batch; exact retries are no-ops, conflicts raise.

        Only original decision fields are accepted. Evaluation is added through
        resolve_strategy_forecast(), never through a replacement/upsert.
        """
        result_fields = {
            "id", "actual_price", "change_pct", "observed_direction", "correct",
            "evaluated_at", "evaluation_note", "last_attempt_at", "recorded_at",
        }
        result_fields.update(PROTOCOL_RESULT_TYPES)
        fields = {c.name for c in strategy_forecasts.c} - result_fields
        prepared = []
        contexts = {}
        for record in records:
            values = dict(record)
            # Old engines/history have no point-in-time input; never invent it.
            values.setdefault("input_data_cutoff_id", None)
            values.setdefault("input_hash", None)
            if set(values) != fields:
                raise ValueError("Provide exactly the original strategy forecast fields")
            cutoff, digest = values["input_data_cutoff_id"], values["input_hash"]
            if (cutoff is None) != (digest is None) or cutoff is not None and (
                type(cutoff) is not int or cutoff <= 0 or not isinstance(digest, str)
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise ValueError("Provide a valid input cutoff and SHA-256 together")
            for key in ("input_close_at", "issued_at", "due_at", "horizon"):
                if type(values[key]) is not int:
                    raise ValueError(f"{key} must be an integer")
            for key in ("symbol", "strategy_id", "strategy_version", "evaluation_version"):
                if not isinstance(values[key], str) or not values[key].strip():
                    raise ValueError(f"{key} must be a nonempty string")
            for key in ("base_price", "score", "neutral_pct"):
                value = values[key]
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError(f"{key} must be finite or None")
            if not isinstance(values["reason_codes"], list) or not all(
                isinstance(code, str) for code in values["reason_codes"]
            ) or not isinstance(values["indicators"], dict):
                raise ValueError("reason_codes must be a string list and indicators a dict")
            group = (values["symbol"], values["input_close_at"])
            context = (values["issued_at"], values["base_price"])
            if group in contexts and contexts[group] != context:
                raise ValueError("A batch must share issue time and hourly base price")
            contexts[group] = context
            for key in ("reason_codes", "indicators"):
                values[key] = json.dumps(
                    values[key], ensure_ascii=False, allow_nan=False, sort_keys=True
                )
            prepared.append(values)

        if not prepared:
            return 0

        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        inserted = 0
        with self.engine.begin() as conn:
            for values in prepared:
                forecast_id = conn.execute(
                    insert(strategy_forecasts).values(**values)
                    .on_conflict_do_nothing(index_elements=list(STRATEGY_FORECAST_KEY))
                    .returning(strategy_forecasts.c.id)
                ).scalar_one_or_none()
                if forecast_id is not None:
                    inserted += 1
                    if values["evaluation_version"] == protocol.VERSION:
                        conn.execute(insert(publish_observations).values(
                            **{k: values[k] for k in PUBLISH_KEY}, status="PENDING",
                            provider="Binance Spot / data-api.binance.vision",
                            endpoint=protocol.ENDPOINT,
                        ).on_conflict_do_nothing(index_elements=list(PUBLISH_KEY)))
                    continue
                existing = conn.execute(
                    select(strategy_forecasts).where(*[
                        strategy_forecasts.c[key] == values[key]
                        for key in STRATEGY_FORECAST_KEY
                    ])
                ).mappings().one()
                if any(existing[key] != value for key, value in values.items()):
                    raise ValueError("A published strategy forecast cannot be overwritten")
        return inserted

    @staticmethod
    def _decode_strategy(row):
        values = dict(row._mapping)
        for key in ("reason_codes", "indicators"):
            values[key] = json.loads(values[key])
        return values

    def has_strategy_forecast(self, symbol, input_close_at, strategy_id,
                              strategy_version, horizon):
        values = (symbol, input_close_at, strategy_id, strategy_version, horizon)
        with self.engine.connect() as conn:
            return conn.execute(
                select(strategy_forecasts.c.id).where(*[
                    strategy_forecasts.c[key] == value
                    for key, value in zip(STRATEGY_FORECAST_KEY, values)
                ])
            ).first() is not None

    def strategy_history(self, symbol, strategy_id=None, horizon=None,
                         offset=0, limit=100, strategy_version=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be nonnegative")
        if horizon is not None and (type(horizon) is not int or horizon not in (1, 4)):
            raise ValueError("horizon must be 1 or 4")
        c = strategy_forecasts.c
        where = [c.symbol == symbol]
        for column, value in ((c.strategy_id, strategy_id), (c.horizon, horizon),
                              (c.strategy_version, strategy_version)):
            if value is not None:
                where.append(column == value)
        with self.engine.connect() as conn:
            total = conn.execute(
                select(func.count()).select_from(strategy_forecasts).where(*where)
            ).scalar_one()
            rows = conn.execute(
                select(strategy_forecasts).where(*where)
                .order_by(c.input_close_at.desc(), c.id.desc())
                .offset(offset).limit(limit)
            ).all()
        return {"total": total, "items": [self._decode_strategy(row) for row in rows]}

    def pending_strategy_forecasts(self, now, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        c = strategy_forecasts.c
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(strategy_forecasts)
                .where(c.status == "OK", c.due_at <= now, c.evaluated_at.is_(None))
                .order_by(c.last_attempt_at.asc().nullsfirst(), c.due_at, c.id)
                .limit(limit)
            ).all()
        return [self._decode_strategy(row) for row in rows]

    def resolve_strategy_forecast(self, forecast_id, evaluation, now):
        """Append the first completed evaluation; decision fields never change."""
        c = strategy_forecasts.c
        pending = (c.id == forecast_id, c.status == "OK", c.evaluated_at.is_(None))
        with self.engine.begin() as conn:
            row = conn.execute(select(strategy_forecasts).where(*pending)).mappings().first()
            if row is None:
                return False
            if type(now) is not int or now < row["due_at"]:
                raise ValueError("Cannot evaluate before the target hourly close")
            if evaluation.get("status", "OK") != "OK":
                raise ValueError("Only a completed evaluation can be saved")
            protocol_values = {}
            if row["evaluation_version"] == protocol.VERSION:
                # Select and evaluate inside the same short DB transaction.
                # Earliest eligible accepted version wins, even after correction.
                target = self._target_observation(conn, row["symbol"], row["due_at"])
                if target is None:
                    return False
                horizon = row["horizon"]
                frozen = {**row, "due_at": {horizon: row["due_at"]},
                          "thresholds_pct": {horizon: row["neutral_pct"]}}
                def calculate(base):
                    return evaluate_result(
                        {**frozen, "base_price": base}, horizon, dict(target),
                        now=now, confirmed_at=row["due_at"],
                    )
                evaluation = calculate(row["base_price"])
                observed = target["observed_at"]
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                observed_ms = observed.timestamp() * 1000
                timing = ("OK" if observed_ms <= row["due_at"] + protocol.TARGET_WINDOW_MS
                          else "LATE_TARGET_DATA")
                protocol_values = dict(target_observation_id=target["observation_id"],
                                       target_observed_at=observed, result_status=timing,
                                       live_status="UNAVAILABLE")
                quote = conn.execute(select(publish_observations).where(*[
                    publish_observations.c[k] == row[k] for k in PUBLISH_KEY
                ])).mappings().first()
                if quote and quote["status"] == "OK":
                    live = calculate(float(quote["mid_price"]))
                    protocol_values.update(live_status=timing,
                                           live_change_pct=live["change_pct"],
                                           live_observed_direction=live["observed_direction"],
                                           live_correct=live["correct"])
            values = {key: evaluation[key] for key in (
                "actual_price", "change_pct", "observed_direction"
            )}
            for key in ("actual_price", "change_pct"):
                value = values[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{key} must be finite")
            if values["actual_price"] <= 0 or values["observed_direction"] not in ("UP", "DOWN", "FLAT"):
                raise ValueError("Invalid actual price or observed direction")
            # Never trust a caller's correct=True for WAIT or a mismatched vote.
            values["correct"] = (
                None if row["direction"] == "WAIT"
                else row["direction"] == values["observed_direction"]
            )
            result = conn.execute(
                strategy_forecasts.update().where(*pending)
                .values(**values, **protocol_values, evaluated_at=now, evaluation_note=None)
            )
            return result.rowcount == 1

    @staticmethod
    def _target_observation(conn, symbol, due_at):
        versions = union_all(select(market_candles), select(*[
            market_candle_revisions.c[c.name] for c in market_candles.c
        ])).subquery()
        c = versions.c
        return conn.execute(select(versions).where(
            c.symbol == symbol, c.interval == "1h", c.time == due_at - 3_600_000,
            c.close_time == due_at - 1, c.confirmed_at >= due_at,
            c.observation_id.is_not(None), c.observed_at.is_not(None),
        ).order_by(c.observation_id).limit(1)).mappings().first()

    def publish_observation(self, symbol, input_close_at, strategy_id, strategy_version):
        with self.engine.connect() as conn:
            row = conn.execute(select(publish_observations).where(*[
                publish_observations.c[k] == value for k, value in zip(
                    PUBLISH_KEY, (symbol, input_close_at, strategy_id, strategy_version))
            ])).mappings().first()
            return dict(row) if row else None

    def target_observation(self, symbol, due_at):
        with self.engine.connect() as conn:
            row = self._target_observation(conn, symbol, due_at)
            return dict(row) if row else None

    def claim_publish_observation(self, key, started_at):
        # A durable one-attempt claim; a process crash remains REQUESTING.
        with self.engine.begin() as conn:
            result = conn.execute(publish_observations.update().where(*[
                publish_observations.c[k] == key[k] for k in PUBLISH_KEY
            ], publish_observations.c.status == "PENDING").values(
                status="REQUESTING", request_started_at=started_at))
            return result.rowcount == 1

    def finish_publish_observation(self, key, received_at, *, data=None, error=None):
        fields = protocol.normalize_quote(data, key["symbol"]) if data is not None else {}
        with self.engine.begin() as conn:
            return conn.execute(publish_observations.update().where(*[
                publish_observations.c[k] == key[k] for k in PUBLISH_KEY
            ], publish_observations.c.status == "REQUESTING").values(
                **fields, response_received_at=received_at,
                status="OK" if data is not None else "QUOTE_FAILED", error=error,
            )).rowcount == 1

    def note_strategy_forecast(self, forecast_id, note, now):
        c = strategy_forecasts.c
        with self.engine.begin() as conn:
            result = conn.execute(
                strategy_forecasts.update()
                .where(c.id == forecast_id, c.status == "OK", c.evaluated_at.is_(None))
                .values(evaluation_note=note, last_attempt_at=now)
            )
            return result.rowcount == 1

    def has_slot(self, symbol, horizon, slot):
        with self.engine.connect() as conn:
            return conn.execute(
                select(forecasts.c.id).where(
                    and_(
                        forecasts.c.symbol == symbol,
                        forecasts.c.horizon == horizon,
                        forecasts.c.slot == slot
                    )
                )
            ).first() is not None

    def save(self, values):
        values = dict(values)

        for key in ("reasons", "indicators"):
            values[key] = json.dumps(
                values[key],
                ensure_ascii=False,
                allow_nan=False
            )

        insert = (
            sqlite_insert
            if self.engine.dialect.name == "sqlite"
            else pg_insert
        )

        with self.engine.begin() as conn:
            result = conn.execute(
                insert(forecasts)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        "symbol",
                        "horizon",
                        "slot"
                    ]
                )
            )

            return result.rowcount == 1

    @staticmethod
    def decode(row):
        value = dict(row._mapping)

        for key in ("reasons", "indicators"):
            value[key] = json.loads(value[key])

        return value

    def latest(self, symbol):
        rows = []

        # 30 = 30 минут, 1 = 1 час, 4 = 4 часа.
        for horizon in (30, 1, 4):
            with self.engine.connect() as conn:
                row = conn.execute(
                    select(forecasts)
                    .where(
                        and_(
                            forecasts.c.symbol == symbol,
                            forecasts.c.horizon == horizon
                        )
                    )
                    .order_by(forecasts.c.id.desc())
                    .limit(1)
                ).first()

            if row:
                rows.append(self.decode(row))

        return rows

    def history(
        self,
        symbol,
        horizon=0,
        offset=0,
        limit=20
    ):
        where = [forecasts.c.symbol == symbol]

        if horizon:
            where.append(
                forecasts.c.horizon == horizon
            )

        with self.engine.connect() as conn:
            total = conn.execute(
                select(func.count())
                .select_from(forecasts)
                .where(*where)
            ).scalar_one()

            rows = conn.execute(
                select(forecasts)
                .where(*where)
                .order_by(forecasts.c.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()

        return {
            "total": total,
            "items": [
                self.decode(row)
                for row in rows
            ]
        }

    def pending(self, now, limit=100):
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(forecasts)
                .where(
                    forecasts.c.due_at <= now,
                    forecasts.c.evaluated_at.is_(None)
                )
                .order_by(
                    forecasts.c.last_attempt_at.asc().nullsfirst(),
                    forecasts.c.due_at
                )
                .limit(limit)
            ).all()

        return [
            self.decode(row)
            for row in rows
        ]

    def resolve(
        self,
        forecast_id,
        evaluation,
        now
    ):
        allowed = {
            key: evaluation[key]
            for key in (
                "actual_price",
                "change_pct",
                "observed_direction",
                "correct"
            )
        }

        with self.engine.begin() as conn:
            conn.execute(
                forecasts.update()
                .where(
                    forecasts.c.id == forecast_id,
                    forecasts.c.evaluated_at.is_(None)
                )
                .values(
                    **allowed,
                    evaluated_at=now,
                    evaluation_note=None
                )
            )

    def note(
        self,
        forecast_id,
        note,
        now
    ):
        with self.engine.begin() as conn:
            conn.execute(
                forecasts.update()
                .where(
                    forecasts.c.id == forecast_id,
                    forecasts.c.evaluated_at.is_(None)
                )
                .values(
                    evaluation_note=note,
                    last_attempt_at=now
                )
            )

    def stats(self, symbol):
        f = forecasts.c

        done = f.evaluated_at.is_not(None)
        directional = and_(
            done,
            f.direction != "WAIT"
        )
        wait = and_(
            done,
            f.direction == "WAIT"
        )

        count = lambda cond: func.sum(
            case((cond, 1), else_=0)
        )

        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    f.horizon,
                    func.count().label("total"),
                    count(done).label("evaluated"),
                    count(directional).label(
                        "directional_total"
                    ),
                    count(
                        and_(
                            directional,
                            f.correct.is_(True)
                        )
                    ).label("directional_correct"),
                    count(wait).label("wait_total"),
                    count(
                        and_(
                            wait,
                            f.correct.is_(True)
                        )
                    ).label("wait_correct")
                )
                .where(f.symbol == symbol)
                .group_by(f.horizon)
            ).all()

        result = []

        for row in rows:
            d = dict(row._mapping)

            d["directional_accuracy"] = (
                round(
                    d["directional_correct"]
                    / d["directional_total"]
                    * 100,
                    1
                )
                if d["directional_total"]
                else None
            )

            d["wait_accuracy"] = (
                round(
                    d["wait_correct"]
                    / d["wait_total"]
                    * 100,
                    1
                )
                if d["wait_total"]
                else None
            )

            result.append(d)

        return result

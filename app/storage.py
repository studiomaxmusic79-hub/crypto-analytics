import json
import math
import os
import hashlib
import uuid
from datetime import timezone
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import (
    create_engine, MetaData, Table, Column, Integer, BigInteger,
    String, Float, Text, Boolean, DateTime, LargeBinary, Index, UniqueConstraint, CheckConstraint,
    ForeignKeyConstraint,
    select, func, case, and_, or_, inspect, text, union_all
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from .market_inputs import NUMERIC_FIELDS, numeric_values
from . import evaluation_protocol as protocol
from . import publication
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
    # Null for historical v1 observations; never relabel old facts.
    Column("quote_kind", String(32)),
)

publication_streams = Table(
    "publication_streams", metadata,
    Column("project_id", String(64), primary_key=True),
    Column("official_stream_id", String(64), primary_key=True),
    Column("stream_status", String(16), nullable=False),
    Column("strategy_id", String(64), nullable=False),
    Column("strategy_version", String(32), nullable=False),
    Column("first_slot_utc", BigInteger, nullable=False),
    Column("last_checked_slot_utc", BigInteger, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False,
           server_default=func.current_timestamp()),
    CheckConstraint("stream_status IN ('OFFICIAL', 'EXPERIMENTAL')", name="publication_stream_role"),
)
Index("one_official_stream_per_project", publication_streams.c.project_id, unique=True,
      sqlite_where=publication_streams.c.stream_status == "OFFICIAL",
      postgresql_where=publication_streams.c.stream_status == "OFFICIAL")

EXTERNAL_SLOT_TYPES = {
    "archive_receipt": Text, "witness_receipt": Text, "public_read_receipt": Text,
    "external_step": String(32), "external_step_status": String(32),
    "last_publication_attempt_at": BigInteger, "publication_error": Text,
    "publication_mode": String(16), "publication_claim_token": String(64),
    "publication_claim_until": BigInteger, "post_publish_quotes": Text,
    "post_publish_attempted": Boolean,
}
PUBLICATION_LEASE_MS = 120_000

official_slots = Table(
    "official_slots", metadata,
    Column("project_id", String(64), primary_key=True),
    Column("official_stream_id", String(64), primary_key=True),
    Column("slot_utc", BigInteger, primary_key=True),
    Column("protocol_version", String(32), nullable=False),
    Column("strategy_id", String(64), nullable=False),
    Column("strategy_version", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False,
           server_default=func.current_timestamp()),
    Column("batch_id", String(80)),
    Column("batch_hash", String(64)),
    Column("batch_bytes", LargeBinary),
    Column("frozen_at", BigInteger),
    Column("publication_status", String(32), nullable=False),
    Column("error", Text),
    Column("external_object_key", Text),
    Column("external_object_version", Text),
    Column("witness_id", Text),
    Column("witness_sequence", Text),
    Column("witness_accepted_at", BigInteger),
    Column("public_read_confirmed_at", BigInteger),
    Column("published_at", BigInteger),
    *[Column(k, kind) for k, kind in EXTERNAL_SLOT_TYPES.items()],
    ForeignKeyConstraint(["project_id", "official_stream_id"],
                         ["publication_streams.project_id", "publication_streams.official_stream_id"]),
    CheckConstraint("slot_utc % 3600000 = 0", name="publication_hourly_slot"),
    CheckConstraint("status IN (" + ",".join(repr(s) for s in publication.SLOT_STATUSES) + ")",
                    name="publication_slot_status"),
    CheckConstraint("publication_status IN (" + ",".join(repr(s) for s in publication.PUBLICATION_STATES) + ")",
                    name="publication_workflow_status"),
    CheckConstraint("(batch_id IS NULL AND batch_hash IS NULL AND batch_bytes IS NULL AND frozen_at IS NULL) OR "
                    "(batch_id IS NOT NULL AND batch_hash IS NOT NULL AND batch_bytes IS NOT NULL AND frozen_at IS NOT NULL)",
                    name="publication_batch_shape"),
)

PROTOCOL_RESULT_TYPES = {
    "target_observation_id": BigInteger,
    "target_observed_at": DateTime(timezone=True),
    "result_status": String(32),
    "live_status": String(32),
    "live_change_pct": Float,
    "live_observed_direction": String(4),
    "live_correct": Boolean,
    "live_evidence_mode": String(16),
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
                ("strategy_publish_observations", {"quote_kind": "VARCHAR(32)"}),
                ("official_slots", {k: str(Column(k, v).type.compile(dialect=conn.dialect))
                                    for k, v in EXTERNAL_SLOT_TYPES.items()}),
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

    @contextmanager
    def _publication_transaction(self):
        """Short DB-only boundary; PostgreSQL uses per-stream/per-slot row locks."""
        with self.engine.connect() as conn:
            with conn.begin():
                if conn.dialect.name == "sqlite":
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield conn

    @staticmethod
    def _official_where(table, slot=None):
        conditions = [table.c.project_id == publication.PROJECT_ID,
                      table.c.official_stream_id == publication.STREAM_ID]
        if slot is not None:
            conditions.append(table.c.slot_utc == slot)
        return conditions

    def publication_slot(self, slot):
        with self.engine.connect() as conn:
            row = conn.execute(select(official_slots).where(
                *self._official_where(official_slots, slot))).mappings().first()
            return self._decode_publication(row) if row else None

    @staticmethod
    def _decode_publication(row):
        values = dict(row)
        for key in ("archive_receipt", "witness_receipt", "public_read_receipt", "post_publish_quotes"):
            if values[key] is not None:
                values[key] = json.loads(values[key])
        return values

    def pending_publication_slots(self, limit=100):
        c = official_slots.c
        with self.engine.connect() as conn:
            return conn.execute(select(c.slot_utc).where(
                *self._official_where(official_slots), c.batch_bytes.is_not(None),
                or_(c.publication_mode.is_(None), c.publication_mode == "mock"),
                or_(c.external_step_status.is_(None), c.external_step_status.not_in(("CONFLICT", "HASH_MISMATCH"))),
                or_(c.publication_status != "PUBLISHED", c.post_publish_attempted.is_not(True)),
            ).order_by(c.slot_utc).limit(limit)).scalars().all()

    def claim_external_publication(self, slot, mode, now):
        if mode != "mock":
            raise ValueError("Only mock publication adapters are enabled")
        with self._publication_transaction() as conn:
            row = conn.execute(select(official_slots).where(
                *self._official_where(official_slots, slot)).with_for_update()).mappings().first()
            if (row is None or row["batch_bytes"] is None or row["publication_status"] == "PUBLISHED"
                or row["external_step_status"] in ("CONFLICT", "HASH_MISMATCH")):
                return None
            if row["publication_mode"] not in (None, mode):
                raise ValueError("Cannot reinterpret publication evidence mode")
            if row["publication_claim_token"] and row["publication_claim_until"] > now:
                return None
            token = uuid.uuid4().hex
            conn.execute(official_slots.update().where(*self._official_where(official_slots, slot)).values(
                publication_claim_token=token, publication_claim_until=now+PUBLICATION_LEASE_MS,
                publication_mode=mode, last_publication_attempt_at=now,
            ))
            result = dict(row, publication_claim_token=token, publication_claim_until=now+PUBLICATION_LEASE_MS,
                          publication_mode=mode, last_publication_attempt_at=now)
            return self._decode_publication(result)

    def external_publication_progress(self, slot, token, now, **values):
        allowed = {"archive_receipt", "witness_receipt", "public_read_receipt", "publication_status",
                   "external_step", "external_step_status", "publication_error", "external_object_key",
                   "external_object_version", "witness_id", "witness_sequence", "witness_accepted_at",
                   "public_read_confirmed_at"}
        if set(values) - allowed:
            raise ValueError("Cannot mutate frozen bytes or publication time through progress")
        c = official_slots.c
        with self._publication_transaction() as conn:
            where = [*self._official_where(official_slots, slot), c.publication_claim_token == token,
                     c.publication_claim_until > now, c.publication_status != "PUBLISHED"]
            row = conn.execute(select(official_slots).where(*where).with_for_update()).mappings().first()
            if row is None:
                return None
            transitions = {
                "BATCH_FROZEN": ("BATCH_FROZEN", "EXTERNAL_SUBMISSION"),
                "EXTERNAL_SUBMISSION": ("EXTERNAL_SUBMISSION", "WITNESS_ACCEPTED"),
                "WITNESS_ACCEPTED": ("WITNESS_ACCEPTED", "PUBLIC_READ_CONFIRMED"),
                "PUBLIC_READ_CONFIRMED": ("PUBLIC_READ_CONFIRMED",),
            }
            if values.get("publication_status", row["publication_status"]) not in transitions[row["publication_status"]]:
                raise ValueError("Invalid external publication transition")
            if values.get("external_step_status") in ("CONFLICT", "HASH_MISMATCH"):
                values["status"] = "ERROR"
            for key in ("archive_receipt", "witness_receipt", "public_read_receipt"):
                if key in values:
                    values[key] = json.dumps(values[key], sort_keys=True, allow_nan=False)
            conn.execute(official_slots.update().where(*where).values(
                **values, publication_claim_until=now+PUBLICATION_LEASE_MS))
            return self._decode_publication(conn.execute(select(official_slots).where(
                *self._official_where(official_slots, slot))).mappings().one())

    def complete_external_publication(self, slot, token, now):
        c = official_slots.c
        with self._publication_transaction() as conn:
            where = [*self._official_where(official_slots, slot), c.publication_claim_token == token,
                     c.publication_claim_until > now, c.publication_status == "PUBLIC_READ_CONFIRMED",
                     c.external_step == "FINALIZE",
                     c.external_step_status.not_in(("CONFLICT", "HASH_MISMATCH"))]
            raw = conn.execute(select(official_slots).where(*where).with_for_update()).mappings().first()
            if raw is None:
                return False
            row = self._decode_publication(raw)
            digest = row["batch_hash"]
            archive, witness, read = (row[k] or {} for k in ("archive_receipt", "witness_receipt", "public_read_receipt"))
            if (hashlib.sha256(bytes(row["batch_bytes"])).hexdigest() != digest
                or archive.get("status") != "STORED" or archive.get("stored_hash") != digest
                or witness.get("status") != "ACCEPTED" or witness.get("submitted_hash") != digest
                or read.get("status") != "OK" or read.get("read_hash") != digest
                or read.get("reference") != archive.get("public_reference")
                or row["public_read_confirmed_at"] is None or row["public_read_confirmed_at"] > now
                or row["witness_accepted_at"] is None):
                conn.execute(official_slots.update().where(*where).values(
                    external_step_status="HASH_MISMATCH", publication_error="PUBLICATION_EVIDENCE_INVALID",
                    status="ERROR"))
                return False
            conn.execute(official_slots.update().where(*where).values(
                publication_status="PUBLISHED", published_at=now, external_step="COMPLETE",
                external_step_status="OK", publication_error=None,
                status="PUBLISHED" if row["status"] == "BATCH_FROZEN" else row["status"],
            ))
            return True

    def release_external_publication(self, slot, token):
        with self.engine.begin() as conn:
            conn.execute(official_slots.update().where(
                *self._official_where(official_slots, slot), official_slots.c.publication_claim_token == token,
            ).values(publication_claim_token=None, publication_claim_until=None))

    def claim_post_publication_quote(self, slot, symbol, started_at):
        if symbol not in publication.SYMBOLS:
            raise ValueError("Unexpected official symbol")
        with self._publication_transaction() as conn:
            row = conn.execute(select(official_slots).where(
                *self._official_where(official_slots, slot)).with_for_update()).mappings().one()
            if (row["publication_status"] != "PUBLISHED" or row["published_at"] is None
                or started_at < row["published_at"]):
                return False
            quotes = json.loads(row["post_publish_quotes"] or "{}")
            if symbol in quotes:
                return False
            quotes[symbol] = dict(status="REQUESTING", quote_kind="LIVE_AFTER_PUBLISH",
                                  request_started_at=started_at, evidence_mode=row["publication_mode"],
                                  provider="Binance Spot / data-api.binance.vision", endpoint=protocol.ENDPOINT)
            conn.execute(official_slots.update().where(*self._official_where(official_slots, slot)).values(
                post_publish_quotes=json.dumps(quotes, sort_keys=True),
                post_publish_attempted=all(s in quotes for s in publication.SYMBOLS)))
            return True

    def finish_post_publication_quote(self, slot, symbol, received_at, *, data=None, error=None):
        fields = protocol.normalize_quote(data, symbol) if data is not None else {}
        with self._publication_transaction() as conn:
            row = conn.execute(select(official_slots).where(
                *self._official_where(official_slots, slot)).with_for_update()).mappings().one()
            quotes = json.loads(row["post_publish_quotes"] or "{}")
            quote = quotes.get(symbol)
            if not quote or quote["status"] != "REQUESTING":
                return False
            if received_at < quote["request_started_at"]:
                raise ValueError("Quote response precedes request")
            quote.update(fields, status="OK" if data is not None else "QUOTE_FAILED",
                         response_received_at=received_at, error=error)
            conn.execute(official_slots.update().where(*self._official_where(official_slots, slot)).values(
                post_publish_quotes=json.dumps(quotes, sort_keys=True)))
            return True

    def prepare_publication_slot(self, now):
        """Activate once, journal missing elapsed hours, then prepare current hour.

        Never infer a history before first activation; never replace missing slots.
        """
        if type(now) is not int or now < 0:
            raise ValueError("Expected UTC milliseconds")
        slot = now // publication.HOUR_MS * publication.HOUR_MS
        insert = sqlite_insert if self.engine.dialect.name == "sqlite" else pg_insert
        identity = dict(project_id=publication.PROJECT_ID,
                        official_stream_id=publication.STREAM_ID,
                        strategy_id=publication.STRATEGY_ID,
                        strategy_version=publication.STRATEGY_VERSION)
        with self._publication_transaction() as conn:
            conn.execute(insert(publication_streams).values(
                **identity, stream_status="OFFICIAL", first_slot_utc=slot,
                last_checked_slot_utc=slot,
            ).on_conflict_do_nothing(index_elements=["project_id", "official_stream_id"]))
            stream = conn.execute(select(publication_streams).where(
                *self._official_where(publication_streams)).with_for_update()).mappings().one()
            if (stream["stream_status"] != "OFFICIAL"
                or stream["strategy_id"] != publication.STRATEGY_ID
                or stream["strategy_version"] != publication.STRATEGY_VERSION):
                raise ValueError("Official stream identity cannot be replaced")
            if slot < stream["first_slot_utc"]:
                raise ValueError("Slot precedes publication journal activation")
            # The saved watermark bounds work across years of repeated ticks.
            for missed in range(stream["last_checked_slot_utc"], slot, publication.HOUR_MS):
                conn.execute(insert(official_slots).values(
                    **identity, slot_utc=missed, protocol_version=publication.VERSION,
                    status="MISSING_UNEXPLAINED", created_at=now,
                    publication_status="NOT_SUBMITTED",
                ).on_conflict_do_nothing(index_elements=["project_id", "official_stream_id", "slot_utc"]))
            conn.execute(official_slots.update().where(
                *self._official_where(official_slots), official_slots.c.status == "PREPARING",
                official_slots.c.batch_bytes.is_(None), official_slots.c.slot_utc < slot,
            ).values(status="ERROR", error="PREPARATION_NOT_FROZEN_BY_DEADLINE"))
            conn.execute(insert(official_slots).values(
                **identity, slot_utc=slot, protocol_version=publication.VERSION,
                status="PREPARING", created_at=now, publication_status="NOT_SUBMITTED",
            ).on_conflict_do_nothing(index_elements=["project_id", "official_stream_id", "slot_utc"]))
            conn.execute(publication_streams.update().where(
                *self._official_where(publication_streams),
                publication_streams.c.last_checked_slot_utc < slot,
            ).values(last_checked_slot_utc=slot))
            row = conn.execute(select(official_slots).where(
                *self._official_where(official_slots, slot))).mappings().one()
            return self._decode_publication(row)

    def freeze_publication_slot(self, slot, now, code_config, failures=None):
        """Freeze the first fixed BTC+ETH composition; retries return exact bytes."""
        if type(now) is not int or now < slot:
            raise ValueError("Cannot freeze before slot")
        failures = failures or {}
        if any(v.get("status") not in ("ERROR", "DATA_UNAVAILABLE") for v in failures.values()):
            raise ValueError("Invalid technical entry status")
        with self._publication_transaction() as conn:
            journal = conn.execute(select(official_slots).where(
                *self._official_where(official_slots, slot)).with_for_update()).mappings().one()
            if journal["batch_bytes"] is not None or journal["status"] != "PREPARING":
                return self._decode_publication(journal)
            c = strategy_forecasts.c
            rows = conn.execute(select(strategy_forecasts).where(
                c.input_close_at == slot, c.strategy_id == journal["strategy_id"],
                c.strategy_version == journal["strategy_version"],
                c.evaluation_version == publication.VERSION,
                c.symbol.in_(publication.SYMBOLS),
            )).all()
            payload = publication.build_batch(slot, [self._decode_strategy(r) for r in rows],
                                              failures, code_config)
            raw = publication.canonical_bytes(payload)
            batch_id, digest = publication.batch_identity(raw)
            statuses = [e["status"] for e in payload["entries"]]
            status = ("LATE" if now >= slot + publication.DEADLINE_MS else
                      "ERROR" if "ERROR" in statuses else
                      "DATA_UNAVAILABLE" if all(s != "OK" for s in statuses) else "BATCH_FROZEN")
            problems = {e["symbol"]: e.get("reason") for e in payload["entries"] if e["status"] != "OK"}
            error = json.dumps(problems, sort_keys=True) if problems else None
            if status == "LATE":
                error = "BATCH_FROZEN_AFTER_DEADLINE" + (": " + error if error else "")
            conn.execute(official_slots.update().where(*self._official_where(official_slots, slot)).values(
                status=status, batch_id=batch_id, batch_hash=digest, batch_bytes=raw,
                frozen_at=now, publication_status="BATCH_FROZEN", error=error,
            ))
            return self._decode_publication(conn.execute(select(official_slots).where(
                *self._official_where(official_slots, slot))).mappings().one())

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
        transaction = (self._publication_transaction if any(
            v["evaluation_version"] == publication.VERSION for v in prepared) else self.engine.begin)
        with transaction() as conn:
            closed_slots = set()
            for slot in sorted({v["input_close_at"] for v in prepared
                                if v["evaluation_version"] == publication.VERSION}):
                journal = conn.execute(select(official_slots).where(
                    *self._official_where(official_slots, slot)).with_for_update()).mappings().first()
                if journal is None:
                    raise ValueError("Prepare the official slot before recording v1.1 forecasts")
                if journal["status"] != "PREPARING" or journal["batch_bytes"] is not None:
                    closed_slots.add(slot)
            for values in prepared:
                if values["evaluation_version"] == publication.VERSION:
                    if (values["strategy_id"] != publication.STRATEGY_ID
                        or values["strategy_version"] != publication.STRATEGY_VERSION
                        or values["symbol"] not in publication.SYMBOLS):
                        raise ValueError("Forecast does not belong to the official stream")
                    if values["input_close_at"] in closed_slots:
                        existing = conn.execute(select(strategy_forecasts).where(*[
                            strategy_forecasts.c[k] == values[k] for k in STRATEGY_FORECAST_KEY
                        ])).mappings().first()
                        if existing is None or any(existing[k] != v for k, v in values.items()):
                            raise ValueError("Official slot is closed; frozen composition cannot change")
                        continue
                forecast_id = conn.execute(
                    insert(strategy_forecasts).values(**values)
                    .on_conflict_do_nothing(index_elements=list(STRATEGY_FORECAST_KEY))
                    .returning(strategy_forecasts.c.id)
                ).scalar_one_or_none()
                if forecast_id is not None:
                    inserted += 1
                    if values["evaluation_version"] in protocol.VERSIONS:
                        conn.execute(insert(publish_observations).values(
                            **{k: values[k] for k in PUBLISH_KEY}, status="PENDING",
                            provider="Binance Spot / data-api.binance.vision",
                            endpoint=protocol.ENDPOINT,
                            quote_kind=("POST_RECORD_QUOTE" if values["evaluation_version"] == publication.VERSION
                                        else None),
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
            if row["evaluation_version"] in protocol.VERSIONS:
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
                if row["evaluation_version"] == publication.VERSION:
                    protocol_values["live_status"] = "NOT_PUBLISHED_EXTERNALLY"
                    journal = conn.execute(select(official_slots).where(
                        *self._official_where(official_slots, row["input_close_at"]))).mappings().first()
                    if journal and journal["publication_status"] == "PUBLISHED" and journal["published_at"] is not None:
                        protocol_values.update(live_status="UNAVAILABLE",
                                               live_evidence_mode=journal["publication_mode"])
                        post_quote = json.loads(journal["post_publish_quotes"] or "{}").get(row["symbol"], {})
                        if (post_quote.get("status") == "OK"
                            and post_quote.get("quote_kind") == "LIVE_AFTER_PUBLISH"
                            and post_quote.get("request_started_at", -1) >= journal["published_at"]
                            and post_quote.get("response_received_at", row["due_at"]) < row["due_at"]):
                            live = calculate(float(post_quote["mid_price"]))
                            protocol_values.update(live_status=timing, live_change_pct=live["change_pct"],
                                live_observed_direction=live["observed_direction"], live_correct=live["correct"])
                quote = conn.execute(select(publish_observations).where(*[
                    publish_observations.c[k] == row[k] for k in PUBLISH_KEY
                ])).mappings().first()
                if (row["evaluation_version"] == protocol.VERSION
                    and quote and quote["status"] == "OK"):
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

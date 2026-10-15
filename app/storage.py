import json
import os
from pathlib import Path
from sqlalchemy import (
    create_engine, MetaData, Table, Column, Integer, BigInteger,
    String, Float, Text, Boolean, UniqueConstraint,
    select, func, case, and_
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url

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
        metadata.create_all(self.engine)

    def close(self):
        self.engine.dispose()

    def ready(self):
        with self.engine.connect() as conn:
            conn.execute(select(1))

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

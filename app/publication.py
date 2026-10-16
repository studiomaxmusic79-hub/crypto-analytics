"""Live Publication v1.1: fixed stream identity and canonical batch format.

No external transport or credentials. A DB record is not public publication.
"""
import hashlib
import json
import math
from datetime import datetime, timezone

from . import evaluation_protocol as evaluation
from .market_inputs import decimal_text

PROJECT_ID = "krypta-max"
STREAM_ID = "krypta-mtf-hourly"
STRATEGY_ID = "krypta_mtf"
STRATEGY_VERSION = "v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
HOUR_MS = 3_600_000
DEADLINE_MS = 300_000
VERSION = evaluation.PUBLICATION_VERSION
SCHEMA = "krypta-official-batch-v1"
SLOT_STATUSES = ("PREPARING", "BATCH_FROZEN", "PUBLISHED", "LATE",
                 "DATA_UNAVAILABLE", "ERROR", "MISSING_UNEXPLAINED")
PUBLICATION_STATES = ("NOT_SUBMITTED", "BATCH_FROZEN", "EXTERNAL_SUBMISSION",
                      "WITNESS_ACCEPTED", "PUBLIC_READ_CONFIRMED", "PUBLISHED")

# Evaluation fields are deliberately excluded: settlement must never affect bytes.
FORECAST_FIELDS = (
    "id", "symbol", "strategy_id", "strategy_version", "input_close_at",
    "horizon", "issued_at", "recorded_at", "base_price", "due_at", "status",
    "direction", "score", "reason_codes", "indicators", "evaluation_version",
    "neutral_pct", "input_data_cutoff_id", "input_hash",
)


def canonical_bytes(value):
    """Sorted UTF-8 JSON; finite floats are normalized decimal strings.

    Integers, booleans, null and list order retain their JSON types/ordering.
    UTC datetimes use ISO-8601 with six fractional digits and Z. No whitespace.
    """
    def normalize(item):
        if isinstance(item, datetime):
            if item.tzinfo is None:
                item = item.replace(tzinfo=timezone.utc)
            return item.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("Non-finite batch value")
            return decimal_text(item)
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ValueError("Batch keys must be strings")
            return {k: normalize(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        return item
    return json.dumps(normalize(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def build_batch(slot, rows, failures, code_config):
    entries = []
    for symbol in SYMBOLS:
        forecasts = sorted((r for r in rows if r["symbol"] == symbol), key=lambda r: r["horizon"])
        complete = (len(forecasts) == 2 and {r["horizon"] for r in forecasts} == {1, 4}
                    and all(r["status"] == "OK" for r in forecasts)
                    and len({r["direction"] for r in forecasts}) == 1)
        entry = dict(symbol=symbol, status="OK" if complete else "DATA_UNAVAILABLE",
                     signal=forecasts[0]["direction"] if complete else None,
                     forecast_ids=[r["id"] for r in forecasts],
                     forecasts=[{k: r[k] for k in FORECAST_FIELDS} for r in forecasts])
        if not complete:
            entry.update(failures.get(symbol, dict(status="DATA_UNAVAILABLE",
                                                   reason="NO_COMPLETE_RECORDED_FORECAST_PAIR")))
        entries.append(entry)
    return dict(schema=SCHEMA, protocol_version=VERSION, project_id=PROJECT_ID,
                official_stream_id=STREAM_ID, stream_status="OFFICIAL", slot_utc=slot,
                deadline_at=slot+DEADLINE_MS,
                strategy_id=STRATEGY_ID, strategy_version=STRATEGY_VERSION,
                code_config_identity=code_config,
                evaluation_protocol_identity=VERSION, entries=entries)


def batch_identity(raw):
    digest = hashlib.sha256(raw).hexdigest()
    return "krypta-batch-" + digest, digest

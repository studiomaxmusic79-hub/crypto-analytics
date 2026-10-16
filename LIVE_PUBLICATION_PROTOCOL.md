# Live Publication Protocol v1.1 groundwork

New evaluation identity: `krypta-live-eval-v1.1`. Historical
`krypta-live-eval-v1` remains supported with its original post-commit quote
evaluation. Its records, observations and results are not relabelled or rewritten.
The historical description is in `EVALUATION_PROTOCOL.md`.

## Scope and time semantics

This phase records decisions and freezes local batches. It performs no external
publication, witness submission or public-read confirmation. It introduces no
credentials, dependencies, AWS/S3, Hedera/HCS, TSA or frontend changes.

- The existing forecast `issued_at` is reused as the logical calculation/issue
  time before the DB write. It is not an external publication timestamp, nor a
  separate measurement of the exact instant strategy arithmetic completed.
- Forecast `recorded_at` is the DB timestamp already stored by the existing
  forecast subsystem. A successful transaction means recorded, not published.
- Journal `created_at` and `frozen_at` are local UTC millisecond operation times;
  journal/stream `recorded_at` is DB time. They are not publication timestamps.
- Future `witness_accepted_at`, `public_read_confirmed_at` and `published_at`
  belong to the batch's journal row, use UTC milliseconds and remain null.
  There is no code in this phase that sets them or marks a batch PUBLISHED.

New v1.1 quotes use the existing one-attempt, post-commit bookTicker collection
and are explicitly labelled `POST_RECORD_QUOTE` in `quote_kind`. They are only
diagnostics. Old quote_kind values stay null; their original meaning is determined
by the historical forecast version. No old quote becomes v1.1 publication evidence.

REFERENCE_SLOT still uses the existing evaluator, frozen neutral_pct and exact
calendar due_at. The existing first-confirmed target observation and inclusive
five-minute target-data rule are unchanged. For v1.1, live_status is
`NOT_PUBLISHED_EXTERNALLY` after settlement, with null live return/direction/correct,
even if a POST_RECORD_QUOTE exists. Before settlement live result fields remain
null. A future implementation must collect a distinct real publication quote with
request_started_at >= published_at; it must not reuse POST_RECORD_QUOTE.

## Storage and official identity

`publication_streams` stores `(project_id, official_stream_id)` as its primary
key, stream_status (`OFFICIAL` or `EXPERIMENTAL`), strategy_id/version,
first_slot_utc, last_checked_slot_utc and recorded_at. A partial unique index
allows one OFFICIAL stream per project; multiple experimental streams may exist.
The current stream is `krypta-max / krypta-mtf-hourly`, strategy `krypta_mtf / v1`.
The stream identifier does not encode a strategy version. No activation/deactivation
workflow or alternative official history is implemented.

`official_slots` references that stream and has primary key
`(project_id, official_stream_id, slot_utc)`. Slots are UTC hour boundaries, with
BTCUSDT and ETHUSDT expected every hour. Its fields are:

- protocol_version, strategy_id/version, status, created_at, recorded_at, error;
- batch_id, batch_hash, exact batch_bytes (BLOB/BYTEA), frozen_at;
- publication_status;
- nullable external_object_key, external_object_version, witness_id,
  witness_sequence, witness_accepted_at, public_read_confirmed_at, published_at.

The only added column on an existing table is
`strategy_publish_observations.quote_kind`; migration leaves all old rows null.
Forecast rows are reused, not duplicated into a new forecast subsystem.

## State machine and recovery

The first Krypta-enabled cycle with a successful market clock sample activates
the journal at its current slot. No pre-activation expected history is fabricated.
Every cycle first journals elapsed hours since the saved watermark, then prepares
its current slot, using uniqueness/idempotency across cron/manual jobs.

The local flow is `PREPARING → freeze`. Freeze atomically fixes the BTC+ETH
composition from existing v1.1 forecast pairs and assigns the slot status:

- `BATCH_FROZEN`: at least one complete pair, no ERROR, before the deadline;
- `DATA_UNAVAILABLE`: both entries lack complete pairs for data reasons;
- `ERROR`: at least one entry records an actual calculation/write error;
- `LATE`: freezing at or after slot + 300000 ms; this takes precedence over
  other aggregate statuses and preserves per-entry reasons in the batch.

WAIT remains an OK forecast decision. A missing symbol has signal=null,
technical status DATA_UNAVAILABLE or ERROR, its actual reason if known, and no
invented forecast IDs. Even a fully technical slot gets a fixed two-entry batch.
If older v1 forecasts already occupy the slot, they remain historical v1 records
and are not adopted as v1.1 batch forecasts.

Any previous expected hour without a journal row becomes `MISSING_UNEXPLAINED`,
with null reason, no batch and no invented forecasts. A previous PREPARING row
that was never frozen becomes ERROR with the factual marker
`PREPARATION_NOT_FROZEN_BY_DEADLINE`; no crash/outage cause is inferred.
These terminal journal rows cannot later be repurposed by stale manual ticks.

For every locally frozen batch, publication_status becomes BATCH_FROZEN even
when the aggregate slot status is ERROR, DATA_UNAVAILABLE or LATE. The reserved
future publication flow is:

`BATCH_FROZEN → EXTERNAL_SUBMISSION → WITNESS_ACCEPTED → PUBLIC_READ_CONFIRMED → PUBLISHED`

Only NOT_SUBMITTED and BATCH_FROZEN are assigned by the current code. Future
states and nullable identifiers are schema placeholders, not functioning delivery
adapters. A local freeze never sets published_at or witness timestamps.

## Exact canonical batch format

Top-level keys: schema (`krypta-official-batch-v1`), protocol_version, project_id,
official_stream_id, stream_status, slot_utc, deadline_at, strategy_id,
strategy_version, code_config_identity, evaluation_protocol_identity, entries.
Code/config identity reuses existing strategy provenance: Git commit, dirty-tree
indicator, input schema/hash algorithm and fixed strategy parameters. This remains
internal provenance; external proof is a separate future phase.

Entries are always ordered BTCUSDT, ETHUSDT. Each entry has symbol, status,
signal, forecast_ids, forecasts, and reason when technically unavailable/failed.
Forecasts are ordered by horizon 1, 4. Each snapshot has exactly:

`id, symbol, strategy_id, strategy_version, input_close_at, horizon, issued_at,
recorded_at, base_price, due_at, status, direction, score, reason_codes,
indicators, evaluation_version, neutral_pct, input_data_cutoff_id, input_hash`.

Indicators preserve the existing factors, reasons, confirmations and input
provenance. Mutable settlement fields and quote diagnostics are excluded.

Serialization is UTF-8 JSON without BOM/newline, sorted string keys, compact
separators, ensure_ascii=false and allow_nan=false. Finite floats become normalized
plain decimal strings using the existing decimal_text; integers, booleans and null
retain their types. List order is fixed as above. Datetimes are UTC ISO-8601 with
six fractional digits and Z. SHA-256 is computed over those exact stored bytes;
batch_id is `krypta-batch-` plus the full lowercase hexadecimal digest.

Freeze, v1.1 recording and journal preparation use short DB-only transactions.
PostgreSQL locks stream/slot rows; SQLite uses BEGIN IMMEDIATE. They do not expand
the candle advisory lock and contain no HTTP. A v1.1 forecast writer locks the
same slot as the freezer, so it cannot append ETH after a partial batch is frozen.
Exact forecast retries remain no-ops; changed/additional forecasts are rejected.
Repeated ticks skip Strategy for closed slots and reuse the stored batch bytes,
ID and hash, including after restart or settlement. Future delivery must read
those bytes rather than regenerate the payload.

Read a journal/batch with `Store.publication_slot(slot_utc)` and the diagnostics
with the existing `Store.publish_observation(...)` method. No public API or
frontend publication claim is added in this groundwork.

## Validation — 16 October 2026

- 20 new publication test cases passed, including concurrent freeze/record,
  immutable retries, partial batches, WAIT, missing hours, legacy migration,
  OFFICIAL identity and the POST_RECORD_QUOTE/LIVE separation.
- Full suite: 504 passed, 1 skipped, 59 subtests passed (46.50 seconds).
  The optional disposable PostgreSQL test is skipped; this is not a live
  PostgreSQL deployment check. Existing Starlette/HTTPX deprecation warning.
- Existing LIVE integration retry expectations now assert the v1.1 rule:
  missing/failed entries cannot be filled after the batch freezes.
- Point-in-time and historical v1 evaluation tests pass. Strategy v1, cron,
  frontend and both untracked backtest files are unchanged.
- `git diff --check` passed. No commit, push, deployment or external publication.

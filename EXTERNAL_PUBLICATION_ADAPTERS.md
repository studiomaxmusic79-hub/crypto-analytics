# External Publication Adapter Layer

This phase adds provider-neutral orchestration and deterministic in-memory mocks.
No production archive/witness/HTTP adapter, SDK, account, paid resource or real
credential is configured. Strategy, calendar targets, cron and frontend are unchanged.

## Configuration and evidence boundary

The only active setting is `EXTERNAL_PUBLICATION_MODE`:

- `disabled` (default): no publication adapter calls or post-publication quotes;
  batches remain BATCH_FROZEN, published_at stays null, reference evaluation
  continues and v1.1 LIVE remains NOT_PUBLISHED_EXTERNALLY.
- `mock`: local providers execute the whole workflow. The slot persists
  publication_mode=mock, and any calculated LIVE result has
  live_evidence_mode=mock. These timestamps/receipts are synthetic test evidence,
  not proof that a signal was publicly available. Future official metrics must
  exclude mock evidence.
- `external`: reserved, explicitly rejected as not implemented. No silent
  fallback or production provider initialization occurs.

Mocks are process-local. Tests can inject the same provider instances into a
restarted service to model an independent provider retaining accepted records.
A new process's fresh mocks have no remote history; they are not durable cloud
services. Durable receipts in the database remain intact. Evidence mode cannot
be reinterpreted by a later publication claim.

## Contracts

`PublicArchive.store_once(batch_id, exact_batch_bytes, batch_hash, slot, stream)`
returns ArchiveReceipt: provider, object_key, optional immutable version,
public_reference, stored_hash, stored_at and status. `check(batch_id, batch_hash)`
reconciles uncertain submission. A provider must enforce immutable idempotency:
the same logical object with the same bytes/hash returns its original receipt;
different bytes/hash must return CONFLICT, never replace the object.

`IndependentWitness.register(project_id, official_stream_id, slot_utc, batch_hash,
archive_reference)` returns WitnessReceipt: provider, record_id and/or sequence,
accepted_at, submitted_hash and status. Optional first_hash/first_sequence expose
conflict ordering. `check(project_id, official_stream_id, slot_utc, batch_hash)`
reconciles uncertainty. Duplicates return the existing receipt; another hash for
an already accepted stream/slot must be visible as CONFLICT.

`PublicReader.read(public_reference)` returns PublicReadReceipt with provider,
reference, returned bytes, local received_at and status. Core verifies exact byte
equality and SHA-256 itself, not just the provider's success flag.

Successful statuses are STORED, ACCEPTED and OK respectively. ERROR means a
definitive failure; UNKNOWN means acceptance is uncertain. A reconciliation
NOT_FOUND must mean submission is definitively absent, not merely not yet visible.
Pending/delayed acceptance must remain UNKNOWN. Adapters must bound their own I/O
and raise TimeoutError or return UNKNOWN on uncertain acceptance.

## State machine, exact bytes and retries

`BATCH_FROZEN → EXTERNAL_SUBMISSION → WITNESS_ACCEPTED → PUBLIC_READ_CONFIRMED → PUBLISHED`

Core reads batch_bytes, batch_id and batch_hash directly from official_slots.
It never calls the batch builder, JSON serializer for the batch or Strategy.
SHA-256 of the exact stored bytes is checked before any provider call and again
before final publication. A mismatch records ERROR/HASH_MISMATCH, blocks provider
delivery and never sets published_at. Frozen bytes/hash/ID are never updated.

Archive storage completes first, then witness registration, then public reading.
Each successful receipt is committed before the next step. Public reading must
return the same frozen bytes, matching reference and a valid local receive time.
PUBLIC_READ_CONFIRMED is committed separately before the final PUBLISHED update.
Finalization validates the receipts/hashes and read time again and sets
published_at to local UTC milliseconds only after confirmation.

Each attempt takes a short DB row claim with a 120-second lease and token.
The IN_PROGRESS step marker is committed before I/O. No network await occurs
inside a DB transaction or candle advisory lock. Progress renews the lease;
only its owner can update progress. An expired/abandoned IN_PROGRESS operation
is reconciled as uncertain, rather than blindly resubmitted. Provider idempotency
is still required when a slow request outlives its lease.

- Archive/witness definitive failures persist FAILED and retry the same identity,
  bytes and hash. Completed earlier steps are reused.
- Timeout, UNKNOWN and unexpected adapter/transport errors persist UNKNOWN.
  Archive/witness check runs first on the next attempt. UNKNOWN or a failed check
  keeps uncertainty; only definitive NOT_FOUND permits resubmission of the same
  payload. Delayed acceptance never creates a replacement batch.
- Read failure retries only the read. A restart after read confirmation resumes
  finalization without repeating archive, witness or read operations.
- Conflicts and hash mismatch are terminal for automatic delivery. Their status,
  reason and available conflicting receipt are retained. No alternate official
  hash is generated to bypass the conflict.
- PUBLISHED is idempotent: archive, witness and public read are not repeated.
  The service can still claim missing post-publication quote attempts.

Unfinished batches are resumed by Krypta-enabled cycles, including older slots.
The bounded scan processes up to 100 eligible slots per cycle. Existing slot
quality statuses such as LATE, DATA_UNAVAILABLE or ERROR remain visible; a
successful external workflow does not rewrite them. A normal BATCH_FROZEN slot
becomes PUBLISHED. publication_status records publication progress independently.

## Storage and post-publication quote

No new tables. official_slots gains JSON-text archive_receipt, witness_receipt,
public_read_receipt, plus external_step, external_step_status,
last_publication_attempt_at, publication_error, publication_mode,
publication_claim_token and publication_claim_until. Existing neutral object,
witness, read-confirmation and published_at fields are reused. Public read bytes
are verified but not duplicated in receipts; the receipt stores the read hash.
Migration adds nullable fields without fabricating past receipts/timestamps.

Distinct post_publish_quotes JSON stores one best-bid/ask attempt per batch
symbol. post_publish_attempted means both symbol attempts have been claimed,
not that both succeeded. These are separate from existing POST_RECORD_QUOTE rows.
Each claim requires PUBLISHED and request_started_at >= published_at, and commits
before the existing Binance bookTicker I/O. The first valid response stores the
exact-decimal midpoint and metadata. Failure is QUOTE_FAILED; a crashed request
remains REQUESTING. Neither is replaced by a later attempt.

v1.1 evaluation uses only a successful LIVE_AFTER_PUBLISH quote from that journal
with a valid post-publication request time. A diagnostic POST_RECORD_QUOTE cannot
be substituted. After publication without a usable quote, LIVE is UNAVAILABLE.
The quote response must also precede that horizon's calendar due_at. Retrying
publication of an old batch cannot score backwards from a later quote to an
earlier target. The quote remains visible as an observation even when ineligible.
Before publication it remains NOT_PUBLISHED_EXTERNALLY. REFERENCE_SLOT, target
selection, neutral_pct and WAIT/FLAT semantics are unchanged. Already completed
evaluations remain immutable even if publication happens later. The nullable
strategy_forecasts.live_evidence_mode marks mock results explicitly; historical
v1 results retain their original path and no invented evidence mode.

## Mock scenarios and future secrets

MockArchive supports immutable duplicates, failure, timeout, acceptance followed
by timeout and delayed acceptance. MockWitness adds sequence ordering and visible
conflicting hashes; pending requests are reconciled before re-registration.
MockPublicReader supports failure, timeout and corrupt bytes. Test clocks control
timestamps deterministically. All mock operations are in memory with no HTTP.

Reserved future environment names only (currently not read): ARCHIVE_PROVIDER,
ARCHIVE_PUBLIC_BASE_URL, ARCHIVE_BUCKET_NAME, ARCHIVE_REGION,
ARCHIVE_CREDENTIALS_FILE, WITNESS_PROVIDER, WITNESS_NETWORK, WITNESS_TOPIC_ID,
WITNESS_OPERATOR_ID, WITNESS_PRIVATE_KEY_FILE. No values, account IDs, private keys
or credential JSON are supplied. Future adapters should load secrets outside the
repository and expose only neutral receipts to core.

.gitignore excludes .env variants, local credential/secret directories, .aws,
.hedera, credential JSON and common private-key files. Representative paths were
checked with git check-ignore; no actual secret files were created or read.

## Validation — 16 October 2026

- 30 new adapter test cases pass: exact frozen bytes, receipt/hash validation,
  archive/witness/read failures, UNKNOWN reconciliation, delayed acceptance,
  conflicts and ordering, lease recovery/concurrency, publication/quote ordering,
  disabled/mock configuration, old-schema migration and late quote eligibility.
- Full suite: 534 passed, 1 skipped, 59 subtests passed (52.02 seconds).
  The optional disposable PostgreSQL test is skipped; this run does not validate
  a live PostgreSQL deployment. Existing Starlette/HTTPX deprecation warning.
- `git diff --check` passed. Both untracked backtest files retain their original
  SHA-256 hashes. No large backtest, real publication, commit or push was run.

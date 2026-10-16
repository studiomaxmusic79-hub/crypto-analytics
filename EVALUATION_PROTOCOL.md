# Krypta LIVE Evaluation Protocol v1

Version: `krypta-live-eval-v1`. Only newly published Krypta Strategy v1
forecasts use this protocol. Strategy decisions, thresholds, WAIT/FLAT semantics,
calendar targets, cron schedule and frontend are unchanged.

## Publication observation

The existing atomic forecast write commits both horizons and one
`strategy_publish_observations` row in `PENDING`. Its primary key is
`(symbol, input_close_at, strategy_id, strategy_version)`; both horizons share it.
After publication commit, the publisher atomically claims `PENDING → REQUESTING`
and commits that claim. Only the claim winner makes one uncached Binance Spot
`/api/v3/ticker/bookTicker` request through the existing market client.
There is no HTTP inside a DB transaction or advisory lock.

The fixed rule is **one attempt per release**. The first and only valid response
becomes `OK`. Bid/ask prices and quantities must be finite decimal numbers,
prices/quantities positive, ask >= bid, and the returned symbol must match.
The midpoint `(bid + ask) / 2` is stored as an exact decimal string alongside
the bid/ask and quantities, provider, endpoint and local UTC millisecond request
and response timestamps. Request start denotes the logical attempt after
publication, including claim/client queue time. A midpoint is a market reference,
not an execution price or promise of a fill.

Invalid responses, cooldown and HTTP errors become `QUOTE_FAILED` with an error.
No retry selects a later quote. A crash before claiming leaves `PENDING`; a
crash after claiming leaves `REQUESTING`. These are incomplete observations,
never usable prices. Duplicate cron/manual ticks do not restart the request.
The forecast survives quote failure and reference evaluation still works.

## Target and evaluation

For each due_at, select the smallest non-null observation_id across existing
`market_candles` and `market_candle_revisions` with symbol matching, interval
`1h`, time `due_at - 3600000`, close_time `due_at - 1`, confirmed_at >= due_at
and non-null observed_at. This is the first accepted confirmed version of the
exact target, not the latest corrected version. If absent, the existing evaluator
fetches the exact target and stores it through normal observation versioning.
No alternate target candle is substituted.

The storage resolver selects the version again in its evaluation transaction and
calculates both returns with the existing `evaluate_result` function and frozen
neutral_pct. It saves the exact target_observation_id and target_observed_at.
Only the first completed evaluation can update a forecast; later candle
corrections and repeated resolution calls cannot overwrite either result.

| Series | Base | Target | Stored result |
|---|---|---|---|
| REFERENCE_SLOT | Existing hourly Close HH:00 | Existing due_at = slot + horizon | Existing actual_price/change_pct/observed_direction/correct |
| LIVE_AFTER_PUBLISH | Saved post-publish midpoint | Same due_at | live_change_pct/live_observed_direction/live_correct |

Returns use `(actual / base - 1) * 100` through the existing implementation.
UP is change > neutral_pct; DOWN is change < -neutral_pct; equality is FLAT.
WAIT always has correct=null. UP/DOWN against FLAT is incorrect. Each horizon
has its own frozen neutral_pct and result. There is no combined accuracy metric.

`result_status` is `OK` if the first eligible target's DB acceptance time
observed_at <= due_at + 300000 milliseconds (inclusive), otherwise
`LATE_TARGET_DATA`. Evaluation time and last confirmation time do not move this
deadline. Late results retain their values but must be excluded from any future
metric claiming timely target observation. If a valid publish quote exists,
live_status carries the same timing status; otherwise it is `UNAVAILABLE`, with
null live result fields. Before completed evaluation these fields remain null.

## Honest history boundary and retrieval

`market_data_state.evaluation_protocol_started_at` is set once using DB time
when this schema first initializes on that database. This is the tracking
availability boundary; the per-forecast evaluation_version identifies actual
adoption. Deployment has not been performed by this change. Existing forecasts
retain their original version and evaluation path; no old quotes, timestamps or
target observation IDs are fabricated, including for old pending forecasts.

`Store.strategy_history()` returns reference and live results, frozen protocol
version, threshold and target provenance. `Store.publish_observation(symbol,
input_close_at, strategy_id, strategy_version)` returns the release quote and
request status. The shared release key joins these records without duplicating
forecasts. No external timestamp/witness or canonical restatement is included.

## Validation (16 October 2026)

- New protocol tests: 25 passed.
- Full suite: 484 passed, 1 skipped, 59 subtests passed (42.03 seconds).
- The optional disposable PostgreSQL test is skipped; this run does not confirm
  a live PostgreSQL deployment. One existing Starlette/HTTPX deprecation warning.
- `git diff --check`: passed. No production backtest, commit, push or deployment.

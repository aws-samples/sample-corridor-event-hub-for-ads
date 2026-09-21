# Lambda Functions and the Formulas They Apply

What each function in the pipeline does, what triggers it, and — where a function
computes a number rather than moving data — the exact arithmetic behind that number.

Six functions. Five are the pipeline; one is an operator command.

```
EventBridge Scheduler ──► collector ──► S3 raw zone
                                 │
                        RawPayloadStored (EventBridge)
                                 │
                                 ▼
                            normalizer ──► CandidateEventProduced
                                 │
                                 ▼
                             resolver ──► DynamoDB event store ──► EventResolved
                                 │                  ▲
                          arms one Step Functions   │
                          execution per event       │
                                 ▼                  │
                             lifecycle ─────────────┘  (TTL ticks)

API Gateway (SigV4) ──► query ──► reads the event store

operator / CI ──► db_migrate ──► Aurora PostgreSQL (schema only)
```

The scoring logic is deliberately NOT in the handlers. Every formula in section 7
lives in `corridor_event_hub/core/`, which touches no AWS API and no clock it was not
handed — that is what makes the interesting cases testable without an account. The
handlers are I/O.

---

## 1. collector

[`corridor_event_hub/handlers/collector.py`](../corridor_event_hub/handlers/collector.py)

**Job:** fetch one source feed, store the raw bytes immutably, announce them. It
does not parse. Raw payloads are persisted BEFORE any transformation,
so every normalized record links back to the exact bytes it came from and replay
never has to contact a source again.

| | |
|---|---|
| Trigger | EventBridge Scheduler, one schedule per `verified_live` source, payload `{"sourceId": "..."}` |
| Cadence | the source's `publishCadenceSeconds` from the catalog, floored at 60s — Oklahoma 60s, NWS / AWS Location / TxDOT / AZ511 300s, NM WeatherShare 600s |
| Timeout / memory | 60s / 512 MB |
| Writes | S3 raw zone (`raw/source=…/year=…/month=…/day=…/hour=…/{iso}-{sha256[:12]}.json`), DynamoDB catalog table (health), EventBridge `RawPayloadStored` |
| Env | `RAW_BUCKET`, `EVENT_BUS`, `CATALOG_TABLE` |

**Steps**

1. Look the `sourceId` up in the catalog; an unknown id raises.
2. Build the URL and headers. Credentials by `authMethod`: `api_key_secret`
   (Secrets Manager, cached per container — the **only** credentialed method since
   the `query_token_public` exception was withdrawn; the query parameter is
   catalog data via `authQueryParam`, `key` by default and `access_token` for
   Oklahoma), `aws_sigv4` (the function's own execution role, no key to rotate),
   `none`, `none_user_agent_required`. A `User-Agent` goes on every request —
   `urllib`'s default is rejected outright by Cloudflare-fronted agency endpoints.
   The scheme is asserted `https` here, at the one place a catalog value becomes a
   URL with a credential appended to it.
3. GET, with a 45s fetch timeout. `aws_sigv4` sources are tiled: N signed
   `geo-maps:GetTile` requests derived from the corridor geometry, wrapped in one
   JSON envelope so the rest of the handler treats them like any other source.
4. Record source health regardless of outcome — a failed fetch is data,
   not just an error.
5. On success: SHA-256 the body, `put_object`, then `PutEvents`.

**Formulas:** none. Two derived values:

- `latency_ms` = finish − start of the fetch, milliseconds.
- `checksum` = SHA-256 of the UTF-8 body. This makes a duplicate *identifiable*;
  it does not deduplicate. The S3 key embeds the fetch timestamp, so identical
  bytes fetched a minute apart are two objects and two events. `unchanged` in the
  return value is a placeholder, still hardcoded `False` — do not describe this as
  fetch idempotency until a last-checksum read is added before the put.

**Failure behaviour:** a non-200 or an empty body is returned, never raised. One
feed being down must not look like a system failure or stop the other sources.
Health recording is itself wrapped — it can never break a collection.

---

## 2. normalizer

[`corridor_event_hub/handlers/normalizer.py`](../corridor_event_hub/handlers/normalizer.py)

**Job:** run the right adapter over a stored raw payload and emit candidate events
plus whatever could not be mapped. It reads bytes from S3 and never re-fetches the
source — that is what makes replay work.

| | |
|---|---|
| Trigger | EventBridge rule on `corridor-event-hub.collector` / `RawPayloadStored` |
| Timeout / memory | 120s / 1024 MB (polygon conflation is the slow path; memory buys CPU) |
| Reads | S3 raw zone, Aurora (corridor geometry) |
| Emits | `CandidateEventProduced` (one per candidate, batched 10 per `PutEvents`), `MappingIssuesFound`, `PayloadQuarantined` |
| Env | `RAW_BUCKET`, `EVENT_BUS`, `EVENT_TABLE`, `CEH_ROUTE`, `SPATIAL_DB_SECRET_ARN` |
| Failure path | Lambda destination → `normalizer-dlq`, `retryAttempts: 2`; rule-level DLQ → `rule-dlq` |

**Steps**

1. Select the adapter by `sourceId`. No adapter registered → `PayloadQuarantined`
   and return; never a silent drop.
2. Read the payload from S3 and parse it through the adapter, handing it a
   conflator. With `SPATIAL_DB_SECRET_ARN` set, the corridor comes from Postgres
   and polygon conflation runs there while per-record point conflation stays in
   process; without it, everything is in process from a JSON corridor (this is
   what keeps `npm run probe`, the UI API and the test suite running with no AWS
   account).
3. Score each candidate provisionally (section 7.1) so scoring stays in the
   real-time path rather than a batch job.
4. Emit candidates; emit mapping issues separately (first 100, with a total count).

**Formulas it applies:** the confidence model (7.1) only, and with `sources` set to
just the one that reported the record — so corroboration always scores as
single-source here. The resolver re-scores after matching. Both scores are
legitimate: this one answers *how much do we trust this report*, the resolver's
answers *how much do we trust this event*.

**Does NOT:** dedup, decide lifecycle state, or derive severity/duration. Keeping
that line clean is what keeps the architecture portable.

---

## 3. resolver

[`corridor_event_hub/handlers/resolver.py`](../corridor_event_hub/handlers/resolver.py) —
policy in [`corridor_event_hub/core/resolution.py`](../corridor_event_hub/core/resolution.py)

**Job:** candidates in, versioned events out. Match, merge, apply field precedence,
walk the lifecycle chain, write the store, arm the TTL timer.

| | |
|---|---|
| Trigger | EventBridge rule on `corridor-event-hub.normalizer` / `CandidateEventProduced`, ONE candidate per invocation |
| Timeout / memory | 60s / 512 MB (the time is DynamoDB round trips, not computation) |
| Concurrency | `reservedConcurrentExecutions: 10` — capped so same-event contention on one DynamoDB item stays rare, and so a replay storm cannot absorb the table at full speed |
| Writes | DynamoDB event store (versions, audit records, source pointers, review queue), Step Functions `StartExecution`, EventBridge `EventResolved` / `MatchReviewQueued` |
| Env | `EVENT_BUS`, `EVENT_TABLE`, `CEH_ROUTE`, `LIFECYCLE_STATE_MACHINE_ARN` |
| Failure path | Lambda destination → `resolver-dlq`; rule DLQ → `resolver-rule-dlq` |

**Steps**

1. Fingerprint the candidate (see below) and look up the per-source-record pointer
   `(sourceId, nativeId) -> eventId`.
2. Pointer + event found → `resolve_update`. Otherwise read events overlapping the
   candidate's measure range ± `SEARCH_PAD_MILES` and → `resolve_new`.
3. Write every version and audit record; queue any review items.
4. Arm one Step Functions execution per event CREATED, named with the event id.
5. Move the pointer LAST, and only after the versions are stored. Writing it first
   would create the one unrecoverable order: a record marked handled whose event
   was never written.

**Outcomes:** `created`, `updated`, `merged`, `review`, `unchanged`,
`late_ignored`.

**Formulas it applies:** match scoring (7.2), confidence re-scoring (7.1), field
precedence (7.3), severity (7.4), expected duration (7.5), validation gates and the
lifecycle chain (7.6).

**Derived values computed here**

- `content_hash` = SHA-256 of the candidate serialized with sorted keys, with
  `source.retrieved_at` and `source.raw_ref` REMOVED. Both change on every fetch by
  construction, so hashing them would make every poll look like a change — the same
  bug as having no hash, but harder to see. `source_updated_at` is kept: that one
  changing IS the source saying the record changed.
- `SEARCH_PAD_MILES` = `NEAR_MISS_DECAY_MILES` (5.0). Not an arbitrary radius —
  beyond it the spatial component is zero, the spatial gate fails, and the score is
  capped below the review band, so an event further away cannot merge or even reach
  review. Reading more corridor would cost money to reach the same verdict.
- `ingestLatencyMs` = `now − candidate.source.retrieved_at`, clamped at 0. The
  ingest-to-queryable number: fetch → queryable, and this is the only place in the
  pipeline where both ends are known. Distinct from `latencyMs`, which times this handler alone.

**Error handling, by kind**

| Exception | Behaviour |
|---|---|
| `ConcurrentModification` | one in-process retry — re-read and decide again, since the decision was made against a version that is no longer current. Still conflicting → DLQ. |
| `VersionAlreadyExists` | returns `unchanged`. A duplicate delivery on an at-least-once bus is normal, not a failure. |
| `IllegalTransition` | logged and raised → DLQ with the reason. An illegal transition is rejected and alarmed, never coerced. |
| Timer arming failure | logged, never raised. The event is already stored and queryable; discarding a correct resolution over a timer is the worse trade. |

**The log format is a contract.** Every metric filter in
[`lib/observability-stack.ts`](../lib/observability-stack.ts) reads the field names
in the `resolved` line. Renaming one blanks a dashboard silently rather than
failing a deploy.

---

## 4. lifecycle

[`corridor_event_hub/handlers/lifecycle.py`](../corridor_event_hub/handlers/lifecycle.py)

**Job:** one TTL tick. This is the fix for the known failure of
existing 511 feeds — a condition no longer being reported stays published as live
because nothing ever decides it has gone quiet.

| | |
|---|---|
| Trigger | the `Tick` state of the per-event Step Functions execution (`Wait → Tick → Choice → Wait`), Standard type, 400-day timeout |
| Timeout / memory | 30s / 512 MB (one read, one conditional write, one `PutEvents`) |
| Env | `EVENT_BUS`, `EVENT_TABLE`, `LIFECYCLE_STATE_MACHINE_ARN` |

**Steps:** read the event fresh, ask `expire()` whether a ladder edge is due, write
it if so, then return `{status, waitSeconds}` for the machine's `Choice` state.

**Why it re-reads instead of trusting its input:** a source update between ticks
resets the TTL and may already have moved the event. Acting on state carried in the
execution input would expire an event that had just been confirmed. The input
carries only an id and a sleep duration — which also means no execution ever has to
be cancelled or restarted when an event changes.

**Formulas / constants**

| | |
|---|---|
| `wait_seconds` | `max(MIN_WAIT_SECONDS, seconds_until_ttl + WAIT_SLACK_SECONDS)` = `max(30, remaining + 5)` |
| `MIN_WAIT_SECONDS` = 30 | a lapsed TTL reports 0 seconds remaining, and a `Wait` of 0 in a loop is a busy spin against DynamoDB and the state-transition bill |
| `WAIT_SLACK_SECONDS` = 5 | wake just AFTER expiry; waking exactly on the deadline means millisecond clock skew reads the event as not-yet-stale and every event pays two ticks for every one it needs |
| `MAX_TICKS_PER_EXECUTION` = 200 | Standard executions cap at 25,000 history events. An event under daily updates can tick for months, and an execution that hits the limit FAILS — silently ending the only thing that would ever have expired it. So a bounded execution starts a successor named `{eventId}-{tick}` and the chain continues. |

Plus the TTL ladder and staleness test (7.6).

**A lost race is not an error.** If the resolver writes between this handler's read
and its write, the conditional write refuses and the tick logs `superseded` and
re-arms. The resolver's update is the better information.

**Why not a scheduled sweeper** over "all events where ttl < now": expiry time is
derived from `updated_at` plus a per-class TTL, so it is not a key, and a sweep is a
scan of every live event every minute. Per-event timers put the cost on the events
that actually have deadlines, and the execution history is a second audit trail for
free.

---

## 5. query

[`corridor_event_hub/handlers/query.py`](../corridor_event_hub/handlers/query.py)

**Job:** the read API. One function behind API Gateway HTTP API (payload format
2.0), IAM/SigV4 authorized by default.

| | |
|---|---|
| Timeout / memory | 29s / 1024 MB — synchronous, so the timeout is a latency budget, not a safety net |
| Permissions | `grantReadData` only. A read API with write permission is one bug away from mutating the record it was asked about. |
| Env | `EVENT_TABLE`, `CEH_ROUTE`, `SPATIAL_DB_SECRET_ARN`, `WZDX_PUBLISHER`, `WZDX_CONTACT_NAME`, `WZDX_CONTACT_EMAIL`, `WZDX_LICENSE`, `WZDX_UPDATE_FREQUENCY` |

| Route | Returns |
|---|---|
| `GET /health` | status and route |
| `GET /events` | filtered by bbox / LRS range / class / state / lifecycle state / min-confidence / time window, with full provenance |
| `GET /events/{id}` | one event plus what explains it: audit trail, field provenance, retained alternates, merge decisions, confidence breakdown |
| `GET /events/{id}/history` | every version and transition; `as_of` makes it bitemporal |
| `GET /ahead` | look-ahead by position + direction, ordered by distance |
| `GET /review` | the ambiguous-match queue |
| `GET /wzdx` | WZDx v4.2 feed — the ONE route not wrapped in the Corridor Event Hub envelope, because a conformance validator pointed at this URL must get a document the spec recognizes |

Field names are snake_case, matching the canonical model, so an adopter can read
the model and grep the payload with one word.

**Almost none of this is spatial.** Look-ahead is a measure comparison, a corridor
window is a range scan on a GSI sort key, and dedup already happened upstream. The
one genuinely 2D input is a bbox, and even that is answered by asking the corridor
which of its vertices the box contains and turning that into a measure range —
which is why corridor geometry is loaded LAZILY and only when a bbox or lat/lon
actually arrives.

**Formulas / constants**

- `DEFAULT_LIMIT` = 200, `MAX_LIMIT` = 1000. Truncation is REPORTED
  (`truncated`, `matched_before_limit`): a consumer deciding what is ahead of it on
  the road cannot be left unable to distinguish an empty corridor from a truncated
  answer. `limit=0` raises 400 rather than being read as "no limit given".
- `DEFAULT_LOOK_AHEAD_MILES` = 50 — roughly 45 minutes at highway speed: long
  enough to matter to a routing decision, short enough that the answer is still
  true when it arrives.
- heading → direction: `EB` if `0 ≤ heading mod 360 < 180`, else `WB`. An explicit
  `direction` parameter wins when both are given.
- `distance_miles` = `|near_edge − position|`, where `near_edge` is the min measure
  of the extent travelling EB and the max travelling WB. Results are ordered by
  distance, not by severity or confidence — the consumer is an automated truck and
  needs the next thing first.
- `independent_source_count` = number of distinct independence groups among the
  event's sources, NOT the number of sources. Two mirrored feeds are not two
  witnesses.
- bbox → measure range: the min and max measure of the centerline vertices inside
  the box. A box that misses the corridor returns `(inf, -inf)` — an empty range,
  which correctly returns nothing, rather than no filter, which would return
  everything and look like the box had been ignored. Resolution floor is the
  centerline's vertex spacing; a smaller box over-selects slightly, which is the
  right direction to be wrong.
- `lifecycle.ttl_expired` is a RACE INDICATOR, not a second state machine — it
  closes the gap between a deadline passing and the tick that acts on it. A
  *persistently* expired event means that event's timer chain died, which is what
  the `CorridorEventHub-lifecycle-executions-failed` alarm watches for.

Every response (except `/wzdx`) carries the advisory notice, the confidence model
version and its weights, the match model version and thresholds, and the resolver
policy version — a consumer that calibrated a trust threshold against one model has
to be able to notice when the model changed.

An unparseable filter returns 400 with the reason, never an empty 200. "No events"
is indistinguishable from a clear road.

---

## 6. db_migrate

[`corridor_event_hub/handlers/db_migrate.py`](../corridor_event_hub/handlers/db_migrate.py)

**Job:** apply pending schema migrations to the Aurora cluster. A bastion-free
one-shot function — no SSH keys, no EC2 instance, no port forwarding — that also
works from CI, which a laptop with `psql` does not. See
[SPATIAL-DB.md](SPATIAL-DB.md) §2.

| | |
|---|---|
| Trigger | manual / CI (`npm run db-migrate`, `npm run db-migrate-plan`). DELIBERATELY not wired to `cdk deploy` |
| Timeout / memory | 5 min / 512 MB |
| Driver | `pg8000`, pure Python — a compiled wheel built for the wrong platform deploys perfectly and fails at invoke |
| Input | `{"dryRun": true}` reports the plan and touches nothing |

**Guarantees:** every application is recorded in `schema_migration` (which the
runner creates itself, since it must read that table to decide whether `001` needs
applying); each file runs in ONE transaction, so a failure rolls the whole file back
rather than stopping midway; an edited run-once file is refused with advice rather
than re-applied; a session advisory lock (`pg_try_advisory_lock`, key `0x4143434C`)
means a second concurrent run fails fast instead of interleaving DDL.

**Constants:** `lock_timeout = 15s`; `statement_timeout = max(1000, (remaining − 5) × 1000)` ms,
so Postgres cancels and names the blocked statement instead of Lambda killing the
whole invocation with no indication of which statement.

Raises on drift or a failed statement. A migration that did not fully apply must be
a FAILED invocation — returning 200 with a sad field in the body is how a broken
schema ends up looking healthy on a dashboard.

---

## 7. The formulas

Every number below is a **reasoned starting point, not a tuned model**, and every
one is declared as a named constant at the top of its module so the team can argue
about it on a whiteboard rather than reverse-engineer it from arithmetic. Each
model carries a version string; bump it when the arithmetic changes, never when a
comment does — a version that moves for cosmetic reasons stops meaning anything.

### 7.1 Confidence — [`core/confidence.py`](../corridor_event_hub/core/confidence.py)

`CONFIDENCE_MODEL_VERSION = "0.1.0"`

Confidence is a 0.0–1.0 value **with a published breakdown**, never an opaque
number. The breakdown is constructed in the same function as the score and the
return type makes it impossible to produce one without the other. The consumer is
an automated truck deciding whether to trust a record about a hazard beyond its
sensor range; an unexplainable score cannot support a trust threshold.

```
confidence = clamp( Σ  wᵢ · componentᵢ , 0, 1 )   rounded to 4 dp
```

| Component | Weight | Formula |
|---|---|---|
| `source_reliability` | 0.25 | `max(seedReliability)` over all sources, default 0.5 for an unknown source. Max, not mean — one unreliable corroborator should not drag down an authoritative report. |
| `corroboration` | 0.20 | by count of distinct **independence groups**: 1 → 0.5, 2 → 0.85, ≥3 → 1.0 |
| `recency` | 0.20 | `0.5 ^ (age_seconds / half_life)`, `age = now − last_confirmed_at`, half-life per class |
| `spatial_precision` | 0.15 | `base(conflation_method) − min(0.4, max(0, (accuracy_m − 500) / 5000))` |
| `completeness` | 0.10 | `fields_present / fields_required_for_class` |
| `internal_consistency` | 0.10 | `1 − min(0.6, 0.2 × conflicts) − 0.15·[any inferred lane] − 0.05·[no end_time and no agency duration]`, floored at 0 |

Reliability and independence group are read from
[`config/sources.json`](../config/sources.json), not from a table in core — a table
keyed by `sourceId` would put ids like `ok-odot-wzdx` inside `core/` and
`scripts/check-portability.sh` fails on exactly that. It also means the
a learned-reliability job could overwrite them without a code deploy, and
adding a source is a catalog edit rather than a core change.

Current seed priors: NWS 0.95, FHWA NBI 0.90, Caltrans RWIS 0.90, Caltrans LCS /
CHP 0.85, OK DOT / TxDOT / AZ511 0.80, AWS Location / MCDOT 0.75, NM DOT 0.70,
Caltrans CMS 0.60, Caltrans CCTV 0.50.

**Independence groups matter more than they look.** Sources sharing an
`independenceGroup` count ONCE. Two feeds reselling the same probe data, or two
state feeds mirroring the same regional TMC, are not independent evidence —
treating them as such inflates confidence exactly when it should not, which is
worse than having no corroboration signal at all. A source repeating its own report
is likewise not corroboration.

**Spatial precision base by conflation method:** `native_lrs` 1.0, `milepost` 0.9,
`coordinate` 0.85, `sensor_snap` 0.7, `polygon_intersect` 0.55 (county-sized
polygons are coarse for a corridor), `text_geocode` 0.35, `unresolved` 0.0. The
penalty starts past ~500 m because an event located to ±2 km is weak evidence of
WHERE even when it is strong evidence of WHAT.

**Required fields per class** (completeness): work_zone / closure — extent,
start_time, lane_impacts; incident — those plus event_subtype; congestion —
extent, start_time; weather / road_surface — extent, start_time, event_subtype;
dimensional_restriction / truck_parking — extent. `lanes: []` counts as *not
known*, which is the live ODOT case.

`explain_confidence()` renders the breakdown as one line per component
(`raw × weight = contribution`) plus the total and model version. It ships in every
`/events` response.

### 7.2 Match score — [`core/matcher.py`](../corridor_event_hub/core/matcher.py)

`MATCH_MODEL_VERSION = "0.1.0"`

Cross-agency dedup. The whole operation is **arithmetic, not spatial**: once
conflation has put every candidate on the corridor as a measure range, "are these
the same event?" reduces to range overlap plus time overlap plus class equality.

```
weighted = Σ wᵢ · componentᵢ
value    = min(weighted, REVIEW_THRESHOLD − 0.01)   if any gate failed
         = weighted                                  otherwise
```

| Component | Weight | Formula |
|---|---|---|
| `spatial_overlap` | 0.40 | overlapping: `overlap_miles / shorter_extent_length`, or `1.0` if both extents are shorter than 0.1 mi. Not overlapping: `max(0, 1 − gap_miles / 5.0) × 0.3` |
| `temporal_overlap` | 0.25 | `1.0` if the windows overlap; else `max(0, 1 + overlap_seconds / reopen_window_seconds)` (overlap is negative here) |
| `class_agreement` | 0.20 | `1.0` same class; `0.15` for a known related-but-not-same pair; `0.0` otherwise |
| `direction_agreement` | 0.10 | equal 1.0; either `UNKNOWN` 0.6; either `BOTH` 0.8; opposing (EB vs WB) 0.0 |
| `independence` | 0.05 | `0.3` same independence group, `1.0` different |

**Decision bands:** `≥ MERGE_THRESHOLD (0.75)` → merge; `≥ REVIEW_THRESHOLD (0.5)`
→ review, routed to a human with both events still separately published;
below → distinct. Guessing in the middle is the failure mode that makes a dedup
claim untrustworthy.

**Four of the five components are gates, not contributions** — this is the part of
the model that matters most. A pure weighted sum lets a high score be assembled
from agreement on everything except the thing that decides identity: two events
800 miles apart, same class, same time, same direction score 0.60 and land in
review; the same location five months apart scores exactly 0.75 and MERGES. Both
are absurd. So:

| Gate | Condition | Failure message |
|---|---|---|
| spatial | `spatial_overlap > 0` | beyond the near-miss tolerance |
| temporal | `temporal_overlap > 0` | outside the reopen window for this class |
| class | `class_agreement == 1` | related, not the same event |
| direction | `direction_agreement > 0` | same road, opposite carriageway |

Fail any one and the value is capped below the review threshold; no amount of
agreement elsewhere rescues it. The weighted sum still does the useful work of
*ranking* the matches that pass.

**Tolerances:** `SPATIAL_TOLERANCE_MILES` 0.5 (two agencies locating one crash a
few tenths apart is normal), `NEAR_MISS_DECAY_MILES` 5.0, `POINT_EVENT_MILES` 0.1.
An event with no end time is treated as still running via a far-future sentinel
(year 2999), not as zero-length — and an *unparseable* end time is treated as open
too, because assuming an event ended because we could not read its end date is the
wrong failure.

**Related but never the same:** `(weather, road_surface)`, `(incident, closure)`,
`(incident, congestion)`, `(work_zone, closure)`. A weather alert and the icy
surface it causes are causally linked and spatially identical, but they have
different lifetimes — merging them would make one disappear when the other cleared.
They become `related_event_ids`.

`cluster_candidates()` is single-link agglomerative over the merge threshold, and
its limitation is recorded rather than fixed: transitivity is assumed, so a shared
middle report can chain one long work zone into a neighbouring one. A real resolver
should compare against the MERGED extent and persist merge parents.

### 7.3 Field precedence — [`core/resolution.py`](../corridor_event_hub/core/resolution.py)

When two agencies describe one event they will disagree about details. Three factors
decide who wins — source reliability, specificity, recency — and their *order* is
where it stops being obvious. It is declared per field:

| Field | Order | Why |
|---|---|---|
| `extent`, `lane_impacts`, `start_time` | specificity → reliability → recency | measurements. How precisely a source located something is a property of its answer, not of its reputation. |
| `end_time` | recency → specificity → reliability | a plan that gets revised; the newest estimate is the operative one |
| `event_subtype`, `agency_severity` | reliability → specificity → recency | opinions in the source's own vocabulary — which agency to believe is exactly what a reliability score answers |
| anything else | reliability → specificity → recency | the default order |

**One global order gets the extent wrong**, and it looks fine until you try it.
Reliability first means a 0.95-reliability national weather feed — a polygon
covering a whole county, positional accuracy in kilometres — overwrites a 160 m
milepost from a 0.8-reliability state feed, and the event moves several miles. For
an automated truck that is the difference between a hazard it can act on and one it
cannot.

**Specificity by field:** time fields use the source's own `time_confidence`
(`observed` 1.0, `estimated` 0.6, `scheduled` 0.4, unstated 0.6 — an unlabelled
timestamp is still a timestamp). `extent` uses conflation method (`native_lrs` 1.0,
`milepost` 0.9, `coordinate` 0.85, `sensor_snap` 0.7, `polygon_intersect` 0.5,
`text_geocode` 0.3) minus 0.2 if positional accuracy exceeds 1 km — a
kilometre-scale extent is a claim about the county, not the road. `lane_impacts` =
`min(1.0, 0.4 + 0.1 × lane_count) × (0.6 if any inferred else 1.0)`. Any other
stated scalar: 0.7. Empty / null / `[]`: 0.0.

Empty claims are dropped **before** ranking, so "said nothing" can never beat "said
something" through a tie-break on another factor. Losing values are RETAINED as
`alternates` with their source, agency, timestamp and time confidence — the losing
value is the evidence that two agencies disagreed, which is a finding about a
crosswalk or a feed, and it is what an un-merge needs to restore the child.

A source revising its own record supersedes its own previous claim rather than
competing with it. Without that, a source *withdrawing* a detail could never take
effect: an agency that reported a closed lane and then reopened it sends a record
with no lane impacts, which as an empty claim loses to any stated one including its
own earlier one — and the lane would stay closed forever, attributed to an agency
that had already said otherwise.

### 7.4 Computed severity — [`core/derive.py`](../corridor_event_hub/core/derive.py)

`SEVERITY_FUNCTION_VERSION = "0.1.0"`

```
score = class_baseline
      + Σ  points(lane.type)         for each closed lane
      + Σ  points(lane.type) × 0.4   for each shifted / alternating / intermittent lane
      − Σ  points(lane.type) × 0.3   for each inferred lane
score = clamp(score, 0, 100)  rounded to 1 dp
```

**Class baselines:** closure 70, incident 50, road_surface 45, weather 40,
work_zone 35, congestion 30, dimensional_restriction 25, truck_parking 5, unknown
class 40. A closed road is severe whatever else is true; a truck-parking record is
inventory, not a hazard. An unknown class scores mid-range rather than zero —
scoring an unrecognized hazard as harmless is the wrong failure. The *ordering*
matters more than the exact numbers.

**Lane closure points:** general 12, exit / entrance 5, HOV 4, shoulder 3, median 2,
unknown type 5. Calibrated for a truck in a travel lane.

**Bands:** ≥80 `severe`, ≥60 `major`, ≥35 `moderate`, else `minor`.

An inferred impact still moves the score — discarding it would score a prose-only
closure as an open road. The agency's own word is carried through in
`agency_asserted` even when it disagrees with the computed band: reconciling the
two silently would destroy the evidence that they disagreed, and a systematic
disagreement with one agency's severity vocabulary is a finding about the
crosswalk, not noise.

### 7.5 Expected duration — [`core/derive.py`](../corridor_event_hub/core/derive.py)

`DURATION_FUNCTION_VERSION = "0.1.0"`. Precedence, first match wins:

1. **Observed** (start and end both present): estimate = low = high = observed
   minutes, `basis: agency_stated`. A stated end time is a stronger statement than
   a stated duration, and this is where the band is narrowest.
2. **Agency-stated duration**: estimate = stated, `low = max(1, 0.5 × stated)`,
   `high = 2.0 × stated`, `basis: agency_stated`. Agencies state a plan, not an
   outcome; a stated 60 minutes routinely runs to 90.
3. **Class prior** `(low, estimate, high)` minutes, `basis: class_prior`:

| Class | low | estimate | high |
|---|---|---|---|
| incident (also the default) | 20 | 60 | 180 |
| congestion | 10 | 30 | 120 |
| closure | 60 | 240 | 1440 |
| road_surface | 60 | 240 | 1440 |
| weather | 60 | 360 | 1440 |
| work_zone | 1440 | 10080 | 129600 |
| dimensional_restriction, truck_parking | 525600 | 525600 | 525600 |

`basis` is the load-bearing field: `agency_stated` and `class_prior` are very
different kinds of claim, and a consumer deciding whether to trust an end time
needs to know which it is looking at rather than inferring it from whether the
numbers look round.

### 7.6 Lifecycle, TTLs and validation

[`core/lifecycle.py`](../corridor_event_hub/core/lifecycle.py) ·
[`core/resolution.py`](../corridor_event_hub/core/resolution.py) —
`RESOLVER_POLICY_VERSION = "0.1.0"`

**Staleness**

```
is_stale(event, now)      = now > event.updated_at + ttl_seconds[lifecycle_state]
ttl_expires_at(event)     = event.updated_at + ttl_seconds[lifecycle_state]
seconds_until_ttl(event)  = max(0, ttl_expires_at − now)      None ⇒ never expires
```

`None` means either a terminal state with no TTL, or something like a
`dimensional_restriction` in `active` whose TTL is measured in years. Both need the
same answer: nothing to wait for that a source update will not reset.

**Per-class TTLs, seconds** — a congestion event and a three-year work zone cannot
share a timeout:

| Class | reported | validated | active | clearing | reopen window | confidence half-life |
|---|---|---|---|---|---|---|
| work_zone | 3 600 | 86 400 | 604 800 | 86 400 | 604 800 | 604 800 |
| incident | 600 | 1 800 | 7 200 | 1 800 | 3 600 | 1 800 |
| closure | 900 | 3 600 | 43 200 | 3 600 | 7 200 | 7 200 |
| congestion | 300 | 300 | 900 | 600 | 900 | 600 |
| weather | 1 800 | 3 600 | 21 600 | 3 600 | 10 800 | 10 800 |
| road_surface | 1 800 | 3 600 | 14 400 | 3 600 | 7 200 | 5 400 |
| dimensional_restriction | 86 400 | 2 592 000 | 31 536 000 | — | 2 592 000 | 31 536 000 |
| truck_parking | 3 600 | 86 400 | 1 800 | 3 600 | 3 600 | 900 |

An unrecognized class falls back to the **incident** profile — the shortest
realistic TTLs, so it expires early rather than lingering active forever.

**The TTL ladder** (`timer_ttl`): `reported → cleared`, `validated → active`,
`active → clearing`, `clearing → cleared`. The ladder ends at `cleared`, never
`archived` — archiving is a retention decision on a different clock, so the timer
stops when the event stops affecting traffic. `active → clearing` rather than
straight to `cleared` because **silence is not confirmation**.

`expire()` returns a resolution even when nothing is due, so the "nothing happened"
cases are as inspectable as the transitions: `terminal` (no TTL here), `waiting`
(not expired yet), `not_yet_due` (expired, but a `validated` event whose start time
is still in the future — re-arm rather than publish a future-dated work zone as
live), `expired` (applied).

**`source_absent` is the critical ambiguity.** When a record disappears from a
state 511 snapshot, does that mean CLEARED or UNKNOWN? It differs by state, cannot
be inferred from the data, and getting it wrong silently corrupts lifecycle
behaviour for that entire state. Until a source's catalog entry says otherwise,
`snapshotSemantics: "UNKNOWN"` routes `source_absent` to `clearing` with
`stale_no_updates_semantics_unconfirmed` — never straight to `cleared`. Lingering
slightly too long is recoverable; clearing a live hazard in front of an automated
truck is not. NWS is the contrast case: an alert leaving the active list genuinely
means expired or cancelled, so its entry says `cleared`. Currently `cleared`: NWS,
AWS Location, NBI, Caltrans RWIS/CCTV/CMS, CHP. Everything else: `UNKNOWN`.

**Validation gates** (`reported → validated`), first failure wins and sends the
event to `cleared` via `validation_fail`:

1. extent did not conflate onto the corridor (`begin_measure` is NaN, or no states)
2. unparseable start time — lifecycle timers cannot be set
3. implausible window — end precedes start
4. `confidence < MIN_VALIDATION_CONFIDENCE[class]`: **0.25** for congestion,
   weather and road_surface (sensed and derived classes carry lower single-source
   confidence by construction, so a shared floor would reject them
   systematically), **0.35** default.

The floor is deliberately low. The gate exists to stop a record that could not be
located or timed from ever being published, not to express editorial taste about
weak reports — a low-confidence event *with its breakdown attached* is exactly what
an integrator needs in order to threshold for itself.

`validated → active` requires `start_time ≤ now ≤ (end_time or ∞)`. A work zone
scheduled for next month is validated and NOT active.

A new event always starts in `reported` and walks the chain, even when it will pass
every gate a microsecond later: an event that appears already-active has no
recorded moment of having been validated, and the audit trail carries the trigger
and actor behind every state it has held. **Every audit record's `sequence` equals
the version it produced**, so `v#7` and `audit#7` describe the same moment and reconstructing
the event as of any instant is one query with no join.

**Matching against the store:** only events in `reported`, `validated`, `active` or
`clearing` are matchable, and only if not stale and not already carrying the
candidate's own `source_id`. Excluding `cleared` is deliberate — a recurrence at the
same location is a RE-OPEN (`cleared → active`), a different decision from a
merge, driven by the source that owns the event rather than by a match score.
Excluding stale events stops a crash whose TTL lapsed hours ago from absorbing a
fresh crash at the same milepost. Excluding same-source events stops one agency's
two records from chaining into one event.

On a merge, both events persist: the child walks `reported → validated → merged`
and keeps its identity, so an un-merge can restore it; the parent gains a
version whose provenance includes the child's source, and the parent is **re-scored
with the full source list** — `last_confirmed_at` is the freshest of all
contributing sources, so one stale corroborator cannot drag down an otherwise fresh
event, and `conflict_count` is the number of contested fields.

---

## Where to change what

| To change | Edit |
|---|---|
| a source's reliability, independence group, poll cadence, snapshot semantics | [`config/sources.json`](../config/sources.json) — no code deploy |
| confidence weights or component formulas | [`core/confidence.py`](../corridor_event_hub/core/confidence.py), bump `CONFIDENCE_MODEL_VERSION` |
| merge / review thresholds, match weights, gates | [`core/matcher.py`](../corridor_event_hub/core/matcher.py), bump `MATCH_MODEL_VERSION` |
| severity baselines / lane points / bands, duration priors | [`core/derive.py`](../corridor_event_hub/core/derive.py), bump the relevant `*_FUNCTION_VERSION` |
| TTLs, reopen windows, half-lives, legal transitions | [`core/lifecycle.py`](../corridor_event_hub/core/lifecycle.py) |
| validation floors, field precedence, the TTL ladder | [`core/resolution.py`](../corridor_event_hub/core/resolution.py), bump `RESOLVER_POLICY_VERSION` |
| memory, timeout, concurrency, env, schedules, DLQs | [`lib/ingest-stack.ts`](../lib/ingest-stack.ts) |

Renaming a field in a handler's log line changes a metric filter in
[`lib/observability-stack.ts`](../lib/observability-stack.ts) and will blank a
dashboard silently rather than failing a deploy. See
[OPERATING.md](OPERATING.md) for what "working" looks like on the dashboard.

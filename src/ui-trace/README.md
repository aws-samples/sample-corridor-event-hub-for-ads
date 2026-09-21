# Record lifecycle tracker

The second local UI. React + Vite, served by a local **read-only** Python API over
the **deployed** event store — every version, every audit record, and the exact S3
payload that caused each one.

```bash
cd src
export AWS_PROFILE=<your-profile>     # required: everything here comes from the cloud
npm run trace-ui                      # API on :8788 + UI on :5174, one Ctrl-C stops both
```

Then open <http://localhost:5174>.

## Why there are two UIs

They answer different questions from different sources, and neither one can answer
the other's.

| | `ui/` — corridor strip | `ui-trace/` — record tracker |
|---|---|---|
| Question | What is on the road **right now**? | What **happened** to this record? |
| Data | Runs the six adapters live against the agency feeds | Reads the deployed DynamoDB event store, S3 raw zone, SQS, Step Functions |
| History | **None.** One snapshot; the payload says `historyAvailable: false` | The whole append-only chain |
| AWS account | Not needed | Required |
| Ports | API 8787, UI 5173 | API 8788, UI 5174 |

Different ports on purpose: following a record from the corridor view into its
history is the normal workflow, so both run at once.

**The history only exists in the cloud.** `strip_export.build()` holds no state
between builds, so every event is first-seen on every build and its TTL countdown
restarts — it says so in its own document. Simulating a lifecycle locally would be a
demo of the state machine rather than a view of the system, and the difference matters
most in exactly the situation you would open this tool for.

## What it shows

**A record list** — one row per current record, most recently touched first. Class,
state, milepost range, agencies, version count, quiet time, TTL position. The two
conditions that mean the pipeline may have failed a record are marked on the row so
finding them does not mean opening a hundred traces: a **lapsed TTL** and an
**unresolved extent**, which no corridor query can return.

**A trace**, four layers, coarsest first:

1. **Findings** — is anything wrong. Audit gaps, illegal stored transitions, dead
   timer chains, merges with no parent, version churn, clock skew, feeds past their
   freshness SLO, single-witness publication.
2. **Pipeline stages** — ingest → normalize → resolve → lifecycle → end of life, each
   stating the evidence it read. `normalize` says outright that mapping issues are
   metered and not persisted per event, rather than inventing a count.
3. **Time in each state** — proportional bands, with a width floor so a state
   occupied for four seconds is still visible.
4. **Steps** — every recorded transition with its trigger, actor, reason, rule
   version, both clocks, the version diff, and a link to the raw agency bytes.

**The raw payload** — the exact bytes from the raw zone, with this record
extracted from them when the feed publishes a per-record id. That is the difference
between a tool that explains the pipeline and one that can be used to find the
pipeline **wrong**.

**A pipeline tab** — per-feed fetch status, dead-letter depths, TTL timer executions,
record counts by state. On the same tool because an empty record list means a quiet
corridor if the feeds are being fetched and the queues are empty, and a broken
pipeline if they are not.

## Read-only, and structurally so

Every AWS call underneath is a `Get`/`Query`/`Describe`/`List` — see
`corridor_event_hub/core/cloud.py`. There is no write path, no operator-override endpoint, and
no client with a mutating call on it, so pointing this at production cannot change
production. Adding an operator override later means adding a deliberate,
audited, authenticated write path — not relaxing something here.

Every table name, bucket and ARN is **discovered** from the deployed CloudFormation
outputs, never hardcoded, so the tool cannot quietly read the wrong account. The
account, region and table are in the header for the same reason. Override discovery
with `EVENT_TABLE=<name>` or `CEH_INGEST_STACK=<name>`.

No login prompt: the API binds to `127.0.0.1` and holds ambient IAM credentials, so a
gate in front of the React app would suggest a protection it does not provide.

## What it costs to run

| Read | Cost |
|---|---|
| Record list | One indexed query per selected lifecycle state, cached 20s |
| Trace | Two queries for the last 200 steps plus two `COUNT` queries, cached 5s |
| Pipeline tab | `COUNT` per state, one small scan, four SQS calls, five SFN lists, cached 20s |
| Raw payload | One ranged `GetObject`, on demand only |

History is opt-in: `cleared`, `merged` and `archived` are unselected by default
because reading them costs an extra indexed query per refresh, and the corridor has
~840 cleared records against ~100 live ones.

## Long histories are windowed, and it says so

Live data contains a record with **over 1,500 versions** — one per 60-second poll for
a day — so a trace reads the most recent 200 steps and reports the true totals
alongside them. `?window=1000` on the API raises it. A view showing 200 of 1,564 steps
without saying which is the failure this whole codebase is written against.

Those long chains are also why confirmation runs collapse: hundreds of steps that
changed nothing but the payload pointer would hide the two that moved the record. The
count is kept — "confirmed 1,563 times" is a trust signal, not noise.

## Commands

Run these from `src/`, not from here. Each has a `make` twin of the same name.

| Command | Does |
|---|---|
| `npm run trace-ui` | Both processes. The one to use. |
| `npm run trace` | The API alone on :8788 — prints the account it resolved before serving |
| `npm run trace-ui-build` | Production build into `ui-trace/dist` |
| `npm run trace-ui-test` | The 34 derivation tests (step grouping, band widths, filters) |

From inside `ui-trace/`, the Vite scripts are `npm run dev`, `npm run build`,
`npm run test`, `npm run typecheck` — front end only, no Python API.

## API

`snake_case`, matching `handlers/query.py` rather than the strip document, because
these routes are local stand-ins for the deployed query API. Pointing this
app at that API is a base-URL change plus SigV4, not a re-model.

| Route | Returns |
|---|---|
| `GET /api/meta` | Cloud identity, the transition table, TTL ladder, class profiles, model versions |
| `GET /api/records?state=&limit=` | Current records by lifecycle state |
| `GET /api/records/{eventId}?window=` | One record's whole trace |
| `GET /api/lookup?source_id=&native_id=` | Agency record id → our event id |
| `GET /api/raw?ref=&native_id=` | The raw agency payload, with the record extracted |
| `GET /api/pipeline` | Feed status, DLQ depths, timer health, state counts |

Add `refresh=1` to bypass the cache.

## Where the logic lives

`ui-trace/src/derive.ts` holds everything that decides *what* is shown — run
collapsing, band widths, filters, formatting — separate from the components that draw
it, and unit-tested. Arithmetic inside a component is arithmetic nobody tests, and
the failures here are silent: a band that renders a short state as zero pixels says
the record was never reported.

The server side is `corridor_event_hub/core/trace.py` (pure assembly and the findings, tested
against `InMemoryEventStore` with no account) and `corridor_event_hub/core/cloud.py` (the reads).

## Palette

Event-class colours are identical to `ui/`'s on purpose — the two apps are read side
by side. Duplicated rather than shared because they are separate packages with
separate installs; the class hues are the part to keep in step if either changes.
Lifecycle-state colours are new here and encode a progression: cool and quiet for
`reported`/`validated`, loud for `active`, warm for `clearing`, grey for history,
purple for `merged`, which is neither live nor ended.

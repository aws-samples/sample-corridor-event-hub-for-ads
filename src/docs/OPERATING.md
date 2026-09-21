# Testing and Monitoring a Deployed Corridor Event Hub

Verified against a live deployment in `us-west-2`.

---

## The commands

```bash
npm run status                    # health of everything, one screen
npm run logs                      # live tail, both functions interleaved
npm run dlq                       # dead letters (should be zero)
npm run invoke -- ok-odot-wzdx    # force a run, follow it end to end
npm run probe                     # run adapters locally
npm run ui                        # same, as a live corridor view in the browser
npm run trace-ui                  # one record's whole life, read from the deployment
```

**`npm run trace-ui` is the tool for "what happened to this record".** It is the only
one here that reads the **deployed event store** rather than the feeds: every version,
every audit record, and the exact S3 payload that caused each one. Read-only by
construction — every AWS call underneath is a `Get`/`Query`/`Describe`/`List`, and it
discovers table names, buckets and ARNs from CloudFormation outputs rather than
configuration. API on :8788, UI on :5174, so it runs alongside `npm run ui`. See
[../ui-trace/README.md](../ui-trace/README.md).

`npm run ui` runs the React strip app — API on :8787, UI on :5173, both stopped by one
Ctrl-C. Same adapter output as `probe`, plus cross-agency matching, per-event
provenance, and the age of the data on screen. See [../ui/README.md](../ui/README.md).

For a demo with no network at all, `npm run serve-fixtures` serves captured payloads
only — the rehearsed fallback for when a state feed is down on the day.
`npm run strip` still writes `docs/strip/data.json` for scripting or archiving a
snapshot.

Every script also runs directly, which is the form to reach for when you want to skip
npm's argument handling entirely:

```bash
bash scripts/invoke.sh nws-alerts
```

`npm run probe` needs no environment variables. For each credentialed source it reads
the environment variable first, then Secrets Manager via your AWS credentials, and
skips that source **with a clear note naming which of the two failed** if neither
works — never silently, because a state quietly missing from the output looks like a
state with no roadwork.

---

## Secrets

<a id="secrets"></a>

**Three secrets must exist before the first deploy.** Nothing in the CDK app creates
them: a feed key is issued by an agency, not by a template, and a stack that generated
a placeholder would deploy green and then fail at every collection.

| Secret name | Source | How to get it | Without it |
|---|---|---|---|
| `corridor-event-hub/ok-odot-wzdx-token` | Oklahoma ODOT | **Published, no signup.** Read it out of the federal WorkZone Feed Registry — command below | `ok-odot-wzdx` returns 401; Oklahoma work zones absent |
| `corridor-event-hub/tx-dot-wzdx-key` | TxDOT | Request from TxDOT. The contact address is published by the feed itself, in `road_event_feed_info.data_sources[].contact_email` — read it from there rather than from this table, so a reissued address cannot leave this document quietly wrong | `tx-dot-wzdx` returns 401; Texas absent |
| `corridor-event-hub/az511-key` | Arizona AZ511 | Developer signup at az511.gov | `az511-events` returns 401; **four of eight event classes absent**, since AZ511 is the only source producing incidents |

A fourth, `corridor-event-hub/spatial-db-credentials`, is **created by CDK** — RDS generates and
owns the password and nothing else should touch it. It is listed here only so nobody
creates it by hand and then wonders why the cluster will not accept it.

The other three live sources need no credential at all: NWS and New Mexico are open
(NWS requires an identifying `User-Agent`, which is policy rather than auth), and the
Amazon Location traffic tiles authenticate with the collector's own execution role —
the one credential story this deployment already solved, and the reason it is the
source with nothing to rotate.

**Oklahoma's token is genuinely public** and it is worth being precise about why it is
in Secrets Manager anyway. It is published verbatim in the federal registry, so it
protects nothing; it used to be committed to this repository for exactly that reason,
and a security review raised that anyway. The fix is not about confidentiality — it is
that a hardcoded credential in an artifact handed to state DOTs teaches the wrong
pattern whatever the token's own sensitivity. See
[ADR 0004](adr/0004-source-credentials-in-secrets-manager.md).

```bash
# Oklahoma: read the token out of the federal WorkZone Feed Registry.
# The registry is the authority - do not copy it out of a wiki or an email, because
# then nobody can tell whether it has been reissued.
OK_TOKEN=$(curl -s 'https://data.transportation.gov/resource/69qe-yiui.json?$limit=5000' \
  | python3 -c '
import json, sys, urllib.parse as u
for record in json.load(sys.stdin):
    if record.get("issuingorganization") == "Oklahoma Department of Transportation":
        query = u.parse_qs(u.urlsplit(record["url"]).query)
        print(query["access_token"][0])
        break
')
test -n "$OK_TOKEN" || { echo "registry lookup found no Oklahoma record - check by hand"; }

aws secretsmanager create-secret \
  --name corridor-event-hub/ok-odot-wzdx-token \
  --description "Oklahoma ODOT WZDx read token. PUBLIC - published in the federal ITS WorkZone Feed Registry (data.transportation.gov/resource/69qe-yiui). Here for uniformity, not confidentiality: see ADR 0004." \
  --secret-string "$OK_TOKEN"

# Texas and Arizona: issued to you, so paste them.
aws secretsmanager create-secret \
  --name corridor-event-hub/tx-dot-wzdx-key \
  --description "TxDOT DriveTexas WZDx API key. Issued by TxDOT." \
  --secret-string '<your-txdot-key>'

aws secretsmanager create-secret \
  --name corridor-event-hub/az511-key \
  --description "Arizona AZ511 API key. Developer signup at az511.gov." \
  --secret-string '<your-az511-key>'
```

**The secret holds the bare credential, not JSON.** The collector uses `SecretString`
as-is and appends it to the query parameter the catalog names in `authQueryParam`
(`access_token` for Oklahoma, `key` for the other two). Wrapping it in `{"key": "..."}`
produces a 401 that reads like a bad key.

Confirm all three before deploying, and confirm the values rather than just the names —
an empty secret exists and fails:

```bash
for s in ok-odot-wzdx-token tx-dot-wzdx-key az511-key; do
  printf '%-24s ' "$s"
  aws secretsmanager get-secret-value --secret-id "corridor-event-hub/$s" \
    --query 'length(SecretString)' --output text 2>/dev/null || echo MISSING
done
```

Then verify end to end, which is the only check that proves the credential works
rather than merely exists:

```bash
npm run invoke -- ok-odot-wzdx    # expect http=200 and a nonzero payload size
npm run api-check                 # the deployed read API, SigV4-signed
```

### Deploy order for the two code-only security fixes

Both are in the repository and neither is live yet. Order matters in both directions:

```bash
# 1. The secret first. Redeploying the ingest stack before this exists means the
#    Oklahoma collector starts returning 401.
aws secretsmanager create-secret --name corridor-event-hub/ok-odot-wzdx-token ...

# 2. A FRESH bundle. The database connection now verifies the Aurora certificate
#    against the RDS CA chain, which `build-lambda.sh` fetches into the bundle -
#    the RDS roots are self-signed and absent from the Lambda trust store, so a
#    re-push of the previous bundle has no chain to verify against.
npm run bundle

# 3. Deploy. `npm run deploy` bundles first, which is why it is the safe command
#    and a bare `cdk deploy` is not.
npm run deploy

# 4. Prove both.
npm run invoke -- ok-odot-wzdx     # http=200 -> the secret resolves
npm run db-check                   # "verified against the bundled RDS chain"
```

**Read the TLS line in step 4.** `db-check` prints the posture on every run, and that
string is what made the problem findable at all — the old code said "certificate NOT verified"
and had been saying it for weeks. Expected values now:

| Posture string | Means |
|---|---|
| `verified against the bundled RDS chain (rds-global-bundle.pem)` | The deployed default. What you want to see |
| `verified against <path>` | `SPATIAL_DB_CA_BUNDLE` is set and wins — fine, and the path to use if AWS rotates the roots |
| `verified against the platform trust store` | No chain in the bundle. **Against Aurora this fails** — run `npm run bundle` |
| `certificate NOT verified` | `SPATIAL_DB_TLS_INSECURE` is set. Local development only; it raises inside Lambda rather than applying |

**Rotation.** Update the secret; containers pick the new value up as they recycle,
because it is cached per container rather than per invocation. Immediate rotation needs
a forced redeploy. No secret needs a rotation *schedule* — these are agency-issued
keys, not credentials this system controls — with one exception on the path to
production: `corridor-event-hub/spatial-db-credentials` should be on a rotation schedule, and
the schedule should be tested against `npm run db-migrate`, which holds an advisory
lock and is the consumer that would break silently.

**IAM is scoped to the path prefix**, `corridor-event-hub/*`, not `secretsmanager:*` — so a new
source brings its own key without a policy change. That prefix is the reason the names
above are not arbitrary.

**Two env-var conventions, and they are not interchangeable.** Worth knowing before you
set one on the wrong side and conclude it is being ignored:

| Where | Variable | Used by |
|---|---|---|
| Local tools | `OK_ODOT_TOKEN`, `TX_DOT_KEY`, `AZ511_KEY` | `npm run probe`, `npm run ui` — short names, because these get typed |
| Deployed collector | `TOKEN_OK_ODOT_WZDX`, `TOKEN_TX_DOT_WZDX`, `TOKEN_AZ511_EVENTS` | The Lambda — derived mechanically from `sourceId`, so a new source needs no code change |

Neither is set in the stack, and neither should be: the deployed path resolves from
Secrets Manager, and `TOKEN_*` exists so a source can be exercised without one. A
credential in a Lambda environment variable is visible in the console and in
`describe-function-configuration`, which is why ADR 0004 rejects it as the normal path.

**Two user-agent variables, and they point in opposite directions.** The same trap as
above, and easier to fall into because the names are similar:

| Variable | Direction | What it carries |
|---|---|---|
| `CEH_USER_AGENT` | Outbound to state DOT feeds | A contact address, which NWS policy requires of a polling client. Set it locally; the collector reads it too |
| `SOLUTION_USER_AGENT` | Outbound to AWS APIs | `AWSSOLUTION/SO0358/v1.0.0`, which attributes service API usage to this solution |

`SOLUTION_USER_AGENT` **is** set in the stack — it is the one env var that is, because
it is not a credential and the template is where the solution version belongs. Every
Lambda gets it from its own template's `Mappings.Solution.Metadata.CustomUserAgent`, so
`aws lambda get-function-configuration` is where to look if you are checking which
version a deployed function reports. Unset it locally and the tools fall back to the
version compiled into `corridor_event_hub/core/awsclients.py`; `npm run lint:solution` is what
keeps those two from drifting.

`npm run status` is the one to reach for first. It discovers resource names from
CloudFormation outputs rather than hardcoding them, so it works against any
deployment of these stacks.

Real output from the live stack:

```
STACKS
  CorridorEventHubIngest   CREATE_COMPLETE
  CorridorEventHubNetwork  CREATE_COMPLETE

SOURCE HEALTH  (written by the collector on every attempt)
    nws-alerts       ok  http= 200   231271B   400ms  last success 91s ago
    ok-odot-wzdx     ok  http= 200    93913B   816ms  last success 26s ago

RAW ZONE  s3://amzn-s3-demo-rawzone        (real name is CloudFormation-generated)
  nws-alerts                  1 payload(s)   latest 2026-08-07 16:16:07
  ok-odot-wzdx                4 payload(s)   latest 2026-08-07 16:17:12

NORMALIZER  (last 30 min)
  nws-alerts         1 run(s)     1 candidates    15 off-corridor    21 issues  confidence 0.713-0.713
  ok-odot-wzdx       5 run(s)    10 candidates    15 off-corridor    35 issues  confidence 0.578-0.595

ERRORS  (last 30 min)
  none
```

A `!` in the left margin of SOURCE HEALTH means no successful collection in 30
minutes — the per-source freshness signal.

---

## What "working" looks like

The pipeline is healthy when a raw payload lands in S3 and a matching
`normalized` line appears within about a second:

```
[collect] {"msg":"collected","sourceId":"ok-odot-wzdx","bytes":93913,"latencyMs":1396,"rawRef":"s3://.../2026-08-07T23:14:11.495Z-dd1a268bb720.json"}
[normal ] {"msg":"normalized","sourceId":"ok-odot-wzdx","candidates":2,"offCorridor":3,"issues":7,"confidenceRange":[0.5777,0.5951]}
```

Observed baselines, so you can tell normal from broken:

| Source | Candidates | Off-corridor | Issues | Confidence | Latency |
|---|---|---|---|---|---|
| `ok-odot-wzdx` | 2 | 3 | **10** | 0.578–0.595 | ~1.0–1.4s |
| `tx-dot-wzdx` | 4 | 0 | 4 | — | ~0.5s (5.6MB) |
| `nws-alerts` | 0–1 | 15 | 20–21 | ~0.713 | ~0.4s |

TxDOT is the largest payload at 5.6MB for 2,059 records, of which 4 are on I-40.
Texas has only 177 corridor miles, so small numbers are correct - but those 4
records sit near the NM and OK borders and are what exercises cross-state dedup.

**These numbers are stable, so a change is meaningful.** If `ok-odot-wzdx`
suddenly reports 40 issues, the feed format changed.

### Three counts that look like problems and are not

**`issues: 10` every run.** Unmappable values are recorded rather than dropped.
Oklahoma's I-40 records genuinely lack lane detail and carry generated end dates, so 10 issues per run is correct behavior. A *spike* is the signal; a steady
rate is the system working.

**This was 7 before the generated-end-date fix**, and the extra 3 are that fix
rather than a regression. ODOT computes `end_date` as *request time + fixed
offset*: measured over an 84,598-second gap, `end_date` advanced by 84,598 seconds
on 54 of 57 records, so a zone that ends "in 10 days" ends in 10 days forever. The
old `> 2 years + millisecond precision` rule caught only the absurd ones (2029) and
published the near-term ones (2026-10-19), which are the dangerous ones — a
consumer acts on a plausible October date. All 5 I-40 candidates now publish
`end_time: null` and a per-class prior supplies the duration instead. **Worth
raising with ODOT.**

**`offCorridor: 15` on NWS.** Most weather alerts in four states legitimately do
not touch I-40. Correct filtering.

**`candidates: 0` on NWS, sometimes.** 20 of 31 live alerts arrive with
`geometry: null` and only UGC zone codes. They cannot be placed without a zone
shapefile join, so they are reported as issues. When no polygon alert touches the
corridor, zero candidates is the right answer.

### Why NWS scores higher than Oklahoma

0.713 vs. 0.578–0.595, and the breakdown explains it: NWS has a higher source
reliability prior and complete required fields, while the ODOT records lose
points on completeness (no lane detail) and spatial precision (coordinate
projection, now 50–388 m against real state LRS geometry rather than the
~1.6–2.2 km the placeholder centerline gave).

That is the confidence model working — the score is explainable, not mystical.

---

## Dashboard and alarms

```bash
npx cdk deploy --all -c alarmEmail=you@example.com
```

Then: **CloudWatch → Dashboards → Corridor-Event-Hub-ADS**

Metrics come from **metric filters over the structured logs**, not explicit
`PutMetricData` calls. That keeps instrumentation out of the handlers and means
the numbers cannot drift from the logs. The tradeoff: ~1 minute delay, and the
handlers' `msg` field becomes a contract rather than a convenience — renaming it
silently breaks the dashboard.

### Reading the dashboard

Rows follow the data path: **collect → normalize → runtime → resolve → dead
letters**. Lifecycle is deployed but has no row — see [what the dashboard will not
tell you](#what-the-dashboard-will-not-tell-you). The
default window is 3 hours, and every per-source widget draws one line per
`verified_live` source in `config/sources.json` (six today), so a widget with
fewer lines than expected is itself a finding.

**Two things to know before reading any number on it.**

*Count widgets are 10-minute Sums, not per-run values.* The baselines table above
is per run; the graphs are per bucket. **The bucket is the slowest source's poll
interval** — 10 minutes today, set by `nm-dot-weathershare`, and derived from the
catalog rather than hardcoded so changing a cadence cannot leave the dashboard
describing the old one. The header widget states the current value and every
source's cadence.

Why not 5 minutes: the per-source filters publish nothing for a bucket with no
matching log line (see below), so a source polled every 10 minutes would leave
every other 5-minute bucket empty and render as a **broken line** — which is the
one signal this dashboard reserves for "the source went silent." The cost is that
the fastest source reads coarser: `ok-odot-wzdx` at 60s shows **~10 collections and
~100 issues per bucket**, the 300s sources ~2, and NMDOT 1. A number needing one
division beats a healthy feed drawn as a fault.

A flat line at 1 on `ok-odot-wzdx` is a 10× collection drop wearing the shape of
health.

*There is no zero on these widgets — only a line or no line.* CloudWatch Logs
rejects `dimensions` and `defaultValue` together, so every per-source filter omits
`defaultValue` and nothing is published for a period with no matching log line.
This is the single most confusing thing about this dashboard, so be concrete about
what it means per widget:

- **`FetchFailures` is empty when everything is healthy.** Not a flat line at zero
  — *no line at all*, and no legend entry. The only sources that ever appear here
  are the ones that have failed. On the observed deployment that was exactly one,
  `aws-location-traffic`, and it is genuinely broken (see below).
- **On `PayloadsCollected`, a line that stops is a source going silent** — the
  same absence the staleness alarms treat as `BREACHING`, and the signal that
  widget exists for.

So a source that never fails and a source that never ran look identical on the
failure widgets: absent. Read `PayloadsCollected` first to establish who is alive,
*then* read the failure widgets. Checking the legend beats eyeballing the shape.

**Row 1 — collect.**

| Widget | Metric | Healthy | A change means |
|---|---|---|---|
| Payloads collected per source | `PayloadsCollected`, Sum | 10/bucket OK, 2 for the 300s sources, 1 NMDOT — every line continuous | Line ending = source silent. Fewer lines than sources = a schedule or filter broke |
| Feed latency (ms) | `FetchLatencyMs`, **Average** | ~400ms NWS, ~500ms TxDOT, ~1.0–1.4s OK | Rising latency is a leading indicator of an agency in trouble, not an error. No alarm watches it — read it before you need it |
| Fetch failures | `FetchFailures`, Sum | **empty — no lines at all** | Any line here is a source that has failed. Isolation NFR: one feed failing must leave the others absent. If several appear at once, suspect NAT or the endpoints, not the agencies |

**Row 2 — normalize.** These three are the ones most often misread.

| Widget | Metric | Healthy | A change means |
|---|---|---|---|
| Candidate events produced | `CandidatesProduced`, Sum | ~20/bucket OK, ~8 TxDOT, 0–2 NWS | Zero across all sources *while collection continues* is the worst signal on the dashboard and **has no alarm** — an adapter can go blind silently |
| Mapping issues (review queue depth) | `MappingIssues`, Sum | ~100/bucket OK, ~40 NWS, ~8 TxDOT | **Not a bug count.** Unmappable values are recorded rather than dropped, so a steady nonzero rate is the system working. A *spike* is format drift |
| Off-corridor records | `OffCorridorRecords`, Sum | ~30/bucket NWS, ~30 OK, 0 TxDOT | Correct filtering, not error. A jump toward the full record count means corridor geometry or LRS matching broke — cross-check `npm run probe` |

Both nonzero baselines have prose above: [three counts that look like problems
and are not](#three-counts-that-look-like-problems-and-are-not).

**Row 3 — runtime.** `Duration` p95 and `Errors`/`Throttles` for both functions,
straight from `AWS/Lambda`. This row is the only part of the dashboard that does
not depend on the log-format contract, which makes it the place to start when the
custom metrics look impossibly quiet — if p95 is normal and errors are zero, the
handlers are running and the `msg` contract is what broke.

**Row 4 — resolve.** Metric filters over the *resolver's*
log group, so unlike the rows above these describe what actually landed in the
event store.

| Widget | Metric | Read it as |
|---|---|---|
| Ingest-to-queryable latency, p95 by class | `IngestLatencyMs`, p95 | **The number to hold the pipeline to.** The budget is p95 ≤ 90s for incidents, measured collector-fetch to event-in-store. This is the number to have on screen at the demo |
| Resolver decisions | `EventsResolved` by `action`, `MatchReviewsQueued` | `merged` is the cross-agency dedup story. A review queue climbing steadily means the match thresholds need revisiting; one at **zero forever** means the ambiguous band is never being reached, which is its own bug |

Note what this row does *not* establish: it counts merges, it does not tell you
which were correct. Dedup precision and recall need labelled pairs.

**Row 5 — dead letters.** Four queues now — normalizer, rule, resolver,
resolver-rule — across two widgets, and the pairing is the point:

- **Depth** (`ApproximateNumberOfMessagesVisible`, **Maximum**) is a gauge — what
  is stuck right now. `Maximum` rather than `Sum` because summing a gauge across
  periods multiplies one stuck message into an imaginary pile.
- **Dead-lettered over time** (`NumberOfMessagesSent`, Sum) is a counter — it
  records that a payload *ever* failed, and keeps that spike after `npm run dlq-replay`
  drains the queue and depth returns to zero.

Read depth to decide whether to act; read the counter to know it happened at all.
Both flat at zero is the single most reassuring thing on this dashboard.

### What the dashboard will not tell you

Worth knowing before someone treats a green screen as an all-clear.

- **No lifecycle instrumentation.** The TTL machine is deployed and alarmed
  (`CorridorEventHub-lifecycle-executions-failed`), but its log group is not passed to the
  observability stack, so **no state transition appears on the dashboard at all**.
  Transition latency is unmeasured, and a stuck event is invisible here.
- **No confidence distribution.** The score lives only in the `normalized` log line
  and `npm run status`.
- **Decision mix is not dedup accuracy.** `EventsResolved` by action graphs
  `merged` / `created` / `review`, which counts merges without telling you which
  were *right*. Precision and recall need labelled pairs, not a counter.
- **`PayloadsQuarantined` has an alarm but no widget.** It is the one signal you
  learn about by email rather than by looking — so an unsubscribed deployment
  loses it entirely.
- **Candidates going to zero is unalarmed** (see row 2). Collection succeeding
  while normalization produces nothing looks fine on the alarm set.

### Two alarms could not fire at all — fixed, and worth understanding

For a time, `CorridorEventHub-fetch-failures` and `CorridorEventHub-mapping-issue-spike` were
**dead**: their metric filters published *with* a `sourceId` dimension, but the alarms
watched the metric with **no dimensions**, and in CloudWatch dimensions are part of a
metric's identity, so the undimensioned metric received nothing, ever. On the live
stack they looked like this, from creation until they were replaced:

```
CorridorEventHub-fetch-failures       OK   no datapoints were received for 1 period
CorridorEventHub-mapping-issue-spike  OK   no datapoints were received for 2 periods
```

Both said `OK` while `aws-location-traffic` was logging **12 real fetch failures**
in a single hour and the pipeline was emitting thousands of mapping issues. An
alarm that reads `OK` because it can never receive data is worse than no alarm.

**What replaced them:** one alarm per source for each — `CorridorEventHub-fetch-failures-<source>`
and `CorridorEventHub-mapping-issue-spike-<source>` — passing `dimensionsMap` explicitly, the way
the staleness alarms always did. Two further corrections were needed, and both are traps
worth avoiding elsewhere:

- **The fetch-failure window is derived from cadence**, not fixed. A flat 15-minute window
  cannot hold three polls of the 600-second source, so `threshold: 3` was *arithmetically
  unreachable* there — the same defect as the missing dimension, better hidden.
- **The mapping-issue alarm thresholds `Average`, not `Sum`.** `Average` is issues **per
  run**, which is cadence-independent; `Sum` measures issues per window and so means
  something different for every cadence. The threshold is a per-source ceiling from
  `mappingIssueCeilingPerRun` in the catalog, because the healthy rate spans 4 issues per
  run (TxDOT) to 97 (tiled source) and no single number fits.

If you re-baseline a feed, edit `mappingIssueCeilingPerRun` in `config/sources.json` and
redeploy the observability stack — the threshold is `max(25, ceiling × 2)`.

**This was the same root cause as the empty widgets**, seen from the alarm side:
dimensioned metrics with no `defaultValue` publish nothing until something happens,
and anything watching the undimensioned name sees silence forever. `npm run check` now
fails any alarm that watches a dimensioned filter's metric without its dimensions, so
this cannot come back by accident.

### A fetch failure that never reached the fetch-failure signals

The alarms above only fire on what the collector *logs*, and the collector only logged
a `fetch_failed` line for failures raised **inside the HTTP call**. Anything that failed
while *preparing* the call skipped that branch entirely, because `_build_url` raised and
the invocation ended before the failure path ran.

Measured on the deployed stack over an eight-minute window: the three `api_key_secret`
sources were redeployed pointing at secrets that did not exist yet, and

```
[ERROR] ResourceNotFoundException: Secrets Manager can't find the specified secret
  File "/var/task/corridor_event_hub/handlers/collector.py", line 91, in handler
    url = _build_url(source)
```

repeated for 23 invocations. `ok-odot-wzdx` collected nothing for nine minutes. What
the observability stack showed for it:

| Signal | What it read | Why |
|---|---|---|
| Fetch failures widget | **empty** | no `fetch_failed` line, so no `FetchFailures` data point |
| `CorridorEventHub-fetch-failures-ok-odot-wzdx` | `OK` | same — the metric it watches was never published |
| `CorridorEventHub-stale-ok-odot-wzdx` | `OK` | correct: the window floor is 30 minutes, the gap was nine |
| Source catalog (`npm run status`) | healthy, `lastError` unset | `_record_source_health` is below the raise |
| `CorridorEventHub-lambda-errors` | `ALARM` 21:43 → `OK` 21:54 | the **only** signal that fired, and it names no source |

So the failure was not invisible — it was *unattributable*. The dashboard's "Lambda
errors and throttles" widget showed 23 errors while every per-source widget and alarm
on the same screen said the sources were fine.

**The fix is in the collector, not the dashboard.** `_build_url`/`_build_headers` now
run inside the same never-raises contract as `_fetch`: an unresolvable credential or a
non-https endpoint returns `status 0` with the exception text in `error`, which means it
flows through the existing failure path — health record, `fetch_failed` line,
`FetchFailures` data point, per-source alarm. An unknown `sourceId` still raises, because
that one is the schedule and the catalog having diverged, not a source failing.

The general shape, and the third instance of it in this project: **a metric derived from
a log line is only as complete as the code paths that reach the `print`.** A failure that
returns early, or raises past it, is a gap in the metric that no amount of correctness in
the filter or the alarm can close.

### Alarms

| Alarm | Fires when | Why |
|---|---|---|
| `CorridorEventHub-stale-<source>` | no successful collection in `max(30 min, 3 × cadence)` | freshness, per source |
| `CorridorEventHub-fetch-failures-<source>` | 3+ failed fetches in `max(15 min, 3 × cadence)` | data completeness, per source. `OK` with no datapoints is correct here — the filter publishes only on failure |
| `CorridorEventHub-payload-quarantined` | any payload with no adapter | never silent |
| `CorridorEventHub-mapping-issue-spike-<source>` | mean issues **per run** > `max(25, ceiling × 2)` for 2 periods | format drift, per source. Ceiling from the catalog; thresholds today are 25 / 25 / 28 / 56 / 128 / 194 |
| `CorridorEventHub-lambda-errors` | 3+ errors in 5 min | unhandled failures |
| `CorridorEventHub-normalizer-dlq` | queue depth ≥ 1 | handler ran and raised through all retries |
| `CorridorEventHub-rule-dlq` | queue depth ≥ 1 | EventBridge never delivered the event |
| `CorridorEventHub-resolver-dlq` | queue depth ≥ 1 | resolver raised through all retries |
| `CorridorEventHub-resolver-rule-dlq` | queue depth ≥ 1 | EventBridge never delivered to the resolver |

**The staleness window is derived from cadence, not fixed.** `max(30 min, 3 ×
cadence)`, so every source today evaluates over 30 minutes — the change alters no
current alarm. It exists because a fixed 30 minutes is only correct for sources
polled faster than that: a future source on a 45-minute cadence would sit in
`ALARM` permanently while being perfectly healthy, and the only upstream guard
rejects cadences over a *day*.

**The staleness alarms use `treatMissingData: BREACHING`.** This matters: a
source going silent produces no data points at all, so an alarm treating missing
data as OK would never fire for exactly the case it exists to catch. The DLQ
alarms make the **opposite** choice (`NOT_BREACHING`) because SQS publishes
nothing for a queue that has never received a message — the healthy case. Absence
means "silent" in one place and "clean" in the other; the difference is which one
the metric can distinguish from success.

**Without `-c alarmEmail=...` nothing is subscribed and the alarms are theatre.**
The stack outputs a note saying so.

The mapping-issue threshold of 200 is deliberately loose against a ~7/run
baseline. Tune it once per-source baselines are known — a tight threshold trains
people to ignore the alarm, which is worse than no alarm. The two DLQ thresholds
are the exception at 1: every other alarm tolerates a baseline because feeds are
legitimately imperfect, but there is no acceptable steady-state rate of payloads
that never became events.

---

## Inspecting the raw zone

Every normalized record links to the exact bytes it came from, and those
bytes are immutable (S3 Object Lock, governance mode). That is what makes replay
work.

```bash
B=$(aws cloudformation describe-stacks --stack-name CorridorEventHubIngest \
  --query "Stacks[0].Outputs[?OutputKey=='RawBucketName'].OutputValue" --output text)

aws s3 ls "s3://$B/raw/source=ok-odot-wzdx/" --recursive | tail -5
aws s3 cp "s3://$B/<key>" - | python3 -m json.tool | head -40

# Fetch metadata travels with the payload:
aws s3api head-object --bucket "$B" --key "<key>" --query Metadata
```

Keys are partitioned `source=/year=/month=/day=/hour=/` for replay-by-window and
Athena scanning. The filename carries the retrieval timestamp *and* a checksum
prefix.

**The checksum makes a duplicate identifiable; it does not deduplicate.** Because
the retrieval timestamp is part of the key, identical bytes fetched five minutes
apart are two objects and two `RawPayloadStored` events. Measured against the live
raw zone: **123 checksums stored under more than one key** — 117 under
two and 6 under three — 122 of them `nm-dot-weathershare`, which is polled at 300s
and refreshes every ~600s, and one `tx-dot-wzdx`. (An earlier measurement found 107,
all New Mexico; the count grows because nothing suppresses the write.) The recoverable
volume is 521 MB of 19.83 GB, so this is a duplicate-events problem rather than a
storage one.

What the storage layout buys is **replay determinism** — the bytes
behind any record are immutable and re-parse identically. Write suppression is a
separate thing and is not implemented; the `unchanged` field the collector returns
is a placeholder, hardcoded `False`.

---

## Verifying specific claims

Things worth being able to demonstrate on demand, and the ones easiest to overstate.

**Idempotency — do not demo this as write suppression.** Invoking twice
writes **two** objects, because the retrieval timestamp is in the key. Worse,
`ok-odot-wzdx` regenerates every `end_date` as *request time + fixed offset*, so
the two payloads differ in content as well and the checksums will not even match.
```bash
npm run invoke -- ok-odot-wzdx && npm run invoke -- ok-odot-wzdx
aws s3 ls "s3://$B/raw/source=ok-odot-wzdx/" --recursive | tail -3
```
What is demonstrable today is **replay determinism** (below): the stored
bytes are immutable and re-parse identically. Claiming more than that in front of
a DOT invites the one question the storage layout cannot answer.

**Replay determinism.** The adapter tests run against real captured
payloads in `test/fixtures/`, and one asserts byte-identical output on re-parse.
```bash
npm test           # or: .venv/bin/python -m pytest tests/test_ok_adapter.py
```

**Isolation NFR.** One feed failing must not affect the others. Point a source at
a bad endpoint in `config/sources.json`, redeploy, and confirm the other source
keeps collecting and the catalog records the failure.

**Source health recording.** The collector writes health on *every*
attempt, success or failure:
```bash
CT=$(aws dynamodb list-tables --query "TableNames[?contains(@,'SourceCatalog')]" --output text)
aws dynamodb scan --table-name "$CT" --output json | python3 -m json.tool | head -30
```

---

## Shell portability traps hit while building these scripts

Recorded because each one failed **silently** on macOS, and the next person
writing an ops script here will meet at least one of them.

| Trap | Symptom | Fix |
|---|---|---|
| `GROUPS` is a bash special variable (caller's group IDs) | Assignment silently ignored; the script read back `20` (the staff gid) and tried to tail a log group named `20` | Renamed to `LOG_GROUPS`. Also avoid `SECONDS`, `LINENO`, `RANDOM`, `PWD`, `IFS`, `UID`, `PIPESTATUS`, `FUNCNAME`, `REPLY`, `COLUMNS`, `LINES` |
| BSD/macOS `grep` rejects `--line-buffered` | With stderr to `/dev/null` the whole pipeline died and printed nothing | Filter in `awk` with `fflush()` - identical on BSD and GNU |
| `aws logs tail --follow` writes nothing when stdout is a **pipe** | Zero output forever; `stdbuf -oL` and `PYTHONUNBUFFERED=1` do not help | Poll with non-follow `aws logs tail` and dedupe by line hash |
| `aws logs describe-log-groups` paginates, and `--query` runs **per page** | Pages with no match emitted blank lines into the results | `--log-group-name-prefix` filters server-side, one page |
| `cdk diff` (changeset mode) hides tag-only changes | Reported "no differences" while the deployed template genuinely lacked a tag we had just added | Use `cdk diff --method=template` when verifying tags. The changeset-based default compares resource *shape*, not every property |
| A `--` separator can survive into a script's own args | A bare `--` was read as the sourceId and sent to Lambda as one | Scripts `shift` a bare `--`; unknown sources now fail locally with the valid list |

The pattern is the same as the CloudFormation failures in ADR 0001 and
`check-ascii.sh`: **the thing that fails silently is the thing worth a guard.**

---

## Known gaps

**`aws-location-traffic` was BROKEN in the deployed stack when this was measured.**
Every poll fails, `CorridorEventHub-stale-aws-location-traffic` is in `ALARM`, and it is the
only source with any `FetchFailures` data points:

```
{"msg": "fetch_failed", "sourceId": "aws-location-traffic", "status": 0,
 "error": "FileNotFoundError: no offline corridor found. Set CEH_CORRIDOR_FILE,
 or point set_corridor_source() at the database (core/postgis.load_corridor)"}
```

This is fallout from moving the corridor into Postgres and off the Lambda bundle.
The tiled source is the one collector that needs corridor geometry *at fetch time* —
it derives N tile addresses from the corridor and signs each with SigV4 — so it is
the only source that breaks when the collector cannot load the corridor. The other
five fetch by URL and never touch the geometry until normalization, which is why
nothing else went red. Fix by pointing the collector's corridor source at the
database the way the normalizer does.

**The event store fills now, and `npm run status` should show events.** The resolver
consumes `CandidateEventProduced` and persists versioned events with audit records;
the query API serves them; the Step Functions lifecycle machine expires stale ones.

**One RUNNING execution per live event is normal.** That is the TTL timer, and the
count tracking the live event count is the healthy shape. What is not healthy is a
**FAILED** execution: it means that event's timer chain died and it will never
expire on its own again, while looking perfectly fine in every query. The
`CorridorEventHub-lifecycle-executions-failed` alarm watches for it, and
`lifecycle.ttl_expired: true` persisting in the API across several minutes is the
same finding seen from the read side — a brief `true` is just the gap between a
deadline and the tick that acts on it.

**`source_absent` is still not handled**, and it is the one lifecycle gap left: a
record disappearing from a feed does nothing until its TTL lapses. That is the
conservative failure (events linger slightly too long rather than clearing early)
and it stays that way until the snapshot-semantics calls happen — see ADR 0003.

**Dead letters are covered, and there are FOUR queues** — two per async stage,
because an undeliverable event and a handler that raised are different failures and
covering only one looks complete. Verified end to end on a deployed stack for the
normalizer pair: a failing payload dead-lettered, was inspected, and replayed
successfully from the original S3 bytes.

| Queue | Catches | Typical cause |
|---|---|---|
| `CorridorEventHubIngest-normalizer-dlq` | the handler RAN and raised through all retries | malformed payload, S3 read failure, adapter bug |
| `CorridorEventHubIngest-rule-dlq` | EventBridge could not DELIVER the event at all | throttling, permissions, function missing |
| `CorridorEventHubIngest-resolver-dlq` | resolution RAN and raised through all retries | illegal transition, persistent write conflict, store error |
| `CorridorEventHubIngest-resolver-rule-dlq` | EventBridge could not deliver the candidate | throttling, permissions, function missing |

The second of each pair is the one people configure; **the first is the one that
actually fires.**

A **resolver** dead letter is the worse silence of the two: the payload was fetched,
stored, parsed and conflated, so every upstream metric is green and the corridor is
simply missing an event.

```bash
npm run dlq            # depth of all four (should be zero)
npm run dlq-peek       # what failed and why - non-destructive
npm run dlq-replay     # re-normalize from the ORIGINAL S3 bytes
```

`npm run dlq-replay` covers **both stages**, and each message picks its own route from
what it carries rather than from which queue it came out of:

| Message carries | Replays via | Guarantee |
|---|---|---|
| `bucket` + `key` | the normalizer | Re-runs the adapter over the ORIGINAL S3 bytes. A mapping fix is applied to the exact payload that failed. |
| `candidate` | the resolver | Re-resolves the already-parsed candidate. Normalization is **not** re-run, so a mapping fix needs the raw payload instead. |

Both are safe to repeat: the content hash makes a re-run of a candidate already in
the store a no-op. The summary line reports the two counts separately,
because claiming byte-level replay for a resolver retry would overstate it.

Replay is safe to re-run: a message is deleted only after its replay succeeds, so a
still-broken adapter leaves the evidence queued. All four queues alarm at depth ≥ 1 —
there is no acceptable steady-state rate above zero, because a dead letter means a
payload we fetched and stored never became events.

**End-to-end latency is measured now, but not yet under load.** The resolver emits
`ingestLatencyMs` — collector fetch to event-in-store, spanning S3, EventBridge,
normalization, every retry, and resolution — dimensioned by `eventClass`, with a
p95 graph against the 90-second budget and the `CorridorEventHub-ingest-latency-p95` alarm
on incidents.

What has **not** happened is verification under 10× load. Nothing has generated 10×
load, so the number on the dashboard is a quiet-day number. The metric existing
makes this look verified, which is exactly why it is listed here.

Do not read `latencyMs` for this: it times the resolver invocation alone and would
report ~40 ms for a payload that spent two minutes waiting on a retry.

**Positional accuracy is 50–388 m**, median 50 m — and the 50 is a deliberate
floor in `_from_coordinate`, not a measurement. Milepost-derived extents report
160 m (~0.1 mi, typical milepost granularity). This replaced 1.6–2.2 km from the
placeholder centerline; `config/corridor.json` now carries `verified: true`,
earned by the landmark check rather than hand-set.

What is *not* yet verified: `state_segment.verified` is still false, because state
boundaries rest on measure arithmetic — `state_segment.segment` is NULL, so no
boundary has been checked geometrically. Six of Oklahoma's 18 control sections are
chained geometrically rather than sign-calibrated. See
[CORRIDOR-GEOMETRY.md](CORRIDOR-GEOMETRY.md).

**Confidence scoring runs twice** — once in the normalizer with a single source,
and again in the resolver after matching. Both are legitimate (report-level vs.
event-level trust). The API returns the event-level score with its breakdown and
the weights that produced it, so the difference is inspectable; what it does not yet
do is return the original report-level score alongside it, which is what would make
"corroboration moved this from 0.58 to 0.71" visible in one response.

---

## Cost check

At this traffic level the network dominates, so watch it rather than the compute:

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date -u -v-7d +%Y-%m-%d),End=$(date -u +%Y-%m-%d) \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[-1].Groups[?Metrics.UnblendedCost.Amount>`0.01`].[Keys[0],Metrics.UnblendedCost.Amount]' \
  --output table
```

Expect NAT Gateway and VPC endpoints to be the top lines — roughly **$88/mo**
combined, since the 4 interface endpoints are billed per AZ and `maxAzs: 2` —
then Aurora at ~$44, then **CloudWatch at ~$16**, against single-digit dollars for
Lambda, S3, and DynamoDB. Full breakdown in
[README.md](../README.md#cost); the network rationale is
[ADR 0001](adr/0001-all-lambdas-in-vpc.md). If a DOT adopter is cost-constrained,
the collectors are the defensible exception to uniform VPC placement.

**If CloudWatch is higher than ~$16, the likely cause is metric count, not log
volume.** Six of the metric filters carry a `sourceId` dimension, so every source
added to the catalog creates 6 more billable custom metrics and one more staleness
alarm. VPC Flow Logs are on by default but capture REJECT traffic only, on a
one-week log group, which is cents rather than dollars on a VPC this quiet — and
`-c flowLogs=false` turns them off if an adopter needs the line item gone. Count
what is actually publishing before assuming a leak:

```bash
aws cloudwatch list-metrics --namespace CorridorEventHub \
  --query 'length(Metrics)' --output text
```

**To stop all cost while keeping the data:**
```bash
aws scheduler update-schedule --name corridor-event-hub-ok-odot-wzdx \
  --group-name CorridorEventHubIngest-sources --state DISABLED  # ...plus its other required args
# or, to remove the expensive part:
npx cdk destroy CorridorEventHubNetwork   # only after destroying dependents
```
The raw S3 bucket is `RETAIN` by design and survives `cdk destroy`.

# Aurora PostGIS — Deploy and Bootstrap

The spatial stack (`lib/spatial-stack.ts`) implements the recommended conflation
path from [ADR 0002](adr/0002-conflation-behind-a-swappable-interface.md).

**Status: DEPLOYED and bootstrapped.** Schema applied, corridor loaded,
cross-validated against `LocalConflator`.

| | |
|---|---|
| Cluster | `corridoreventhubspatial-clustereb0386a7-uoocfviuzpjg` |
| Engine | PostgreSQL **18.4** |
| PostGIS | **3.6.3**, GEOS 3.14.1, PROJ 9.5.0 |
| Database | **`corridoreventhub`** (no hyphen - `corridor-event-hub` does not exist) |
| User | `corridoreventhub_admin` |
| Secret | `corridor-event-hub/spatial-db-credentials` |
| Data API | enabled, so the console Query Editor works |

**The 18.x PostGIS risk is resolved.** `CREATE EXTENSION postgis` succeeded and
`postgis_full_version()` reports 3.6.3 - no need to fall back to 16.14.

### Verified against the live database

```
milepost_to_measure('I-40','AZ',359.5)  ->  359.500
milepost_to_measure('I-40','NM',0)      ->  359.500   <- same place
milepost_to_measure('I-40','AZ',9999)   ->  NULL      <- not clamped
conflate_point('I-40', -101.83, 35.20)  ->  803.482 mi, 841 m off-corridor
```

`LocalConflator` puts that same point at **803.482** - identical to the three
decimal places both round to. That is expected rather than lucky: both now READ the
measure by interpolating the same per-vertex values, PostGIS via
`ST_InterpolatePoint` on `centerline_m` and the Python path via the parallel
`measures` array in `reference/corridor.json`.

**Read the previous version of this claim as a caution.** It said the two agreeing
to within 0.235 mi was "independent confirmation that neither is wrong". It was
not. Both were computing the same fraction-times-`total_miles` formula correctly
against the same distorted placeholder geometry, and both were about 19 miles out
in the same direction. Two implementations agreeing tests the implementations, not
the model. The check that actually has an independent source of truth is
`npm run db-landmarks`, against agency-surveyed milepost markers - see
[CORRIDOR-GEOMETRY.md](CORRIDOR-GEOMETRY.md).

---

## 1. Deploy the cluster

```bash
cd src
npm run check                         # every gate, including the live engine-version check
npx cdk deploy CorridorEventHubSpatial
```

Takes roughly 10–15 minutes. Nothing else needs redeploying — the stack is
additive and touches no existing resource.

Optional context flags:

```bash
npx cdk deploy CorridorEventHubSpatial \
  -c dbMinAcu=0.5 \
  -c dbMaxAcu=4 \
  -c dbDeletionProtection=true    # recommended for anything but a prototype
```

### What you get

| | |
|---|---|
| Engine | Aurora PostgreSQL **18.4**, Serverless v2 |
| Capacity | 0.5–4 ACU, single writer, no reader |
| Placement | `PRIVATE_ISOLATED` subnets — **no internet route at all** |
| Access | Only from the Lambda security group, port 5432 |
| Encryption | Storage encrypted; credentials in Secrets Manager |
| Backups | 7-day retention, 08:00–09:00 UTC window |
| Removal | `SNAPSHOT` on delete, deletion protection off by default |

**Cost: roughly $44/mo at the 0.5 ACU floor**, on top of the ~$88/mo network
(ADR 0001 — 4 interface endpoints billed per AZ across `maxAzs: 2`, plus NAT).
Serverless v2 does not scale to zero at `minCapacity: 0.5`, and even at `0` the
ingest cadence polls every 60–600s, so the cluster never idles long enough for
auto-pause to help. Budget for always-on. Full bill in
[README.md](../README.md#cost). Verify regional pricing before quoting to a DOT.

---

## 2. Apply or update the schema

```bash
npm run db-info           # which account/cluster/secret am I pointed at?
npm run db-migrate-plan   # what WOULD be applied. Changes nothing. Run this first.
npm run db-migrate        # apply it
npm run db-history        # what has been applied, and when
npm run db                # smoke test: postgis, row counts, LRS invariant
```

`npm run db-migrate` invokes a **migration Lambda inside the VPC**
(`corridor_event_hub/handlers/db_migrate.py`, wired in `lib/spatial-stack.ts`). It
applies every pending file in `sql/`, records each one in `schema_migration`, and
wraps each file in a single transaction.

### A NEW MIGRATION NEEDS A DEPLOY BEFORE IT CAN BE APPLIED

The Lambda reads the migrations from **its own bundle**, at `/var/task/sql`. That
is deliberate - a deployed function cannot be a version behind the SQL it is meant
to run - but it has a consequence that surprises people exactly once:

```bash
# after adding or regenerating a file in sql/
# WITHOUT the deploy, the next line cannot see the new file
# (synth rebuilds the bundle itself, so there is no separate `npm run bundle` step)
npx cdk deploy CorridorEventHubSpatial
# now it appears as pending
npm run db-migrate-plan
npm run db-migrate
```

Comments are on their own lines above, not trailing the commands, and that is
deliberate: **interactive zsh does not treat `#` as a comment** unless
`interactive_comments` is set. Pasting `npm run db-migrate-plan  # like this` into zsh
passes `#` to make as a target and gets `No rule to make target '#'`. Bash strips
it; the default macOS shell does not.

Skip the deploy and `db-migrate-plan` reports `NOTHING TO APPLY` while sitting
next to a repository that plainly contains a new migration. It is not lying: the
function really has no such file. `npm run db-migrate-plan` lists everything it
DISCOVERED for exactly this reason - compare that list against `ls sql/` and the
mismatch is visible immediately.

The consequence to watch as the corridor grows: every migration's data ships in
every function's bundle. `003-nbi-structures.sql` alone is 5.6 MB, which took the
bundle from 54 MB to 60 MB. Against the 250 MB unzipped limit that is fine, and
it is the reason to keep bulk data loads out of `sql/` once they stop being
reference-sized.

Everything else - ad-hoc queries, `--file`, the smoke test - still goes through
`scripts/db.sh` over the Data API. Both **discover the cluster and secret per
account** and neither hardcodes an ARN: the stack has been deployed to two
accounts now, and pasted ARNs fail in a way that looks like an unreachable
database rather than a wrong target. Both follow `AWS_PROFILE` /
`AWS_DEFAULT_REGION`, so they target whatever account your shell (or
`.vscode/settings.json`) points at.

```bash
./scripts/db.sh "SELECT * FROM state_segment ORDER BY corridor_offset"
```

### Two ways to apply SQL, and when each is right

|  | `npm run db-migrate` | `npm run db-bootstrap` / `db.sh --file` |
|---|---|---|
| Path | Lambda in the VPC, port 5432 | RDS Data API, HTTPS |
| Needs the Data API | no | **yes** |
| Needs a deployed bundle | yes (`npm run deploy`) | no |
| Records what it applied | **yes**, in `schema_migration` | no |
| On a failed statement | rolls the whole file back | keeps going, leaves it half-applied |
| Applies | every pending file, in order | the one file you name |

**Use `db-migrate`.** `db-bootstrap` is kept for the one case it is still better
at: applying a schema to a cluster whose migration function has not been deployed
yet - and for a production deployment that sets `enableDataApi: false`, it stops
working entirely while `db-migrate` does not.

Mixing them is safe but not free: `db-bootstrap` records nothing, so a later
`db-migrate` will apply `001-init.sql` again. Harmless only because that file is
idempotent.

### How to change the schema

**`001-init.sql` is the current shape of the schema, and is declared
`-- migration: repeatable`** - it is written entirely as `CREATE TABLE IF NOT
EXISTS`, `CREATE OR REPLACE FUNCTION`, `CREATE INDEX IF NOT EXISTS`, so for
anything additive, edit that file and re-run:

```bash
# 1. edit sql/001-init.sql
npm run db-migrate-plan   # shows it as `changed`
npm run db-migrate        # re-applies it; existing objects are untouched
npm run db                # confirm the invariants still hold
```

That covers new tables, new functions, new indexes, and changed function bodies.

**For anything NOT idempotent** - dropping a column, changing a type, adding a
`NOT NULL` to a populated table, renaming - add a numbered file instead:

```bash
sql/003-add-tmc-segments.sql
sql/004-backfill-clearance-units.sql
```

Then `npm run db-migrate`. Numbered files are **run-once by default**: applied
exactly once, then frozen. Do not add the `repeatable` directive to one unless
every statement in it genuinely tolerates a second application.

### There IS a migration tracking table now

`schema_migration` records `filename`, `checksum`, `statements`, `repeatable`,
`applied_at`, `applied_ms`, and `applied_by`. `applied_by` is
`<function-name>:<request-id>`, so a row points at the exact CloudWatch log stream
that wrote it.

The runner creates this table itself rather than `001-init.sql` defining it: the
runner has to READ it to decide whether `001` needs applying, so a tracking table
defined inside a migration cannot exist the first time it is needed.

**The checksum is the point.** A filename-only record cannot distinguish "already
applied" from "applied, then edited" - and the second is the case that leaves a
cluster and a repository silently disagreeing about the schema. So:

| | run-once file | repeatable file |
|---|---|---|
| Never applied | apply | apply |
| Checksum matches | skip | skip |
| Checksum differs | **DRIFT - refuse, and say why** | re-apply |

Drift blocks the **whole** run before anything is applied, rather than applying
what it can. A partial migration set is how a schema ends up in a state no file
describes. `npm run db-migrate-plan` reports drift instead of failing, since that is
the one thing you ran it to find out.

The checksum covers the whole file, comments included, so a comment-only edit to a
run-once file counts as drift. Deliberate: the alternative is a normaliser
deciding which edits "don't count", which is a worse thing to be wrong about. The
remedy is in the error message - put the change in a new numbered file, or update
the checksum by hand and say so in the commit.

### Two things to know before editing

**Statement splitting is dollar-quote and string aware, and shared.** The Data API
and the Lambda both take one statement per call, and a naive `split(';')` destroys
every function body. One implementation, in
`corridor_event_hub/core/migrations.py:split_statements`, used by both and unit-tested
against the real `sql/` files. It handles `$tag$` bodies as well as `$$`,
semicolons inside string literals and comments, nested block comments, and `$1`
placeholders. **The old "stick to `$$`" restriction is gone.**

**`db.sh --file` still keeps going after a failed statement**, which can leave a
file half-applied. `npm run db-migrate` does not - Postgres has transactional DDL and
the runner uses it, so a file either applied or it did not.

### Concurrency

Two guards, because they cover different things. `reservedConcurrentExecutions: 1`
stops Lambda running two invocations of this function at once. A Postgres session
advisory lock stops what that does not: an operator running `npm run db-migrate`
while CI does the same from another account against the same cluster. The second
one fails immediately with "another migration is already running" rather than
queueing - waiting would hold a connection until the Lambda timed out and show the
operator a timeout instead of the reason.

### Two bugs the first bootstrap surfaced

**`(measure_to_milepost(...)).*` collided with `bridge_structure.state`:**
`ERROR: column "state" specified more than once`. The record-expansion syntax
emits its own `state` column. Rewritten as `LEFT JOIN LATERAL` with explicit
aliases (`nbi_state` / `lrs_state`).

**The database is `corridoreventhub`, not `corridor-event-hub`.** The hyphenated form gives
`ERROR: database "corridor-event-hub" does not exist`. The hyphen appears only in Secrets
Manager paths.

### Getting to the database at all

Aurora sits in an isolated subnet with no public endpoint, so a laptop `psql`
cannot reach it. Two routes cover everything, and **neither needs a bastion host
to stand up, patch, and justify in a security review**:

| Want to | Use |
|---|---|
| Apply a schema change | `npm run db-migrate` - the Lambda, inside the VPC |
| Run a query | `./scripts/db.sh "SELECT ..."`, or the console Query Editor |

That is why the network stack creates no EC2 instance and there is no SSM
port-forwarding path: nothing needs one. It is also why the migration Lambda was
worth building rather than tunnelling - it answered the connectivity question for
`PostgisConflator`, which needs exactly the same VPC placement and secret access.

The one thing neither route gives you is a persistent interactive session. If a
task genuinely needs `psql` - a `\copy`, or a long ad-hoc investigation - SSM
port forwarding to an SSM-managed instance is the way to add one, and it should be
added deliberately and removed afterwards rather than left standing.

---

## 3. Schema reference

Three tables, one view, four functions. [`sql/001-init.sql`](../sql/001-init.sql)
is authoritative — if this section and that file disagree, the file is right and
this section is stale.

**Everything here is reference data or derived-and-rebuildable. No event state
lives in Postgres** — observations go to the DynamoDB `EventStore`
(`lib/ingest-stack.ts`), which is the division
[ADR 0002](adr/0002-conflation-behind-a-swappable-interface.md) draws between the two
stores. Dropping and rebuilding this database
loses nothing that cannot be re-derived from `reference/corridor.json` and an NBI
extract.

Only the `postgis` extension is installed. `postgis_topology` is deliberately
absent: linear referencing needs just `ST_LineLocatePoint`, `ST_LineSubstring`,
and `ST_Intersection` from core PostGIS, and topology would add schema surface
for no benefit.

### `corridor` — the route definition

One row per route; today just `I-40`. **The corridor is configuration**,
not something the system discovers.

| Column | Type | Notes |
|---|---|---|
| `route` | `text` | Primary key. `'I-40'`. |
| `description` | `text` | Free text. |
| `centerline` | `geography(LineString, 4326)` | **NOT NULL.** Stored SRID 4326 to match every source feed; all length and distance math casts to `geography` so results come back in metres, not degrees. |
| `buffer_meters` | `integer` | NOT NULL, default `1600` (≈1 mile). The on-corridor test radius used by `conflate_point`. |
| `centerline_m` | `geometry(LineStringM, 4326)` | Nullable. The same line with the corridor measure in its **M** dimension. `geometry`, not `geography`, for two reasons: `geography(LineString, 4326)` cannot hold a measure dimension, and `ST_InterpolatePoint` needs geometry. `conflate_point` prefers this column when populated. |
| `verified` | `boolean` | NOT NULL, default `false`. **The geometry tripwire** — `false` means the geometry is a placeholder and positions are accurate to miles, not metres. Currently **`true`**, written by `scripts/fetch-arnold.py` only when the seven-landmark check passes, so a bad fetch cannot set it. |
| `total_miles` | `numeric(10,3)` | Configured corridor length. `conflate_point` scales `ST_LineLocatePoint`'s 0–1 fraction by this, so a wrong value silently skews every measure. |
| `created_at` | `timestamptz` | NOT NULL, default `now()`. |
| `updated_at` | `timestamptz` | NOT NULL, default `now()`. Not maintained by a trigger — whoever updates a row sets it. |

### `state_segment` — per-state milepost offsets

**The table that makes cross-state dedup possible**. See the
explanation in section 4.

| Column | Type | Notes |
|---|---|---|
| `route` | `text` | NOT NULL, `REFERENCES corridor(route) ON DELETE CASCADE`. |
| `state` | `char(2)` | NOT NULL. Uppercase — `milepost_to_measure` applies `upper()` to its argument, but nothing normalizes on insert. |
| `state_mp_min` | `numeric(10,3)` | NOT NULL. This state's lowest milepost on the route. |
| `state_mp_max` | `numeric(10,3)` | NOT NULL. Highest. |
| `corridor_offset` | `numeric(10,3)` | NOT NULL. Corridor measure at `state_mp_min`. The whole point of the table. |
| `segment` | `geography(LineString, 4326)` | Nullable. Where this state's extent actually sits on the centerline, so a boundary can be checked geometrically rather than trusted from the numbers. Currently unpopulated — see [CORRIDOR-GEOMETRY.md](CORRIDOR-GEOMETRY.md). |
| `verified` | `boolean` | NOT NULL, default `false`. Same meaning as `corridor.verified`, per state. |

Primary key `(route, state)`. Constraints: `state_mp_range` (`state_mp_max >
state_mp_min`) and `corridor_offset_nonneg` (`corridor_offset >= 0`).

### `bridge_structure` — NBI structures

Reference data, refreshed annually. Class 7, `dimensional_restriction`. The three
NBI data traps are handled at **ingest**, not here (see
[DATA-SOURCES.md](DATA-SOURCES.md)) — but the schema makes the dangerous
one impossible to reintroduce.

| Column | Type | Notes |
|---|---|---|
| `structure_number` | `text` | Primary key. Note this is not state-scoped, so two states colliding on a structure number would overwrite each other. Not observed yet, but the NBI key is properly `(state, structure_number)`. |
| `state` | `char(2)` | NOT NULL. The NBI record's own state. |
| `route` | `text` | Nullable. No FK to `corridor` — off-corridor structures are loadable. |
| `min_vert_clearance_m` | `numeric(6,2)` | **NULL means UNKNOWN.** NBI encodes "no restriction" as the sentinel `99.99`, and sometimes `0`; both must become NULL on the way in. Guarded by `clearance_sane` below. |
| `location` | `geography(Point, 4326)` | Nullable. As-published NBI coordinates. |
| `corridor_measure` | `numeric(10,3)` | Conflated position along the route. NULL until the structure is matched to the corridor. |
| `facility_carried` | `text` | What crosses over. |
| `features_intersected` | `text` | What is crossed. |
| `nbi_year` | `integer` | NOT NULL. Which annual extract this row came from. |
| `raw` | `jsonb` | NOT NULL. **Nothing dropped** — the full NBI record, all ~120 fields. |
| `ingested_at` | `timestamptz` | NOT NULL, default `now()`. |

`CONSTRAINT clearance_sane CHECK (min_vert_clearance_m IS NULL OR
(min_vert_clearance_m > 0 AND min_vert_clearance_m < 30))` — a sentinel value
**cannot be inserted**. See section 4 for why this matters more than it looks.

| Index | Definition |
|---|---|
| `bridge_location_gix` | GIST on `location`. Spatial lookup. |
| `bridge_measure_idx` | `(route, corridor_measure)` where `corridor_measure IS NOT NULL`. "What is ahead of me." |
| `bridge_clearance_idx` | `(route, min_vert_clearance_m)` where `min_vert_clearance_m IS NOT NULL`. The over-height truck query. |

### `corridor_clearances` (view)

`bridge_structure` joined back through the LRS, filtered to rows an over-height
routing decision can actually use: `min_vert_clearance_m IS NOT
NULL AND corridor_measure IS NOT NULL`, ordered by `corridor_measure`.

| Column | Source |
|---|---|
| `structure_number` | `bridge_structure` |
| `nbi_state` | `bridge_structure.state` — the NBI record's state |
| `route` | `bridge_structure` |
| `min_vert_clearance_m` | `bridge_structure` |
| `min_vert_clearance_ft` | computed, `× 3.28084` rounded to 2dp |
| `corridor_measure` | `bridge_structure` |
| `lrs_state` | `measure_to_milepost()` — the state the LRS puts the measure in |
| `milepost` | `measure_to_milepost()`, rounded to 3dp |
| `facility_carried` | `bridge_structure` |
| `nbi_year` | `bridge_structure` |

The two state columns are separate on purpose: `nbi_state` and `lrs_state`
disagreeing is a conflation bug worth seeing rather than hiding. It is also why
the view uses `LEFT JOIN LATERAL` with explicit aliases — see the bug note in
section 2.

**Rows absent from this view are unknown, NOT unrestricted.** Absence of a record
is not evidence of clearance.

### Functions

All four are `LANGUAGE sql STABLE`.

| Function | Returns | Notes |
|---|---|---|
| `milepost_to_measure(route, state, milepost)` | `numeric` | State milepost → corridor measure. Pure arithmetic over `state_segment`, but lives next to the offset table so the two cannot drift. **Returns NULL for an out-of-range milepost rather than clamping**. |
| `measure_to_milepost(route, measure)` | `TABLE (state char(2), milepost numeric)` | The inverse, for rendering back into a state's own reference. `LIMIT 1`, so a measure exactly on a state line resolves to whichever row the planner returns first. |
| `conflate_point(route, lon, lat)` | `TABLE (corridor_measure, offset_meters, on_corridor)` | Real `ST_LineLocatePoint` projection, not the 400-point sampling `LocalConflator` uses. `on_corridor` is `ST_DWithin` against `buffer_meters`. |
| `conflate_polygon(route, geojson)` | `TABLE (begin_measure, end_measure, on_corridor)` | Polygon × corridor, for NWS county-sized alert polygons. `ST_Intersection` then `ST_LineMerge`; measures are NULL when the intersection is empty. |

`conflate_point` and `conflate_polygon` both scale by `corridor.total_miles`, so
they inherit any error in that value.

---

## 4. What the schema gives you

### The table that makes cross-state dedup possible

```sql
SELECT state, state_mp_min, state_mp_max, corridor_offset FROM state_segment;
```

`state_segment` is the offset table. Mileposts restart at every state
line, so AZ MP 359.5 and NM MP 0 are the same physical place — only after
applying `corridor_offset` do they become comparable numbers. This is
the thing ADR 0002 means by "the LRS becomes inspectable data."

### Conflation in SQL

```sql
-- Coordinate -> corridor measure. The real ST_LineLocatePoint projection,
-- not the 400-point sampling the shapely implementation uses.
SELECT * FROM conflate_point('I-40', -101.83, 35.20);

-- Milepost -> measure, and back.
SELECT milepost_to_measure('I-40', 'NM', 100);   -- 459.5
SELECT * FROM measure_to_milepost('I-40', 459.5); -- NM, 100

-- Polygon x corridor, for NWS alerts.
SELECT * FROM conflate_polygon('I-40', '<geojson>');
```

`milepost_to_measure` returns **NULL** for an out-of-range milepost rather than
clamping — an invalid input is a mapping issue, not something to coerce.

### The over-height truck query

```sql
SELECT * FROM corridor_clearances WHERE min_vert_clearance_ft < 14.0;
```

`corridor_clearances` only includes structures with a **known** clearance and a
resolved position. **Rows absent from it are unknown, not unrestricted** —
absence of a record is not evidence of clearance.

### A constraint that prevents a whole class of bug

NBI encodes "no restriction" as the sentinel `99.99`, and sometimes `0`. Read
literally, `99.99` is 99.99 metres of clearance and every over-height check
passes. So:

```sql
CONSTRAINT clearance_sane CHECK (
  min_vert_clearance_m IS NULL
  OR (min_vert_clearance_m > 0 AND min_vert_clearance_m < 30)
)
```

A sentinel value **cannot be inserted**. The ingest must map it to NULL, and the
database enforces that rather than trusting the adapter. Of 409 I-40 structures
in Oklahoma, only **30** have a real clearance.

---

## 5. Four things deliberately not done

**`cloudwatchLogsRetention` is not set.** It makes CDK inject a singleton
custom-resource Lambda that runs *outside* the VPC, violating ADR 0001. The VPC
check caught it. Consequence: the exported `postgresql` log group retains
forever by default — set retention on the group out of band, or accept it for a
prototype.

**The engine version is pinned with `of('18.4', '18')`, not the CDK enum.**
CDK 2.173's `AuroraPostgresEngineVersion` stops at `VER_16_6` — it cannot express
18.x at all, and 16.6 has since been **retired** by AWS. Using a stale enum value
synths cleanly and fails at CREATE.

**Parameter scope moved in PG18.** `log_min_duration_statement` and
`shared_preload_libraries` are INSTANCE-level on `aurora-postgresql18`, not
cluster-level — verified against `describe-engine-default-parameters`. An earlier
revision set them on a cluster parameter group, where the engine never reads
them. That fails *silently*: no deploy error, the settings just never apply.

`scripts/check-engine-versions.sh` now verifies both the version availability and
the parameter scope against the live API on every `npm run check`.

**The migration runner does not verify the server certificate.** Its connection is
encrypted - and an explicit TLS context is passed precisely so that pg8000 cannot
do what it does by default, which is try TLS and **silently fall back to
plaintext** if the server does not offer it. What is missing is verification: the
Amazon RDS root CA is not in the Lambda image's trust store, so there is nothing to
check the certificate against.

The fix is one file and one environment variable:

```bash
curl -o certs/rds-ca.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
# then set SPATIAL_DB_CA_BUNDLE=/var/task/certs/rds-ca.pem and ship certs/ in the bundle
```

Not done by default because it makes the build fetch a rotating artifact from the
network, and a build that silently produces a weaker posture when offline is worse
than one that is consistently explicit. The residual risk is an in-VPC
machine-in-the-middle between a Lambda subnet and an isolated subnet reachable only
from one security group. The runner **logs its TLS posture on every invocation** so
this is visible in the output rather than discovered in a review.

**The migration runner is not invoked by `cdk deploy`.** A custom resource could
do it, and does not, for two reasons: the CDK Provider framework injects its own
Lambdas that run outside the VPC unless explicitly placed (ADR 0001 - and
`scripts/check-vpc.sh` would fail the build), and a schema change that happens as a
side effect of deploying application code is the wrong default for a system a state
DOT operates. Migrating is a decision, so it is a command. Wiring it into a
pipeline stage is the right way to automate it; the function is exposed as
`SpatialStack.migrationFunction` for exactly that.

---

## 6. Conflation, switched over

**The database is on the request path.** All five items below are done; what runs in
the deployed normalizer is `HybridConflator`, and the corridor itself now comes from
Postgres rather than from a file in the bundle.

1. ~~**Bootstrap Lambda** to apply `sql/001-init.sql`.~~ **DONE** - `npm run db-migrate`.
   It confirmed the two things the conflator needed to know: that a VPC Lambda reaches
   the cluster on 5432 with credentials from Secrets Manager, and that a pure-Python
   driver in the bundle is enough.
2. ~~**Load the corridor**~~ — **done.** `sql/002-corridor-real.sql`, generated by
   `scripts/fetch-arnold.py` from the federal NTAD National Highway System: 11,924
   vertices, `centerline_m` calibrated, measured `state_segment` offsets. Rebuild with
   `npm run corridor`, apply with `npm run db-migrate`, verify with `npm run db-landmarks`. Two
   things to know before regenerating: the geometry is staged in chunks and assembled
   with `string_agg` because the Data API caps statement size well below 346 KB of
   WKT; and the Lambda reads `sql/` from **its own bundle**, so a redeploy is required
   before a regenerated file can apply.
3. ~~**`PostgisConflator`**~~ — **done**, in `corridor_event_hub/core/postgis.py`, plus
   `load_corridor(route)` so the corridor is read from the database instead of a
   bundled file. That removed the single-corridor limit: everything is keyed by route.
4. ~~**Connection handling**~~ — **done for this concurrency**, in
   `corridor_event_hub/core/dbconn.py`: one connection per container with a liveness check,
   shared by the conflator and the migration runner. **RDS Proxy is still the answer
   if anything fans out** - the failure mode is "too many connections" under exactly
   the load you were hoping to serve.
5. ~~**NBI loader**~~ — **done**, `scripts/fetch-nbi.py` → `sql/003-nbi-structures.sql`.
   1,543 structures, 387 with a known clearance. All three documented traps handled,
   plus two the catalog had not recorded: a second family of sentinels (30.48 m is
   exactly 100 ft) and the fact that **item 10 is the wrong field** - 96% of it is
   "no restriction" and the real limits are in item 54B.

### What each implementation is now for

| | `LocalConflator` | `PostgisConflator` |
|---|---|---|
| Runs | in the Lambda process | in the database |
| Coordinate / milepost / linestring | **used** - no round trip per record | available, no accuracy gain |
| Polygon (NWS alerts) | fallback, 3.1 mi floor | **used** - exact `ST_Intersection` |
| Corridor geometry | `reference/corridor.json` | `corridor` + `state_segment` |
| Needs AWS | no | yes |

`LocalConflator` **was `ShapelyConflator`** until it stopped using shapely — see
[ADR 0002](adr/0002-conflation-behind-a-swappable-interface.md). It is still the
whole offline story: `npm run probe`, `npm run ui` and the test suite run with no AWS
account, which is the property that made keeping both worthwhile.

### What building the second one found

A bug in the first. `conflate_polygon` was deriving its measure from
`ST_LineLocatePoint × total_miles` — the fraction-scaling formula this schema
elsewhere calls systematically biased — and was **5.7 miles out** on a test polygon
near Amarillo. `conflate_point` had been fixed to prefer `centerline_m`;
`conflate_polygon` never was. Neither implementation's own tests could have caught
it, because each was self-consistent. See the comment above the function in
`sql/001-init.sql` for the four measurements.

The same comparison also caught the corridor's `verified` flag disagreeing with
itself — `true` in `reference/corridor.json`, `false` in the database, same geometry
from the same generator. `002`'s `ON CONFLICT DO UPDATE` list omitted the column, so
a corridor first loaded as unverified stayed unverified no matter what later runs
said. Since the deployed pipeline reads the database, it would have withheld
positions the offline tools were publishing. Fixed in the generator; re-apply with
`npm run db-migrate`.

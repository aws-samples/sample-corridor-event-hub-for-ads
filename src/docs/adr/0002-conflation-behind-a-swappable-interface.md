# ADR 0002 — Spatial conflation sits behind a swappable interface

Status: Accepted

## Context

Every source locates events differently: coordinates, state mileposts, county
polygons, TMC segments, or prose. All of it must resolve to one corridor linear
reference. That conflation is the only genuinely spatial work in
the system.

Being precise about the spatial surface changes the sizing:

**Spatial — write path and reference data only:**
corridor centerline and per-state milepost offsets; coordinate → measure
projection; NWS polygon × corridor intersection; RWIS sensor snapping; TMC
segments; NBI structures; parking facilities.

**Not spatial at all:** event versions, audit trail, lifecycle state, confidence
scoring, provenance — all keyed by `event_id`.

**The part that surprises people:** once conflation yields
`route + begin_measure + end_measure + direction`, the spatial problem becomes a
numeric one. Look-ahead is a range scan. Dedup proximity is
range overlap. State-line matching is comparing numbers after offset
normalization. Reference data is small — ~1,200 corridor miles, a few hundred
bridges, a few thousand TMC segments — small enough to fit in a Lambda's memory.

## Decision

Conflation is defined by a narrow interface:

```python
class Conflator:
    def conflate(self, spatial_input: SpatialInput) -> ConflationResult:
        ...  # -> route + measure range
```

**Aurora Serverless v2 PostgreSQL + PostGIS is the recommended production
implementation**, and it is now built: `PostgisConflator`, in
`corridor_event_hub/core/postgis.py`. `LocalConflator` — conflating in the Lambda process
against corridor geometry it holds — is the working alternative, and is what keeps
`npm run probe`, the UI and the test suite running with no AWS account at all.

**`LocalConflator` was called `ShapelyConflator`** until it stopped using shapely.
The library was there for a single point-in-polygon test, brought numpy with it, and
the two were 85% of the deployment bundle; twenty lines of even-odd ray casting
replaced them, verified against shapely over 20,000 random points. shapely remains a
DEV dependency so that comparison keeps running, and is not deployed.

**What runs in production is neither one alone.** `HybridConflator` sends polygon
conflation to the database and keeps per-record point conflation in process - see
"the split, and why" below.

## Consequences

**The decision is reversible.** Swapping implementations is a constructor change,
not a rewrite. That matters because Aurora/VPC setup eating the first week is a
common failure, and the fallback stays available through week 3.

**Two stores, deliberately.** DynamoDB is the system of record for events;
Aurora holds corridor reference data and does write-path geometry. Real
complexity, justified below.

**THE CORRIDOR ITSELF IS NOW REFERENCE DATA IN THE DATABASE, not a file in the
deployment bundle**, and this turned out to matter for a reason this ADR did not
anticipate. `config/corridor.json` was **singular by construction** — one route, one
centerline, one list of states, loaded into a module-level singleton at import. A
corridor service intended to grow past one interstate could not express two
corridors at once, whatever the geometry was stored in. The database always could:
every SQL function takes a route and `state_segment` is keyed `(route, state)`.

So the move was not a storage preference, it was removing a cardinality limit from
the architecture: adopting a different route must not require a core code change.
The singleton is gone; corridors are loaded per route from an injected source, and `reference/corridor.json` remains only
as the OFFLINE source for the probe, the UI and the tests.

`scripts/build-lambda.sh` fails if a corridor JSON appears in a bundle. A stale
bundled corridor would be preferred over none and would place events against
geometry the database has since replaced.

**This implementation originally shipped a placeholder centerline** — ~40 control points,
accuracy ±several miles, `verified: false`, not publishable. It has been replaced
with geometry from the federal NTAD National Highway System plus a small Oklahoma
milepost calibration, and `reference/corridor.json` now carries `verified: true`.
Open Question 5 is answered in practice: one corridor measure backed by publishable
federal geometry, with the state-specific offsets applied where federal data stops
short. See [CORRIDOR-GEOMETRY.md](../CORRIDOR-GEOMETRY.md).

**This ADR's central claim survived the change, and was tested by it.** The two
implementations now agree EXACTLY on `conflate_point('I-40', -101.83, 35.20)` —
803.482 from both. But note what the earlier agreement was worth: at 0.235 mi
apart on the placeholder, they looked mutually confirming while both were ~19 miles
wrong, because both computed the same biased formula correctly. Agreement between
implementations tests the implementations, not the model. The reversibility this
ADR argues for is real; the cross-validation figure alone was never the evidence.

## Why PostGIS is recommended

1. **Adoption legibility — the strongest argument, and it is about the
   deliverable rather than the runtime.** State DOT GIS shops already work in
   PostGIS and Esri. A reference architecture that expresses linear
   referencing in the idiom its adopters use is more likely to be adopted than
   one hiding the LRS inside application code.
2. **SQL for researchers.** A university project whose output includes analysis,
   not only a service.
3. **Building the offset tables.** Deriving state-line offsets is materially
   easier interactively in PostGIS than in code, whatever executes it later.
4. **The LRS becomes inspectable data** rather than logic buried in a Lambda,
   which matters for reproducing conflation and for the ADR trail.

### The split, and why

`ST_Intersection` against a real centerline is more accurate than 400-point
sampling for the NWS polygon case — the one place where the two implementations
differ in **capability** rather than convenience. The sampler publishes its own
accuracy floor as 4,992 m (3.1 miles), the worst positional accuracy in the system,
on the class whose extents are the largest.

Everything else is a tie, measured rather than assumed: both implementations read the
same per-vertex LRS measures — PostGIS from `centerline_m`, the local path from the
parallel `measures` array — and agree to three decimals on the live cluster
(803.482). So `HybridConflator` sends polygons to the database and keeps coordinate,
milepost and linestring conflation in process, where they cost no round trip and no
write-path dependency on Aurora. If the database is unreachable the polygon path
falls back to sampling and logs the degradation rather than failing the payload.

**Building the second implementation is what found the bug in the first.**
`conflate_polygon` was deriving its measure from `ST_LineLocatePoint × total_miles`
— the fraction-scaling formula this schema elsewhere calls systematically biased —
and was **5.7 miles out**. Nothing but a second implementation computing the same
number a different way could have shown that; neither path's own tests could.

## When to take the alternative

Adopt `LocalConflator` alone and drop the database if: the schedule is behind in week
1, nobody on the team is comfortable operating Postgres, or VPC networking starts
eating days. It removes a database, a VPC dependency, and its ops burden, and it is
pure Python — no compiled dependency, nothing to build for the wrong platform.

**Two costs, now that both exist and have been compared.** The first is adoption
legibility, as originally stated. The second was not obvious in advance: without the
database the polygon path is the sampler, so NWS alert extents carry a 3.1-mile
floor, and there is nowhere to put reference data that cannot be bundled — the 1,543
NBI structures behind the over-height query are 5.6 MB of SQL.

Still a **viable architecture, not a degraded one**, for a deployment that does not
need class 7 and can accept coarse alert extents.

## Alternatives considered

**PostGIS for everything, no DynamoDB.** Simpler operationally — one store. The
event store needs conditional-write idempotency and cheap append-only
versioning at high write rates, which Postgres does less well for this access
pattern. This is the simplification to make if time runs tight.

**DynamoDB only, conflation in Lambda.** Cheapest and simplest. Loses researcher
SQL and adoption legibility. This is `LocalConflator` plus deleting Aurora, and it
is the fallback above.

**A geospatial service (Location Service, Athena geospatial).** Neither is shaped
for linear referencing along a custom corridor with per-state milepost offsets.
Not evaluated further.

# Corridor geometry: what was wrong, and what fixed it

Investigated, fixed, and verified against the live database.

The placeholder centerline was wrong in two independent ways, one of which was
silently discarding events. Both are now fixed by real geometry from the four
states' own linear referencing systems. This document is the record of what was
wrong, how it was proved, and what is still open - in that order, because the
diagnosis is more reusable than the fix.

---

## What conflation has to do

Events arrive as lat/lon. Nobody at a DOT talks in lat/lon - they talk in
mileposts. So every event has to be answered: *how many miles along I-40 is
this?* That is linear referencing, and `conflate_point()` is where it happens.

Two things can go wrong, and both did:

- the **geometry** can be too coarse to say where the road is
- the **measure** can be derived wrongly even when the geometry is right

The second is harder to see and was the worse of the two.

---

## The headline, before and after

```sql
SELECT route, verified, total_miles,
       ROUND((ST_Length(centerline)/1609.34)::numeric,1) AS geom_miles,
       ST_NPoints(centerline_m) AS control_points
FROM corridor;
```

| | placeholder | now |
|---|---|---|
| control points | 40 | **11,894** |
| geometry length | 1,165.6 mi | **1,238.6 mi** |
| corridor measure | derived from a fraction | **0 -> 1,240.698, read from M** |
| average leg | 29.9 mi | **0.07 mi** |
| worst leg | 74.7 mi | **0.75 mi** |
| landmark error | -7 to -28 mi, all negative | **-0.32 to +0.03 mi, mixed sign** |
| landmarks rejected as off-corridor | 2 of 7 | **0 of 7** |

---

## Problem 1: 40 points cannot describe 1,241 miles of road

`total_miles: 1241` came from the corridor document's per-state mileposts, and
those were approximately right. **The config was never the error.** Measured
against real state LRS data it was accurate to within 1.3 miles per state:

| state | config said | measured | delta |
|---|---|---|---|
| AZ | 359.5 | 359.349 | -0.15 |
| NM | 373.5 | 373.309 | -0.19 |
| TX | 177.0 | 177.072 | +0.07 |
| OK | 331.0 | 330.969 | -0.03 |

The geometry was the error. 40 control points across 1,241 miles meant legs
averaging 29.9 miles, with the worst a **74.7-mile straight line** from
Tucumcari (-104.52) to San Jon (-103.20), New Mexico.

A straight line between two points on a curving road is always shorter than the
road. Chord-vs-arc error, 39 times over, summed to a 75-mile deficit - 6.1% of
the corridor - distributed at roughly 20 miles lost per state.

That is a **precision** problem: positions were coarse, but not systematically
biased in one direction.

---

## Problem 2: the measure was systematically biased, and this one was worse

`conflate_point()` computed a *fraction* along the line with
`ST_LineLocatePoint`, then multiplied by `total_miles`:

```sql
ROUND((ST_LineLocatePoint(centerline::geometry, pt) * total_miles)::numeric, 3)
```

That mixes two incompatible measures. The fraction was of the **1,165.6-mile
geometry**; the multiplier was the **1,241-mile config**. Two different rulers.

The instinctive objection is that this cancels - it is 6% short, multiplying by
1241 scales it back up. It would, **if the shrinkage were uniform.** It was not.
Straight desert stretches lose almost nothing to corner-cutting; curved
stretches lose a lot. So the fraction itself was distorted before the
multiplication ever happened.

Measured against known landmark mileposts, before the fix:

| Landmark | Real measure | Conflated | Error |
|---|---|---|---|
| Flagstaff AZ 195 | 195.0 | 184.8 | **-10.2** |
| Winslow AZ 253 | 253.0 | 243.9 | **-9.1** |
| Albuquerque NM 159 | 518.5 | 490.0 | **-28.5** |
| Tucumcari NM 332 | 691.5 | 669.2 | **-22.3** |
| Amarillo TX 70 | 803.0 | 784.0 | **-19.0** |
| Oklahoma City OK 145 | 1055.0 | 1047.7 | **-7.3** |

**Every error was negative.** That is the signature of systematic bias rather
than noise. The errors were also pinned near zero at both ends - fraction 0
gives 0, fraction 1.0 gives exactly 1,241 - so the distortion had to bulge in
the middle, and it peaked at Albuquerque.

### The proof it was the scaling, not the coarseness

Albuquerque at (-106.650, 35.084) was **exactly a control point** of the
placeholder - zero distance to the nearest vertex, so chord-vs-arc error could
not explain it. Yet:

```
frac_along       0.39486   <- fraction of the 1165.6-mile geometry
frac_if_correct  0.41781   <- 518.5 / 1241.0, what it should have been
scaled_to_1241   490.0     <- what conflate_point returned
```

The fraction was wrong by 0.023, which at 1,241 miles is 28 miles.

The line worth keeping: **sitting exactly on the line tells you where you are
across the road. It says nothing about where you are along it.**

---

## Problem 3: two of seven landmarks were silently discarded

Problems 1 and 2 produced a *wrong number*. This produced **no number at all**.

| Landmark | Off-corridor | `on_corridor` |
|---|---|---|
| Gallup NM | **30.9 mi** | **false** |
| Winslow AZ | **1.3 mi** | **false** |
| the other five | 0-0.4 mi | true |

Both are I-40 towns. Their events were rejected as not on the corridor - the
record incremented an `offCorridor` counter and vanished.

Gallup was dramatic: the placeholder's nearby control point sat at latitude
35.08 while real I-40 there is nearer 35.53. Half a degree, about 31 miles.

**Winslow was the more instructive failure.** At 1.3 miles it missed the 1,600 m
buffer by a hair. Nothing about it looked anomalous in the output; it was simply
gone. A coarse centerline does not fail loudly at the buffer edge - it quietly
trims whatever falls just outside, and how much it trims depends on where the
nearest control point happens to be.

Widening the buffer was never the answer: rescuing Gallup would have needed
about 31 miles, admitting a great deal of genuine off-corridor noise.

---

## The fix

`scripts/fetch-arnold.py` builds the centerline from the **federal NTAD National
Highway System**, one service covering all four states.

> **This used to read each state's own LRS layer, and the switch was a LICENSING
> decision, not a technical one.** The state layers are fresher and carry a true
> per-vertex measure, but none of them licenses redistribution - TxDOT asserts
> copyright and requires written consent to pass the data to a third party, ODOT
> publishes "Authorized reference use only". This repository is MIT-0. NTAD is a
> US government work and its own metadata grants free distribution, so the
> geometry can ship. See [/NOTICE](../../NOTICE). The figures below changed with
> the source; the previous per-state table is in git history.

| State | Filter | Segments | Max M |
|---|---|---|---|
| AZ | `SIGNT1='I' AND SIGNN1='40' AND STFIPS=4` | 94 | 359.347 |
| NM | ...`STFIPS=35` | 242 | 373.530 |
| TX | ...`STFIPS=48` | 143 | 177.141 |
| OK | ...`STFIPS=40`, roadbed `HX` | 290 | 330.940 (calibrated) |

One filter shape replaces four different mainline filters, because `SIGNT1`/`SIGNN1`
are the route's *sign designation* rather than an inventory key.

**The one cost: measures are interpolated, not surveyed.** NTAD geometry is 2D and
carries `BEGINPOINT`/`ENDPOINT` per segment, so every interior vertex's measure is
interpolated along the segment. Because each segment is anchored at *both* ends
that error is bounded per segment and cannot accumulate - which is why Arizona and
Texas still come out exact. Chaining by cumulative length instead, the obvious
alternative, drifts about 1.7 mi over Oklahoma.

**Problem 2 does not get smaller - it stops existing.** Every state's M values
*are* that state's mileposts, so the corridor measure is now read off the line
rather than derived:

```sql
ST_InterpolatePoint(c.centerline_m, pt.g::geometry)
```

No fraction, no multiplication, no ruler to mismatch. `corridor.centerline_m`
holds the same line as `geometry(LineStringM, 4326)`, because
`geography(LineString, 4326)` cannot carry a measure dimension at all. The old
fraction path survives only as a fallback for a corridor with no calibrated
geometry, and is commented as biased where it sits.

Per-state offsets are measured rather than published, which is what makes the
state-line identity exact - AZ MP 359.349 and NM MP 0 resolve to the same
corridor measure:

| | config | state LRS (was) | NTAD (now) |
|---|---|---|---|
| NM `corridorOffset` | 359.5 | 359.349 | **359.347** |
| TX `corridorOffset` | 733.0 | 732.658 | **732.877** |
| OK `corridorOffset` | 910.0 | 909.730 | **910.018** |

The NTAD and state-LRS columns agreeing to 2 thousandths of a mile in Arizona -
about 3 metres, from two independent publishers - is the strongest evidence that
re-sourcing did not degrade the geometry. Oklahoma moves 0.29 mi because its
sections are stitched from control-section measures rather than a statewide one.

### An unexpected corroboration

Four independently maintained state LRS layers, measured at the state lines:

```
AZ end M=359.349  ->  NM start M=0.000    gap  5.9 m
NM end M=373.309  ->  TX start M=0.000    gap 25.5 m
TX end M=177.072  ->  OK start M=0.000    gap 19.9 m
```

Under 26 metres at every seam, from four sources with no common author. That is
the independent agreement the placeholder never had.

---

## Oklahoma needed a second dataset

AZ, NM and TX all hand back statewide mileposts. **Oklahoma does not.** Its M
restarts at 0 in every control section, so a 331-mile route reports measures
topping out at 37.34. `corridor_offset + M` is simply wrong there.

The route also arrives as 18 features per roadbed across 15 control sections,
and both carriageways are included - unfiltered, `ODOTROUTE='I040'` totals
659 mi, exactly twice the corridor. Roadbed is characters 8-9 of `ROUTEID`.

The calibration comes from `Signs__2021_Mile_Marker_View`, where `ASSETCOMMENT`
is the posted milepost and `BEG_MI` is the sign's control-section measure. Both
are in miles, so the slope is 1 by construction and only the intercept is
unknown:

```
statewide_mp = offset + section_measure
offset       = median(posted_mp - beg_mi)
```

**Median, not mean, and not a single marker.** The inventory has bad rows: on
control section 1, `BEG_MI` 2.001 is labelled milepost 1 when 0.990 already is.
Fitting to one sign inherits that error wholesale.

Result: 12 of 18 sections calibrated from 5 to 45 signs each, every one with a
median absolute deviation under 0.04 mi. The sign-derived offsets agree with
pure geometric chaining to about 0.05 mi, which is the cross-check that they are
right. Six sections have no signs in the 2021 inventory and chain geometrically
from their neighbour; the script names them on every run.

The calibration also surfaced a genuine **0.77 mi hole in ODOT's geometry**
between sections `5500069HN0000` and `5500068HN0000`, near Oklahoma City. The
sign offsets account for it correctly. Geometric chaining alone would have
swallowed 0.77 miles silently.

---

## The test fixture was lying too

This was the most surprising part of the fix, and the most transferable.

With real geometry loaded, `npm run db-landmarks` **failed**: a +5.1 mi error at
Oklahoma City and three landmarks rejected as off-corridor. The geometry was
fine. The fixture was wrong.

The probe coordinates in `sql/checks/landmarks.sql` had been hand-entered town
centroids:

- "Oklahoma City MP 145" was at (-97.520, 35.470). The physical MP 145 sign is
  at (-97.6094, 35.4602) - **5.08 miles east of where the fixture put it.**
- Flagstaff, Albuquerque and Tucumcari sat 1.4 to 1.5 mi off the real
  centerline, far enough to be rejected by the 1,600 m buffer.

And the reason Albuquerque looked *perfect* against the placeholder - "exactly a
control point", as an earlier revision of this document put it - is that the
placeholder was drawn through town centroids as well. The fixture and the thing
it was testing shared an error, so the test could only pass while the code was
wrong.

The probes are now generated, not written by hand. Each is resolved from its
state's own milepost marker layer, so the position is agency-surveyed and the
milepost is the number on the physical sign:

| State | Marker layer |
|---|---|
| AZ | `Mileposts_View` |
| NM | `Mileposts` (1-mile intervals) |
| TX | `TxDOT_Mile_Markers` |
| OK | `Signs__2021_Mile_Marker_View` |

Regenerate with `python3 scripts/fetch-arnold.py --rebuild-landmarks`.

---

## Results, measured against the live database

```
npm run db-landmarks

  name              real_ref  real_measure  conflated  err_mi  off_corridor_mi  on_corridor
  Flagstaff AZ      AZ 195.0  195.0         194.8      -0.2    0                True
  Winslow AZ        AZ 253.0  253.0         252.7      -0.3    0                True
  Gallup NM         NM 20.0   379.3         379.4       0      0                True
  Albuquerque NM    NM 159.0  518.3         518.2      -0.1    0                True
  Tucumcari NM      NM 332.0  691.3         691.2      -0.1    0                True
  Amarillo TX       TX 70.0   802.7         802.6       0      0                True
  Oklahoma City OK  OK 145.0  1054.7        1054.7      0      0                True
```

Gallup went from 30.9 mi off and rejected to on-corridor. Errors are mixed-sign
and sub-half-mile where they were uniformly -7 to -28.

**The residual is not ours.** `scripts/fetch-arnold.py` also compares each
conflated position against the state's own published measure for the same
marker, which isolates our interpolation from the agency's marker-vs-sign
offset:

| | our error vs the agency's own measure |
|---|---|
| AZ | **0.000 mi** - exact |
| TX | **0.000 mi** - exact |
| NM | **up to 0.180 mi** (290 m) at Tucumcari |
| OK | not comparable - see below |

These were 0.001 mi (AZ, NM) and 0.026 mi (TX) when measures were read
per-vertex from the state layers. They are the cost of per-segment interpolation
against a 2D source, and the cost is confined to New Mexico because NM's own
segment measure spans disagree with its geometry by up to **0.478 mi**, against
0.026 mi in Arizona and 0.022 mi in Texas - which is why those two come out exact
rather than merely close. The check in
`fetch-arnold.py` bounds this at 0.25 mi.

Oklahoma is checked against the posted milepost only. Its `BEG_MI` is
control-section-relative, and converting it to a statewide measure needs the
very sign-derived offsets those markers produced, so comparing against it would
be circular.

So the `err_mi` above is dominated by each agency's own offset between its
marker layer and its posted signs - which is also why it leans slightly
negative at a tenth of a mile.

---

## Two lessons worth keeping

**1. Agreement between two implementations tests the implementations, not the
model.** An earlier revision of this document treated turf and PostGIS agreeing
on Amarillo to within 0.235 mi as mutual confirmation. It confirmed exactly one
thing: both implemented the same formula correctly. Both consumed the same
distorted geometry and the same fraction-times-config scaling, so both were
wrong by about 19 miles in the same direction, in close agreement.

The same caution applies to the fix. The offline Python check and PostGIS
`ST_InterpolatePoint` now agree to the displayed precision - which again proves
only that they implement the same thing. What makes the result trustworthy is
the *independent* source of truth: agency-surveyed markers, and the agencies'
own published measures for them.

**2. A fixture calibrated against the thing it tests proves nothing.** The
landmark check was the one test that could have caught systematic bias, and it
was silently unable to, because its probes and the placeholder shared an
assumption. All-one-sign remains the tell for bias, but judge it with the
magnitudes: at a tenth of a mile it is agency rounding; at whole miles it is the
fraction-times-total_miles bug returning.

---

## Still open

**1. ~~The in-process path still has the placeholder.~~ RESOLVED.** Both paths carry
the same 11,924 vertices and the same measures, and agree to three decimals on
the live cluster once `sql/002-corridor-real.sql` has been re-applied after a
re-source. Note that **GeoJSON has no M coordinate** - the spec's
third element is elevation - so the measures live in a sibling `measures` array
rather than being smuggled into the coordinates, and PostGIS gets the identical
numbers as a real `LINESTRING M` in `corridor.centerline_m`.

The file also moved: `reference/corridor.json`, not `config/`. A corridor in the
deployment bundle is one route frozen at build time, and production now reads
corridors from Postgres keyed by route. The file is the OFFLINE source, which is what
keeps `npm run probe` and the test suite running with no AWS account.

**2. `verified` — and a bug worth recording.** `reference/corridor.json` says
`true`; the database said `false`, for the same geometry from the same generator. The
cause was `002`'s `ON CONFLICT DO UPDATE` list omitting the column, so a corridor
first loaded as unverified stayed unverified however many times the file was
re-applied. Since the deployed pipeline reads the database, it would have withheld
positions the offline tools were happily publishing - a tripwire disagreeing with
itself, which is worse than not having one.

Fixed in `scripts/fetch-arnold.py`; the regenerated `002` updates every column it
sets. Whether `verified` SHOULD be true remains a deliberate publishing decision -
`probe.py` and `strip_export.py` read it - but the two sources will now at least
agree on the answer.

**3. `state_segment.segment` is still NULL,** so state assignment relies on
measure arithmetic alone. With real geometry it can now be populated, which
allows checking state boundaries geometrically rather than trusting the offsets.
That is what `verified` on that table is for.

**4. Six Oklahoma control sections are chained geometrically,** not
sign-calibrated, because the 2021 sign inventory has no markers on them. They
are named on every run of the script. A newer ODOT sign extract would close it.

**5. Buffer width is still Open Question 8.** 1,600 m remains a placeholder. It
is no longer *hazardous* - nothing legitimate is being rejected - but it has
still never been confirmed with the states.

Open Question 5, "whose LRS is canonical?", is effectively answered: each state's
own, chained onto one corridor measure through the offset table, with events
carrying both the corridor measure and the native state milepost.

---

## How to reproduce

```sh
npm run corridor            # rebuild from the state services, prints the landmark check
npm run corridor-offline    # same from build/arnold-cache, no network

npx cdk deploy CorridorEventHubSpatial   # sql/ ships inside the Lambda bundle, rebuilt at synth
npm run db-migrate-plan
npm run db-migrate
npm run db-landmarks
```

Two traps in that sequence, both of which cost time on the way through:

- **The migration Lambda reads `sql/` from its own bundle.** Editing a `.sql`
  file locally changes nothing until a redeploy, which rebuilds the bundle.
- **`CREATE TABLE IF NOT EXISTS` is a no-op on an existing table.** Adding a
  column to the table body in `001-init.sql` only affects fresh clusters. Every
  addition needs an explicit `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` beside
  it. `centerline_m` was added without one and failed 11 statements later inside
  `conflate_point`, reporting the function rather than the table.

**Run `npm run db-landmarks` after any geometry change.** If every error is several
miles negative, `centerline_m` is NULL and conflation has fallen back to the
biased path.

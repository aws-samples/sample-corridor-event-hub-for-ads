# WZDx 4.2 JSON Schema — vendored

The official schemas, verbatim, as published by USDOT JPO. `scripts/lib/wzdx_schema.py`
validates our projected feed against them; `npm run lint:wzdx` and
`tests/test_wzdx_schema.py` both run that.

**Two licences, not one.** The five WZDx schemas are CC0; the two `geojson-*` files
are MIT from a different project, and MIT has a condition CC0 does not. Read the
table before assuming this directory is uniformly public domain.

**Vendored, not fetched.** The conformance check has to be deterministic and has to
run with no network — the same reason `tests/fixtures/` holds captured payloads. A
check that silently fetches a schema is a check that silently changes, and one that
needs a network is a check that gets skipped in CI.

## Source — WZDx schemas (CC0)

`WorkZoneFeed.json`, `FeedInfo.json`, `RoadEventFeature.json`, `BoundingBox.json`,
`Direction.json`.

| | |
|---|---|
| Repository | <https://github.com/usdot-jpo-ode/wzdx> |
| Path | `schemas/4.2/` |
| Commit | `be5a8001b03c057bd84cb326fd0a452a7047aec2` (2024-11-12) |
| Draft | JSON Schema draft-07 |
| Licence | **CC0-1.0** — public domain dedication, so redistributing these files in this repository is permitted, with nothing required in return |

## Source — GeoJSON geometry schemas (MIT, *not* CC0)

`geojson-LineString.json`, `geojson-MultiPoint.json`.

| | |
|---|---|
| Repository | <https://github.com/geojson/schema> (served at <https://geojson.org/schema/>) |
| Copyright | © 2018 Tim Schaub |
| Draft | JSON Schema draft-07 |
| Licence | **MIT** — redistribution permitted **provided the copyright and permission notice are reproduced**. That notice is in [/NOTICE](../../../../NOTICE) at the repository root; it must stay there for as long as these two files are vendored |
| Pinned | **No.** The fetch below hits an unversioned URL, so unlike the WZDx files these are not reproducible to a commit. Re-fetching may not return the same bytes |

These two came from a different project under a different licence than the five
above. The distinction is easy to lose because they sit in the same directory and
arrive from the same script — losing it is what put an unnoticed MIT obligation in
an MIT-0 repository until 2026-09-16.

## What produced these files

```bash
# WZDx 4.2 schemas -- CC0-1.0, pinned to a commit.
for f in WorkZoneFeed FeedInfo RoadEventFeature BoundingBox Direction; do
  curl -sS -o "reference/wzdx/4.2/$f.json" \
    "https://raw.githubusercontent.com/usdot-jpo-ode/wzdx/be5a8001b03c057bd84cb326fd0a452a7047aec2/schemas/4.2/$f.json"
done
# GeoJSON geometry schemas, referenced by RoadEventFeature.json by absolute URL.
# DIFFERENT LICENCE: MIT (c) 2018 Tim Schaub, not CC0. Adding or refreshing these
# means the MIT notice in /NOTICE must still be present and still accurate.
for f in LineString MultiPoint; do
  curl -sSL -o "reference/wzdx/4.2/geojson-$f.json" "https://geojson.org/schema/$f.json"
done
```

`geojson-LineString.json` and `geojson-MultiPoint.json` are here because
`RoadEventFeature.json` `$ref`s them at `https://geojson.org/schema/...`. Without them
the registry cannot resolve a road event's geometry, and geometry is the field most
worth validating.

## Editing

Don't. These are upstream artifacts; the file contents are the authority the check
appeals to, and a local tweak makes the conformance claim worthless. To move to a
newer WZDx version, re-run the commands above against the new path, bump
`WZDX_VERSION` in `corridor_event_hub/core/wzdx.py`, and fix what the check reports.

If a future version stops `$ref`ing the GeoJSON schemas and you delete the two
`geojson-*` files, delete their entry from [/NOTICE](../../../../NOTICE) in the same
change — a notice for material no longer shipped is noise that makes the rest of the
file less trustworthy.

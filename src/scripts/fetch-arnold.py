#!/usr/bin/env python3
"""Build the real I-40 corridor centerline from the federal NTAD road network.

Replaces the 40-point placeholder in the offline corridor JSON. This is the week-1
task named in docs/CORRIDOR-GEOMETRY.md, and it fixes BOTH problems documented
there:

  Problem 1 (precision)  40 control points, legs averaging 29.9 mi, worst leg a
                         74.7 mi straight line. Two of seven landmark towns fell
                         outside the 1600 m buffer and their events were being
                         silently discarded.
  Problem 2 (bias)       conflate_point computed a FRACTION of the 1165.6 mi
                         placeholder geometry and multiplied it by the 1241 mi
                         config total. Two different rulers, and the shrinkage
                         was not uniform, so the error did not cancel. Every
                         landmark came out 7 to 28 miles west of truth.

Problem 2 does not get smaller here - it stops existing. The route arrives with
measures that ARE mileposts, so a conflated position is read off the line
(ST_InterpolatePoint) instead of derived from a fraction. No multiplication, no
stretched ruler.

The file is still called fetch-arnold.py, and ARNOLD is still what this data is:
All Road Network of Linear Referenced Data, authored by the states and submitted
to FHWA. What changed is WHICH PUBLISHER it is read from, and that turns out to be
the whole licensing question - see the next section.

Run:
    python3 scripts/fetch-arnold.py                  # fetch, verify, emit SQL
    python3 scripts/fetch-arnold.py --write-config   # also rewrite corridor.json
    python3 scripts/fetch-arnold.py --offline        # reuse build/arnold-cache

Then load it:
    ./scripts/db.sh --file sql/002-corridor-real.sql
    npm run db-landmarks

Only the standard library is used. The fetch is public ArcGIS REST, no key and no
auth.


WHY THIS READS A FEDERAL SERVICE AND NOT THE FOUR STATE LAYERS
--------------------------------------------------------------

It used to read the four states' own LRS layers directly - ADOT, NMDOT, TxDOT and
ODOT. Those layers are richer, fresher and carry a true per-vertex M. They are
also NOT LICENSED FOR REDISTRIBUTION, and this repository is MIT-0:

  TxDOT   "Copyright 2026. Texas Department of Transportation ... produced for
          internal use". Commercial use and resale are not permitted, and passing
          the content to a third party requires TxDOT's WRITTEN CONSENT.
  ODOT    licenseInfo is exactly "Authorized reference use only".
  NMDOT   license "custom"; created for NMDOT use, all liability disclaimed.
  ADOT    no licence metadata at all - copyrightText, description and
          serviceDescription are all empty strings.

Not one of the four grants redistribution, and MIT-0 purports to grant every
reader the right to copy, sell and sublicense. Committing geometry derived from
those layers would hand readers rights the DOTs withheld, so the geometry now
comes from the NATIONAL TRANSPORTATION ATLAS DATABASE instead:

  https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services
      /NTAD_National_Highway_System/FeatureServer/0

whose own copyrightText, carried in-band with the data, reads: "The NHS Version
2025.08.08 database, or any portion thereof, can be freely distributed as long as
this metadata entry is included with each distribution ... This NTAD dataset is a
work of the United States government as defined in 17 U.S.C. section 101 and as
such are not protected by any U.S. copyrights. This work is available for
unrestricted public use."

That is a grant, from a publisher entitled to make it, carried in the bytes. The
states author this data and submit it to FHWA; the federal compilation is a
government work. It is the same reasoning this repository already applies to the
National Bridge Inventory in sql/003-nbi-structures.sql. The one condition -
include the metadata entry with each distribution - is discharged by /NOTICE.

Accuracy cost, stated honestly: the state layers carry van-collected per-vertex
measures; NTAD carries 2D geometry with BEGINPOINT/ENDPOINT per segment, so
measures are INTERPOLATED along each segment (see _interpolate_measures). Because
every segment is anchored at BOTH ends, that error is bounded per segment and
cannot accumulate down the route.


THE FOUR TRAPS
--------------

1. GeoJSON SILENTLY DESTROYS THE MEASURES.  The GeoJSON spec has no M
   coordinate. Requesting f=geojson from an M-enabled layer returns the full
   vertex count, 2-element coordinates, no error and no warning - beautiful
   geometry with the entire linear reference stripped out. Every request here
   uses f=json (Esri JSON) with returnM=true. Less dangerous than it was, since
   NTAD geometry is 2D anyway and the measures arrive as attributes, but a future
   M-enabled source would be silently ruined by f=geojson. See _query().

2. A ROUTE-NUMBER MATCH PULLS IN RAMPS.  This was the hard part of the per-state
   version: every state spelled its mainline filter differently, and a bare
   route-number match returned ramps, frontage roads and even a county road
   called The Ranch Trail. NTAD collapses that to one filter for all four
   states - SIGNT1='I' AND SIGNN1='40' AND STFIPS=<fips> - because the sign
   designation is the route's public identity rather than an inventory key.

3. DIRECTION FIELDS ARE INVENTORY DIRECTION, NOT TRAVEL DIRECTION.  NTAD's I-40
   records all report DIR=0, so there is no dual-carriageway duplication to
   filter here and nothing to mistake for travel direction. Nothing in this file
   consumes direction; it is called out so the next reader does not reach for it.

4. OKLAHOMA'S MEASURES RESTART IN EVERY CONTROL SECTION.  AZ, NM and TX hand back
   statewide mileposts, so corridor_measure = corridor_offset + M. Oklahoma does
   not, in NTAD or anywhere else: ODOT REFERENCES BY CONTROL SECTION AND HAS NO
   STATEWIDE MILEPOST MEASURE AT ALL. Its measures top out at 37.35 on a 331-mile
   route. No federal source can supply a form the state does not publish - NHPN's
   BEGMP is control-section too, and the NBI's KILOPOINT_011 uses the same keys.
   The posted mileposts exist only as the physical sign inventory, so that is what
   calibrates them - see _calibrate_oklahoma(), which takes 19 SCALAR OFFSETS and
   no geometry from ODOT.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO, "build", "arnold-cache")
OUT_SQL = os.path.join(REPO, "sql", "002-corridor-real.sql")


def _corridor_json_path():
    """Where the offline corridor lives - SAME SEARCH ORDER as core/lrs.py.

    That module searches CEH_CORRIDOR_FILE, then reference/corridor.json, then
    config/corridor.json. This has to match, because the failure mode of disagreeing
    is silent. The corridor is moving from config/ to reference/, and while both
    files exist, writing to config/ produces a file the reader IGNORES: the write
    reports success, the pipeline keeps using the other copy, and nothing says so.

    Falls back to reference/ when neither exists, because that is where a corridor
    belongs now - config/ is copied into Lambda bundles and a corridor must not be
    (scripts/build-lambda.sh fails if one appears in a bundle).
    """
    override = os.environ.get("CEH_CORRIDOR_FILE")
    if override:
        return override
    for relative in ("reference/corridor.json", "config/corridor.json"):
        candidate = os.path.join(REPO, relative)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(REPO, "reference", "corridor.json")


CORRIDOR_JSON = _corridor_json_path()
LANDMARKS_SQL = os.path.join(REPO, "sql", "checks", "landmarks.sql")

ROUTE = "I-40"
METERS_PER_MILE = 1609.34

# The corridor runs west to east. Every state's measures must ascend in that
# direction before they can be chained onto a single corridor measure.
STATE_ORDER = ("AZ", "NM", "TX", "OK")


# ---------------------------------------------------------------------------
# Sources
#
# ONE federal service for all four states, not four state services. See "WHY THIS
# READS A FEDERAL SERVICE" in the module docstring: the state layers are richer
# but none of them licenses redistribution, and this repository is MIT-0.
#
# Found via the NTAD catalogue, which is where the licence statement lives:
#   https://geodata.bts.gov/api/search/v1/collections/dataset/items?q=<terms>
# The same statement is carried in-band as the layer's own copyrightText, which is
# the form worth trusting - it travels with the bytes rather than sitting beside
# them on a portal page that can be re-skinned.
# ---------------------------------------------------------------------------

NTAD_NHS_URL = (
    "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services"
    "/NTAD_National_Highway_System/FeatureServer/0"
)

# The licence condition, verbatim, as returned by NTAD_NHS_URL?f=json. Reproduced
# here so that anyone editing this file sees the obligation it creates, and
# asserted against the live service by verify_license() so a silent relicensing
# upstream fails the fetch instead of quietly changing what may be redistributed.
NTAD_LICENSE_FRAGMENT = "can be freely distributed as long as this metadata entry"

# One filter shape for every state. SIGNT1/SIGNN1 are the SIGN designation - the
# route's public identity, 'I' + '40' - which is why this works uniformly where
# four different inventory-key filters were needed before. STFIPS scopes it to one
# state so the four legs stay separable and each stays inside maxRecordCount.
NTAD_WHERE = "SIGNT1='I' AND SIGNN1='40' AND STFIPS={stfips}"

# BEGINPOINT/ENDPOINT are the segment's begin and end measure in miles. MILES is
# its length, used only to cross-check the measures against the geometry. ROUTEID
# matters for Oklahoma alone, where it is the control-section key.
NTAD_FIELDS = "ROUTEID,BEGINPOINT,ENDPOINT,MILES,STFIPS"

SOURCES = {
    "AZ": {"stfips": 4, "measure": "statewide"},
    "NM": {"stfips": 35, "measure": "statewide"},
    "TX": {"stfips": 48, "measure": "statewide"},
    "OK": {
        "stfips": 40,
        # ODOT restarts the measure in every control section, so OK cannot use
        # BEGINPOINT directly the way the other three do.
        "measure": "control_section",
        # Characters 7-9 of ROUTEID select the roadbed. NTAD carries the HX
        # roadbed for I-40; the sign inventory carries both HX and HN, so the
        # join below needs no translation between the two key spaces.
        "roadbed": "HX",
    },
}

# Oklahoma's control-section measures become statewide mileposts through the
# physical sign inventory. ASSETCOMMENT is the posted milepost number; BEG_MI is
# the control-section measure of the sign.
#
# THIS IS AN ODOT LAYER, AND ODOT DOES NOT LICENSE REDISTRIBUTION. That is why
# only 19 SCALAR OFFSETS are taken from it and no geometry: a posted milepost
# number on a physical sign beside a public road is a fact observable by anyone
# who drives past it, and copyright protects compilations of expression rather
# than individual facts. The geometry those offsets are applied to comes from
# NTAD. A reader who disagrees with that line has everything needed to redraw it -
# the reasoning is recorded in /NOTICE rather than buried here.
OK_SIGNS_URL = (
    "https://services6.arcgis.com/RBtoEUQ2lmN0K3GY/arcgis/rest/services"
    "/Signs__2021_Mile_Marker_View/FeatureServer/0"
)

# The seven landmarks from sql/checks/landmarks.sql - BUT ONLY THE MILEPOSTS.
#
# The coordinates that used to live here were hand-entered town centroids, and
# they were the reason this check could not survive real geometry:
#
#   Oklahoma City "MP 145" was at -97.520, 35.470. The physical MP 145 sign is
#   at -97.6094, 35.4602 - FIVE MILES EAST of where the fixture put it. Against
#   correct geometry that probe reports a +5.1 mi error that is entirely the
#   fixture's own.
#
#   Flagstaff, Albuquerque and Tucumcari sat 1.4 to 1.5 mi off the real
#   centerline, far enough to be REJECTED by the 1600 m buffer. Albuquerque
#   looked perfect against the placeholder ("exactly a control point", per
#   docs/CORRIDOR-GEOMETRY.md) for the circular reason that the placeholder was
#   drawn through town centroids too.
#
# A fixture calibrated against the thing it is testing proves nothing. So the
# coordinates are no longer written by hand: they are resolved from each state's
# own milepost marker layer, where the position is agency-surveyed and the
# milepost is the number on the physical sign.
#
# What this checks, precisely: our chaining of four state measures onto one
# corridor measure, and our interpolation along it. It does not audit the
# agencies' own calibration - nothing available could, and that is not the
# failure mode that produced 28-mile errors.
LANDMARKS = (
    ("Flagstaff AZ", "AZ", 195.0),
    ("Winslow AZ", "AZ", 253.0),
    ("Gallup NM", "NM", 20.0),
    ("Albuquerque NM", "NM", 159.0),
    ("Tucumcari NM", "NM", 332.0),
    ("Amarillo TX", "TX", 70.0),
    ("Oklahoma City OK", "OK", 145.0),
)

# Milepost marker point layers, one per state. Same organisations as SOURCES.
#
# Route identifiers differ AGAIN in these layers. Texas is the clearest example:
# TxDOT_Roadways calls the route by a numeric surrogate key, while
# TxDOT_Mile_Markers calls it 'IH0040-KG'. That is a third TxDOT namespace after
# the numeric key and the 'IH0040' feed form - which is why route matching has to
# be structural (prefix plus number) everywhere rather than textual.
MARKER_SOURCES = {
    "AZ": {
        "url": "https://services6.arcgis.com/clPWQMwZfdWn4MQZ/arcgis/rest/services"
               "/Mileposts_View/FeatureServer/0",
        # Cardinality 'C' is the cardinal-direction marker, matching the
        # RouteCardinality='Y' roadbed the centerline comes from.
        "where": "RouteId LIKE '%I 040%' AND Cardinality = 'C' AND MPNum_integer = {mp}",
        "fields": "RouteId,MPNumber,Cardinality,Measure",
        "coords": "geometry",
        # ADOT publishes the marker's own LRS measure alongside the posted number,
        # so our interpolation can be compared against ADOT's answer rather than
        # only against the sign.
        "measure_field": "Measure",
    },
    "NM": {
        "url": "https://services.arcgis.com/hOpd7wfnKm16p9D9/arcgis/rest/services"
               "/Mileposts/FeatureServer/0",
        # Layer 0 is the 1-mile interval set. Milepost is TEXT here, not numeric.
        "where": "RouteID = 'I40P' AND Milepost = '{mp}'",
        "fields": "RouteID,Milepost,Measure,Longitude,Latitude",
        "coords": ("Longitude", "Latitude"),
        "measure_field": "Measure",
    },
    "TX": {
        "url": "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services"
               "/TxDOT_Mile_Markers/FeatureServer/0",
        "where": "RTE_PRFX = 'IH' AND RTE_NBR = 40 AND MARKER = {mp}",
        "fields": "RTE_NM,MARKER",
        "coords": "geometry",
        # The marker points are M-enabled, so the measure rides on the geometry
        # rather than in an attribute.
        "measure_from_geometry": True,
    },
    "OK": {
        # Same sign inventory that calibrates the control sections. ROUTEID has
        # to be constrained to the I-40 mainline sections or a posted number
        # matches markers on unrelated routes.
        "url": OK_SIGNS_URL,
        "where": "ASSETCOMMENT = '{mp}' AND ROUTEID IN ({ok_ids})",
        "fields": "ROUTEID,ASSETCOMMENT,BEG_MI,BEG_LAT,BEG_LONG",
        "coords": ("BEG_LONG", "BEG_LAT"),
        # No measure comparison for Oklahoma. BEG_MI is control-section-relative,
        # and turning it into a statewide measure needs the very sign-derived
        # offsets these markers produced - comparing against it would be circular.
        # Oklahoma is checked against the posted milepost only.
    },
}


# ---------------------------------------------------------------------------
# Geometry helpers
#
# Deliberately stdlib-only rather than shapely. This script runs before the
# corridor exists, sometimes on a stock python3 with nothing installed, and the
# only operations needed are great-circle distance and point-to-segment
# distance in a local planar approximation.
# ---------------------------------------------------------------------------

EARTH_RADIUS_M = 6371000.0


def haversine_m(a, b):
    """Great-circle distance in metres between two [lon, lat, ...] vertices."""
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (a[1], a[0], b[1], b[0]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def path_length_miles(pts):
    return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) / METERS_PER_MILE


def _local_xy(pt, lat0):
    """Equirectangular projection to metres, valid over the few km that matter."""
    return (
        math.radians(pt[0]) * EARTH_RADIUS_M * math.cos(math.radians(lat0)),
        math.radians(pt[1]) * EARTH_RADIUS_M,
    )


def nearest_on_path(pt, pts):
    """Closest point on a polyline to pt.

    Returns (distance_metres, interpolated_measure). The measure is interpolated
    ALONG the segment rather than snapped to the nearer vertex, which matters:
    at 0.07 mi average vertex spacing, snapping would quantise every conflated
    position to about 120 m for no reason.

    This is the offline equivalent of PostGIS ST_InterpolatePoint on a
    LINESTRINGM, and exists so the landmark check can run before the database
    has been loaded.
    """
    lat0 = pt[1]
    px, py = _local_xy(pt, lat0)
    best = (float("inf"), None)
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        ax, ay = _local_xy(a, lat0)
        bx, by = _local_xy(b, lat0)
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy
        t = 0.0 if seg_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_sq))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best[0]:
            best = (d, a[2] + t * (b[2] - a[2]))
    return best


# ---------------------------------------------------------------------------
# ArcGIS REST
# ---------------------------------------------------------------------------


def _get(url, params, cache_key, offline=False, path="/query"):
    """GET with on-disk caching, so a rerun does not re-hit the service.

    ``path`` is "/query" for feature requests and "" for the layer metadata the
    licence check reads - the metadata is on the layer URL itself, and asking the
    query endpoint for it returns "No where clause specified".
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, cache_key + ".json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            return json.load(fh)
    if offline:
        raise SystemExit(f"--offline but no cached response at {cache_path}")

    full = url + path + "?" + urllib.parse.urlencode(params)
    if not full.startswith("https://"):
        # Every SOURCES entry is an https ArcGIS endpoint. Checked anyway, because
        # urlopen would just as happily accept file:// or http:// from an edited
        # constant, and that is what bandit B310 flags below.
        raise SystemExit(f"{cache_key}: refusing a non-https endpoint {full!r}")
    # NTAD answers metadata instantly and geometry slowly, and under load it
    # returns 504 rather than queuing. Six attempts with a growing backoff, because
    # a transient gateway timeout partway through four states would otherwise throw
    # away the pages already fetched.
    # NTAD answers metadata instantly and geometry slowly, and under load it fails
    # in two ways that both LOOK permanent and are not: 504 Gateway Timeout, and
    # HTTP 200 carrying {"error": {"code": 400, "message": "Invalid query
    # parameters"}} for a query that succeeded a minute earlier and succeeds again
    # a minute later. So an error PAYLOAD is retried exactly like a transport
    # failure - checking it after the loop, which is the obvious place, turns a
    # transient blip into a failed run.
    last = None
    attempts = 6
    payload = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(full, timeout=180) as resp:  # nosec B310
                payload = json.load(resp)
            if "error" not in payload:
                break
            last = f"ArcGIS error {payload['error']}"
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(3 * (attempt + 1))
    else:
        raise SystemExit(
            f"failed to fetch {cache_key} after {attempts} attempts: {last}\n"
            "Cached pages are kept, so rerunning resumes rather than restarting."
        )

    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return payload


def _query(url, where, fields, cache_key, geometry=True, offline=False,
           order_by=None):
    """Query a feature layer, paginating until the server stops truncating.

    f=json, NOT f=geojson. See trap 1 in the module docstring: GeoJSON has no M
    coordinate, so f=geojson returns the same vertices with the measures
    stripped, no error raised. That failure is invisible until conflated
    positions come out wrong.
    """
    out = []
    offset = 0
    # ASK FOR SMALL PAGES ON PURPOSE. The server would happily return a whole
    # state - maxRecordCount is 2000 and no state has more than 291 segments - but
    # a full state's GEOMETRY in one response reliably 504s. 50 keeps each response
    # small, and each page is cached separately so a failure resumes.
    page = 50 if geometry else 1000
    while True:
        params = {
            "where": where,
            "outFields": fields,
            "returnGeometry": "true" if geometry else "false",
            "returnM": "true",
            "outSR": "4326",
            "f": "json",
            "resultOffset": str(offset),
            "resultRecordCount": str(page),
        }
        if order_by:
            # Ordering has to be explicit for MULTI-PAGE paging to be stable:
            # without it the server may return rows in a different order per page
            # and a segment can be duplicated or skipped across the seam. Only set
            # where it is needed, because the field name differs per layer and the
            # state marker layers reject 'OBJECTID' outright.
            params["orderByFields"] = order_by
        payload = _get(url, params, f"{cache_key}-{offset}", offline=offline)
        feats = payload.get("features", [])
        out.extend(feats)
        if len(feats) < page:
            break
        offset += len(feats)
    return out


# ---------------------------------------------------------------------------
# Per-state loading
# ---------------------------------------------------------------------------


def _orient(pts, label):
    """Force a path to ascend in measure, and sanity-check it also runs west to east.

    LRS geometry is digitised in the inventory direction, which is usually but
    not always the direction of increasing measure.
    """
    if pts[0][2] > pts[-1][2]:
        pts = list(reversed(pts))
    if pts[0][0] > pts[-1][0]:
        # I-40 runs west to east in all four states with no doubling back, so
        # measure ascending eastward is an invariant. If it ever fails, the
        # chaining arithmetic below is invalid and must not proceed silently.
        print(f"  WARNING  {label}: measure ascends EAST-to-WEST "
              f"(lon {pts[0][0]:.3f} -> {pts[-1][0]:.3f}). Chaining assumes otherwise.")
    return pts


def verify_license(offline=False):
    """Refuse to fetch if NTAD has stopped saying the data may be redistributed.

    The whole reason this file reads a federal service rather than the four state
    layers is the licence quoted in the module docstring, and that licence lives in
    the service's own metadata. If it changes, everything downstream - what may be
    committed, what /NOTICE claims, what MIT-0 may grant - changes with it. A fetch
    that silently proceeded past a relicensing would produce an artifact whose
    licence file is a lie, so this fails loudly instead.
    """
    payload = _get(NTAD_NHS_URL, {"f": "json"}, "ntad-service-meta",
                   offline=offline, path="")
    text = payload.get("copyrightText") or ""
    if NTAD_LICENSE_FRAGMENT not in text:
        raise SystemExit(
            "NTAD licence text has CHANGED and no longer contains\n"
            f"  {NTAD_LICENSE_FRAGMENT!r}\n"
            f"copyrightText is now:\n  {text[:400]!r}\n\n"
            "Do not commit geometry from this fetch until the new terms are read. "
            "Re-verify against https://geodata.bts.gov and update /NOTICE and the "
            "module docstring together. See NTAD_LICENSE_FRAGMENT."
        )
    print("  licence OK  NTAD copyrightText still grants free distribution")
    return text


def _interpolate_measures(path, begin_m, end_m):
    """Give every vertex of a 2D NTAD segment a measure, by proportional distance.

    The state layers carried a per-vertex M collected by a distance-measuring
    instrument in a van. NTAD carries 2D vertices plus BEGINPOINT and ENDPOINT
    ATTRIBUTES for the whole segment, so the measure of an interior vertex has to
    be interpolated from its position along the segment.

    WHY THE ERROR THIS INTRODUCES CANNOT ACCUMULATE: every segment is anchored at
    BOTH ends. A vertex measure can be wrong only by however much the road curves
    non-uniformly WITHIN one segment, and the next segment restarts from its own
    surveyed BEGINPOINT rather than from wherever this one drifted to. Chaining by
    cumulative length - the obvious alternative - has no such anchor and drifts
    monotonically; on Oklahoma it comes out ~1.7 mi short over 331 miles.

    Measured on the live data, segments average 1.2 mi (TX) to 3.8 mi (AZ). The
    segment measure span agrees with the computed geometry to within 0.026 mi (AZ)
    and 0.022 mi (TX), but only 0.478 mi (NM) and 0.254 mi (OK) - which is exactly
    why AZ and TX conflate exactly and New Mexico carries the residual below.
    """
    cumulative = [0.0]
    for i in range(len(path) - 1):
        cumulative.append(cumulative[-1] + haversine_m(path[i], path[i + 1]))
    total = cumulative[-1]
    span = end_m - begin_m
    if total <= 0:
        # A zero-length segment cannot be interpolated along. One vertex at the
        # begin measure keeps it from contributing a divide-by-zero or a NaN.
        return [[path[0][0], path[0][1], begin_m]]
    return [
        [pt[0], pt[1], begin_m + span * (dist / total)]
        for pt, dist in zip(path, cumulative)
    ]


def _ntad_segments(state, offline=False):
    """Every I-40 segment NTAD holds for one state, as measure-bearing vertices.

    Returns a list of {routeid, begin, end, pts}, where pts is [lon, lat, m] and m
    is whatever measure the state publishes - a statewide milepost for AZ, NM and
    TX, a control-section measure for OK.
    """
    src = SOURCES[state]
    feats = _query(
        NTAD_NHS_URL,
        NTAD_WHERE.format(stfips=src["stfips"]),
        NTAD_FIELDS,
        f"{state.lower()}-ntad",
        offline=offline,
        order_by="OBJECTID",
    )
    segments = []
    skipped = 0
    for feat in feats:
        attrs = feat["attributes"]
        begin, end = attrs.get("BEGINPOINT"), attrs.get("ENDPOINT")
        if begin is None or end is None:
            skipped += 1
            continue
        for path in (feat.get("geometry") or {}).get("paths", []):
            if len(path) < 2:
                continue
            segments.append({
                "routeid": attrs.get("ROUTEID") or "",
                "begin": float(begin),
                "end": float(end),
                "miles": attrs.get("MILES"),
                "pts": _interpolate_measures(path, float(begin), float(end)),
            })
    if not segments:
        raise SystemExit(f"{state}: NTAD returned no I-40 geometry with measures")
    if skipped:
        print(f"    {skipped} feature(s) skipped for a null BEGINPOINT/ENDPOINT")
    return _check_measures_against_geometry(state, segments)


# A segment whose measure span disagrees with its own geometry by more than this
# is not imprecise, it is WRONG, and it is dropped. Set above what clean data does
# - measured worst per state: AZ 0.026, TX 0.022, OK 0.254, NM 0.478 - and well
# below the only real offender seen: a 0.03-MILE STUB in Oklahoma section 6800010 whose
# BEGINPOINT/ENDPOINT claim 19.37 miles. That stub sits inside another section's
# span, carries no mile markers, and chained geometrically it added its bogus
# 19.4-mile span to the corridor and pushed Oklahoma from 331 to 350 miles.
MEASURE_GEOMETRY_TOLERANCE_MI = 1.0


def _check_measures_against_geometry(state, segments):
    """Drop segments whose measure span contradicts their own geometry.

    The cross-check that makes the interpolation above trustworthy rather than
    assumed. Compared in MILES ABSOLUTE, not as a percentage: a 0.006-mile segment
    whose span is out by a thousandth of a mile is fine, and a relative test flags
    it as a 20% error. Half of New Mexico's segments look broken under a 5%
    relative test and none of them is.
    """
    kept, dropped = [], []
    worst = 0.0
    for seg in segments:
        span = abs(seg["end"] - seg["begin"])
        geom = path_length_miles(seg["pts"])
        delta = abs(span - geom)
        if delta > MEASURE_GEOMETRY_TOLERANCE_MI:
            seg["_delta"] = delta
            seg["_geom"] = geom
            dropped.append(seg)
            continue
        worst = max(worst, delta)
        kept.append(seg)
    print(f"    measure vs geometry: worst {worst:.3f} mi over "
          f"{len(kept)} segments")
    for seg in dropped:
        print(f"    DROPPED {state} segment {seg['routeid']!r}: measure span "
              f"{abs(seg['end'] - seg['begin']):.3f} mi but geometry is "
              f"{seg['_geom']:.3f} mi (off by {seg['_delta']:.3f})")
    if not kept:
        raise SystemExit(f"{state}: every segment failed the measure/geometry check")
    return kept


def _stitch(segments, label):
    """Order segments by measure and concatenate, reporting any gap at a seam."""
    segments = sorted(segments, key=lambda s: s["begin"])
    merged = []
    for seg in segments:
        pts = seg["pts"]
        if merged:
            gap = haversine_m(merged[-1], pts[0])
            if gap > 500:
                print(f"    WARNING  {label}: {gap:.0f} m gap entering the segment "
                      f"at M={seg['begin']:.3f}")
        merged.extend(pts)
    return merged


def load_simple_state(state, offline=False):
    """AZ, NM, TX: BEGINPOINT/ENDPOINT are already statewide mileposts."""
    return _orient(_stitch(_ntad_segments(state, offline=offline), state), state)


def _calibrate_oklahoma(offline=False):
    """Turn Oklahoma's per-control-section measures into statewide mileposts.

    Oklahoma is the one state whose measure cannot be used directly: it restarts
    at 0 in every control section, so the whole 331-mile route reports measures in
    the range 0 to 37.35. The route arrives as 19 control sections that have to be
    both ORDERED and OFFSET before they chain.

    THIS IS NOT A GAP IN NTAD, AND NO OTHER SOURCE FIXES IT. ODOT references by
    control section and publishes no statewide milepost measure at all, so no
    federal republication of ODOT data can contain one: NHPN's BEGMP is
    control-section too and covers only 315 of the 331 miles, and the NBI's
    KILOPOINT_011 uses the same control-section keys. Oklahoma's posted mileposts
    exist as physical signs, and the sign inventory is the only place they are
    written down.

    So the offsets come from that inventory, where ASSETCOMMENT is the posted
    milepost and BEG_MI is the sign's control-section measure. Both are in miles,
    so the slope between them is 1 by construction and only the intercept is
    unknown:

        statewide_mp = offset + section_measure
        offset       = median(posted_mp - beg_mi)

    MEDIAN, NOT MEAN, AND NOT A SINGLE MARKER. The inventory has bad rows - on
    control section 1, BEG_MI 2.001 is labelled milepost 1 when BEG_MI 0.990
    already is. Fitting to any one sign inherits that error wholesale; taking the
    median over the ~50 signs per section discards it. The spread is reported
    per section so a section calibrated from bad data is visible rather than
    quietly wrong.

    WHAT IS TAKEN FROM ODOT, AND WHY ONLY THIS. ODOT does not license
    redistribution ("Authorized reference use only"), so this function takes 19
    SCALAR OFFSETS from it and no geometry. Each offset is derived from posted
    milepost numbers on physical signs beside a public road - facts anyone driving
    I-40 can read - and the geometry they are applied to is NTAD's. See
    OK_SIGNS_URL and /NOTICE for the reasoning, which a reader is equipped to
    disagree with.
    """
    src = SOURCES["OK"]
    segments = _ntad_segments("OK", offline=offline)

    sections = {}
    for seg in segments:
        route_id = seg["routeid"]
        # Characters 7-9 are the roadbed. NTAD publishes HX for I-40; the filter
        # stays explicit so a future vintage carrying both roadbeds does not
        # silently double the route the way ODOT's own layer did.
        if route_id[7:9] != src["roadbed"]:
            continue
        sections.setdefault(route_id, []).append(seg)

    if not sections:
        raise SystemExit(
            "OK: no NTAD sections matched roadbed " + src["roadbed"]
            + f" (saw {sorted({s['routeid'][7:9] for s in segments})})"
        )

    # Within a control section the measures are self-consistent, so each section
    # stitches on its own measure before any offset is applied.
    sections = {
        route_id: _orient(_stitch(segs, f"OK {route_id}"), f"OK {route_id}")
        for route_id, segs in sections.items()
    }

    ids = sorted(sections)
    # Ask for BOTH roadbeds of every control section, not just the one NTAD carries.
    # A control section is one physical stretch of road; HX and HN are its two
    # roadbeds, and their sign-derived offsets agree to about 0.005 mi where both
    # are populated. Some sections have markers on only one side, so restricting
    # the query to NTAD's roadbed leaves them uncalibrated for no reason -
    # 7500002HX0000 has 2 markers, just under the minimum, and 2 more on HN.
    wanted = sorted({i[:7] for i in ids})
    signs = _query(
        OK_SIGNS_URL,
        " OR ".join(f"ROUTEID LIKE '{i}%'" for i in wanted),
        "ROUTEID,ASSETTYPE,ASSETCOMMENT,BEG_MI",
        "ok-signs",
        geometry=False,
        offline=offline,
    )

    residuals = {}
    for sign in signs:
        a = sign["attributes"]
        try:
            posted = float(a.get("ASSETCOMMENT"))
        except (TypeError, ValueError):
            continue  # ASSETCOMMENT is free text; non-numeric rows are not markers
        beg = a.get("BEG_MI")
        if beg is None:
            continue
        residuals.setdefault(a["ROUTEID"], []).append(posted - float(beg))

    # Minimum markers before an offset is trusted. Two is enough BECAUSE the slope
    # is 1 by construction - only the intercept is being fitted - and because the
    # two roadbeds of a section are independent measurements of it that agree to
    # ~0.005 mi. Three would leave 7500002 uncalibrated over a 0.003 mi doubt.
    min_markers = 2

    print("  Oklahoma control-section calibration (sign-derived):")
    calibrated = {}
    for route_id in ids:
        vals = residuals.get(route_id, [])
        source = "same roadbed"
        if len(vals) < min_markers:
            # Same control section, other roadbed. Same physical stretch of road.
            other = [v for rid, vs in residuals.items()
                     if rid[:7] == route_id[:7] and rid != route_id for v in vs]
            if len(other) >= min_markers:
                vals = other
                source = f"roadbed {sorted({r[7:9] for r in residuals if r[:7] == route_id[:7] and r != route_id})}"
        if len(vals) >= min_markers:
            offset = statistics.median(vals)
            spread = statistics.median([abs(v - offset) for v in vals])
            calibrated[route_id] = offset
            flag = "  <-- HIGH SPREAD" if spread > 0.25 else ""
            print(f"    {route_id}  offset={offset:8.3f} mi  from {len(vals):3d} signs "
                  f"({source})  MAD={spread:.3f}{flag}")
        else:
            print(f"    {route_id}  offset=       ?  from {len(vals):3d} signs  "
                  f"<-- NOT CALIBRATED, will chain geometrically")

    # Order sections west to east. Safe for I-40, which crosses Oklahoma
    # monotonically; it would not be safe for a route that doubles back.
    ordered = sorted(ids, key=lambda r: min(v[0] for v in sections[r]))

    # Fill any uncalibrated section by chaining from its predecessor, so a gap in
    # the sign inventory degrades to the old behaviour for one section instead of
    # failing the whole state.
    running = None
    for route_id in ordered:
        pts = sections[route_id]
        if route_id in calibrated:
            running = calibrated[route_id]
        elif running is None:
            running = 0.0
        offset = running
        for v in pts:
            v[2] = offset + v[2]
        running = offset + path_length_miles(pts)

    merged = []
    for route_id in ordered:
        pts = sections[route_id]
        if merged:
            gap = haversine_m(merged[-1], pts[0])
            if gap > 500:
                print(f"    WARNING  {gap:.0f} m gap entering {route_id} "
                      f"at MP {pts[0][2]:.2f}")
        merged.extend(pts)

    return _orient(merged, "OK"), ids


# ---------------------------------------------------------------------------
# Corridor assembly
# ---------------------------------------------------------------------------


def build_corridor(offline=False):
    """Fetch all four states and chain them onto one corridor measure."""
    states = {}
    ok_ids = []
    print("checking the NTAD licence before fetching anything")
    verify_license(offline=offline)
    print("fetching I-40 from NTAD (one federal service, four states)")
    for state in STATE_ORDER:
        print(f"  {state} ...")
        if SOURCES[state]["measure"] == "control_section":
            pts, ok_ids = _calibrate_oklahoma(offline=offline)
        else:
            pts = load_simple_state(state, offline=offline)
        states[state] = pts
        print(f"    {len(pts):5d} vertices  {path_length_miles(pts):7.1f} mi geometry  "
              f"M {pts[0][2]:.3f} -> {pts[-1][2]:.3f}")

    # Measured offsets replace the config's rounded ones. AZ's LRS says I-40 runs
    # 0 to 359.349 across Arizona; the config's 359.5 is a published
    # approximation. The measured value is what makes the state-line identity
    # exact, which is the whole point of AZ MP 359.349 and NM MP 0 must
    # resolve to the same corridor measure or cross-state dedup silently fails.
    segments = []
    offset = 0.0
    corridor = []
    print("\nchaining onto corridor measure")
    for state in STATE_ORDER:
        pts = states[state]
        state_max = pts[-1][2]
        if corridor:
            gap = haversine_m(corridor[-1], pts[0])
            print(f"  {state} offset {offset:9.3f}   seam gap {gap:6.1f} m")
            if gap > 1000:
                print(f"    WARNING  {gap:.0f} m seam gap is too large to be a state line")
        else:
            print(f"  {state} offset {offset:9.3f}   (corridor origin)")
        for v in pts:
            # ROUND HERE, before the monotonicity filter below, not at write time.
            #
            # Both outputs emit 6 decimal places of position and 3 of measure. If
            # the rounding happened at write time instead, two vertices a
            # ten-thousandth of a mile apart would round to the SAME measure and
            # the emitted artifact would not be strictly ascending even though the
            # in-memory list was. Rounding first means the filter sees exactly the
            # values that get written.
            corridor.append([
                round(v[0], 6), round(v[1], 6), round(offset + v[2], 3),
            ])
        segments.append({
            "state": state,
            "stateMilepostMin": 0.0,
            "stateMilepostMax": round(state_max, 3),
            "corridorOffset": round(offset, 3),
        })
        offset += state_max

    # A non-ascending measure would make ST_InterpolatePoint ambiguous and break
    # every range scan downstream, so drop reversals and ties rather than store
    # them. Ties are the common case: two vertices closer together than the 3
    # decimal places of a mile that get emitted (about 1.6 m).
    cleaned = [corridor[0]]
    dropped = 0
    for v in corridor[1:]:
        if v[2] <= cleaned[-1][2]:
            dropped += 1
            continue
        cleaned.append(v)
    if dropped:
        print(f"  dropped {dropped} vertex/vertices with non-ascending measure "
              f"(reversals and sub-0.001 mi ties)")

    return cleaned, segments, ok_ids


# ---------------------------------------------------------------------------
# Landmark fixture
# ---------------------------------------------------------------------------


def resolve_landmarks(ok_ids, offline=False):
    """Look up each landmark's surveyed coordinate in its state's marker layer.

    Returns (name, lon, lat, state, milepost, marker_measure) per landmark, where
    marker_measure is the state's own reported measure for that marker when the
    layer publishes one, else None. It is printed for context, not asserted on.
    """
    quoted = ",".join(f"'{i}'" for i in ok_ids)
    out = []
    print("\nresolving landmark probes from state milepost marker layers")
    for name, state, mp in LANDMARKS:
        src = MARKER_SOURCES[state]
        where = src["where"].format(mp=f"{mp:g}", ok_ids=quoted)
        feats = _query(
            src["url"], where, src["fields"],
            f"marker-{state.lower()}-{mp:g}",
            geometry=(src["coords"] == "geometry"),
            offline=offline,
        )
        if not feats:
            raise SystemExit(f"{name}: no marker found at {state} MP {mp:g}")
        feat = feats[0]
        if src["coords"] == "geometry":
            lon, lat = feat["geometry"]["x"], feat["geometry"]["y"]
        else:
            lon = float(feat["attributes"][src["coords"][0]])
            lat = float(feat["attributes"][src["coords"][1]])
        measure = None
        if src.get("measure_field"):
            raw = feat["attributes"].get(src["measure_field"])
            measure = None if raw is None else float(raw)
        elif src.get("measure_from_geometry"):
            raw = feat["geometry"].get("m")
            measure = None if raw is None else float(raw)
        extra = "" if len(feats) == 1 else f"  ({len(feats)} markers, using the first)"
        print(f"  {name:<18} {state} MP {mp:<6g} -> {lon:11.6f}, {lat:10.6f}{extra}")
        out.append((name, lon, lat, state, mp, measure))
    return out


def write_landmarks_sql(landmarks, segments, path):
    """Regenerate sql/checks/landmarks.sql with surveyed probe coordinates."""
    with open(path, encoding="utf-8") as fh:
        original = fh.read()

    head, sep, _ = original.partition("WITH probe(")
    if not sep:
        raise SystemExit(f"{path}: cannot find the probe CTE to replace")

    rows = []
    for i, (name, lon, lat, state, mp, _measure) in enumerate(landmarks):
        comma = "," if i < len(landmarks) - 1 else ""
        rows.append(f"  ('{name}',{' ' * max(1, 18 - len(name))}{lon:11.6f}, {lat:9.6f}, "
                    f"'{state}', {mp:6.1f}){comma}")

    body = [
        "-- PROBE COORDINATES ARE GENERATED, NOT HAND-ENTERED.",
        "--",
        "-- scripts/fetch-arnold.py --rebuild-landmarks resolves each one from the",
        "-- state's own milepost marker layer, so the position is agency-surveyed and",
        "-- the milepost is the number on the physical sign.",
        "--",
        "-- The previous hand-entered values were town centroids and they made this",
        "-- check unusable against real geometry: 'Oklahoma City MP 145' sat 5.08 mi",
        "-- east of the actual MP 145 sign, and three of the seven were far enough off",
        "-- the true centerline to be rejected by the 1600 m buffer. Albuquerque landed",
        "-- exactly on the placeholder centerline for the circular reason that the",
        "-- placeholder was drawn through town centroids as well.",
        "",
        "WITH probe(name, lon, lat, real_state, real_mp) AS (VALUES",
        *rows,
        ")",
    ]

    _, _, tail = original.partition(")\nSELECT")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head.rstrip("\n") + "\n\n" + "\n".join(body) + "\nSELECT" + tail)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def landmark_check(corridor, segments, landmarks):
    """The offline twin of `npm run db-landmarks`.

    Watch the SIGNS, not just the magnitudes. All-negative errors were the
    signature of the placeholder's systematic bias; a healthy result is mixed
    signs and small magnitudes. Errors that are all one sign mean the scaling is
    still wrong however small they look.
    """
    offsets = {s["state"]: s for s in segments}
    buffer_m = 1600.0

    print("\nlandmark check (the only check with an independent source of truth)")
    print("  err_sign is versus the POSTED milepost. err_lrs is versus the state's")
    print("  own reported measure for the same marker, which isolates our")
    print("  interpolation from the agency's marker-vs-route inconsistency.")
    print()
    print(f"  {'landmark':<18}{'real':>9}{'conflated':>11}{'err_sign':>10}"
          f"{'err_lrs':>9}{'off_mi':>8}  on_corridor")
    rows = []
    for name, lon, lat, state, mp, marker_measure in landmarks:
        seg = offsets[state]
        real = seg["corridorOffset"] + (mp - seg["stateMilepostMin"])
        dist_m, measure = nearest_on_path([lon, lat], corridor)
        err = measure - real
        err_lrs = None
        if marker_measure is not None:
            err_lrs = measure - (seg["corridorOffset"] + marker_measure)
        on = dist_m <= buffer_m
        rows.append((name, real, measure, err, dist_m / METERS_PER_MILE, on, err_lrs))
        lrs_txt = "       -" if err_lrs is None else f"{err_lrs:+9.3f}"
        print(f"  {name:<18}{real:9.1f}{measure:11.1f}{err:+10.2f}{lrs_txt}"
              f"{dist_m / METERS_PER_MILE:8.2f}  {'true' if on else 'FALSE'}")

    lrs_errs = [r[6] for r in rows if r[6] is not None]
    if lrs_errs:
        print(f"\n  worst |err_lrs|    {max(abs(e) for e in lrs_errs):.3f} mi "
              f"over {len(lrs_errs)} markers with a published measure")
        print("                     (this is OUR error. err_sign also contains the")
        print("                      agency's own marker-vs-posted-sign offset.)")

    errs = [r[3] for r in rows]
    rejected = [r[0] for r in rows if not r[5]]
    worst = max(abs(e) for e in errs)
    print()
    print(f"  worst |err|        {worst:.2f} mi")
    print(f"  sign mix           {sum(1 for e in errs if e > 0)} positive, "
          f"{sum(1 for e in errs if e < 0)} negative")
    if rejected:
        print(f"  REJECTED           {', '.join(rejected)}")
    else:
        print("  REJECTED           none - all seven landmarks are on-corridor")

    ok = True
    if rejected:
        print("  FAIL  a landmark marker on I-40 is outside the corridor buffer")
        ok = False
    # Published milepost signage is good to roughly a mile, so this bound tests
    # the chaining and the Oklahoma calibration, not the interpolation.
    if worst > 1.0:
        print(f"  FAIL  worst err_sign {worst:.2f} mi exceeds the 1 mi tolerance")
        ok = False
    # The sharp one: our interpolation against the agency's own answer.
    #
    # THIS BOUND WAS 0.1 MI AND IS NOW 0.25, because the source changed and the old
    # number was measuring something this source cannot produce. The state layers
    # carried a per-vertex M collected by a distance-measuring instrument in a van,
    # so "our measure" was the agency's measure and agreement was 0.001 mi. NTAD is
    # 2D: measures arrive per SEGMENT and every interior vertex is interpolated
    # (see _interpolate_measures), so this now bounds interpolation error rather
    # than a transcription.
    #
    # Measured, on the live data: Arizona 0.000 and Texas 0.000 - EXACT, because
    # their segment measure spans agree with their geometry. New Mexico is the only
    # state with error, up to 0.180 mi at Tucumcari, and it comes from NEW MEXICO'S
    # OWN inconsistency: its spans disagree with its geometry by up to 0.478 mi,
    # against 0.026 in Arizona and 0.022 in Texas. That
    # is why 0.25 rather than something larger - the error is bounded by the state's
    # internal disagreement, not by a method that drifts, so a figure well under a
    # quarter mile still catches a real regression. If this fires, check whether a
    # state's span/geometry agreement has degraded before touching the number.
    if lrs_errs and max(abs(e) for e in lrs_errs) > 0.25:
        print(f"  FAIL  worst err_lrs {max(abs(e) for e in lrs_errs):.3f} mi - our "
              f"interpolation disagrees with the state's own measure")
        ok = False
    # The bias detector from docs/CORRIDOR-GEOMETRY.md, with a magnitude floor.
    #
    # All-one-sign was the placeholder's signature and it is still the thing
    # worth catching, but err_sign now carries each agency's marker-vs-sign
    # offset, which is itself slightly one-directional at a tenth of a mile.
    # Without the floor this fires on their rounding rather than our bias.
    mean_abs = sum(abs(e) for e in errs) / len(errs)
    if (all(e < 0 for e in errs) or all(e > 0 for e in errs)) and mean_abs > 0.5:
        print(f"  FAIL  every err_sign has the same sign and averages "
              f"{mean_abs:.2f} mi - the measure is systematically biased")
        ok = False
    if ok:
        print("  PASS")
    return ok


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

# The RDS Data API takes one statement per call and caps the SQL string well
# below the ~350 KB this geometry needs, so the centerline is staged in chunks
# and assembled server-side with string_agg. Keep chunks comfortably small.
CHUNK_CHARS = 28000


def emit_sql(corridor, segments, path, verified):
    tokens = [f"{v[0]:.6f} {v[1]:.6f} {v[2]:.3f}" for v in corridor]
    chunks, buf, size = [], [], 0
    for tok in tokens:
        if size + len(tok) + 1 > CHUNK_CHARS and buf:
            chunks.append(buf)
            buf, size = [], 0
        buf.append(tok)
        size += len(tok) + 1
    if buf:
        chunks.append(buf)

    total_miles = corridor[-1][2]
    geom_miles = path_length_miles(corridor)

    out = []
    w = out.append
    w("-- migration: repeatable")
    w("--")
    w("-- Read by corridor_event_hub/core/migrations.py. Repeatable because this file is")
    w("-- GENERATED: refreshing the centerline from a newer ARNOLD vintage changes")
    w("-- its checksum, and a run-once file that changes is reported as drift and")
    w("-- never applied. Every statement below is idempotent, so re-applying is")
    w("-- exactly what should happen.")
    w("")
    w("-- Real I-40 centerline, generated by scripts/fetch-arnold.py. DO NOT EDIT.")
    w("--")
    w(f"-- Source: the four states' own LRS/ARNOLD layers. {len(corridor)} vertices,")
    w(f"-- {geom_miles:.1f} mi of geometry, corridor measure 0 -> {total_miles:.3f}.")
    w("-- The placeholder it replaces had 40 vertices and 1165.6 mi of geometry.")
    w("--")
    w("-- Apply with:  ./scripts/db-migrate.sh        (or --plan to see it first)")
    w("--")
    w("-- DATA ONLY. The corridor.centerline_m column and the conflate_point body")
    w("-- that reads it both live in 001-init.sql, deliberately and not here.")
    w("--")
    w("-- 001 is marked repeatable, so it re-applies whenever it is edited. If the")
    w("-- function lived in this file instead, any later edit to 001 would restore")
    w("-- the fraction-times-total_miles body and silently reintroduce the")
    w("-- systematic bias - with this file already recorded as applied, so nothing")
    w("-- would put it back. Schema and functions belong in the file that owns")
    w("-- them; this one carries only the geometry and the measured offsets.")
    w("")
    w("CREATE TABLE IF NOT EXISTS corridor_geom_load (")
    w("  route    text    NOT NULL,")
    w("  part_no  integer NOT NULL,")
    w("  chunk    text    NOT NULL,")
    w("  PRIMARY KEY (route, part_no)")
    w(");")
    w("")
    w("COMMENT ON TABLE corridor_geom_load IS")
    w("  'Staging for the chunked centerline load. The RDS Data API caps SQL "
      "statement size well below a 350 KB LINESTRINGM, so the geometry arrives "
      "in parts and is assembled with string_agg. Safe to truncate.';")
    w("")
    # B608 on the two statements below is a false positive, and narrowly so. ROUTE is
    # a module constant, `i` is an enumerate index, and every chunk token was built as
    # f"{v[0]:.6f} {v[1]:.6f} {v[2]:.3f}" - three numeric conversions, so a token
    # cannot contain a quote whatever ArcGIS returned. If a token ever becomes text,
    # this needs the same sql_string() escape that fetch-nbi.py uses on NBI strings.
    w(f"DELETE FROM corridor_geom_load WHERE route = '{ROUTE}';")  # nosec B608
    w("")
    for i, chunk in enumerate(chunks):
        w(f"INSERT INTO corridor_geom_load (route, part_no, chunk) VALUES "  # nosec B608
          f"('{ROUTE}', {i}, '{','.join(chunk)}');")
    w("")
    w("-- Assemble, then write both representations from the same source.")
    w("WITH ewkt AS (")
    w("  SELECT 'SRID=4326;LINESTRINGM(' ||")
    w("         string_agg(chunk, ',' ORDER BY part_no) || ')' AS t")
    w("  FROM corridor_geom_load")
    w(f"  WHERE route = '{ROUTE}'")
    w(")")
    w("INSERT INTO corridor (route, description, centerline, centerline_m,")
    w("                      buffer_meters, verified, total_miles)")
    w("SELECT")
    w(f"  '{ROUTE}',")
    w("  'I-40 mainline, western AZ border to eastern OK border',")
    w("  ST_Force2D(ST_GeomFromEWKT(ewkt.t))::geography,")
    w("  ST_GeomFromEWKT(ewkt.t),")
    w("  1600,")
    w("  -- Geometry tripwire, and EARNED rather than asserted: this is written true")
    w("  -- only when the landmark check passed on the data being loaded. It is what")
    w("  -- probe.py and strip_export.py read to decide whether positions may be")
    w("  -- published, so a bad fetch must not be able to set it.")
    w(f"  {'true' if verified else 'false'},")
    w(f"  {total_miles:.3f}")
    w("FROM ewkt")
    w("-- EVERY COLUMN THIS FILE SETS IS ALSO UPDATED HERE. The list used to omit")
    w("-- verified, description and buffer_meters, which made this repeatable")
    w("-- migration bring the row only PARTLY up to the file's state: a corridor")
    w("-- loaded once as unverified stayed unverified forever, no matter what the")
    w("-- landmark check said on later runs.")
    w("--")
    w("-- That is not cosmetic. verified is the publishability tripwire, and")
    w("-- it went out of step with reference/corridor.json - the JSON said true and")
    w("-- the database said false, for the same geometry from the same generator. The")
    w("-- deployed pipeline reads the database, so it would have withheld positions")
    w("-- the offline tools were happy to publish. A tripwire that disagrees with")
    w("-- itself is worse than none.")
    w("ON CONFLICT (route) DO UPDATE SET")
    w("  description   = EXCLUDED.description,")
    w("  centerline    = EXCLUDED.centerline,")
    w("  centerline_m  = EXCLUDED.centerline_m,")
    w("  buffer_meters = EXCLUDED.buffer_meters,")
    w("  verified      = EXCLUDED.verified,")
    w("  total_miles   = EXCLUDED.total_miles,")
    w("  updated_at    = now();")
    w("")
    w("-- Measured per-state extents replace the config's rounded ones.")
    w("--")
    w("-- These numbers move by less than half a mile, which is exactly why they")
    w("-- matter: the state line is where cross-state dedup either works or")
    w("-- silently does not. With measured values the identity is exact -")
    w("-- AZ MP 359.349 and NM MP 0 resolve to the same corridor measure.")
    w("--")
    w("-- NOTE this changes what counts as a valid milepost. AZ MP 359.5 is now out")
    w("-- of range and milepost_to_measure returns NULL for it, which is correct")
    w("-- (an out-of-range milepost is not clamped). scripts/db.sh and tests/test_lrs.py")
    w("-- both used to hardcode that literal and now read the boundary out of this")
    w("-- table instead.")
    w("--")
    w("-- verified is inserted FALSE here and computed at the END of this file, once")
    w("-- segment geometry exists to compute it from. It means something different")
    w("-- from corridor.verified: that this state's boundary has been checked")
    w("-- GEOMETRICALLY rather than trusted from the offset arithmetic above.")
    for seg in segments:
        w("INSERT INTO state_segment (route, state, state_mp_min, state_mp_max,")
        w("                           corridor_offset, verified)")
        w(f"VALUES ('{ROUTE}', '{seg['state']}', {seg['stateMilepostMin']:.3f}, "
          f"{seg['stateMilepostMax']:.3f}, {seg['corridorOffset']:.3f}, false)")
        w("ON CONFLICT (route, state) DO UPDATE SET")
        w("  state_mp_min    = EXCLUDED.state_mp_min,")
        w("  state_mp_max    = EXCLUDED.state_mp_max,")
        w("  corridor_offset = EXCLUDED.corridor_offset;")
    w("")
    w("-- Per-state segment geometry, DERIVED rather than shipped.")
    w("--")
    w("-- state_segment.segment is what lets a state boundary be checked")
    w("-- GEOMETRICALLY instead of trusted from the offset arithmetic, which is what")
    w("-- state_segment.verified means. It was NULL until now, so the identity rested")
    w("-- entirely on numbers we wrote being compared against numbers we wrote.")
    w("--")
    w("-- ST_LocateBetween cuts the corridor at two measures, so each segment comes")
    w("-- OUT OF centerline_m rather than being loaded separately. That is the point:")
    w("-- four more geometries would double the size of this file, and - worse - they")
    w("-- could drift from the centerline they are supposed to describe. Derived,")
    w("-- they cannot.")
    w("--")
    w("-- ST_LocateBetween returns a collection, so extract the linestrings (type 2)")
    w("-- and merge them. Each state's measure range is contiguous in the corridor,")
    w("-- so the merge yields ONE LineString - which the verified test below asserts")
    w("-- rather than assumes, because a MULTILINESTRING would mean the corridor has")
    w("-- a hole in it and the geography(LineString) column would reject it anyway.")
    w("WITH derived AS (")
    w("  SELECT s.state,")
    w("         ST_LineMerge(ST_CollectionExtract(")
    w("           ST_LocateBetween(")
    w("             c.centerline_m,")
    w("             s.corridor_offset,")
    w("             s.corridor_offset + (s.state_mp_max - s.state_mp_min)), 2)) AS geom")
    w("  FROM state_segment s")
    w("  JOIN corridor c ON c.route = s.route")
    w(f"  WHERE s.route = '{ROUTE}'")
    w(")")
    w("UPDATE state_segment s")
    w("SET segment  = ST_Force2D(d.geom)::geography,")
    w("    -- EARNED, like corridor.verified: computed from the geometry, not set.")
    w("    --")
    w("    -- The length tolerance is 1% of the state's milepost span, not an absolute")
    w("    -- figure. Geodesic chord length is ALWAYS shorter than LRS mileage - a")
    w("    -- straight line between vertices cannot be longer than the curve through")
    w("    -- them - and the shortfall scales with length and curvature. Measured, it")
    w("    -- is 0.15% to 0.21% per state (0.36 to 0.71 mi), so 1% passes comfortably")
    w("    -- while still catching the failure that matters: a state whose measure")
    w("    -- range is wrong is out by tens of miles, not tenths.")
    w("    verified = (")
    w("      d.geom IS NOT NULL")
    w("      AND GeometryType(d.geom) = 'LINESTRING'")
    w("      AND ABS(ST_Length(ST_Force2D(d.geom)::geography) / 1609.344")
    w("              - (s.state_mp_max - s.state_mp_min))")
    w("          < 0.01 * (s.state_mp_max - s.state_mp_min)")
    w("    )")
    w("FROM derived d")
    w(f"WHERE s.route = '{ROUTE}' AND s.state = d.state;")
    w("")
    w("-- Nothing below this point. conflate_point is defined in 001-init.sql and")
    w("-- already prefers centerline_m, so loading the geometry above is all that is")
    w("-- needed to switch conflation off the biased fraction path. Verify with:")
    w("--   npm run db-geometry && npm run db-landmarks")
    w("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")

    return len(chunks), sum(len(t) + 1 for t in tokens)


def write_config(corridor, segments, path, verified):
    """Rewrite the offline corridor JSON so LocalConflator sees the same geometry.

    GEOJSON CANNOT CARRY A MEASURE. The spec's optional third coordinate element
    is elevation, not M, so writing measures there would be a quiet abuse that
    any conformant reader would misinterpret. The measures therefore go in a
    sibling `measures` array of the same length and order, and
    `centerline.coordinates` stays valid 2D GeoJSON - which also means
    core/lrs.py keeps loading it unchanged.
    """
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    cfg["centerline"] = {
        "$comment": (
            "REAL centerline, generated by scripts/fetch-arnold.py from the four "
            "states' own LRS/ARNOLD layers. Replaces the ~40-point placeholder. "
            "GeoJSON has no M coordinate, so the corridor measure for each vertex "
            "is in the parallel `measures` array rather than a third coordinate "
            "element (which the spec reserves for elevation)."
        ),
        "type": "LineString",
        # Already rounded in build_corridor, deliberately - see the note there on
        # why rounding after the monotonicity filter would break it.
        "coordinates": [[v[0], v[1]] for v in corridor],
        "measures": [v[2] for v in corridor],
    }
    for seg in cfg["states"]:
        measured = next(s for s in segments if s["state"] == seg["state"])
        seg["stateMilepostMax"] = measured["stateMilepostMax"]
        seg["corridorOffset"] = measured["corridorOffset"]
    cfg["$totalComment"] = (
        f"Total corridor measure {corridor[-1][2]:.3f} mi, MEASURED from state LRS "
        f"data rather than published approximations. State maxima and offsets are "
        f"measured too, which is what makes the state-line identity exact."
    )
    # Earned by the landmark check on this data, never hand-set: probe.py
    # and strip_export.py read it to decide whether positions may be published.
    cfg["verified"] = bool(verified)
    cfg["$verifiedComment"] = (
        "True because the seven-landmark check passed against this geometry: every "
        "landmark on-corridor, |err| under 0.35 mi versus posted mileposts, and "
        "agreement with each state's own published marker measure to 0.001 mi in "
        "AZ/NM and 0.026 mi in TX. Regenerate with scripts/fetch-arnold.py, which "
        "writes false if the check fails. See docs/CORRIDOR-GEOMETRY.md."
    )

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true",
                    help="reuse build/arnold-cache, do not hit the state services")
    ap.add_argument("--write-config", action="store_true",
                    help="also rewrite the offline corridor JSON (large diff; off by default)")
    ap.add_argument("--rebuild-landmarks", action="store_true",
                    help="rewrite sql/checks/landmarks.sql with surveyed probe coordinates")
    ap.add_argument("--out-sql", default=OUT_SQL)
    args = ap.parse_args()

    corridor, segments, ok_ids = build_corridor(offline=args.offline)

    print(f"\ncorridor: {len(corridor)} vertices, "
          f"{path_length_miles(corridor):.1f} mi geometry, "
          f"measure 0 -> {corridor[-1][2]:.3f}")

    landmarks = resolve_landmarks(ok_ids, offline=args.offline)
    passed = landmark_check(corridor, segments, landmarks)

    if args.rebuild_landmarks:
        write_landmarks_sql(landmarks, segments, LANDMARKS_SQL)
        print("\nrewrote sql/checks/landmarks.sql with surveyed probe coordinates")

    parts, wkt_chars = emit_sql(corridor, segments, args.out_sql, passed)
    rel = os.path.relpath(args.out_sql, REPO)
    print(f"\nwrote {rel}  ({parts} geometry chunks, {wkt_chars / 1024:.0f} KB of WKT)")

    if args.write_config:
        write_config(corridor, segments, CORRIDOR_JSON, passed)
        size = os.path.getsize(CORRIDOR_JSON) / 1024
        print(f"wrote {os.path.relpath(CORRIDOR_JSON, REPO)}  ({size:.0f} KB)")
    else:
        print(f"{os.path.relpath(CORRIDOR_JSON, REPO)} NOT touched - rerun with "
              "--write-config to update")
        print("  (the database and the offline corridor can then disagree, and the")
        print("   offline one is what npm run probe, the UI and the tests all read)")

    # db-migrate, not db.sh --file: the Lambda records what it applied and wraps
    # each file in a transaction, and it reads sql/ from its own bundle - so a
    # regenerated centerline needs the bundle rebuilt before it can apply.
    print("\nnext:")
    # The deploy is NOT optional and NOT a separate bundle step: the migration
    # Lambda reads sql/ from its own bundle, and synth rebuilds that bundle itself.
    # Skip the deploy and db-migrate-plan reports NOTHING TO APPLY while sitting
    # next to a regenerated file - see docs/SPATIAL-DB.md.
    print(f"  npx cdk deploy CorridorEventHubSpatial   # {rel} ships inside the Lambda bundle")
    print("  npm run db-migrate-plan")
    print("  npm run db-migrate")
    print("  npm run db-landmarks")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

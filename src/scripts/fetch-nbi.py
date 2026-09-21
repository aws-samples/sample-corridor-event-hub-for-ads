#!/usr/bin/env python3
"""Build sql/003-nbi-structures.sql from the FHWA National Bridge Inventory.

NBI is the federal register of every bridge and culvert on a public road in the
US - about 620,000 structures, one row per structure, republished annually by
FHWA as one delimited file per state. It is a US government work, so it is public
domain: no key, no registration, no data agreement. That makes it the only class-7
source that needs nobody's permission (DATA-SOURCES.md).

    https://www.fhwa.dot.gov/bridge/nbi/2025/delimited/OK25.txt

Each row carries 123 fields identified by NBI item numbers - the structure's
identity, location, materials, condition ratings, inspection dates, and the two
that matter here: how much room there is above the road.

    python3 scripts/fetch-nbi.py              # fetch, then write the migration
    python3 scripts/fetch-nbi.py --offline    # from build/nbi-cache, no network
    python3 scripts/fetch-nbi.py --dry-run    # report only, write nothing

Then apply it the normal way:  npm run db-migrate

WHY A GENERATED MIGRATION rather than a Lambda that loads at runtime: this is an
ANNUAL batch of near-static reference data (~1,500 rows for the corridor), not a
feed. Generating SQL makes the load reproducible, reviewable in a diff, and
applied through the recorded transactional path that everything else uses. Same
shape as scripts/fetch-arnold.py.

=============================================================================
WHICH FIELD IS THE CLEARANCE, AND WHY THE OBVIOUS ANSWER IS WRONG
=============================================================================

The question is "can a 15-foot load travel this corridor". The obvious
approach - take structures whose inventory route IS the corridor and read item 10,
MIN_VERT_CLR_010 - is what the catalog originally described, and it is almost
useless. Measured across all four states' 2025 files:

    structures with inventory route = I-40      1,199
      item 10 = 99.99 (the "no restriction" sentinel)  1,154   96%
      item 10 = 30.48 m / 100.00 ft  (another sentinel)   21
      item 10 = 30.45 m /  99.90 ft  (another sentinel)    2
      item 10 = 0                                          2
      item 10 = a real measurement                        20
      ... of which BELOW 14 ft                             0

Item 10 is the clearance over the roadway the structure CARRIES. A bridge
carrying the interstate usually has open sky above it, so 96% correctly report
"no restriction" - and the query `WHERE min_vert_clearance_ft < 14` returns
nothing at all.

What actually restricts a tall load is the structure CROSSING OVER the corridor,
and its clearance lives in a different field: item 54B, VERT_CLR_UND_054B, the
minimum vertical UNDERclearance, qualified by item 54A which says what is
underneath ('H' for highway). Same four files:

    structures crossing over a highway matching the corridor    369
      item 54B usable                                          369   100%
      ... in the 14-16 ft band                                   40

369 usable values instead of 20, and the ones near the legal limit are all here.

So BOTH are loaded, `clearance_item` records which field a row's number came
from, and where a structure appears in both sets the MORE RESTRICTIVE value wins.
A structure that both carries and crosses the corridor is real - an interchange
ramp bridge - and taking the minimum is the only safe reading.

=============================================================================
THE TRAPS, ALL VERIFIED AGAINST THE 2025 FILES
=============================================================================

1. 99.99 IS A SENTINEL, NOT A MEASUREMENT. It means "no restriction". Read
   literally it is 99.99 metres of clearance and every over-height check passes.
   So are 30.48 (exactly 100.00 ft) and 30.45 (99.90 ft), which the catalog did
   not record: 23 structures carry them, and they would have been loaded as real
   100-foot clearances. All of these become NULL, and the schema's
   `clearance_sane` CHECK (0 < x < 30) is the backstop that makes a missed one a
   failed transaction rather than a wrong answer.

2. LATITUDE AND LONGITUDE USE DIFFERENT DIGIT WIDTHS. LAT_016 is DDMMSSss (8
   digits), LONG_017 is DDDMMSSss (9). Using one width for both puts longitude
   near -10 instead of -99: a plausible-looking number in the Atlantic. All 1,543
   selected rows carry exactly those widths, so this is decodable rather than
   guessable.

3. TWO ROWS HAVE COORDINATES THAT ARE SIMPLY WRONG, and no field marks them:
     NM 000000000007211  LAT_016 '03585664' -> 3.98 N, off Africa
     TX 230680000706298  claims inventory route 40 but sits on I-20 near
                         Weatherford, 300 miles from the corridor
   They are loaded with `location` set and `corridor_measure` LEFT NULL, because
   conflate_point's own buffer test rejects them. They never reach
   corridor_clearances, which requires a resolved position - so they read as
   UNKNOWN rather than as a structure at the wrong milepost.

4. THE FILE IS LATIN-1, per the catalog. Worth a correction: all four 2025 files
   are pure ASCII - zero bytes above 127 - so the two decodings are identical and
   the claim is untestable on this vintage. Decoding as latin-1 anyway, because it
   cannot fail on any byte sequence, where utf-8 can.

STRUCTURE_NUMBER IS NOT NATIONALLY UNIQUE, and it is the table's whole primary
key. Checked: zero collisions among the 1,543 corridor structures across the four
states, so the load is safe today. It is still the wrong key - the natural one is
(state, structure_number) - and a fifth state could break it silently, since the
INSERT is ON CONFLICT DO UPDATE. The check below fails the run rather than
letting one state quietly overwrite another.
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import os
import re
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(REPO, "build", "nbi-cache")
OUT_SQL = os.path.join(REPO, "sql", "003-nbi-structures.sql")

def _corridor_json_path() -> str:
    """Where the offline corridor lives - SAME SEARCH ORDER as core/lrs.py.

    This script only READS the corridor (for the route pattern and the state list),
    so a stale path is less destructive here than in fetch-arnold.py - but it is not
    harmless: hardcoding config/corridor.json meant this script stopped working the
    moment the corridor moved to reference/, which is where a corridor now belongs.
    config/ is copied into Lambda bundles and a corridor must not be
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


CONFIG = _corridor_json_path()

NBI_YEAR = 2025
NBI_URL = "https://www.fhwa.dot.gov/bridge/nbi/{year}/delimited/{state}{yy}.txt"

# NBI codes clearance in metres. Anything at or above this is a sentinel, not a
# structure: 30 m is 98 feet, and the tallest real value in four states is 19 ft.
# Matches the schema's clearance_sane CHECK on purpose - the loader and the
# constraint must agree, or the constraint turns a mapping bug into an outage.
SENTINEL_CLEARANCE_M = 30.0

# Inventory route prefix 1 = Interstate (NBI item 5B).
INTERSTATE_PREFIX = "1"

# Item 54A: what passes UNDER the structure. 'H' = highway.
UNDERCLEARANCE_REF_HIGHWAY = "H"


def route_pattern(route: str) -> re.Pattern:
    """Match a corridor designation as agencies actually write it.

    ANCHORED, never a substring test. 'I 40' must not match 'I 40TH ST', and TX
    writes interstates as 'IH 0040' or 'IH 40'. The number is taken from config so
    this file names no corridor of its own.
    """
    number = re.sub(r"\D", "", route) or "0"
    return re.compile(
        rf"\b(?:I|IH|INTERSTATE)[-\s]?0*{number}\b",
        re.IGNORECASE,
    )


def decode_dms(packed: str, degree_digits: int) -> float | None:
    """NBI packed DDMMSSss / DDDMMSSss -> decimal degrees.

    Returns None for blank or all-zero, which NBI uses for "not recorded". Does
    NOT validate the result: an impossible latitude is a real thing in this data
    and belongs to the corridor test to reject, not to a parser to silently fix.
    """
    packed = (packed or "").strip()
    if not packed or set(packed) == {"0"}:
        return None
    if len(packed) != degree_digits + 6:
        return None
    degrees = int(packed[:degree_digits])
    minutes = int(packed[degree_digits : degree_digits + 2])
    seconds = int(packed[degree_digits + 2 : degree_digits + 4])
    hundredths = int(packed[degree_digits + 4 : degree_digits + 6])
    return degrees + minutes / 60 + (seconds + hundredths / 100) / 3600


def clearance_metres(raw_value: str) -> tuple[float | None, str | None]:
    """A clearance field -> (metres, why_it_is_unknown).

    Returns (None, reason) for every flavour of "no restriction" so the caller can
    COUNT them. A sentinel silently becoming NULL is correct behaviour and still
    worth reporting: the ratio is how you notice a new sentinel appearing in next
    year's vintage.
    """
    value = (raw_value or "").strip()
    if not value:
        return None, "blank"
    try:
        metres = float(value)
    except ValueError:
        return None, f"unparseable ({value!r})"
    if metres == 0:
        return None, "coded 0"
    if metres >= SENTINEL_CLEARANCE_M:
        return None, f"sentinel {value} ({metres * 3.28084:.2f} ft)"
    return metres, None


def fetch_state(state: str, offline: bool) -> bytes:
    """One state's annual file, from the cache when it is there.

    Cached because the four files are 38 MB together and regenerating the
    migration should not re-download them. --offline makes the whole run
    network-free, which is what a restricted or offline network needs.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{state}{NBI_YEAR % 100}.txt")
    if os.path.exists(path):
        with open(path, "rb") as handle:
            return handle.read()
    if offline:
        raise SystemExit(
            f"--offline but {path} is not cached. Run once without --offline first."
        )
    url = NBI_URL.format(year=NBI_YEAR, state=state, yy=NBI_YEAR % 100)
    if not url.startswith("https://"):
        # NBI_URL is an https constant. Checked anyway: urlopen would accept
        # file:// or http:// from an edited constant, which is bandit B310 below.
        raise SystemExit(f"refusing a non-https NBI url {url!r}")
    print(f"  fetching {url}")
    with urllib.request.urlopen(url, timeout=180) as response:  # nosec B310
        payload = response.read()
    with open(path, "wb") as handle:
        handle.write(payload)
    return payload


def read_rows(payload: bytes) -> list[dict]:
    # latin-1 cannot raise on any byte sequence. See trap 4.
    return list(csv.DictReader(io.StringIO(payload.decode("latin-1"))))


def select(rows: list[dict], route: str, pattern: re.Pattern, state: str, stats):
    """Structures that restrict the corridor, with the clearance that applies to it.

    Two independent reasons a structure belongs here, and a structure can qualify
    both ways:

      carries  its inventory route IS the corridor -> item 10, clearance above the
               corridor's own roadway
      crosses  it spans a highway that IS the corridor -> item 54B, the clearance
               available to traffic passing underneath

    Where both apply, the smaller number wins. `relation` and `clearance_item`
    record which reading produced the value, because "14.5 ft" means something
    different depending on which one it is.
    """
    number = re.sub(r"\D", "", route)
    selected = {}

    for row in rows:
        carries = (
            row.get("ROUTE_PREFIX_005B", "").strip() == INTERSTATE_PREFIX
            and row.get("ROUTE_NUMBER_005D", "").strip().lstrip("0") == number.lstrip("0")
        )
        crosses = (
            row.get("VERT_CLR_UND_REF_054A", "").strip() == UNDERCLEARANCE_REF_HIGHWAY
            and pattern.search(row.get("FEATURES_DESC_006A") or "") is not None
        )
        if not (carries or crosses):
            continue

        candidates = []
        if carries:
            metres, reason = clearance_metres(row.get("MIN_VERT_CLR_010", ""))
            candidates.append(("carries", "010", metres, reason))
        if crosses:
            metres, reason = clearance_metres(row.get("VERT_CLR_UND_054B", ""))
            candidates.append(("crosses", "054B", metres, reason))

        # The most restrictive KNOWN value; failing that, the first reading, so the
        # structure is still recorded as present-but-unknown rather than dropped.
        known = [c for c in candidates if c[2] is not None]
        relation, item, metres, reason = (
            min(known, key=lambda c: c[2]) if known else candidates[0]
        )

        stats[f"clearance from item {item}" if metres is not None else "clearance unknown"] += 1
        if reason:
            stats[f"unknown: {reason.split(' (')[0]}"] += 1
        if len(candidates) == 2:
            stats["carries AND crosses the corridor"] += 1

        structure_number = row.get("STRUCTURE_NUMBER_008", "").strip()
        lat = decode_dms(row.get("LAT_016", ""), 2)
        lon = decode_dms(row.get("LONG_017", ""), 3)
        if lat is None or lon is None:
            stats["no coordinates"] += 1

        selected[structure_number] = {
            "structure_number": structure_number,
            "state": state,
            "route": route,  # the corridor this restricts - what the LRS view joins on
            "min_vert_clearance_m": metres,
            "clearance_item": item,
            "relation": relation,
            # NBI publishes longitude unsigned; the corridor is west of Greenwich.
            "lon": -lon if lon is not None else None,
            "lat": lat,
            "facility_carried": (row.get("FACILITY_CARRIED_007") or "").strip(),
            "features_intersected": (row.get("FEATURES_DESC_006A") or "").strip(),
            "raw": {k: (v or "").strip() for k, v in row.items() if k},
        }
    return selected


def sql_string(value) -> str:
    """Quote a value for the generated migration. THE ONLY way NBI text reaches SQL.

    Every string below comes from an FHWA delimited file - FACILITY_CARRIED_007 and
    FEATURES_DESC_006A are free text - so a bare f-string would let one apostrophe
    in a bridge name break the migration, and a crafted one write it. Doubling the
    quote is the standard Postgres escape and is why the B608 suppressions on the
    INSERT below are false positives rather than accepted risk. Numbers are
    formatted with an explicit numeric conversion, never interpolated as text.
    """
    if value is None or value == "":
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def emit(structures: list[dict], route: str, stats) -> str:
    """The migration. Idempotent, so it carries the repeatable directive.

    FULL REPLACEMENT for this route, per the catalog's `snapshotSemantics:
    cleared`: NBI is republished annually as a complete file, so a structure absent
    from the new vintage is genuinely gone rather than merely unmentioned. The
    DELETE is scoped to the route so another corridor's rows in the same table are
    not collateral.
    """
    out: list[str] = []
    w = out.append

    w("-- migration: repeatable")
    w("--")
    w("-- Read by corridor_event_hub/core/migrations.py. Repeatable because this file is")
    w("-- GENERATED: next year's NBI vintage changes its checksum, and a run-once file")
    w("-- that changes is reported as drift and never applied. Every statement below is")
    w("-- idempotent, so re-applying is exactly what should happen.")
    w("")
    w(f"-- NBI structures restricting {route}, generated by scripts/fetch-nbi.py.")
    w("-- DO NOT EDIT.")
    w("--")
    w(f"-- Source: FHWA National Bridge Inventory {NBI_YEAR}, per-state delimited files.")
    w("-- US federal government work, public domain. No credential required.")
    w("--")
    known = sum(1 for s in structures if s["min_vert_clearance_m"] is not None)
    w(f"-- {len(structures)} structures, {known} with a KNOWN clearance.")
    w("-- Rows WITHOUT one are unknown, NOT unrestricted.")
    w("")

    w("-- NOTE: clearance_item and relation are declared in 001-init.sql, not here.")
    w("-- They belong to the table, and 001 is the file that defines it - including the")
    w("-- ALTER ... ADD COLUMN IF NOT EXISTS that adds them to a cluster which predates")
    w("-- them. Declaring them in THIS file instead is what broke the first attempt:")
    w("-- 001 runs before 003, so its corridor_clearances view referenced a column that")
    w("-- did not exist yet and the whole migration rolled back.")
    w("")
    w("-- Full annual replacement (snapshotSemantics: cleared). Scoped to this route.")
    w(f"DELETE FROM bridge_structure WHERE route = {sql_string(route)};")  # nosec B608
    w("")

    for s in sorted(structures, key=lambda x: (x["state"], x["structure_number"])):
        location = (
            "NULL"
            if s["lat"] is None or s["lon"] is None
            else f"ST_SetSRID(ST_MakePoint({s['lon']:.6f}, {s['lat']:.6f}), 4326)::geography"
        )
        clearance = "NULL" if s["min_vert_clearance_m"] is None else f"{s['min_vert_clearance_m']:.2f}"
        raw = json.dumps(s["raw"], separators=(",", ":"), sort_keys=True)
        # Values reach this statement only through sql_string() or a numeric format.
        w(
            "INSERT INTO bridge_structure (structure_number, state, route, "  # nosec B608
            "min_vert_clearance_m, clearance_item, relation, location, "
            "facility_carried, features_intersected, nbi_year, raw) VALUES ("
            f"{sql_string(s['structure_number'])}, {sql_string(s['state'])}, "
            f"{sql_string(s['route'])}, {clearance}, {sql_string(s['clearance_item'])}, "
            f"{sql_string(s['relation'])}, {location}, "
            f"{sql_string(s['facility_carried'])}, {sql_string(s['features_intersected'])}, "
            f"{NBI_YEAR}, {sql_string(raw)}::jsonb) "
            "ON CONFLICT (structure_number) DO UPDATE SET "
            "state = EXCLUDED.state, route = EXCLUDED.route, "
            "min_vert_clearance_m = EXCLUDED.min_vert_clearance_m, "
            "clearance_item = EXCLUDED.clearance_item, relation = EXCLUDED.relation, "
            "location = EXCLUDED.location, "
            "facility_carried = EXCLUDED.facility_carried, "
            "features_intersected = EXCLUDED.features_intersected, "
            "nbi_year = EXCLUDED.nbi_year, raw = EXCLUDED.raw, ingested_at = now();"
        )

    w("")
    w("-- Conflate IN THE DATABASE, against centerline_m. Deliberate: the M values")
    w("-- are the corridor's own LRS measures, so a structure's milepost comes from")
    w("-- the same calibration every other position does. Doing it in the generator")
    w("-- would freeze today's geometry into the file and go stale the next time the")
    w("-- centerline is rebuilt.")
    w("--")
    w("-- A correlated subquery rather than UPDATE ... FROM: conflate_point needs this")
    w("-- row's own coordinates, and the scalar form yields NULL when the point fails")
    w("-- the corridor buffer test - which is the right answer for the two structures")
    w("-- whose published coordinates are wrong. Unresolved beats confidently wrong.")
    w("--")
    w("-- THE ::numeric CASTS ARE REQUIRED. ST_X and ST_Y return double precision,")
    w("-- conflate_point declares its parameters numeric, and Postgres does NOT")
    w("-- implicitly cast float8 -> numeric when resolving a function call. Without them:")
    w("--   function conflate_point(text, double precision, double precision) does not exist")
    w("-- which reads like a missing function rather than a type mismatch.")
    w("UPDATE bridge_structure b")
    w("SET corridor_measure = (")
    w("      SELECT c.corridor_measure")
    w("      FROM conflate_point(b.route, ST_X(b.location::geometry)::numeric,")
    w("                          ST_Y(b.location::geometry)::numeric) c")
    w("      WHERE c.on_corridor")
    w("    )")
    w(f"WHERE b.route = {sql_string(route)} AND b.location IS NOT NULL;")
    w("")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="use build/nbi-cache only")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()

    with open(CONFIG, encoding="utf-8") as handle:
        config = json.load(handle)
    route = config["route"]
    states = [s["state"] for s in config["states"]]
    pattern = route_pattern(route)

    print(f"NBI {NBI_YEAR} -> structures restricting {route}")
    print(f"states from {os.path.relpath(CONFIG, REPO)}: {', '.join(states)}\n")

    stats: collections.Counter = collections.Counter()
    everything: dict[str, dict] = {}
    for state in states:
        payload = fetch_state(state, args.offline)
        rows = read_rows(payload)
        selected = select(rows, route, pattern, state, stats)
        high_bytes = sum(1 for b in payload if b > 127)
        print(
            f"  {state}: {len(rows):>6} structures statewide, {len(selected):>4} restrict "
            f"{route}, {high_bytes} bytes >127"
        )
        collisions = set(selected) & set(everything)
        if collisions:
            # See the module docstring: structure_number is the whole primary key.
            raise SystemExit(
                f"\nFAILED - structure_number collision between {state} and an earlier "
                f"state: {sorted(collisions)[:5]}\nThe INSERT is ON CONFLICT DO UPDATE, so "
                "one state would silently overwrite the other. The primary key needs to "
                "become (state, structure_number) before this vintage can be loaded."
            )
        everything.update(selected)

    structures = list(everything.values())
    known = [s for s in structures if s["min_vert_clearance_m"] is not None]
    print(f"\n  {len(structures)} structures selected, {len(known)} with a known clearance")
    for key in sorted(stats):
        print(f"    {key:44s} {stats[key]}")

    if known:
        lowest = sorted(known, key=lambda s: s["min_vert_clearance_m"])[:5]
        print("\n  lowest clearances (what the over-height query surfaces):")
        for s in lowest:
            feet = s["min_vert_clearance_m"] * 3.28084
            print(
                f"    {feet:5.2f} ft  {s['state']} {s['structure_number']:>16}  "
                f"item {s['clearance_item']:<5} {s['features_intersected'][:34]}"
            )

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    sql = emit(structures, route, stats)
    with open(OUT_SQL, "w", encoding="utf-8") as handle:
        handle.write(sql)
    print(f"\n  wrote {os.path.relpath(OUT_SQL, REPO)}  ({len(sql) / 1024:.0f} KB)")
    print("\nApply it:  npm run db-migrate-plan   then   npm run db-migrate")
    return 0


if __name__ == "__main__":
    sys.exit(main())

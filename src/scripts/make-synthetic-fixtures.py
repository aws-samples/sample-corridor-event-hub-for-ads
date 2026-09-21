#!/usr/bin/env python3
"""Generate the SYNTHETIC test fixtures.

    python scripts/make-synthetic-fixtures.py            # write all
    python scripts/make-synthetic-fixtures.py az511      # one source
    python scripts/make-synthetic-fixtures.py --check     # fail if a file is stale

WHY THREE OF SIX FIXTURES ARE SYNTHETIC. `tests/fixtures/` is the replay path, and
captured agency bytes are worth more than anything hand-written - a hand-built fixture
encodes what we EXPECT the data to look like, and the entire difficulty of this project
is that agency data does not. So the split is decided by REDISTRIBUTION RIGHTS alone,
never by convenience:

  captured    ok-odot-wzdx, tx-dot-wzdx   CC0-1.0, declared by the feed itself in
                                          `feed_info.license` - the WZDx spec requires
                                          exactly that URL, and both comply
              nws-alerts                  NWS: public domain, weather.gov/disclaimer

  generated   aws-location-traffic        `redistributable: false` - HERE content via
                                          AWS, republication not licensed
              az511-events                terms UNKNOWN, never confirmed with ADOT
              nm-dot-weathershare         terms UNKNOWN, aggregator and NMDOT both

THE REFERENCE FOR EACH IS RECORDED, not remembered: `docs/DATA-SOURCES.md` under
"Redistribution terms, with references", and `tests/fixtures/README.md`. Do not add a
source here on the strength of a `redistributable` flag alone - that flag is this
repository's own claim, and the point of those tables is that a claim needs a citation.

UNKNOWN IS TREATED AS NOT GRANTED. Publishing this repository under MIT-0 hands every
reader the right to reuse anything in it, which we can only grant where the source
granted it to us. If someone confirms terms with ADOT or the WeatherShare operator,
delete that builder and go back to a capture - it is strictly the better test.

Each builder states which observed characteristic every record exists to reproduce.

WHAT IS LOST, SAID PLAINLY. These three fixtures no longer prove anything about the
real feeds. They prove the adapter handles the shapes we RECORDED the real feeds
having, and the record of those observations - with dates and counts - is
`docs/DATA-SOURCES.md`. A reader can verify the adapter; they cannot verify the
observation. For the three genuine captures, they can do both.

COORDINATES COME FROM THE REAL CORRIDOR, never typed in. Every on-corridor record is
placed at a vertex of the ARNOLD centerline in `reference/corridor.json` (public FHWA
geometry, already in this repository), so a synthetic record cannot silently drift off
the road and start testing the conflation buffer instead of what it was written for.
That was a real failure mode here: earlier fixtures used town centroids that sat
outside the 1,600 m buffer, and the tests passed vacuously over empty candidate lists.
"""

from __future__ import annotations

import bisect
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corridor_event_hub.core.lrs import active_corridor  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# Epoch seconds inside timeutil.EPOCH_MIN/EPOCH_MAX, around the 2026-08-07/08 window
# the genuine captures came from, so the synthetic and captured fixtures describe the
# same moment on the corridor.
AUG_07 = 1786312800  # 2026-08-07T22:00:00Z - the retrieved_at the tests use
DAY = 86_400


def coord_at(measure: float) -> tuple[float, float]:
    """The centerline vertex nearest a corridor measure, as (lon, lat).

    A VERTEX, not an interpolation: a real vertex is guaranteed to conflate back onto
    the corridor, which is the property these fixtures depend on.
    """
    corridor = _corridor()
    index = min(bisect.bisect_left(corridor.measures, measure), len(corridor.centerline) - 1)
    return corridor.centerline[index]


_CORRIDOR = None


def _corridor():
    global _CORRIDOR
    if _CORRIDOR is None:
        _CORRIDOR = active_corridor()
    return _CORRIDOR


def encode_polyline(points: list[tuple[float, float]]) -> str:
    """Google encoded polyline, so `EncodedPolyline` carries a real encoding.

    The adapter does not decode this field - it flags its presence as a known loss of
    precision. Encoding it properly anyway costs fifteen lines and means the fixture
    does not contain a string that would break the day somebody implements the decoder.
    """
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lon, lat in points:
        ilat, ilon = round(lat * 1e5), round(lon * 1e5)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            value = ~(delta << 1) if delta < 0 else (delta << 1)
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


# ---------------------------------------------------------------------------
# az511-events
# ---------------------------------------------------------------------------


def az511_record(
    *,
    native_id: int,
    measure: float,
    span: float = 1.0,
    event_type: str = "roadwork",
    event_subtype: str = "constructionWork",
    roadway_name: str = "I-40",
    direction: str | None = "East",
    lanes_affected: str = "No Data",
    lane_count: int | None = None,
    severity: str | None = "Minor",
    full_closure: bool = False,
    restrictions: dict[str, Any] | None = None,
    start_offset_days: int = -30,
    end_offset_days: int | None = 120,
) -> dict[str, Any]:
    """One AZ511 record in the vendor's own shape.

    The field names are the vendor's, including the PascalCase and the epoch-seconds
    timestamps - the shape is the whole point of the fixture.
    """
    lon, lat = coord_at(measure)
    lon2, lat2 = coord_at(measure + span)
    return {
        "ID": native_id,
        "SourceId": str(300000 + native_id),
        "Organization": "ERS",
        "RoadwayName": roadway_name,
        "DirectionOfTravel": direction,
        "Description": (
            f"{event_subtype} on {roadway_name} from MP {measure:.0f} to MP {measure + span:.0f}"
        ),
        # EPOCH SECONDS, not ISO 8601. The 1970 trap: a naive parse of these turns
        # every date into the epoch, and 1970 timestamps look like data rather than
        # like a bug.
        "Reported": AUG_07 + start_offset_days * DAY,
        "LastUpdated": AUG_07 - 3 * 3600,
        "StartDate": AUG_07 + start_offset_days * DAY,
        "PlannedEndDate": None if end_offset_days is None else AUG_07 + end_offset_days * DAY,
        "LanesAffected": lanes_affected,
        "Latitude": lat,
        "Longitude": lon,
        "LatitudeSecondary": lat2,
        "LongitudeSecondary": lon2,
        "EventType": event_type,
        "EventSubType": event_subtype,
        "IsFullClosure": full_closure,
        "Severity": severity,
        "EncodedPolyline": encode_polyline([(lon, lat), (lon2, lat2)]),
        "Restrictions": restrictions
        or {"Width": None, "Height": None, "Length": None, "Weight": None, "Speed": None},
        "DetourPolyline": "",
        "DetourInstructions": "",
        "Recurrence": "<b>Mon, Tue, Wed, Thu, Fri, Sat, Sun:</b><br/>Active all day<br/><br/>",
        "RecurrenceSchedules": [],
        "LaneCount": lane_count,
    }


def build_az511() -> list[dict[str, Any]]:
    """The AZ511 payload: a bare JSON array, as the feed serves it.

    EVERY RECORD HERE EXISTS FOR A NAMED REASON. The comments are the specification;
    if a test stops depending on one of these, delete the record rather than leaving it
    as scenery.
    """
    records = [
        # TWO CLOSURES WITH EventSubType 'constructionWork'. The trap: a closure caused
        # by construction is still a closure, and mapping on EventSubType would file
        # both as work zones. Both must place on the corridor - an earlier placeholder
        # centerline rejected the real pair as off-corridor and the assertion passed
        # over an empty list.
        az511_record(
            native_id=586001,
            measure=60.0,
            event_type="closures",
            event_subtype="constructionWork",
            lanes_affected="All lanes closed",
            lane_count=2,
            full_closure=True,
            severity="Major",
            direction="East",
        ),
        az511_record(
            native_id=586002,
            measure=195.0,
            event_type="closures",
            event_subtype="constructionWork",
            lanes_affected="All lanes closed",
            lane_count=3,
            full_closure=True,
            severity="Major",
            direction="West",
        ),
        # LANE PROSE that resolves to ordinals. 'Right' needs LaneCount to become an
        # ordinal counted from the left edge; 'Left' does not.
        az511_record(
            native_id=586003,
            measure=12.0,
            direction="East",
            lanes_affected="1 Right lane closed",
            lane_count=3,
            # DIMENSIONAL RESTRICTIONS with UNDOCUMENTED UNITS. 28 feet and 28 metres
            # are the difference between "fine" and "your truck does not fit", so the
            # adapter must flag rather than emit a dimensional_restriction candidate.
            restrictions={
                "Width": 28.0,
                "Height": None,
                "Length": None,
                "Weight": None,
                "Speed": None,
            },
        ),
        az511_record(
            native_id=586004,
            measure=120.0,
            direction="West",
            lanes_affected="1 Right lane closed",
            lane_count=2,
            severity="Major",
        ),
        az511_record(
            native_id=586005,
            measure=240.0,
            direction="East",
            lanes_affected="1 Left lane closed",
            lane_count=3,
        ),
        # Statuses that are neither open nor closed. A rolling closure moves, so at any
        # fixed point it is intermittent rather than closed.
        az511_record(
            native_id=586006, measure=283.0, direction="West", lanes_affected="Lane Rolling",
            lane_count=2,
        ),
        az511_record(
            native_id=586007, measure=300.0, direction="East",
            lanes_affected="Lanes Alternating", lane_count=2, severity="Major",
        ),
        # 'No Data' LANES: reported as unresolved, NEVER assumed open. These are also
        # the no-lane-impact half of the confidence-completeness comparison.
        az511_record(native_id=586008, measure=30.0, direction="East"),
        az511_record(native_id=586009, measure=90.0, direction="West"),
        # SEVERITY: blank and the literal string 'None' both mean "not stated", which
        # is not the same as "no impact". 1,933 of 2,453 live records were blank.
        az511_record(native_id=586010, measure=150.0, direction="East", severity=""),
        az511_record(native_id=586011, measure=210.0, direction="West", severity="None"),
        az511_record(native_id=586012, measure=270.0, direction="East", severity=None),
        # NO PlannedEndDate. Open-ended is a real state, not a missing value.
        az511_record(native_id=586013, measure=330.0, direction="West", end_offset_days=None),
        # DIRECTION LEAKS INTO RoadwayName. DirectionOfTravel is 'Unknown' or absent
        # while the name carries the answer - so the adapter reads both rather than
        # publishing UNKNOWN and losing a carriageway.
        az511_record(
            native_id=586014, measure=45.0, direction="Unknown",
            roadway_name="I-40 Westbound",
        ),
        az511_record(
            native_id=586015, measure=165.0, direction=None,
            roadway_name="I-40 Eastbound",
        ),
        # NORTH/SOUTH on an east-west corridor: a cross-street event, so the I-40
        # direction is genuinely unknown. NOT coerced to BOTH, which would over-report
        # impact on a carriageway nobody said anything about.
        az511_record(native_id=586016, measure=255.0, direction="North"),
        # MORE THAN ONE EVENT CLASS FROM ONE ENDPOINT - the thing no WZDx feed does,
        # and the reason this source matters at all.
        az511_record(
            native_id=586017, measure=105.0, event_type="accidentsAndIncidents",
            event_subtype="crash", direction="East", severity="Major",
            lanes_affected="1 Right lane closed", lane_count=3,
        ),
        az511_record(
            native_id=586018, measure=315.0, event_type="accidentsAndIncidents",
            event_subtype="disabledVehicle", direction="West",
        ),
        # ROUTE MATCHING MUST BE ANCHORED, not a substring test. Both of these are ON
        # the corridor geographically, so the ONLY thing that can reject them is the
        # name anchor - which is what makes this a real test of it. '40TH ST' contains
        # '40'; a naive `'40' in name` accepts it and puts a Phoenix surface street on
        # an interstate.
        az511_record(native_id=586019, measure=75.0, roadway_name="40TH ST"),
        az511_record(native_id=586020, measure=225.0, roadway_name="40TH ST"),
        # A POLICE DISPATCH BLOB pasted into RoadwayName. The real one carried an
        # agency, a residential street address with an apartment number, a patrol beat
        # and a live on-scene status - real personal information, found by a content
        # review of the captured payloads. The address here is invented; the SHAPE is
        # what the anchored match has to survive.
        az511_record(
            native_id=586021,
            measure=345.0,
            roadway_name=(
                "Agency: Example Police Department\n"
                "Location: 100 N EXAMPLE RD;example apt 100\n"
                "Beat: Example  "
            ),
        ),
        # AN UNMAPPED EventType is an issue, not a default. Never guess a class.
        az511_record(native_id=586022, measure=135.0, event_type="somethingNew"),
    ]
    return records


# ---------------------------------------------------------------------------
# nm-dot-weathershare
# ---------------------------------------------------------------------------
#
# THE SHAPE IS THE FINDING for this source, so the generator reproduces it exactly:
#
#   1. The payload is DOUBLE-WRAPPED: `[[ ...records... ]]`.
#   2. Some fields are PAIR-ENCODED - `["routeNumber", "40"]` - and some are plain,
#      in the SAME record. Unwrapping unconditionally truncates real lists; not
#      unwrapping at all makes every route comparison fail.
#   3. There is no `route` field on an NMDOT record. The route is split across
#      `routeName` ("I") and `routeNumber` ("40"), so `'I-40' in record['route']`
#      finds nothing and a whole state goes missing quietly.
#   4. There are NO event times. `starttime` and `endtime` are empty strings on every
#      record, so start time falls back to fetch time - which is legitimate only
#      because the adapter says so in an issue.
#   5. There are NO identifiers. No `id`, `uid` or `log-id`, so native ids are
#      synthesized and labelled as synthesized.
#   6. The file carries FOUR upstream agencies. AZDOT records are empty shells.

NM_UPDATED = "202608112043 UTC"


def paired(field: str, value: Any) -> list[Any]:
    """The aggregator's self-named pair encoding: `["routeNumber", "40"]`."""
    return [field, value]


def nm_record(
    *,
    route_name: str,
    route_number: str,
    event_type: int,
    name: str,
    title: str,
    body: str = "",
    milepost: float | None = None,
    lon: float | None = None,
    lat: float | None = None,
) -> dict[str, Any]:
    """One NMDOT record. Fields the adapter reads are pair-encoded or plain to match.

    `description` is `title~~~body` - the aggregator's own delimiter, and the only
    place lane detail exists, because there is no lane field.
    """
    if lon is None or lat is None:
        # Place the coordinate at the same milepost the prose states, so the
        # milepost/coordinate agreement check has something honest to compare. A
        # record with no milepost gets a plain corridor coordinate in New Mexico.
        corridor = _corridor()
        from corridor_event_hub.core.lrs import LocalConflator, MilepostInput

        conflator = LocalConflator(corridor=corridor)
        resolved = conflator.conflate(
            MilepostInput(state="NM", begin_mp=milepost if milepost is not None else 100.0)
        )
        lon, lat = coord_at(resolved.begin_measure)
    return {
        # NOTE what is absent: no `id`, `uid`, `log-id`, `logId` or `index`. That
        # absence is why native ids have to be synthesized, and a test asserts it.
        "routeName": paired("routeName", route_name),
        "routeNumber": paired("routeNumber", route_number),
        "eventType": paired("eventType", event_type),
        "description": paired("description", f"{title}~~~{body}" if body else title),
        "name": name,
        "type": "Construction",
        "source": "NMDOT",
        "latitude": lat,
        "longitude": lon,
        "milepost": "" if milepost is None else f"{milepost:.3f}",
        "road_dir": "",
        "routeName_display": f"{route_name}-{route_number}",
        "icon": "construction",
        # NO TIMES. Empty strings, not nulls and not absent keys - the distinction
        # matters because an absent key and an empty string reach different branches.
        "starttime": "",
        "endtime": "",
        "updated": NM_UPDATED,
        "updatedDate": "2026-08-11",
        "updatedTime": "14:43:02",
        "severity": "",
        "delay": "",
        "advice": "",
        "closed-lanes": "",
        "total-lanes": "",
    }


def build_nm_weathershare() -> list[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = [
        # ------------------------------------------------------------------
        # THE TWO RECORDS ACTUALLY ON I-40. Both must carry a prose milepost:
        # NMDOT mileposts are STATE mileposts and are the authority on position,
        # so the extent comes from the milepost rather than the coordinate.
        # ------------------------------------------------------------------
        nm_record(
            route_name="I",
            route_number="40",
            event_type=9,  # Roadwork -> work_zone
            name="Roadwork, I 40 eastbound from mile marker 140 to mile marker 146",
            title="Roadwork",
            body="Eastbound driving lane closed on I-40 for bridge deck repair.",
            milepost=140.0,
        ),
        nm_record(
            route_name="I",
            route_number="40",
            event_type=8,  # Lane Closure -> closure
            name="Lane Closure, I 40 westbound at mile marker 164",
            title="Lane Closure",
            body="Westbound left lane closed on I-40.",
            milepost=164.0,
        ),
        # ------------------------------------------------------------------
        # THE THREE PROSE FALSE POSITIVES. Each mentions I-40 and is NOT on it, so
        # a text search over the prose over-reports by 150%. This is the set that
        # justifies structural route matching; if it ever collapses into the set
        # above, the structural matcher stopped earning its complexity.
        # ------------------------------------------------------------------
        # The dangerous one. A genuine height restriction SOMEWHERE ELSE that
        # recommends I-40 as the truck alternative. Admitting it would publish a
        # 13'6" clearance limit ON the corridor it tells trucks to use - the exact
        # inversion of the fact, on the one class where being wrong strands a truck
        # under a bridge.
        nm_record(
            route_name="US",
            route_number="54",
            event_type=7,  # 'Alert' - deliberately unmapped
            name="Low Clearance Structure, US 54 at mile marker 12",
            title="Low Clearance Structure",
            body="CMV's please use I-40 between exits 89 & 96. Height Restriction 13'6\".",
            milepost=12.0,
        ),
        nm_record(
            route_name="NM",
            route_number="566",
            event_type=9,
            name="Roadwork, NM 566 at mile marker 0, Church Rock (I-40)",
            title="Roadwork",
            body="Shoulder work near the I-40 interchange.",
            milepost=None,
        ),
        nm_record(
            route_name="NM",
            route_number="566",
            event_type=9,
            name="Roadwork, NM 566 from mile marker 6 to mile marker 9",
            title="Roadwork",
            body="6 miles north of I-40, expect delays.",
            milepost=None,
        ),
        # ------------------------------------------------------------------
        # THE REST OF NEW MEXICO. Not on I-40, so they exit at the route gate and
        # produce no issues - which is the point: another route's records must not
        # flood the review queue. Most carry a prose milepost, matching the observed
        # file where 67 of 84 records do.
        # ------------------------------------------------------------------
        nm_record(
            route_name="US", route_number="285", event_type=9,
            name="Roadwork, US 285 from mile marker 120 to mile marker 124",
            title="Roadwork", body="Utility work.", milepost=None,
        ),
        nm_record(
            route_name="US", route_number="70", event_type=13,  # wet roads
            name="Fair Driving Conditions, US 70 at mile marker 44",
            title="Fair Driving Conditions", body="Roads are wet.", milepost=None,
        ),
        nm_record(
            route_name="US", route_number="64", event_type=16,
            name="Difficult Driving Conditions, US 64 at mile marker 88",
            title="Difficult Driving Conditions", body="Blowing dust.", milepost=None,
        ),
        nm_record(
            route_name="NM", route_number="14", event_type=5,
            name="Closure, NM 14 from mile marker 10 to mile marker 14",
            title="Closure", body="Road closed for bridge replacement.", milepost=None,
        ),
        nm_record(
            route_name="NM", route_number="38", event_type=20,
            name="Seasonal Closure, NM 38 from mile marker 2 to mile marker 20",
            title="Seasonal Closure", body="Closed for the season.", milepost=None,
        ),
        # eventType 19: a single observed record. n=1 is not a vocabulary, so it
        # quarantines rather than being read as the closure it looks like.
        nm_record(
            route_name="NM", route_number="423", event_type=19,
            name="Closure, Montgomery Blvd. Loop Ramp at mile marker 3",
            title="Closure, Montgomery Blvd. Loop Ramp", milepost=None,
        ),
        # No prose milepost at all - 17 of 84 observed records have none.
        nm_record(
            route_name="US", route_number="491", event_type=7,
            name="Alert, Alert", title="Alert", body="Advisory only.", milepost=None,
        ),
        nm_record(
            route_name="NM", route_number="602", event_type=7,
            name="Alert, Alert", title="Alert", body="Advisory only.", milepost=None,
        ),
    ]

    # FILLER TO THE OBSERVED PROPORTIONS: 84 NMDOT of 104 total records. The counts
    # are not decoration - "38% of the file is empty AZDOT shells" and "67 of 84
    # records carry a prose milepost" are claims the adapter's design rests on, and a
    # fixture with fifteen records cannot exercise the same behaviour as one with a
    # hundred. Deterministic, and never on I-40: every filler record must exit at the
    # route gate, or it becomes a candidate and breaks the count that matters.
    filler_routes = [("US", "285"), ("US", "70"), ("NM", "14"), ("NM", "518"),
                     ("US", "550"), ("NM", "128"), ("US", "84"), ("NM", "6")]
    filler_types = [9, 13, 16, 5, 7, 20]
    while len(records) < 84:
        index = len(records)
        route_name, route_number = filler_routes[index % len(filler_routes)]
        event_type = filler_types[index % len(filler_types)]
        milepost_prose = index % 5 != 4  # 4 in 5 carry a prose milepost
        begin = float(10 + (index % 30))
        records.append(
            nm_record(
                route_name=route_name,
                route_number=route_number,
                event_type=event_type,
                name=(
                    f"Roadwork, {route_name} {route_number} from mile marker {begin:.0f}"
                    f" to mile marker {begin + 3:.0f}"
                    if milepost_prose
                    else f"Alert, {route_name} {route_number}"
                ),
                title="Roadwork" if milepost_prose else "Alert",
                body="Utility work." if milepost_prose else "Advisory only.",
                milepost=None,
            )
        )

    # ------------------------------------------------------------------
    # THREE OTHER UPSTREAM AGENCIES, on purpose. Each has a DIFFERENT schema, so
    # applying NMDOT field logic to them produces garbage issues about another
    # state rather than useful ones - which is why the adapter filters on `source`
    # before it reads a single field.
    # ------------------------------------------------------------------
    caltrans = {
        "id": "C1CB-SR-1-North / South-Example-82.550",
        "log-id": "1",
        "updatedDate": "2026-08-11",
        "updatedTime": "13:43:02",
        "route": "SR-1",  # note: a `route` field, which NMDOT records do not have
        "direction": "North / South",
        "longitude": -123.800471,
        "latitude": 39.698544,
        "facility": "Conventional Hwy",
        "type-closure": "One-Way Traffic",
        "type-work": "Drainage Work",
        "delay": "Not Reported",
        "closed-lanes": "1",
        "total-lanes": "2",
        "uid": "C1CB-SR-1-North / South-Example-82.550",
        "starttime": ["202603191401 UTC"],
        "endtime": ["202608310301 UTC"],
        "updated": NM_UPDATED,
        "advice": "Lane(s) 1 closed out of 2 total lanes",
        "description": "One-Way Traffic Closure for Drainage Work on Route SR-1",
        "severity": "Expect Not Reported minute delays",
        "type": "Construction",
        "typeabbr": "C",
        "source": "CALTRANS",
        "start_loc": {"county": "Example", "postmile": "82.550", "direction": "",
                      "description": "", "location": ""},
        "end_loc": {"county": "Example", "postmile": "82.910", "direction": "",
                    "description": "", "location": ""},
        "location": "",
    }
    oregon = {
        "id": "OR-205-1",
        "route": "I205",
        "source": "OregonDOT",
        "latitude": 45.412,
        "longitude": -122.573,
        "description": "Incident on I205 northbound",
        "type": "Info",
        "starttime": ["202608110900 UTC"],
        "endtime": [""],
        "updated": NM_UPDATED,
    }
    # EMPTY AZDOT SHELLS. 38% of the observed file. They must be skipped before any
    # field logic runs, or every poll floods the review queue with issues about
    # Arizona - a state this adapter is not responsible for.
    azdot_shells = [{"source": "AZDOT", "type": None, "route": None} for _ in range(6)]

    records = (
        records
        + [dict(caltrans, id=f"C1CB-SR-1-{n}", uid=f"C1CB-SR-1-{n}") for n in range(8)]
        + [dict(oregon, id=f"OR-205-{n}") for n in range(6)]
        + azdot_shells
    )

    # DOUBLE-WRAPPED, as the aggregator serves it.
    return [records]


# ---------------------------------------------------------------------------
# aws-location-traffic
# ---------------------------------------------------------------------------
#
# This one needs an MVT ENCODER, because the fixture is not JSON - it is base64
# protobuf vector tiles, and `tests/test_mvt.py` runs them through the real decoder in
# `core/mvt.py`. A fixture of placeholder strings would test the base64 envelope and
# nothing below it.
#
# The encoder below is the exact inverse of that decoder, which is what keeps the two
# honest: `core/mvt.py` defines the wire subset MVT actually uses here (layers, packed
# tags, packed zigzag geometry commands, typed Tile.Value), and this writes precisely
# that subset back. If the decoder grows support for something, this will not emit it,
# and the test that asserts a decoded segment count is what will say so.

_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LENGTH = 2
_WIRE_32BIT = 5

MVT_EXTENT = 4096
MVT_ZOOM = 8

# The tile addresses the live capture covered. Kept verbatim: they are z8 addresses
# derived from the corridor's own bounding box, so they carry no licensed content, and
# keeping them means the envelope this fixture presents is the one the collector really
# sees. (59, 100) is deliberately among them and the corridor does NOT pass through it -
# a tile with traffic on it, none of it on I-40.
TILE_ADDRESSES = [
    (46, 101), (47, 101), (48, 101), (49, 101), (50, 101), (51, 101),
    (52, 101), (53, 101), (54, 101), (55, 101), (56, 101), (57, 100),
    (58, 100), (59, 101), (59, 100), (60, 100), (60, 101),
]

# Epoch seconds for the incident layer, inside timeutil's plausible window.
ALS_CAPTURED_AT = "2026-08-11T00:00:00.000Z"
ALS_INCIDENT_START = 1786492800  # 2026-08-11T12:00:00Z
ALS_INCIDENT_STOP = 1786507200  # 2026-08-11T16:00:00Z


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _zz(value: int) -> int:
    """Zigzag encode, the inverse of core/mvt._zigzag."""
    return (value << 1) if value >= 0 else (~value << 1) | 1


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _bytes_field(field: int, payload: bytes) -> bytes:
    return _tag(field, _WIRE_LENGTH) + _varint(len(payload)) + payload


def _varint_field(field: int, value: int) -> bytes:
    return _tag(field, _WIRE_VARINT) + _varint(value)


def _mvt_value(value: Any) -> bytes:
    """One `Tile.Value`: exactly one of seven typed fields, per spec 4.1.

    The type choice is not cosmetic. `core/mvt._decode_value` returns field 1 as str,
    fields 4/5 as int and field 2 as a real float (because `_iter_fields` unpacks
    wire type 5 with struct). A ratio written as a string would arrive as a string and
    the adapter's numeric extensions would silently carry text.
    """
    import struct

    if isinstance(value, bool):
        return _bytes_field(4, _varint_field(7, 1 if value else 0))
    if isinstance(value, float):
        return _bytes_field(4, _tag(2, _WIRE_32BIT) + struct.pack("<f", value))
    if isinstance(value, int):
        return _bytes_field(4, _varint_field(4, value))
    return _bytes_field(4, _bytes_field(1, str(value).encode("utf-8")))


def lonlat_to_tile_px(
    lon: float, lat: float, tile_x: int, tile_y: int, zoom: int = MVT_ZOOM,
    extent: int = MVT_EXTENT,
) -> tuple[int, int]:
    """lon/lat -> tile-local integer coordinate. Inverse of mvt.tile_point_to_lonlat."""
    import math

    world_x = (lon + 180.0) / 360.0
    world_y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0
    scale = extent * (2.0**zoom)
    return (
        round(world_x * scale - tile_x * extent),
        round(world_y * scale - tile_y * extent),
    )


def encode_layer(name: str, features: list[dict[str, Any]]) -> bytes:
    """One MVT layer. `features` are dicts of `{"line": [(lon,lat)...], "props": {...}}`.

    Keys and values are interned into the layer dictionaries and referenced by index
    from each feature's packed tag array, which is how MVT keeps repeated attribute
    names out of every feature - and the indirection the decoder has to walk.
    """
    keys: list[str] = []
    values: list[Any] = []
    encoded_features: list[bytes] = []

    for feature in features:
        tags: list[int] = []
        for key, value in feature["props"].items():
            if value is None:
                continue  # An absent attribute, not a null one - MVT has no null.
            if key not in keys:
                keys.append(key)
            if value not in values:
                values.append(value)
            tags.extend([keys.index(key), values.index(value)])

        # Geometry: MoveTo(1) for the first point, then LineTo(n-1). Deltas
        # accumulate, so every parameter after the first is relative to the cursor.
        commands: list[int] = []
        cursor_x = cursor_y = 0
        points = [
            lonlat_to_tile_px(lon, lat, feature["tile"][0], feature["tile"][1])
            for lon, lat in feature["line"]
        ]
        commands.append((1 << 3) | _CMD_MOVE_TO)
        commands.extend([_zz(points[0][0] - cursor_x), _zz(points[0][1] - cursor_y)])
        cursor_x, cursor_y = points[0]
        if len(points) > 1:
            commands.append(((len(points) - 1) << 3) | _CMD_LINE_TO)
            for px, py in points[1:]:
                commands.extend([_zz(px - cursor_x), _zz(py - cursor_y)])
                cursor_x, cursor_y = px, py

        body = b"".join(
            [
                _bytes_field(2, b"".join(_varint(t) for t in tags)),  # tags, packed
                _varint_field(3, 2),  # geometry type: LineString
                _bytes_field(4, b"".join(_varint(c) for c in commands)),  # packed
            ]
        )
        encoded_features.append(_bytes_field(2, body))

    return _bytes_field(
        3,
        b"".join(
            [
                _bytes_field(1, name.encode("utf-8")),
                *encoded_features,
                *[_bytes_field(3, k.encode("utf-8")) for k in keys],
                *[_mvt_value(v) for v in values],
                _varint_field(5, MVT_EXTENT),
            ]
        ),
    )


_CMD_MOVE_TO = 1
_CMD_LINE_TO = 2


def _corridor_points_in_tile(tile: tuple[int, int]) -> list[tuple[float, float]]:
    """Centerline vertices that fall inside this tile, in corridor order."""
    import math

    corridor = _corridor()
    n = 2**MVT_ZOOM
    out = []
    for lon, lat in corridor.centerline:
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        if (x, y) == tile:
            out.append((lon, lat))
    return out


def build_aws_location_traffic() -> dict[str, Any]:
    """The tile envelope: base64 MVT bodies keyed by tile address.

    THE SEGMENT MIX IS THE SPECIFICATION. Live tiles were overwhelmingly `free` -
    1,535 of 1,586 flow features - with congestion on a small minority, and the
    adapter's whole job is to emit events for the minority without flooding the store
    with the majority. A fixture of nothing but congestion would not exercise that.
    """
    import base64

    # kind cycles so every mapped congestion subtype appears, and `free` dominates the
    # way it does live. `free` and `none` must produce NO candidates.
    flow_kinds = ["free", "free", "queuing", "free", "slow", "free", "free",
                  "stationary", "free", "minor", "free", "none"]
    incident_kinds = ["accident", "construction", "closure", "disabled_vehicle"]

    tiles = []
    for index, tile in enumerate(TILE_ADDRESSES):
        on_corridor = _corridor_points_in_tile(tile)
        features = []

        # MAINLINE FLOW along the corridor itself. Two-point segments: enough to be a
        # LineString, short enough that a segment stays inside one tile.
        for segment in range(4):
            kind = flow_kinds[(index * 4 + segment) % len(flow_kinds)]
            if len(on_corridor) < 8:
                continue
            step = len(on_corridor) // 5
            line = on_corridor[segment * step : segment * step + 2] or on_corridor[:2]
            if len(line) < 2:
                continue
            features.append(
                {
                    "tile": tile,
                    "line": line,
                    "props": {
                        "id": f"als-seg-{tile[0]}-{tile[1]}-{segment}",
                        "kind": kind,
                        # MAINLINE ONLY. Local streets inside the corridor buffer
                        # would flood an interstate with city traffic.
                        "road_kind_detail": "motorway",
                        "road_kind": "major_road",
                        "network": "us_interstate",
                        # Numeric, and typed as numbers on the wire: a ratio that
                        # arrives as a string is a silent downgrade.
                        "speed": 24.0 if kind in ("queuing", "stationary") else 88.0,
                        "congestion": 0.82 if kind in ("queuing", "stationary") else 0.05,
                        "is_link": False,
                        "is_bridge": False,
                        "is_tunnel": False,
                        "min_zoom": 8,
                        # ATTRIBUTION IS A LICENCE OBLIGATION, so it travels per
                        # record rather than living only in the catalog.
                        "source": "HERE",
                    },
                }
            )

        # A LOCAL STREET, congested. Inside the corridor buffer and correctly ignored:
        # this is the filter that keeps city traffic off an interstate.
        if on_corridor:
            features.append(
                {
                    "tile": tile,
                    "line": on_corridor[:2] if len(on_corridor) > 1 else on_corridor * 2,
                    "props": {
                        "id": f"als-local-{tile[0]}-{tile[1]}",
                        "kind": "stationary",
                        "road_kind_detail": "residential",
                        "road_kind": "minor_road",
                        "speed": 8.0,
                        "congestion": 0.95,
                        "source": "HERE",
                    },
                }
            )

        # OFF-CORRIDOR MAINLINE congestion: real motorway traffic that is not on I-40.
        # `off_corridor` has to be COUNTED rather than silently dropped, so at least
        # one of these must exist - and on (59, 100) the corridor never enters the
        # tile at all, which is the honest version of that case.
        offset_lon, offset_lat = (
            (on_corridor[0][0], on_corridor[0][1] + 1.2) if on_corridor else (-96.9, 36.4)
        )
        features.append(
            {
                "tile": tile,
                "line": [(offset_lon, offset_lat), (offset_lon + 0.01, offset_lat)],
                "props": {
                    "id": f"als-off-{tile[0]}-{tile[1]}",
                    "kind": "queuing",
                    "road_kind_detail": "motorway",
                    "road_kind": "major_road",
                    "speed": 20.0,
                    "congestion": 0.9,
                    "source": "HERE",
                },
            }
        )

        layers = [encode_layer("traffic_flow", features)]

        # THE INCIDENTS LAYER, on some tiles. Unlike flow, incidents carry epoch
        # start/stop times - so class 1-3 events arrive from this source with real
        # temporal bounds rather than with our fetch time standing in for them.
        if index % 4 == 0 and len(on_corridor) >= 4:
            kind = incident_kinds[(index // 4) % len(incident_kinds)]
            layers.append(
                encode_layer(
                    "traffic_incidents",
                    [
                        {
                            "tile": tile,
                            "line": on_corridor[1:3],
                            "props": {
                                "id": f"als-inc-{tile[0]}-{tile[1]}",
                                "kind": kind,
                                "road_kind_detail": "motorway",
                                "start_time": ALS_INCIDENT_START,
                                "stop_time": ALS_INCIDENT_STOP,
                                "warning_level": "major",
                                "source": "HERE",
                            },
                        }
                    ],
                )
            )

        body = b"".join(layers)
        tiles.append(
            {
                "z": MVT_ZOOM,
                "x": tile[0],
                "y": tile[1],
                "bytes": len(body),
                "mvtBase64": base64.b64encode(body).decode("ascii"),
            }
        )

    return {
        "$comment": (
            "SYNTHETIC vector.traffic tiles, generated by "
            "scripts/make-synthetic-fixtures.py. NOT a capture: Amazon Location "
            "traffic is licensed and not redistributable (config/sources.json marks "
            "the source redistributable: false), so this repository cannot carry the "
            "real bytes. Geometry is the ARNOLD corridor centerline from "
            "reference/corridor.json; the attribute vocabulary and the "
            "free-flow-dominant segment mix reproduce what docs/DATA-SOURCES.md "
            "records observing on 2026-08-11."
        ),
        "tileset": "vector.traffic",
        "zoom": MVT_ZOOM,
        "capturedAt": ALS_CAPTURED_AT,
        "tiles": tiles,
    }


# ---------------------------------------------------------------------------

BUILDERS = {
    "az511": ("az511-events.json", build_az511),
    "nm": ("nm-dot-weathershare.json", build_nm_weathershare),
    "als": ("aws-location-traffic.json", build_aws_location_traffic),
}


def _describe(document: Any) -> str:
    """Record count, seeing through each feed's own wrapper.

    Reported per source rather than as `len(document)`, because two of the three are
    wrapped: New Mexico double-wraps its array and the tile envelope is an object.
    A flat length prints "1 record" for a 104-record file.
    """
    if isinstance(document, dict) and "tiles" in document:
        return f"{len(document['tiles'])} tiles"
    if isinstance(document, list) and len(document) == 1 and isinstance(document[0], list):
        return f"{len(document[0])} records (double-wrapped)"
    return f"{len(document)} records"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    wanted = [a for a in argv if not a.startswith("-")] or list(BUILDERS)

    stale = []
    for name in wanted:
        if name not in BUILDERS:
            print(f"unknown fixture {name!r}; known: {', '.join(BUILDERS)}", file=sys.stderr)
            return 2
        filename, builder = BUILDERS[name]
        path = FIXTURE_DIR / filename
        body = json.dumps(builder(), indent=1) + "\n"
        if check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != body:
                stale.append(filename)
                print(f"STALE  {filename} - re-run scripts/make-synthetic-fixtures.py")
            else:
                print(f"ok     {filename}")
            continue
        path.write_text(body, encoding="utf-8")
        print(f"wrote  {filename}  ({_describe(builder())})")

    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

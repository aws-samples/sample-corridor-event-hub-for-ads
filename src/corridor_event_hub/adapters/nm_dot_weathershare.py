"""New Mexico DOT road conditions, via the WeatherShare OSS aggregator.

Feed:    https://oss.weathershare.org/data/ROADINFO/OSS_roadinfo.json
Spec:    NONE - aggregator-proprietary JSON, undocumented, and it VARIES BY UPSTREAM
License: unknown - aggregator terms unstated, NMDOT terms unconfirmed
Auth:    none - fully public, no key, no registration

===========================================================================
THIS IS THE FIRST SOURCE READ THROUGH AN AGGREGATOR, AND THE FIRST NM FEED.
===========================================================================

New Mexico was the one corridor state with no working feed: ``nm-dot-wzdx``
(NMDOT work zones republished by Blyncsy) has been returning 503. This reaches the
same agency's data by a different route, with no credential and no agency contact.

Verified live 2026-08-11: 5,407 roadinfo records from 8 upstream DOTs, 84 of them
NMDOT, 2 of those on I-40.

WHY THIS ADAPTER IS SCOPED TO NMDOT, AND WHY THAT IS A DESIGN DECISION RATHER THAN
LAZINESS. The endpoint carries Caltrans, AZDOT, NMDOT, MDOT, WSDOT, OregonDOT,
UDOT and NDOT simultaneously. The catalog attaches ONE ``independenceGroup`` to a
source_id, so an adapter emitting candidates from all eight would need a group that
is correct for none of them - and the failure is not cosmetic: a re-served ADOT
closure would corroborate THE SAME closure from ``az511-events``, inflating
confidence exactly where the independence grouping exists to prevent it. Filtering to a single upstream
makes ``independenceGroup: nmdot`` true rather than approximate. Reading the other
upstreams needs per-candidate independence, which is a schema change - see the
catalog entry.

RELATED-SOURCE WARNING: if ``nm-dot-wzdx`` ever comes back up, it and this
source are BOTH NMDOT data and MUST NOT corroborate each other. Its catalog group
is currently ``blyncsy`` (the vendor hosting it), which would make them look
independent. That is a latent bug, harmless only because that feed is down and has
no adapter. Flagged in the catalog.

WHAT THIS FEED GIVES US THAT NOTHING ELSE DOES:
  - REAL AGENCY ROAD-SURFACE REPORTS. eventType 13/16 are 'Fair Driving Conditions
    - Roads are wet' and 'Difficult Driving Conditions', 25 of 84 records. Class 6
    everywhere else in this catalog is DERIVED (from NWS alerts, or absent because
    Caltrans did not put pavement sensors on I-40). These are an agency stating a
    surface condition directly.
  - MILEPOST RANGES, on 52 of 84 records. The only NM source giving a linear extent
    rather than a point - and mileposts are what the corridor LRS is built on.

AND WHAT MAKES IT HARDER THAN ANY FEED SO FAR:
  - The JSON root is an array of length 1 CONTAINING the array of records.
  - NMDOT values are ``[fieldname, value]`` PAIRS: ``"routeNumber": ["routeNumber",
    "40"]``. The key is repeated inside its own value.
  - THERE IS NO RECORD ID. Not one field on any of the 84 records identifies the
    record. ``native_id`` has to be synthesized - see ``_synthesize_native_id``.
  - THERE ARE NO EVENT TIMES. ``starttime`` and ``endtime`` are the empty string on
    all 84 records, and ``updated`` is IDENTICAL across all 84 - it is the
    aggregator's scrape time, not a per-record update.
  - Route lives in TWO fields (``routeName`` 'I' + ``routeNumber`` '40'), never as
    'I-40'.
  - Mileposts, direction, and lane detail are all in PROSE, in the ``name`` field.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..core.lrs import CoordinateInput, MilepostInput
from ..core.timeutil import iso_utc
from ..core.types import (
    CandidateEvent,
    Extent,
    GeoJsonGeometry,
    LaneImpact,
    MappingIssue,
    SourceRef,
)
from .adapter import Adapter, AdapterContext, AdapterResult, issue, record_issues

# The upstream this adapter reads. Everything else in the payload is skipped - see
# the module docstring for why that is a correctness requirement, not a shortcut.
UPSTREAM_SOURCE = "NMDOT"

# The state whose mileposts these records use. NMDOT records never say so; it is
# implied by the upstream, which is exactly why it is named once here rather than
# assumed at each use.
UPSTREAM_STATE = "NM"

# NMDOT ``eventType`` -> canonical class.
#
# THE ENUM IS UNDOCUMENTED. This mapping was derived by correlating all 84 observed
# records against the title the aggregator renders alongside them, and the
# correlation was 1:1 with no crossover - eventType 9 was 'Roadwork' 33 times out of
# 33, and so on. That is strong evidence and it is still INFERENCE, which is why
# unmapped values quarantine instead of falling back to a default.
#
# 8 ('Lane Closure') maps to `closure`, NOT `work_zone`, even though its `type`
# field says 'Construction'. The same call the AZ511 adapter makes: a
# closure caused by roadwork is still a closure, and the work-zone relationship is
# expressed by linking rather than by collapsing the classes.
EVENT_TYPE_TO_CLASS: dict[int, str] = {
    5: "closure",  # 'Closure' (3 observed)
    8: "closure",  # 'Lane Closure' (4 observed)
    9: "work_zone",  # 'Roadwork' (33 observed)
    13: "road_surface",  # 'Fair Driving Conditions - Roads are wet' (13)
    16: "road_surface",  # 'Difficult Driving Conditions' (12)
    20: "closure",  # 'Seasonal Closure' (2 observed)
}

# DELIBERATELY NOT MAPPED, with the reason recorded so a future reader does not
# "fix" it by guessing:
#
#   7  - 'Alert' (16 observed). A container word, not a class. The observed 7s
#        include a low-clearance structure with a height restriction
#        (dimensional_restriction), a truck-length prohibition
#        (dimensional_restriction), and general advisories (no class at all). One
#        eventType covering three classes cannot be mapped without reading the
#        prose, and prose-driven classification is exactly what is ruled out.
#   19 - one single observed record, title 'Closure, Montgomery Blvd. Loop Ramp'.
#        It looks like a closure. n=1 is not a vocabulary.
#
# Both quarantine to the review queue. 17 of 84 records, which is a lot to leave on
# the floor and still better than mapping them wrongly - ask NMDOT for the enum.
UNMAPPED_EVENT_TYPES: dict[int, str] = {
    7: "'Alert' spans dimensional_restriction and non-events; needs the NMDOT enum",
    19: "single observed record; n=1 is not a vocabulary",
}

# ``description`` packs a title and a body with a '~~~' delimiter. Some records
# carry no delimiter at all, in which case the whole string is the title.
_TITLE_DELIMITER = "~~~"

# 'from mile marker 344, 8 miles east of Tucumcari to mile marker 350, ...'
# Non-greedy across the interstitial prose, which itself contains the word 'miles'.
_MILEPOST_RANGE = re.compile(
    r"from\s+mile\s*(?:marker|post)\s*(\d+(?:\.\d+)?).*?"
    r"\bto\s+mile\s*(?:marker|post)\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)
# 'at mile marker 164, Anthony.'
_MILEPOST_POINT = re.compile(
    r"at\s+mile\s*(?:marker|post)\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)

_EASTBOUND = re.compile(r"\beastbound\b", re.IGNORECASE)
_WESTBOUND = re.compile(r"\bwestbound\b", re.IGNORECASE)
_NORTHBOUND = re.compile(r"\bnorthbound\b", re.IGNORECASE)
_SOUTHBOUND = re.compile(r"\bsouthbound\b", re.IGNORECASE)

_LANE_CLOSED = re.compile(r"\b(driving|left|right|inside|outside)\s+lane\s+closed\b", re.I)

# How far the prose milepost and the record's own coordinate may disagree before the
# gap is reported for review. This does NOT decide which one is used - see
# ``_resolve_extent``; the milepost always wins when it resolves.
#
# 25 MILES LOOKS ABSURD AND IS CURRENTLY CORRECT, because the disagreement is mostly
# OURS. Measured against the placeholder centerline in config/corridor.json:
#
#   geometric length of the centerline : 1163.2 mi
#   length of the milepost model       : 1241.0 mi
#
# The centerline is 6.3% SHORT, and the error accumulates eastward: a coordinate at
# the NM/TX state line resolves to NM milepost 351 where corridor.json's own model
# puts that line at 373.5 - a 22-mile disagreement produced entirely by our own
# geometry. A 5-mile threshold flags healthy records; the real live I-40 roadwork
# record at NM MP 344-350 misses its coordinate by 18.7 mi for this reason alone.
#
# So while ``corridor.verified`` is False this threshold detects gross prose-parsing
# errors and nothing finer. WHEN REAL CORRIDOR GEOMETRY LANDS, TIGHTEN THIS - at
# that point the gap becomes a genuine data-quality signal instead of a measurement
# of our placeholder. The per-record gap is recorded in extensions either way, so the
# distribution can be checked rather than argued about.
MILEPOST_COORDINATE_REPORT_THRESHOLD_MILES = 25.0

# 'YYYYMMDDHHMM UTC' - the aggregator's own format, and the only source in this
# catalog that states its zone in the value.
_AGGREGATOR_TIME = re.compile(r"^(\d{12})\s+UTC$")


def unpair(value: Any, field_name: str) -> Any:
    """Undo NMDOT's ``[fieldname, value]`` encoding.

    ``"routeNumber": ["routeNumber", "40"]`` - the key is repeated inside its own
    value. Reading the field directly yields a 2-list, and ``str()`` of that looks
    almost plausible in a log (``['routeNumber', '40']``), which is how this
    survives to production.

    Only unwraps when the first element actually matches the field name. A genuine
    2-element list of data would be left alone.
    """
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0] == field_name
    ):
        return value[1]
    return value


def unwrap_payload(doc: Any) -> tuple[list[Any] | None, str | None]:
    """Unwrap the double-nested root.

    The root is ``[[{...}, {...}]]`` - an array of length 1 whose single element is
    the real array. Returns ``(records, error)``.

    Written as a loop rather than a hardcoded ``doc[0]`` so that the aggregator
    dropping or adding a wrapping level does not break the adapter. A shape change
    is still reported when nothing list-like is found.
    """
    if not isinstance(doc, list):
        return None, f"expected a JSON array at the root, got {type(doc).__name__}"
    inner = doc
    depth = 0
    while isinstance(inner, list) and len(inner) == 1 and isinstance(inner[0], list):
        inner = inner[0]
        depth += 1
        if depth > 4:  # runaway guard; the observed shape needs exactly one unwrap
            return None, "more than 4 levels of array nesting"
    if not isinstance(inner, list):
        return None, "no record array found inside the wrapper"
    return inner, None


def parse_aggregator_time(value: Any) -> str | None:
    """``'202608111953 UTC'`` -> ISO 8601, or None.

    Deliberately strict about the ' UTC' suffix. The digits alone are ambiguous
    (they look like an epoch to nothing and like a date to everything), and the
    aggregator stating its zone explicitly is the one thing that makes this field
    trustworthy - so a value that stops saying UTC should stop parsing.
    """
    if not isinstance(value, str):
        return None
    match = _AGGREGATOR_TIME.match(value.strip())
    if not match:
        return None
    try:
        moment = datetime.strptime(match.group(1), "%Y%m%d%H%M")
    except ValueError:
        return None
    return iso_utc(moment.replace(tzinfo=timezone.utc))


def split_description(description: Any) -> tuple[str, str]:
    """``'Roadwork~~~The New Mexico DOT will have...'`` -> ``(title, body)``.

    Records with no delimiter are title-only, so the body comes back empty rather
    than the title being lost.
    """
    text = description if isinstance(description, str) else ""
    if _TITLE_DELIMITER in text:
        title, _, body = text.partition(_TITLE_DELIMITER)
        return title.strip(), body.strip()
    return text.strip(), ""


def normalize_nm_direction(name: str) -> tuple[str, list[str]]:
    """Direction, extracted from the ``name`` prose.

    Direction has no field of its own; it appears - when it appears at all - inside
    the human-readable name: ``'I 40 northbound and eastbound from mile marker 44'``.
    Only 10 of 84 records carry any bound word, so UNKNOWN is the common case and
    must stay distinct from BOTH (treating it as BOTH would over-report impact in a
    direction nobody claimed).

    North/south are not I-40 directions. On a cross-state east-west corridor they
    indicate a cross-street or a mis-tagged record, and they are REPORTED rather
    than coerced - the observed I-40 lane closure says 'northbound and eastbound',
    which is a contradiction in the source worth surfacing.

    Returns ``(direction, noncorridor_words)``.
    """
    east = bool(_EASTBOUND.search(name))
    west = bool(_WESTBOUND.search(name))
    noncorridor = []
    if _NORTHBOUND.search(name):
        noncorridor.append("northbound")
    if _SOUTHBOUND.search(name):
        noncorridor.append("southbound")

    if east and west:
        return "BOTH", noncorridor
    if east:
        return "EB", noncorridor
    if west:
        return "WB", noncorridor
    return "UNKNOWN", noncorridor


def parse_mileposts(text: str) -> tuple[float, float] | None:
    """Extract a milepost range from prose. Returns ``(begin, end)`` or None.

    52 of 84 records phrase it as a range ('from mile marker 344 ... to mile marker
    350'), 15 as a point ('at mile marker 164'), and 17 not at all. A point returns
    equal begin and end, matching ``Extent``'s convention for point events.

    The range pattern is tried FIRST because the interstitial prose of a range
    frequently contains 'at mile marker' as well, so a point-first order would
    truncate ranges to their start.
    """
    range_match = _MILEPOST_RANGE.search(text)
    if range_match:
        begin = float(range_match.group(1))
        end = float(range_match.group(2))
        return (begin, end) if begin <= end else (end, begin)
    point_match = _MILEPOST_POINT.search(text)
    if point_match:
        value = float(point_match.group(1))
        return value, value
    return None


def parse_lane_impacts(title: str, body: str) -> tuple[list[LaneImpact], str | None]:
    """Lane detail from prose.

    There is no lane field. What exists is a phrase in the description body -
    ``'Eastbound driving lane closed on I-40'`` - and a title that says 'Lane
    Closure' without saying which lane.

    Every impact produced here is ``inferred=True`` with the source text retained
   . Crucially, when the side is not stated we DO NOT GUESS AN ORDINAL:
    ordinals count from the left edge and 'driving lane' names a function,
    not a position. So the lane count is known to be >= 1 and its ordinal is
    reported unresolved rather than invented.

    Returns ``(impacts, unresolved_text)``.
    """
    haystack = f"{title} {body}"
    match = _LANE_CLOSED.search(haystack)
    if not match:
        if "lane closure" in title.lower():
            return [], "title says 'Lane Closure' but no lane is identified in the prose"
        return [], None

    descriptor = match.group(1).lower()
    phrase = match.group(0)

    if descriptor == "left" or descriptor == "inside":
        # Leftmost is ordinal 1 and needs no total lane count to place.
        return [
            LaneImpact(
                ordinal=1,
                type="general",
                status="closed",
                inferred=True,
                inferred_from=phrase,
            )
        ], None

    # 'right' / 'outside' need a total to convert to a left-edge ordinal, and this
    # feed never states one. 'driving' names a function, not a position.
    return [], f"{phrase} (no total lane count, so no left-edge ordinal)"


def _synthesize_native_id(
    route_name: str, route_number: str, title: str, lon: float, lat: float
) -> str:
    """Build a stable-ish record id, because the feed provides NONE.

    Provenance wants the source's own record id and there is not one: no id, uid,
    log-id, or index on any of the 84 records. Something has to fill
    ``SourceRef.native_id``, and the choice has consequences.

    WHAT THIS HASHES, AND WHY THOSE FIELDS: route, title, and coordinates rounded to
    4 decimal places (~11 m). Deliberately EXCLUDED are the description body and
    ``updated`` - the body is edited as conditions change ('This event will be
    updated as conditions change' is in the observed text), and ``updated`` changes
    on every scrape, so including either would mint a new id for the same real
    event on almost every poll. Coordinates are rounded because the aggregator emits
    17 significant digits (-108.35854452838429) that are a projection artifact, not
    precision.

    THE RESIDUAL RISK IS REAL AND MUST NOT BE PAPERED OVER: NMDOT editing a title,
    or the event moving more than ~11 m, produces a new native_id for the same
    physical event. The matcher's spatial and temporal overlap logic is what
    has to absorb that; this id is not a durable key and downstream must not treat
    it as one. A source-provided id is the fix - ask NMDOT.

    NOT A SECURITY PRIMITIVE, hence ``usedforsecurity=False``: this digest names a
    record, it does not authenticate one, and nothing downstream trusts it. The flag
    is also what lets the adapter run on a FIPS-enabled interpreter, where an
    unqualified SHA-1 raises. Note that truncating to 16 hex characters caps
    collision resistance at 64 bits whatever algorithm sits underneath, so swapping
    in SHA-256 would change every id already in the store and buy nothing.
    """
    material = "|".join(
        [route_name, route_number, title, f"{lon:.4f}", f"{lat:.4f}"]
    )
    digest = hashlib.sha1(
        material.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]
    return f"nmws-{digest}"


def _is_number(value: Any) -> bool:
    """True for a real numeric coordinate. Excludes bools, which are ints in Python
    and would otherwise pass as a latitude of 1.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class NmDotWeathershareAdapter(Adapter):
    source_id = "nm-dot-weathershare"
    agency = "New Mexico DOT (via WeatherShare OSS)"
    expected_schema_version = "weathershare-oss-roadinfo-2026-08-11"

    def __init__(self, route: str = "I-40") -> None:
        self.route = route
        # 'I-40' -> ('I', '40'), because the feed splits the route across two
        # fields and never writes 'I-40'. Derived from configuration
        # rather than hardcoded.
        match = re.match(r"^([A-Za-z]+)-?(\d+)$", route)
        self._route_prefix = match.group(1).upper() if match else route.upper()
        self._route_number = match.group(2) if match else ""

    def _matches_route(self, route_name: Any, route_number: Any) -> bool:
        """Structural route matching, on the two route fields.

        NOT a text search over the prose, and that distinction is load-bearing.
        Of the 5 NMDOT records whose text mentions I-40, only 2 are ON I-40. The
        other 3 name it as a landmark or a detour:

          - 'Roadwork, NM 566 ... at mile marker 0, Church Rock (I-40).'
          - 'Roadwork, NM 566 ... from mile marker 6, 6 miles north of I-40 ...'
          - 'Low Clearance Structure, CMV's please use I-40 between exits 89 & 96.
             Height Restriction 13'6".'

        The third is the dangerous one: a genuine height restriction that
        RECOMMENDS I-40 as the truck alternative. A prose match would place a
        13'6" clearance limit ON the corridor it is telling trucks to use - the
        exact inversion of the fact, on the class where being wrong strands a
        truck under a bridge.
        """
        name = str(route_name or "").strip().upper()
        number = str(route_number or "").strip()
        return name == self._route_prefix and number == self._route_number

    def _resolve_extent(
        self,
        ctx: AdapterContext,
        mileposts: tuple[float, float] | None,
        coordinate_result: Any,
        native_id: str,
    ) -> tuple[Any, bool, float | None, list[MappingIssue]]:
        """Choose between the prose milepost and the record's coordinate.

        THE MILEPOST WINS WHENEVER IT RESOLVES, and that ordering was not the
        obvious one - it is the result of measuring both against this corridor:

        1. It is the AGENCY'S OWN linear reference. The corridor LRS is built on
           state mileposts, and NMDOT publishing 'mile marker 344' is NMDOT telling us a
           position in the reference system its maintenance records use.
        2. It gives a LINEAR extent. 52 of 84 records state a range; the coordinate
           is a single point, so preferring it collapses a 6-mile work zone to a
           dot and loses the length of the thing a truck has to drive through.
        3. OUR COORDINATE PATH IS DEMONSTRABLY THE WEAKER OF THE TWO RIGHT NOW.
           The placeholder centerline is 1163.2 mi against a 1241.0 mi milepost
           model - 6.3% short, accumulating eastward to a 22-mile discrepancy at
           the NM/TX line. Projecting a coordinate onto it inherits all of that.
        4. The confidence model already ranks them this way: ``milepost`` scores
           0.9 for spatial precision, ``coordinate`` 0.85.

        The coordinate is still the fallback, and the gap between the two is always
        recorded so the centerline error can be measured across the corpus rather
        than argued about.

        Returns ``(conflation, milepost_used, gap_miles, issues)``.
        """
        issues: list[MappingIssue] = []
        if mileposts is None:
            return coordinate_result, False, None, issues

        begin_mp, end_mp = mileposts
        milepost_result = ctx.conflator.conflate(
            MilepostInput(state=UPSTREAM_STATE, begin_mp=begin_mp, end_mp=end_mp)
        )

        if not milepost_result.on_corridor:
            # Outside the state's configured milepost range: either a bad prose
            # parse or a corridor.json range that is wrong. Either way the
            # coordinate is all we have left.
            issues.append(
                issue(
                    "name",
                    f"mile marker {begin_mp}-{end_mp}",
                    "out_of_corridor",
                    f"prose milepost falls outside {UPSTREAM_STATE}'s configured"
                    f" milepost range in corridor.json; falling back to the record"
                    f" coordinate; nativeId={native_id}",
                )
            )
            return coordinate_result, False, None, issues

        gap_miles: float | None = None
        if coordinate_result.on_corridor:
            gap_miles = abs(
                milepost_result.begin_measure - coordinate_result.begin_measure
            )
            if gap_miles > MILEPOST_COORDINATE_REPORT_THRESHOLD_MILES:
                # Reported, but it does NOT change the choice: at this
                # magnitude the likeliest cause is still our own geometry, and
                # switching to the coordinate would trade a documented bias for an
                # unknown one.
                issues.append(
                    issue(
                        "name",
                        f"mile marker {begin_mp}-{end_mp}",
                        "unmapped_vocabulary",
                        f"prose milepost and record coordinate disagree by"
                        f" {gap_miles:.1f} mi, beyond the"
                        f" {MILEPOST_COORDINATE_REPORT_THRESHOLD_MILES} mi review"
                        f" threshold; using the milepost (see _resolve_extent);"
                        f" nativeId={native_id}",
                    )
                )
        return milepost_result, True, gap_miles, issues

    def parse(self, raw_body: str, ctx: AdapterContext) -> AdapterResult:  # noqa: C901
        issues: list[MappingIssue] = []
        candidates: list[CandidateEvent] = []
        off_corridor = 0

        try:
            doc = json.loads(raw_body)
        except ValueError as exc:
            return AdapterResult(
                candidates=[],
                off_corridor=0,
                issues=[issue("$", raw_body[:200], "unparseable", str(exc))],
            )

        records, unwrap_error = unwrap_payload(doc)
        if records is None:
            # The wrapper shape changing is a real signal, not a parse hiccup.
            return AdapterResult(
                candidates=[],
                off_corridor=0,
                issues=[issue("$", type(doc).__name__, "unparseable", unwrap_error)],
            )

        for record in records:
            if not isinstance(record, dict):
                issues.append(
                    issue("$[]", type(record).__name__, "unparseable", "record is not an object")
                )
                continue

            # Filter to our single upstream FIRST. Every other upstream in this file
            # has a different schema, so applying NMDOT field logic to them would
            # produce garbage issues rather than useful ones.
            if record.get("source") != UPSTREAM_SOURCE:
                continue

            # Everything appended from here belongs to THIS record - see record_issues.
            # Marked AFTER the upstream filter, so a skipped record's issues (there are
            # none by design) could never be attributed to the next one.
            issue_mark = len(issues)

            route_name = unpair(record.get("routeName"), "routeName")
            route_number = unpair(record.get("routeNumber"), "routeNumber")
            if not self._matches_route(route_name, route_number):
                continue

            name = record.get("name") if isinstance(record.get("name"), str) else ""
            title, body = split_description(unpair(record.get("description"), "description"))
            native_id = "unknown"

            # --- class --------------------------------------------------------
            raw_event_type = unpair(record.get("eventType"), "eventType")
            event_type = raw_event_type if isinstance(raw_event_type, int) else None
            if event_type is None:
                issues.append(
                    issue(
                        "eventType",
                        raw_event_type,
                        "unparseable",
                        f"expected an int after unpairing; name={name[:80]!r}",
                    )
                )
                continue

            event_class = EVENT_TYPE_TO_CLASS.get(event_type)
            if not event_class:
                reason = UNMAPPED_EVENT_TYPES.get(
                    event_type, "eventType not present in the observed vocabulary"
                )
                issues.append(
                    issue(
                        "eventType",
                        event_type,
                        "unmapped_vocabulary",
                        f"{reason}; title={title[:60]!r}",
                    )
                )
                continue  # Never guess a class.

            # --- spatial ------------------------------------------------------
            longitude = record.get("longitude")
            latitude = record.get("latitude")
            if not (_is_number(longitude) and _is_number(latitude)):
                issues.append(
                    issue(
                        "longitude/latitude",
                        [longitude, latitude],
                        "missing_required",
                        f"title={title[:60]!r}",
                    )
                )
                continue

            native_id = _synthesize_native_id(
                str(route_name), str(route_number), title, longitude, latitude
            )

            coordinate_result = ctx.conflator.conflate(
                CoordinateInput(lon=longitude, lat=latitude)
            )

            # Mileposts live in the prose, and when present they WIN. See
            # ``_resolve_extent`` for why the agency's own linear reference beats a
            # coordinate projected onto our placeholder centerline.
            mileposts = parse_mileposts(name) or parse_mileposts(body)
            conflation, milepost_used, gap_miles, spatial_issues = self._resolve_extent(
                ctx, mileposts, coordinate_result, native_id
            )
            issues.extend(spatial_issues)

            if not conflation.on_corridor:
                off_corridor += 1
                continue

            # --- direction ----------------------------------------------------
            direction, noncorridor_words = normalize_nm_direction(name)
            if noncorridor_words:
                issues.append(
                    issue(
                        "name",
                        ", ".join(noncorridor_words),
                        "unmapped_vocabulary",
                        "north/south bound stated on an east-west corridor; not coerced"
                        f" into a corridor direction; nativeId={native_id}",
                    )
                )
            if direction == "UNKNOWN":
                issues.append(
                    issue(
                        "name",
                        name[:120],
                        "missing_required",
                        f"no direction stated in the prose; nativeId={native_id}",
                    )
                )

            # --- lanes --------------------------------------------------------
            impacts, unresolved = parse_lane_impacts(title, body)
            if unresolved:
                issues.append(
                    issue(
                        "description",
                        unresolved,
                        "missing_required",
                        "lane detail present in prose but not resolvable to a"
                        f" left-edge ordinal; nativeId={native_id}",
                    )
                )

            # --- time ---------------------------------------------------------
            # THE FEED CARRIES NO EVENT TIMES AT ALL. starttime and endtime are the
            # empty string on all 84 observed records, so start_time falls back to
            # our own retrieval time and end_time is open. This is a
            # genuine data gap, not a parse failure, and it is reported per record
            # because a start time that is really "when we looked" must not be
            # mistaken for an agency-stated one downstream.
            raw_start = record.get("starttime")
            raw_end = record.get("endtime")
            start_time = ctx.retrieved_at
            end_time = None
            if not raw_start:
                issues.append(
                    issue(
                        "starttime",
                        raw_start,
                        "missing_required",
                        "feed states no start time; substituted retrieved_at, so"
                        f" time_confidence is 'estimated'; nativeId={native_id}",
                    )
                )
            if not raw_end:
                issues.append(
                    issue(
                        "endtime",
                        raw_end,
                        "missing_required",
                        f"feed states no end time; treated as open-ended;"
                        f" nativeId={native_id}",
                    )
                )

            # ``updated`` is the AGGREGATOR'S SCRAPE TIME, not this record's update
            # time - it was byte-identical across all 84 NMDOT records. Carried as
            # source_updated_at because it is the only time signal available, and
            # labelled in extensions so nobody reads it as per-record freshness.
            aggregator_updated = parse_aggregator_time(record.get("updated"))
            if record.get("updated") and aggregator_updated is None:
                issues.append(
                    issue(
                        "updated",
                        record.get("updated"),
                        "unparseable",
                        "expected 'YYYYMMDDHHMM UTC'; nativeId=" + native_id,
                    )
                )

            contributed = ["extent", "event_subtype"]
            if impacts:
                contributed.append("lane_impacts")
            if milepost_used:
                contributed.append("milepost")

            candidates.append(
                CandidateEvent(
                    event_class=event_class,
                    event_subtype=title or event_class,
                    extent=Extent(
                        route=self.route,
                        begin_measure=conflation.begin_measure,
                        end_measure=conflation.end_measure,
                        direction=direction,
                        states=conflation.states,
                        # The source gives one point even when the prose describes a
                        # range, so the geometry stays a Point while the MEASURES
                        # carry the range. Synthesizing a LineString from a milepost
                        # range would be inventing geometry the source never stated.
                        geometry=GeoJsonGeometry(
                            type="Point", coordinates=[longitude, latitude]
                        ),
                        positional_accuracy_meters=conflation.positional_accuracy_meters,
                        conflation_method=conflation.method,
                    ),
                    lane_impacts=impacts,
                    start_time=start_time,
                    end_time=end_time,
                    # Not 'scheduled': there are no agency dates to schedule from.
                    # Not 'observed': nobody reported observing this at this time.
                    time_confidence="estimated",
                    # NMDOT asserts no severity anywhere in this feed.
                    agency_severity=None,
                    agency_duration_minutes=None,
                    source=SourceRef(
                        source_id=self.source_id,
                        agency=self.agency,
                        native_id=native_id,
                        retrieved_at=ctx.retrieved_at,
                        source_updated_at=aggregator_updated,
                        contributed_fields=contributed,
                        raw_ref=ctx.raw_ref,
                    ),
                    # Nothing dropped.
                    extensions={
                        "nmws_upstream_source": record.get("source"),
                        "nmws_event_type": event_type,
                        "nmws_type": record.get("type"),
                        "nmws_type_abbr": record.get("typeabbr"),
                        "nmws_icon": record.get("icon"),
                        "nmws_route_name": route_name,
                        "nmws_route_number": route_number,
                        "nmws_name": name,
                        "nmws_description_title": title,
                        "nmws_description_body": body,
                        "nmws_milepost_begin": mileposts[0] if mileposts else None,
                        "nmws_milepost_end": mileposts[1] if mileposts else None,
                        "nmws_milepost_used_for_extent": milepost_used,
                        # How far the agency's milepost and its own coordinate
                        # disagree, in corridor miles. Recorded on EVERY record so
                        # the placeholder centerline's error can be measured across
                        # the corpus instead of estimated - see _resolve_extent.
                        "nmws_milepost_coordinate_gap_miles": (
                            round(gap_miles, 2) if gap_miles is not None else None
                        ),
                        # Flagged as the aggregator's, not NMDOT's. Identical across
                        # every record in the batch.
                        "nmws_aggregator_updated_raw": record.get("updated"),
                        "nmws_aggregator_scrape_time": aggregator_updated,
                        "nmws_starttime_raw": record.get("starttime"),
                        "nmws_endtime_raw": record.get("endtime"),
                        # This id is ours, not the agency's. Recorded so a consumer
                        # can tell the difference.
                        "nmws_native_id_synthesized": True,
                    },
                    mapping_issues=record_issues(issues, issue_mark),
                )
            )

        return AdapterResult(candidates=candidates, off_corridor=off_corridor, issues=issues)

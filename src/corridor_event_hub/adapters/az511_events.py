"""AZ511 (Arizona DOT) events adapter.

Feed:    https://az511.gov/api/v2/get/event?key=...
Spec:    NONE - vendor 511 platform, proprietary JSON
License: unknown - CONFIRM redistribution terms with ADOT before publishing
Auth:    API key in Secrets Manager (corridor-event-hub/az511-key)
Limits:  10 requests / 60 seconds

===========================================================================
THIS IS THE FIRST NON-WZDx SOURCE, AND THE FIRST INCIDENT/CLOSURE FEED.
===========================================================================

It covers FOUR event classes from one endpoint, which no other source does:
incident, closure, work_zone, and dimensional_restriction. That makes it the feed
that proves the canonical model actually absorbs more than WZDx.

Verified live 2026-08-08 with a real key: 2,453 statewide events, 30 on I-40.

ENDPOINT DISCOVERY, now settled. Only ``/event`` exists. I probed roadwork, alert,
winterroad, camera, message, service, and restarea - all genuine 404s with a valid
key. So AZ511 does NOT give us winter road conditions, and class 6 stays unsourced
from Arizona.

WHAT MAKES THIS FEED BETTER THAN THE WZDx ONES:
  - ``LanesAffected`` carries real prose values ("1 Right lane closed"), and
    ``LaneCount`` gives a number. Oklahoma had lanes:[] and nothing else.
  - ``IsFullClosure`` is an explicit boolean.
  - ``Restrictions`` is a typed object with Width/Height/Length/Weight/Speed.
    137 of 2,453 events carry a real value - the first live source for class 8
    dimensional restrictions beyond static NBI data.
  - ``EncodedPolyline`` on 27 of 30 I-40 events, so extents are lines not points.

AND WHAT MAKES IT HARDER:
  - Timestamps are UNIX EPOCH SECONDS, not ISO 8601. A naive parse produces 1970.
  - ``DirectionOfTravel`` has TWELVE spellings across the feed: East, west,
    eastbound, westbound, Both, All, Unknown, None, plus north/south variants.
  - ``Severity`` is blank on 1,933 of 2,453 records, and the string 'None' on 32
    more. Blank and 'None' are different from Minor, and neither means "no impact".
  - ``RoadwayName`` is mostly 'I-40' but sometimes 'I-40 Westbound', so direction
    leaks into the route name (as in Oklahoma, differently).
  - One record had a police dispatch blob pasted into RoadwayName. Route matching
    has to be resilient to that rather than trusting the field.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.lrs import CoordinateInput, LineStringInput
from ..core.timeutil import duration_minutes, epoch_to_iso
from ..core.types import (
    CandidateEvent,
    Extent,
    GeoJsonGeometry,
    LaneImpact,
    MappingIssue,
    SourceRef,
)
from .adapter import Adapter, AdapterContext, AdapterResult, issue, record_issues

# AZ511 EventType -> canonical class (deterministic, versioned as data).
#
# NOTE ``closures`` maps to ``closure``, NOT to ``work_zone``, even though its
# EventSubType is frequently ``constructionWork``. The authority, duration,
# and routing consequence of a closure differ from a work zone, and a closure
# caused by construction is still a closure. The work-zone relationship is
# expressed by linking, not by collapsing the classes.
EVENT_TYPE_TO_CLASS: dict[str, str] = {
    "roadwork": "work_zone",
    "closures": "closure",
    "accidentsAndIncidents": "incident",
    "specialEvents": "incident",
    "restrictionClass": "dimensional_restriction",
}

_WESTBOUND = re.compile(r"\bwestbound\b", re.IGNORECASE)
_EASTBOUND = re.compile(r"\beastbound\b", re.IGNORECASE)
_LEADING_COUNT = re.compile(r"^(\d+)")
_RIGHT = re.compile(r"\bright\b")
_LEFT = re.compile(r"\bleft\b")
_SHOULDER = re.compile(r"\bshoulder\b")
_CLOSED = re.compile(r"\bclosed\b")


def normalize_az_direction(raw: str | None) -> str:
    """Direction normalization. TWELVE spellings observed live, including
    blank, 'None', 'Unknown', and 'All'. Each maps deliberately:

      - north/south are NOT I-40 directions; on a cross-state east-west corridor
        they indicate a cross-street event and resolve to UNKNOWN rather than
        being coerced.
      - 'All' and 'Both' mean both directions.
      - blank / 'None' / 'Unknown' all mean we do not know. That is not the same
        as BOTH, and treating it as BOTH would over-report impact.
    """
    value = (raw or "").lower().strip()
    if value in ("east", "eastbound", "eb"):
        return "EB"
    if value in ("west", "westbound", "wb"):
        return "WB"
    if value in ("both", "all"):
        return "BOTH"
    # Includes '', 'none', 'unknown', 'north', 'south', 'northbound',
    # 'southbound'. All genuinely unknown for an east-west corridor.
    return "UNKNOWN"


def parse_lanes_affected(
    lanes_affected: str | None,
    lane_count: int | None,
    is_full_closure: bool | None,
) -> tuple[list[LaneImpact], str | None]:
    """``LanesAffected`` -> structured lane impacts.

    Observed vocabulary on I-40: 'No Data', '1 Right lane closed', '1 Left lane
    closed', 'All lanes closed', 'Lane Rolling', 'Lanes Alternating'.

    This is prose, so every result is tagged ``inferred`` with the source text
    retained. Where the count and side are both stated we can produce real
    ordinals; where only a count appears we deliberately do NOT guess which lanes.

    Returns ``(impacts, unresolved)``.
    """
    raw = (lanes_affected or "").strip()
    lower = raw.lower()

    if is_full_closure is True or lower == "all lanes closed":
        return [
            LaneImpact(
                ordinal=1,
                type="general",
                status="closed",
                inferred=True,
                inferred_from=raw or "IsFullClosure",
            )
        ], None

    if not raw or lower == "no data":
        return [], raw or "absent"

    if "alternating" in lower:
        return [
            LaneImpact(
                ordinal=1,
                type="general",
                status="alternating",
                inferred=True,
                inferred_from=raw,
            )
        ], None
    if "rolling" in lower:
        # A rolling closure moves; it is intermittent at any fixed point.
        return [
            LaneImpact(
                ordinal=1,
                type="general",
                status="intermittent",
                inferred=True,
                inferred_from=raw,
            )
        ], None

    # "<n> Right lane closed" / "<n> Left lane closed" / "shoulder closed"
    count_match = _LEADING_COUNT.match(raw)
    count = int(count_match.group(1)) if count_match else None
    side = "right" if _RIGHT.search(lower) else ("left" if _LEFT.search(lower) else None)
    is_shoulder = bool(_SHOULDER.search(lower))
    closed = bool(_CLOSED.search(lower))

    if is_shoulder:
        return [
            LaneImpact(
                ordinal=1,
                type="shoulder",
                status="closed" if closed else "open",
                inferred=True,
                inferred_from=raw,
            )
        ], None

    if count is not None and side and closed:
        # Ordinals count from the LEFT edge. With a known total we can
        # place right-side closures; without one we can only place left-side ones.
        total = lane_count if isinstance(lane_count, int) and lane_count > 0 else None
        impacts: list[LaneImpact] = []
        for i in range(count):
            if side == "left":
                ordinal = i + 1
            elif total is not None:
                ordinal = total - i
            else:
                # Right-lane closure with no total: side is known, position is not.
                return [], f"{raw} (no LaneCount for right-side ordinal)"
            impacts.append(
                LaneImpact(
                    ordinal=ordinal,
                    type="general",
                    status="closed",
                    inferred=True,
                    inferred_from=raw,
                )
            )
        return impacts, None

    return [], raw


def _mentions_route(event: dict[str, Any], route: str) -> bool:
    """Route matching. Deliberately anchored rather than a substring test: one live
    record had a police dispatch blob pasted into RoadwayName - agency, a residential
    street address with an apartment number, patrol beat, and a live on-scene status -
    and '40TH ST' appears 8 times. A naive ``'40' in name`` would match both.

    The address is not quoted here. It was real personal information, and an example in
    a comment is as committed as one in a fixture.
    """
    name = (event.get("RoadwayName") or "").strip()
    # Route is config; build the pattern from it.
    number = re.sub(r"^I-?", "", route, flags=re.IGNORECASE)
    return bool(re.match(rf"^I-?{number}\b", name, flags=re.IGNORECASE))


def _normalize_severity(raw: str | None) -> str | None:
    """Keep the agency's own severity verbatim; do not reconcile it."""
    value = (raw or "").strip()
    # Blank and the literal string 'None' both mean "not stated" - which is NOT
    # the same as "no impact". Preserve the distinction as None.
    if not value or value.lower() == "none":
        return None
    return value


class Az511EventsAdapter(Adapter):
    source_id = "az511-events"
    agency = "Arizona DOT (AZ511)"
    expected_schema_version = "az511-v2"

    def __init__(self, route: str = "I-40") -> None:
        self.route = route

    def parse(self, raw_body: str, ctx: AdapterContext) -> AdapterResult:
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

        if not isinstance(doc, list):
            # The feed is a bare JSON array. A shape change is a real signal.
            return AdapterResult(
                candidates=[],
                off_corridor=0,
                issues=[
                    issue(
                        "$",
                        type(doc).__name__,
                        "unparseable",
                        "expected a JSON array of events",
                    )
                ],
            )

        for event in doc:
            if not _mentions_route(event, self.route):
                continue

            # Everything appended from here belongs to THIS event - see record_issues.
            issue_mark = len(issues)

            native_id = str(event.get("ID", "unknown"))

            # --- class --------------------------------------------------------
            event_class = EVENT_TYPE_TO_CLASS.get((event.get("EventType") or "").strip())
            if not event_class:
                issues.append(
                    issue(
                        "EventType",
                        event.get("EventType"),
                        "unmapped_vocabulary",
                        f"nativeId={native_id}",
                    )
                )
                continue  # Never guess a class.

            # --- spatial ------------------------------------------------------
            # Prefer the primary/secondary coordinate pair as a two-point line;
            # fall back to the single point. EncodedPolyline is richer but needs a
            # polyline decoder - a deliberate follow-up rather than a silent
            # approximation.
            latitude = event.get("Latitude")
            longitude = event.get("Longitude")
            latitude_secondary = event.get("LatitudeSecondary")
            longitude_secondary = event.get("LongitudeSecondary")

            has_primary = _is_number(latitude) and _is_number(longitude)
            has_secondary = _is_number(latitude_secondary) and _is_number(longitude_secondary)

            if not has_primary:
                issues.append(
                    issue("Latitude", latitude, "missing_required", f"nativeId={native_id}")
                )
                continue

            if has_secondary:
                conflation = ctx.conflator.conflate(
                    LineStringInput(
                        coordinates=[
                            (longitude, latitude),
                            (longitude_secondary, latitude_secondary),
                        ]
                    )
                )
            else:
                conflation = ctx.conflator.conflate(
                    CoordinateInput(lon=longitude, lat=latitude)
                )

            if not conflation.on_corridor:
                off_corridor += 1
                continue

            encoded_polyline = event.get("EncodedPolyline")
            if encoded_polyline:
                issues.append(
                    issue(
                        "EncodedPolyline",
                        f"{str(encoded_polyline)[:24]}...",
                        "unmapped_vocabulary",
                        "encoded polyline present but not decoded; extent derived from"
                        " endpoint coordinates only, so it is coarser than the source"
                        " allows",
                    )
                )

            # --- direction ----------------------------------------------------
            # Direction also leaks into RoadwayName ('I-40 Westbound'), so check
            # both.
            direction = normalize_az_direction(event.get("DirectionOfTravel"))
            if direction == "UNKNOWN":
                roadway_name = event.get("RoadwayName") or ""
                if _WESTBOUND.search(roadway_name):
                    direction = "WB"
                elif _EASTBOUND.search(roadway_name):
                    direction = "EB"
            if direction == "UNKNOWN":
                issues.append(
                    issue(
                        "DirectionOfTravel",
                        event.get("DirectionOfTravel"),
                        "missing_required",
                        f"nativeId={native_id}",
                    )
                )

            # --- lanes --------------------------------------------------------
            impacts, unresolved = parse_lanes_affected(
                event.get("LanesAffected"),
                event.get("LaneCount"),
                event.get("IsFullClosure"),
            )
            if unresolved:
                issues.append(
                    issue(
                        "LanesAffected",
                        unresolved,
                        "missing_required",
                        "lane detail present but not resolvable to ordinals;"
                        f" nativeId={native_id}",
                    )
                )

            # --- time ---------------------------------------------------------
            # EPOCH SECONDS. epoch_to_iso returns None rather than 1970 for junk.
            start_time = epoch_to_iso(event.get("StartDate")) or ctx.retrieved_at
            end_time = epoch_to_iso(event.get("PlannedEndDate"))
            last_updated = epoch_to_iso(event.get("LastUpdated"))

            if event.get("StartDate") is not None and epoch_to_iso(event.get("StartDate")) is None:
                issues.append(
                    issue(
                        "StartDate",
                        event.get("StartDate"),
                        "unparseable",
                        "not a plausible epoch timestamp",
                    )
                )
            if event.get("PlannedEndDate") is not None and end_time is None:
                issues.append(
                    issue(
                        "PlannedEndDate",
                        event.get("PlannedEndDate"),
                        "unparseable",
                        "not a plausible epoch timestamp",
                    )
                )

            # --- restrictions (class 8 signal) --------------------------------
            restrictions = event.get("Restrictions") or {}
            has_restriction = any(v is not None for v in restrictions.values())

            geometry = (
                GeoJsonGeometry(
                    type="LineString",
                    coordinates=[
                        [longitude, latitude],
                        [longitude_secondary, latitude_secondary],
                    ],
                )
                if has_secondary
                else GeoJsonGeometry(type="Point", coordinates=[longitude, latitude])
            )

            contributed = ["extent", "start_time", "end_time", "event_subtype"]
            if impacts:
                contributed.append("lane_impacts")
            if has_restriction:
                contributed.append("restrictions")

            # A restriction rides along with a work zone or closure rather
            # than replacing it. A LINKED dimensional_restriction candidate would
            # give the routing constraint its own lifecycle - but not until the
            # units are confirmed.
            #
            # Raised BEFORE the candidate is built, because record_issues snapshots the
            # list at construction time. Below the append it would land in the feed's
            # review queue and be missing from the event that caused it - the exact
            # split this change exists to close.
            if has_restriction and event_class != "dimensional_restriction":
                issues.append(
                    issue(
                        "Restrictions",
                        json.dumps(restrictions),
                        "unmapped_vocabulary",
                        "event carries dimensional restrictions; UNITS ARE UNDOCUMENTED"
                        " so a separate class-8 candidate is NOT emitted until confirmed"
                        " with ADOT",
                    )
                )

            candidates.append(
                CandidateEvent(
                    event_class=event_class,
                    event_subtype=(event.get("EventSubType") or "").strip() or event_class,
                    extent=Extent(
                        route=self.route,
                        begin_measure=conflation.begin_measure,
                        end_measure=conflation.end_measure,
                        direction=direction,
                        states=conflation.states,
                        geometry=geometry,
                        positional_accuracy_meters=conflation.positional_accuracy_meters,
                        conflation_method=conflation.method,
                    ),
                    lane_impacts=impacts,
                    start_time=start_time,
                    end_time=end_time,
                    # Agency-planned dates, not observed: the basis is
                    # agency_stated.
                    time_confidence="scheduled",
                    agency_severity=_normalize_severity(event.get("Severity")),
                    agency_duration_minutes=duration_minutes(start_time, end_time),
                    source=SourceRef(
                        source_id=self.source_id,
                        agency=self.agency,
                        native_id=native_id,
                        retrieved_at=ctx.retrieved_at,
                        source_updated_at=last_updated,
                        contributed_fields=contributed,
                        raw_ref=ctx.raw_ref,
                    ),
                    # Nothing dropped.
                    extensions={
                        "az511_event_type": event.get("EventType"),
                        "az511_event_subtype": event.get("EventSubType"),
                        "az511_severity_raw": event.get("Severity"),
                        "az511_is_full_closure": event.get("IsFullClosure"),
                        "az511_lane_count": event.get("LaneCount"),
                        "az511_lanes_affected_raw": event.get("LanesAffected"),
                        "az511_roadway_name": event.get("RoadwayName"),
                        "az511_direction_raw": event.get("DirectionOfTravel"),
                        "az511_organization": event.get("Organization"),
                        "az511_description": event.get("Description"),
                        "az511_details": event.get("Details"),
                        "az511_recurrence": event.get("Recurrence"),
                        "az511_recurrence_schedules": event.get("RecurrenceSchedules"),
                        "az511_encoded_polyline": event.get("EncodedPolyline"),
                        "az511_detour_instructions": event.get("DetourInstructions"),
                        "az511_detour_polyline": event.get("DetourPolyline"),
                        "az511_source_id": event.get("SourceId"),
                        # Dimensional restrictions: the first LIVE source for these
                        # beyond static NBI. Units are not documented by the feed -
                        # confirm with ADOT before treating Width/Height as feet vs
                        # metres.
                        "az511_restriction_width": restrictions.get("Width"),
                        "az511_restriction_height": restrictions.get("Height"),
                        "az511_restriction_length": restrictions.get("Length"),
                        "az511_restriction_weight": restrictions.get("Weight"),
                        "az511_restriction_speed": restrictions.get("Speed"),
                        "az511_reported": epoch_to_iso(event.get("Reported")),
                    },
                    mapping_issues=record_issues(issues, issue_mark),
                )
            )

        return AdapterResult(candidates=candidates, off_corridor=off_corridor, issues=issues)


def _is_number(value: Any) -> bool:
    """True for a real numeric coordinate. Excludes bools, which are ints in
    Python and would otherwise pass as a latitude of 1.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)

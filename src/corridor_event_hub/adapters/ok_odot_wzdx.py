"""Oklahoma ODOT WZDx adapter - work zones (event class 1).

Feed:    https://oktraffic.org/api/Geojsons/workzones?access_token=...
Spec:    WZDx v4.0 (GeoJSON)
License: CC0 1.0 public domain - no redistribution restriction
Cadence: 60s per feed metadata (``update_frequency``)
Contact: published in the feed's ``road_event_feed_info.data_sources``

WHY THIS FEED FIRST: no signup - the token is published in the federal ITS
registry - and CC0 licensed, so anyone can reproduce this adapter's results.

WHAT MAKES IT A USEFUL REFERENCE is that the data is imperfect in every way a
real agency feed is. Observed in live payloads on 2026-08-07:

  - ``vehicle_impact: "unknown"`` with ``lanes: []`` on I-40 records. No lane
    detail at all, so prose inference is load-bearing from day one.
    Descriptions are terse engineering shorthand ("GRADE, DRAIN, BRIDGE AND
    SURFACE"), which is exactly the case where deterministic rules fail and a
    model MIGHT help - tagged ``inferred``, never trusted silently.
  - ``end_date: "2029-08-05T22:16:48.351Z"`` - a 3.5-year work zone with
    millisecond precision. Obviously synthetic. This is why every time carries a
    ``basis`` tag: `agency_stated` does not mean `agency_knows`.
  - THE SAME PAYLOAD carries ``2026-10-19T22:16:48.354Z`` and
    ``2026-08-17T22:16:48.354Z`` - years apart, same minute-of-day, same
    millisecond. Measured against 23.5 hours of live payloads, every one of these
    advanced by exactly the elapsed time: ODOT computes end dates as
    ``request time + fixed offset``, so a zone that ends "in 10 days" ends in 10
    days forever. The near-term ones are the dangerous ones - a consumer acts on a
    plausible October date. ``regenerated_end_date_minutes`` finds them from the
    payload alone, so replay stays deterministic.
  - ``road_names: ["I-40 W"]`` AND ``direction: "westbound"`` - direction encoded
    twice, redundantly. Direction normalization has to reconcile them.
  - No mileposts, only LineString geometry, so conflation takes the coordinate
    path and inherits centerline accuracy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ..core.lrs import CoordinateInput, LineStringInput, UnresolvedInput
from ..core.timeutil import parse_iso
from ..core.types import CandidateEvent, Extent, GeoJsonGeometry, LaneImpact, MappingIssue
from .adapter import Adapter, AdapterContext, AdapterResult, issue, record_issues

# Vocabularies are versioned DATA with crosswalks, not code.
WZDX_LANE_TYPE_MAP: dict[str, str] = {
    "general": "general",
    "general-regulated-access": "general",
    "shoulder": "shoulder",
    "shoulder-left": "shoulder",
    "shoulder-right": "shoulder",
    "hov-lane": "HOV",
    "exit-lane": "exit",
    "entrance-lane": "entrance",
    "median": "median",
    "center-left-turn-lane": "median",
}

WZDX_LANE_STATUS_MAP: dict[str, str] = {
    "open": "open",
    "closed": "closed",
    "shift-left": "shifted",
    "shift-right": "shifted",
    "merge-left": "shifted",
    "merge-right": "shifted",
    "alternating-flow": "alternating",
    "alternating-one-way": "alternating",
}

_ROAD_NAME_SUFFIX = re.compile(r"\b([EW])B?$", re.IGNORECASE)
_MILLIS = re.compile(r"\.\d{3}")
_SECONDS_PER_YEAR = 3.156e7


def normalize_direction(
    direction: str | None, road_names: Sequence[str] | None
) -> tuple[str, bool]:
    """Direction normalization. Handles ODOT encoding it in BOTH
    ``direction`` and a road-name suffix.

    Returns ``(value, conflict)``.
    """
    from_field = _map_direction_token(direction)
    from_name = _map_road_name_suffix(road_names[0] if road_names else None)

    if from_field != "UNKNOWN" and from_name != "UNKNOWN" and from_field != from_name:
        # Prefer the explicit field, but record the disagreement - precedence keeps
        # losing values rather than discarding them.
        return from_field, True
    return (from_field if from_field != "UNKNOWN" else from_name), False


def _map_direction_token(value: str | None) -> str:
    token = (value or "").lower().strip()
    if token in ("eastbound", "east", "eb"):
        return "EB"
    if token in ("westbound", "west", "wb"):
        return "WB"
    if token in ("both", "bidirectional"):
        return "BOTH"
    return "UNKNOWN"


def _map_road_name_suffix(name: str | None) -> str:
    if not name:
        return "UNKNOWN"
    match = _ROAD_NAME_SUFFIX.search(name.strip())
    if not match:
        return "UNKNOWN"
    return "EB" if match.group(1).upper() == "E" else "WB"


def regenerated_end_date_minutes(end_isos: Sequence[str | None]) -> frozenset[str]:
    """Find the ``HH:MM`` markers of end dates ODOT recomputes on every request.

    THE DEFECT: this feed does not store end dates, it GENERATES them as
    ``request time + fixed offset``. Measured against the live raw zone over a
    84,598-second gap, ``end_date`` advanced 84,598 seconds on 54 of 57 records -
    a ratio of 1.0000. A work zone whose end is always "in 10 days" never ends.

    The ``> 2 years`` rule below catches only the most absurd of these. The ones
    that slip through are worse, because a plausible-looking October date is one
    a consumer will act on.

    DETECTION IS FROM THE PAYLOAD ALONE, deliberately. The obvious test - compare
    the end date's time-of-day to ``ctx.retrieved_at`` - would make output depend
    on WHEN we parsed, and replaying old bytes later would then produce different
    events. Replay requires the opposite. So this reads the signature the generator
    leaves inside a single payload: many records sharing one minute-of-day, to the
    millisecond, while their calendar dates are months apart. Real agency dates
    are round numbers and do not cluster this way.
    """
    by_minute: dict[str, list[str]] = {}
    for end_iso in end_isos:
        # Millisecond precision is the machine-generated tell. An agency typing a
        # date into a form does not produce '.351'.
        if not end_iso or not _MILLIS.search(end_iso):
            continue
        end = parse_iso(end_iso)
        if end is None:
            continue
        by_minute.setdefault(end.strftime("%H:%M"), []).append(end.date().isoformat())
    # Three records at one minute-of-day spread over two or more calendar dates is
    # a generator; one record on one date could be a genuine scheduled end.
    return frozenset(
        minute
        for minute, dates in by_minute.items()
        if len(dates) >= 3 and len(set(dates)) >= 2
    )


def end_date_is_plausible(
    start_iso: str | None,
    end_iso: str | None,
    *,
    regenerated_minutes: frozenset[str] = frozenset(),
) -> tuple[bool, str | None]:
    """Flag agency end dates that cannot be taken at face value.

    A multi-year duration stated to the millisecond is a generated placeholder,
    not an estimate - the ``basis`` tag exists for exactly this. Returns
    ``(plausible, reason)``.

    ``regenerated_minutes`` comes from :func:`regenerated_end_date_minutes` over
    the whole payload; it catches the sliding dates that are too near-term for the
    duration rule to reject. Defaulted so existing callers keep working, but the
    adapter always passes it.
    """
    if not end_iso:
        return True, None
    end = parse_iso(end_iso)
    if end is None:
        return False, "unparseable end_date"
    start = parse_iso(start_iso)
    if start is not None and end < start:
        return False, "end_date precedes start_date"
    years = (end - start).total_seconds() / _SECONDS_PER_YEAR if start is not None else 0.0
    if years > 2 and _MILLIS.search(end_iso):
        return False, f"duration {years:.1f}y stated to the millisecond - likely synthetic"
    if regenerated_minutes and end.strftime("%H:%M") in regenerated_minutes:
        return False, (
            f"end_date regenerated per request - {end.strftime('%H:%M')} is shared to the"
            " millisecond by records months apart, so this date advances with the clock"
            " and the zone never ends"
        )
    return True, None


def _mentions_route(feature: dict[str, Any], route: str) -> bool:
    """Is this record on the corridor we care about? Route name is config."""
    core = (feature.get("properties") or {}).get("core_details") or {}
    haystack = " ".join(
        [
            *(core.get("road_names") or []),
            core.get("name") or "",
            core.get("description") or "",
        ]
    ).upper()
    needle = re.sub(r"[-\s]", "", route.upper())
    return needle in re.sub(r"[-\s]", "", haystack)


class OkOdotWzdxAdapter(Adapter):
    source_id = "ok-odot-wzdx"
    agency = "Oklahoma DOT"
    expected_schema_version = "wzdx-4.0"

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

        # Schema drift detection. Do not silently map a changed spec.
        version = (doc.get("road_event_feed_info") or {}).get("version")
        if version and f"wzdx-{version}" != self.expected_schema_version:
            issues.append(
                issue(
                    "road_event_feed_info.version",
                    version,
                    "unmapped_vocabulary",
                    f"feed is WZDx {version}, adapter expects {self.expected_schema_version}"
                    " - verify field semantics before trusting output",
                )
            )

        # Computed over EVERY feature, not just the corridor ones: the generator's
        # signature is a property of the payload, and the off-corridor records are
        # extra evidence for free.
        regenerated_minutes = regenerated_end_date_minutes(
            [
                (feature.get("properties") or {}).get("end_date")
                for feature in doc.get("features") or []
            ]
        )

        for feature in doc.get("features") or []:
            if not _mentions_route(feature, self.route):
                continue

            # Everything appended from here belongs to THIS feature - see record_issues.
            issue_mark = len(issues)

            props = feature.get("properties") or {}
            core = props.get("core_details") or {}
            native_id = feature.get("id") or core.get("name") or "unknown"

            # --- spatial conflate ---------------------------------------------
            geometry = feature.get("geometry") or {}
            geom_type = geometry.get("type")
            coordinates = geometry.get("coordinates")

            if geom_type == "LineString" and isinstance(coordinates, list):
                conflation = ctx.conflator.conflate(LineStringInput(coordinates=coordinates))
            elif geom_type == "Point" and isinstance(coordinates, list):
                conflation = ctx.conflator.conflate(
                    CoordinateInput(lon=coordinates[0], lat=coordinates[1])
                )
            else:
                conflation = ctx.conflator.conflate(
                    UnresolvedInput(text=f"unsupported geometry: {geom_type}")
                )

            if not conflation.on_corridor:
                off_corridor += 1
                issues.append(
                    issue("geometry", geom_type, "out_of_corridor", f"nativeId={native_id}")
                )
                continue

            # --- direction ----------------------------------------------------
            direction, conflict = normalize_direction(
                core.get("direction"), core.get("road_names")
            )
            if conflict:
                issues.append(
                    issue(
                        "direction",
                        {
                            "direction": core.get("direction"),
                            "road_names": core.get("road_names"),
                        },
                        "unmapped_vocabulary",
                        "direction field and road-name suffix disagree; used the explicit field",
                    )
                )
            if direction == "UNKNOWN":
                issues.append(issue("direction", core.get("direction"), "missing_required"))

            # --- lanes --------------------------------------------------------
            lane_impacts: list[LaneImpact] = []
            for index, lane in enumerate(props.get("lanes") or []):
                lane_type = WZDX_LANE_TYPE_MAP.get((lane.get("type") or "").lower())
                lane_status = WZDX_LANE_STATUS_MAP.get((lane.get("status") or "").lower())
                if not lane_type:
                    issues.append(
                        issue("lanes[].type", lane.get("type"), "unmapped_vocabulary")
                    )
                if not lane_status:
                    issues.append(
                        issue("lanes[].status", lane.get("status"), "unmapped_vocabulary")
                    )
                lane_impacts.append(
                    LaneImpact(
                        ordinal=lane.get("order") or index + 1,
                        type=lane_type or "general",
                        status=lane_status or "open",
                        inferred=False,
                    )
                )

            # The live-data reality: ODOT I-40 records arrive with `lanes: []` and
            # `vehicle_impact: "unknown"`. We record the gap rather than inventing
            # lane detail. Inference from prose is permitted, but the
            # description here is engineering shorthand, so inference belongs
            # downstream where it can be tagged and confidence-penalized - NOT here.
            if not lane_impacts:
                description = (core.get("description") or "")[:60]
                issues.append(
                    issue(
                        "lanes",
                        props.get("vehicle_impact"),
                        "missing_required",
                        f'no lane detail; description="{description}"'
                        " - candidate for tagged inference downstream",
                    )
                )

            # --- duration -----------------------------------------------------
            start_date = props.get("start_date")
            end_date = props.get("end_date")
            plausible, reason = end_date_is_plausible(
                start_date, end_date, regenerated_minutes=regenerated_minutes
            )
            if not plausible:
                issues.append(issue("end_date", end_date, "synthetic_placeholder", reason))

            duration_minutes: int | None = None
            if start_date and end_date and plausible:
                start = parse_iso(start_date)
                end = parse_iso(end_date)
                if start is not None and end is not None:
                    duration_minutes = round((end - start).total_seconds() / 60)

            candidates.append(
                CandidateEvent(
                    event_class="work_zone",
                    event_subtype=core.get("event_type") or "work-zone",
                    extent=Extent(
                        route=self.route,
                        begin_measure=conflation.begin_measure,
                        end_measure=conflation.end_measure,
                        direction=direction,
                        states=conflation.states,
                        geometry=(
                            GeoJsonGeometry(type=geom_type, coordinates=coordinates)
                            if geom_type
                            else None
                        ),
                        positional_accuracy_meters=conflation.positional_accuracy_meters,
                        conflation_method=conflation.method,
                    ),
                    lane_impacts=lane_impacts,
                    start_time=start_date or ctx.retrieved_at,
                    # Only publish an end time we believe. A per-class prior
                    # supplies the duration instead.
                    end_time=(end_date if plausible else None),
                    time_confidence=(
                        "observed"
                        if props.get("start_date_accuracy") == "verified"
                        else "estimated"
                    ),
                    agency_severity=props.get("vehicle_impact"),
                    agency_duration_minutes=duration_minutes,
                    source=self._source_ref(native_id, core, lane_impacts, ctx),
                    # Nothing is dropped, even where the canonical model
                    # has no home.
                    extensions={
                        "wzdx_event_status": props.get("event_status"),
                        "wzdx_vehicle_impact": props.get("vehicle_impact"),
                        "wzdx_types_of_work": props.get("types_of_work") or [],
                        "wzdx_restrictions": props.get("restrictions") or [],
                        "wzdx_beginning_accuracy": props.get("beginning_accuracy"),
                        "wzdx_ending_accuracy": props.get("ending_accuracy"),
                        "wzdx_description": core.get("description"),
                        "wzdx_road_names": core.get("road_names") or [],
                        "odot_end_date_raw": end_date,
                    },
                    mapping_issues=record_issues(issues, issue_mark),
                )
            )

        return AdapterResult(candidates=candidates, off_corridor=off_corridor, issues=issues)

    def _source_ref(
        self,
        native_id: Any,
        core: dict[str, Any],
        lane_impacts: Sequence[LaneImpact],
        ctx: AdapterContext,
    ):
        from ..core.types import SourceRef

        contributed = ["extent", "start_time", "end_time", "event_subtype"]
        if lane_impacts:
            contributed.append("lane_impacts")
        return SourceRef(
            source_id=self.source_id,
            agency=self.agency,
            native_id=str(native_id),
            retrieved_at=ctx.retrieved_at,
            source_updated_at=core.get("update_date"),
            contributed_fields=contributed,
            raw_ref=ctx.raw_ref,
        )

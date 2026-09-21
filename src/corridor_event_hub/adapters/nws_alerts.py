"""National Weather Service alerts adapter - weather (class 5) and road surface
(class 6).

Feed:    https://api.weather.gov/alerts/active?area=AZ,NM,TX,OK
Spec:    NWS API, CAP-derived (the NWS API spec)
License: US federal government work, public domain
Auth:    none, but a User-Agent with contact info is REQUIRED by NWS policy

WHY THIS FEED SECOND: it exercises a completely different spatial path from WZDx.
Alerts are POLYGONS covering whole counties or forecast zones, so this is the
adapter that needs real 2D intersection against the corridor - the operation that
most justifies PostGIS (ADR 0002 § The split, and why). Two feeds, two spatial paths, zero paperwork.

Observed in live payloads on 2026-08-07 (31 active alerts across AZ/NM/TX/OK):

  - ``ends: null`` on a Special Weather Statement. The open-ended duration case
    in the very first record.
  - ``areaDesc: "Marfa Plateau; Presidio Valley"`` - named forecast zones, not
    roads. Nothing ties an alert to I-40 except geometry, which is why the polygon
    intersection is load-bearing rather than a nicety.
  - 32 properties per alert, most with no canonical home. The extension bag earns
    its place immediately.
  - Some alerts have ``geometry: null`` and reference UGC/SAME zone codes instead.
    Those cannot be conflated without a zone shapefile lookup, so they are reported
    as issues rather than guessed at.

ONE ALERT CAN PRODUCE TWO EVENTS. A winter storm warning implies both a
weather event and a road-surface event, with distinct lifecycles. They are emitted
as linked candidates, never blended into one record.
"""

from __future__ import annotations

import json
import re

from ..core.lrs import PolygonInput
from ..core.timeutil import duration_minutes
from ..core.types import (
    CandidateEvent,
    Extent,
    GeoJsonGeometry,
    MappingIssue,
    SourceRef,
)
from .adapter import Adapter, AdapterContext, AdapterResult, issue, record_issues

# Which NWS event names imply a road-surface condition in addition to weather.
# This is a versioned crosswalk artifact - a mapping correction is a data
# change, backfillable by replay, not a code deploy.
SURFACE_IMPLYING: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"winter storm|snow|blizzard", re.IGNORECASE), "snow"),
    (re.compile(r"ice storm|freezing rain|frost|freeze", re.IGNORECASE), "ice"),
    (re.compile(r"flood", re.IGNORECASE), "standing_water"),
)

# Alerts with no bearing on a roadway. Filtered, but the count is reported.
NOT_ROADWAY_RELEVANT = re.compile(
    r"^(air quality|beach hazards|rip current|small craft|special marine"
    r"|marine weather|hurricane local statement)",
    re.IGNORECASE,
)

# NWS severity vocabulary -> our normalized enum input.
NWS_SEVERITY_ORDER = ("Minor", "Moderate", "Severe", "Extreme")


def classify_weather(event_name: str) -> str:
    """Map an NWS event name to a class-5 subtype. Data, not code."""
    name = event_name.lower()
    if "dust" in name:
        return "dust_storm"
    if "blowing snow" in name:
        return "blowing_snow"
    if "wind" in name:
        return "high_wind"
    if re.search(r"winter storm|snow|blizzard", name):
        return "winter_storm"
    if re.search(r"ice|freezing", name):
        return "ice"
    if re.search(r"fog|visibility", name):
        return "low_visibility"
    if "flood" in name:
        return "flooding"
    if re.search(r"thunderstorm|tornado", name):
        return "severe_thunderstorm"
    if "heat" in name:
        return "extreme_heat"
    return "advisory"


class NwsAlertsAdapter(Adapter):
    source_id = "nws-alerts"
    agency = "National Weather Service"
    expected_schema_version = "nws-api-v1"

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

        for feature in doc.get("features") or []:
            # Everything appended from here belongs to THIS alert - see record_issues.
            issue_mark = len(issues)

            props = feature.get("properties") or {}
            native_id = props.get("id") or feature.get("id") or "unknown"
            event_name = props.get("event") or ""

            if NOT_ROADWAY_RELEVANT.match(event_name):
                continue

            # --- spatial: polygon x corridor ----------------------------------
            geometry = feature.get("geometry")
            if not geometry:
                # Zone-coded alerts need a UGC shapefile join we do not
                # have yet. Report it; never guess a location.
                area_desc = (props.get("areaDesc") or "")[:40]
                issues.append(
                    issue(
                        "geometry",
                        (props.get("geocode") or {}).get("UGC"),
                        "missing_required",
                        f'alert "{event_name}" is zone-coded only'
                        f' (areaDesc="{area_desc}") - needs a UGC zone shapefile join',
                    )
                )
                continue

            conflation = ctx.conflator.conflate(PolygonInput(geometry=geometry))

            if not conflation.on_corridor:
                off_corridor += 1
                continue  # Most alerts legitimately do not touch I-40. Not an issue.

            # --- time ---------------------------------------------------------
            start = (
                props.get("onset")
                or props.get("effective")
                or props.get("sent")
                or ctx.retrieved_at
            )
            # `ends` is frequently null; `expires` is when the ALERT lapses, which
            # is not the same as when the WEATHER ends. Prefer ends, fall back, and
            # say which we used.
            end = props.get("ends") or props.get("expires")
            if not props.get("ends") and props.get("expires"):
                issues.append(
                    issue(
                        "ends",
                        None,
                        "missing_required",
                        "used `expires` (alert lapse) as a proxy for `ends` (weather"
                        " end) - different semantics, flagged for confidence penalty",
                    )
                )

            severity = props.get("severity")
            if severity and severity not in NWS_SEVERITY_ORDER and severity != "Unknown":
                issues.append(issue("severity", severity, "unmapped_vocabulary"))

            geocode = props.get("geocode") or {}
            shared_extensions = {
                "nws_event": event_name,
                "nws_severity": severity,
                "nws_certainty": props.get("certainty"),
                "nws_urgency": props.get("urgency"),
                "nws_headline": props.get("headline"),
                "nws_areaDesc": props.get("areaDesc"),
                "nws_messageType": props.get("messageType"),
                "nws_senderName": props.get("senderName"),
                "nws_ugc": geocode.get("UGC") or [],
                "nws_expires": props.get("expires"),
                "nws_ends_raw": props.get("ends"),
                "nws_instruction": props.get("instruction"),
            }

            classes = [("weather", classify_weather(event_name))]
            # A SEPARATE road-surface event where implied. Linked, not
            # blended - they clear at different times, which is the whole point of
            # splitting classes 5 and 6.
            surface_subtype = _surface_subtype(event_name)
            if surface_subtype:
                classes.append(("road_surface", surface_subtype))

            for event_class, event_subtype in classes:
                candidates.append(
                    self._make_candidate(
                        event_class=event_class,
                        event_subtype=event_subtype,
                        conflation=conflation,
                        geometry=geometry,
                        props=props,
                        start=start,
                        end=end,
                        severity=severity,
                        native_id=native_id,
                        extensions=shared_extensions,
                        ctx=ctx,
                        mapping_issues=record_issues(issues, issue_mark),
                    )
                )

        return AdapterResult(candidates=candidates, off_corridor=off_corridor, issues=issues)

    def _make_candidate(
        self,
        *,
        event_class: str,
        event_subtype: str,
        conflation,
        geometry: dict,
        props: dict,
        start: str,
        end: str | None,
        severity: str | None,
        native_id,
        extensions: dict,
        ctx: AdapterContext,
        mapping_issues: list[MappingIssue],
    ) -> CandidateEvent:
        """Build one candidate.

        Each gets its OWN ``Extent`` and extensions dict rather than a shared
        reference: two candidates aliasing one mutable extent is the kind of bug
        that only shows up once something downstream starts editing an event in
        place, by which point it looks like a data problem rather than a code one.

        ``mapping_issues`` is copied for the same reason. One alert can yield both a
        `weather` and a `road_surface` candidate, and they must not share a list.
        """
        return CandidateEvent(
            event_class=event_class,
            event_subtype=event_subtype,
            extent=Extent(
                route=self.route,
                begin_measure=conflation.begin_measure,
                end_measure=conflation.end_measure,
                direction="BOTH",  # weather affects both directions
                states=list(conflation.states),
                geometry=GeoJsonGeometry(
                    type=geometry.get("type"),
                    coordinates=geometry.get("coordinates"),
                ),
                positional_accuracy_meters=conflation.positional_accuracy_meters,
                conflation_method=conflation.method,
            ),
            lane_impacts=[],  # weather does not close specific lanes
            start_time=start,
            end_time=end,
            time_confidence="observed" if props.get("onset") else "estimated",
            agency_severity=severity,
            agency_duration_minutes=duration_minutes(start, end),
            source=SourceRef(
                source_id=self.source_id,
                agency=self.agency,
                native_id=str(native_id),
                retrieved_at=ctx.retrieved_at,
                source_updated_at=props.get("sent"),
                contributed_fields=["extent", "start_time", "end_time", "event_subtype"],
                raw_ref=ctx.raw_ref,
            ),
            extensions=dict(extensions),
            mapping_issues=list(mapping_issues),
        )


def _surface_subtype(event_name: str) -> str | None:
    for pattern, subtype in SURFACE_IMPLYING:
        if pattern.search(event_name):
            return subtype
    return None

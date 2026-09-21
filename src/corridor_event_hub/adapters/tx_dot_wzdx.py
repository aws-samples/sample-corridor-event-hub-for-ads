"""TxDOT DriveTexas WZDx adapter - work zones (event class 1).

Feed:    https://api.drivetexas.org/api/conditions.wzdx.geojson?key=...
Spec:    WZDx v4.2 (GeoJSON)
License: CC0 1.0 public domain
Auth:    API key, held in Secrets Manager (corridor-event-hub/tx-dot-wzdx-key)
Contact: trv-hcr@txdot.gov (TxDOT Travel Division)

===========================================================================
THIS ADAPTER IS THE ARGUMENT FOR THE ADAPTER PATTERN.
===========================================================================

Oklahoma and Texas both publish "WZDx work zones". They are not the same thing.
Differences observed in live payloads on 2026-08-07:

1. ROUTE NAMING. TxDOT writes ``IH0040``, not ``I-40``. A naive route match on
   "I-40" finds ZERO Texas records out of 2,059. This is "inconsistent geographic
   references" as a concrete, silent data loss - the pipeline would
   have looked healthy while dropping an entire state.

2. SPEC VERSION DRIFT. 4.2 replaced 4.0's ``start_date_accuracy: "verified"``
   strings with ``is_start_date_verified: boolean``. Same concept, incompatible
   encoding. All 2,059 records carry the boolean form; none carry the string form.
   Serving multiple spec versions concurrently is a hard requirement, and this is
   why - two states, two versions, today.

3. LANE DETAIL IS IN PROSE, NOT STRUCTURE. ``lanes: []`` on all 2,059 records, but
   ``vehicle_impact`` is populated with a real vocabulary AND the description
   contains HTML like "- Left lane closed.<br/><br/>". So Texas has MORE lane
   information than Oklahoma and LESS structure to hold it. Lane counts have to be
   derivable even when the source says only "two right lanes closed" - this
   adapter derives what it safely can from ``vehicle_impact`` and flags the rest.

4. NO MILEPOSTS, but ``beginning_cross_street`` on every record. Cross-street text
   is a lower-precision locator than a milepost and would need geocoding
   (``text_geocode``). Not attempted here; geometry is present and better.

Only 4 of 2,059 Texas work zones are on I-40 - Texas has just 177 corridor miles
and most of its work is elsewhere. Small numbers, but they exercise the
cross-state dedup case at the NM and OK borders.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ..core.lrs import CoordinateInput, LineStringInput, UnresolvedInput
from ..core.timeutil import duration_minutes
from ..core.types import (
    CandidateEvent,
    Extent,
    GeoJsonGeometry,
    LaneImpact,
    MappingIssue,
    SourceRef,
)
from .adapter import Adapter, AdapterContext, AdapterResult, issue, record_issues

# TxDOT route naming. ``IH0040`` = Interstate Highway 40, zero-padded to 4 digits.
# The same scheme covers ``US0084``, ``SH0155``, ``RM0584``, ``SL1604``, ``FM...``.
#
# This crosswalk is DATA. When a fifth state arrives with a sixth naming
# convention, this is the shape of the fix - a table entry, not a code change.
TXDOT_ROUTE_PATTERNS: dict[str, tuple[re.Pattern, ...]] = {
    "I-40": (
        re.compile(r"^IH0*40$", re.IGNORECASE),
        re.compile(r"^I-?40$", re.IGNORECASE),
        re.compile(r"^IH40$", re.IGNORECASE),
    ),
}

_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_LEADING_PUNCT = re.compile(r"^[\s-]+")


def _mentions_route(feature: dict[str, Any], route: str) -> bool:
    patterns = TXDOT_ROUTE_PATTERNS.get(route)
    if not patterns:
        return False
    core = (feature.get("properties") or {}).get("core_details") or {}
    names = [*(core.get("road_names") or []), core.get("name") or ""]
    return any(
        pattern.match((name or "").strip()) for name in names for pattern in patterns
    )


def _map_direction(value: str | None) -> str:
    token = (value or "").lower().strip()
    if token in ("eastbound", "east", "eb"):
        return "EB"
    if token in ("westbound", "west", "wb"):
        return "WB"
    if token in ("both", "bidirectional", "undefined"):
        return "BOTH"
    return "UNKNOWN"


def strip_html(text: str) -> str:
    """TxDOT descriptions carry HTML. Keep the text, drop the markup."""
    cleaned = _BR.sub(" ", text)
    cleaned = _TAG.sub("", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned)
    return _LEADING_PUNCT.sub("", cleaned).strip()


def lane_impacts_from_vehicle_impact(
    vehicle_impact: str | None, description: str | None
) -> tuple[list[LaneImpact], str | None]:
    """Derive what lane impact we can from the WZDx ``vehicle_impact`` vocabulary.

    Inference is permitted but must be TAGGED and confidence-penalized, with the
    source text retained. So this returns a coarse, honest answer rather
    than a precise, invented one:

      ``all-lanes-closed``  -> we know the road is closed, which is real
                               information even without knowing the lane count.
      ``some-lanes-closed`` -> we know SOMETHING is closed but not what or how
                               many. Emitting a fabricated "lane 1 closed" would
                               be worse than emitting nothing, so we emit nothing
                               and record the gap.

    The ``-merge-left``/``-merge-right`` suffixes tell us which side, which narrows
    it but still does not give an ordinal. Deliberately not guessed.

    Returns ``(impacts, unresolved)``.
    """
    impact = (vehicle_impact or "").lower().strip()
    source_text = strip_html(description or "") or impact

    if impact == "all-lanes-open":
        return [], None  # nothing closed; nothing to say
    if impact == "all-lanes-closed":
        # A full closure is expressible without knowing the lane count.
        return [
            LaneImpact(
                ordinal=1,
                type="general",
                status="closed",
                inferred=True,
                inferred_from=source_text,
            )
        ], None
    if impact in ("alternating-one-way", "flagging"):
        return [
            LaneImpact(
                ordinal=1,
                type="general",
                status="alternating",
                inferred=True,
                inferred_from=source_text,
            )
        ], None
    if impact in (
        "some-lanes-closed",
        "some-lanes-closed-merge-left",
        "some-lanes-closed-merge-right",
    ):
        # We know a closure exists but not which lanes. Do NOT invent ordinals.
        return [], impact
    if impact in ("unknown", ""):
        return [], impact or "absent"
    return [], impact


def _time_confidence(props: dict[str, Any]) -> str:
    """WZDx 4.2 uses ``is_*_verified`` booleans where 4.0 used ``*_accuracy``
    strings. Normalizing here rather than in core is the point of the adapter
    boundary: downstream code never learns that two spec versions exist.
    """
    if props.get("is_start_date_verified") is True:
        return "observed"
    if props.get("start_date"):
        return "scheduled"  # TxDOT work zones are planned, not observed
    return "estimated"


class TxDotWzdxAdapter(Adapter):
    source_id = "tx-dot-wzdx"
    agency = "Texas DOT"
    expected_schema_version = "wzdx-4.2"

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

        # Schema drift detection. NOTE 4.2 nests feed metadata under
        # `feed_info`, where 4.0 used `road_event_feed_info` - another silent
        # difference between two feeds nominally following the same spec.
        version = (doc.get("feed_info") or {}).get("version")
        if version and f"wzdx-{version}" != self.expected_schema_version:
            issues.append(
                issue(
                    "feed_info.version",
                    version,
                    "unmapped_vocabulary",
                    f"feed is WZDx {version}, adapter expects {self.expected_schema_version}"
                    " - verify field semantics before trusting output",
                )
            )

        for feature in doc.get("features") or []:
            if not _mentions_route(feature, self.route):
                continue

            # Everything appended from here belongs to THIS feature - see record_issues.
            issue_mark = len(issues)

            props = feature.get("properties") or {}
            core = props.get("core_details") or {}
            native_id = feature.get("id") or core.get("name") or "unknown"

            # --- spatial ------------------------------------------------------
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
                continue

            # --- direction ----------------------------------------------------
            direction = _map_direction(core.get("direction"))
            if direction == "UNKNOWN":
                issues.append(
                    issue("core_details.direction", core.get("direction"), "missing_required")
                )

            # --- lanes --------------------------------------------------------
            impacts, unresolved = lane_impacts_from_vehicle_impact(
                props.get("vehicle_impact"), core.get("description")
            )
            if props.get("lanes"):
                # Structured lanes would be better than inference. None observed
                # live, but handle it if TxDOT starts publishing them.
                issues.append(
                    issue(
                        "lanes",
                        props.get("lanes"),
                        "unmapped_vocabulary",
                        "structured lanes[] appeared - prefer these over vehicle_impact"
                        " inference and extend this adapter",
                    )
                )
            if unresolved:
                description = strip_html(core.get("description") or "")[:70]
                issues.append(
                    issue(
                        "vehicle_impact",
                        unresolved,
                        "missing_required",
                        "closure exists but lane ordinals unknown;"
                        f' description="{description}"',
                    )
                )

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
                    lane_impacts=impacts,
                    start_time=props.get("start_date") or ctx.retrieved_at,
                    end_time=props.get("end_date"),
                    time_confidence=_time_confidence(props),
                    agency_severity=props.get("vehicle_impact"),
                    agency_duration_minutes=duration_minutes(
                        props.get("start_date"), props.get("end_date")
                    ),
                    source=self._source_ref(native_id, core, impacts, ctx),
                    # Nothing dropped, including the 4.2-only fields.
                    extensions={
                        "wzdx_vehicle_impact": props.get("vehicle_impact"),
                        "wzdx_location_method": props.get("location_method"),
                        "wzdx_is_start_date_verified": props.get("is_start_date_verified"),
                        "wzdx_is_end_date_verified": props.get("is_end_date_verified"),
                        "wzdx_is_start_position_verified": props.get(
                            "is_start_position_verified"
                        ),
                        "wzdx_is_end_position_verified": props.get(
                            "is_end_position_verified"
                        ),
                        "wzdx_description": strip_html(core.get("description") or "") or None,
                        "wzdx_description_raw": core.get("description"),
                        "wzdx_road_names": core.get("road_names") or [],
                        "txdot_beginning_cross_street": props.get("beginning_cross_street"),
                        "txdot_ending_cross_street": props.get("ending_cross_street"),
                        "txdot_name": core.get("name"),
                    },
                    mapping_issues=record_issues(issues, issue_mark),
                )
            )

        return AdapterResult(candidates=candidates, off_corridor=off_corridor, issues=issues)

    def _source_ref(
        self,
        native_id: Any,
        core: dict[str, Any],
        impacts: Sequence[LaneImpact],
        ctx: AdapterContext,
    ) -> SourceRef:
        contributed = ["extent", "start_time", "end_time", "event_subtype"]
        if impacts:
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

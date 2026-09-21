"""Amazon Location Service traffic tiles - congestion (class 4), plus incidents,
closures and work zones from the same payload.

Feed:    geo-maps GetTile, Tileset=vector.traffic, z/x/y
Spec:    NONE - Mapbox Vector Tile carrying an undocumented property schema
License: HERE content via AWS. Attribution is MANDATORY and arrives per feature
         in the `source` property ("(c) 2026 HERE"). NOT redistributable.
Auth:    AWS SigV4 (IAM). No API key, no Secrets Manager entry - see ADR 0004.

===========================================================================
THIS IS THE ONLY SOURCE FOR CONGESTION, AND THE ONLY TILED SOURCE.
IT IS ALSO INTERIM - PLANNED FOR REPLACEMENT. SEE BELOW.
===========================================================================

INTERIM STATUS. This source unblocked class 4; it is not the long-term answer.
Three of the limitations listed under "WHAT MAKES THIS HARDER" - no confidence or
provenance band, no direction, not redistributable - are properties of the tile
SCHEMA, so no adapter work closes them. They close only by changing source.

  Planned replacement: NPMRDS via RITIS (provenance, TMC referencing, historical
  baselines, a redistribution story) and/or a commercial probe feed - INRIX, HERE
  direct, TomTom - for real-time WITH redistribution rights. NPMRDS alone is
  batch/lagged, so it supplements real-time rather than replacing it.

  When a replacement lands: this module stays as a corroborating source, and
  `time_confidence="estimated"` plus `direction=UNKNOWN` below MUST be revisited -
  both are here only because this feed cannot state them, not because class 4 is
  inherently unknowable. HERE-direct would remain in `independenceGroup: here`,
  so it buys fields, not independence.

  See DATA-SOURCES.md, "The replacement path for class 4", and the
  `$interimComment` on this source's catalog entry.

Congestion was the one class with both an access problem and a latency problem:
NPMRDS needs a RITIS account AND is batch/lagged, so it cannot answer "is there a
queue right now". This source answers that, with no signup, because the pipeline
already deploys into AWS.

Verified live 2026-08-11 across the corridor at z8 (17 tiles, ~121KB): 1,586 flow
segments and 59 incidents, including 6 `queuing`, 5 `slow` and 3 `stationary`
segments - real congestion, not a synthetic curve.

WHAT MAKES THIS FEED STRUCTURALLY DIFFERENT FROM EVERY OTHER ONE:

  - It is TILED. Every other source answers "give me your records"; this answers
    "what is inside this square". The adapter is handed the already-fetched tile
    bodies and their addresses, because a tile's coordinates are local to itself -
    the bytes carry no georeference at all. See core/tiles.py.
  - It is BINARY protobuf, not JSON. core/mvt.py decodes it with no dependency.
  - It carries TWO layers with different meanings: `traffic_flow` (continuous
    conditions) and `traffic_incidents` (discrete events). One payload, two very
    different lifecycles - which is why they produce different event classes.

WHAT IT GIVES US THAT NOTHING ELSE DOES:
  - Numeric `speed` (km/h) and `congestion` (0-1 ratio) per segment.
  - A STABLE segment/incident `id`, so the matcher gets a real correlation key
    across polling cycles instead of inferring identity from geometry overlap.
  - Incidents with epoch `start_time`/`stop_time`, so class 1-3 events arrive with
    genuine temporal bounds.

AND WHAT MAKES IT HARDER - the honest list:

  - NO CONFIDENCE OR PROVENANCE FIELD. This is the important one. HERE's own API
    publishes confidence bands that say whether a reading is observed, historical,
    or speed-limit-derived; Amazon's tile schema drops them. So a `speed` here
    cannot be distinguished from a historical average, and this adapter must not
    let a speed reading corroborate an agency-reported closure. Enforced by
    `independenceGroup: here` in the catalog plus `time_confidence="estimated"`
    on every candidate.
  - The property schema is UNDOCUMENTED. Every field below was learned by
    decoding live tiles, so schema drift is a live risk with no spec to diff
    against. A new `kind` value is reported as a mapping issue, never
    guessed.
  - `congestion` was 0.0 on nearly every free-flowing segment, so the mapping
    from `kind` to a subtype leans on `kind` rather than the ratio.
  - Tiles have no direction field. A carriageway is a separate feature, but which
    one is eastbound is not stated - so direction is UNKNOWN, never guessed. The
    style descriptor's `drives_on_left` is a rendering hint, not a heading.
  - Segments are SUB-TILE FRAGMENTS. One physical stretch of road appears as many
    short features, and the same `id` can recur in adjacent tiles. Deduplication
    is the matcher's job, so this adapter emits one candidate per feature
    and lets `id` travel in `extensions` for the matcher to key on.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..core.lrs import LineStringInput
from ..core.mvt import decode_tile
from ..core.timeutil import duration_minutes, epoch_to_iso
from ..core.types import (
    CandidateEvent,
    Extent,
    GeoJsonGeometry,
    MappingIssue,
    SourceRef,
)
from .adapter import Adapter, AdapterContext, AdapterResult, issue

# `kind` on traffic_flow -> class-4 subtype. Learned from live tiles and from the
# style descriptor's own colour ramp, which switches on exactly these five values.
# A versioned crosswalk, so a correction is a data change not a deploy.
FLOW_KIND_TO_SUBTYPE: dict[str, str] = {
    "minor": "light_congestion",
    "slow": "moderate_congestion",
    "queuing": "queue",
    "stationary": "stopped_traffic",
}

# `kind` values that are NOT congestion. `free` means the road is clear, which is
# an observation worth having but not an event - emitting a "no congestion here"
# event every cycle for every segment would flood the store with non-events.
# `none` appears on segments with no flow data at all.
FLOW_KIND_NOT_AN_EVENT = frozenset({"free", "none"})

# `kind` on traffic_incidents -> canonical class + subtype.
#
# NOTE `construction` maps to work_zone and `closure` to closure, matching the
# reasoning in the AZ511 adapter: a closure caused by construction is still a
# closure, and the relationship is expressed by linking rather than by collapsing
# the two classes.
INCIDENT_KIND_TO_CLASS: dict[str, tuple[str, str]] = {
    "accident": ("incident", "crash"),
    "construction": ("work_zone", "construction"),
    "closure": ("closure", "full_closure"),
    "road_closure": ("closure", "full_closure"),
    "congestion": ("congestion", "queue"),
    "disabled_vehicle": ("incident", "disabled_vehicle"),
    "lane_restriction": ("incident", "lane_restriction"),
    "mass_transit": ("incident", "mass_transit"),
    "planned_event": ("incident", "planned_event"),
    "road_hazard": ("incident", "hazard"),
    "weather": ("weather", "advisory"),
    "other": ("incident", "unknown"),
}

# Only mainline motorway classes are relevant to a limited-access corridor. Local
# streets inside the buffer would otherwise flood the corridor with city traffic
# that no truck on the interstate cares about.
MAINLINE_ROAD_KINDS = frozenset({"motorway", "trunk"})

# What limits this source's precision is how coarsely the tile quantizes geometry,
# and that is set by the layer's declared `extent`, not by the raster pixel size.
# These tiles declare extent 4096, so one coordinate unit is 156,543/4096 = ~38 m
# at the equator and ~31 m at this latitude. An earlier value of 600 m used the
# 256-pixel raster size instead (156,543/256 = 611 m), which is a rendering
# concept and 16x too pessimistic for vector geometry; its second justification
# was the placeholder centerline, which real ARNOLD geometry has since replaced.
#
# Measured against the ARNOLD centerline over 7,768 vertices of corridor-tangent
# mainline in the captured tiles: median 14 m, p75 30 m, p90 46 m. The two road
# networks register tightly on I-40 mainline, so the residual is quantization plus
# carriageway offset, not network disagreement. 50 m is therefore the honest floor
# - it matches the LRS's own coordinate floor and is above the measured p90.
# Positional accuracy cuts both ways: overstating the error is also a false claim,
# and it silently penalizes this source in every confidence breakdown.
TILE_POSITIONAL_ACCURACY_FLOOR_METERS = 50.0


@dataclass(frozen=True)
class TilePayload:
    """One fetched tile plus the address it came from.

    The address is REQUIRED alongside the bytes: MVT coordinates are tile-local,
    so decoding with the wrong address produces plausible coordinates in the wrong
    place. Pairing them in one object makes that mistake hard to make.
    """

    z: int
    x: int
    y: int
    body: bytes


def payloads_from_json(raw_body: str) -> tuple[list[TilePayload], list[MappingIssue]]:
    """Read the collector's envelope: base64 tiles plus their addresses.

    Tiles are binary, and every other stage of this pipeline - the raw S3 object,
    the fixture, the replay path - is text. So the collector wraps the
    tile set in one JSON envelope with the addresses alongside, and this is the
    inverse. A per-tile decode failure is an issue, not an exception: one corrupt
    tile must not cost the other sixteen.
    """
    issues: list[MappingIssue] = []
    try:
        doc = json.loads(raw_body)
    except ValueError as exc:
        return [], [issue("$", raw_body[:200], "unparseable", str(exc))]

    if not isinstance(doc, dict) or not isinstance(doc.get("tiles"), list):
        return [], [
            issue(
                "$",
                type(doc).__name__,
                "unparseable",
                "expected an object with a `tiles` array of {z,x,y,mvtBase64}",
            )
        ]

    payloads: list[TilePayload] = []
    for index, entry in enumerate(doc["tiles"]):
        try:
            payloads.append(
                TilePayload(
                    z=int(entry["z"]),
                    x=int(entry["x"]),
                    y=int(entry["y"]),
                    body=base64.b64decode(entry["mvtBase64"]),
                )
            )
        except Exception as exc:  # noqa: BLE001 - malformed entry, keep the rest
            issues.append(
                issue(
                    f"tiles[{index}]",
                    str(entry)[:80],
                    "unparseable",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return payloads, issues


class AwsLocationTrafficAdapter(Adapter):
    source_id = "aws-location-traffic"
    agency = "Amazon Location Service (HERE data)"
    # There is no published schema version. This string is OUR label for the
    # property set observed on 2026-08-11, so a drift alert has something
    # to compare against even though the vendor publishes nothing.
    expected_schema_version = "alsv2-vector-traffic-observed-2026-08-11"

    def __init__(self, route: str = "I-40") -> None:
        self.route = route

    def parse(self, raw_body: str, ctx: AdapterContext) -> AdapterResult:
        payloads, issues = payloads_from_json(raw_body)
        if not payloads:
            return AdapterResult(candidates=[], off_corridor=0, issues=issues)
        return self.parse_tiles(payloads, ctx, prior_issues=issues)

    def parse_tiles(
        self,
        payloads: Sequence[TilePayload],
        ctx: AdapterContext,
        *,
        prior_issues: Sequence[MappingIssue] = (),
    ) -> AdapterResult:
        """Decode and map an already-fetched tile set.

        Split from ``parse`` so a caller holding raw tile bytes - the probe, a
        test, a future collector that skips the JSON envelope - does not have to
        base64 them first only to have this undo it.
        """
        issues: list[MappingIssue] = list(prior_issues)
        candidates: list[CandidateEvent] = []
        off_corridor = 0
        # The same segment id legitimately recurs across adjacent tiles. Emitting
        # both would hand the matcher two candidates that are one segment, so they
        # are collapsed here - this is tile bookkeeping, not deduplication of
        # independent agency reports (which stays the matcher's job).
        seen_ids: set[tuple[str, Any]] = set()

        for payload in payloads:
            try:
                layers = decode_tile(payload.body, payload.x, payload.y, payload.z)
            except Exception as exc:  # noqa: BLE001 - a corrupt tile is expected data
                issues.append(
                    issue(
                        f"tile[{payload.z}/{payload.x}/{payload.y}]",
                        f"{len(payload.body)}B",
                        "unparseable",
                        f"MVT decode failed: {type(exc).__name__}: {exc}",
                    )
                )
                continue

            for layer_name, layer in layers.items():
                # `incident_icons` is the same incidents repeated as points for
                # label placement. Skipping it is not data loss.
                if layer_name == "incident_icons":
                    continue

                for feature in layer.features:
                    result = self._map_feature(
                        layer_name=layer_name,
                        feature_properties=feature.properties,
                        coordinates=feature.coordinates,
                        geometry_type=feature.geometry_type,
                        tile=payload,
                        ctx=ctx,
                        seen_ids=seen_ids,
                    )
                    candidates.extend(result.candidates)
                    issues.extend(result.issues)
                    off_corridor += result.off_corridor

        return AdapterResult(
            candidates=candidates, off_corridor=off_corridor, issues=issues
        )

    def _map_feature(
        self,
        *,
        layer_name: str,
        feature_properties: dict[str, Any],
        coordinates: list[tuple[float, float]],
        geometry_type: str,
        tile: TilePayload,
        ctx: AdapterContext,
        seen_ids: set[tuple[str, Any]],
    ) -> AdapterResult:
        """Map one tile feature. Returns a single-feature AdapterResult."""
        issues: list[MappingIssue] = []
        native_id = feature_properties.get("id")
        tile_label = f"{tile.z}/{tile.x}/{tile.y}"

        if layer_name not in ("traffic_flow", "traffic_incidents"):
            # An unrecognized layer is schema drift on an undocumented
            # feed. Report it once per feature rather than assuming it is noise.
            return AdapterResult(
                issues=[
                    issue(
                        "layer",
                        layer_name,
                        "unmapped_vocabulary",
                        f"unknown tile layer in {tile_label}; not mapped",
                    )
                ]
            )

        # --- mainline filter -------------------------------------------------
        road_kind_detail = (feature_properties.get("road_kind_detail") or "").strip()
        if road_kind_detail not in MAINLINE_ROAD_KINDS:
            # Off-mainline by road class, not by geography. Not an issue, and not
            # off_corridor either - it is a deliberate filter.
            return AdapterResult()

        # --- class + subtype -------------------------------------------------
        kind = (feature_properties.get("kind") or "").strip()
        if layer_name == "traffic_flow":
            if kind in FLOW_KIND_NOT_AN_EVENT:
                return AdapterResult()
            subtype = FLOW_KIND_TO_SUBTYPE.get(kind)
            if subtype is None:
                return AdapterResult(
                    issues=[
                        issue(
                            "kind",
                            kind,
                            "unmapped_vocabulary",
                            f"unknown traffic_flow kind in {tile_label};"
                            " never guessed - see FLOW_KIND_TO_SUBTYPE",
                        )
                    ]
                )
            event_class = "congestion"
        else:
            mapped = INCIDENT_KIND_TO_CLASS.get(kind)
            if mapped is None:
                return AdapterResult(
                    issues=[
                        issue(
                            "kind",
                            kind,
                            "unmapped_vocabulary",
                            f"unknown traffic_incidents kind in {tile_label};"
                            " never guessed - see INCIDENT_KIND_TO_CLASS",
                        )
                    ]
                )
            event_class, subtype = mapped

        # --- tile-boundary bookkeeping ---------------------------------------
        if native_id is not None:
            key = (layer_name, native_id)
            if key in seen_ids:
                return AdapterResult()
            seen_ids.add(key)

        # --- spatial ---------------------------------------------------------
        if not coordinates:
            return AdapterResult(
                issues=[
                    issue(
                        "geometry",
                        geometry_type,
                        "unparseable",
                        f"feature in {tile_label} decoded with no coordinates;"
                        f" nativeId={native_id}",
                    )
                ]
            )

        conflation = ctx.conflator.conflate(LineStringInput(coordinates=coordinates))
        if not conflation.on_corridor:
            # Expected in bulk: a z8 tile spans far more than the corridor.
            return AdapterResult(off_corridor=1)

        # --- time ------------------------------------------------------------
        # traffic_flow carries NO timestamp: a flow reading is "now", and the only
        # honest start time is when we fetched it. traffic_incidents carries real
        # epoch bounds.
        if layer_name == "traffic_incidents":
            start_time = epoch_to_iso(feature_properties.get("start_time")) or ctx.retrieved_at
            end_time = epoch_to_iso(feature_properties.get("stop_time"))
            for field_name in ("start_time", "stop_time"):
                raw_value = feature_properties.get(field_name)
                if raw_value is not None and epoch_to_iso(raw_value) is None:
                    issues.append(
                        issue(
                            field_name,
                            raw_value,
                            "unparseable",
                            f"not a plausible epoch timestamp; nativeId={native_id}",
                        )
                    )
        else:
            start_time = ctx.retrieved_at
            end_time = None

        # --- direction -------------------------------------------------------
        # Tiles model each carriageway as its own feature but never say which
        # heading it is. Report it, never guess. This is the single biggest
        # gap in this source, and it matters - "queue eastbound" and "queue
        # westbound" are different events to a truck.
        issues.append(
            issue(
                "direction",
                None,
                "missing_required",
                "tile features carry no direction/heading field; extent direction is"
                f" UNKNOWN. nativeId={native_id} in {tile_label}",
            )
        )

        speed = feature_properties.get("speed")
        congestion_ratio = feature_properties.get("congestion")

        # Attribution is a licence obligation, so it travels with the
        # record rather than living only in the catalog.
        attribution = feature_properties.get("source")

        contributed = ["extent", "start_time", "event_subtype"]
        if layer_name == "traffic_flow":
            contributed.append("speed")
        if end_time:
            contributed.append("end_time")

        return AdapterResult(
            candidates=[
                CandidateEvent(
                    event_class=event_class,
                    event_subtype=subtype,
                    extent=Extent(
                        route=self.route,
                        begin_measure=conflation.begin_measure,
                        end_measure=conflation.end_measure,
                        direction="UNKNOWN",
                        states=conflation.states,
                        geometry=GeoJsonGeometry(
                            type="LineString",
                            coordinates=[[lon, lat] for lon, lat in coordinates],
                        ),
                        positional_accuracy_meters=max(
                            conflation.positional_accuracy_meters or 0.0,
                            TILE_POSITIONAL_ACCURACY_FLOOR_METERS,
                        ),
                        conflation_method=conflation.method,
                    ),
                    # Flow data says traffic is slow, not which lane is blocked.
                    lane_impacts=[],
                    start_time=start_time,
                    end_time=end_time,
                    # NEVER "observed". There is no confidence field, so an
                    # observed reading is indistinguishable from a historical
                    # average and claiming observation would be a false provenance
                    # claim - the exact failure the confidence model guards against.
                    time_confidence="estimated",
                    # `warning_level` (minor/major) is the incident layer's own
                    # severity. Kept verbatim, never reconciled.
                    agency_severity=feature_properties.get("warning_level"),
                    agency_duration_minutes=duration_minutes(start_time, end_time),
                    source=SourceRef(
                        source_id=self.source_id,
                        agency=self.agency,
                        native_id=str(native_id) if native_id is not None else "unknown",
                        retrieved_at=ctx.retrieved_at,
                        # Tiles carry no publish timestamp. Claiming one would be
                        # inventing freshness we cannot observe.
                        source_updated_at=None,
                        contributed_fields=contributed,
                        raw_ref=ctx.raw_ref,
                    ),
                    # Nothing dropped.
                    extensions={
                        "als_layer": layer_name,
                        "als_kind": kind,
                        "als_segment_id": native_id,
                        "als_speed_kph": speed,
                        "als_congestion_ratio": congestion_ratio,
                        "als_road_kind": feature_properties.get("road_kind"),
                        "als_road_kind_detail": road_kind_detail,
                        "als_network": feature_properties.get("network"),
                        "als_is_link": feature_properties.get("is_link"),
                        "als_is_bridge": feature_properties.get("is_bridge"),
                        "als_is_tunnel": feature_properties.get("is_tunnel"),
                        "als_min_zoom": feature_properties.get("min_zoom"),
                        "als_warning_level": feature_properties.get("warning_level"),
                        "als_start_time_raw": feature_properties.get("start_time"),
                        "als_stop_time_raw": feature_properties.get("stop_time"),
                        "als_tile": tile_label,
                        # Mandatory attribution, carried per record.
                        "als_attribution": attribution,
                    },
                    # This method maps ONE feature and owns its own `issues` list, so
                    # every issue in it belongs to this record - no mark needed, unlike
                    # the adapters that accumulate across a loop (see record_issues).
                    # Copied rather than aliased: the same list also goes out as the
                    # feed's issues below.
                    mapping_issues=list(issues),
                )
            ],
            off_corridor=0,
            issues=issues,
        )

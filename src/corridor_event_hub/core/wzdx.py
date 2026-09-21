"""WZDx v4.2 projection and conformance check.

The fourth of the four problems this pipeline targets: standards conformance. The canonical model is a
SUPERSET of every input standard, so publishing WZDx is a PROJECTION - a
lossy, one-directional narrowing from what we hold to what the spec allows.

TWO THINGS LIVE HERE AND THEY BELONG TOGETHER:

  to_wzdx_feed()   the projection
  validate_feed()  the conformance check that runs in CI

Together, because non-conformance has to be a BUILD FAILURE. A projection
whose validator lives elsewhere drifts from it; a projection with no validator at
all produces a feed that looks right and fails in a consumer's parser. The check
runs over the projection's own output in the same test run.

WHAT THIS VALIDATOR IS, STATED PLAINLY. It encodes WZDx v4.2's REQUIRED FIELDS and
CLOSED ENUMERATIONS - the parts a consumer's parser will reject - as data below. It
is fast enough for unit tests and its messages name the projection field that is
wrong, which is what makes it useful to debug against.

IT IS NOT THE AUTHORITY. The official schema is, and it is vendored in
`reference/wzdx/4.2/` and run by `scripts/lib/wzdx_schema.py` from both
`npm run lint:wzdx` and `tests/test_wzdx_schema.py`. Where the two disagree, the
schema is right and this file is the bug. Do not add a rule here that the schema
does not have: a check stricter than the spec makes conformant output fail, which
teaches everyone to relax the check.

THE ENUM MAPPINGS ARE THE INTERESTING PART, because they are where a superset meets
a closed vocabulary and something has to give. Every one of them is a decision:

  - Our `direction` is EB/WB/BOTH/UNKNOWN. WZDx has no "both": a work zone
    affecting both carriageways is TWO road events in WZDx, or one with
    `direction: undefined`. We emit `undefined` rather than guessing a side,
    because guessing puts a closure on the wrong carriageway.
  - Our lane `status` has `intermittent`, which WZDx does not. It maps to
    `alternating-flow`, the closest defined value, and the original is retained in
    the event so nothing is lost on our side.
  - An event with no geometry CANNOT be published: WZDx requires it. Rather than
    invent a point, the projection derives a LineString from the corridor
    centerline when it has one, and EXCLUDES the event with a reason when it does
    not. Exclusions are counted and returned, never silent - the
    record-rather-than-drop rule applied to the output side.
  - `end_date` is REQUIRED and must be a timestamp. An event whose end time we do
    not know is excluded for that reason rather than published with an invented
    one - see the note at the exclusion site, because it drops real work zones and
    that has to be a visible decision rather than a quiet one.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any

from .config import load_json
from .lrs import CorridorConfig
from .timeutil import parse_iso
from .types import Event

#: The spec version this projection targets. In the feed AND in this constant, so a
#: version bump is one edit and the tests move with it. It must match the vendored
#: schema directory the conformance check reads (`reference/wzdx/<version>/`).
#:
#: 4.2 is the current spec, and it is also what TxDOT publishes - so the version we
#: emit is one a consumer on this corridor already parses.
WZDX_VERSION = "4.2"

#: The ONLY value WZDx permits for `feed_info.license`: the spec names this URL
#: literally, as an enum of one. A feed carrying any other licence URL fails the
#: schema, so `to_wzdx_feed` rejects one rather than publishing it.
WZDX_LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"

#: The projection is versioned like any other derived output, so a consumer
#: can tell a mapping fix from a data change.
PROJECTION_VERSION = "0.1.0"

#: Only work zones. WZDx is a WORK ZONE data exchange - publishing a crash or a
#: weather alert as a `work-zone` road event would be a conformance lie that
#: validates cleanly, which is the worst kind. The other classes leave through
#: their own standards (TMDD, WZDx RoadRestriction) or through the query API.
PUBLISHABLE_CLASSES = ("work_zone",)

#: Lifecycle states worth publishing. `clearing` is included deliberately: a work
#: zone being packed up still affects traffic, and a consumer that drops it early
#: sends a truck into a lane that is not open yet.
PUBLISHABLE_STATES = ("active", "clearing")

#: Sources this feed MAY republish: `redistributable: true` in the catalog, and
#: nothing else.
#:
#: DEFAULT-DENY, and the default is the whole point. `redistributable` is
#: three-valued in `config/sources.json` - true, false, and absent-meaning-UNKNOWN -
#: and unknown is not permission. It is the same rule ADR 0003 applies to a source
#: that goes silent: an unconfirmed term is not a term in our favour.
#:
#: WHY THIS GUARD EXISTS, from the failure it was found by. A deployed feed was
#: publishing 6 of 24 features attributed to `aws-location-traffic`, which carries
#: HERE content licensed via AWS with `redistributable: false` and mandatory
#: attribution. Nothing was wrong with the class filter: an Amazon Location
#: `traffic_incidents` feature with `kind: construction` legitimately maps to
#: `work_zone`, which is legitimately publishable. The licence was simply never
#: consulted on the way out. `PUBLISHABLE_CLASSES` answers "is this the right kind
#: of event"; only this answers "may we republish it at all".
_CATALOG_REDISTRIBUTABLE: dict[str, Any] = {
    source["sourceId"]: source.get("redistributable")
    for source in load_json("sources.json")["sources"]
}


def may_republish(source_id: str) -> bool:
    """Whether this feed may carry a record from ``source_id``.

    A source absent from the catalog returns False for the same reason an unknown
    value does: this is the point where a licence obligation becomes a published
    document, and a source nobody catalogued is a source nobody checked.
    """
    return _CATALOG_REDISTRIBUTABLE.get(source_id) is True

# ---------------------------------------------------------------------------
# Vocabulary crosswalks (crosswalks are DATA, not code)
# ---------------------------------------------------------------------------

_DIRECTION: dict[str, str] = {
    "EB": "eastbound",
    "WB": "westbound",
    # No WZDx value means "both carriageways". `undefined` is the honest answer;
    # picking a side would publish a closure on a road that is open.
    "BOTH": "undefined",
    "UNKNOWN": "unknown",
}

_LANE_STATUS: dict[str, str] = {
    "open": "open",
    "closed": "closed",
    # WZDx distinguishes shift direction, which our model does not carry. Without
    # the direction the honest projection is the generic value.
    "shifted": "shift-left",
    "alternating": "alternating-flow",
    "intermittent": "alternating-flow",
}

_LANE_TYPE: dict[str, str] = {
    "general": "general",
    "shoulder": "shoulder",
    "HOV": "general",  # WZDx has no HOV lane type
    "exit": "exit-lane",
    "entrance": "entrance-lane",
    "median": "median",
}

#: How a merge shows up in the published feed. Our `related_event_ids` are the other
#: events this one was reconciled with, which is `related-work-zone` in WZDx's
#: vocabulary - the sequence and occurrence values describe an agency's own planned
#: staging, and claiming one of those would assert an ordering nobody told us.
_RELATED_ROAD_EVENT_TYPE = "related-work-zone"

# ---------------------------------------------------------------------------
# What the spec requires. The validator reads these.
#
# EXACTLY the `required` arrays from reference/wzdx/4.2/, not a superset. A rule
# here that the schema does not have fails conformant output, and the fix everyone
# reaches for then is to relax the check.
# ---------------------------------------------------------------------------

_FEED_INFO_REQUIRED = ("update_date", "publisher", "version", "data_sources")
_DATA_SOURCE_REQUIRED = ("data_source_id", "organization_name")
_FEATURE_REQUIRED = ("id", "type", "properties", "geometry")
_CORE_DETAILS_REQUIRED = (
    "event_type",
    "data_source_id",
    "direction",
    "road_names",
)
#: `end_date` is in here, and it is not nullable. WZDx requires a work zone to say
#: when it ends; see `_feature` for what the projection does when we do not know.
_PROPERTIES_REQUIRED = (
    "core_details",
    "start_date",
    "end_date",
    "location_method",
    "vehicle_impact",
)

_ENUMS: dict[str, tuple[str, ...]] = {
    "event_type": ("work-zone", "detour", "restriction"),
    "direction": (
        "northbound",
        "eastbound",
        "southbound",
        "westbound",
        "undefined",
        "unknown",
        "inner-loop",
        "outer-loop",
    ),
    "related_road_event_type": (
        "first-in-sequence",
        "next-in-sequence",
        "first-occurrence",
        "next-occurrence",
        "related-work-zone",
        "related-detour",
        "planned-moving-operation",
        "active-moving-operation",
    ),
    "vehicle_impact": (
        "all-lanes-closed",
        "some-lanes-closed",
        "all-lanes-open",
        "alternating-one-way",
        "some-lanes-closed-merge-left",
        "some-lanes-closed-merge-right",
        "all-lanes-open-shift-left",
        "all-lanes-open-shift-right",
        "some-lanes-closed-split",
        "flagging",
        "temporary-traffic-signal",
        "unknown",
    ),
    "location_method": (
        "channel-device-method",
        "sign-method",
        "junction-method",
        "other",
        "unknown",
    ),
    "lane_status": ("open", "closed", "shift-left", "shift-right", "merge-left",
                    "merge-right", "alternating-flow"),
    "lane_type": (
        "general",
        "exit-lane",
        "exit-ramp",
        "entrance-lane",
        "entrance-ramp",
        "sidewalk",
        "bike-lane",
        "shoulder",
        "parking",
        "median",
        "two-way-center-turn-lane",
        "center-left-turn-lane",
    ),
}


@dataclass
class Excluded:
    """An event that could not be published conformantly, and why.

    A COUNTED, EXPLAINED exclusion rather than a silent drop. The publisher logs
    these and the feed carries the count: a work zone missing from a standards feed
    with no record of why is exactly the kind of quiet loss forbidden on the way
    in, and the output side deserves the same rule.
    """

    event_id: str
    reason: str


@dataclass
class Projection:
    feed: dict[str, Any]
    excluded: list[Excluded] = field(default_factory=list)

    @property
    def published(self) -> int:
        return len(self.feed.get("features", []))


def _iso_or_none(value: str | None) -> str | None:
    """WZDx wants ISO 8601. An unparseable timestamp becomes None so the validator
    catches a missing required date rather than a consumer choking on garbage.
    """
    return value if parse_iso(value) is not None else None


def corridor_slice(
    corridor: CorridorConfig, begin_measure: float, end_measure: float
) -> list[tuple[float, float]]:
    """The centerline between two corridor measures, as coordinates.

    WZDx requires geometry and most of our extents arrive as mileposts, so this is
    how a linear reference becomes a LineString. It reads the corridor's calibrated
    measures array - the same numbers `_scale_to_corridor` uses on the way in - so
    the published shape and the stored measure describe the same stretch of road.

    Returns [] for an uncalibrated corridor: without per-vertex measures there is no
    honest mapping from measure to position, and a fabricated line is worse than an
    excluded event.
    """
    measures = corridor.measures
    if not measures or not corridor.centerline:
        return []

    lo = min(begin_measure, end_measure)
    hi = max(begin_measure, end_measure)
    ascending = list(measures)
    start = max(0, bisect_left(ascending, lo) - 1)
    stop = min(len(ascending) - 1, bisect_left(ascending, hi) + 1)
    sliced = [corridor.centerline[i] for i in range(start, stop + 1)]

    # A point event lands between two vertices and slices to one or two of them. A
    # LineString needs two DISTINCT positions, so a degenerate slice is duplicated
    # rather than emitted as an invalid geometry.
    if len(sliced) == 1:
        sliced = [sliced[0], sliced[0]]
    return sliced


def _vehicle_impact(event: Event) -> str:
    """Lane impacts -> the single WZDx summary value.

    `unknown` when we hold no lane detail at all, which is the honest answer and the
    common one - the live Oklahoma feed reports `lanes: []`, meaning "we do not
    know", not "all lanes open". Publishing `all-lanes-open` there would invent
    reassurance the source never gave.
    """
    if not event.lane_impacts:
        return "unknown"
    general = [lane for lane in event.lane_impacts if lane.type == "general"]
    considered = general or event.lane_impacts
    closed = [lane for lane in considered if lane.status == "closed"]
    if not closed:
        if any(lane.status in ("alternating", "intermittent") for lane in considered):
            return "alternating-one-way"
        return "all-lanes-open"
    if len(closed) == len(considered):
        return "all-lanes-closed"
    return "some-lanes-closed"


def _feature(
    event: Event, corridor: CorridorConfig | None
) -> tuple[dict[str, Any] | None, str | None]:
    """One RoadEventFeature, or (None, reason) if it cannot be published."""
    geometry: dict[str, Any] | None = None
    if event.extent.geometry is not None:
        geometry = {
            "type": event.extent.geometry.type,
            "coordinates": event.extent.geometry.coordinates,
        }
    elif corridor is not None and not math.isnan(event.extent.begin_measure):
        coordinates = corridor_slice(
            corridor, event.extent.begin_measure, event.extent.end_measure
        )
        if coordinates:
            geometry = {
                "type": "LineString",
                "coordinates": [[lon, lat] for lon, lat in coordinates],
            }

    if geometry is None:
        return None, (
            "no geometry: the event has none of its own and the corridor centerline "
            "is unavailable or uncalibrated, so a WZDx-required geometry cannot be "
            "derived without inventing one"
        )

    start_date = _iso_or_none(event.start_time)
    if start_date is None:
        return None, f"unusable start_date: {event.start_time!r}"

    # WZDx REQUIRES end_date, as a timestamp. Not "the key must be present" - a
    # value, and null is not one.
    #
    # THIS DROPS REAL WORK ZONES, and it is still the right answer. Our `end_time`
    # is null when the source gave none or gave one we rejected - the Oklahoma feed
    # regenerates `end_date` on every request, so `adapters/ok_odot_wzdx.py` throws
    # those away rather than publish a timestamp that changes when you refresh. The
    # alternatives to excluding are worse: synthesizing an end from a class-average
    # duration puts a number in a standards feed that no agency said, and a
    # consumer scheduling around it cannot tell it from a real one.
    #
    # Excluded and counted, like a missing geometry. The count is the argument for
    # asking these DOTs for real end dates.
    end_date = _iso_or_none(event.end_time)
    if end_date is None:
        return None, (
            "no end_date: WZDx requires one and the source gave none we trust, so "
            "publishing would mean inventing an end time for a live work zone"
        )

    lanes = [
        {
            "order": lane.ordinal,
            "status": _LANE_STATUS.get(lane.status, "open"),
            "type": _LANE_TYPE.get(lane.type, "general"),
        }
        for lane in event.lane_impacts
    ]

    properties: dict[str, Any] = {
        "core_details": {
            "event_type": "work-zone",
            # One data source per contributing agency; the first is the winner of
            # most fields. All of them appear in feed_info.data_sources, which is
            # how provenance survives into the projection at all - WZDx has
            # no richer place to put it.
            "data_source_id": event.sources[0].source_id if event.sources else "unknown",
            "road_names": [event.extent.route],
            "direction": _DIRECTION.get(event.extent.direction, "unknown"),
            "description": _description(event),
            "creation_date": _iso_or_none(event.created_at),
            "update_date": _iso_or_none(event.updated_at) or _iso_or_none(event.created_at),
            "related_road_events": [
                {"type": _RELATED_ROAD_EVENT_TYPE, "id": related}
                for related in event.related_event_ids
            ],
        },
        "start_date": start_date,
        "end_date": end_date,
        # Our `time_confidence` says whether a time was observed, estimated
        # or scheduled, which is exactly what these booleans ask.
        "is_start_date_verified": event.time_confidence == "observed",
        "is_end_date_verified": event.time_confidence == "observed",
        # A conflated position is a projection onto a centerline, not a
        # surveyed one. `native_lrs` is the only method where the source itself gave
        # us the linear reference, so it is the only one that can claim verified.
        "is_start_position_verified": event.extent.conflation_method == "native_lrs",
        "is_end_position_verified": event.extent.conflation_method == "native_lrs",
        "location_method": "channel-device-method",
        "vehicle_impact": _vehicle_impact(event),
        "lanes": lanes,
        # The milepost a DOT actually recognizes, alongside the geometry.
        "beginning_milepost": _milepost(event.extent.begin_measure, corridor),
        "ending_milepost": _milepost(event.extent.end_measure, corridor),
    }

    return (
        {
            "id": event.event_id,
            "type": "Feature",
            "properties": properties,
            "geometry": geometry,
        },
        None,
    )


def _publisher_id(publisher: str) -> str:
    """A `data_source_id` for the publisher itself.

    Slugified rather than the raw name: `data_source_id` is an identifier a consumer
    keys on, and a value containing spaces and punctuation invites the kind of
    mismatch nobody notices until two feeds disagree about the same source.
    """
    slug = "".join(char.lower() if char.isalnum() else "-" for char in publisher)
    return "-".join(part for part in slug.split("-") if part) or "publisher"


def _milepost(measure: float, corridor: CorridorConfig | None) -> float | None:
    if corridor is None or math.isnan(measure):
        return None
    resolved = corridor.milepost_for(measure)
    return round(resolved[1], 2) if resolved else None


def _description(event: Event) -> str:
    """A human-readable summary.

    No field a machine needs may depend on reading prose, which is why every fact
    here also exists as a structured field. This is for the operator
    looking at a feed viewer, and it is assembled from the structured values rather
    than carried through from a source's own free text - so it cannot contradict
    them.
    """
    parts = [event.event_subtype.replace("_", " ") or "work zone"]
    if event.lane_impacts:
        closed = sum(1 for lane in event.lane_impacts if lane.status == "closed")
        parts.append(f"{closed} of {len(event.lane_impacts)} lanes closed")
    else:
        parts.append("lane impacts not reported by the source")
    parts.append(f"severity {event.severity.computed}")
    parts.append(f"confidence {event.confidence.value:.2f}")
    return "; ".join(parts)


def to_wzdx_feed(
    events: list[Event],
    *,
    publisher: str,
    update_date: str,
    corridor: CorridorConfig | None = None,
    contact_name: str | None = None,
    contact_email: str | None = None,
    license_url: str | None = None,
    update_frequency_seconds: int | None = None,
) -> Projection:
    """Project events into a WZDx v4.2 feed.

    Events outside ``PUBLISHABLE_CLASSES`` or ``PUBLISHABLE_STATES`` are skipped
    WITHOUT being counted as exclusions - they were never candidates for this feed.
    An event that should have been published and could not be IS counted, because
    that is a defect rather than a filter.
    """
    features: list[dict[str, Any]] = []
    excluded: list[Excluded] = []
    data_sources: dict[str, dict[str, Any]] = {}

    for event in events:
        if event.event_class not in PUBLISHABLE_CLASSES:
            continue
        if event.lifecycle_state not in PUBLISHABLE_STATES:
            continue

        # LICENCE BEFORE PROJECTION. Checked here rather than inside `_feature`
        # because it is not a property of the record's shape - a perfectly
        # well-formed work zone can be one we have no right to republish, and
        # conflating "malformed" with "not ours to publish" is how the second one
        # gets fixed by relaxing the first.
        #
        # ANY contributing source disqualifies the event, not just the first. A merge
        # means the published record carries fields from every source that
        # contributed, so one non-redistributable contributor makes the whole feature
        # unpublishable - taking the majority vote here would republish the licensed
        # part of a merged record and call it Texas's.
        # REFUSED and UNCONFIRMED are reported separately, though both block. They
        # are the same outcome and completely different problems: `false` is a
        # licence saying no and will never publish, `unknown` is a question nobody
        # asked ADOT yet and publishes the day someone does. Collapsing them into one
        # count is how "16 records are one phone call away" gets read as "16 records
        # are forbidden" - the same mistake as treating a blank severity as no impact.
        refused = sorted(
            {
                source.source_id
                for source in event.sources
                if _CATALOG_REDISTRIBUTABLE.get(source.source_id) is False
            }
        )
        unconfirmed = sorted(
            {
                source.source_id
                for source in event.sources
                if not may_republish(source.source_id)
                and _CATALOG_REDISTRIBUTABLE.get(source.source_id) is not False
            }
        )
        if refused or unconfirmed:
            parts = []
            if refused:
                parts.append(f"licence forbids republication ({', '.join(refused)})")
            if unconfirmed:
                parts.append(f"redistribution terms unconfirmed ({', '.join(unconfirmed)})")
            excluded.append(
                Excluded(
                    event_id=event.event_id,
                    reason="not redistributable: " + "; ".join(parts),
                )
            )
            continue

        feature, reason = _feature(event, corridor)
        if feature is None:
            excluded.append(Excluded(event_id=event.event_id, reason=reason or "unknown"))
            continue
        features.append(feature)

        for source in event.sources:
            data_sources.setdefault(
                source.source_id,
                {
                    "data_source_id": source.source_id,
                    "organization_name": source.agency,
                    "update_date": _iso_or_none(source.source_updated_at)
                    or _iso_or_none(source.retrieved_at),
                },
            )

    if not data_sources:
        # AN EMPTY CORRIDOR STILL HAS TO PRODUCE A CONFORMANT FEED, and `data_sources`
        # is required and non-empty. "No work zones anywhere" is a legitimate and
        # frequent answer - it is how a consumer learns the road is clear - so it
        # cannot be the one case that publishes an invalid document.
        #
        # With no features there is no contributing agency to name, so the feed names
        # US: Corridor Event Hub is the source of the (empty) answer. As soon as there is a
        # feature, its own agency appears here instead and this entry does not.
        data_sources[""] = {
            "data_source_id": _publisher_id(publisher),
            "organization_name": publisher,
            "update_date": update_date,
        }

    feed_info: dict[str, Any] = {
        "update_date": update_date,
        "publisher": publisher,
        "version": WZDX_VERSION,
        # Sorted so two runs over the same events produce byte-identical feeds -
        # which is what makes a diff between two published feeds meaningful.
        "data_sources": [data_sources[key] for key in sorted(data_sources)],
    }
    if update_frequency_seconds is not None:
        feed_info["update_frequency"] = update_frequency_seconds
    if contact_name:
        feed_info["contact_name"] = contact_name
    if contact_email:
        feed_info["contact_email"] = contact_email
    if license_url:
        if license_url != WZDX_LICENSE_URL:
            # Reject, do not coerce - and do not drop it either. WZDx permits one
            # licence URL and one only, so any other value is a configuration
            # mistake (`WZDX_LICENSE` in the query handler's environment). Failing
            # here surfaces it as a broken endpoint someone fixes; omitting it
            # silently would publish a feed that claims no licence at all, and
            # passing it through would publish one no consumer will accept.
            raise ValueError(
                f"WZDx permits only {WZDX_LICENSE_URL!r} for feed_info.license, "
                f"got {license_url!r}"
            )
        feed_info["license"] = license_url

    return Projection(
        feed={
            "feed_info": feed_info,
            "type": "FeatureCollection",
            "features": features,
        },
        excluded=excluded,
    )


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def validate_feed(feed: dict[str, Any]) -> list[str]:
    """Every conformance problem in this feed. Empty means it validates.

    RETURNS ALL of them rather than raising on the first: a mapping change that
    breaks four fields should show four errors in one CI run, not four runs.

    THE FAST CHECK, NOT THE AUTHORITY. Required fields, closed enumerations and
    geometry shape, with messages that name the projection field at fault. The
    official schema in `reference/wzdx/4.2/` is what conformance actually means -
    `scripts/lib/wzdx_schema.py` runs it, and every caller of this function in the
    test suite and in `npm run lint:wzdx` runs both.
    """
    errors: list[str] = []

    if feed.get("type") != "FeatureCollection":
        errors.append(f"feed.type must be 'FeatureCollection', got {feed.get('type')!r}")

    info = feed.get("feed_info")
    if not isinstance(info, dict):
        errors.append("feed_info is missing")
    else:
        for key in _FEED_INFO_REQUIRED:
            if info.get(key) in (None, "", []):
                errors.append(f"feed_info.{key} is required")
        if info.get("version") != WZDX_VERSION:
            errors.append(
                f"feed_info.version must be {WZDX_VERSION!r}, got {info.get('version')!r}"
            )
        if "license" in info and info.get("license") != WZDX_LICENSE_URL:
            errors.append(
                f"feed_info.license must be {WZDX_LICENSE_URL!r}, got {info.get('license')!r}"
            )
        for index, source in enumerate(info.get("data_sources") or []):
            for key in _DATA_SOURCE_REQUIRED:
                if not source.get(key):
                    errors.append(f"feed_info.data_sources[{index}].{key} is required")

    features = feed.get("features")
    if not isinstance(features, list):
        errors.append("features must be a list")
        return errors

    declared_sources = {
        source.get("data_source_id") for source in (info or {}).get("data_sources") or []
    }
    seen_ids: set[str] = set()

    for index, feature in enumerate(features):
        where = f"features[{index}]"
        if not isinstance(feature, dict):
            errors.append(f"{where} is not an object")
            continue
        for key in _FEATURE_REQUIRED:
            if feature.get(key) in (None, ""):
                errors.append(f"{where}.{key} is required")
        if feature.get("type") != "Feature":
            errors.append(f"{where}.type must be 'Feature', got {feature.get('type')!r}")

        feature_id = feature.get("id")
        if feature_id in seen_ids:
            # A duplicate id makes a consumer's upsert ambiguous, and it is exactly
            # what a dedup bug upstream would produce - so it is worth catching here
            # as well as there.
            errors.append(f"{where}.id {feature_id!r} is duplicated in this feed")
        if isinstance(feature_id, str):
            seen_ids.add(feature_id)

        errors.extend(_validate_geometry(feature.get("geometry"), where))

        properties = feature.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"{where}.properties is missing")
            continue

        for key in _PROPERTIES_REQUIRED:
            if key not in properties:
                errors.append(f"{where}.properties.{key} is required")
            elif properties.get(key) is None:
                errors.append(f"{where}.properties.{key} must not be null")

        errors.extend(
            _enum(properties, "vehicle_impact", "vehicle_impact", f"{where}.properties")
        )
        errors.extend(
            _enum(properties, "location_method", "location_method", f"{where}.properties")
        )
        errors.extend(_validate_dates(properties, where))

        core = properties.get("core_details")
        if not isinstance(core, dict):
            errors.append(f"{where}.properties.core_details is missing")
        else:
            for key in _CORE_DETAILS_REQUIRED:
                if core.get(key) in (None, "", []):
                    errors.append(f"{where}.properties.core_details.{key} is required")
            errors.extend(_enum(core, "event_type", "event_type", f"{where}.properties.core_details"))
            errors.extend(_enum(core, "direction", "direction", f"{where}.properties.core_details"))
            for related_index, related in enumerate(core.get("related_road_events") or []):
                related_where = (
                    f"{where}.properties.core_details.related_road_events[{related_index}]"
                )
                if not related.get("id"):
                    errors.append(f"{related_where}.id is required")
                errors.extend(_enum(related, "type", "related_road_event_type", related_where))
            source_id = core.get("data_source_id")
            if declared_sources and source_id not in declared_sources:
                # A feature naming a source the feed never declared is a broken
                # reference, and a consumer resolving provenance will find nothing.
                errors.append(
                    f"{where}.properties.core_details.data_source_id {source_id!r} is not "
                    "in feed_info.data_sources"
                )

        for lane_index, lane in enumerate(properties.get("lanes") or []):
            lane_where = f"{where}.properties.lanes[{lane_index}]"
            if not isinstance(lane.get("order"), int) or lane.get("order") < 1:
                errors.append(f"{lane_where}.order must be an integer >= 1")
            errors.extend(_enum(lane, "status", "lane_status", lane_where))
            errors.extend(_enum(lane, "type", "lane_type", lane_where))

    return errors


def _enum(holder: dict[str, Any], key: str, enum_name: str, where: str) -> list[str]:
    value = holder.get(key)
    if value is None:
        return []
    allowed = _ENUMS[enum_name]
    if value not in allowed:
        return [f"{where}.{key} {value!r} is not one of {', '.join(allowed)}"]
    return []


def _validate_dates(properties: dict[str, Any], where: str) -> list[str]:
    errors = []
    start = parse_iso(properties.get("start_date"))
    end = parse_iso(properties.get("end_date")) if properties.get("end_date") else None
    if properties.get("start_date") and start is None:
        errors.append(f"{where}.properties.start_date is not ISO 8601")
    if properties.get("end_date") and end is None:
        errors.append(f"{where}.properties.end_date is not ISO 8601")
    if start and end and end < start:
        errors.append(f"{where}.properties.end_date precedes start_date")
    return errors


def _validate_geometry(geometry: Any, where: str) -> list[str]:
    if not isinstance(geometry, dict):
        return [f"{where}.geometry is missing"]
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if kind not in ("Point", "LineString", "MultiPoint", "MultiLineString"):
        return [f"{where}.geometry.type {kind!r} is not valid for a WZDx road event"]
    if kind == "Point":
        positions = [coordinates]
    elif kind in ("LineString", "MultiPoint"):
        positions = coordinates
    else:
        positions = [p for part in (coordinates or []) for p in part]

    if not isinstance(positions, list) or not positions:
        return [f"{where}.geometry.coordinates is empty"]
    if kind == "LineString" and len(positions) < 2:
        return [f"{where}.geometry LineString needs at least 2 positions"]

    errors = []
    for position in positions:
        if not isinstance(position, (list, tuple)) or len(position) < 2:
            errors.append(f"{where}.geometry has a malformed position: {position!r}")
            break
        lon, lat = position[0], position[1]
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            errors.append(f"{where}.geometry position is not numeric: {position!r}")
            break
        # Caught here because a lon/lat swap validates as GeoJSON and puts the
        # corridor in the Indian Ocean - a mistake with no other tripwire.
        if not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
            errors.append(
                f"{where}.geometry position out of range: {position!r} "
                "(GeoJSON is [longitude, latitude])"
            )
            break
    return errors

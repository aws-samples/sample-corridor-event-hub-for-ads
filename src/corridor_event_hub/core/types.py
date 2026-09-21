"""Corridor Event Hub canonical types.

The canonical model is deliberately a SUPERSET of every input standard, so nothing
is lost on the way in. Fields with no
canonical home go in ``extensions``, never dropped.

All eight event classes are representable from day one, even where no adapter
exists yet - that is what makes "build the pipe once and the classes
follow" literally true rather than aspirational.

WHY DATACLASSES AND NOT TypedDict OR pydantic. Dataclasses give the adapter
boundary real teeth: an adapter can only construct a ``CandidateEvent``, which
has no field for a lifecycle state or a confidence score, so the adapter contract
is enforced by the constructor rather than by code review. pydantic would add validation we
do not want at this boundary - a malformed agency payload must become a
MappingIssue, not an exception - and a dependency the Lambda bundle does
not otherwise need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Event classification
# ---------------------------------------------------------------------------

# The eight classes, and an adopter should expect to renegotiate them.
#
# A taxonomy this pipeline was measured against splits `incident` into three
# (crash/disabled vehicle, emergency response, debris/obstacle), adds temporary
# traffic control, and does not name `weather` or `dimensional_restriction` at
# all - overlapping these on only FOUR: closure, work_zone, congestion,
# road_surface. Expect that kind of disagreement rather than a clean adoption.
#
# The names here are deliberately NOT chased to fit any one of them. They are
# load-bearing for the lifecycle profiles below, the confidence profiles, the
# severity baselines, and the TTL tables - all built and tested against these
# eight strings. Renaming speculatively churns four subsystems.
#
# The split, if adopted, is a mapping-table change and not an adapter change
# - but ONLY once `event_subtype` draws from a controlled vocabulary
#. It does not today: subtypes are passthrough strings from four vendors,
# so promoting subtype to class right now would make the class enum inherit
# vendor spellings, which is exactly what the canonical enum exists to prevent.
#
# These are plain string tuples rather than enums on purpose. Class and state
# names cross the wire as JSON in EventBridge details and DynamoDB items, and an
# enum would mean serializing on every boundary for no added safety - the values
# are validated where they enter, in the adapters.
EVENT_CLASSES = (
    "work_zone",
    "incident",
    "closure",
    "congestion",
    "weather",
    "road_surface",
    "dimensional_restriction",
    "truck_parking",
)

# The seven lifecycle states.
LIFECYCLE_STATES = (
    "reported",
    "validated",
    "active",
    "merged",
    "clearing",
    "cleared",
    "archived",
)

SEVERITIES = ("minor", "moderate", "major", "severe")

# Direction normalized across all sources.
DIRECTIONS = ("EB", "WB", "BOTH", "UNKNOWN")

# ---------------------------------------------------------------------------
# Spatial
# ---------------------------------------------------------------------------

# How an extent got its position. Drives the spatial-precision component of
# confidence - a text-geocoded extent must score lower than a
# coordinate-precise one.
CONFLATION_METHODS = (
    "native_lrs",  # source gave us a route + measure directly
    "milepost",  # source gave a state milepost, offset applied
    "coordinate",  # projected onto the corridor centerline
    "polygon_intersect",  # polygon x corridor (NWS alerts)
    "sensor_snap",  # point sensor snapped to nearest centerline position
    "text_geocode",  # parsed from prose - lowest precision
    "unresolved",
)


@dataclass
class GeoJsonGeometry:
    """A GeoJSON geometry, carried through verbatim.

    ``coordinates`` is intentionally untyped: the point is to hand the source's
    own geometry downstream unchanged, not to re-model GeoJSON.
    """

    type: str  # Point | LineString | Polygon | MultiLineString | MultiPolygon
    coordinates: Any


@dataclass
class Extent:
    """Every event carries BOTH linear referencing and geometry.

    A measure range may span states, so one event covers a state line
    rather than becoming two events.
    """

    route: str  # corridor id, supplied by configuration rather than code
    begin_measure: float  # corridor-wide miles from the western terminus
    end_measure: float  # equals begin_measure for point events
    direction: str
    # States the extent touches, west to east. Length > 1 means a state line.
    states: list[str]
    geometry: GeoJsonGeometry | None
    positional_accuracy_meters: float | None
    conflation_method: str


# ---------------------------------------------------------------------------
# Lane impacts
# ---------------------------------------------------------------------------

LANE_TYPES = ("general", "shoulder", "HOV", "exit", "entrance", "median")

LANE_STATUSES = ("open", "closed", "shifted", "alternating", "intermittent")


@dataclass
class LaneImpact:
    """Per-lane, ordinal from the left edge."""

    ordinal: int  # 1 = leftmost
    type: str
    status: str
    # True when derived from prose rather than stated by the source.
    # Inferred impacts carry reduced confidence and MUST retain the source text.
    inferred: bool
    inferred_from: str | None = None


# ---------------------------------------------------------------------------
# Provenance and confidence
# ---------------------------------------------------------------------------


@dataclass
class SourceRef:
    """Every contributing source, and which fields it contributed."""

    source_id: str  # catalog key, e.g. 'ok-odot-wzdx'
    agency: str
    native_id: str  # the source's own record id
    retrieved_at: str  # when WE fetched it
    source_updated_at: str | None  # when the SOURCE says it changed
    contributed_fields: list[str]
    raw_ref: str  # s3://... the exact bytes this came from


@dataclass
class ConfidenceBreakdown:
    """Confidence is a value WITH a published breakdown, never an opaque
    number. If the breakdown is not built alongside the score, the quality claim
    hollows out (README.md § Not built yet).
    """

    source_reliability: float  # per-source rolling accuracy
    corroboration: float  # independent sources agreeing
    recency: float  # decay since last confirming update
    spatial_precision: float  # from conflation_method + accuracy
    completeness: float  # required fields present for this class
    internal_consistency: float  # contradictions across sources


@dataclass
class Confidence:
    value: float  # 0.0 - 1.0
    breakdown: ConfidenceBreakdown
    model_version: str  # Consumers can see which model scored this
    computed_at: str


# ---------------------------------------------------------------------------
# Duration and severity
# ---------------------------------------------------------------------------


@dataclass
class ExpectedDuration:
    """A point estimate plus bounds and a basis tag."""

    estimate_minutes: int | None
    low_minutes: int | None
    high_minutes: int | None
    basis: str  # agency_stated | class_prior | model


@dataclass
class SeverityAssessment:
    """Agency-asserted severity is retained, never silently reconciled."""

    computed: str
    score: float  # 0-100
    function_version: str  # Historical scores stay reproducible
    agency_asserted: str | None


# ---------------------------------------------------------------------------
# The Event
# ---------------------------------------------------------------------------


@dataclass
class Event:
    event_id: str  # ULID, stable across the whole lifecycle
    event_class: str
    event_subtype: str
    lifecycle_state: str
    version: int  # monotonic per event_id

    extent: Extent
    lane_impacts: list[LaneImpact]
    severity: SeverityAssessment
    confidence: Confidence
    expected_duration: ExpectedDuration

    start_time: str
    end_time: str | None  # None while open-ended
    time_confidence: str  # scheduled | estimated | observed

    sources: list[SourceRef]  # >= 1, always
    related_event_ids: list[str]  # merge parents, secondary crashes, detours

    created_at: str  # system time, distinct from event time
    updated_at: str

    # No input field is ever dropped.
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass
class MappingIssue:
    field: str
    raw_value: Any
    # unmapped_vocabulary | missing_required | unparseable | out_of_corridor
    # | synthetic_placeholder  (parses fine, but the agency generated it - see
    #   ok_odot_wzdx.regenerated_end_date_minutes)
    reason: str
    detail: str | None = None


@dataclass
class CandidateEvent:
    """What an adapter emits.

    Adapters do parse -> map -> conflate and NOTHING else: no dedup, no
    scoring, no lifecycle decisions. Enforcing that boundary is what keeps the
    architecture portable - the moment an adapter sets lifecycle state, the
    reference architecture stops generalizing. Note what this class does NOT
    have: no ``event_id``, no ``lifecycle_state``, no ``confidence``. An adapter
    has no vocabulary for expressing those.
    """

    event_class: str
    event_subtype: str
    extent: Extent
    lane_impacts: list[LaneImpact]
    start_time: str
    end_time: str | None
    time_confidence: str
    agency_severity: str | None
    agency_duration_minutes: int | None
    source: SourceRef
    extensions: dict[str, Any] = field(default_factory=dict)
    # Unmappable values go to review, never dropped or defaulted.
    mapping_issues: list[MappingIssue] = field(default_factory=list)

"""Serialization for the canonical model.

WHY THIS EXISTS: the canonical model is dataclasses (see ``types.py`` for why),
but everything it crosses - EventBridge details, DynamoDB items, the WZDx
projection, log lines - is JSON. ``dataclasses.asdict`` would almost work, except
for two things that matter here:

1. NaN. An unresolved extent carries ``begin_measure = nan`` (never guess a
   location). Python's ``json.dumps`` emits a bare ``NaN`` token by default, which
   is NOT valid JSON - EventBridge and DynamoDB both reject it, and the failure
   surfaces at runtime in a Lambda rather than in a test. So NaN becomes ``null``.

2. Key naming. The canonical field names are snake_case
   (``event_id``, ``related_event_ids``), and so is the Python model, so the wire
   wire format matches the canonical model field for field. No translation layer,
   which is the point: an adopter can grep the model and the payload with one word.

READING IS NOT THE MIRROR OF WRITING, which is why the ``*_from_dict`` readers in
the second half of this file are hand-written rather than generated from the
dataclass fields. Two asymmetries force it:

  NaN COMES BACK. ``to_jsonable`` turns an unresolved measure into ``null``
  because that is what crosses the wire. A reader that left it ``None`` would hand
  downstream code a float field holding None, and the first ``math.isnan`` call on
  it raises. So ``null`` measures are restored to NaN, and the round trip is
  lossless in the only sense that matters.

  MISSING IS NOT EMPTY, but sometimes it has to be. A candidate that crossed
  EventBridge before a field existed has no key for it; refusing to read that
  record would make a schema addition a replay-breaking change. So new
  fields read with defaults and required ones raise.
"""

from __future__ import annotations

import dataclasses
import json
import math
from typing import Any

from .types import (
    CandidateEvent,
    Confidence,
    ConfidenceBreakdown,
    Event,
    ExpectedDuration,
    Extent,
    GeoJsonGeometry,
    LaneImpact,
    MappingIssue,
    SeverityAssessment,
    SourceRef,
)


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses, and anything containing them, into JSON-safe types.

    NaN and infinity become ``None`` - an unresolvable measure is genuinely
    absent, and ``null`` says that in a way every consumer understands.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def dumps(value: Any, **kwargs: Any) -> str:
    """JSON-encode a canonical object. ``allow_nan=False`` is a tripwire: if a
    non-finite number ever survives ``to_jsonable``, fail here in a test rather
    than emit invalid JSON that a downstream AWS API rejects.
    """
    return json.dumps(to_jsonable(value), allow_nan=False, **kwargs)


# ---------------------------------------------------------------------------
# Reading back - the resolver's side of the wire
# ---------------------------------------------------------------------------
#
# The normalizer writes a candidate into an EventBridge detail and the resolver
# reads it out again; the event store writes an Event as a DynamoDB item and the
# query API reads it out again. Both directions have to agree exactly, so both live
# in this file.


def _measure(value: Any) -> float:
    """A measure, with ``null`` restored to NaN.

    Guessing a location is forbidden, so an unresolved extent carries NaN and
    crosses the wire as ``null``. Reading it back as 0.0 would place the event at
    the corridor's western terminus - a real place, confidently wrong, and exactly
    the class of silent error this project keeps designing against.
    """
    if value is None:
        return math.nan
    return float(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def geometry_from_dict(data: dict[str, Any] | None) -> GeoJsonGeometry | None:
    if not data:
        return None
    return GeoJsonGeometry(type=data["type"], coordinates=data.get("coordinates"))


def extent_from_dict(data: dict[str, Any]) -> Extent:
    return Extent(
        route=data["route"],
        begin_measure=_measure(data.get("begin_measure")),
        end_measure=_measure(data.get("end_measure")),
        direction=data.get("direction") or "UNKNOWN",
        states=list(data.get("states") or []),
        geometry=geometry_from_dict(data.get("geometry")),
        positional_accuracy_meters=_optional_float(data.get("positional_accuracy_meters")),
        conflation_method=data.get("conflation_method") or "unresolved",
    )


def lane_impact_from_dict(data: dict[str, Any]) -> LaneImpact:
    return LaneImpact(
        ordinal=int(data["ordinal"]),
        type=data["type"],
        status=data["status"],
        inferred=bool(data.get("inferred")),
        inferred_from=data.get("inferred_from"),
    )


def source_ref_from_dict(data: dict[str, Any]) -> SourceRef:
    return SourceRef(
        source_id=data["source_id"],
        agency=data["agency"],
        native_id=data["native_id"],
        retrieved_at=data["retrieved_at"],
        source_updated_at=data.get("source_updated_at"),
        contributed_fields=list(data.get("contributed_fields") or []),
        raw_ref=data.get("raw_ref") or "",
    )


def confidence_from_dict(data: dict[str, Any]) -> Confidence:
    breakdown = data.get("breakdown") or {}
    return Confidence(
        value=float(data.get("value") or 0.0),
        breakdown=ConfidenceBreakdown(
            source_reliability=float(breakdown.get("source_reliability") or 0.0),
            corroboration=float(breakdown.get("corroboration") or 0.0),
            recency=float(breakdown.get("recency") or 0.0),
            spatial_precision=float(breakdown.get("spatial_precision") or 0.0),
            completeness=float(breakdown.get("completeness") or 0.0),
            internal_consistency=float(breakdown.get("internal_consistency") or 0.0),
        ),
        model_version=data.get("model_version") or "unknown",
        computed_at=data.get("computed_at") or "",
    )


def candidate_from_dict(data: dict[str, Any]) -> CandidateEvent:
    """Rebuild the adapter's output from an EventBridge detail."""
    return CandidateEvent(
        event_class=data["event_class"],
        event_subtype=data.get("event_subtype") or "",
        extent=extent_from_dict(data["extent"]),
        lane_impacts=[lane_impact_from_dict(lane) for lane in data.get("lane_impacts") or []],
        start_time=data.get("start_time") or "",
        end_time=data.get("end_time"),
        time_confidence=data.get("time_confidence") or "estimated",
        agency_severity=data.get("agency_severity"),
        agency_duration_minutes=_optional_int(data.get("agency_duration_minutes")),
        source=source_ref_from_dict(data["source"]),
        extensions=dict(data.get("extensions") or {}),
        mapping_issues=[
            MappingIssue(
                field=issue["field"],
                raw_value=issue.get("raw_value"),
                reason=issue["reason"],
                detail=issue.get("detail"),
            )
            for issue in data.get("mapping_issues") or []
        ],
    )


def event_from_dict(data: dict[str, Any]) -> Event:
    """Rebuild a stored Event. The inverse of ``to_jsonable(event)``."""
    severity = data.get("severity") or {}
    duration = data.get("expected_duration") or {}
    return Event(
        event_id=data["event_id"],
        event_class=data["event_class"],
        event_subtype=data.get("event_subtype") or "",
        lifecycle_state=data["lifecycle_state"],
        version=int(data["version"]),
        extent=extent_from_dict(data["extent"]),
        lane_impacts=[lane_impact_from_dict(lane) for lane in data.get("lane_impacts") or []],
        severity=SeverityAssessment(
            computed=severity.get("computed") or "minor",
            score=float(severity.get("score") or 0.0),
            function_version=severity.get("function_version") or "unknown",
            agency_asserted=severity.get("agency_asserted"),
        ),
        confidence=confidence_from_dict(data.get("confidence") or {}),
        expected_duration=ExpectedDuration(
            estimate_minutes=_optional_int(duration.get("estimate_minutes")),
            low_minutes=_optional_int(duration.get("low_minutes")),
            high_minutes=_optional_int(duration.get("high_minutes")),
            basis=duration.get("basis") or "class_prior",
        ),
        start_time=data.get("start_time") or "",
        end_time=data.get("end_time"),
        time_confidence=data.get("time_confidence") or "estimated",
        sources=[source_ref_from_dict(source) for source in data.get("sources") or []],
        related_event_ids=list(data.get("related_event_ids") or []),
        created_at=data.get("created_at") or "",
        updated_at=data.get("updated_at") or "",
        extensions=dict(data.get("extensions") or {}),
    )

"""Confidence scoring.

Confidence is a 0.0-1.0 value WITH A PUBLISHED BREAKDOWN, never an opaque
number. README.md § Not built yet names the failure mode
directly: "if the breakdown isn't built alongside the score, it never gets built, and
the quality claim hollows out." So
the breakdown is constructed here, in the same function, and the return type makes
it impossible to produce a value without one.

Why this matters more than it might seem: the consumer is an automated truck
deciding whether to trust a record about a hazard beyond its sensor range. An
integrator has to be able to set a trust threshold and understand the behavior on
each side of it. An unexplainable score cannot support that.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import load_json
from .lifecycle import profile_for
from .timeutil import iso_utc, parse_iso
from .types import CandidateEvent, Confidence, ConfidenceBreakdown, SourceRef

CONFIDENCE_MODEL_VERSION = "0.1.0"

# Per-source reliability and independence, READ FROM THE SOURCE CATALOG.
#
# These are not literal tables here, for two reasons, and the first is the
# load-bearing one:
#
# 1. PORTABILITY. A table keyed by `sourceId` puts ids like `ok-odot-wzdx` inside
#    corridor_event_hub/core, and core services must name no state or agency. This
#    module lives in core precisely because it is portable to any corridor;
#    scripts/check-portability.sh fails on exactly this. Note that such a table
#    can hide there for a while - it only trips the check once an added id happens
#    to match one of its patterns.
#
# 2. These should be COMPUTED from observed outcomes (false-positive rate,
#    clear-time accuracy, location error, staleness) and recalculated on a
#    schedule. The reliability job must be able to overwrite them without a code
#    deploy.
#
# The catalog is the sanctioned home for per-source knowledge, so adding a
# source is a catalog edit rather than a core code change.
#
# `seedReliability` is a SEED PRIOR - a hand-guessed starting value. Labelling
# them as guesses is the honest thing to do: a learned score is a Should in the
# scope tiering, not a Must.
#
# Sources sharing an `independenceGroup` count ONCE for
# corroboration. Two feeds reselling the same probe data, or two state feeds
# mirroring the same regional TMC, are not independent evidence - treating them as
# such inflates confidence exactly when it should not, which is worse than having
# no corroboration signal at all.
_CATALOG = load_json("sources.json")["sources"]

SEED_SOURCE_RELIABILITY: dict[str, float] = {
    s["sourceId"]: float(s["seedReliability"])
    for s in _CATALOG
    if isinstance(s.get("seedReliability"), (int, float))
}

INDEPENDENCE_GROUPS: dict[str, str] = {
    s["sourceId"]: s["independenceGroup"]
    for s in _CATALOG
    if isinstance(s.get("independenceGroup"), str)
}

# An unknown source is neither trusted nor dismissed: publishable, never
# authoritative, until it has a track record.
DEFAULT_RELIABILITY = 0.5

# Spatial precision by conflation method.
_PRECISION_BY_METHOD: dict[str, float] = {
    "native_lrs": 1.0,
    "milepost": 0.9,
    "coordinate": 0.85,
    "sensor_snap": 0.7,
    "polygon_intersect": 0.55,  # county-sized polygons are coarse for a corridor
    "text_geocode": 0.35,  # Text-geocoded scores lower, explicitly
    "unresolved": 0.0,
}

# Fields each class needs to be considered complete.
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "work_zone": ["extent", "start_time", "lane_impacts"],
    "incident": ["extent", "start_time", "lane_impacts", "event_subtype"],
    "closure": ["extent", "start_time", "lane_impacts"],
    "congestion": ["extent", "start_time"],
    "weather": ["extent", "start_time", "event_subtype"],
    "road_surface": ["extent", "start_time", "event_subtype"],
    "dimensional_restriction": ["extent"],
    "truck_parking": ["extent"],
}

# Weights are a reasoned starting point, NOT a tuned model - nothing here has been
# fitted against outcomes. They are declared here rather than buried so an adopter
# can re-set them without reading the algorithm.
WEIGHTS: dict[str, float] = {
    "source_reliability": 0.25,
    "corroboration": 0.2,
    "recency": 0.2,
    "spatial_precision": 0.15,
    "completeness": 0.1,
    "internal_consistency": 0.1,
}


@dataclass
class ScoringInput:
    candidate: CandidateEvent
    # All sources currently corroborating this event, including the candidate's.
    sources: Sequence[SourceRef]
    # When the most recent confirming update arrived.
    last_confirmed_at: str
    # Contradictions found by the matcher.
    conflict_count: int = 0
    now: datetime | None = None


def score_confidence(scoring_input: ScoringInput) -> Confidence:
    """Compute confidence and its breakdown together."""
    now = scoring_input.now or datetime.now(timezone.utc)
    candidate = scoring_input.candidate
    sources = list(scoring_input.sources)

    breakdown = ConfidenceBreakdown(
        source_reliability=_score_source_reliability(sources),
        corroboration=score_corroboration(sources),
        recency=score_recency(scoring_input.last_confirmed_at, candidate.event_class, now),
        spatial_precision=_score_spatial_precision(candidate),
        completeness=_score_completeness(candidate),
        internal_consistency=_score_internal_consistency(
            candidate, scoring_input.conflict_count
        ),
    )

    value = sum(weight * getattr(breakdown, name) for name, weight in WEIGHTS.items())

    return Confidence(
        value=round(max(0.0, min(1.0, value)), 4),
        breakdown=breakdown,
        model_version=CONFIDENCE_MODEL_VERSION,
        computed_at=iso_utc(now),
    )


def _score_source_reliability(sources: Sequence[SourceRef]) -> float:
    if not sources:
        return 0.0
    # Best available source, not the average: one unreliable corroborator should
    # not drag down an otherwise authoritative report.
    return max(
        SEED_SOURCE_RELIABILITY.get(s.source_id, DEFAULT_RELIABILITY) for s in sources
    )


def score_corroboration(sources: Sequence[SourceRef]) -> float:
    """Corroboration, weighted by independence.

    A source repeating its OWN report is not corroboration. Deduplicating
    by independence group is what prevents that self-corroboration inflation.
    """
    groups = {INDEPENDENCE_GROUPS.get(s.source_id, s.source_id) for s in sources}
    if not groups:
        return 0.0
    if len(groups) == 1:
        return 0.5  # single-source: plausible, uncorroborated
    if len(groups) == 2:
        return 0.85
    return 1.0


def score_recency(last_confirmed_at: str, event_class: str, now: datetime) -> float:
    """Continuous decay on a class-specific half-life, not step-on-poll."""
    half_life = profile_for(event_class).confidence_half_life_seconds
    confirmed = parse_iso(last_confirmed_at)
    if confirmed is None:
        return 0.0
    age_seconds = max(0.0, (now - confirmed).total_seconds())
    return round(math.pow(0.5, age_seconds / half_life), 4)


def _score_spatial_precision(candidate: CandidateEvent) -> float:
    base = _PRECISION_BY_METHOD.get(candidate.extent.conflation_method, 0.5)
    accuracy = candidate.extent.positional_accuracy_meters
    if accuracy is None:
        return base
    # Penalize beyond ~500m; a corridor event located to +/- 2km is weak evidence
    # of WHERE, even if it is strong evidence of WHAT.
    penalty = min(0.4, max(0.0, (accuracy - 500) / 5000))
    return round(max(0.0, base - penalty), 4)


def _score_completeness(candidate: CandidateEvent) -> float:
    required = _REQUIRED_FIELDS.get(candidate.event_class, ["extent"])
    present = 0
    for name in required:
        if name == "extent":
            if not math.isnan(candidate.extent.begin_measure):
                present += 1
        elif name == "lane_impacts":
            # The live ODOT case: lanes:[] means we do NOT know lane impacts.
            if candidate.lane_impacts:
                present += 1
        elif name == "start_time":
            if candidate.start_time:
                present += 1
        elif name == "event_subtype":
            if candidate.event_subtype and candidate.event_subtype != "advisory":
                present += 1
        else:
            present += 1
    return round(present / len(required), 4)


def _score_internal_consistency(candidate: CandidateEvent, conflicts: int) -> float:
    score = 1.0
    # Each unresolved cross-source contradiction costs.
    score -= min(0.6, conflicts * 0.2)
    # Inferred lane impacts are weaker evidence than stated ones.
    if any(lane.inferred for lane in candidate.lane_impacts):
        score -= 0.15
    # An end time we rejected as implausible is itself an inconsistency signal.
    if candidate.end_time is None and candidate.agency_duration_minutes is None:
        score -= 0.05
    return round(max(0.0, score), 4)


def explain_confidence(confidence: Confidence) -> list[str]:
    """A human- and integrator-readable explanation. This is what
    makes the API's confidence field defensible rather than mystical.
    """
    labels = {
        "source_reliability": "source reliability",
        "corroboration": "independent corroboration",
        "recency": "recency of last confirmation",
        "spatial_precision": "spatial precision",
        "completeness": "field completeness",
        "internal_consistency": "internal consistency",
    }
    lines = []
    for name, label in labels.items():
        raw = getattr(confidence.breakdown, name)
        contribution = WEIGHTS[name] * raw
        lines.append(f"{label}: {raw:.2f} x weight {WEIGHTS[name]} = {contribution:.3f}")
    lines.append(f"total: {confidence.value:.4f} (model {confidence.model_version})")
    return lines

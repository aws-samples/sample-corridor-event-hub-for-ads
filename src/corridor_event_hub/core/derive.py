"""Derived fields: computed severity and expected duration.

WHY THESE TWO LIVE TOGETHER AND WHY THEY LIVE HERE. An adapter must not compute
either of them - the adapter contract forbids it, and ``CandidateEvent``
deliberately has no field to put them in, only ``agency_severity`` and ``agency_duration_minutes`` as reported.
The resolver is the first thing in the pipeline that constructs an ``Event``, so
it is the first thing that needs both, and both are derived fields in the strict
sense: labelled as derived, with the producing logic's version.

THE VERSION STRINGS ARE THE POINT. A historical severity score has to stay
reproducible, which is impossible unless every score records which function
produced it. Bump the version when the arithmetic changes, never when a comment
does - a version that moves for cosmetic reasons stops meaning anything.

WHAT THESE ARE NOT. Neither is a tuned model. The severity weights and the
duration priors are hand-set starting points, declared as data at the top of the
module so the team can argue about them on a whiteboard rather than reverse
engineer them from arithmetic - the same treatment as the confidence weights and
the match thresholds. A learned duration model is a Should rather than a Must,
not a Must.
"""

from __future__ import annotations

from .types import ExpectedDuration, LaneImpact, SeverityAssessment

SEVERITY_FUNCTION_VERSION = "0.1.0"
DURATION_FUNCTION_VERSION = "0.1.0"

# Baseline severity per class, 0-100, before lane impacts adjust it.
#
# A closure starts high because a closed road is severe whatever else is true; a
# truck-parking record starts near zero because it is inventory, not a hazard. The
# ordering here matters more than the exact numbers.
_CLASS_BASELINE: dict[str, float] = {
    "closure": 70.0,
    "incident": 50.0,
    "work_zone": 35.0,
    "congestion": 30.0,
    "weather": 40.0,
    "road_surface": 45.0,
    "dimensional_restriction": 25.0,
    "truck_parking": 5.0,
}

# An unknown class scores mid-range rather than zero: an unrecognized class is an
# unknown hazard, and scoring it harmless is the wrong failure for a truck.
_DEFAULT_BASELINE = 40.0

# What a closed general-purpose lane adds. Shoulders and ramps matter less to a
# truck in a travel lane, which is the consumer this scale is calibrated for.
_LANE_CLOSURE_POINTS: dict[str, float] = {
    "general": 12.0,
    "HOV": 4.0,
    "exit": 5.0,
    "entrance": 5.0,
    "median": 2.0,
    "shoulder": 3.0,
}

# Bands the 0-100 score maps onto. SEVERITIES in types.py is the vocabulary; this
# is where the number becomes one of its four words.
_BANDS = ((80.0, "severe"), (60.0, "major"), (35.0, "moderate"))

# Duration priors per class, minutes, as (low, estimate, high). Used only when the
# agency states nothing - a point estimate needs BOUNDS, and bounds
# invented per record would be less honest than a published per-class prior.
_DURATION_PRIORS: dict[str, tuple[int, int, int]] = {
    "incident": (20, 60, 180),
    "closure": (60, 240, 1440),
    "work_zone": (1440, 10080, 129600),
    "congestion": (10, 30, 120),
    "weather": (60, 360, 1440),
    "road_surface": (60, 240, 1440),
    "dimensional_restriction": (525600, 525600, 525600),  # effectively permanent
    "truck_parking": (525600, 525600, 525600),
}

_DEFAULT_PRIOR = (20, 60, 180)  # the incident prior: shortest realistic bounds

# How much wider the bounds get around an agency's own stated duration. Agencies
# state a plan, not an outcome, and a stated 60 minutes routinely runs to 90.
_STATED_LOW_FACTOR = 0.5
_STATED_HIGH_FACTOR = 2.0


def derive_severity(
    event_class: str,
    lane_impacts: list[LaneImpact],
    agency_severity: str | None,
) -> SeverityAssessment:
    """Compute a severity WITHOUT discarding what the agency said.

    The agency's own word is carried through in ``agency_asserted`` even when it
    disagrees with the computed band, because reconciling the two silently would
    destroy the evidence that they disagreed - and a systematic disagreement with
    one agency's severity vocabulary is a finding about the crosswalk, not noise.
    """
    score = _CLASS_BASELINE.get(event_class, _DEFAULT_BASELINE)

    for lane in lane_impacts:
        if lane.status == "closed":
            score += _LANE_CLOSURE_POINTS.get(lane.type, 5.0)
        elif lane.status in ("shifted", "alternating", "intermittent"):
            # Passable but degraded: a fraction of the closure cost.
            score += _LANE_CLOSURE_POINTS.get(lane.type, 5.0) * 0.4
        if lane.inferred:
            # An inferred impact is weaker evidence, so it moves the score
            # less than a stated one. It still moves it - discarding it entirely
            # would score a prose-only closure as an open road.
            score -= _LANE_CLOSURE_POINTS.get(lane.type, 5.0) * 0.3

    score = round(max(0.0, min(100.0, score)), 1)
    return SeverityAssessment(
        computed=_band(score),
        score=score,
        function_version=SEVERITY_FUNCTION_VERSION,
        agency_asserted=agency_severity,
    )


def _band(score: float) -> str:
    for floor, label in _BANDS:
        if score >= floor:
            return label
    return "minor"


def derive_duration(
    event_class: str,
    agency_duration_minutes: int | None,
    observed_minutes: int | None = None,
) -> ExpectedDuration:
    """An estimate plus bounds plus the basis it came from.

    ``basis`` is the load-bearing field. ``agency_stated`` and ``class_prior`` are
    very different kinds of claim, and a consumer deciding whether to trust an
    end time needs to know which one it is looking at - so the basis travels with
    the estimate rather than being inferable from whether the numbers look round.

    ``observed_minutes`` is start-to-end from the source's own timestamps. It wins
    when present, because a stated end time is a stronger statement than a stated
    duration, and it is where the ``low``/``high`` band is narrowest.
    """
    if observed_minutes is not None and observed_minutes > 0:
        return ExpectedDuration(
            estimate_minutes=observed_minutes,
            low_minutes=observed_minutes,
            high_minutes=observed_minutes,
            basis="agency_stated",
        )

    if agency_duration_minutes is not None and agency_duration_minutes > 0:
        return ExpectedDuration(
            estimate_minutes=agency_duration_minutes,
            low_minutes=max(1, round(agency_duration_minutes * _STATED_LOW_FACTOR)),
            high_minutes=round(agency_duration_minutes * _STATED_HIGH_FACTOR),
            basis="agency_stated",
        )

    low, estimate, high = _DURATION_PRIORS.get(event_class, _DEFAULT_PRIOR)
    return ExpectedDuration(
        estimate_minutes=estimate,
        low_minutes=low,
        high_minutes=high,
        basis="class_prior",
    )

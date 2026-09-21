"""Cross-agency matching.

This is the third of the four problems this pipeline targets: "dedup across
agencies in real time." It runs
AFTER adapters and BEFORE lifecycle, which is why it is here rather than in an
adapter - the adapter contract forbids deduplicating there, and ``CandidateEvent``
has no field with which to express a merge.

The whole operation is ARITHMETIC, not spatial. Once conflation has put every
candidate on the corridor as a measure range, "are these the same event?" reduces
to range overlap plus time overlap plus class equality. ADR 0002
§ The split, and why makes this claim; this file is where it either holds
or does not.

Every decision is explainable. A merge that cannot say WHY it merged is
indistinguishable from a bug.

Scores in an ambiguous band go to review rather than being forced into a
merge-or-not decision. Guessing in the middle is the failure mode that makes a
dedup claim untrustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .confidence import INDEPENDENCE_GROUPS
from .lifecycle import profile_for
from .lrs import measure_overlap
from .timeutil import parse_iso
from .types import CandidateEvent

MATCH_MODEL_VERSION = "0.1.0"

# Decision bands. Deliberately declared as constants rather than buried in
# comparisons so they can be argued about and re-set without reading the algorithm -
# the same reasoning as the confidence weights. UNCALIBRATED: these are reasoned
# starting values, not fitted ones, and calibrating them needs labelled pairs.
#
# At or above MERGE_THRESHOLD: merge automatically.
# Between REVIEW and MERGE: ambiguous, route to a human.
MERGE_THRESHOLD = 0.75
REVIEW_THRESHOLD = 0.5

WEIGHTS = {
    "spatial_overlap": 0.4,
    "temporal_overlap": 0.25,
    "class_agreement": 0.2,
    "direction_agreement": 0.1,
    "independence": 0.05,
}

# Near-miss tolerance, miles. Two agencies locating one crash a few tenths apart
# is normal; this is what stops that from reading as two events.
SPATIAL_TOLERANCE_MILES = 0.5

# Beyond this gap the spatial score is zero rather than decaying further.
NEAR_MISS_DECAY_MILES = 5.0

# A point event for matching purposes. Below this length, overlap alone answers it.
POINT_EVENT_MILES = 0.1

# Stand-in for "no end time", i.e. still running. Far enough out that it
# always wins a min() against a real end date, without reaching datetime.max, whose
# arithmetic overflows.
_STILL_RUNNING = datetime(2999, 12, 31, tzinfo=timezone.utc)


@dataclass
class MatchComponents:
    """The components, not just the verdict."""

    spatial_overlap: float
    temporal_overlap: float
    class_agreement: float
    direction_agreement: float
    independence: float


@dataclass
class MatchScore:
    value: float
    decision: str  # merge | review | distinct
    components: MatchComponents
    # Human-readable, for the API and any provenance panel.
    explanation: list[str]
    model_version: str = MATCH_MODEL_VERSION


def _score_spatial(a: CandidateEvent, b: CandidateEvent) -> float:
    """Spatial component: fraction of the SHORTER extent covered by the overlap.

    Using the shorter extent is deliberate. A 200-mile weather polygon overlapping
    a 2-mile work zone covers 100% of the work zone but 1% of the alert; scoring by
    the longer one would call that a near-miss, and scoring symmetrically would
    split the difference. Neither is right - containment IS the signal, and the
    class check below is what stops a contained-but-unrelated pair from merging.
    """
    result = measure_overlap(
        (a.extent.begin_measure, a.extent.end_measure),
        (b.extent.begin_measure, b.extent.end_measure),
        SPATIAL_TOLERANCE_MILES,
    )
    if not result.overlaps:
        # Near misses decay rather than dropping to zero: a half-mile gap between
        # two reports of the same crash is well within agency location error.
        return max(0.0, 1 - result.gap_miles / NEAR_MISS_DECAY_MILES) * 0.3

    a_length = abs(a.extent.end_measure - a.extent.begin_measure)
    b_length = abs(b.extent.end_measure - b.extent.begin_measure)
    shorter = min(a_length, b_length)
    # Both are point events (or nearly): the overlap test alone is the answer.
    if shorter < POINT_EVENT_MILES:
        return 1.0
    return round(min(1.0, max(0.0, result.overlap_miles) / shorter), 4)


def _score_temporal(a: CandidateEvent, b: CandidateEvent) -> float:
    """Temporal component, with the reopen window from the lifecycle profile as the
    tolerance - so a three-year work zone and a twenty-minute crash are not held to
    the same standard of simultaneity.
    """
    a_start = parse_iso(a.start_time)
    b_start = parse_iso(b.start_time)
    if a_start is None or b_start is None:
        return 0.0

    # An open-ended event is treated as STILL RUNNING, which is what `None` means
    # - not as a zero-length event. A far-future sentinel expresses that
    # without special-casing each combination of open ends.
    #
    # An unparseable end time is also treated as open rather than as absent: the
    # adapter already recorded it as a mapping issue, and assuming an event
    # ended because we could not read its end date is the wrong failure.
    a_end = parse_iso(a.end_time) or _STILL_RUNNING if a.end_time else _STILL_RUNNING
    b_end = parse_iso(b.end_time) or _STILL_RUNNING if b.end_time else _STILL_RUNNING

    overlap_seconds = (min(a_end, b_end) - max(a_start, b_start)).total_seconds()
    if overlap_seconds >= 0:
        return 1.0

    window_seconds = profile_for(a.event_class).reopen_window_seconds
    return round(max(0.0, 1 + overlap_seconds / window_seconds), 4)


# Class agreement. Same class is the ordinary case; the interesting part is the
# pairs that are RELATED but must never merge.
#
# A weather alert and the icy road surface it causes are causally linked and
# spatially identical, but they are different facts with different lifetimes -
# merging them would make one disappear when the other cleared. They become
# `related_event_ids`, not one event. Same for a closure and the incident that
# caused it: the closure outlives the crash.
RELATED_NOT_SAME = (
    ("weather", "road_surface"),
    ("incident", "closure"),
    ("incident", "congestion"),
    ("work_zone", "closure"),
)


def _score_class(a: CandidateEvent, b: CandidateEvent) -> float:
    if a.event_class == b.event_class:
        return 1.0
    pair = {a.event_class, b.event_class}
    related = any(pair == set(known) for known in RELATED_NOT_SAME)
    # Scored low, not zero: the pair is worth surfacing as a relationship, and a
    # zero here would hide it entirely.
    return 0.15 if related else 0.0


def _score_direction(a: CandidateEvent, b: CandidateEvent) -> float:
    """Direction agreement. UNKNOWN is not a mismatch - AZ511 alone has twelve
    spellings of direction, most of which resolve to UNKNOWN, so treating unknown as
    disagreement would suppress most real Arizona matches.

    Opposing directions ARE a real distinction: the two Oklahoma work zones in the
    live feed are the same project reported once per direction, and they are
    genuinely two events for a truck travelling one way.
    """
    x = a.extent.direction
    y = b.extent.direction
    if x == y:
        return 1.0
    if x == "UNKNOWN" or y == "UNKNOWN":
        return 0.6
    if x == "BOTH" or y == "BOTH":
        return 0.8
    return 0.0  # EB vs WB


def _score_independence(a: CandidateEvent, b: CandidateEvent) -> float:
    """Two records from the SAME agency are usually that agency
    reporting one situation twice, which is a different problem from two agencies
    independently observing it. Cross-agency agreement is the stronger signal and
    the one the deck cares about, so it scores higher.
    """
    group_a = INDEPENDENCE_GROUPS.get(a.source.source_id, a.source.source_id)
    group_b = INDEPENDENCE_GROUPS.get(b.source.source_id, b.source.source_id)
    return 0.3 if group_a == group_b else 1.0


def score_match(a: CandidateEvent, b: CandidateEvent) -> MatchScore:
    components = MatchComponents(
        spatial_overlap=_score_spatial(a, b),
        temporal_overlap=_score_temporal(a, b),
        class_agreement=_score_class(a, b),
        direction_agreement=_score_direction(a, b),
        independence=_score_independence(a, b),
    )

    weighted = sum(
        weight * getattr(components, name) for name, weight in WEIGHTS.items()
    )

    # Four of the five components are NECESSARY conditions, not contributions.
    #
    # This is the part of this file that matters most. Treating them as weights
    # alone means a high score can be assembled from agreement on everything EXCEPT
    # the thing that decides identity: two events 800 miles apart, same class, same
    # time, same direction, score 0.60 and land in the review queue; the same
    # location five months apart scores exactly 0.75 and MERGES. Both are absurd,
    # and both are what a pure weighted sum produces whenever one strong
    # disagreement is outvoted by several weak agreements.
    #
    # So: two reports describe one event only if they are in the same place, at the
    # same time, about the same kind of thing, on the same side of the road. Fail
    # any of those and no amount of agreement elsewhere rescues the match. The
    # weighted sum still does the useful work of RANKING the matches that pass.
    gates = (
        (
            components.spatial_overlap > 0,
            "no measured proximity: beyond the near-miss tolerance",
        ),
        (
            components.temporal_overlap > 0,
            "no temporal overlap: outside the reopen window for this class",
        ),
        (
            components.class_agreement == 1,
            "class mismatch: related, not the same event",
        ),
        (
            components.direction_agreement > 0,
            "opposing directions: same road, opposite carriageway",
        ),
    )
    failed = [reason for ok, reason in gates if not ok]
    value = min(weighted, REVIEW_THRESHOLD - 0.01) if failed else weighted

    rounded = round(max(0.0, min(1.0, value)), 4)
    if rounded >= MERGE_THRESHOLD:
        decision = "merge"
    elif rounded >= REVIEW_THRESHOLD:
        decision = "review"
    else:
        decision = "distinct"

    return MatchScore(
        value=rounded,
        decision=decision,
        components=components,
        explanation=_explain_match(components, rounded, decision, failed),
    )


def _explain_match(
    components: MatchComponents,
    value: float,
    decision: str,
    failed_gates: list[str],
) -> list[str]:
    labels = {
        "spatial_overlap": "measure-range overlap",
        "temporal_overlap": "time-window overlap",
        "class_agreement": "event class agreement",
        "direction_agreement": "direction agreement",
        "independence": "source independence",
    }
    lines = []
    for name, label in labels.items():
        raw = getattr(components, name)
        lines.append(
            f"{label}: {raw:.2f} x weight {WEIGHTS[name]} = {WEIGHTS[name] * raw:.3f}"
        )
    for reason in failed_gates:
        lines.append(f"GATE FAILED - {reason}; capped below the merge threshold")
    lines.append(f"total: {value:.4f} -> {decision} (model {MATCH_MODEL_VERSION})")
    return lines


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


@dataclass
class MatchPair:
    from_index: int
    to_index: int
    score: MatchScore


@dataclass
class MatchCluster:
    """A set of candidates the matcher believes describe one real-world event.

    ``len(members) > 1`` is a demonstrated cross-agency merge.
    """

    # Indices into the input list, so callers can map back to their own data.
    members: list[int]
    # Why each member after the first joined, for explainability.
    joins: list[MatchPair] = field(default_factory=list)
    # Pairs that landed in the ambiguous band - NOT merged.
    review_pairs: list[MatchPair] = field(default_factory=list)


def cluster_candidates(candidates: list[CandidateEvent]) -> list[MatchCluster]:
    """Single-link agglomerative clustering over the merge threshold.

    WHY SINGLE-LINK, AND WHERE IT BREAKS: transitivity is assumed - if A merges
    with B and B with C, all three become one event even if A and C were never
    compared favourably. For a corridor that is usually right (three agencies
    reporting one crash) but it can chain a long work zone into a neighbouring one
    through a shared middle report. A real resolver should compare against the
    MERGED extent rather than pairwise, and should persist merge parents so a bad
    merge is reversible (bitemporal history makes that possible).

    Recorded rather than fixed: it is a live design decision, and an O(n^2) pairwise
    pass over corridor-scale volume is not the bottleneck worth optimizing first.
    """
    parent = list(range(len(candidates)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]  # path halving
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        root_i, root_j = find(i), find(j)
        if root_i != root_j:
            parent[root_j] = root_i

    joins: list[MatchPair] = []
    review_pairs: list[MatchPair] = []

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            score = score_match(candidates[i], candidates[j])
            if score.decision == "merge":
                joins.append(MatchPair(i, j, score))
                union(i, j)
            elif score.decision == "review":
                review_pairs.append(MatchPair(i, j, score))

    by_root: dict[int, MatchCluster] = {}
    for i in range(len(candidates)):
        root = find(i)
        if root in by_root:
            by_root[root].members.append(i)
        else:
            by_root[root] = MatchCluster(members=[i])

    for join in joins:
        by_root[find(join.from_index)].joins.append(join)

    # A review pair may span two clusters; record it on both so neither side of an
    # ambiguous decision is invisible to whoever works the queue.
    for pair in review_pairs:
        from_root = find(pair.from_index)
        by_root[from_root].review_pairs.append(pair)
        to_root = find(pair.to_index)
        if to_root != from_root:
            by_root[to_root].review_pairs.append(pair)

    return sorted(
        by_root.values(),
        key=lambda cluster: min(
            candidates[i].extent.begin_measure for i in cluster.members
        ),
    )


def best_match(
    candidate: CandidateEvent, others: list[CandidateEvent]
) -> MatchPair | None:
    """The strongest match for one candidate, or None if nothing reaches review.

    Useful for the incremental path: a new candidate arriving against events
    already in the store, which is what the resolver actually does.
    """
    best: MatchPair | None = None
    for index, other in enumerate(others):
        score = score_match(candidate, other)
        if score.decision == "distinct":
            continue
        if best is None or score.value > best.score.value:
            best = MatchPair(from_index=-1, to_index=index, score=score)
    return best

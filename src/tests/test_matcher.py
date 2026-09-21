"""Matcher tests - the false-positive cases are the point.

A dedup system is easy to make look good: merge aggressively and the demo shows
lots of cross-agency merges. The tests that matter are the ones proving it does NOT
merge things that merely look similar, because a wrong merge destroys two true
events to create one false one, and it does so silently.

"The quality claim hollows out" is one of the quiet failure modes named in
README.md § Not built yet. These are the assertions that
keep it honest.
"""

from __future__ import annotations

import pytest

from conftest import load_fixture
from corridor_event_hub.adapters.adapter import AdapterContext
from corridor_event_hub.adapters.ok_odot_wzdx import OkOdotWzdxAdapter
from corridor_event_hub.adapters.registry import registered_source_ids
from corridor_event_hub.core.confidence import INDEPENDENCE_GROUPS, SEED_SOURCE_RELIABILITY
from corridor_event_hub.core.matcher import (
    MATCH_MODEL_VERSION,
    MERGE_THRESHOLD,
    REVIEW_THRESHOLD,
    cluster_candidates,
    score_match,
)
from corridor_event_hub.core.types import CandidateEvent, Extent, SourceRef

_UNSET = object()


def candidate(
    *,
    source_id: str,
    begin: float,
    end: float,
    event_class: str = "incident",
    direction: str = "EB",
    start_time: str = "2026-08-10T12:00:00.000Z",
    end_time=_UNSET,
) -> CandidateEvent:
    """A minimal candidate.

    Fields the matcher does not read are filled with values that would be obviously
    wrong if it ever started reading them - 'TEST-ROUTE' is not a corridor.
    """
    resolved_end: str | None = (
        "2026-08-10T14:00:00.000Z" if end_time is _UNSET else end_time
    )
    return CandidateEvent(
        event_class=event_class,
        event_subtype="crash",
        extent=Extent(
            route="TEST-ROUTE",
            begin_measure=begin,
            end_measure=end,
            direction=direction,
            states=["OK"],
            geometry=None,
            positional_accuracy_meters=100,
            conflation_method="coordinate",
        ),
        lane_impacts=[],
        start_time=start_time,
        end_time=resolved_end,
        time_confidence="observed",
        agency_severity=None,
        agency_duration_minutes=None,
        source=SourceRef(
            source_id=source_id,
            agency=source_id,
            native_id=f"{source_id}-1",
            retrieved_at="2026-08-10T12:00:00.000Z",
            source_updated_at=None,
            contributed_fields=[],
            raw_ref="s3://amzn-s3-demo-rawzone/not-real",
        ),
        extensions={},
        mapping_issues=[],
    )


class TestCrossAgencyMerge:
    """cross-agency merge"""

    def test_merges_two_agencies_reporting_the_same_crash(self):
        a = candidate(source_id="az511-events", begin=100, end=100.5)
        b = candidate(source_id="ok-odot-wzdx", begin=100.2, end=100.7)

        score = score_match(a, b)
        assert score.decision == "merge"
        assert score.value >= MERGE_THRESHOLD

    def test_publishes_a_breakdown_and_a_model_version_never_a_bare_number(self):
        score = score_match(
            candidate(source_id="az511-events", begin=100, end=100.5),
            candidate(source_id="ok-odot-wzdx", begin=100.2, end=100.7),
        )

        assert score.model_version == MATCH_MODEL_VERSION
        assert len(vars(score.components)) == 5
        # Every component must contribute a line to the explanation - an unexplained
        # component is one a consumer cannot reason about. Asserting on the
        # count rather than on prose keeps this from breaking when labels are
        # reworded, while still failing if a component stops being explained.
        component_lines = [line for line in score.explanation if "x weight" in line]
        assert len(component_lines) == len(vars(score.components))
        assert "merge" in score.explanation[-1]

    def test_scores_cross_agency_agreement_above_same_agency_repetition(self):
        cross_agency = score_match(
            candidate(source_id="az511-events", begin=100, end=100.5),
            candidate(source_id="ok-odot-wzdx", begin=100, end=100.5),
        )
        same_agency = score_match(
            candidate(source_id="az511-events", begin=100, end=100.5),
            candidate(source_id="az511-events", begin=100, end=100.5),
        )
        assert cross_agency.value > same_agency.value


class TestTheMergesThatMustNotHappen:
    def test_does_not_merge_a_weather_alert_with_the_road_surface_it_caused(self):
        # Identical extent and time. Every signal except class says "same event".
        # They are related facts with different lifetimes: merging makes one vanish
        # when the other clears.
        weather = candidate(
            source_id="nws-alerts",
            event_class="weather",
            begin=900,
            end=1100,
            direction="BOTH",
        )
        surface = candidate(
            source_id="nws-alerts",
            event_class="road_surface",
            begin=900,
            end=1100,
            direction="BOTH",
        )

        score = score_match(weather, surface)
        assert score.decision != "merge"
        explanation = "\n".join(score.explanation)
        assert "class mismatch" in explanation
        assert "GATE FAILED" in explanation

    def test_does_not_merge_opposing_directions_of_the_same_work_zone(self):
        # The live Oklahoma feed does exactly this: one project, two records, one
        # per direction. For a truck travelling east, the westbound closure is not
        # its problem.
        eastbound = candidate(
            source_id="ok-odot-wzdx",
            event_class="work_zone",
            begin=956.9,
            end=960.3,
            direction="EB",
        )
        westbound = candidate(
            source_id="ok-odot-wzdx",
            event_class="work_zone",
            begin=957.5,
            end=960.2,
            direction="WB",
        )

        score = score_match(eastbound, westbound)
        assert score.decision != "merge"
        explanation = "\n".join(score.explanation)
        assert "opposing directions" in explanation
        assert "GATE FAILED" in explanation

    def test_does_not_merge_a_long_alert_that_merely_contains_a_work_zone(self):
        # The containment trap: a 200-mile alert covers 100% of a 2-mile work zone.
        alert = candidate(
            source_id="nws-alerts",
            event_class="weather",
            begin=900,
            end=1100,
            direction="BOTH",
        )
        zone = candidate(
            source_id="ok-odot-wzdx",
            event_class="work_zone",
            begin=1000,
            end=1002,
            direction="BOTH",
        )
        assert score_match(alert, zone).decision != "merge"

    def test_does_not_merge_events_far_apart_on_the_corridor(self):
        a = candidate(source_id="az511-events", begin=100, end=101)
        b = candidate(source_id="ok-odot-wzdx", begin=900, end=901)
        assert score_match(a, b).decision == "distinct"

    def test_does_not_merge_the_same_location_months_apart(self):
        spring = candidate(
            source_id="az511-events",
            begin=100,
            end=101,
            start_time="2026-03-01T12:00:00.000Z",
            end_time="2026-03-01T14:00:00.000Z",
        )
        summer = candidate(
            source_id="ok-odot-wzdx",
            begin=100,
            end=101,
            start_time="2026-08-01T12:00:00.000Z",
            end_time="2026-08-01T14:00:00.000Z",
        )
        assert score_match(spring, summer).decision != "merge"

    def test_treats_two_open_ended_events_as_temporally_overlapping(self):
        # `end_time: None` means STILL RUNNING, not zero-length. Two
        # open-ended reports of one work zone must not fail the temporal gate.
        a = candidate(
            source_id="az511-events", event_class="work_zone", begin=100, end=101, end_time=None
        )
        b = candidate(
            source_id="ok-odot-wzdx",
            event_class="work_zone",
            begin=100.2,
            end=101.2,
            end_time=None,
        )
        assert score_match(a, b).components.temporal_overlap == 1.0
        assert score_match(a, b).decision == "merge"


class TestAmbiguousBandGoesToReview:
    """ambiguous band goes to review, not to a guess"""

    def test_routes_a_near_miss_pair_to_review(self):
        # Same class, same direction, overlapping time, but 2 miles apart - inside
        # agency location error for a crash, outside a confident merge.
        a = candidate(source_id="az511-events", begin=100, end=100.2)
        b = candidate(source_id="ok-odot-wzdx", begin=102, end=102.2)

        score = score_match(a, b)
        assert score.decision == "review"
        assert REVIEW_THRESHOLD <= score.value < MERGE_THRESHOLD

    def test_surfaces_review_pairs_on_both_clusters(self):
        a = candidate(source_id="az511-events", begin=100, end=100.2)
        b = candidate(source_id="ok-odot-wzdx", begin=102, end=102.2)

        clusters = cluster_candidates([a, b])
        assert len(clusters) == 2
        assert len(clusters[0].review_pairs) == 1
        assert len(clusters[1].review_pairs) == 1


class TestClustering:
    def test_collapses_three_agencies_reporting_one_crash(self):
        clusters = cluster_candidates(
            [
                candidate(source_id="az511-events", begin=100, end=100.5),
                candidate(source_id="ok-odot-wzdx", begin=100.1, end=100.6),
                candidate(source_id="tx-dot-wzdx", begin=100.2, end=100.7),
            ]
        )
        assert len(clusters) == 1
        assert len(clusters[0].members) == 3
        # A merge must be able to say why it happened.
        assert clusters[0].joins

    def test_keeps_unrelated_events_in_separate_clusters(self):
        clusters = cluster_candidates(
            [
                candidate(source_id="az511-events", begin=100, end=101),
                candidate(source_id="ok-odot-wzdx", begin=900, end=901),
            ]
        )
        assert len(clusters) == 2

    def test_returns_clusters_ordered_west_to_east(self):
        candidates = [
            candidate(source_id="az511-events", begin=900, end=901),
            candidate(source_id="ok-odot-wzdx", begin=100, end=101),
            candidate(source_id="tx-dot-wzdx", begin=500, end=501),
        ]
        clusters = cluster_candidates(candidates)
        firsts = [
            min(candidates[i].extent.begin_measure for i in cluster.members)
            for cluster in clusters
        ]
        assert firsts == sorted(firsts)

    def test_handles_an_empty_input(self):
        assert cluster_candidates([]) == []

    def test_is_symmetric_scoring_order_does_not_change_the_verdict(self):
        a = candidate(source_id="az511-events", begin=100, end=100.5)
        b = candidate(source_id="ok-odot-wzdx", begin=100.2, end=100.7)
        assert score_match(a, b).value == score_match(b, a).value


class TestTheConfidenceModelKnowsEverySource:
    """Found by inspecting a rendered breakdown, not by a test: AZ511 events were
    scoring `source reliability: 0.50` - the default fallback - because the adapter
    was registered without anyone adding it to the seed tables. Nothing failed. The
    events just quietly scored as an unknown-quality source.

    Onboarding a source is meant to be "an adapter module plus a catalog entry plus
    one line in the registry". That is true for INGEST and silently untrue for
    SCORING, so the guard belongs here.
    """

    @pytest.mark.parametrize("source_id", registered_source_ids())
    def test_has_a_seed_reliability_and_an_independence_group(self, source_id):
        assert source_id in SEED_SOURCE_RELIABILITY, (
            f"{source_id} missing seedReliability in config/sources.json"
        )
        assert source_id in INDEPENDENCE_GROUPS, (
            f"{source_id} missing independenceGroup in config/sources.json"
        )

    def test_gives_each_agency_its_own_independence_group(self):
        # Two feeds sharing a group are treated as ONE source for corroboration
        #. That is correct for a shared upstream and wrong for two genuinely
        # separate agencies, so collisions must be deliberate.
        ids = registered_source_ids()
        groups = [INDEPENDENCE_GROUPS[i] for i in ids]
        assert len(set(groups)) == len(ids)


class TestAgainstTheRealOklahomaPayload:
    def test_keeps_the_two_live_work_zones_separate_because_they_oppose(self, conflator):
        # The regression that matters: these two records are the same project and
        # overlap almost exactly. Anything that merged them would be reporting a
        # westbound closure to an eastbound truck.
        result = OkOdotWzdxAdapter().parse(
            load_fixture("ok-odot-wzdx.json"),
            AdapterContext(
                conflator=conflator,
                raw_ref="s3://amzn-s3-demo-rawzone/ok",
                retrieved_at="2026-08-10T12:00:00.000Z",
            ),
        )

        # SCOPED to the opposing pair rather than to the whole payload.
        #
        # This used to assert the payload produced exactly 2 candidates, which was
        # true only because the placeholder centerline discarded the other three as
        # off-corridor. Real geometry places all 5, so the count is no longer the
        # thing worth pinning - the non-merge is.
        assert len(result.candidates) == 5

        pair = [c for c in result.candidates
                if c.extensions["wzdx_description"] == "GRADE, DRAIN, BRIDGE AND SURFACE"]
        assert len(pair) == 2
        # Same project, near-identical extent, opposite directions.
        assert {c.extent.direction for c in pair} == {"EB", "WB"}
        assert abs(pair[0].extent.begin_measure - pair[1].extent.begin_measure) < 1.0
        assert len(cluster_candidates(pair)) == 2

        # And across the full payload nothing opposing is merged. MatchCluster
        # carries INDICES into the input list, not candidates.
        for cluster in cluster_candidates(result.candidates):
            directions = {result.candidates[i].extent.direction for i in cluster.members}
            assert len(directions) == 1, (
                f"cluster {cluster.members} merged opposing directions {directions}"
            )

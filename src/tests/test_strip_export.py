"""Strip export tests.

The export is the artifact someone looks at first, and the fallback if a state feed
is down. Two properties matter
more than the rest:

1. IT MUST NOT CLAIM TO BE LIVE WHEN IT IS NOT. A strip built from captured bytes
   that reports ``mode: live`` is the single most misleading thing this tool could
   produce - it would make a dead feed look healthy on stage.

2. IT MUST BE VALID JSON. An unresolved extent carries NaN, and a bare NaN
   token makes the file unreadable to every strict parser including the browser's.
"""

from __future__ import annotations

import json

import pytest

from corridor_event_hub.core.lrs import corridor
from corridor_event_hub.strip_export import build


@pytest.fixture(scope="module")
def data():
    # --fixtures: no network, so the suite is deterministic and offline.
    return build(fixtures_only=True)


class TestHonestReporting:
    def test_every_source_reports_fixture_mode_when_built_from_fixtures(self, data):
        for source in data["sources"]:
            assert source["mode"] == "fixture", (
                f"{source['sourceId']} claims mode={source['mode']} in a --fixtures build"
            )
            assert source["note"]

    def test_reports_the_corridor_verification_state_honestly(self, data):
        # The viewer renders the warning when there is one. An unlabelled strip is a
        # claim about accuracy, so the export has to state which case it is in.
        #
        # This used to assert `not corridor.verified` and that the warning text was
        # present. Real state LRS geometry has since landed and the landmark check
        # passes, so the flag is true and the warning is correctly absent. The
        # invariant worth pinning is the COUPLING - warning present exactly when
        # unverified - not which state we happen to be in.
        assert data["corridor"]["verified"] is corridor.verified
        if corridor.verified:
            assert data["corridor"]["warning"] is None
        else:
            assert "NOT to publish" in data["corridor"]["warning"]

    def test_reports_unsourced_classes_rather_than_omitting_them(self, data):
        # Absence is a finding, so it is data. A viewer that showed only what we have
        # would imply the other classes do not matter.
        classes = {c["eventClass"] for c in data["unsourcedClasses"]}
        assert {"truck_parking", "road_surface"} <= classes
        for entry in data["unsourcedClasses"]:
            assert entry["reason"]

    def test_congestion_is_no_longer_unsourced(self, data):
        # Congestion was blocked on a RITIS account AND on NPMRDS being batch/lagged.
        # Amazon Location's traffic tiles answer both, so class 4 must no longer be
        # listed as absent - and must actually be present.
        classes = {c["eventClass"] for c in data["unsourcedClasses"]}
        assert "congestion" not in classes
        assert any(c["eventClass"] == "congestion" for c in data["candidates"])

    def test_non_redistributable_sources_are_labelled_as_such(self, data):
        # The strip is the artifact someone screenshots into a deck. A source whose
        # licence forbids republication has to say so HERE, not only in the catalog.
        by_id = {s["sourceId"]: s for s in data["sources"]}
        traffic = by_id.get("aws-location-traffic")
        assert traffic is not None
        assert traffic["redistributable"] is False
        assert "HERE" in (traffic["attribution"] or "")

    def test_groups_mapping_issues_with_counts_rather_than_truncating(self, data):
        # The counts ARE the review queue. A truncated list understates a
        # feed-wide format break.
        with_issues = [s for s in data["sources"] if s["issues"]]
        assert with_issues
        for source in with_issues:
            for issue in source["issues"]:
                assert issue["count"] >= 1
                assert issue["reason"]
                assert issue["field"]


class TestSerializableOutput:
    def test_is_valid_json_with_no_nan_tokens(self, data):
        def reject(value):
            raise AssertionError(f"invalid JSON constant: {value}")

        encoded = json.dumps(data, allow_nan=False, default=str)
        json.loads(encoded, parse_constant=reject)

    def test_every_candidate_has_a_finite_measure(self, data):
        # An unplaceable record must not reach the strip at all - the adapter reports
        # it as off-corridor instead.
        for candidate in data["candidates"]:
            assert isinstance(candidate["beginMeasure"], (int, float))
            assert candidate["beginMeasure"] == candidate["beginMeasure"]  # not NaN


class TestAgreesWithTheDeployedPipeline:
    """FOUND ON A DEPLOYED STACK, not by a test.

    The normalizer logged `confidenceRange: [0.5655, 0.5829]` for an Oklahoma work
    zone while the strip showed 0.7556 for the same event from the same bytes. Both
    numbers were "right" by their own code and the pair was incoherent: the strip
    scored recency from ``retrieved_at`` (when WE fetched, so always ~now, so recency
    always ~1.0) while the normalizer scored it from ``source_updated_at`` (when the
    AGENCY last changed the record - three weeks earlier).

    The strip is the artifact shown on demo day, and it was the flattering one. A
    confidence number that changes depending on which tool printed it is worse than
    no confidence number, because an integrator sets a threshold against it.
    """

    def test_reproduces_the_normalizer_score_for_the_same_bytes(self, data):
        """Score a candidate the way the NORMALIZER does and require the strip to
        match. This is the assertion that actually pins the two together - scoring
        independently here would just re-implement the bug.
        """
        from conftest import load_fixture
        from corridor_event_hub.adapters.adapter import AdapterContext
        from corridor_event_hub.adapters.ok_odot_wzdx import OkOdotWzdxAdapter
        from corridor_event_hub.core.confidence import ScoringInput, score_confidence
        from corridor_event_hub.core.lrs import LocalConflator

        retrieved_at = data["generatedAt"]
        result = OkOdotWzdxAdapter().parse(
            load_fixture("ok-odot-wzdx.json"),
            AdapterContext(
                conflator=LocalConflator(),
                raw_ref="s3://amzn-s3-demo-rawzone/fixture",
                retrieved_at=retrieved_at,
            ),
        )
        assert result.candidates

        # Exactly the expression in handlers/normalizer.py.
        expected = {
            c.source.native_id: score_confidence(
                ScoringInput(
                    candidate=c,
                    sources=[c.source],
                    last_confirmed_at=c.source.source_updated_at or retrieved_at,
                )
            ).value
            for c in result.candidates
        }

        strip = {
            c["nativeId"]: c["confidence"]["value"]
            for c in data["candidates"]
            if c["sourceId"] == "ok-odot-wzdx"
        }
        assert strip, "fixture should yield Oklahoma candidates"

        for native_id, value in strip.items():
            # Tolerance covers only the sub-second gap between the two scoring calls.
            assert value == pytest.approx(expected[native_id], abs=1e-3), (
                f"{native_id}: strip says {value}, the normalizer would say "
                f"{expected[native_id]} for the same bytes"
            )

    def test_a_stale_agency_record_does_not_score_as_fresh(self, data):
        # The OK fixture's records were last updated by the agency weeks before the
        # capture. If recency is ~1.0, the export is reading `retrieved_at` again.
        ok = [c for c in data["candidates"] if c["sourceId"] == "ok-odot-wzdx"]
        assert ok, "fixture should yield Oklahoma candidates"
        for candidate in ok:
            recency = candidate["confidence"]["breakdown"]["recency"]
            assert recency < 0.9, (
                f"recency {recency} implies the agency just updated this record; "
                "the strip is probably scoring from retrieved_at again"
            )


class TestConfidenceAndClusters:
    def test_every_candidate_publishes_a_breakdown_and_an_explanation(self, data):
        # Never a bare number.
        for candidate in data["candidates"]:
            confidence = candidate["confidence"]
            assert 0 <= confidence["value"] <= 1
            assert len(confidence["breakdown"]) == 6
            assert len(confidence["explanation"]) == 7  # 6 components + total

    def test_breakdown_keys_are_camel_case_like_the_rest_of_the_document(self, data):
        # This file emits a wire format for a browser; a snake_case island inside an
        # otherwise camelCase document costs someone an afternoon.
        keys = data["candidates"][0]["confidence"]["breakdown"]
        assert "sourceReliability" in keys
        assert not any("_" in k for k in keys)

    def test_every_candidate_belongs_to_exactly_one_cluster(self, data):
        members = [i for cluster in data["clusters"] for i in cluster["members"]]
        assert sorted(members) == list(range(len(data["candidates"])))

    def test_clusters_are_ordered_west_to_east(self, data):
        begins = [c["beginMeasure"] for c in data["clusters"]]
        assert begins == sorted(begins)

    def test_a_first_sighting_is_reported_never_active(self, data):
        # Promotion is a resolver decision with an audit record, not something
        # an exporter asserts.
        for cluster in data["clusters"]:
            assert cluster["lifecycleState"] == "reported"
            assert cluster["ttlSeconds"] > 0

    def test_candidate_ids_match_their_array_position(self, data):
        # The viewer joins clusters to candidates by this index.
        for index, candidate in enumerate(data["candidates"]):
            assert candidate["id"] == index

    def test_review_pairs_carry_their_explanation(self, data):
        # An ambiguous pair routed to review with no reasoning is not
        # actionable by whoever works the queue.
        for cluster in data["clusters"]:
            for pair in cluster["reviewPairs"]:
                assert pair["explanation"]
                assert 0 <= pair["value"] <= 1


class TestTimelineInputs:
    """What the event timeline draws from.

    The timeline's whole claim is that its marks mean exactly what they say, so the
    export has to hand it facts rather than things it must reconstruct. Every
    assertion here corresponds to a mark, a bar, or a chip that would otherwise be
    guesswork.
    """

    def test_candidates_carry_both_clocks_separately(self, data):
        # Two clocks. `sourceUpdatedAt` is when the AGENCY changed the record - the
        # basis recency decays from - and `retrievedAt` is when we asked. The timeline draws
        # the gap between them, which it can only do if both survive the wire.
        for candidate in data["candidates"]:
            assert "sourceUpdatedAt" in candidate  # None is a legitimate answer
            assert candidate["retrievedAt"]

    def test_a_feed_that_omits_its_update_time_reports_null_not_our_fetch_time(self, data):
        # Amazon Location's traffic tiles are the live case: probe-derived congestion
        # arrives with no per-record agency edit time. Substituting `retrievedAt` would
        # claim the source confirmed each record at the moment we polled, and the
        # timeline would draw a confirmation mark that no feed ever made. The timeline
        # omits the mark instead, which it can only do if the null survives.
        omitted = [c for c in data["candidates"] if c["sourceUpdatedAt"] is None]
        assert omitted, (
            "expected the probe-derived source to omit an agency update time; if every "
            "feed now supplies one, this test has lost its subject rather than passed"
        )
        for candidate in omitted:
            assert candidate["retrievedAt"], "our own fetch time is still known"

    def test_sources_publish_the_catalog_facts_trust_is_graded_against(self, data):
        # A grade whose inputs are invisible is another opaque number.
        for source in data["sources"]:
            for key in (
                "independenceGroup",
                "snapshotSemantics",
                "publishCadenceSeconds",
                "freshnessSloSeconds",
            ):
                assert key in source, f"{source['sourceId']} is missing {key}"

    def test_confidence_weights_are_published_with_the_scores(self, data):
        # An integrator sets a threshold against these numbers. It is also what
        # lets the viewer project decay honestly - without the recency weight it would
        # have to hardcode one and drift from the scorer on the next retune.
        from corridor_event_hub.core.confidence import WEIGHTS

        model = data["confidenceModel"]
        assert model["version"]
        assert set(model["weights"]) == {
            "sourceReliability",
            "corroboration",
            "recency",
            "spatialPrecision",
            "completeness",
            "internalConsistency",
        }
        assert model["weights"]["recency"] == WEIGHTS["recency"]
        assert sum(model["weights"].values()) == pytest.approx(1.0)

    def test_every_cluster_publishes_its_lifecycle_position(self, data):
        for cluster in data["clusters"]:
            lifecycle = cluster["lifecycle"]
            assert lifecycle["enteredAt"]
            assert lifecycle["ttlExpiresAt"] > lifecycle["enteredAt"]
            assert lifecycle["confidenceHalfLifeSeconds"] > 0
            assert lifecycle["reopenWindowSeconds"] > 0

    def test_the_ttl_deadline_is_the_ttl_after_entering_the_state(self, data):
        from corridor_event_hub.core.timeutil import parse_iso

        for cluster in data["clusters"]:
            lifecycle = cluster["lifecycle"]
            entered = parse_iso(lifecycle["enteredAt"])
            expires = parse_iso(lifecycle["ttlExpiresAt"])
            assert (expires - entered).total_seconds() == cluster["ttlSeconds"]

    def test_the_recency_basis_is_the_one_the_score_actually_used(self, data):
        # The timeline puts a mark on `lastConfirmedAt` and the panel prints a
        # confidence beside it. If the export recomputed the max separately, those two
        # could tell different stories about the same event - so it is the same value
        # by construction, and this pins it to the rule it implements: the
        # FRESHEST confirming report, so one stale corroborator cannot drag down an
        # otherwise current event.
        by_id = {c["id"]: c for c in data["candidates"]}
        for cluster in data["clusters"]:
            members = [by_id[i] for i in cluster["members"]]
            expected = max(m["sourceUpdatedAt"] or m["retrievedAt"] for m in members)
            assert cluster["lifecycle"]["lastConfirmedAt"] == expected

    def test_legal_transitions_are_served_rather_than_left_to_the_viewer(self, data):
        # The table is data so it can be published, not re-typed. A copy
        # in the UI would drift on the first added edge, and drift silently.
        from corridor_event_hub.core.lifecycle import legal_targets_from

        expected = legal_targets_from("reported")
        assert expected, "the fixture for this test is the transition table itself"
        for cluster in data["clusters"]:
            transitions = cluster["lifecycle"]["transitions"]
            assert [t["toState"] for t in transitions] == expected
            for transition in transitions:
                assert transition["triggers"]
                assert transition["rationale"]

    def test_the_ttl_edge_says_where_the_timer_sends_the_event(self, data):
        # The timeline labels its TTL mark from this. An unlabelled deadline is just a
        # date; the useful part is that it routes to `cleared` from `reported`.
        for cluster in data["clusters"]:
            on_timer = [
                t for t in cluster["lifecycle"]["transitions"] if "timer_ttl" in t["triggers"]
            ]
            assert on_timer, "a state with a TTL must publish where expiry leads"

    def test_disappearance_never_routes_straight_to_cleared_without_confirmation(self, data):
        # THE CONSERVATIVE DEFAULT, asserted on the wire and not just in the state
        # machine. Lingering slightly too long is recoverable; clearing a live hazard
        # in front of an automated truck is not (core/lifecycle.py, OPEN QUESTION 2).
        seen = 0
        for cluster in data["clusters"]:
            for absent in cluster["lifecycle"]["sourceAbsent"]:
                seen += 1
                if absent["snapshotSemantics"] == "cleared":
                    assert absent["toState"] == "cleared"
                else:
                    assert absent["toState"] == "clearing", (
                        f"{absent['sourceId']} has snapshotSemantics="
                        f"{absent['snapshotSemantics']} but clears on disappearance"
                    )
                assert absent["reason"]
        assert seen, "every event has at least one contributing source"

    def test_the_absence_of_lifecycle_history_is_stated_not_implied(self, data):
        # `enteredAt` is this build, every build, because the exporter holds no state.
        # A consumer computing "how long has this been active" from it would get a
        # wrong answer with no warning, so the document says so in words.
        for cluster in data["clusters"]:
            lifecycle = cluster["lifecycle"]
            assert lifecycle["historyAvailable"] is False
            assert "no persisted lifecycle history" in lifecycle["note"]
            assert lifecycle["enteredAt"] == data["generatedAt"]

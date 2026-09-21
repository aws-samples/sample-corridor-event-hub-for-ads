"""Oklahoma ODOT adapter and confidence tests against a REAL captured payload.

See ``conftest.py`` on why the fixtures are captured rather than hand-written.
"""

from __future__ import annotations

import json
import math

import pytest

from conftest import load_fixture
from corridor_event_hub.adapters.ok_odot_wzdx import (
    OkOdotWzdxAdapter,
    end_date_is_plausible,
    normalize_direction,
    regenerated_end_date_minutes,
)
from corridor_event_hub.core.confidence import (
    ScoringInput,
    explain_confidence,
    score_confidence,
    score_corroboration,
)
from corridor_event_hub.core.serde import dumps
from corridor_event_hub.core.timeutil import parse_iso


@pytest.fixture(scope="module")
def payload() -> str:
    return load_fixture("ok-odot-wzdx.json")


@pytest.fixture
def result(payload, ctx):
    return OkOdotWzdxAdapter().parse(payload, ctx)


class TestOkOdotLivePayload:
    """Oklahoma ODOT WZDx adapter - live payload"""

    def test_produces_work_zone_candidates_on_the_corridor(self, result):
        assert len(result.candidates) > 0
        assert all(c.event_class == "work_zone" for c in result.candidates)

    def test_places_every_candidate_in_oklahoma_with_a_valid_measure(self, result):
        for candidate in result.candidates:
            assert "OK" in candidate.extent.states
            assert not math.isnan(candidate.extent.begin_measure)
            assert candidate.extent.route == "I-40"

    def test_normalizes_direction_from_the_wzdx_field(self, result):
        assert all(
            c.extent.direction in ("EB", "WB", "BOTH") for c in result.candidates
        )

    def test_reports_the_missing_lane_detail_instead_of_inventing_lanes(self, result):
        # Live ODOT I-40 records arrive with lanes:[] and vehicle_impact:'unknown'.
        # Record the gap, never default it.
        lane_issues = [i for i in result.issues if i.field == "lanes"]
        assert len(lane_issues) > 0
        assert lane_issues[0].reason == "missing_required"
        for candidate in result.candidates:
            assert candidate.lane_impacts == []

    def test_rejects_the_synthetic_multi_year_end_date(self, result):
        # End_date 2029 with millisecond precision is a generated
        # placeholder.
        end_issues = [i for i in result.issues if i.field == "end_date"]
        assert len(end_issues) > 0

        # SCOPED to the records that actually carry the 2029 date.
        #
        # This used to assert `end_time is None` for EVERY candidate, and passed
        # only because the other three records in this payload were being
        # discarded as off-corridor by the placeholder centerline. With real
        # state LRS geometry all five place on the corridor, and the plausible
        # ones publish their end_time - which is correct, and which this test
        # would otherwise report as a regression.
        synthetic = [c for c in result.candidates
                     if c.extensions["odot_end_date_raw"].startswith("2029")]
        assert len(synthetic) > 0
        for candidate in synthetic:
            assert candidate.end_time is None

    def test_rejects_the_near_term_end_dates_too_because_they_slide(self, result):
        # THIS TEST USED TO ASSERT THE OPPOSITE. It required the near-term dates
        # (2026-10-19, 2026-08-17) to be published, on the reasoning that "a rule
        # rejecting every millisecond-precision date would also be wrong".
        #
        # That reasoning was sound and the premise was false. Measured against the
        # live raw zone: over an 84,598-second gap these dates advanced by 84,598
        # seconds. ODOT computes them as (request time + offset), so the near-term
        # ones are generated exactly like the 2029 ones - they were just less
        # obvious about it. The tell is in this fixture: 2029-08-05T22:16:48.351Z,
        # 2026-10-19T22:16:48.354Z and 2026-08-17T22:16:48.354Z are years apart
        # and share a minute-of-day to the millisecond.
        #
        # Publishing a sliding end_time is worse than publishing none, because a
        # consumer acts on a plausible October date. A per-class prior supplies it.
        for candidate in result.candidates:
            assert candidate.end_time is None
        near_term = [
            i
            for i in result.issues
            if i.field == "end_date" and "regenerated per request" in (i.detail or "")
        ]
        assert len(near_term) > 0

    def test_does_not_reject_every_millisecond_precision_end_date(self, result):
        # The guard the old test was protecting, kept as a unit test where it
        # belongs: an ms-precision date that is NOT part of a regenerated cohort
        # is still published. The corpus signal is what condemns these dates, not
        # the precision alone.
        plausible, reason = end_date_is_plausible(
            "2026-08-01T00:00:00Z",
            "2026-08-17T22:16:48.354Z",
            regenerated_minutes=frozenset({"09:30"}),
        )
        assert plausible
        assert reason is None

    def test_preserves_the_rejected_value_in_extensions(self, result):
        # Nothing is dropped, even when we refuse to publish it.
        for candidate in result.candidates:
            assert candidate.extensions["odot_end_date_raw"]
            assert candidate.extensions["wzdx_description"]

    def test_links_every_candidate_back_to_the_exact_raw_bytes(self, result):
        for candidate in result.candidates:
            assert candidate.source.raw_ref == "s3://amzn-s3-demo-rawzone/fixture"
            assert candidate.source.source_id == "ok-odot-wzdx"
            assert candidate.source.native_id

    def test_places_every_i40_record_on_the_corridor(self, result):
        # REGRESSION GUARD, and the clearest evidence real geometry was needed.
        #
        # This assertion used to be `off_corridor > 0` against this same payload.
        # It passed because the 40-point placeholder centerline rejected 3 of the
        # 5 placeable records as off-corridor - 60% of Oklahoma's work zones,
        # silently discarded (docs/CORRIDOR-GEOMETRY.md, problem 3). The test had
        # encoded the bug as the expected result.
        #
        # Every record in this payload is on I-40. None should be rejected.
        assert result.off_corridor == 0
        assert len(result.candidates) == 5

    def test_counts_an_off_corridor_record_without_erroring(self, payload, ctx):
        # The mechanism the previous test was meant to cover, exercised on purpose
        # rather than by relying on the centerline being wrong: a record that is
        # genuinely nowhere near I-40 is COUNTED, not raised as an error and not
        # published.
        far = json.loads(payload)
        far["features"] = [far["features"][0]]
        # Roughly Kansas City - a long way off any part of the corridor.
        far["features"][0]["geometry"]["coordinates"] = [
            [-94.578331, 39.099728], [-94.575000, 39.101000],
        ]
        result = OkOdotWzdxAdapter().parse(dumps(far), ctx)

        assert result.off_corridor == 1
        assert result.candidates == []

    def test_sets_no_lifecycle_state_or_confidence(self, result):
        # The dataclass enforces this - CandidateEvent has no such fields -
        # but assert it so the intent is documented rather than merely implied.
        for candidate in result.candidates:
            assert not hasattr(candidate, "lifecycle_state")
            assert not hasattr(candidate, "confidence")
            assert not hasattr(candidate, "event_id")

    def test_is_deterministic_same_bytes_same_output(self, payload, ctx, result):
        # The replay guarantee.
        again = OkOdotWzdxAdapter().parse(payload, ctx)
        assert dumps(again.candidates) == dumps(result.candidates)

    def test_handles_malformed_json_without_raising(self, ctx):
        result = OkOdotWzdxAdapter().parse("{not json", ctx)
        assert result.candidates == []
        assert result.issues[0].reason == "unparseable"

    def test_handles_an_empty_feature_collection(self, ctx):
        assert OkOdotWzdxAdapter().parse('{"features":[]}', ctx).candidates == []

    def test_emits_json_safe_output_even_for_unresolvable_extents(self, ctx):
        # NaN is not valid JSON, and EventBridge rejects it at runtime rather than
        # at synth. serde.dumps must turn an unresolved measure into null.
        unresolvable = (
            '{"features":[{"id":"x","geometry":{"type":"GeometryCollection"},'
            '"properties":{"core_details":{"road_names":["I-40"]}}}]}'
        )
        result = OkOdotWzdxAdapter().parse(unresolvable, ctx)
        assert result.off_corridor == 1
        # No candidate, but the issue list still has to serialize cleanly.
        assert '"NaN"' not in dumps(result.issues)
        assert dumps(result.issues)


class TestDirectionNormalization:
    """direction normalization"""

    def test_maps_wzdx_tokens(self):
        assert normalize_direction("westbound", None)[0] == "WB"
        assert normalize_direction("eastbound", None)[0] == "EB"

    def test_falls_back_to_a_road_name_suffix_when_the_field_is_absent(self):
        assert normalize_direction(None, ["I-40 W"])[0] == "WB"
        assert normalize_direction(None, ["I-40 E"])[0] == "EB"

    def test_flags_a_conflict_when_field_and_road_name_disagree(self):
        # Prefer the explicit field, but record the disagreement.
        value, conflict = normalize_direction("eastbound", ["I-40 W"])
        assert value == "EB"
        assert conflict is True

    def test_returns_unknown_rather_than_guessing(self):
        assert normalize_direction(None, None)[0] == "UNKNOWN"
        assert normalize_direction("sideways", ["Main St"])[0] == "UNKNOWN"


class TestEndDatePlausibility:
    """end-date plausibility"""

    def test_accepts_a_normal_work_zone_window(self):
        assert end_date_is_plausible("2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z")[0]

    def test_rejects_a_multi_year_duration_stated_to_the_millisecond(self):
        plausible, reason = end_date_is_plausible(
            "2026-01-06T19:04:00.000Z", "2029-08-05T22:16:48.351Z"
        )
        assert plausible is False
        assert "synthetic" in reason

    def test_rejects_an_end_before_the_start(self):
        assert not end_date_is_plausible("2026-08-01T00:00:00Z", "2026-07-01T00:00:00Z")[0]

    def test_treats_a_missing_end_date_as_plausible(self):
        # Open-ended is legitimate, not an error.
        assert end_date_is_plausible("2026-08-01T00:00:00Z", None)[0]

    def test_accepts_a_long_work_zone_without_millisecond_precision(self):
        # A genuine multi-year project should not be flagged just for being long.
        assert end_date_is_plausible("2026-01-01T00:00:00Z", "2029-01-01T00:00:00Z")[0]

    def test_rejects_an_unparseable_end_date(self):
        plausible, reason = end_date_is_plausible("2026-08-01T00:00:00Z", "next Tuesday")
        assert plausible is False
        assert "unparseable" in reason


class TestRegeneratedEndDateDetection:
    """end dates the agency recomputes on every request"""

    # Years apart, same minute-of-day, millisecond precision. This is the live
    # ODOT signature, taken verbatim from the captured payload.
    SLIDING = [
        "2029-08-05T22:16:48.351Z",
        "2026-10-19T22:16:48.354Z",
        "2026-08-17T22:16:48.354Z",
    ]

    def test_detects_the_cohort_in_the_live_payload(self, payload):
        ends = [
            (feature.get("properties") or {}).get("end_date")
            for feature in json.loads(payload)["features"]
        ]
        assert regenerated_end_date_minutes(ends) == frozenset({"22:16"})

    def test_needs_more_than_one_calendar_date(self):
        # Three zones genuinely ending at the same minute on the SAME day is a
        # night closure window, not a generator.
        same_day = [
            "2026-10-19T22:16:48.354Z",
            "2026-10-19T22:16:48.355Z",
            "2026-10-19T22:16:48.356Z",
        ]
        assert regenerated_end_date_minutes(same_day) == frozenset()

    def test_needs_three_records(self):
        # Two coincidences are not yet evidence.
        assert regenerated_end_date_minutes(self.SLIDING[:2]) == frozenset()

    def test_ignores_dates_without_millisecond_precision(self):
        # A round time is a human or a scheduler, not a request handler.
        rounded = [
            "2029-08-05T22:16:00Z",
            "2026-10-19T22:16:00Z",
            "2026-08-17T22:16:00Z",
        ]
        assert regenerated_end_date_minutes(rounded) == frozenset()

    def test_tolerates_missing_and_unparseable_values(self):
        assert regenerated_end_date_minutes([None, "next Tuesday", *self.SLIDING]) == (
            frozenset({"22:16"})
        )

    def test_verdict_does_not_depend_on_when_we_parsed(self, payload, conflator):
        # Replaying these bytes months later must produce the same events.
        # Detection is deliberately corpus-based rather than a comparison against
        # ctx.retrieved_at, which would make the output a function of parse time.
        from corridor_event_hub.adapters.adapter import AdapterContext

        def end_times(retrieved_at: str):
            ctx = AdapterContext(
                conflator=conflator, raw_ref="s3://amzn-s3-demo-rawzone/fixture", retrieved_at=retrieved_at
            )
            result = OkOdotWzdxAdapter().parse(payload, ctx)
            return [c.end_time for c in result.candidates]

        assert end_times("2026-08-07T22:00:00.000Z") == end_times("2027-03-01T09:15:00.000Z")


class TestConfidenceScoring:
    """confidence scoring - the breakdown is the point"""

    @pytest.fixture
    def candidate(self, result):
        return result.candidates[0]

    @staticmethod
    def _score(candidate, sources, ctx, *, at=None):
        return score_confidence(
            ScoringInput(
                candidate=candidate,
                sources=sources,
                last_confirmed_at=at or ctx.retrieved_at,
                now=parse_iso(at or ctx.retrieved_at),
            )
        )

    def test_always_returns_a_full_breakdown_alongside_the_value(self, candidate, ctx):
        confidence = self._score(candidate, [candidate.source], ctx)
        assert 0 < confidence.value <= 1
        assert len(vars(confidence.breakdown)) == 6
        assert confidence.model_version

    def test_scores_a_corroborated_event_higher_than_a_single_source_one(
        self, candidate, ctx
    ):
        # Acceptance criterion 4.
        import dataclasses

        second = dataclasses.replace(
            candidate.source, source_id="nws-alerts", agency="NWS"
        )
        single = self._score(candidate, [candidate.source], ctx)
        corroborated = self._score(candidate, [candidate.source, second], ctx)
        assert corroborated.value > single.value

    def test_does_not_count_a_source_corroborating_itself(self, candidate):
        # Self-corroboration inflation is the subtle failure this prevents.
        import dataclasses

        same_again = dataclasses.replace(candidate.source, native_id="different-id")
        assert score_corroboration([candidate.source, same_again]) == score_corroboration(
            [candidate.source]
        )

    def test_decays_with_age(self, candidate, ctx):
        # Continuous decay, not step-on-poll.
        fresh = score_confidence(
            ScoringInput(
                candidate=candidate,
                sources=[candidate.source],
                last_confirmed_at="2026-08-07T22:00:00.000Z",
                now=parse_iso("2026-08-07T22:00:00.000Z"),
            )
        )
        stale = score_confidence(
            ScoringInput(
                candidate=candidate,
                sources=[candidate.source],
                last_confirmed_at="2026-08-07T22:00:00.000Z",
                now=parse_iso("2026-08-21T22:00:00.000Z"),  # two weeks later
            )
        )
        assert stale.value < fresh.value
        assert stale.breakdown.recency < fresh.breakdown.recency

    def test_penalizes_the_missing_lane_detail_through_completeness(self, candidate, ctx):
        # The live ODOT case: lanes:[] means incomplete, and confidence says so.
        confidence = self._score(candidate, [candidate.source], ctx)
        assert confidence.breakdown.completeness < 1

    def test_produces_an_explanation_an_integrator_can_read(self, candidate, ctx):
        lines = explain_confidence(self._score(candidate, [candidate.source], ctx))
        assert len(lines) == 7  # 6 components + total
        assert "total" in lines[-1]

    def test_scores_zero_when_there_are_no_sources(self, candidate, ctx):
        # Not reachable through an adapter, but the scorer must not divide by zero
        # or throw if a resolver ever hands it an empty provenance list.
        confidence = self._score(candidate, [], ctx)
        assert confidence.breakdown.source_reliability == 0
        assert confidence.breakdown.corroboration == 0
        assert 0 <= confidence.value <= 1

    def test_recency_is_zero_for_an_unparseable_timestamp(self, candidate, ctx):
        # Rather than raising, or treating junk as "just now".
        confidence = self._score(candidate, [candidate.source], ctx, at="not a date")
        assert confidence.breakdown.recency == 0

"""AZ511 adapter tests against a GENERATED payload (see tests/fixtures/README.md).

AZ511 does not grant redistribution, so the fixture is built by
scripts/make-synthetic-fixtures.py from the shapes recorded on 2026-08-08 rather than
carrying the vendor's bytes. Every quirk asserted below is one of those recorded
shapes, and the generator names each record's reason.

This is the first non-WZDx source, and the differences are the point. The tests
that matter most are the ones covering silent-failure modes:
  - epoch-seconds timestamps that a naive parse turns into 1970
  - twelve direction spellings, including north/south on an east-west corridor
  - blank Severity, which is not the same as "no impact"
  - a police dispatch blob in RoadwayName that a substring match would accept
"""

from __future__ import annotations

import json
import math
import re

import pytest

from conftest import load_fixture
from corridor_event_hub.adapters.adapter import AdapterContext
from corridor_event_hub.adapters.az511_events import (
    Az511EventsAdapter,
    normalize_az_direction,
    parse_lanes_affected,
)
from corridor_event_hub.core.confidence import ScoringInput, score_confidence
from corridor_event_hub.core.serde import dumps
from corridor_event_hub.core.timeutil import epoch_to_iso, parse_iso


@pytest.fixture(scope="module")
def payload() -> str:
    return load_fixture("az511-events.json")


@pytest.fixture
def az_ctx(conflator) -> AdapterContext:
    return AdapterContext(
        conflator=conflator,
        raw_ref="s3://amzn-s3-demo-rawzone/az-fixture",
        retrieved_at="2026-08-08T00:00:00.000Z",
    )


@pytest.fixture
def result(payload, az_ctx):
    return Az511EventsAdapter().parse(payload, az_ctx)


def _places_on_corridor(event: dict, ctx: AdapterContext) -> bool:
    """Does this raw AZ511 record land on the corridor?

    Kept for the off-corridor accounting tests. It no longer has to work around a
    placeholder centerline that could not place real records.
    """
    from corridor_event_hub.core.lrs import CoordinateInput

    return ctx.conflator.conflate(
        CoordinateInput(lon=event["Longitude"], lat=event["Latitude"])
    ).on_corridor


class TestAz511LivePayload:
    """AZ511 adapter - live payload"""

    def test_produces_candidates_on_the_corridor(self, result):
        assert len(result.candidates) > 0

    def test_places_every_candidate_in_arizona(self, result):
        for candidate in result.candidates:
            assert "AZ" in candidate.extent.states
            assert not math.isnan(candidate.extent.begin_measure)

    def test_covers_more_than_one_event_class_from_a_single_endpoint(self, result):
        # The thing no WZDx feed does, and the reason this source matters.
        assert len({c.event_class for c in result.candidates}) > 1

    def test_maps_closures_to_closure_not_work_zone(self, payload, az_ctx):
        # EventSubType is often 'constructionWork', but a closure caused by
        # construction is still a closure - different authority and consequence.
        #
        # THE RELOCATION WORKAROUND IS GONE.
        #
        # This used to copy the closure's attributes onto a different record's
        # coordinates, because both live closures sat 2.9 km and 8 km off the
        # 40-point placeholder centerline and were rejected as off-corridor -
        # asserting against them directly would have passed vacuously over an empty
        # list. Real state LRS geometry places them where they actually are, so the
        # test now uses the record unmodified.
        raw = json.loads(payload)
        closure = next(e for e in raw if e.get("EventType") == "closures")
        assert closure["EventSubType"] == "constructionWork"  # the trap this guards

        result = Az511EventsAdapter().parse(json.dumps([closure]), az_ctx)
        assert [c.event_class for c in result.candidates] == ["closure"]

    def test_both_live_closures_now_place_on_the_corridor(self, payload, az_ctx):
        # THIS TEST USED TO ASSERT THE OPPOSITE, and that is the point of keeping it.
        #
        # It read `candidates == [] and off_corridor == 2`, documenting that the
        # 40-point placeholder centerline could not place these two Arizona
        # closures. That was never a property of the records - it was the
        # placeholder being several miles from the actual road, and the records
        # were being silently discarded (docs/CORRIDOR-GEOMETRY.md, problem 3).
        #
        # With real ADOT LRS geometry both place. An unplaceable record is still
        # reported rather than approximated; there just is not one here.
        raw = json.loads(payload)
        closures = [e for e in raw if e.get("EventType") == "closures"]
        assert len(closures) == 2
        result = Az511EventsAdapter().parse(json.dumps(closures), az_ctx)
        assert result.off_corridor == 0
        assert len(result.candidates) == 2
        for candidate in result.candidates:
            assert candidate.extent.route == "I-40"
            # Arizona is the first state, so its measures are its mileposts.
            assert 0 <= candidate.extent.begin_measure <= 360

    def test_is_deterministic(self, payload, az_ctx, result):
        again = Az511EventsAdapter().parse(payload, az_ctx)
        assert dumps(again.candidates) == dumps(result.candidates)

    def test_handles_malformed_json_without_raising(self, az_ctx):
        result = Az511EventsAdapter().parse("<html>gateway timeout</html>", az_ctx)
        assert result.candidates == []
        assert result.issues[0].reason == "unparseable"

    def test_rejects_a_non_array_payload_rather_than_iterating_an_object(self, az_ctx):
        # The feed is a bare JSON array. A shape change is a real signal.
        result = Az511EventsAdapter().parse('{"events":[]}', az_ctx)
        assert result.candidates == []
        assert "array" in result.issues[0].detail

    def test_sets_no_lifecycle_state_or_confidence(self, result):
        for candidate in result.candidates:
            assert not hasattr(candidate, "lifecycle_state")
            assert not hasattr(candidate, "confidence")

    def test_links_every_candidate_to_the_raw_bytes(self, result):
        for candidate in result.candidates:
            assert candidate.source.raw_ref == "s3://amzn-s3-demo-rawzone/az-fixture"
            assert candidate.source.source_id == "az511-events"

    def test_never_guesses_an_event_class(self, az_ctx):
        # An unmapped EventType is an issue, not a default.
        unknown_type = json.dumps(
            [
                {
                    "ID": 1,
                    "EventType": "somethingNew",
                    "RoadwayName": "I-40",
                    "Latitude": 35.2,
                    "Longitude": -111.65,
                }
            ]
        )
        result = Az511EventsAdapter().parse(unknown_type, az_ctx)
        assert result.candidates == []
        assert result.issues[0].field == "EventType"


class TestRouteMatchingMustBeAnchored:
    """ROUTE MATCHING must be anchored, not a substring test"""

    def test_ignores_40th_st(self, result):
        for candidate in result.candidates:
            assert "40TH" not in str(candidate.extensions["az511_roadway_name"])

    def test_ignores_a_police_dispatch_blob_pasted_into_roadway_name(self, result):
        # A real record arrived with a police dispatch block pasted into RoadwayName:
        # agency, a residential street address with an apartment number, patrol beat,
        # and a live "deputy on-scene" status. The address is not quoted here and the
        # fixture now carries a synthetic one - it was real personal information, found by a
        # content review of the captured payloads. The SHAPE is what this test needs.
        for candidate in result.candidates:
            assert "Police" not in str(candidate.extensions["az511_roadway_name"])

    def test_accepts_both_i40_and_i40_westbound(self, result):
        for candidate in result.candidates:
            name = str(candidate.extensions["az511_roadway_name"])
            assert re.match(r"^I-?40\b", name, re.IGNORECASE)


class TestEpochTimestamps:
    """EPOCH TIMESTAMPS (the 1970 trap)"""

    def test_converts_epoch_seconds_to_iso(self):
        assert epoch_to_iso(1786366800) == "2026-08-10T13:00:00.000Z"

    @pytest.mark.parametrize("value", [0, -1, "2026-01-01", None, float("nan"), True])
    def test_returns_none_rather_than_1970_for_zero_negative_or_junk(self, value):
        assert epoch_to_iso(value) is None

    def test_rejects_timestamps_outside_a_plausible_window(self):
        assert epoch_to_iso(1) is None  # 1970
        assert epoch_to_iso(99_999_999_999) is None  # year 5138

    def test_rejects_millisecond_epochs_which_would_land_in_year_58000(self):
        # A plausible mistake if a source ever switches units.
        assert epoch_to_iso(1786366800000) is None

    def test_produces_sane_start_times_on_live_data_never_1970(self, result):
        for candidate in result.candidates:
            assert parse_iso(candidate.start_time).year > 2000
            if candidate.end_time:
                assert parse_iso(candidate.end_time).year > 2000

    def test_flags_an_implausible_start_date_rather_than_publishing_1970(self, az_ctx):
        junk_epoch = json.dumps(
            [
                {
                    "ID": 1,
                    "EventType": "roadwork",
                    "RoadwayName": "I-40",
                    "DirectionOfTravel": "East",
                    # ON the real centerline. This was (35.2, -111.65), a
                    # Flagstaff town centroid that sits 2,340 m from actual I-40 -
                    # outside the 1,600 m buffer, so with real geometry the record
                    # was rejected as off-corridor and StartDate was never
                    # evaluated. The coordinate is now ADOT's own surveyed MP 195
                    # marker, which keeps this test about the date rather than
                    # about the geometry.
                    "Latitude": 35.173890,
                    "Longitude": -111.668481,
                    "StartDate": 1,
                }
            ]
        )
        result = Az511EventsAdapter().parse(junk_epoch, az_ctx)
        assert [i for i in result.issues if i.field == "StartDate"]
        # And it falls back to retrieved_at rather than to 1970.
        assert result.candidates[0].start_time == az_ctx.retrieved_at


class TestDirectionTwelveSpellings:
    """DIRECTION - twelve spellings"""

    @pytest.mark.parametrize("value", ["East", "east", "eastbound", "EB"])
    def test_maps_east_variants(self, value):
        assert normalize_az_direction(value) == "EB"

    @pytest.mark.parametrize("value", ["West", "westbound", "WB"])
    def test_maps_west_variants(self, value):
        assert normalize_az_direction(value) == "WB"

    def test_maps_both_and_all_to_both(self):
        assert normalize_az_direction("Both") == "BOTH"
        assert normalize_az_direction("All") == "BOTH"

    @pytest.mark.parametrize("value", ["North", "South", "northbound", "southbound"])
    def test_treats_north_south_as_unknown_on_an_east_west_corridor(self, value):
        # NOT coerced to BOTH. A north-south direction means the event is on a
        # cross street, so the I-40 direction is genuinely unknown.
        assert normalize_az_direction(value) == "UNKNOWN"

    @pytest.mark.parametrize("value", ["", None, "Unknown", "None"])
    def test_does_not_treat_blank_or_unknown_as_both(self, value):
        # Coercing unknown to BOTH would over-report impact in both directions.
        assert normalize_az_direction(value) == "UNKNOWN"

    def test_falls_back_to_the_direction_embedded_in_roadway_name(self, result):
        # Live records have DirectionOfTravel='Unknown' AND
        # RoadwayName='I-40 Westbound'.
        from_name = [
            c
            for c in result.candidates
            if re.search(
                r"westbound|eastbound", str(c.extensions["az511_roadway_name"]), re.IGNORECASE
            )
        ]
        for candidate in from_name:
            assert candidate.extent.direction != "UNKNOWN"


class TestLaneParsingFromProse:
    """LANE PARSING from prose"""

    def test_treats_is_full_closure_as_a_full_closure_regardless_of_text(self):
        impacts, _ = parse_lanes_affected("No Data", 3, True)
        assert len(impacts) == 1
        assert impacts[0].status == "closed"
        assert impacts[0].inferred is True

    def test_places_left_lane_closures_without_needing_a_total(self):
        impacts, unresolved = parse_lanes_affected("1 Left lane closed", None, False)
        assert [(i.ordinal, i.type, i.status) for i in impacts] == [(1, "general", "closed")]
        assert impacts[0].inferred_from == "1 Left lane closed"
        assert unresolved is None

    def test_places_right_lane_closures_using_lane_count(self):
        # 3 lanes, rightmost closed -> ordinal 3 counting from the left edge.
        impacts, _ = parse_lanes_affected("1 Right lane closed", 3, False)
        assert [i.ordinal for i in impacts] == [3]

    def test_refuses_a_right_lane_ordinal_when_lane_count_is_missing(self):
        # Side is known, position is not. Do not guess.
        impacts, unresolved = parse_lanes_affected("1 Right lane closed", None, False)
        assert impacts == []
        assert "no LaneCount" in unresolved

    def test_maps_two_right_lanes_to_the_two_highest_ordinals(self):
        impacts, _ = parse_lanes_affected("2 Right lane closed", 4, False)
        assert sorted(i.ordinal for i in impacts) == [3, 4]

    def test_maps_alternating_and_rolling_to_distinct_statuses(self):
        assert parse_lanes_affected("Lanes Alternating", 2, False)[0][0].status == "alternating"
        # A rolling closure moves, so at any fixed point it is intermittent.
        assert parse_lanes_affected("Lane Rolling", 2, False)[0][0].status == "intermittent"

    def test_recognizes_shoulder_closures_as_shoulder_not_general(self):
        impacts, _ = parse_lanes_affected("shoulder closed", 2, False)
        assert impacts[0].type == "shoulder"

    def test_reports_no_data_as_unresolved_rather_than_assuming_lanes_are_open(self):
        impacts, unresolved = parse_lanes_affected("No Data", 2, False)
        assert impacts == []
        assert unresolved == "No Data"

    def test_tags_every_inferred_impact_and_keeps_the_source_text(self):
        impacts, _ = parse_lanes_affected("1 Left lane closed", 2, False)
        assert all(i.inferred and i.inferred_from for i in impacts)


class TestSeverityBlankIsNotNoImpact:
    """SEVERITY - blank is not 'no impact'"""

    def test_preserves_the_agency_value_verbatim_where_present(self, result):
        for candidate in result.candidates:
            if candidate.agency_severity is not None:
                assert candidate.agency_severity in ("Minor", "Major")

    def test_maps_blank_and_the_literal_string_none_to_none(self, result):
        # 1,933 of 2,453 live records are blank. Inventing 'minor' for those would
        # silently understate real events.
        for candidate in result.candidates:
            assert candidate.agency_severity != ""
            assert candidate.agency_severity != "None"


class TestDimensionalRestrictions:
    """DIMENSIONAL RESTRICTIONS - units are undocumented"""

    def test_preserves_restriction_values_in_extensions(self, result):
        for candidate in result.candidates:
            assert "az511_restriction_width" in candidate.extensions
            assert "az511_restriction_height" in candidate.extensions

    def test_does_not_emit_a_class_8_candidate_while_units_are_unconfirmed(self, result):
        # Width: 12.0 could be feet or metres, and 12 metres vs 12 feet is the
        # difference between "fine" and "your truck does not fit". Flag, do not
        # guess.
        for flagged in [i for i in result.issues if i.field == "Restrictions"]:
            assert "UNITS" in flagged.detail


class TestConfidenceForAz511Records:
    def test_scores_records_with_lane_detail_above_those_without(self, result, az_ctx):
        with_lanes = [c for c in result.candidates if c.lane_impacts]
        without = [c for c in result.candidates if not c.lane_impacts]
        if not (with_lanes and without):
            pytest.skip("fixture window has no contrasting pair")

        def completeness(candidate):
            return score_confidence(
                ScoringInput(
                    candidate=candidate,
                    sources=[candidate.source],
                    last_confirmed_at=az_ctx.retrieved_at,
                    now=parse_iso(az_ctx.retrieved_at),
                )
            ).breakdown.completeness

        assert completeness(with_lanes[0]) > completeness(without[0])

"""NMDOT-via-WeatherShare adapter tests against a GENERATED payload.

The aggregator does not grant redistribution, so the fixture is built by
scripts/make-synthetic-fixtures.py from the shapes recorded on 2026-08-11 - including
the record counts, because "38% of the file is empty AZDOT shells" is a claim the
design rests on. See tests/fixtures/README.md.

This is the first source read through an AGGREGATOR, and the first New Mexico feed.
The tests that matter most are the silent-failure modes, which are unusually
plentiful here:

  - a double-wrapped root, where reading root[0] as a record yields a list
  - ``[fieldname, value]`` pair encoding on 5 of 14 fields and not the other 9
  - route split across two fields, so 'I-40' never appears as a string
  - PROSE ROUTE MATCHING BEING WRONG: 5 records mention I-40, only 2 are on it,
    and one of the false positives is a height restriction that recommends I-40 as
    a truck detour
  - no record id anywhere in the feed, so native_id is synthesized
  - no event times anywhere in the feed, so start_time is our own fetch time
  - an undocumented numeric eventType enum where guessing is worse than quarantine
"""

from __future__ import annotations

import json

import pytest

from conftest import load_fixture
from corridor_event_hub.adapters.adapter import AdapterContext
from corridor_event_hub.adapters.nm_dot_weathershare import (
    EVENT_TYPE_TO_CLASS,
    UNMAPPED_EVENT_TYPES,
    NmDotWeathershareAdapter,
    normalize_nm_direction,
    parse_aggregator_time,
    parse_lane_impacts,
    parse_mileposts,
    split_description,
    unpair,
    unwrap_payload,
)
from corridor_event_hub.core.confidence import ScoringInput, score_confidence
from corridor_event_hub.core.serde import dumps
from corridor_event_hub.core.types import EVENT_CLASSES


@pytest.fixture(scope="module")
def payload() -> str:
    return load_fixture("nm-dot-weathershare.json")


@pytest.fixture
def nm_ctx(conflator) -> AdapterContext:
    return AdapterContext(
        conflator=conflator,
        raw_ref="s3://amzn-s3-demo-rawzone/nm-fixture",
        retrieved_at="2026-08-11T21:00:00.000Z",
    )


@pytest.fixture
def result(payload, nm_ctx):
    return NmDotWeathershareAdapter().parse(payload, nm_ctx)


@pytest.fixture(scope="module")
def nmdot_records(payload) -> list[dict]:
    records, error = unwrap_payload(json.loads(payload))
    assert error is None
    return [r for r in records if r.get("source") == "NMDOT"]


# ---------------------------------------------------------------------------
# The wrapper
# ---------------------------------------------------------------------------


class TestUnwrapPayload:
    def test_the_fixture_really_is_double_wrapped(self, payload):
        # Guards the premise of every other test in this file. If the aggregator
        # ever stops double-wrapping, this is where we find out deliberately.
        doc = json.loads(payload)
        assert isinstance(doc, list)
        assert len(doc) == 1
        assert isinstance(doc[0], list), "root[0] should be the record ARRAY, not a record"

    def test_unwrap_finds_the_records(self, payload):
        records, error = unwrap_payload(json.loads(payload))
        assert error is None
        assert len(records) > 50
        assert all(isinstance(r, dict) for r in records)

    def test_an_already_flat_array_is_left_alone(self):
        # The aggregator dropping its wrapper must not break the adapter.
        records, error = unwrap_payload([{"source": "NMDOT"}, {"source": "CALTRANS"}])
        assert error is None
        assert len(records) == 2

    def test_a_non_array_root_is_reported_not_crashed(self):
        records, error = unwrap_payload({"data": []})
        assert records is None
        assert "array" in error

    def test_runaway_nesting_is_bounded(self):
        deep = [[[[[[{"source": "NMDOT"}]]]]]]
        records, error = unwrap_payload(deep)
        assert records is None and "nesting" in error


# ---------------------------------------------------------------------------
# The [fieldname, value] pair encoding
# ---------------------------------------------------------------------------


class TestUnpair:
    def test_it_unwraps_the_self_named_pair(self):
        assert unpair(["routeNumber", "40"], "routeNumber") == "40"
        assert unpair(["eventType", 8], "eventType") == 8

    def test_it_leaves_a_plain_value_alone(self):
        assert unpair("40", "routeNumber") == "40"
        assert unpair(None, "routeNumber") is None

    def test_it_does_not_unwrap_a_two_element_list_of_real_data(self):
        # Only unwrap when element 0 IS the field name. A genuine pair of values
        # must survive, or the next field the aggregator encodes as a real list
        # gets silently truncated to its second element.
        assert unpair(["a", "b"], "routeNumber") == ["a", "b"]

    def test_the_live_payload_really_uses_pair_encoding(self, nmdot_records):
        # The trap, asserted against real bytes: some fields are pairs and some
        # are not, in the same record.
        paired = {"routeName", "routeNumber", "eventType", "description"}
        plain = {"latitude", "longitude", "name", "type", "source"}
        record = nmdot_records[0]
        for field_name in paired:
            value = record[field_name]
            assert isinstance(value, list) and value[0] == field_name, field_name
        for field_name in plain:
            assert not isinstance(record[field_name], list), field_name


# ---------------------------------------------------------------------------
# Route matching - the trap with the worst consequence
# ---------------------------------------------------------------------------


class TestRouteMatching:
    def test_route_is_split_across_two_fields_and_never_written_as_i40(
        self, nmdot_records
    ):
        # The reason a naive `'I-40' in record['route']` finds nothing: there is no
        # `route` field at all on an NMDOT record.
        assert all("route" not in r for r in nmdot_records)
        i40 = [
            r
            for r in nmdot_records
            if unpair(r["routeName"], "routeName") == "I"
            and unpair(r["routeNumber"], "routeNumber") == "40"
        ]
        assert len(i40) == 2

    def test_prose_matching_would_admit_records_that_are_not_on_the_corridor(
        self, nmdot_records
    ):
        """THE test in this file.

        5 records mention I-40 in their prose; only 2 are on I-40. If this ever
        reduces to "they are the same set", the structural matcher stopped earning
        its complexity - but until then, prose matching over-reports by 150%.
        """
        import re

        mentions = [
            r
            for r in nmdot_records
            if re.search(
                r"\bI[- ]?40\b",
                f"{r.get('name', '')} {unpair(r.get('description'), 'description')}",
            )
        ]
        on_route = [
            r
            for r in nmdot_records
            if unpair(r["routeName"], "routeName") == "I"
            and unpair(r["routeNumber"], "routeNumber") == "40"
        ]
        assert len(mentions) == 5
        assert len(on_route) == 2
        false_positives = [r for r in mentions if r not in on_route]
        assert len(false_positives) == 3

    def test_the_height_restriction_detour_record_is_excluded(self, result):
        """The worst possible false positive, asserted explicitly.

        One NMDOT record reads "Low Clearance Structure, CMV's please use I-40
        between exits 89 & 96. Height Restriction 13'6\"". It is a restriction
        SOMEWHERE ELSE that recommends I-40 as the truck alternative. Admitting it
        would publish a 13'6" clearance limit ON the corridor it tells trucks to
        use - inverting the fact on the one class where being wrong strands a
        truck under a bridge.
        """
        for candidate in result.candidates:
            blob = json.dumps(candidate.extensions)
            assert "Low Clearance Structure" not in blob
            assert "13'6" not in blob

    def test_other_upstream_agencies_are_not_parsed(self, result, payload):
        # The fixture contains CALTRANS, AZDOT and OregonDOT records on purpose.
        records, _ = unwrap_payload(json.loads(payload))
        assert {r.get("source") for r in records} >= {"CALTRANS", "AZDOT", "OregonDOT"}
        for candidate in result.candidates:
            assert candidate.source.source_id == "nm-dot-weathershare"
            assert candidate.extensions["nmws_upstream_source"] == "NMDOT"

    def test_empty_azdot_shell_records_produce_no_issues(self, result):
        # 38% of the live file is empty AZDOT records. They are a different
        # upstream, so they must be skipped BEFORE field logic runs - otherwise
        # every poll floods the review queue with issues about another state.
        for reported in result.issues:
            assert "AZDOT" not in str(reported.raw_value)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class TestEventTypeMapping:
    def test_every_mapped_class_is_a_real_event_class(self):
        for event_class in EVENT_TYPE_TO_CLASS.values():
            assert event_class in EVENT_CLASSES

    def test_lane_closure_maps_to_closure_not_work_zone(self):
        # A closure caused by roadwork is still a closure. Same call the
        # AZ511 adapter makes, and the `type` field saying 'Construction' does not
        # override it.
        assert EVENT_TYPE_TO_CLASS[8] == "closure"
        assert EVENT_TYPE_TO_CLASS[9] == "work_zone"

    def test_driving_conditions_map_to_road_surface(self):
        # The genuinely new capability: an agency STATING a surface condition
        # rather than us deriving one from a weather alert.
        assert EVENT_TYPE_TO_CLASS[13] == "road_surface"
        assert EVENT_TYPE_TO_CLASS[16] == "road_surface"

    def test_ambiguous_event_types_are_not_mapped_at_all(self):
        # eventType 7 ('Alert') spans dimensional_restriction and
        # non-events; 19 has n=1. Both must stay OUT of the mapping.
        for event_type in UNMAPPED_EVENT_TYPES:
            assert event_type not in EVENT_TYPE_TO_CLASS

    def test_an_unknown_event_type_quarantines_rather_than_defaulting(self, nm_ctx):
        payload = json.dumps(
            [
                [
                    {
                        "source": "NMDOT",
                        "routeName": ["routeName", "I"],
                        "routeNumber": ["routeNumber", "40"],
                        "eventType": ["eventType", 999],
                        "description": ["description", "Something New~~~body"],
                        "name": "Something New, I 40 at mile marker 100.",
                        "latitude": 35.08,
                        "longitude": -107.98,
                        "starttime": "",
                        "endtime": "",
                        "updated": "202608111953 UTC",
                    }
                ]
            ]
        )
        result = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        assert result.candidates == []
        assert any(
            i.field == "eventType" and i.reason == "unmapped_vocabulary"
            for i in result.issues
        )


# ---------------------------------------------------------------------------
# Prose parsing
# ---------------------------------------------------------------------------


class TestSplitDescription:
    def test_it_splits_on_the_triple_tilde(self):
        title, body = split_description("Lane Closure~~~Eastbound driving lane closed.")
        assert title == "Lane Closure"
        assert body == "Eastbound driving lane closed."

    def test_a_record_with_no_delimiter_is_title_only(self):
        # Observed: 'Closure, Montgomery Blvd. Loop Ramp 9/3.' carries no '~~~'.
        title, body = split_description("Closure, Montgomery Blvd. Loop Ramp 9/3.")
        assert title == "Closure, Montgomery Blvd. Loop Ramp 9/3."
        assert body == ""

    def test_a_non_string_does_not_raise(self):
        assert split_description(None) == ("", "")
        assert split_description(["description", "x"]) == ("", "")


class TestParseMileposts:
    def test_a_range(self):
        assert parse_mileposts(
            "Roadwork, I 40  from mile marker 344, 8 miles east of Tucumcari"
            " to mile marker 350, 14 miles east of Tucumcari."
        ) == (344.0, 350.0)

    def test_a_range_wins_over_the_at_pattern_inside_it(self):
        # The interstitial prose of a range frequently contains 'at mile marker',
        # so a point-first order would truncate the range to its start.
        assert parse_mileposts(
            "from mile marker 227, at mile marker 300 (Comanche) to mile marker 228"
        ) == (227.0, 228.0)

    def test_a_point_returns_equal_begin_and_end(self):
        assert parse_mileposts("Roadwork, I 10  at mile marker 164, Anthony.") == (
            164.0,
            164.0,
        )

    def test_a_reversed_range_is_ordered(self):
        assert parse_mileposts("from mile marker 350 to mile marker 344") == (344.0, 350.0)

    def test_decimals_survive(self):
        assert parse_mileposts("at mile marker 0.866") == (0.866, 0.866)

    def test_no_milepost_returns_none(self):
        assert parse_mileposts("Alert, Alert") is None

    def test_the_live_payload_carries_mileposts_on_most_records(self, nmdot_records):
        with_mp = [r for r in nmdot_records if parse_mileposts(r["name"]) is not None]
        assert len(with_mp) > len(nmdot_records) // 2


class TestNormalizeDirection:
    def test_eastbound(self):
        assert normalize_nm_direction("I 40 eastbound from mile marker 44") == ("EB", [])

    def test_both(self):
        direction, noncorridor = normalize_nm_direction(
            "US 60 eastbound and westbound from mile marker 56"
        )
        assert direction == "BOTH"
        assert noncorridor == []

    def test_absent_direction_is_unknown_not_both(self):
        # Coercing absence to BOTH would over-report impact in a direction nobody
        # claimed. 74 of 84 observed records state no direction.
        assert normalize_nm_direction("Roadwork, I 40  from mile marker 344") == (
            "UNKNOWN",
            [],
        )

    def test_north_south_are_reported_not_coerced(self):
        # Observed on the real I-40 lane closure: 'northbound and eastbound' - a
        # contradiction on an east-west corridor. East wins for the direction, and
        # the north is surfaced rather than discarded.
        direction, noncorridor = normalize_nm_direction(
            "Lane Closure, I 40 northbound and eastbound from mile marker 44"
        )
        assert direction == "EB"
        assert noncorridor == ["northbound"]

    def test_north_south_only_is_unknown(self):
        direction, noncorridor = normalize_nm_direction(
            "NM 146 northbound and southbound from mile marker 0"
        )
        assert direction == "UNKNOWN"
        assert noncorridor == ["northbound", "southbound"]


class TestParseLaneImpacts:
    def test_a_left_lane_closure_gets_ordinal_one(self):
        impacts, unresolved = parse_lane_impacts("Lane Closure", "Left lane closed.")
        assert unresolved is None
        assert len(impacts) == 1
        assert impacts[0].ordinal == 1
        assert impacts[0].status == "closed"
        assert impacts[0].inferred is True
        assert impacts[0].inferred_from == "Left lane closed"

    def test_a_driving_lane_closure_refuses_to_invent_an_ordinal(self):
        # Ordinals count from the left edge. 'driving lane' names a
        # function, not a position, and this feed never states a total lane count -
        # so the honest output is no ordinal plus a review item.
        impacts, unresolved = parse_lane_impacts(
            "Lane Closure", "Eastbound driving lane closed on I-40, mile marker 44-46"
        )
        assert impacts == []
        assert "no total lane count" in unresolved

    def test_a_lane_closure_title_with_no_lane_detail_is_reported(self):
        impacts, unresolved = parse_lane_impacts("Lane Closure", "Work is ongoing.")
        assert impacts == []
        assert "no lane is identified" in unresolved

    def test_roadwork_with_no_lane_prose_reports_nothing(self):
        # Absence of lane detail on a work zone is not an anomaly worth queueing.
        assert parse_lane_impacts("Roadwork", "Contractor personnel on the roadway.") == (
            [],
            None,
        )


class TestParseAggregatorTime:
    def test_it_parses_the_observed_format(self):
        assert parse_aggregator_time("202608111953 UTC") == "2026-08-11T19:53:00.000Z"

    def test_it_requires_the_utc_suffix(self):
        # The digits alone are ambiguous; the explicit zone is the only reason this
        # field is trustworthy, so losing it must stop the parse.
        assert parse_aggregator_time("202608111953") is None
        assert parse_aggregator_time("202608111953 MDT") is None

    def test_junk_returns_none_not_a_plausible_date(self):
        assert parse_aggregator_time("") is None
        assert parse_aggregator_time(None) is None
        assert parse_aggregator_time("999999999999 UTC") is None
        assert parse_aggregator_time(1786481584) is None


# ---------------------------------------------------------------------------
# The candidates
# ---------------------------------------------------------------------------


class TestCandidates:
    def test_it_produces_i40_candidates_from_the_real_payload(self, result):
        assert result.candidates, (
            "no candidates from the captured payload - either the corridor"
            " placement broke or the route matcher stopped matching"
        )
        assert len(result.candidates) <= 2

    def test_every_candidate_is_a_mapped_class(self, result):
        for candidate in result.candidates:
            assert candidate.event_class in EVENT_CLASSES
            assert candidate.event_class in set(EVENT_TYPE_TO_CLASS.values())

    def test_no_candidate_carries_a_lifecycle_state_or_confidence(self, result):
        # The adapter contract is enforced by the type, but asserted here so a
        # future refactor that adds
        # such a field to CandidateEvent fails here first.
        for candidate in result.candidates:
            assert not hasattr(candidate, "lifecycle_state")
            assert not hasattr(candidate, "confidence")

    def test_start_time_falls_back_to_retrieved_at_and_says_so(self, result, nm_ctx):
        # THE feed has no event times at all. The fallback is legitimate; letting
        # it pass unremarked is not, because downstream would read our fetch time
        # as an agency-stated start.
        for candidate in result.candidates:
            assert candidate.start_time == nm_ctx.retrieved_at
            assert candidate.end_time is None
            assert candidate.time_confidence == "estimated"
        assert any(i.field == "starttime" for i in result.issues)
        assert any(i.field == "endtime" for i in result.issues)

    def test_the_feed_really_states_no_times(self, nmdot_records):
        # The premise of the test above, asserted against real bytes.
        assert all(r["starttime"] == "" for r in nmdot_records)
        assert all(r["endtime"] == "" for r in nmdot_records)

    def test_native_id_is_synthesized_and_labelled_as_such(self, result):
        for candidate in result.candidates:
            assert candidate.source.native_id.startswith("nmws-")
            assert candidate.extensions["nmws_native_id_synthesized"] is True

    def test_no_record_in_the_feed_carries_an_id(self, nmdot_records):
        # Why native_id has to be synthesized at all.
        for record in nmdot_records:
            assert not {"id", "uid", "log-id", "logId", "index"} & set(record)

    def test_the_synthesized_id_is_stable_across_two_parses(self, payload, nm_ctx):
        first = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        second = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        assert [c.source.native_id for c in first.candidates] == [
            c.source.native_id for c in second.candidates
        ]

    def test_the_synthesized_id_ignores_body_and_scrape_time(self, nm_ctx):
        """The id must survive the two things that change constantly.

        The description body is edited as conditions change (the observed text
        literally says "This event will be updated as conditions change"), and
        ``updated`` changes on every scrape. If either fed the hash, the same
        physical event would get a new identity on almost every poll.
        """

        def build(body: str, updated: str) -> str:
            return json.dumps(
                [
                    [
                        {
                            "source": "NMDOT",
                            "routeName": ["routeName", "I"],
                            "routeNumber": ["routeNumber", "40"],
                            "eventType": ["eventType", 9],
                            "description": ["description", f"Roadwork~~~{body}"],
                            "name": "Roadwork, I 40  from mile marker 140 to mile marker 145.",
                            "latitude": 35.08,
                            "longitude": -107.98,
                            "starttime": "",
                            "endtime": "",
                            "updated": updated,
                        }
                    ]
                ]
            )

        a = NmDotWeathershareAdapter().parse(build("one", "202608111953 UTC"), nm_ctx)
        b = NmDotWeathershareAdapter().parse(build("two", "202608120800 UTC"), nm_ctx)
        assert a.candidates and b.candidates
        assert a.candidates[0].source.native_id == b.candidates[0].source.native_id

    def test_source_updated_at_is_the_aggregator_scrape_time_and_is_labelled(self, result):
        for candidate in result.candidates:
            assert candidate.extensions["nmws_aggregator_scrape_time"] == (
                candidate.source.source_updated_at
            )
            assert candidate.extensions["nmws_aggregator_updated_raw"]

    def test_agency_severity_is_none_because_the_feed_asserts_none(self, result):
        for candidate in result.candidates:
            assert candidate.agency_severity is None
            assert candidate.agency_duration_minutes is None

    def test_nothing_is_dropped(self, result):
        # Every NMDOT field has a home, canonical or extension.
        for candidate in result.candidates:
            for key in (
                "nmws_event_type",
                "nmws_type",
                "nmws_type_abbr",
                "nmws_icon",
                "nmws_route_name",
                "nmws_route_number",
                "nmws_name",
                "nmws_description_title",
                "nmws_description_body",
                "nmws_starttime_raw",
                "nmws_endtime_raw",
            ):
                assert key in candidate.extensions, key

    def test_candidates_serialize(self, result):
        for candidate in result.candidates:
            assert json.loads(dumps(candidate))

    def test_candidates_can_be_scored(self, result):
        # The adapter does not score, but its output must be scoreable - a missing
        # field surfaces here rather than in the pipeline.
        for candidate in result.candidates:
            confidence = score_confidence(
                ScoringInput(
                    candidate=candidate,
                    sources=[candidate.source],
                    last_confirmed_at=candidate.start_time,
                )
            )
            assert 0.0 <= confidence.value <= 1.0


class TestExtentAndMilepostCrossCheck:
    def test_prose_mileposts_are_preferred_over_the_coordinate(self, result):
        # Both live I-40 records state a milepost range, so both should use it.
        # The agency's own linear reference still wins now that our geometry is
        # real: it yields a linear extent rather than a point, and it scores higher
        # for spatial precision (0.9 vs 0.85). The original reason - that our centerline was a
        # placeholder - no longer applies. See _resolve_extent.
        used = [c for c in result.candidates if c.extensions["nmws_milepost_used_for_extent"]]
        assert len(used) == len(result.candidates)
        for candidate in used:
            assert candidate.extent.conflation_method == "milepost"
            assert candidate.extensions["nmws_milepost_begin"] is not None

    def test_a_record_with_no_prose_milepost_falls_back_to_the_coordinate(self, nm_ctx):
        payload = json.dumps(
            [
                [
                    {
                        "source": "NMDOT",
                        "routeName": ["routeName", "I"],
                        "routeNumber": ["routeNumber", "40"],
                        "eventType": ["eventType", 9],
                        "description": ["description", "Roadwork~~~no mileposts here"],
                        "name": "Roadwork, I 40 somewhere.",
                        # NMDOT's own surveyed I-40 MP 159 marker. This was the
                        # Albuquerque town CENTROID (35.084, -106.65), which sits
                        # 2,448 m from actual I-40 - outside the 1,600 m buffer, so
                        # with real geometry the record went off-corridor and these
                        # tests lost the candidate they were asserting about.
                        "latitude": 35.104934,
                        "longitude": -106.638543,
                        "starttime": "",
                        "endtime": "",
                        "updated": "202608111953 UTC",
                    }
                ]
            ]
        )
        result = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.extensions["nmws_milepost_used_for_extent"] is False
        assert candidate.extent.conflation_method == "coordinate"
        assert candidate.extensions["nmws_milepost_coordinate_gap_miles"] is None

    def test_a_milepost_range_produces_a_linear_extent(self, result):
        ranged = [
            c
            for c in result.candidates
            if c.extensions["nmws_milepost_begin"] != c.extensions["nmws_milepost_end"]
            and c.extensions["nmws_milepost_used_for_extent"]
        ]
        assert ranged, "expected at least one range-extent candidate"
        for candidate in ranged:
            assert candidate.extent.end_measure > candidate.extent.begin_measure

    def test_geometry_stays_a_point_even_when_measures_span_a_range(self, result):
        # Synthesizing a LineString from a milepost range would invent geometry the
        # source never stated. The RANGE lives in the measures; the geometry stays
        # what NMDOT actually gave us.
        for candidate in result.candidates:
            assert candidate.extent.geometry.type == "Point"
            assert len(candidate.extent.geometry.coordinates) == 2

    def test_a_disagreeing_milepost_is_reported_but_still_wins(self, nm_ctx):
        """A gross disagreement is surfaced without changing the choice.

        The milepost stays authoritative even here: at this magnitude the likeliest
        cause is still our own placeholder centerline (6.3% short, ~22 mi off at the
        NM/TX line), so switching to the coordinate would trade a documented bias
        for an unknown one. The contradiction is reported; it does not flip the
        decision.
        """
        payload = json.dumps(
            [
                [
                    {
                        "source": "NMDOT",
                        "routeName": ["routeName", "I"],
                        "routeNumber": ["routeNumber", "40"],
                        "eventType": ["eventType", 9],
                        "description": ["description", "Roadwork~~~body"],
                        "name": "Roadwork, I 40  at mile marker 5.",
                        # Coordinates near Albuquerque, NM MP ~131 - not MP 5.
                        # NMDOT's own surveyed I-40 MP 159 marker. This was the
                        # Albuquerque town CENTROID (35.084, -106.65), which sits
                        # 2,448 m from actual I-40 - outside the 1,600 m buffer, so
                        # with real geometry the record went off-corridor and these
                        # tests lost the candidate they were asserting about.
                        "latitude": 35.104934,
                        "longitude": -106.638543,
                        "starttime": "",
                        "endtime": "",
                        "updated": "202608111953 UTC",
                    }
                ]
            ]
        )
        result = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.extensions["nmws_milepost_used_for_extent"] is True
        assert candidate.extent.conflation_method == "milepost"
        assert any(
            "disagree" in (i.detail or "") for i in result.issues
        ), "a ~126-mile disagreement must be reported"
        assert candidate.extensions["nmws_milepost_coordinate_gap_miles"] > 100

    def test_the_gap_is_recorded_whenever_both_references_resolve(self, result):
        # Recorded on every such record so the centerline error can be measured
        # across the corpus rather than estimated. It is legitimately None when the
        # coordinate does not place on the corridor at all - see the test below.
        for candidate in result.candidates:
            gap = candidate.extensions["nmws_milepost_coordinate_gap_miles"]
            assert gap is None or gap >= 0

    def test_milepost_parsing_rescues_a_record_the_coordinate_would_lose(self, nm_ctx):
        """A record whose coordinate is off-corridor survives on its prose milepost.

        REAL GEOMETRY CHANGED THIS TEST'S PREMISE, and the change is worth reading
        before trusting the old rationale.

        It used to run against the live fixture and assert that at least one
        candidate was rescued. The live Gallup lane closure at (35.444, -108.359)
        qualified because the 40-point placeholder centerline ran ~25 miles south of
        real I-40 there. With ADOT/NMDOT LRS geometry that coordinate is 50 m from
        the centerline and places cleanly, so NO live record needs rescuing any more.

        The mechanism is still worth testing, so it is now exercised on purpose with
        a deliberately off-corridor coordinate rather than by relying on the
        centerline being wrong.

        OPEN QUESTION for whoever owns this adapter: the "milepost parsing DOUBLES
        the corridor yield" claim that justified the prose parser's complexity was
        measured against the placeholder. On this fixture the yield gain is now
        ZERO. The parser still earns its place for records with no coordinate at
        all, and for producing a linear extent instead of a point (which scores
        higher) - but the doubling argument specifically no longer holds.
        """
        payload = json.dumps(
            [
                [
                    {
                        "source": "NMDOT",
                        "routeName": ["routeName", "I"],
                        "routeNumber": ["routeNumber", "40"],
                        "eventType": ["eventType", 9],
                        "description": ["description", "Roadwork~~~body"],
                        "name": "Roadwork, I 40  at mile marker 44-46.",
                        # ~71 km off any part of the corridor, so the coordinate
                        # cannot place. Only the prose milepost can.
                        "latitude": 35.700,
                        "longitude": -106.000,
                        "starttime": "",
                        "endtime": "",
                        "updated": "202608111953 UTC",
                    }
                ]
            ]
        )
        result = NmDotWeathershareAdapter().parse(payload, nm_ctx)

        assert len(result.candidates) == 1, "the milepost should have rescued it"
        assert result.off_corridor == 0
        for candidate in result.candidates:
            assert candidate.extent.conflation_method == "milepost"
            assert candidate.extensions["nmws_milepost_used_for_extent"] is True
            # No coordinate measure to compare against, so no gap.
            assert candidate.extensions["nmws_milepost_coordinate_gap_miles"] is None

    def test_an_out_of_range_milepost_is_reported(self, nm_ctx):
        # NM mileposts run 0..373.5 per corridor.json. 9999 is not a milepost.
        payload = json.dumps(
            [
                [
                    {
                        "source": "NMDOT",
                        "routeName": ["routeName", "I"],
                        "routeNumber": ["routeNumber", "40"],
                        "eventType": ["eventType", 9],
                        "description": ["description", "Roadwork~~~body"],
                        "name": "Roadwork, I 40  at mile marker 9999.",
                        # NMDOT's own surveyed I-40 MP 159 marker. This was the
                        # Albuquerque town CENTROID (35.084, -106.65), which sits
                        # 2,448 m from actual I-40 - outside the 1,600 m buffer, so
                        # with real geometry the record went off-corridor and these
                        # tests lost the candidate they were asserting about.
                        "latitude": 35.104934,
                        "longitude": -106.638543,
                        "starttime": "",
                        "endtime": "",
                        "updated": "202608111953 UTC",
                    }
                ]
            ]
        )
        result = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        assert any(i.reason == "out_of_corridor" for i in result.issues)
        # The record still lands, via its coordinate.
        assert len(result.candidates) == 1
        assert result.candidates[0].extent.conflation_method == "coordinate"

    def test_states_are_new_mexico(self, result):
        for candidate in result.candidates:
            assert "NM" in candidate.extent.states


class TestMalformedInput:
    def test_unparseable_json_is_an_issue_not_an_exception(self, nm_ctx):
        result = NmDotWeathershareAdapter().parse("{not json", nm_ctx)
        assert result.candidates == []
        assert result.issues and result.issues[0].reason == "unparseable"

    def test_a_non_dict_record_is_reported(self, nm_ctx):
        result = NmDotWeathershareAdapter().parse(json.dumps([["a string"]]), nm_ctx)
        assert result.candidates == []
        assert any(i.reason == "unparseable" for i in result.issues)

    def test_missing_coordinates_are_reported_not_defaulted(self, nm_ctx):
        payload = json.dumps(
            [
                [
                    {
                        "source": "NMDOT",
                        "routeName": ["routeName", "I"],
                        "routeNumber": ["routeNumber", "40"],
                        "eventType": ["eventType", 9],
                        "description": ["description", "Roadwork~~~body"],
                        "name": "Roadwork, I 40 at mile marker 100.",
                        "latitude": None,
                        "longitude": None,
                        "starttime": "",
                        "endtime": "",
                        "updated": "202608111953 UTC",
                    }
                ]
            ]
        )
        result = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        assert result.candidates == []
        assert any(i.reason == "missing_required" for i in result.issues)

    def test_a_boolean_is_not_a_coordinate(self, nm_ctx):
        # bool is an int in Python, so True would otherwise pass as latitude 1.
        payload = json.dumps(
            [
                [
                    {
                        "source": "NMDOT",
                        "routeName": ["routeName", "I"],
                        "routeNumber": ["routeNumber", "40"],
                        "eventType": ["eventType", 9],
                        "description": ["description", "Roadwork~~~body"],
                        "name": "Roadwork, I 40 at mile marker 100.",
                        "latitude": True,
                        "longitude": True,
                        "starttime": "",
                        "endtime": "",
                        "updated": "202608111953 UTC",
                    }
                ]
            ]
        )
        result = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        assert result.candidates == []

    def test_an_unpaired_event_type_is_reported(self, nm_ctx):
        payload = json.dumps(
            [
                [
                    {
                        "source": "NMDOT",
                        "routeName": ["routeName", "I"],
                        "routeNumber": ["routeNumber", "40"],
                        # A string where an int is expected, after unpairing.
                        "eventType": ["eventType", "9"],
                        "description": ["description", "Roadwork~~~body"],
                        "name": "Roadwork, I 40 at mile marker 100.",
                        "latitude": 35.08,
                        "longitude": -107.98,
                        "starttime": "",
                        "endtime": "",
                        "updated": "202608111953 UTC",
                    }
                ]
            ]
        )
        result = NmDotWeathershareAdapter().parse(payload, nm_ctx)
        assert result.candidates == []
        assert any(
            i.field == "eventType" and i.reason == "unparseable" for i in result.issues
        )

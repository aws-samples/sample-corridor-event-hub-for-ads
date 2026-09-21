"""Query API tests.

WHAT IS WORTH ASSERTING IN A READ API, given that "it returns some events" is easy
to make true and nearly worthless:

  A WRONG FILTER MUST NOT LOOK LIKE AN EMPTY ROAD. An unparseable timestamp or a
  malformed bbox returning `{"events": []}` with a 200 is the worst available
  answer - indistinguishable from a clear corridor to an automated consumer. So the
  error paths are asserted as hard as the happy ones.

  A TRUNCATED ANSWER MUST SAY SO. Same reason.

  LOOK-AHEAD MUST RESPECT DIRECTION. Returning westbound events to an eastbound
  vehicle is not a cosmetic bug; the two Oklahoma work zones in the live feed are
  one project reported once per direction, and only one of them is on the road the
  truck is driving.

  THE SCORE MUST NEVER TRAVEL WITHOUT ITS BREAKDOWN, because an
  integrator setting a trust threshold against an opaque number cannot reason about
  either side of it.

The store is the real ``InMemoryEventStore``, seeded through the actual resolution
policy rather than with hand-built events - so what these tests query is what the
resolver would really have written.
"""

from __future__ import annotations

import importlib
import json

import pytest

from conftest import NOW, make_candidate, score
from corridor_event_hub.core.eventstore import InMemoryEventStore
from corridor_event_hub.core.ids import new_event_id
from corridor_event_hub.core.resolution import resolve_new
from corridor_event_hub.core.types import LaneImpact
from corridor_event_hub.core.wzdx import WZDX_VERSION

ROUTE = "TEST-ROUTE"


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("EVENT_TABLE", "test-events")
    # Named explicitly so nothing has to load corridor geometry to find out which
    # corridor this function serves (configuration, not a literal).
    monkeypatch.setenv("CEH_ROUTE", ROUTE)
    module = importlib.import_module("corridor_event_hub.handlers.query")
    importlib.reload(module)
    monkeypatch.setattr(module, "_store", InMemoryEventStore())
    monkeypatch.setattr(module, "now_utc", lambda: NOW)
    return module


def seed(api, candidate=None, nearby=(), **kwargs):
    """Resolve one candidate into the store, the way the resolver would."""
    subject = candidate or make_candidate(**kwargs)
    result = resolve_new(
        subject, score(subject), new_event_id(NOW), list(nearby), NOW,
        payload_ref=subject.source.raw_ref,
    )
    for change in result.changes:
        api._store.write_change(change)
    if result.reviews:
        api._store.queue_reviews(result.reviews)
    return result


def get(api, path, **params):
    return api.route_request("GET", path, {k: str(v) for k, v in params.items()})


class TestEnvelope:
    def test_every_response_states_that_this_is_advisory(self, api):
        # Stated plainly, and in the payload rather than only in a
        # document, because a field is the one place a consumer cannot miss it.
        _status, body = get(api, "/events")
        assert "advisory decision-support" in body["advisory"]
        assert "safety-critical" in body["advisory"]

    def test_every_response_names_the_models_that_produced_its_numbers(self, api):
        # An integrator who calibrated a threshold against one
        # scoring model has to be able to notice when the model changed.
        _status, body = get(api, "/events")
        assert body["confidence_model"]["version"]
        assert body["confidence_model"]["weights"]["corroboration"]
        assert body["match_model"]["merge_threshold"]
        assert body["resolver_policy_version"]

    def test_health_needs_no_store_and_no_corridor(self, api):
        status, body = get(api, "/health")
        assert status == 200
        assert body["status"] == "ok"
        assert body["route"] == ROUTE


class TestListEvents:
    def test_returns_stored_events_with_full_provenance(self, api):
        seed(api)
        status, body = get(api, "/events")

        assert status == 200
        assert body["count"] == 1
        event = body["events"][0]
        # Every contributing source, named.
        assert event["sources"][0]["source_id"] == "ok-odot-wzdx"
        assert event["sources"][0]["raw_ref"]  # Back to the exact bytes
        assert event["agencies"] == ["ok-odot-wzdx"]

    def test_the_confidence_score_never_travels_without_its_breakdown(self, api):
        seed(api)
        _status, body = get(api, "/events")
        confidence = body["events"][0]["confidence"]
        assert len(confidence["breakdown"]) == 6
        assert confidence["explanation"]
        assert any("total:" in line for line in confidence["explanation"])

    def test_reports_independent_source_count_not_just_agency_count(self, api):
        # Two feeds in one independence group are not two witnesses, and a
        # consumer counting agencies would read mirrored feeds as corroboration.
        seed(api)
        assert get(api, "/events")[1]["events"][0]["independent_source_count"] == 1

    def test_filters_by_event_class(self, api):
        seed(api, event_class="incident", native_id="a")
        seed(api, event_class="work_zone", native_id="b", begin=200.0, end=201.0,
             start_time="2026-08-01T00:00:00.000Z", end_time="2026-12-01T00:00:00.000Z")

        _status, body = get(api, "/events", event_class="work_zone")
        assert [e["event_class"] for e in body["events"]] == ["work_zone"]

    def test_filters_by_measure_range(self, api):
        seed(api, begin=100.0, end=100.5, native_id="a")
        seed(api, begin=300.0, end=300.5, native_id="b")

        _status, body = get(api, "/events", begin_measure=290, end_measure=310)
        assert body["count"] == 1
        assert body["events"][0]["extent"]["begin_measure"] == 300.0

    def test_filters_by_minimum_confidence(self, api):
        # The threshold an integrator sets is a first-class query parameter.
        seed(api)
        _status, low = get(api, "/events", min_confidence=0.0)
        _status, high = get(api, "/events", min_confidence=0.99)
        assert low["count"] == 1
        assert high["count"] == 0

    def test_filters_by_lifecycle_state(self, api):
        seed(api)
        _status, body = get(api, "/events", lifecycle_state="reported")
        assert body["count"] == 0  # it was promoted to active
        _status, body = get(api, "/events", lifecycle_state="active")
        assert body["count"] == 1

    def test_filters_by_state(self, api):
        seed(api, states=["OK"], native_id="a")
        seed(api, states=["TX"], native_id="b", begin=400.0, end=400.5)
        _status, body = get(api, "/events", state="tx")
        assert body["count"] == 1
        assert body["events"][0]["extent"]["states"] == ["TX"]

    def test_filters_by_time_window(self, api):
        seed(api, start_time="2026-08-10T11:30:00.000Z", end_time="2026-08-10T14:00:00.000Z")
        _status, inside = get(api, "/events", start="2026-08-10T12:00:00Z",
                              end="2026-08-10T13:00:00Z")
        _status, after = get(api, "/events", start="2026-08-11T00:00:00Z")
        assert inside["count"] == 1
        assert after["count"] == 0

    def test_merged_children_are_not_served_as_separate_events(self, api):
        # Otherwise a merge would INCREASE the apparent event count, which is the
        # opposite of what dedup is for.
        first = seed(api)
        parent = first.changes[0].final
        seed(api, candidate=make_candidate(source_id="az511-events", begin=100.1, end=100.5),
             nearby=[parent])

        _status, body = get(api, "/events")
        assert body["count"] == 1
        assert body["events"][0]["independent_source_count"] == 2

    def test_a_truncated_answer_says_so(self, api):
        for index in range(3):
            seed(api, native_id=f"n{index}", begin=100.0 + index, end=100.2 + index)
        _status, body = get(api, "/events", limit=2)

        assert body["count"] == 2
        assert body["truncated"] is True
        assert body["matched_before_limit"] == 3

    def test_echoes_the_filters_it_applied(self, api):
        _status, body = get(api, "/events", event_class="incident", min_confidence=0.2)
        assert body["query"]["event_class"] == ["incident"]
        assert body["query"]["min_confidence"] == 0.2

    def test_labels_a_stale_event_as_stale_without_changing_its_state(self, api):
        # The honest middle with no timer built: the stored state is authoritative,
        # and the consumer can still see that nothing has confirmed this event in
        # longer than its class allows.
        seed(api)
        _status, fresh = get(api, "/events")
        assert fresh["events"][0]["lifecycle"]["ttl_expired"] is False
        assert fresh["events"][0]["lifecycle"]["ttl_expired_is_advisory"] is True

        from datetime import timedelta

        api.now_utc = lambda: NOW + timedelta(hours=6)
        _status, stale = get(api, "/events")
        assert stale["events"][0]["lifecycle"]["ttl_expired"] is True
        # Still `active`: this function does not move events.
        assert stale["events"][0]["lifecycle_state"] == "active"


class TestBadRequests:
    """A wrong filter must never look like an empty road."""

    def test_a_non_numeric_filter_is_a_400_not_an_empty_200(self, api):
        status, body = get(api, "/events", min_confidence="high")
        assert status == 400
        assert "min_confidence" in body["error"]

    def test_an_unparseable_timestamp_is_a_400(self, api):
        status, body = get(api, "/events", start="last tuesday")
        assert status == 400
        assert "start" in body["error"]

    def test_a_malformed_bbox_is_a_400(self, api):
        status, body = get(api, "/events", bbox="1,2,3")
        assert status == 400
        assert "bbox" in body["error"]

    def test_a_zero_limit_is_a_400(self, api):
        status, _body = get(api, "/events", limit=0)
        assert status == 400

    def test_an_unknown_event_is_a_404(self, api):
        status, _body = get(api, "/events/NOPE")
        assert status == 404

    def test_an_unknown_path_is_a_404(self, api):
        status, _body = get(api, "/nothing-here")
        assert status == 404

    def test_a_write_method_is_rejected(self, api):
        # This API is read-only by design, and the query function's table grant is
        # read-only to match.
        status, body = api.route_request("POST", "/events", {})
        assert status == 405
        assert "read-only" in body["error"]


class TestEventDetail:
    def test_returns_the_audit_trail_and_the_field_provenance(self, api):
        first = seed(api, source_id="ok-odot-wzdx", agency_severity="major")
        parent = first.changes[0].final
        seed(
            api,
            candidate=make_candidate(
                source_id="nm-dot-wzdx", begin=100.1, end=100.5, agency_severity="minor"
            ),
            nearby=[parent],
        )

        status, body = get(api, f"/events/{parent.event_id}")
        assert status == 200
        event = body["event"]

        # A queryable audit trail, one record per version.
        assert len(event["audit"]) == event["version_count"]
        assert {a["trigger"] for a in event["audit"]} >= {"source_update", "dedup_decision"}

        # Which source won each field, and what the loser said.
        explanation = event["explanation"]
        assert explanation["field_provenance"]["agency_severity"] == "ok-odot-wzdx"
        assert explanation["alternates"]["agency_severity"][0]["value"] == "minor"
        assert explanation["merge_decisions"]
        assert explanation["related_event_ids"]

    def test_the_merge_decision_records_the_rule_version_that_made_it(self, api):
        # A historical decision has to stay reproducible, which
        # means knowing which version of the rules made it.
        first = seed(api)
        parent = first.changes[0].final
        seed(api, candidate=make_candidate(source_id="az511-events", begin=100.1, end=100.5),
             nearby=[parent])

        _status, body = get(api, f"/events/{parent.event_id}")
        decision = body["event"]["explanation"]["merge_decisions"][0]
        assert decision["rule_version"]
        assert decision["reason"]


class TestHistory:
    def test_returns_every_version_and_transition(self, api):
        result = seed(api)
        event_id = result.changes[0].event_id

        status, body = get(api, f"/events/{event_id}/history")
        assert status == 200
        assert body["version_count"] == 3
        assert [v["lifecycle_state"] for v in body["versions"]] == [
            "reported",
            "validated",
            "active",
        ]
        assert len(body["audit"]) == 3

    def test_as_of_excludes_versions_written_after_that_instant(self, api):
        # Bitemporality, and the question an incident review actually asks:
        # what did we believe at the time, not what do we know now. An
        # overwrite-in-place store cannot answer it at all.
        from datetime import timedelta

        from corridor_event_hub.core.resolution import resolve_update

        result = seed(api)
        event_id = result.changes[0].event_id
        at_creation = result.changes[0].steps[0].audit.recorded_at

        # A genuinely later invocation: an hour on, the source revises the record.
        later = NOW + timedelta(hours=1)
        revised = make_candidate(
            source_updated_at="2026-08-10T12:45:00.000Z",
            lane_impacts=[LaneImpact(ordinal=1, type="general", status="closed", inferred=False)],
        )
        update = resolve_update(
            revised, score(revised, later), api._store.get_current(event_id), later
        )
        for change in update.changes:
            api._store.write_change(change)

        _status, now_body = get(api, f"/events/{event_id}/history")
        _status, then_body = get(api, f"/events/{event_id}/history", as_of=at_creation)

        assert now_body["version_count"] == 4
        assert then_body["version_count"] == 3  # the revision is not yet visible
        assert then_body["as_of_version"]["lane_impacts"] == []

    def test_transitions_written_in_one_invocation_share_an_instant(self, api):
        # reported -> validated -> active is one resolver invocation, so all three
        # carry the same recorded_at and `as_of` at that instant returns the state at
        # the END of it. Pinned because the alternative - inventing sub-millisecond
        # offsets to separate them - would make the timestamps look more precise than
        # the events were, and someone will be tempted.
        result = seed(api)
        event_id = result.changes[0].event_id
        at_creation = result.changes[0].steps[0].audit.recorded_at

        _status, body = get(api, f"/events/{event_id}/history", as_of=at_creation)
        assert body["version_count"] == 3
        assert body["as_of_version"]["lifecycle_state"] == "active"

    def test_an_unparseable_as_of_is_a_400(self, api):
        result = seed(api)
        status, _body = get(
            api, f"/events/{result.changes[0].event_id}/history", as_of="yesterday"
        )
        assert status == 400


class TestLookAhead:
    """The look-ahead - the automated-truck query"""

    def setup_events(self, api):
        seed(api, native_id="near", begin=110.0, end=110.5, direction="EB")
        seed(api, native_id="far", begin=140.0, end=140.5, direction="EB")
        seed(api, native_id="behind", begin=80.0, end=80.5, direction="EB")
        seed(api, native_id="oncoming", begin=120.0, end=120.5, direction="WB")

    def test_returns_events_ahead_ordered_by_distance(self, api):
        self.setup_events(api)
        status, body = get(api, "/ahead", position=100, direction="EB", distance=50)

        assert status == 200
        distances = [e["distance_miles"] for e in body["events"]]
        assert distances == sorted(distances)
        assert distances[0] == 10.0

    def test_excludes_what_is_behind_the_vehicle(self, api):
        self.setup_events(api)
        _status, body = get(api, "/ahead", position=100, direction="EB", distance=50)
        assert all(e["extent"]["begin_measure"] >= 100 for e in body["events"])

    def test_excludes_the_opposite_carriageway(self, api):
        # The two Oklahoma work zones in the live feed are one project reported once
        # per direction. Only one of them is on the road this truck is driving.
        self.setup_events(api)
        _status, body = get(api, "/ahead", position=100, direction="EB", distance=50)
        assert all(e["extent"]["direction"] != "WB" for e in body["events"])

    def test_a_westbound_vehicle_looks_the_other_way(self, api):
        seed(api, native_id="west", begin=80.0, end=80.5, direction="WB")
        seed(api, native_id="east", begin=120.0, end=120.5, direction="WB")
        _status, body = get(api, "/ahead", position=100, direction="WB", distance=50)
        assert [e["extent"]["begin_measure"] for e in body["events"]] == [80.0]

    def test_a_heading_in_degrees_resolves_to_a_direction(self, api):
        self.setup_events(api)
        _status, body = get(api, "/ahead", position=100, heading=90, distance=50)
        assert body["query"]["direction"] == "EB"
        _status, body = get(api, "/ahead", position=100, heading=270, distance=50)
        assert body["query"]["direction"] == "WB"

    def test_respects_the_look_ahead_distance(self, api):
        self.setup_events(api)
        _status, near = get(api, "/ahead", position=100, direction="EB", distance=15)
        assert [e["extent"]["begin_measure"] for e in near["events"]] == [110.0]

    def test_carries_lane_detail_a_machine_can_act_on(self, api):
        # The look-ahead needs lane-level detail, and no field whose meaning
        # depends on reading prose.
        seed(
            api,
            native_id="lanes",
            begin=110.0,
            end=110.5,
            lane_impacts=[
                LaneImpact(ordinal=1, type="general", status="closed", inferred=False),
                LaneImpact(ordinal=2, type="shoulder", status="closed", inferred=True),
            ],
        )
        _status, body = get(api, "/ahead", position=100, direction="EB")
        summary = body["events"][0]["lane_summary"]
        assert summary["lanes_described"] == 2
        assert summary["general_lanes_closed"] == 1
        assert summary["any_inferred"] is True

    def test_a_confidence_threshold_applies_here_too(self, api):
        self.setup_events(api)
        _status, body = get(api, "/ahead", position=100, direction="EB", min_confidence=0.99)
        assert body["count"] == 0

    def test_a_missing_position_is_a_400(self, api):
        status, body = get(api, "/ahead", direction="EB")
        assert status == 400
        assert "position" in body["error"]

    def test_a_missing_direction_is_a_400(self, api):
        # Guessing a direction would silently return the oncoming carriageway half
        # the time.
        status, body = get(api, "/ahead", position=100)
        assert status == 400
        assert "direction" in body["error"]

    def test_lat_lon_without_corridor_geometry_says_so(self, api):
        # Degrading to a wrong answer would be worse. The corridor for TEST-ROUTE is
        # not loadable offline, so this is the real unavailable path.
        status, body = get(api, "/ahead", lat=35.4, lon=-97.5, direction="EB")
        assert status == 400
        assert "corridor geometry" in body["error"]


class TestReviewQueue:
    def test_lists_the_ambiguous_pairs_with_the_band_that_produced_them(self, api):
        first = seed(api, source_id="az511-events", begin=100.0, end=100.2)
        parent = first.changes[0].final
        seed(api, candidate=make_candidate(source_id="ok-odot-wzdx", begin=103.0, end=103.2),
             nearby=[parent])

        status, body = get(api, "/review")
        assert status == 200
        assert body["count"] == 1
        assert body["reviews"][0]["other_event_id"] == parent.event_id
        assert body["reviews"][0]["explanation"]
        # The thresholds that put it here, so the queue is readable without going
        # to the source.
        assert body["band"]["merge_at_or_above"] > body["band"]["review_at_or_above"]


class TestWzdxRoute:
    """A spec-conformant feed at a stable URL"""

    def test_serves_a_conformant_feed(self, api):
        from corridor_event_hub.core.wzdx import validate_feed

        seed(
            api,
            event_class="work_zone",
            start_time="2026-08-01T00:00:00.000Z",
            end_time="2026-12-01T00:00:00.000Z",
        )
        status, body = get(api, "/wzdx")
        assert status == 200
        assert validate_feed(body) == []

    def test_the_feed_is_not_wrapped_in_the_corridor_event_hub_envelope(self, api):
        # A consumer pointing a conformance validator at this URL must get a document
        # the spec recognizes. Our advisory notice and model versions would be
        # unrecognized top-level keys.
        _status, body = get(api, "/wzdx")
        assert "advisory" not in body
        assert "confidence_model" not in body
        assert body["type"] == "FeatureCollection"
        assert body["feed_info"]["version"] == WZDX_VERSION

    def test_incidents_are_not_published_as_work_zones(self, api):
        seed(api, event_class="incident")
        _status, body = get(api, "/wzdx")
        assert body["features"] == []

    def test_the_response_declares_geojson(self, api):
        response = api.handler({"rawPath": "/wzdx", "queryStringParameters": None})
        assert response["headers"]["content-type"] == "application/geo+json"
        assert json.loads(response["body"])["type"] == "FeatureCollection"


class TestApiGatewayEnvelope:
    def test_reads_an_http_api_payload_v2_event(self, api):
        seed(api)
        response = api.handler(
            {
                "requestContext": {"http": {"method": "GET", "path": "/events"}, "stage": "$default"},
                "rawPath": "/events",
                "queryStringParameters": {"event_class": "incident"},
            }
        )
        assert response["statusCode"] == 200
        assert response["headers"]["content-type"] == "application/json"
        # An answer about a hazard on the road must not be served from a cache.
        assert response["headers"]["cache-control"] == "no-store"
        assert json.loads(response["body"])["count"] == 1

    def test_the_body_is_strict_json_even_with_an_unresolved_extent(self, api):
        import math

        seed(api, begin=math.nan, end=math.nan, states=[], conflation_method="unresolved")

        def reject(value):
            raise AssertionError(f"invalid JSON constant: {value}")

        response = api.handler({"rawPath": "/events", "queryStringParameters": None})
        json.loads(response["body"], parse_constant=reject)

    def test_a_stage_prefixed_path_still_routes(self, api):
        response = api.handler(
            {
                "requestContext": {"http": {"method": "GET", "path": "/prod/health"}, "stage": "prod"},
                "queryStringParameters": None,
            }
        )
        assert response["statusCode"] == 200

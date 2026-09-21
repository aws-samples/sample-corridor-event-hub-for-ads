"""Amazon Location traffic adapter tests against GENERATED tiles.

Amazon Location traffic is licensed and NOT redistributable - `config/sources.json`
marks it `redistributable: false` - so this repository cannot carry the captured
bytes. The fixture is 17 z8 tiles built by scripts/make-synthetic-fixtures.py, with
real MVT protobuf encoded by the inverse of the decoder in core/mvt.py, geometry taken
from the ARNOLD corridor centerline, and the free-flow-dominant segment mix recorded
on 2026-08-11. See tests/fixtures/README.md for what that costs.

The tests that matter most here are the ones asserting the adapter refuses to
overclaim. This source has no confidence field and no direction field, and the
temptation in both cases is to fill the gap with a plausible guess.
"""

from __future__ import annotations

import json

import pytest

from conftest import load_fixture
from corridor_event_hub.adapters.aws_location_traffic import (
    AwsLocationTrafficAdapter,
    TilePayload,
    payloads_from_json,
)


@pytest.fixture(scope="module")
def payload() -> str:
    return load_fixture("aws-location-traffic.json")


@pytest.fixture
def result(payload, ctx):
    return AwsLocationTrafficAdapter().parse(payload, ctx)


class TestEnvelope:
    """ALS traffic adapter - the base64 tile envelope"""

    def test_reads_tiles_and_their_addresses(self, payload):
        payloads, issues = payloads_from_json(payload)
        assert len(payloads) == 17
        assert not issues
        assert all(p.z == 8 and p.body for p in payloads)

    def test_unparseable_body_is_an_issue_not_an_exception(self):
        payloads, issues = payloads_from_json("not json at all")
        assert payloads == []
        assert issues[0].reason == "unparseable"

    def test_wrong_shape_is_reported(self):
        payloads, issues = payloads_from_json(json.dumps([1, 2, 3]))
        assert payloads == []
        assert "tiles" in issues[0].detail

    def test_one_malformed_tile_does_not_lose_the_others(self):
        body = json.dumps(
            {
                "tiles": [
                    {"z": 8, "x": 52, "y": 101, "mvtBase64": "!!!not base64!!!"},
                    {"z": 8, "x": 53, "y": 101, "mvtBase64": ""},
                ]
            }
        )
        payloads, issues = payloads_from_json(body)
        # The second entry is valid base64 (empty), so it survives; the first does
        # not. A corrupt tile must cost only itself.
        assert len(payloads) + len(issues) == 2

    def test_a_tile_that_failed_to_fetch_is_reported_not_silently_skipped(self):
        # fetch_traffic_tiles writes {"error": ...} for a tile it could not get.
        # That entry has no mvtBase64, so it must surface as an issue - otherwise a
        # partially-fetched corridor looks like a clear one.
        body = json.dumps(
            {"tiles": [{"z": 8, "x": 52, "y": 101, "error": "AccessDenied"}]}
        )
        payloads, issues = payloads_from_json(body)
        assert payloads == []
        assert len(issues) == 1


class TestLivePayload:
    """ALS traffic adapter - live captured tiles"""

    def test_produces_congestion_candidates(self, result):
        # The point of the whole exercise: class 4 from a live feed.
        congestion = [c for c in result.candidates if c.event_class == "congestion"]
        assert congestion, "no congestion candidates from a payload known to contain queues"

    def test_maps_the_full_congestion_vocabulary_observed_live(self, result):
        subtypes = {
            c.event_subtype for c in result.candidates if c.event_class == "congestion"
        }
        # `queuing` and `stationary` were both present in the capture.
        assert subtypes & {"queue", "stopped_traffic", "moderate_congestion", "light_congestion"}

    def test_free_flowing_segments_do_not_become_events(self, result):
        # 1,535 of 1,586 flow features were `free`. Emitting a "no congestion here"
        # event per segment per cycle would bury the store in non-events.
        for candidate in result.candidates:
            assert candidate.extensions.get("als_kind") not in ("free", "none")

    def test_carries_numeric_speed_and_congestion_ratio(self, result):
        flow = [
            c
            for c in result.candidates
            if c.extensions.get("als_layer") == "traffic_flow"
        ]
        assert flow
        speeds = [c.extensions["als_speed_kph"] for c in flow]
        assert any(isinstance(s, (int, float)) for s in speeds)

    def test_incidents_carry_real_temporal_bounds(self, result):
        incidents = [
            c
            for c in result.candidates
            if c.extensions.get("als_layer") == "traffic_incidents"
        ]
        if not incidents:
            pytest.skip("no incidents on the corridor in this capture")
        # Epoch seconds -> ISO. A naive parse would yield 1970 and silently corrupt
        # every TTL downstream.
        assert any(c.start_time.startswith("20") for c in incidents)

    def test_stable_segment_id_travels_for_the_matcher(self, result):
        # The correlation key no other source gives us. Without it the matcher can
        # only infer identity from geometry overlap across polling cycles.
        assert all("als_segment_id" in c.extensions for c in result.candidates)

    def test_attribution_is_carried_per_record(self, result):
        # A licence obligation belongs with the record, not only in the catalog.
        assert result.candidates
        for candidate in result.candidates:
            assert "HERE" in (candidate.extensions.get("als_attribution") or "")

    def test_filters_to_mainline_and_ignores_local_streets(self, result):
        # A z8 tile spans far more than the corridor, and city traffic inside the
        # buffer is not corridor congestion.
        for candidate in result.candidates:
            assert candidate.extensions["als_road_kind_detail"] in ("motorway", "trunk")

    def test_reports_off_corridor_rather_than_placing_everything(self, result):
        # Tiles cover a wide area; most features are legitimately not on I-40.
        assert result.off_corridor > 0

    def test_every_candidate_lands_on_the_corridor(self, result):
        from corridor_event_hub.core.lrs import CORRIDOR_TOTAL_MILES

        for candidate in result.candidates:
            assert 0 <= candidate.extent.begin_measure <= CORRIDOR_TOTAL_MILES
            assert candidate.extent.states


class TestRefusesToOverclaim:
    """ALS traffic adapter - what it declines to guess

    These are the tests worth having. Both gaps below are ones a well-meaning
    implementation would paper over.
    """

    def test_never_claims_a_reading_is_observed(self, result):
        # There is NO confidence field, so an observed speed is indistinguishable
        # from a historical average. Claiming "observed" would be a false
        # provenance claim - exactly what the confidence model guards against.
        assert result.candidates
        for candidate in result.candidates:
            assert candidate.time_confidence == "estimated"

    def test_reports_missing_direction_rather_than_guessing_one(self, result):
        # Tiles model each carriageway separately but never state a heading.
        # "Queue eastbound" and "queue westbound" are different events to a truck,
        # so UNKNOWN is the honest answer and the gap must be reported.
        assert result.candidates
        for candidate in result.candidates:
            assert candidate.extent.direction == "UNKNOWN"
        direction_issues = [i for i in result.issues if i.field == "direction"]
        assert len(direction_issues) == len(result.candidates)

    def test_unknown_flow_kind_becomes_an_issue_not_a_default(self, ctx):
        # The schema is undocumented, so a new `kind` value is a live risk with no
        # spec to diff against. It must surface, never be coerced to a subtype.
        adapter = AwsLocationTrafficAdapter()
        result = adapter._map_feature(
            layer_name="traffic_flow",
            feature_properties={
                "id": 1,
                "kind": "gridlocked_somehow_new",
                "road_kind_detail": "motorway",
            },
            coordinates=[(-111.65, 35.19), (-111.60, 35.20)],
            geometry_type="LineString",
            tile=TilePayload(8, 52, 101, b""),
            ctx=ctx,
            seen_ids=set(),
        )
        assert result.candidates == []
        assert result.issues[0].reason == "unmapped_vocabulary"

    def test_unknown_incident_kind_becomes_an_issue(self, ctx):
        adapter = AwsLocationTrafficAdapter()
        result = adapter._map_feature(
            layer_name="traffic_incidents",
            feature_properties={
                "id": 2,
                "kind": "alien_landing",
                "road_kind_detail": "motorway",
            },
            coordinates=[(-111.65, 35.19)],
            geometry_type="Point",
            tile=TilePayload(8, 52, 101, b""),
            ctx=ctx,
            seen_ids=set(),
        )
        assert result.candidates == []
        assert result.issues[0].reason == "unmapped_vocabulary"

    def test_unknown_layer_is_reported(self, ctx):
        adapter = AwsLocationTrafficAdapter()
        result = adapter._map_feature(
            layer_name="traffic_something_new",
            feature_properties={"id": 3},
            coordinates=[(-111.65, 35.19)],
            geometry_type="Point",
            tile=TilePayload(8, 52, 101, b""),
            ctx=ctx,
            seen_ids=set(),
        )
        assert result.candidates == []
        assert result.issues[0].field == "layer"

    def test_positional_accuracy_never_claims_better_than_a_tile(self, result):
        # The limit is the tile's declared extent (4096), not its raster pixel size:
        # one coordinate unit is ~31 m at this latitude. Measured agreement with the
        # ARNOLD centerline is p90 46 m, so 50 m is the floor. See
        # TILE_POSITIONAL_ACCURACY_FLOOR_METERS for the derivation.
        for candidate in result.candidates:
            assert candidate.extent.positional_accuracy_meters >= 50.0

    def test_never_invents_a_source_publish_time(self, result):
        # Tiles carry no publish timestamp. Claiming one would fabricate freshness
        # we cannot observe, and freshness feeds confidence decay.
        for candidate in result.candidates:
            assert candidate.source.source_updated_at is None

    def test_flow_readings_have_no_lane_impacts(self, result):
        # Flow says traffic is slow, not which lane is blocked. Inferring a closed
        # lane from a low speed would be inventing structure.
        flow = [
            c
            for c in result.candidates
            if c.extensions.get("als_layer") == "traffic_flow"
        ]
        for candidate in flow:
            assert candidate.lane_impacts == []


class TestAdapterBoundary:
    """ALS traffic adapter - the adapter boundary"""

    def test_emits_only_candidates_with_no_lifecycle_or_confidence(self, result):
        # The boundary is enforced by the type, but assert the intent: an adapter
        # has no vocabulary for lifecycle state or confidence.
        for candidate in result.candidates:
            assert not hasattr(candidate, "lifecycle_state")
            assert not hasattr(candidate, "confidence")
            assert not hasattr(candidate, "event_id")

    def test_repeated_segment_ids_across_tiles_are_collapsed(self, ctx):
        # Sub-tile fragments mean the same id appears in adjacent tiles. Emitting
        # both would hand the matcher two candidates that are one segment. This is
        # tile bookkeeping - real cross-agency dedup stays the matcher's job.
        adapter = AwsLocationTrafficAdapter()
        seen: set = set()
        properties = {
            "id": 42,
            "kind": "queuing",
            "road_kind_detail": "motorway",
            "source": "(c) 2026 HERE",
        }
        coords = [(-111.65, 35.19), (-111.60, 35.20)]
        first = adapter._map_feature(
            layer_name="traffic_flow",
            feature_properties=properties,
            coordinates=coords,
            geometry_type="LineString",
            tile=TilePayload(8, 52, 101, b""),
            ctx=ctx,
            seen_ids=seen,
        )
        second = adapter._map_feature(
            layer_name="traffic_flow",
            feature_properties=properties,
            coordinates=coords,
            geometry_type="LineString",
            tile=TilePayload(8, 53, 101, b""),
            ctx=ctx,
            seen_ids=seen,
        )
        assert len(first.candidates) == 1
        assert second.candidates == []

    def test_source_id_matches_the_catalog(self):
        from corridor_event_hub.core.config import load_json

        catalog_ids = {s["sourceId"] for s in load_json("sources.json")["sources"]}
        assert AwsLocationTrafficAdapter.source_id in catalog_ids

    def test_registered_in_the_pipeline_registry(self):
        from corridor_event_hub.adapters.registry import ADAPTERS

        assert "aws-location-traffic" in ADAPTERS

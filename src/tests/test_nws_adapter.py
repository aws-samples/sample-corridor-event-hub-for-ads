"""NWS alerts adapter tests against a REAL captured payload (2026-08-07).

This adapter takes the polygon spatial path, which is the one that most justifies
PostGIS (ADR 0002 § The split, and why). The tests
that matter are the ones asserting it reports what it cannot place rather than
guessing.
"""

from __future__ import annotations

import json
import math

import pytest

from conftest import load_fixture
from corridor_event_hub.adapters.nws_alerts import NwsAlertsAdapter, classify_weather

# A box straddling I-40 near Amarillo, TX.
AMARILLO_BOX = [
    [
        [-102.5, 34.9],
        [-101.0, 34.9],
        [-101.0, 35.6],
        [-102.5, 35.6],
        [-102.5, 34.9],
    ]
]


@pytest.fixture(scope="module")
def payload() -> str:
    return load_fixture("nws-alerts.json")


@pytest.fixture
def result(payload, ctx):
    return NwsAlertsAdapter().parse(payload, ctx)


class TestNwsLivePayload:
    """NWS alerts adapter - live payload"""

    def test_reports_zone_coded_alerts_as_unmappable_rather_than_guessing(self, result):
        # 20 of 31 live alerts had geometry:null and only UGC zone codes. Placing
        # them requires a zone shapefile join we do not have - so they are issues.
        geometry_issues = [i for i in result.issues if i.field == "geometry"]
        assert len(geometry_issues) > 0
        assert "zone-coded" in geometry_issues[0].detail

    def test_never_emits_a_candidate_without_a_resolved_location(self, result):
        for candidate in result.candidates:
            assert not math.isnan(candidate.extent.begin_measure)
            assert len(candidate.extent.states) > 0

    def test_marks_weather_as_affecting_both_directions(self, result):
        for candidate in result.candidates:
            assert candidate.extent.direction == "BOTH"

    def test_uses_polygon_intersect_conflation_for_alerts_it_can_place(self, result):
        for candidate in result.candidates:
            assert candidate.extent.conflation_method == "polygon_intersect"

    def test_handles_malformed_json_without_raising(self, ctx):
        result = NwsAlertsAdapter().parse("not json at all", ctx)
        assert result.candidates == []
        assert result.issues[0].reason == "unparseable"

    def test_emits_both_a_weather_and_a_road_surface_event_for_a_winter_storm(self, ctx):
        # One alert, two classes, linked not blended - they clear at
        # different times.
        synthetic = json.dumps(
            {
                "features": [
                    {
                        "id": "test-winter",
                        "geometry": {"type": "Polygon", "coordinates": AMARILLO_BOX},
                        "properties": {
                            "id": "test-winter",
                            "event": "Winter Storm Warning",
                            "severity": "Severe",
                            "onset": "2026-08-07T20:00:00Z",
                            "ends": "2026-08-08T12:00:00Z",
                            "areaDesc": "Potter; Randall",
                        },
                    }
                ]
            }
        )
        result = NwsAlertsAdapter().parse(synthetic, ctx)
        classes = [c.event_class for c in result.candidates]
        assert "weather" in classes
        assert "road_surface" in classes
        surface = next(c for c in result.candidates if c.event_class == "road_surface")
        assert surface.event_subtype == "snow"

    def test_the_two_linked_candidates_do_not_share_a_mutable_extent(self, ctx):
        # Aliasing one Extent across two candidates is the kind of bug that only
        # appears once something downstream edits an event in place - by which time
        # it looks like a data problem rather than a code one.
        synthetic = json.dumps(
            {
                "features": [
                    {
                        "id": "test-winter",
                        "geometry": {"type": "Polygon", "coordinates": AMARILLO_BOX},
                        "properties": {
                            "id": "test-winter",
                            "event": "Winter Storm Warning",
                            "onset": "2026-08-07T20:00:00Z",
                        },
                    }
                ]
            }
        )
        weather, surface = NwsAlertsAdapter().parse(synthetic, ctx).candidates
        assert weather.extent is not surface.extent
        assert weather.extensions is not surface.extensions
        surface.extent.direction = "EB"
        assert weather.extent.direction == "BOTH"

    def test_filters_alerts_with_no_roadway_relevance(self, ctx):
        marine = json.dumps(
            {
                "features": [
                    {
                        "id": "m",
                        "geometry": {"type": "Polygon", "coordinates": AMARILLO_BOX},
                        "properties": {"id": "m", "event": "Rip Current Statement"},
                    }
                ]
            }
        )
        assert NwsAlertsAdapter().parse(marine, ctx).candidates == []

    def test_flags_using_expires_as_a_proxy_for_ends(self, ctx):
        # `expires` is when the ALERT lapses, not when the WEATHER ends. Using it
        # is defensible; using it silently is not.
        no_ends = json.dumps(
            {
                "features": [
                    {
                        "id": "e",
                        "geometry": {"type": "Polygon", "coordinates": AMARILLO_BOX},
                        "properties": {
                            "id": "e",
                            "event": "Dust Storm Warning",
                            "onset": "2026-08-07T20:00:00Z",
                            "ends": None,
                            "expires": "2026-08-07T23:00:00Z",
                        },
                    }
                ]
            }
        )
        result = NwsAlertsAdapter().parse(no_ends, ctx)
        assert [i for i in result.issues if i.field == "ends"]
        assert result.candidates[0].end_time == "2026-08-07T23:00:00Z"

    def test_survives_a_self_intersecting_polygon(self, ctx):
        # Agency polygons self-intersect more often than you would hope. A bowtie
        # must not take down a normalize run.
        bowtie = json.dumps(
            {
                "features": [
                    {
                        "id": "b",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-102.5, 34.9],
                                    [-101.0, 35.6],
                                    [-102.5, 35.6],
                                    [-101.0, 34.9],
                                    [-102.5, 34.9],
                                ]
                            ],
                        },
                        "properties": {"id": "b", "event": "High Wind Warning"},
                    }
                ]
            }
        )
        result = NwsAlertsAdapter().parse(bowtie, ctx)
        # Placed or not placed are both acceptable; raising is not.
        assert result.off_corridor + len(result.candidates) >= 1

    def test_keeps_every_nws_property_it_does_not_model(self, result):
        # 32 properties per alert, most with no canonical home.
        for candidate in result.candidates:
            assert "nws_ugc" in candidate.extensions
            assert "nws_headline" in candidate.extensions
            assert candidate.extensions["nws_event"]


class TestWeatherClassification:
    @pytest.mark.parametrize(
        ("event_name", "expected"),
        [
            ("Dust Storm Warning", "dust_storm"),
            ("High Wind Warning", "high_wind"),
            ("Winter Storm Warning", "winter_storm"),
            ("Dense Fog Advisory", "low_visibility"),
            ("Blowing Snow Advisory", "blowing_snow"),
            ("Flood Warning", "flooding"),
            ("Severe Thunderstorm Warning", "severe_thunderstorm"),
            ("Excessive Heat Warning", "extreme_heat"),
        ],
    )
    def test_maps_corridor_relevant_hazards_to_subtypes(self, event_name, expected):
        assert classify_weather(event_name) == expected

    def test_falls_back_to_advisory_for_anything_unrecognized(self):
        assert classify_weather("Special Weather Statement") == "advisory"

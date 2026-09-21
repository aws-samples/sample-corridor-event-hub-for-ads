"""Serialization tests.

These exist because the failure they guard against happens in a Lambda at runtime,
not in a test or at synth. ``json.dumps`` emits a bare ``NaN`` token by default,
which is not valid JSON; EventBridge and DynamoDB both reject it. An unresolved
extent legitimately carries ``begin_measure = nan`` (never guess a
location), so the two meet on the normalizer's happy path.
"""

from __future__ import annotations

import json
import math

import pytest

from corridor_event_hub.core.serde import dumps, to_jsonable
from corridor_event_hub.core.types import Extent, GeoJsonGeometry, LaneImpact, MappingIssue


def _unresolved_extent() -> Extent:
    return Extent(
        route="I-40",
        begin_measure=math.nan,
        end_measure=math.nan,
        direction="UNKNOWN",
        states=[],
        geometry=None,
        positional_accuracy_meters=None,
        conflation_method="unresolved",
    )


class TestToJsonable:
    def test_converts_a_dataclass_to_a_dict(self):
        impact = LaneImpact(ordinal=1, type="general", status="closed", inferred=False)
        assert to_jsonable(impact) == {
            "ordinal": 1,
            "type": "general",
            "status": "closed",
            "inferred": False,
            "inferred_from": None,
        }

    def test_converts_nested_dataclasses(self):
        extent = Extent(
            route="I-40",
            begin_measure=100.0,
            end_measure=101.0,
            direction="EB",
            states=["OK"],
            geometry=GeoJsonGeometry(type="Point", coordinates=[-98.0, 35.4]),
            positional_accuracy_meters=50.0,
            conflation_method="coordinate",
        )
        result = to_jsonable(extent)
        assert result["geometry"] == {"type": "Point", "coordinates": [-98.0, 35.4]}

    def test_turns_nan_into_null(self):
        # The whole reason this module exists.
        assert to_jsonable(_unresolved_extent())["begin_measure"] is None

    @pytest.mark.parametrize("value", [float("inf"), float("-inf")])
    def test_turns_infinity_into_null(self, value):
        assert to_jsonable({"x": value}) == {"x": None}

    def test_preserves_ordinary_floats(self):
        assert to_jsonable({"x": 100.5}) == {"x": 100.5}

    def test_walks_lists_and_dicts_of_dataclasses(self):
        issues = [MappingIssue(field="lanes", raw_value=None, reason="missing_required")]
        assert to_jsonable({"issues": issues})["issues"][0]["field"] == "lanes"

    def test_leaves_a_dataclass_type_alone(self):
        # ``is_dataclass`` is true for the CLASS as well as an instance; converting
        # the class would produce nonsense.
        assert to_jsonable(Extent) is Extent


class TestDumps:
    def test_produces_valid_json_for_an_unresolved_extent(self):
        encoded = dumps(_unresolved_extent())
        assert "NaN" not in encoded
        assert json.loads(encoded)["begin_measure"] is None

    def test_round_trips_through_a_strict_parser(self):
        # ``parse_constant`` fires on NaN/Infinity, which is exactly what an AWS
        # API's parser would reject.
        def reject(value):
            raise AssertionError(f"invalid JSON constant: {value}")

        json.loads(dumps(_unresolved_extent()), parse_constant=reject)

    def test_passes_through_json_dumps_kwargs(self):
        assert "\n" in dumps({"a": 1, "b": 2}, indent=1)

"""LRS tests - with state-line cases as first-class assertions.

Milepost/LRS conflation is the risk that undermines every downstream claim, since
dedup depends on it. State-line tests are therefore acceptance here rather than
afterthought. This file is that acceptance.
"""

from __future__ import annotations

import math

import pytest

from corridor_event_hub.core.geo import haversine_miles, line_length_miles, nearest_point_on_line
from corridor_event_hub.core.lrs import (
    CORRIDOR_TOTAL_MILES,
    CoordinateInput,
    UnresolvedInput,
    corridor,
    is_ahead_of,
    measure_overlap,
    measure_to_state_milepost,
    state_milepost_to_measure,
    states_for_range,
)


class TestMilepostToMeasure:
    """milepost -> corridor measure"""

    def test_az_starts_at_corridor_zero(self):
        assert state_milepost_to_measure("AZ", 0) == 0
        assert state_milepost_to_measure("AZ", 100) == 100

    def test_applies_per_state_offset_so_mileposts_stop_restarting(self):
        # The whole point: NM MP 100 is NOT corridor measure 100.
        #
        # The offsets are MEASURED from the route geometry, not the published
        # per-state mileages that used to be here (359.5 / 459.5 / 910). Using the
        # measured value is what makes the state-line identity exact rather than
        # half a mile out. Regenerate with scripts/fetch-arnold.py.
        #
        # These moved when the SOURCE changed, and the size of the move is the
        # point. They were 359.349 and 909.730, read from the four state DOT LRS
        # layers. Those layers do not license redistribution (see /NOTICE), so the
        # geometry now comes from the federal NTAD National Highway System, and
        # Arizona's I-40 extent reads 359.347 there instead - 2 THOUSANDTHS of a
        # mile, about 3 metres, from an independent publisher. Oklahoma moves more,
        # 0.29 mi, because its sections are stitched from control-section measures
        # rather than a statewide one; that is _calibrate_oklahoma's residual, not
        # a geometry disagreement.
        nm_offset = state_milepost_to_measure("NM", 0)
        assert nm_offset == pytest.approx(359.347, abs=0.001)
        # 100 miles into NM is 100 miles further along the corridor. This is the
        # relationship under test; the absolute above just pins the geometry.
        assert state_milepost_to_measure("NM", 100) == pytest.approx(nm_offset + 100)
        assert state_milepost_to_measure("OK", 0) == pytest.approx(910.018, abs=0.001)

    def test_rejects_out_of_range_milepost_rather_than_clamping(self):
        # An out-of-range value is a mapping issue, not something to coerce.
        assert state_milepost_to_measure("AZ", 9999) is None
        assert state_milepost_to_measure("AZ", -5) is None

    def test_case_insensitive_on_state_but_rejects_unknown_states(self):
        assert state_milepost_to_measure("az", 10) == 10
        assert state_milepost_to_measure("CA", 10) is None

    def test_round_trips_back_to_the_state_reference(self):
        measure = state_milepost_to_measure("TX", 88)
        assert measure is not None
        state, milepost = measure_to_state_milepost(measure)
        assert state == "TX"
        assert milepost == pytest.approx(88, abs=1e-6)


class TestTheStateLineCase:
    """THE STATE-LINE CASE - dedup depends on this"""

    def test_az_last_milepost_and_nm_mp_0_resolve_to_the_same_measure(self):
        # Two agencies reporting the same physical location with different
        # mileposts. If this fails, cross-state dedup is impossible.
        #
        # AZ's last milepost is READ from the config, not written as a literal.
        # It used to be 359.5; measured ADOT LRS data says 359.349, and
        # state_milepost_to_measure does not clamp - so the old literal
        # became out-of-range and returned None, making this check pass a
        # comparison of None against None in an earlier form. Reading the boundary
        # back means it cannot go stale when the measured extent moves.
        az = next(s for s in corridor.states if s.state == "AZ")
        assert state_milepost_to_measure("AZ", az.state_milepost_max) is not None
        assert state_milepost_to_measure(
            "AZ", az.state_milepost_max
        ) == state_milepost_to_measure("NM", 0)

    def test_every_state_boundary_is_continuous(self):
        for current, following in zip(corridor.states, corridor.states[1:]):
            current_end = current.corridor_offset + current.length_miles
            assert following.corridor_offset == pytest.approx(current_end, abs=1e-6)

    def test_extent_spanning_a_state_line_reports_both_states(self):
        # One event, multi-state extent - NOT two events.
        assert states_for_range(355, 365) == ["AZ", "NM"]

    def test_wholly_in_state_extent_reports_one_state(self):
        assert states_for_range(100, 200) == ["AZ"]
        assert states_for_range(950, 1000) == ["OK"]

    def test_extent_spanning_three_states_reports_all_three(self):
        # NM->TX->OK: measures 700 to 950.
        assert states_for_range(700, 950) == ["NM", "TX", "OK"]

    def test_corridor_total_is_the_sum_of_state_lengths(self):
        assert pytest.approx(1241, abs=0.5) == CORRIDOR_TOTAL_MILES


class TestDedupProximityIsArithmetic:
    """dedup proximity is ARITHMETIC, not spatial

    ADR 0002 § The split, and why
    """

    def test_detects_overlap_between_two_measure_ranges(self):
        assert measure_overlap((100, 105), (103, 110)).overlaps is True

    def test_treats_near_misses_inside_tolerance_as_overlapping(self):
        # Two agencies locate the same crash 0.3 mi apart. Same event.
        assert measure_overlap((100.0, 100.0), (100.3, 100.3), 0.5).overlaps is True

    def test_reports_a_gap_when_genuinely_separate(self):
        result = measure_overlap((100, 101), (110, 111), 0.5)
        assert result.overlaps is False
        assert result.gap_miles == pytest.approx(8.5, abs=0.05)

    def test_matches_across_a_state_line_once_both_are_corridor_measures(self):
        # Same crash at the border, reported by both states: AZ a tenth of a mile
        # short of its last milepost, NM a tenth of a mile past its first.
        #
        # The AZ milepost is derived from the measured extent rather than the
        # literal 359.4 that used to be here - which is now PAST Arizona's last
        # milepost of 359.349, so it returned None and this test failed comparing
        # None to None.
        az_state = next(s for s in corridor.states if s.state == "AZ")
        az = state_milepost_to_measure("AZ", az_state.state_milepost_max - 0.1)
        nm = state_milepost_to_measure("NM", 0.1)
        assert az is not None and nm is not None
        assert measure_overlap((az, az), (nm, nm), 0.5).overlaps is True


class TestLookAheadQuery:
    """look-ahead query - the automated-truck case"""

    def test_finds_an_event_ahead_in_the_travel_direction(self):
        assert is_ahead_of((120, 121, "EB"), (100, "EB"), 50) is True

    def test_excludes_an_event_already_passed(self):
        assert is_ahead_of((80, 81, "EB"), (100, "EB"), 50) is False

    def test_excludes_an_event_beyond_the_look_ahead_distance(self):
        assert is_ahead_of((300, 301, "EB"), (100, "EB"), 50) is False

    def test_excludes_opposite_direction_events(self):
        assert is_ahead_of((120, 121, "WB"), (100, "EB"), 50) is False

    def test_includes_both_direction_events_like_weather(self):
        assert is_ahead_of((120, 121, "BOTH"), (100, "EB"), 50) is True

    def test_handles_westbound_travel_where_measures_decrease(self):
        assert is_ahead_of((80, 81, "WB"), (100, "WB"), 50) is True
        assert is_ahead_of((120, 121, "WB"), (100, "WB"), 50) is False


class TestCoordinateConflation:
    def test_places_an_on_corridor_coordinate_and_reports_its_state(self, conflator):
        # Near Amarillo, TX - on I-40.
        result = conflator.conflate(CoordinateInput(lon=-101.83, lat=35.2))
        assert result.on_corridor is True
        assert result.states == ["TX"]
        assert result.method == "coordinate"

    def test_rejects_a_coordinate_far_off_the_corridor(self, conflator):
        # Denver - nowhere near I-40.
        result = conflator.conflate(CoordinateInput(lon=-104.99, lat=39.74))
        assert result.on_corridor is False
        assert result.states == []

    def test_marks_unresolved_input_as_such_rather_than_guessing(self, conflator):
        result = conflator.conflate(UnresolvedInput(text="near the big curve"))
        assert result.method == "unresolved"
        assert result.on_corridor is False
        assert math.isnan(result.begin_measure)

    def test_carries_an_accuracy_estimate_so_confidence_can_penalize_imprecision(
        self, conflator
    ):
        result = conflator.conflate(CoordinateInput(lon=-101.83, lat=35.2))
        assert result.positional_accuracy_meters > 0


class TestGeodesicDistance:
    """Distances must be GEODESIC, not shapely's planar degrees.

    A planar length over lon/lat returns DEGREES, which is both meaningless as
    miles and plausible enough to ship. These tests pin real distances so a future
    refactor toward ``LineString.length`` fails loudly instead of silently
    producing mileposts that are wrong by a few percent - or, on a north-south
    corridor, badly wrong.
    """

    def test_haversine_matches_a_known_distance(self):
        # Flagstaff AZ to Albuquerque NM, ~325 mi great-circle.
        flagstaff = (-111.651, 35.198)
        albuquerque = (-106.650, 35.084)
        assert haversine_miles(flagstaff, albuquerque) == pytest.approx(283, abs=8)

    def test_one_degree_of_latitude_is_about_69_miles(self):
        assert haversine_miles((-100.0, 35.0), (-100.0, 36.0)) == pytest.approx(69.1, abs=0.3)

    def test_a_degree_of_longitude_is_shorter_than_a_degree_of_latitude(self):
        # The exact reason a planar length is wrong: the two axes are not the same
        # scale, and the longitude scale varies with latitude.
        lon_degree = haversine_miles((-100.0, 35.0), (-99.0, 35.0))
        lat_degree = haversine_miles((-100.0, 35.0), (-100.0, 36.0))
        assert lon_degree < lat_degree
        assert lon_degree == pytest.approx(56.7, abs=0.5)

    def test_centerline_length_is_in_miles_not_degrees(self):
        # The centerline spans I-40 end to end, so its length must be in the
        # 1,100-1,400 mi range. A planar length would be ~20 (degrees). The band
        # stays wide on purpose - it is a units check, not a geometry check;
        # npm run db-landmarks is what pins the geometry.
        assert 1_000 < line_length_miles(corridor.centerline) < 1_500

    def test_snapping_returns_a_distance_along_in_miles(self):
        line = [(-100.0, 35.0), (-99.0, 35.0)]
        _snapped, along, offset = nearest_point_on_line(line, (-99.5, 35.0))
        assert along == pytest.approx(28.3, abs=0.5)
        assert offset == pytest.approx(0.0, abs=0.01)

    def test_snapping_measures_perpendicular_offset(self):
        line = [(-100.0, 35.0), (-99.0, 35.0)]
        _snapped, _along, offset = nearest_point_on_line(line, (-99.5, 35.1))
        assert offset == pytest.approx(6.9, abs=0.2)

    def test_snapping_clamps_to_the_line_rather_than_extrapolating(self):
        # A point well past the eastern end must snap to the end, not beyond it.
        line = [(-100.0, 35.0), (-99.0, 35.0)]
        snapped, along, _offset = nearest_point_on_line(line, (-98.0, 35.0))
        assert snapped == pytest.approx((-99.0, 35.0))
        assert along == pytest.approx(line_length_miles(line))


class TestCrossValidationAgainstPostgis:
    """docs/SPATIAL-DB.md publishes a specific agreement figure between this
    implementation and the PostGIS one. That claim is the evidence for ADR 0002's
    "the decision is reversible" - two independent implementations agreeing is what
    makes the swap safe - so it is pinned here rather than left as prose that quietly
    drifts.

    The PostGIS value was measured against the deployed cluster (see SPATIAL-DB.md
    §"Cross-validation"). If this test fails, either the conflator changed or the
    documented figure is now wrong; fix whichever is actually stale.
    """

    # `conflate_point('I-40', -101.83, 35.20)` on the deployed PostGIS cluster.
    #
    # Measured 2026-09-16 against the deployed cluster, on NTAD geometry, AFTER
    # re-applying sql/002-corridor-real.sql so the cluster and the offline corridor
    # hold the same line:
    #
    #   ./scripts/db.sh --file sql/002-corridor-real.sql
    #   ./scripts/db.sh "SELECT corridor_measure FROM \
    #     conflate_point('I-40', -101.83, 35.20)"    ->  803.725
    #
    # Agreement to 0.001 mi (1.6 m), one unit in the last place both
    # implementations round to. That is expected rather than lucky: both READ the
    # measure by interpolating the same per-vertex values - PostGIS via
    # ST_InterpolatePoint on centerline_m, this conflator via the parallel
    # `measures` array - so they compute the same quantity from the same numbers.
    #
    # THIS CONSTANT CANNOT BE UPDATED FROM THE OFFLINE CORRIDOR ALONE. It is the
    # cluster's answer, and the only way to refresh it is to load the geometry and
    # ask - the two commands above, in that order. Re-sourcing the geometry without
    # re-applying the migration makes this test fail with a delta that looks like an
    # implementation bug and is really just a stale database: on the state-LRS
    # geometry this read 803.482, and after re-sourcing the local conflator returned
    # 803.724 against that stale figure for an apparent 0.242 mi disagreement.
    #
    # The previous figures were 784.023 and a 0.235 mi delta, taken against the
    # 40-point placeholder. Worth remembering that the old agreement was NOT
    # evidence the model was right: both implementations were computing the same
    # biased fraction-times-total_miles formula correctly, and both were ~19 miles
    # out. See docs/CORRIDOR-GEOMETRY.md, "Two lessons worth keeping".
    POSTGIS_MEASURE = 803.725
    DOCUMENTED_DELTA_MILES = 0.001

    def test_agrees_with_postgis_to_the_documented_tolerance(self, conflator):
        result = conflator.conflate(CoordinateInput(lon=-101.83, lat=35.20))
        delta = abs(result.begin_measure - self.POSTGIS_MEASURE)
        assert delta == pytest.approx(self.DOCUMENTED_DELTA_MILES, abs=0.01), (
            f"drifted from the figure published in docs/SPATIAL-DB.md: {delta:.3f} mi"
        )

    def test_the_disagreement_stays_far_inside_the_centerline_error(self, conflator):
        # 0.235 mi on a 1,241-mile corridor is immaterial next to the placeholder
        # centerline's own several-mile error. If this ever fails, the two
        # implementations have diverged enough that one of them has a bug.
        result = conflator.conflate(CoordinateInput(lon=-101.83, lat=35.20))
        assert abs(result.begin_measure - self.POSTGIS_MEASURE) < 1.0

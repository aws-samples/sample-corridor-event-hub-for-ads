"""Point-in-polygon: the pure-Python replacement for shapely.

WHY THIS EXISTS: shapely was the ONLY use of a compiled library in this package,
imported in one function for one boolean - and it drags numpy behind it. Together
they were 51 MB, 85% of the Lambda bundle, for a containment test. numpy was never
imported by any of our code at all.

Replacing something that worked demands proof of equivalence, not just green tests
on the fixtures we happen to have. So this file DIFFERENTIAL-TESTS the replacement
against shapely itself over thousands of random points, on the shapes that break
naive implementations: concave outlines, holes, multiple holes, MultiPolygon, and
the boundary.

shapely stays a DEV dependency precisely so this test can keep running. It is no
longer in the deployment bundle, so a divergence between the two is a test failure
here rather than a differently-placed weather alert in production.

The reference is ``covers``, not ``contains``: an alert polygon whose edge runs
along the corridor should place the alert.
"""

from __future__ import annotations

import random

import pytest

from corridor_event_hub.core.lrs import _point_in_polygon, _polygon_rings

shapely_geometry = pytest.importorskip(
    "shapely.geometry", reason="shapely is a dev dependency; it is not in the bundle"
)

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]]}
CONCAVE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [4, 0], [4, 1], [1, 1], [1, 4], [0, 4], [0, 0]]],
}
WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [6, 0], [6, 6], [0, 6], [0, 0]],
        [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]],
    ],
}
TWO_HOLES = {
    "type": "Polygon",
    "coordinates": [
        [[0, 0], [9, 0], [9, 9], [0, 9], [0, 0]],
        [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]],
        [[5, 5], [7, 5], [7, 7], [5, 7], [5, 5]],
    ],
}
MULTIPOLYGON = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
        [[[5, 5], [8, 5], [8, 8], [5, 8], [5, 5]]],
    ],
}

ALL_SHAPES = [
    pytest.param(SQUARE, id="square"),
    pytest.param(CONCAVE, id="concave-L"),
    pytest.param(WITH_HOLE, id="one-hole"),
    pytest.param(TWO_HOLES, id="two-holes"),
    pytest.param(MULTIPOLYGON, id="multipolygon"),
]


class TestAgreesWithShapely:
    @pytest.mark.parametrize("geometry", ALL_SHAPES)
    def test_same_answer_on_thousands_of_random_points(self, geometry):
        """The whole justification for the swap, on every shape that could break it."""
        reference = shapely_geometry.shape(geometry)
        rings = _polygon_rings(geometry)
        rng = random.Random(7)  # fixed: a flaky equivalence test is worthless

        disagreements = []
        for _ in range(4000):
            point = (rng.uniform(-1, 10), rng.uniform(-1, 10))
            mine = _point_in_polygon(point, rings)
            theirs = reference.covers(shapely_geometry.Point(point))
            if mine != theirs:
                disagreements.append((point, mine, theirs))

        assert not disagreements, (
            f"{len(disagreements)} disagreements, first: point={disagreements[0][0]} "
            f"ours={disagreements[0][1]} shapely={disagreements[0][2]}"
        )


class TestHoles:
    def test_a_point_in_a_hole_is_OUTSIDE(self):
        # Even-odd gets this free: the exterior crossing plus the hole crossing is
        # two, which is even. A winding implementation needs explicit hole handling.
        assert not _point_in_polygon((3, 3), _polygon_rings(WITH_HOLE))

    def test_a_point_in_the_ring_between_hole_and_edge_is_INSIDE(self):
        assert _point_in_polygon((1, 1), _polygon_rings(WITH_HOLE))

    def test_both_holes_are_holes(self):
        rings = _polygon_rings(TWO_HOLES)
        assert not _point_in_polygon((2, 2), rings)
        assert not _point_in_polygon((6, 6), rings)
        assert _point_in_polygon((4.5, 4.5), rings)


class TestBoundary:
    @pytest.mark.parametrize(
        "point,label",
        [((2, 0), "edge midpoint"), ((0, 0), "vertex"), ((4, 2), "right edge"), ((2, 4), "top")],
    )
    def test_the_boundary_counts_as_inside(self, point, label):
        # covers(), not contains(). An alert whose edge runs along the corridor
        # should place the alert rather than fall through to unresolved.
        assert _point_in_polygon(point, _polygon_rings(SQUARE)), label

    def test_a_ray_through_a_vertex_does_not_double_count(self):
        # The classic ray-casting bug: a horizontal ray grazing a vertex flips
        # parity twice and reports outside. The half-open y rule prevents it.
        diamond = {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [4, 0], [2, -2], [0, 0]]]}
        rings = _polygon_rings(diamond)
        assert _point_in_polygon((2, 0), rings)  # dead centre, ray through 2 vertices
        assert not _point_in_polygon((5, 0), rings)
        assert not _point_in_polygon((-1, 0), rings)


class TestMalformedInput:
    def test_a_self_intersecting_ring_still_answers(self):
        # shapely reported is_valid == False here and needed a buffer(0) repair.
        # Even-odd has a defined answer for any ring sequence, so there is nothing
        # to repair and no alert to discard. Agency polygons self-intersect.
        bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [4, 4], [4, 0], [0, 4], [0, 0]]]}
        rings = _polygon_rings(bowtie)
        assert isinstance(_point_in_polygon((2, 1), rings), bool)
        assert not _point_in_polygon((10, 10), rings)

    def test_a_non_polygon_geometry_is_rejected(self):
        # The caller turns this into `unresolved`, which the adapter records rather
        # than swallows.
        with pytest.raises(ValueError, match="not a polygon"):
            _polygon_rings({"type": "LineString", "coordinates": [[0, 0], [1, 1]]})

    def test_a_ring_too_short_to_enclose_anything_is_rejected(self):
        with pytest.raises(ValueError):
            _polygon_rings({"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 0]]]})

    def test_a_multipolygon_is_not_read_as_a_polygon(self):
        # The silent-inversion case: treating a MultiPolygon's parts as a Polygon's
        # rings makes the second part a hole in the first.
        rings = _polygon_rings(MULTIPOLYGON)
        assert len(rings) == 2, "each part must stay its own polygon"
        assert _point_in_polygon((1, 1), rings)
        assert _point_in_polygon((6, 6), rings)

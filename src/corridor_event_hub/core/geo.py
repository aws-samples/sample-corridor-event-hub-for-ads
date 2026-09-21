"""Geodesic helpers for corridor geometry.

WHY THIS FILE EXISTS: shapely is a PLANAR library. ``LineString.length`` on
lon/lat coordinates returns degrees, and ``project`` returns a distance along the
line in degrees - both meaningless as corridor miles, and both quietly plausible
enough to ship.

The error is not uniform, which is what makes it dangerous. A degree of longitude
is ~57 mi at 35 degrees latitude and ~69 mi at the equator, while a degree of
latitude is ~69 mi everywhere. So a planar length over an east-west corridor is
wrong by a few percent, and over a north-south one it is wrong by a different
amount in a different direction. An adopter pointing this at their own corridor
would get badly wrong mileposts with no error message anywhere.

So distances here are GEODESIC (haversine on a spherical earth), and shapely is
used only for the operation it is genuinely good at: polygon containment, where
the planar approximation is harmless because the test is topological rather than
metric.

The equirectangular projection used for point-to-segment snapping is accurate to
well under a mile for segments of this length at mid latitudes. That was written
when the centerline was a 40-point placeholder accurate to several miles, and it
mattered less then. Real state LRS geometry now averages 0.07 mi between vertices,
so the projection error is the smaller term rather than the invisible one - it is
metres against a centerline that is itself good to metres.

CALIBRATION NOTE: against turf.js, which snaps using great-circle cross-track
distance rather than a local projection, reported offsets differ by roughly 0.4%
(~7m on a 1.6km offset). Worth knowing if anyone compares output between
implementations and wonders which is right.

PostGIS is the reference, and the corridor measure has since been checked against
it directly: ``conflate_point('I-40', -101.83, 35.20)`` returns 803.482 on the
deployed cluster and this implementation returns 803.482. See
tests/test_lrs.py::TestCrossValidationAgainstPostgis. Note that the agreement is
on the MEASURE, which both now read from calibrated M values; the offset distance
is still computed differently by each, which is what this note is about.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# Mean earth radius, miles. WGS84 ellipsoidal distance would be more precise;
# the difference is ~0.3% and the centerline error dominates it by three orders
# of magnitude.
EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.34

Coord = tuple[float, float]  # (lon, lat) - GeoJSON order, not (lat, lon)


def haversine_miles(a: Coord, b: Coord) -> float:
    """Great-circle distance between two (lon, lat) points, in miles."""
    lon1, lat1 = a
    lon2, lat2 = b
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    h = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, h)))


def cumulative_miles(coords: Sequence[Coord]) -> list[float]:
    """Distance from the first vertex to each vertex, in miles.

    Returned list is the same length as ``coords``, starting at 0.0, so the last
    element is the total line length.
    """
    out = [0.0]
    for i in range(1, len(coords)):
        out.append(out[-1] + haversine_miles(coords[i - 1], coords[i]))
    return out


def line_length_miles(coords: Sequence[Coord]) -> float:
    if len(coords) < 2:
        return 0.0
    return cumulative_miles(coords)[-1]


def _project_local(origin: Coord, pt: Coord) -> Coord:
    """Equirectangular projection to miles east/north of ``origin``."""
    lon0, lat0 = origin
    lon, lat = pt
    cos_lat = math.cos(math.radians(lat0))
    x = math.radians(lon - lon0) * cos_lat * EARTH_RADIUS_MILES
    y = math.radians(lat - lat0) * EARTH_RADIUS_MILES
    return (x, y)


def nearest_point_on_line(
    coords: Sequence[Coord], point: Coord
) -> tuple[Coord, float, float]:
    """Snap a point onto a polyline. The ``ST_LineLocatePoint`` equivalent.

    Returns ``(snapped_coord, distance_along_miles, offset_miles)`` where
    ``distance_along_miles`` is measured from the first vertex and
    ``offset_miles`` is how far the input point sat off the line.
    """
    if not coords:
        raise ValueError("cannot snap to an empty line")
    if len(coords) == 1:
        return coords[0], 0.0, haversine_miles(coords[0], point)

    cumulative = cumulative_miles(coords)
    best = (coords[0], 0.0, float("inf"))

    for i in range(len(coords) - 1):
        start, end = coords[i], coords[i + 1]
        # Work in a local frame centred on the segment start: within a single
        # segment the earth is flat enough that the perpendicular foot is exact
        # to a few metres.
        sx, sy = 0.0, 0.0
        ex, ey = _project_local(start, end)
        px, py = _project_local(start, point)

        seg_len_sq = (ex - sx) ** 2 + (ey - sy) ** 2
        if seg_len_sq == 0:
            t = 0.0
        else:
            t = ((px - sx) * (ex - sx) + (py - sy) * (ey - sy)) / seg_len_sq
            t = max(0.0, min(1.0, t))  # clamp to the segment, never extrapolate

        # Interpolating in lon/lat is what turf does too, and over a single
        # segment the difference from a great-circle interpolation is negligible.
        snapped: Coord = (
            start[0] + (end[0] - start[0]) * t,
            start[1] + (end[1] - start[1]) * t,
        )
        offset = haversine_miles(snapped, point)
        if offset < best[2]:
            along = cumulative[i] + haversine_miles(start, snapped)
            best = (snapped, along, offset)

    return best


def point_along(coords: Sequence[Coord], distance_miles: float) -> Coord:
    """The point a given distance along a polyline, in miles from its start."""
    if not coords:
        raise ValueError("cannot walk an empty line")
    cumulative = cumulative_miles(coords)
    if distance_miles <= 0:
        return coords[0]
    if distance_miles >= cumulative[-1]:
        return coords[-1]

    for i in range(1, len(coords)):
        if cumulative[i] >= distance_miles:
            span = cumulative[i] - cumulative[i - 1]
            t = 0.0 if span == 0 else (distance_miles - cumulative[i - 1]) / span
            start, end = coords[i - 1], coords[i]
            return (
                start[0] + (end[0] - start[0]) * t,
                start[1] + (end[1] - start[1]) * t,
            )
    return coords[-1]

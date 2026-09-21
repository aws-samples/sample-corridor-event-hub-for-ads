"""Corridor linear referencing.

================================================================
THIS IS THE SWAPPABLE INTERFACE described in ADR 0002.
================================================================

The recommended production path puts Aurora PostGIS behind ``Conflator``: state
DOT GIS shops already work in PostGIS, researchers get SQL over the corridor,
and the LRS becomes inspectable data rather than logic buried in a Lambda. The
alternative - this file's ``LocalConflator``, corridor geometry as static data
in a Lambda layer - removes a database, a VPC dependency, and its ops burden.

Both live behind the same narrow interface so the choice stays REVERSIBLE. That is
the point: an adopter picks one, and can change its mind later, because the swap is
a constructor argument rather than a rewrite.

The key insight (ADR 0002 § The split, and why):
once conflation produces
``route + measure range``, everything downstream is NUMERIC. Dedup proximity
becomes range overlap; look-ahead becomes a range scan. Spatial work is confined
to this file.
"""

from __future__ import annotations

import json
import math
import os
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from .geo import (
    METERS_PER_MILE,
    Coord,
    cumulative_miles,
    line_length_miles,
    nearest_point_on_line,
    point_along,
)
from .types import GeoJsonGeometry


@dataclass(frozen=True)
class StateSegment:
    state: str
    state_milepost_min: float
    state_milepost_max: float
    corridor_offset: float

    @property
    def length_miles(self) -> float:
        return self.state_milepost_max - self.state_milepost_min


@dataclass(frozen=True)
class CorridorConfig:
    route: str
    centerline: list[Coord]
    corridor_buffer_meters: float
    states: tuple[StateSegment, ...]
    verified: bool
    # Corridor measure per centerline vertex, in miles, or None when the geometry
    # is not calibrated.
    #
    # WHY A PARALLEL ARRAY AND NOT A THIRD COORDINATE ELEMENT: GeoJSON has no M
    # dimension. The spec's optional third element is ELEVATION, so putting a
    # measure there would be silently misread by any conformant reader. PostGIS
    # gets the same numbers as a real LINESTRINGM in corridor.centerline_m; this
    # array is how the same information survives a GeoJSON round trip.
    measures: tuple[float, ...] | None = None

    # --- the LRS, as methods rather than module functions -------------------
    #
    # These used to be module-level functions reading a module-level singleton,
    # which is what made the whole system single-corridor: there was exactly one
    # `corridor` and every conversion went through it. As methods they are per
    # corridor, so two routes are two objects rather than two deployments.
    # The module-level wrappers further down still exist and still work; they now
    # delegate to whichever corridor is active.

    @property
    def total_miles(self) -> float:
        """Corridor length from the LRS, not from the geometry.

        Summed from the state segments, so it is the MEASURE range. The geometry's
        own geodesic length is close but not equal - see docs/CORRIDOR-GEOMETRY.md
        on why conflating the two is the systematic-bias bug.
        """
        return sum(s.length_miles for s in self.states)

    def measure_for(self, state: str, milepost: float) -> float | None:
        """State milepost -> corridor measure.

        The conversion that makes cross-state dedup possible: AZ MP 359 and
        NM MP 0 are the same physical place and only become comparable here.

        Returns None for an out-of-range milepost rather than clamping -
        measure 0 is a real place, so a coerced value would be silently wrong.
        """
        key = state.upper()
        for seg in self.states:
            if seg.state != key:
                continue
            if milepost < seg.state_milepost_min or milepost > seg.state_milepost_max:
                return None
            return seg.corridor_offset + (milepost - seg.state_milepost_min)
        return None

    def milepost_for(self, measure: float) -> tuple[str, float] | None:
        """Inverse, for rendering back into a state's own reference."""
        for seg in self.states:
            begin = seg.corridor_offset
            end = seg.corridor_offset + seg.length_miles
            if begin <= measure <= end:
                return seg.state, seg.state_milepost_min + (measure - begin)
        return None

    def states_in(self, begin_measure: float, end_measure: float) -> list[str]:
        """Which states a measure range touches, west to east.

        Length > 1 means the extent crosses a state line, which is ONE event with a
        multi-state extent, not two events.
        """
        lo = min(begin_measure, end_measure)
        hi = max(begin_measure, end_measure)
        return [
            seg.state
            for seg in self.states
            if hi >= seg.corridor_offset
            and lo <= seg.corridor_offset + seg.length_miles
        ]


# ---------------------------------------------------------------------------
# Where the corridor comes from
# ---------------------------------------------------------------------------
#
# THIS USED TO BE ONE LINE: `corridor = _load_corridor()`, at import, reading
# config/corridor.json. That single line was the architecture's cardinality limit -
# one file, one route, resolved before anything could ask for a different one, and
# read from disk merely by importing this module.
#
# Now: a SOURCE is injected, corridors are cached PER ROUTE, and nothing is read
# until something asks. The deployed pipeline injects the database
# (core/postgis.load_corridor); the probe, the UI API and the tests fall back to a
# JSON file, which is what keeps `npm run probe` working with no AWS account at all.
#
# lrs.py deliberately does NOT import core.postgis - postgis imports from here, so
# that would be circular. Injection rather than import is what keeps the dependency
# pointing one way.

#: Set by whoever knows where corridors live. None means "read a JSON file".
_corridor_source: Callable[[str], CorridorConfig] | None = None

#: Per route, because loading an 11,873-vertex centerline is not free.
_corridor_cache: dict[str, CorridorConfig] = {}

#: Which route the module-level helpers mean when nobody says. Set from the JSON
#: file's own `route`, or by set_active_route().
_active_route: str | None = None


def set_corridor_source(source: Callable[[str], CorridorConfig] | None) -> None:
    """Install the corridor loader. Pass None to go back to the JSON file.

    Clears the cache, because a source change means the old objects came from
    somewhere else - and a stale corridor is the kind of thing that places events
    confidently in the wrong state.
    """
    global _corridor_source
    _corridor_source = source
    _corridor_cache.clear()


def set_active_route(route: str | None) -> None:
    """Choose which corridor the module-level helpers refer to."""
    global _active_route
    _active_route = route


def corridor_for(route: str) -> CorridorConfig:
    """One corridor, by route. Cached."""
    cached = _corridor_cache.get(route)
    if cached is not None:
        return cached
    loader = _corridor_source or _load_corridor_from_json
    loaded = loader(route)
    _corridor_cache[route] = loaded
    return loaded


def active_corridor() -> CorridorConfig:
    """The corridor the module-level helpers mean.

    Exists so a single-corridor caller - which is most of them, and every test -
    does not have to thread a route through. A multi-corridor caller uses
    ``corridor_for(route)`` and never touches this.
    """
    if _active_route is not None:
        return corridor_for(_active_route)
    # No route named: fall back to whatever the JSON file declares, which is the
    # historical behaviour and what keeps the offline tools working unchanged.
    default = corridor_for("")
    set_active_route(default.route)
    return default


def __getattr__(name: str) -> Any:
    """Module-level ``corridor`` and ``CORRIDOR_TOTAL_MILES``, resolved lazily.

    PEP 562. These were assignments evaluated at import, which meant importing this
    module read a file - so the migration Lambda, which needs none of this, could
    not import anything from here without config/ in its bundle.

    Kept as module attributes rather than deleted because ~7 call sites and 5 test
    modules do `from .lrs import corridor`, and that form goes through here too.
    """
    if name == "corridor":
        return active_corridor()
    if name == "CORRIDOR_TOTAL_MILES":
        return active_corridor().total_miles
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_corridor_from_json(route: str = "") -> CorridorConfig:
    """The offline source: a corridor from a JSON file.

    ``route`` is accepted and ignored: a JSON file holds exactly one corridor, which
    is the limitation this whole section exists to route around. A caller asking for
    a route the file does not describe gets a clear error rather than the wrong
    corridor.
    """
    loaded = _load_corridor()
    if route and route != loaded.route:
        raise LookupError(
            f"corridor {route!r} was requested but the JSON source describes "
            f"{loaded.route!r}. A JSON file holds ONE corridor - point "
            "set_corridor_source() at the database for more than one."
        )
    return loaded


def _corridor_json_path() -> Path:
    """Where the offline corridor lives.

    NOT config/, and that is the point of this whole exercise. A corridor in config/
    was copied into every Lambda bundle, and it is singular by construction - one
    route per file. Production reads corridors from Postgres, where they are keyed by
    route; this file is the OFFLINE source, for `npm run probe`, the UI API and the test
    suite, none of which have an AWS account.

    Searched in order so a checkout works with no configuration:
      1. CEH_CORRIDOR_FILE   - explicit, wins
      2. reference/corridor.json  - the checked-in offline corridor
      3. config/corridor.json     - where it used to live, for anything not yet moved
    """
    override = os.environ.get("CEH_CORRIDOR_FILE")
    if override:
        return Path(override)

    # The directory CONTAINING the package - src/ in a checkout, /var/task in the
    # Lambda bundle. Both put reference/ and config/ there, so one root serves both.
    root = Path(__file__).resolve().parent.parent.parent
    for relative in ("reference/corridor.json", "config/corridor.json"):
        candidate = root / relative
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        "no offline corridor found. Set CEH_CORRIDOR_FILE, or point "
        "set_corridor_source() at the database (core/postgis.load_corridor), which "
        "is what the deployed pipeline does. Looked for reference/corridor.json and "
        f"config/corridor.json beside the package, in {root}."
    )


def _load_corridor() -> CorridorConfig:
    with _corridor_json_path().open(encoding="utf-8") as handle:
        raw = json.load(handle)
    coords = [(float(c[0]), float(c[1])) for c in raw["centerline"]["coordinates"]]

    # A measures array of the wrong length is worse than none at all: it would
    # silently misplace every event past the point where the two diverge. Reject
    # it rather than truncate to the shorter of the two.
    measures_raw = raw["centerline"].get("measures")
    measures = None
    if measures_raw is not None:
        if len(measures_raw) != len(coords):
            raise ValueError(
                f"corridor.json: centerline has {len(coords)} coordinates but "
                f"{len(measures_raw)} measures. Regenerate with "
                f"scripts/fetch-arnold.py --write-config."
            )
        measures = tuple(float(m) for m in measures_raw)

    return CorridorConfig(
        route=raw["route"],
        centerline=coords,
        measures=measures,
        corridor_buffer_meters=float(raw["corridorBufferMeters"]),
        states=tuple(
            StateSegment(
                state=s["state"],
                state_milepost_min=float(s["stateMilepostMin"]),
                state_milepost_max=float(s["stateMilepostMax"]),
                corridor_offset=float(s["corridorOffset"]),
            )
            for s in raw["states"]
        ),
        verified=bool(raw["verified"]),
    )


# `corridor` and `CORRIDOR_TOTAL_MILES` used to be assigned here. They are now
# resolved on demand by __getattr__ above, so importing this module reads nothing.


# ---------------------------------------------------------------------------
# Spatial input - what a source can give us
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MilepostInput:
    state: str
    begin_mp: float
    end_mp: float | None = None


@dataclass(frozen=True)
class CoordinateInput:
    lon: float
    lat: float


@dataclass(frozen=True)
class LineStringInput:
    coordinates: Sequence[Coord]


@dataclass(frozen=True)
class PolygonInput:
    geometry: GeoJsonGeometry | dict[str, Any]


@dataclass(frozen=True)
class UnresolvedInput:
    text: str


SpatialInput = Union[
    MilepostInput, CoordinateInput, LineStringInput, PolygonInput, UnresolvedInput
]


@dataclass
class ConflationResult:
    begin_measure: float
    end_measure: float
    states: list[str]
    method: str
    positional_accuracy_meters: float | None
    # False when the input falls outside the corridor buffer entirely.
    on_corridor: bool


class Conflator:
    """The seam. ``LocalConflator`` and ``PostgisConflator`` both satisfy this."""

    def conflate(self, spatial_input: SpatialInput) -> ConflationResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Milepost <-> corridor measure
# ---------------------------------------------------------------------------


# THIN WRAPPERS over CorridorConfig, kept because ~20 call sites use them and a
# single-corridor caller should not have to name a route. Each takes an optional
# corridor so a multi-corridor caller can be explicit; omitted, they use the active
# one. The logic lives on the dataclass - see the methods above.


def state_milepost_to_measure(
    state: str, milepost: float, corridor: CorridorConfig | None = None
) -> float | None:
    """State milepost -> corridor measure."""
    return (corridor or active_corridor()).measure_for(state, milepost)


def measure_to_state_milepost(
    measure: float, corridor: CorridorConfig | None = None
) -> tuple[str, float] | None:
    """Inverse, for rendering back into a state's own reference."""
    return (corridor or active_corridor()).milepost_for(measure)


def states_for_range(
    begin_measure: float, end_measure: float, corridor: CorridorConfig | None = None
) -> list[str]:
    """Which states a measure range touches, west to east."""
    return (corridor or active_corridor()).states_in(begin_measure, end_measure)


# ---------------------------------------------------------------------------
# Numeric operations - everything below is arithmetic, NOT spatial
# ---------------------------------------------------------------------------


@dataclass
class OverlapResult:
    overlaps: bool
    overlap_miles: float
    gap_miles: float


def measure_overlap(
    a: tuple[float, float],
    b: tuple[float, float],
    tolerance_miles: float = 0.5,
) -> OverlapResult:
    """Proximity matching, reduced to range overlap.

    This is the operation people assume needs a spatial database. It does not.
    Ranges are ``(begin_measure, end_measure)`` pairs; the tolerance is applied
    to ``a`` so two agencies locating the same crash a few tenths of a mile apart
    still match.
    """
    a_lo = min(a) - tolerance_miles
    a_hi = max(a) + tolerance_miles
    b_lo, b_hi = min(b), max(b)

    overlap_miles = min(a_hi, b_hi) - max(a_lo, b_lo)
    if overlap_miles >= 0:
        return OverlapResult(True, overlap_miles, 0.0)
    return OverlapResult(False, 0.0, -overlap_miles)


def is_ahead_of(
    event: tuple[float, float, str],
    position: tuple[float, str],
    look_ahead_miles: float,
) -> bool:
    """The look-ahead query - "what is ahead of me", the automated-truck question,
    also just arithmetic once measures exist.

    ``event`` is ``(begin_measure, end_measure, direction)`` and ``position`` is
    ``(measure, direction)``.
    """
    begin, end, event_direction = event
    measure, travel_direction = position

    if (
        event_direction != "BOTH"
        and travel_direction != "BOTH"
        and event_direction != travel_direction
    ):
        return False

    lo, hi = min(begin, end), max(begin, end)
    # Eastbound measures increase; westbound decrease.
    if travel_direction == "WB":
        return lo <= measure and hi >= measure - look_ahead_miles
    return hi >= measure and lo <= measure + look_ahead_miles


# ---------------------------------------------------------------------------
# The in-process implementation (the ADR 0002 alternative)
#
# NAMED ShapelyConflator UNTIL IT STOPPED USING SHAPELY. The library was here for a
# single point-in-polygon boolean, dragged numpy behind it, and together they were
# 85% of the deployment bundle; _point_in_polygon below replaced both. The name
# outlived the dependency by exactly one commit, which is long enough for a reader
# to go looking for a library that is not there.
#
# `Local` rather than `Python` or `Bundled`: the distinction that matters is WHERE it
# runs. This one conflates in the Lambda process against geometry it holds;
# PostgisConflator asks the database. HybridConflator takes one of each, as `local`
# and `remote`.
# ---------------------------------------------------------------------------

_POLYGON_SAMPLE_COUNT = 400


class LocalConflator(Conflator):
    """Conflation against corridor geometry held as static data.

    NOTE: the offline fallback centerline is coarse. Real corridor geometry is
    loaded from Postgres in the deployed pipeline, derived from the four states' own
    ARNOLD/LRS layers - see docs/CORRIDOR-GEOMETRY.md. This path exists so the probe,
    the local UI and the tests run end to end with no database.

    Accuracy with the placeholder is roughly +/- several miles. Good enough to
    demonstrate the pipeline, NOT good enough to publish. ``verified: false`` in
    corridor.json is the tripwire.

    Distances are geodesic rather than planar - see
    ``core/geo.py`` for why that distinction is load-bearing rather than pedantic.
    """

    def __init__(
        self,
        centerline_coords: Sequence[Coord] | None = None,
        measures: Sequence[float] | None = None,
        corridor: CorridorConfig | None = None,
    ) -> None:
        """``corridor`` is the whole multi-corridor story on this side of the seam.

        It used to reach for the module-level singleton, which is why there could
        only ever be one. Now it takes a corridor - from JSON, from Postgres, or
        constructed in a test - and defaults to the active one so the ~20 existing
        call sites keep working unchanged.
        """
        self.corridor = corridor or active_corridor()
        default_geometry = centerline_coords is None
        self.centerline: list[Coord] = list(
            self.corridor.centerline if default_geometry else centerline_coords
        )
        self.length_miles = line_length_miles(self.centerline)

        # Measures only apply to the corridor's own geometry. A caller passing a
        # different centerline (the tests do, for state-line cases) gets no
        # calibration unless it supplies its own, because an array indexed against
        # some other line would place events confidently and wrongly.
        if measures is None and default_geometry:
            measures = self.corridor.measures
        self._measures: tuple[float, ...] | None = (
            tuple(float(m) for m in measures)
            if measures is not None and len(measures) == len(self.centerline)
            else None
        )
        # Cumulative geodesic distance per vertex, so a distance-along returned by
        # nearest_point_on_line can be turned back into an index and interpolated.
        self._cumulative: list[float] | None = (
            cumulative_miles(self.centerline) if self._measures else None
        )

    def conflate(self, spatial_input: SpatialInput) -> ConflationResult:
        if isinstance(spatial_input, MilepostInput):
            return self._from_milepost(spatial_input)
        if isinstance(spatial_input, CoordinateInput):
            return self._from_coordinate(spatial_input.lon, spatial_input.lat)
        if isinstance(spatial_input, LineStringInput):
            return self._from_linestring(spatial_input.coordinates)
        if isinstance(spatial_input, PolygonInput):
            return self._from_polygon(spatial_input.geometry)
        if isinstance(spatial_input, UnresolvedInput):
            return self._unresolved()
        raise TypeError(f"unsupported spatial input: {type(spatial_input).__name__}")

    @staticmethod
    def _unresolved(method: str = "unresolved") -> ConflationResult:
        return ConflationResult(
            begin_measure=math.nan,
            end_measure=math.nan,
            states=[],
            method=method,
            positional_accuracy_meters=None,
            on_corridor=False,
        )

    def _from_milepost(self, i: MilepostInput) -> ConflationResult:
        """Most precise path: the source already speaks a linear reference."""
        begin = state_milepost_to_measure(i.state, i.begin_mp)
        end = begin if i.end_mp is None else state_milepost_to_measure(i.state, i.end_mp)
        if begin is None or end is None:
            return self._unresolved(method="milepost")
        return ConflationResult(
            begin_measure=begin,
            end_measure=end,
            states=self.corridor.states_in(begin, end),
            method="milepost",
            positional_accuracy_meters=160,  # ~0.1 mi, typical milepost granularity
            on_corridor=True,
        )

    def _from_coordinate(self, lon: float, lat: float) -> ConflationResult:
        """Project a point onto the centerline. The ``ST_LineLocatePoint`` equivalent."""
        _snapped, along_miles, offset_miles = nearest_point_on_line(
            self.centerline, (lon, lat)
        )
        off_corridor_meters = offset_miles * METERS_PER_MILE
        measure = self._scale_to_corridor(along_miles)
        on_corridor = off_corridor_meters <= self.corridor.corridor_buffer_meters

        return ConflationResult(
            begin_measure=measure,
            end_measure=measure,
            states=self.corridor.states_in(measure, measure) if on_corridor else [],
            method="coordinate",
            # Never claim better than 50m: the centerline itself is not that good.
            positional_accuracy_meters=max(off_corridor_meters, 50),
            on_corridor=on_corridor,
        )

    def _from_linestring(self, coords: Sequence[Coord]) -> ConflationResult:
        if not coords:
            return self._unresolved()
        first = self._from_coordinate(coords[0][0], coords[0][1])
        last = self._from_coordinate(coords[-1][0], coords[-1][1])
        begin = min(first.begin_measure, last.begin_measure)
        end = max(first.begin_measure, last.begin_measure)
        on_corridor = first.on_corridor or last.on_corridor
        return ConflationResult(
            begin_measure=begin,
            end_measure=end,
            states=self.corridor.states_in(begin, end) if on_corridor else [],
            method="coordinate",
            positional_accuracy_meters=max(
                first.positional_accuracy_meters or 0.0,
                last.positional_accuracy_meters or 0.0,
            ),
            on_corridor=on_corridor,
        )

    def _from_polygon(
        self, geometry: GeoJsonGeometry | dict[str, Any]
    ) -> ConflationResult:
        """Polygon x corridor. The genuine 2D operation, needed for NWS alerts,
        which arrive as polygons covering whole counties (class 5).

        This is the one case where the PostGIS argument is strongest -
        ``ST_Intersection`` against a real centerline is more accurate than this
        sampling approach.
        """
        try:
            polygons = _polygon_rings(geometry)
        except Exception:
            # Malformed polygon: treat as no intersection rather than raising.
            # The mapping issue is recorded by the adapter, not swallowed here.
            return self._unresolved(method="polygon_intersect")

        hits: list[float] = []
        for i in range(_POLYGON_SAMPLE_COUNT + 1):
            frac = i / _POLYGON_SAMPLE_COUNT
            along = point_along(self.centerline, frac * self.length_miles)
            if _point_in_polygon(along, polygons):
                hits.append(self._scale_to_corridor(frac * self.length_miles))

        if not hits:
            return self._unresolved(method="polygon_intersect")

        begin, end = min(hits), max(hits)
        return ConflationResult(
            begin_measure=begin,
            end_measure=end,
            states=self.corridor.states_in(begin, end),
            method="polygon_intersect",
            # Sampling resolution is the accuracy floor here.
            positional_accuracy_meters=(self.corridor.total_miles / _POLYGON_SAMPLE_COUNT)
            * METERS_PER_MILE,
            on_corridor=True,
        )

    def _scale_to_corridor(self, miles_along_centerline: float) -> float:
        """Distance along the centerline -> corridor measure.

        Two paths, and the difference between them is the whole of Problem 2 in
        docs/CORRIDOR-GEOMETRY.md.

        CALIBRATED (preferred): READ the measure out of the measures array,
        interpolating within the vertex the distance falls in. This is the
        ``ST_InterpolatePoint`` equivalent and it is what keeps this
        implementation and the PostGIS one agreeing on real numbers rather than
        merely agreeing with each other.

        UNCALIBRATED (fallback): a fraction of the geometry's length scaled by the
        configured mileage. SYSTEMATICALLY BIASED, because the fraction is of the
        geometry while the multiplier is of the config, and coarse geometry loses
        length unevenly so the error does not cancel. Against the 40-point
        placeholder this put every landmark 7 to 28 miles west of truth. Kept only
        so an uncalibrated corridor still resolves.
        """
        if self._measures and self._cumulative:
            return round(_interpolate(self._cumulative, self._measures,
                                      miles_along_centerline), 3)
        frac = 0.0 if self.length_miles == 0 else miles_along_centerline / self.length_miles
        return round(frac * self.corridor.total_miles, 3)


def _interpolate(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    """Piecewise-linear lookup: the y at x, given ascending xs.

    Used to turn a distance along the centerline into a corridor measure. Both
    arrays are per-vertex and the same length, so bisect finds the containing
    segment and the remainder interpolates within it. Clamps at both ends rather
    than extrapolating - a point beyond the corridor's extent is at its endpoint,
    not at an invented measure past it.
    """
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x) - 1
    span = xs[i + 1] - xs[i]
    if span <= 0:
        return ys[i]
    t = (x - xs[i]) / span
    return ys[i] + t * (ys[i + 1] - ys[i])


def _polygon_rings(geometry: GeoJsonGeometry | dict[str, Any]) -> list[list[list[Coord]]]:
    """GeoJSON Polygon or MultiPolygon -> a list of polygons, each a list of rings.

    Normalises the two shapes so the containment test below does not care which it
    was given. A Polygon becomes a one-element list; a MultiPolygon keeps its parts.
    NWS sends Polygon today (and null on watches, which the adapter handles), but
    MultiPolygon is legal in the same field and would otherwise be read as a
    Polygon whose "rings" are whole polygons - inside-out, silently.
    """
    if isinstance(geometry, GeoJsonGeometry):
        kind, coordinates = geometry.type, geometry.coordinates
    else:
        kind, coordinates = geometry.get("type"), geometry.get("coordinates")

    if kind == "Polygon":
        polygons = [coordinates]
    elif kind == "MultiPolygon":
        polygons = list(coordinates)
    else:
        raise ValueError(f"not a polygon geometry: {kind!r}")

    out = []
    for polygon in polygons:
        rings = [[(float(c[0]), float(c[1])) for c in ring] for ring in polygon if len(ring) >= 4]
        if rings:
            out.append(rings)
    if not out:
        raise ValueError("polygon has no usable ring")
    return out


def _point_in_polygon(point: Coord, polygons: list[list[list[Coord]]]) -> bool:
    """Even-odd ray casting. Replaces shapely, which was here for this alone.

    WHY NOT shapely: it was the ONLY use of it in the entire package, and it drags
    numpy behind it - together 51 MB, 85% of the Lambda bundle, to answer one
    boolean. numpy is not imported anywhere in this codebase; it was pure
    transitive weight. Twenty lines of ray casting removes both.

    EVEN-ODD RATHER THAN WINDING, deliberately, because it gets two awkward cases
    right for free:

      HOLES - a GeoJSON Polygon is [exterior, hole, hole...]. Counting crossings
      across ALL rings means a point inside a hole crosses the exterior once and
      the hole once: two, even, outside. Correct, with no special casing.

      SELF-INTERSECTION - agency polygons self-intersect more often than you would
      hope. shapely returned `is_valid == False` here and needed a `buffer(0)`
      repair; even-odd has a defined answer for any ring sequence, so there is
      nothing to repair and nothing to discard.

    PLANAR, on purpose. The test is topological, not metric - a point either is or
    is not inside the ring - so treating lon/lat as a plane is harmless at
    corridor scale. Every DISTANCE in this module remains geodesic (core/geo.py),
    which is the distinction that actually matters.

    Boundary counts as inside, matching shapely's `covers` rather than `contains`.
    An alert polygon whose edge runs along the corridor should place the alert.
    """
    x, y = point
    for rings in polygons:
        inside = False
        for ring in rings:
            for i in range(len(ring) - 1):
                x1, y1 = ring[i]
                x2, y2 = ring[i + 1]
                if _on_segment(point, (x1, y1), (x2, y2)):
                    return True  # exactly on an edge: covers() says inside
                # Half-open rule on y: a vertex is counted once, not twice, so a
                # ray passing exactly through one does not flip parity twice.
                if (y1 > y) != (y2 > y):
                    crossing_x = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
                    if x < crossing_x:
                        inside = not inside
        if inside:
            return True
    return False


def _on_segment(point: Coord, start: Coord, end: Coord, tolerance: float = 1e-12) -> bool:
    """Is ``point`` on the segment ``start``-``end``, within rounding?

    Cross product for collinearity, then a bounding-box test to exclude the rest of
    the infinite line. The tolerance is in degrees, so ~1e-7 m - tight enough that
    it only catches genuine coincidence.
    """
    (x, y), (x1, y1), (x2, y2) = point, start, end
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    if abs(cross) > tolerance:
        return False
    return (
        min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance
        and min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance
    )

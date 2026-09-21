"""The PostGIS conflator, and the corridor read from the database.

THIS IS THE IMPLEMENTATION ADR 0002 RECOMMENDS, finally built. It satisfies the
same ``Conflator`` interface as the in-process one, so adopting it is a constructor
change and backing it out is too.

WHY IT MATTERS BEYOND ACCURACY - and this is the argument that actually forced it:
``config/corridor.json`` is SINGULAR BY CONSTRUCTION. One route, one centerline,
one list of states, loaded into a module-level singleton at import. A corridor
service that is supposed to grow past one interstate cannot express two corridors
at once, no matter how the geometry is stored. The database already can: every
function takes a route, and ``state_segment`` is keyed ``(route, state)``. So
reading the corridor from Postgres is not a storage preference, it is what removes
a cardinality limit from the architecture (Adopting a different route must
not require a core code change).

WHAT IT IS BETTER AT, measured rather than assumed:

  POLYGON x CORRIDOR. ``conflate_polygon`` intersects the real centerline with
  ``ST_Intersection``. The in-process path samples 400 points along the line and
  reports its own accuracy floor as 4,992 m - 3.1 miles, the worst positional
  accuracy anywhere in this system, on the class where a weather alert's extent is
  the whole point. This is a genuine improvement, not a tie.

  REFERENCE DATA IT CAN JOIN. Clearances, TMC segments and parking cannot be
  bundled; 1,543 NBI structures with their full records are 5.6 MB of SQL.

WHAT IT IS NOT BETTER AT: point and milepost conflation. Both implementations now
read the SAME per-vertex LRS measures - PostGIS from ``centerline_m``, the local
path from the parallel ``measures`` array - and agree to three decimals on the
live cluster (803.482). Moving per-record conflation here buys no accuracy and
costs a network round trip per record plus a write-path dependency on Aurora. The
seam exists so that choice can be made per deployment rather than once.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from . import dbconn
from .lrs import (
    ConflationResult,
    Conflator,
    CoordinateInput,
    CorridorConfig,
    LineStringInput,
    MilepostInput,
    PolygonInput,
    SpatialInput,
    StateSegment,
    UnresolvedInput,
)
from .types import GeoJsonGeometry

#: How the corridor's own geometry is claimed to be accurate once it comes from a
#: calibrated LRS. Not a measurement of any individual input - conflate_point
#: returns the real off-corridor distance, which is what gets reported per record.
_CALIBRATED_ACCURACY_METERS = 50.0


def _geojson_text(geometry: Any) -> str:
    """A geometry as the GeoJSON string ``conflate_polygon`` takes.

    ``ST_GeomFromGeoJSON`` wants text, and it wants only type and coordinates - an
    NWS Feature carries properties it would reject. Both shapes of input are
    normalised here rather than at each call site.
    """
    if isinstance(geometry, GeoJsonGeometry):
        payload = {"type": geometry.type, "coordinates": geometry.coordinates}
    else:
        payload = {"type": geometry.get("type"), "coordinates": geometry.get("coordinates")}
    if not payload["type"] or payload["coordinates"] is None:
        raise ValueError(f"not a usable geometry: {geometry!r}")
    return json.dumps(payload)


def _fetch(connection: Any, sql: str, params: tuple = ()) -> list[tuple]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql, params) if params else cursor.execute(sql)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def load_corridor(route: str, connection: Any | None = None) -> CorridorConfig:
    """Read a corridor out of the database, in the shape the local path also uses.

    RETURNS THE SAME ``CorridorConfig``, deliberately. That is what makes this a
    drop-in source rather than a parallel universe: the in-process conflator can be
    handed a corridor loaded from Postgres and behave identically, which is also how
    the two implementations get compared on identical input.

    The M values come back from ``centerline_m`` when it is populated. When it is
    not, ``measures`` is None and the local conflator falls back to its biased
    fraction-times-total_miles path - the same degradation, surfaced the same way,
    rather than a different failure for the DB source.
    """
    connection = connection or dbconn.connect()

    rows = _fetch(
        connection,
        """
        SELECT route, buffer_meters, verified, total_miles,
               ST_AsText(centerline::geometry)  AS centerline_wkt,
               ST_AsText(centerline_m)          AS centerline_m_wkt
        FROM corridor WHERE route = %s
        """,
        (route,),
    )
    if not rows:
        # Naming what IS there beats "not found": the usual cause is a cluster the
        # migrations never reached, not a typo.
        available = [r[0] for r in _fetch(connection, "SELECT route FROM corridor ORDER BY route")]
        present = ", ".join(available) if available else (
            "NONE - has npm run db-migrate run against this cluster?"
        )
        raise LookupError(f"corridor {route!r} is not in the database. Present: {present}")

    route_name, buffer_meters, verified, _total_miles, centerline_wkt, centerline_m_wkt = rows[0]

    coordinates = _linestring_coords(centerline_wkt)
    measures = None
    if centerline_m_wkt:
        m_coords, m_values = _linestring_measures(centerline_m_wkt)
        # Same rule as the JSON loader: a measures array of the wrong length is
        # worse than none, because it misplaces everything past the divergence.
        if len(m_coords) == len(coordinates):
            measures = m_values
        else:
            raise ValueError(
                f"corridor {route_name}: centerline has {len(coordinates)} vertices but "
                f"centerline_m has {len(m_coords)}. They are supposed to be the same "
                "line. Reload with scripts/fetch-arnold.py and npm run db-migrate."
            )

    segments = _fetch(
        connection,
        """
        SELECT state, state_mp_min, state_mp_max, corridor_offset
        FROM state_segment WHERE route = %s ORDER BY corridor_offset
        """,
        (route,),
    )
    if not segments:
        raise LookupError(
            f"corridor {route_name} has no state_segment rows, so no milepost can be "
            "converted to a measure. The corridor is half-loaded."
        )

    return CorridorConfig(
        route=route_name,
        centerline=coordinates,
        measures=measures,
        corridor_buffer_meters=float(buffer_meters),
        states=tuple(
            StateSegment(
                state=state,
                state_milepost_min=float(mp_min),
                state_milepost_max=float(mp_max),
                corridor_offset=float(offset),
            )
            for state, mp_min, mp_max, offset in segments
        ),
        verified=bool(verified),
    )


def available_routes(connection: Any | None = None) -> list[str]:
    """Every corridor in the database.

    The plural that ``config/corridor.json`` cannot express, and the reason this
    module exists at all.
    """
    connection = connection or dbconn.connect()
    return [r[0] for r in _fetch(connection, "SELECT route FROM corridor ORDER BY route")]


def _linestring_coords(wkt: str) -> list[tuple[float, float]]:
    """Coordinates out of ``LINESTRING(...)`` WKT.

    WKT rather than GeoJSON because ST_AsGeoJSON of an 11,873-vertex line is
    considerably larger over the wire and has to be JSON-parsed at the other end,
    for the same numbers.
    """
    return [(float(p[0]), float(p[1])) for p in _wkt_points(wkt)]


def _linestring_measures(wkt: str) -> tuple[list[tuple[float, float]], tuple[float, ...]]:
    """Coordinates and M values out of ``LINESTRING M (...)`` WKT."""
    points = _wkt_points(wkt)
    coords = [(float(p[0]), float(p[1])) for p in points]
    if any(len(p) < 3 for p in points):
        raise ValueError("centerline_m has a vertex with no M value")
    return coords, tuple(float(p[2]) for p in points)


def _wkt_points(wkt: str) -> list[list[str]]:
    inner = wkt[wkt.index("(") + 1 : wkt.rindex(")")]
    return [part.split() for part in inner.split(",") if part.strip()]


class PostgisConflator(Conflator):
    """Conflation executed by the database, against ``centerline_m``.

    One instance per route. The route is passed to every SQL function, so a second
    corridor is a second instance rather than a second deployment.

    ``connect`` is injectable so tests can supply a stub and so a caller that
    already holds a connection - the migration runner, a batch loader - does not
    open a second one.
    """

    def __init__(self, route: str, connect: Callable[[], Any] | None = None) -> None:
        self.route = route
        self._connect = connect or dbconn.connect

    @property
    def connection(self) -> Any:
        return self._connect()

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
            return self._unresolved(method="unresolved_text")
        return self._unresolved()

    # --- the four inputs -----------------------------------------------------

    def _from_milepost(self, spatial_input: MilepostInput) -> ConflationResult:
        """Milepost -> corridor measure, in SQL. Returns unresolved for an
        out-of-range milepost.

        ``milepost_to_measure`` returns NULL rather than clamping, and that
        NULL has to stay a mapping issue here rather than becoming measure 0 - which
        is a real place on the corridor.
        """
        begin = self._measure_for(spatial_input.state, spatial_input.begin_mp)
        if begin is None:
            return self._unresolved(method="milepost")
        end = begin
        if spatial_input.end_mp is not None:
            resolved = self._measure_for(spatial_input.state, spatial_input.end_mp)
            end = begin if resolved is None else resolved
        return self._result(begin, end, "milepost", accuracy=None, on_corridor=True)

    def _measure_for(self, state: str, milepost: float) -> float | None:
        rows = _fetch(
            self.connection,
            "SELECT milepost_to_measure(%s, %s, %s)",
            (self.route, state.upper(), milepost),
        )
        value = rows[0][0] if rows else None
        return None if value is None else float(value)

    def _from_coordinate(self, lon: float, lat: float) -> ConflationResult:
        """``ST_LineLocatePoint`` against the real centerline.

        The ::numeric casts are required: ST_X/ST_Y return double precision and
        conflate_point declares numeric parameters, and Postgres will not implicitly
        cast that direction when resolving a function call. Here the values are
        already Python floats bound as parameters, so the cast is on the parameter.
        """
        rows = _fetch(
            self.connection,
            "SELECT corridor_measure, offset_meters, on_corridor "
            "FROM conflate_point(%s, %s::numeric, %s::numeric)",
            (self.route, lon, lat),
        )
        if not rows or rows[0][0] is None:
            return self._unresolved(method="coordinate")
        measure, offset_meters, on_corridor = rows[0]
        return self._result(
            float(measure),
            float(measure),
            "coordinate",
            # Never claim better than the corridor's own geometry: report the real
            # off-corridor distance, floored, exactly as the local path does.
            accuracy=max(float(offset_meters or 0.0), _CALIBRATED_ACCURACY_METERS),
            on_corridor=bool(on_corridor),
        )

    def _from_linestring(self, coordinates) -> ConflationResult:
        """First and last vertex, same as the local path.

        Not ST_Intersection: an agency LineString is a description of an extent, and
        its endpoints are the extent. Intersecting it with the corridor would return
        the part that happens to be within a metre of the centerline, which for a
        parallel-running geometry is nothing.
        """
        if not coordinates:
            return self._unresolved()
        first = self._from_coordinate(coordinates[0][0], coordinates[0][1])
        last = self._from_coordinate(coordinates[-1][0], coordinates[-1][1])
        if not (first.on_corridor or last.on_corridor):
            return self._unresolved(method="linestring")
        begin = min(first.begin_measure, last.begin_measure)
        end = max(first.begin_measure, last.begin_measure)
        return self._result(
            begin,
            end,
            "linestring",
            accuracy=max(
                first.positional_accuracy_meters or 0.0, last.positional_accuracy_meters or 0.0
            ),
            on_corridor=True,
        )

    def _from_polygon(self, geometry) -> ConflationResult:
        """THE REASON TO PREFER THIS IMPLEMENTATION.

        ``ST_Intersection`` clips the actual centerline by the polygon and reads the
        measures off the ends. The in-process alternative samples 400 points and
        publishes a 3.1-mile accuracy floor; this has no sampling step, so a county
        polygon's extent on the corridor is exact to the geometry.
        """
        rows = _fetch(
            self.connection,
            "SELECT begin_measure, end_measure, on_corridor FROM conflate_polygon(%s, %s)",
            (self.route, _geojson_text(geometry)),
        )
        if not rows or rows[0][0] is None or not rows[0][2]:
            return self._unresolved(method="polygon_intersect")
        begin, end, _ = rows[0]
        return self._result(
            float(begin),
            float(end),
            "polygon_intersect",
            accuracy=_CALIBRATED_ACCURACY_METERS,
            on_corridor=True,
        )

    # --- shared ---------------------------------------------------------------

    def _result(
        self,
        begin: float,
        end: float,
        method: str,
        accuracy: float | None,
        on_corridor: bool,
    ) -> ConflationResult:
        return ConflationResult(
            begin_measure=round(begin, 3),
            end_measure=round(end, 3),
            states=self._states_for_range(begin, end) if on_corridor else [],
            method=method,
            positional_accuracy_meters=accuracy,
            on_corridor=on_corridor,
        )

    def _states_for_range(self, begin: float, end: float) -> list[str]:
        """Which states the extent touches, west to east.

        From ``state_segment``, so a corridor whose states change does not need a
        code change - which is the whole point of this class.
        """
        rows = _fetch(
            self.connection,
            """
            SELECT state FROM state_segment
            WHERE route = %s
              AND corridor_offset <= %s
              AND corridor_offset + (state_mp_max - state_mp_min) >= %s
            ORDER BY corridor_offset
            """,
            (self.route, max(begin, end), min(begin, end)),
        )
        return [r[0] for r in rows]

    def _unresolved(self, method: str = "unresolved") -> ConflationResult:
        return ConflationResult(
            begin_measure=0.0,
            end_measure=0.0,
            states=[],
            method=method,
            positional_accuracy_meters=None,
            on_corridor=False,
        )


class HybridConflator(Conflator):
    """Point conflation in process, polygon conflation in the database.

    THE SPLIT IS BY OPERATION, and each half is where the evidence puts it:

      COORDINATE, MILEPOST, LINESTRING -> in process. Both implementations read the
      same per-vertex LRS measures and agree to three decimals on the live cluster
      (803.482). The database adds no accuracy here and costs a network round trip
      PER RECORD, plus a write-path dependency on Aurora for every payload. A feed
      that can still be normalised while the cluster is restarting is worth more
      than a duplicate of a number we already have.

      POLYGON -> the database. ST_Intersection clips the actual centerline; the
      in-process path samples 400 points and publishes a 3.1-mile accuracy floor.
      That is the worst positional accuracy in the system, on the class whose
      extents are the largest. Weather alerts are also PER ALERT rather than per
      record, so the round trip is affordable in a way it is not on the hot path.

    Building this also caught the bug that made it worth building: conflate_polygon
    was deriving its measure from fraction-times-total_miles, 5.7 miles out, and
    nothing but a second implementation could have shown that.

    IF THE DATABASE IS UNREACHABLE the polygon path falls back to the in-process
    sampler rather than failing the payload. A 3.1-mile extent is worse than an
    exact one and far better than no event at all, and the degradation is logged so
    it cannot pass for normal.
    """

    def __init__(self, local: Conflator, remote: PostgisConflator) -> None:
        self.local = local
        self.remote = remote

    def conflate(self, spatial_input: SpatialInput) -> ConflationResult:
        if not isinstance(spatial_input, PolygonInput):
            return self.local.conflate(spatial_input)
        try:
            return self.remote.conflate(spatial_input)
        except Exception as exc:  # noqa: BLE001 - any DB failure, see the docstring
            print(
                json.dumps(
                    {
                        "msg": "polygon_conflation_fell_back_to_sampling",
                        "route": self.remote.route,
                        "error": f"{type(exc).__name__}: {exc}",
                        "consequence": "extent accurate to ~3.1 mi rather than exact",
                    }
                )
            )
            return self.local.conflate(spatial_input)

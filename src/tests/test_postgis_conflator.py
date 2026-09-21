"""PostgisConflator and the corridor read from the database.

WHY A STUB AND NOT A REAL CLUSTER: Aurora sits in an isolated subnet, so no test on
a laptop or in CI can reach it. What CAN be pinned here is the contract - the SQL
each input type issues, the route threaded through every call, and the handling of
the answers that matter (a NULL measure, an off-corridor point, an empty polygon
intersection). Whether the SQL is VALID is a different question, answered by
pglast against the real grammar and by running it against the live cluster.

WHAT THIS CLASS IS FOR, in one line: ``config/corridor.json`` can hold exactly one
route. The database is keyed by route everywhere, so a second corridor is a second
instance rather than a second deployment. Several tests below exist only to pin
that the route is actually threaded through and not assumed.
"""

from __future__ import annotations

import pytest

from corridor_event_hub.core.lrs import (
    ConflationResult,
    Conflator,
    CoordinateInput,
    LineStringInput,
    MilepostInput,
    PolygonInput,
    UnresolvedInput,
)
from corridor_event_hub.core.postgis import PostgisConflator, available_routes, load_corridor

POLYGON = {"type": "Polygon", "coordinates": [[[-102, 35], [-101, 35], [-101, 36], [-102, 36], [-102, 35]]]}

# A short LINESTRING M, as ST_AsText renders it. The real one is 350 KB.
CENTERLINE_M_WKT = "LINESTRING M (-114.491833 34.716954 0,-114.4 34.72 5.5,-114.3 34.73 11.25)"
CENTERLINE_WKT = "LINESTRING(-114.491833 34.716954,-114.4 34.72,-114.3 34.73)"


class _StubCursor:
    def __init__(self, connection):
        self.connection = connection
        self._rows = []

    def execute(self, sql, params=()):
        self.connection.executed.append((" ".join(sql.split()), params))
        self._rows = self.connection.answer(sql)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _StubConnection:
    """Answers by matching on the SQL, so a test states what the DB returns."""

    def __init__(self, **answers):
        self.answers = answers
        self.executed: list[tuple[str, tuple]] = []

    def answer(self, sql: str):
        for needle, rows in self.answers.items():
            if needle in sql:
                return rows
        return []

    def cursor(self):
        return _StubCursor(self)

    def commit(self):
        pass

    def close(self):
        pass

    def sql_containing(self, needle: str) -> list[tuple[str, tuple]]:
        return [(s, p) for s, p in self.executed if needle in s]


def conflator(connection) -> PostgisConflator:
    return PostgisConflator("I-40", connect=lambda: connection)


class TestSatisfiesTheSeam:
    def test_it_is_a_conflator(self):
        # ADR 0002's whole claim: swapping implementations is a constructor change.
        assert isinstance(conflator(_StubConnection()), Conflator)

    def test_every_input_type_returns_a_result_rather_than_raising(self):
        # An adapter must never see an exception from conflation - the mapping issue
        # is recorded, the invocation does not fail.
        c = conflator(_StubConnection())
        for spatial_input in (
            MilepostInput(state="NM", begin_mp=100),
            CoordinateInput(lon=-101.8, lat=35.2),
            LineStringInput(coordinates=[(-101.8, 35.2), (-101.7, 35.2)]),
            PolygonInput(geometry=POLYGON),
            UnresolvedInput(text="near the state line"),
        ):
            assert isinstance(c.conflate(spatial_input), ConflationResult)


class TestRouteIsThreadedThrough:
    """The multi-corridor property, pinned. Nothing may assume one route."""

    def test_the_route_is_a_parameter_to_every_query(self):
        connection = _StubConnection(**{"conflate_point": [(803.482, 839.4, True)]})
        PostgisConflator("US-66", connect=lambda: connection).conflate(
            CoordinateInput(lon=-101.83, lat=35.20)
        )
        assert connection.executed
        for _sql, params in connection.executed:
            assert params and params[0] == "US-66", "route must be bound, not baked in"

    def test_two_conflators_can_coexist(self):
        # The thing config/corridor.json structurally cannot do.
        connection = _StubConnection(**{"conflate_point": [(1.0, 0.0, True)]})
        a = PostgisConflator("I-40", connect=lambda: connection)
        b = PostgisConflator("I-10", connect=lambda: connection)
        a.conflate(CoordinateInput(lon=-101.0, lat=35.0))
        b.conflate(CoordinateInput(lon=-101.0, lat=35.0))
        routes = [params[0] for _sql, params in connection.executed if "conflate_point" in _sql]
        assert routes == ["I-40", "I-10"]


class TestCoordinate:
    def test_reads_measure_and_off_corridor_distance(self):
        connection = _StubConnection(**{"conflate_point": [(803.482, 839.4, True)]})
        result = conflator(connection).conflate(CoordinateInput(lon=-101.83, lat=35.20))
        assert result.begin_measure == 803.482
        assert result.on_corridor
        assert result.method == "coordinate"

    def test_casts_the_coordinates_to_numeric(self):
        # ST_X/ST_Y and Python floats both arrive as double precision, and
        # conflate_point declares numeric. Postgres will not implicitly cast that
        # direction, and the error reads "function does not exist".
        connection = _StubConnection(**{"conflate_point": [(1.0, 0.0, True)]})
        conflator(connection).conflate(CoordinateInput(lon=-101.0, lat=35.0))
        sql, _ = connection.sql_containing("conflate_point")[0]
        assert sql.count("::numeric") == 2

    def test_an_off_corridor_point_reports_no_states(self):
        # Claiming a state for a point outside the buffer would place an event on a
        # corridor it is not on.
        connection = _StubConnection(**{"conflate_point": [(803.482, 90000.0, False)]})
        result = conflator(connection).conflate(CoordinateInput(lon=-80.0, lat=40.0))
        assert not result.on_corridor
        assert result.states == []

    def test_accuracy_is_never_better_than_the_geometry(self):
        # A 0 m offset does not mean 0 m accuracy - the centerline itself is not that
        # good. Floored, exactly as the in-process path floors it.
        connection = _StubConnection(**{"conflate_point": [(500.0, 0.0, True)]})
        result = conflator(connection).conflate(CoordinateInput(lon=-101.0, lat=35.0))
        assert result.positional_accuracy_meters >= 50

    def test_a_null_measure_is_unresolved(self):
        connection = _StubConnection(**{"conflate_point": [(None, None, False)]})
        assert not conflator(connection).conflate(
            CoordinateInput(lon=0.0, lat=0.0)
        ).on_corridor


class TestMilepost:
    def test_converts_through_milepost_to_measure(self):
        connection = _StubConnection(**{"milepost_to_measure": [(459.349,)]})
        result = conflator(connection).conflate(MilepostInput(state="nm", begin_mp=100))
        assert result.begin_measure == 459.349
        assert result.method == "milepost"

    def test_upper_cases_the_state(self):
        connection = _StubConnection(**{"milepost_to_measure": [(1.0,)]})
        conflator(connection).conflate(MilepostInput(state="nm", begin_mp=5))
        _sql, params = connection.sql_containing("milepost_to_measure")[0]
        assert params[1] == "NM"

    def test_an_out_of_range_milepost_is_UNRESOLVED_not_zero(self):
        # milepost_to_measure returns NULL rather than clamping. Measure 0 is
        # a real place on the corridor, so a NULL that became 0 would silently put
        # every bad milepost at the western end.
        connection = _StubConnection(**{"milepost_to_measure": [(None,)]})
        result = conflator(connection).conflate(MilepostInput(state="AZ", begin_mp=9999))
        assert not result.on_corridor
        assert result.method == "milepost"


class TestPolygon:
    def test_uses_conflate_polygon_not_sampling(self):
        # The reason to prefer this implementation for the alert class.
        connection = _StubConnection(**{"conflate_polygon": [(782.215, 828.455, True)]})
        result = conflator(connection).conflate(PolygonInput(geometry=POLYGON))
        assert (result.begin_measure, result.end_measure) == (782.215, 828.455)
        assert result.method == "polygon_intersect"

    def test_sends_only_type_and_coordinates(self):
        # ST_GeomFromGeoJSON rejects a Feature. Anything extra on the geometry dict
        # has to be dropped here rather than at each call site.
        connection = _StubConnection(**{"conflate_polygon": [(1.0, 2.0, True)]})
        conflator(connection).conflate(
            PolygonInput(geometry={**POLYGON, "properties": {"x": 1}, "id": "abc"})
        )
        _sql, params = connection.sql_containing("conflate_polygon")[0]
        assert "properties" not in params[1] and "abc" not in params[1]
        assert '"type"' in params[1] and '"coordinates"' in params[1]

    def test_an_empty_intersection_is_unresolved(self):
        connection = _StubConnection(**{"conflate_polygon": [(None, None, False)]})
        assert not conflator(connection).conflate(PolygonInput(geometry=POLYGON)).on_corridor

    def test_a_geometry_that_is_not_a_polygon_is_unresolved(self):
        assert not conflator(_StubConnection()).conflate(
            PolygonInput(geometry={"type": "Point", "coordinates": [0, 0]})
        ).on_corridor


class TestStatesForRange:
    def test_a_multi_state_extent_lists_every_state_west_to_east(self):
        # One event with a multi-state extent, not two events.
        connection = _StubConnection(
            **{"conflate_point": [(500.0, 10.0, True)], "FROM state_segment": [("AZ",), ("NM",)]}
        )
        result = conflator(connection).conflate(CoordinateInput(lon=-109.0, lat=35.0))
        assert result.states == ["AZ", "NM"]

    def test_states_come_from_the_table_not_from_code(self):
        connection = _StubConnection(**{"conflate_point": [(1.0, 0.0, True)]})
        conflator(connection).conflate(CoordinateInput(lon=-101.0, lat=35.0))
        assert connection.sql_containing("FROM state_segment"), "must query, not hardcode"


class TestLoadCorridor:
    def _connection(self, **overrides):
        answers = {
            "FROM corridor WHERE route": [
                ("I-40", 1600, False, 1240.698, CENTERLINE_WKT, CENTERLINE_M_WKT)
            ],
            "FROM state_segment WHERE route": [
                ("AZ", 0, 359.349, 0.0),
                ("NM", 0, 373.309, 359.349),
            ],
        }
        answers.update(overrides)
        return _StubConnection(**answers)

    def test_returns_the_same_CorridorConfig_the_json_loader_returns(self):
        # The property that makes this a drop-in source: the in-process conflator can
        # be handed a corridor loaded from Postgres and behave identically.
        corridor = load_corridor("I-40", self._connection())
        assert corridor.route == "I-40"
        assert corridor.centerline == [
            (-114.491833, 34.716954),
            (-114.4, 34.72),
            (-114.3, 34.73),
        ]
        assert corridor.corridor_buffer_meters == 1600
        assert corridor.verified is False
        assert [s.state for s in corridor.states] == ["AZ", "NM"]

    def test_reads_the_M_values_off_centerline_m(self):
        corridor = load_corridor("I-40", self._connection())
        assert corridor.measures == (0.0, 5.5, 11.25)

    def test_no_centerline_m_means_no_measures_rather_than_wrong_ones(self):
        connection = self._connection(
            **{
                "FROM corridor WHERE route": [
                    ("I-40", 1600, False, 1240.698, CENTERLINE_WKT, None)
                ]
            }
        )
        assert load_corridor("I-40", connection).measures is None

    def test_a_length_mismatch_between_the_two_lines_RAISES(self):
        # Same rule as the JSON loader: a measures array of the wrong length
        # misplaces everything past the point where the two diverge, so it is worse
        # than having none.
        connection = self._connection(
            **{
                "FROM corridor WHERE route": [
                    (
                        "I-40",
                        1600,
                        False,
                        1240.698,
                        CENTERLINE_WKT,
                        "LINESTRING M (-114.491833 34.716954 0,-114.4 34.72 5.5)",
                    )
                ]
            }
        )
        with pytest.raises(ValueError, match="same line"):
            load_corridor("I-40", connection)

    def test_an_unknown_route_names_what_IS_there(self):
        # The usual cause is a cluster the migrations never reached, not a typo.
        connection = _StubConnection(
            **{"SELECT route FROM corridor ORDER BY route": [("I-10",)]}
        )
        with pytest.raises(LookupError, match="I-10"):
            load_corridor("I-40", connection)

    def test_a_corridor_with_no_state_segments_is_half_loaded_and_says_so(self):
        connection = self._connection(**{"FROM state_segment WHERE route": []})
        with pytest.raises(LookupError, match="no state_segment"):
            load_corridor("I-40", connection)

    def test_available_routes_is_the_plural_json_cannot_express(self):
        connection = _StubConnection(
            **{"SELECT route FROM corridor ORDER BY route": [("I-10",), ("I-40",)]}
        )
        assert available_routes(connection) == ["I-10", "I-40"]

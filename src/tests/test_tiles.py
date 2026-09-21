"""Tile coverage tests.

The property that matters is COVERAGE: a gap in the tile set is a stretch of
corridor where events silently do not exist. That failure is invisible - the
pipeline reports fewer events, not an error - so it is worth testing directly.
"""

from __future__ import annotations

from corridor_event_hub.core.lrs import corridor
from corridor_event_hub.core.tiles import TileAddress, lonlat_to_tile, tiles_covering_line


class TestLonLatToTile:
    """Tile math - lon/lat to tile address"""

    def test_zoom_zero_is_a_single_tile(self):
        assert lonlat_to_tile(-111.0, 35.0, 0) == (0, 0)

    def test_known_corridor_points(self):
        # Verified against the live tiles actually fetched from the service.
        assert lonlat_to_tile(-106.6504, 35.0844, 12) == (834, 1621)
        assert lonlat_to_tile(-111.6513, 35.1983, 12) == (777, 1619)

    def test_x_increases_eastward_and_y_increases_southward(self):
        west_x, _ = lonlat_to_tile(-114.0, 35.0, 8)
        east_x, _ = lonlat_to_tile(-95.0, 35.0, 8)
        assert east_x > west_x

        _, north_y = lonlat_to_tile(-105.0, 40.0, 8)
        _, south_y = lonlat_to_tile(-105.0, 30.0, 8)
        assert south_y > north_y

    def test_clamps_beyond_the_mercator_limit_rather_than_raising(self):
        # tan() diverges at the poles. No corridor reaches there, but a malformed
        # coordinate can, and an exception inside a fetch loop is a poor way to
        # discover a bad input.
        x, y = lonlat_to_tile(0.0, 89.9, 4)
        assert 0 <= x < 16
        assert 0 <= y < 16

    def test_never_returns_an_index_off_the_grid(self):
        # A point exactly on the antimeridian lands one tile past the edge.
        for zoom in (0, 1, 8, 12):
            x, y = lonlat_to_tile(180.0, -85.05112878, zoom)
            assert 0 <= x < 2**zoom
            assert 0 <= y < 2**zoom


class TestCorridorCoverage:
    """Tile math - corridor coverage"""

    def test_covers_the_corridor_without_gaps(self):
        # THE test in this file. The centerline has ~40 control points over ~1240
        # miles, so successive points are many tiles apart at z8 - per-vertex
        # tiling would leave most of the corridor uncovered. Every tile a
        # centerline point falls in must be in the result.
        tiles = set(tiles_covering_line(corridor.centerline, 8))
        for lon, lat in corridor.centerline:
            x, y = lonlat_to_tile(lon, lat, 8)
            assert TileAddress(8, x, y) in tiles

    def test_adjacent_tiles_form_a_connected_chain(self):
        # No holes between consecutive tiles: each must touch the previous one
        # (sharing an edge or a corner). A jump means an uncovered stretch.
        tiles = tiles_covering_line(corridor.centerline, 8)
        assert len(tiles) > 1
        for previous, current in zip(tiles, tiles[1:]):
            assert (
                abs(current.x - previous.x) <= 1 and abs(current.y - previous.y) <= 1
            ), f"gap between {previous} and {current}"

    def test_tile_count_is_economical_at_the_configured_zoom(self):
        # Cost is 4^zoom. The catalog picks z8 because it is the lowest zoom that
        # still carries mainline motorway flow; this pins the resulting request
        # count so a zoom change shows up as a test failure rather than a bill.
        #
        # 19, up from 17 against the old placeholder centerline. Not a regression:
        # a line that actually follows the road clips two more z8 tiles than one
        # drawn in 40 straight chords, which cut corners across tile boundaries.
        assert len(tiles_covering_line(corridor.centerline, 8)) == 19

    def test_padding_expands_coverage(self):
        plain = tiles_covering_line(corridor.centerline, 8)
        padded = tiles_covering_line(corridor.centerline, 8, pad=1)
        assert len(padded) > len(plain)
        assert set(plain).issubset(set(padded))

    def test_returns_no_duplicates(self):
        tiles = tiles_covering_line(corridor.centerline, 8, pad=1)
        assert len(tiles) == len(set(tiles))

    def test_empty_line_yields_no_tiles(self):
        assert tiles_covering_line([], 8) == []

    def test_single_point_yields_one_tile(self):
        assert len(tiles_covering_line([(-111.0, 35.0)], 8)) == 1

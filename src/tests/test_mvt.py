"""MVT decoder tests.

The decoder is hand-written (core/mvt.py explains why there is no dependency), so
it needs tests that would catch a wire-format mistake rather than tests that only
prove it runs. Two kinds here: hand-built byte sequences where the expected output
is known by construction, and the real captured tiles.

A protobuf bug is exactly the sort that produces plausible-looking coordinates in
the wrong place, which is undetectable downstream - a queue reported 40 miles from
where it is still looks like a queue.
"""

from __future__ import annotations

import base64
import json

from conftest import load_fixture
from corridor_event_hub.core.mvt import (
    _read_varint,
    _zigzag,
    decode_tile,
    tile_point_to_lonlat,
)


class TestWirePrimitives:
    """MVT decoder - protobuf wire primitives"""

    def test_single_byte_varint(self):
        assert _read_varint(b"\x08", 0) == (8, 1)

    def test_multi_byte_varint(self):
        # 300 = 0b100101100 -> 0xAC 0x02 little-endian base-128.
        assert _read_varint(b"\xac\x02", 0) == (300, 2)

    def test_zigzag_maps_alternating_signs(self):
        # The encoding that makes small negative deltas cheap. Getting this
        # backwards mirrors every geometry about its start point.
        assert [_zigzag(n) for n in (0, 1, 2, 3, 4)] == [0, -1, 1, -2, 2]

    def test_varint_rejects_truncation_rather_than_returning_a_partial_value(self):
        # A continuation bit with no following byte. Returning a half-read value
        # here would corrupt every subsequent field in the message.
        try:
            _read_varint(b"\x80", 0)
        except ValueError:
            return
        raise AssertionError("expected ValueError on truncated varint")


class TestProjection:
    """MVT decoder - tile pixel to lon/lat"""

    def test_tile_origin_is_the_northwest_corner(self):
        lon, lat = tile_point_to_lonlat(0, 0, 4096, 0, 0, 0)
        assert lon == -180.0
        assert lat > 85.0  # Mercator north limit

    def test_tile_center_at_zoom_zero_is_null_island(self):
        lon, lat = tile_point_to_lonlat(2048, 2048, 4096, 0, 0, 0)
        assert abs(lon) < 1e-9
        assert abs(lat) < 1e-9

    def test_round_trips_against_the_tile_the_point_came_from(self):
        # z8 tile 52/101 covers the corridor near Amarillo. A pixel in its middle
        # must land inside that tile's own bounds, which is the property that
        # catches an x/y or zoom mix-up.
        lon, lat = tile_point_to_lonlat(2048, 2048, 4096, 52, 101, 8)
        from corridor_event_hub.core.tiles import lonlat_to_tile

        assert lonlat_to_tile(lon, lat, 8) == (52, 101)


class TestRealTiles:
    """MVT decoder - real captured tiles (2026-08-11)"""

    def _tiles(self):
        return json.loads(load_fixture("aws-location-traffic.json"))["tiles"]

    def test_decodes_every_captured_tile(self):
        for tile in self._tiles():
            layers = decode_tile(
                base64.b64decode(tile["mvtBase64"]), tile["x"], tile["y"], tile["z"]
            )
            assert "traffic_flow" in layers

    def test_extracts_the_numeric_properties_that_make_this_source_worth_having(self):
        # speed and congestion are the reason this source exists. If the value
        # decoder regresses to returning None, the adapter still produces
        # candidates and nothing fails loudly - so assert it here.
        found_speed = False
        for tile in self._tiles():
            layers = decode_tile(
                base64.b64decode(tile["mvtBase64"]), tile["x"], tile["y"], tile["z"]
            )
            for feature in layers["traffic_flow"].features:
                speed = feature.properties.get("speed")
                if speed is not None:
                    assert isinstance(speed, (int, float))
                    assert 0 <= speed < 300  # km/h, sanity not precision
                    found_speed = True
        assert found_speed, "no speed values decoded from any tile"

    def test_coordinates_land_on_the_corridor_not_somewhere_plausible(self):
        # The failure mode this catches: a projection or tile-address bug puts
        # features in the right shape but the wrong hemisphere/state. The corridor
        # runs roughly -114.5 to -94.4 lon, 34.5 to 36.5 lat.
        for tile in self._tiles():
            layers = decode_tile(
                base64.b64decode(tile["mvtBase64"]), tile["x"], tile["y"], tile["z"]
            )
            for feature in layers["traffic_flow"].features:
                for lon, lat in feature.coordinates:
                    assert -118.0 < lon < -92.0, f"lon {lon} outside the corridor region"
                    assert 30.0 < lat < 40.0, f"lat {lat} outside the corridor region"

    def test_geometry_types_are_lines_or_points_never_unknown(self):
        for tile in self._tiles():
            layers = decode_tile(
                base64.b64decode(tile["mvtBase64"]), tile["x"], tile["y"], tile["z"]
            )
            for layer in layers.values():
                for feature in layer.features:
                    assert feature.geometry_type in ("Point", "LineString", "Polygon")

    def test_attribution_is_present_on_features(self):
        # Attribution is a licence obligation (see the catalog entry). If the feed
        # stops sending it, that is a compliance signal and this test is where it
        # surfaces.
        layers = None
        for tile in self._tiles():
            layers = decode_tile(
                base64.b64decode(tile["mvtBase64"]), tile["x"], tile["y"], tile["z"]
            )
            if layers["traffic_flow"].features:
                break
        assert layers is not None
        assert "HERE" in layers["traffic_flow"].features[0].properties["source"]

    def test_malformed_bytes_do_not_raise(self):
        # A truncated tile is a normal network outcome. decode_tile is allowed to
        # return nothing, but must not explode - the adapter turns an empty result
        # into a mapping issue.
        raw = base64.b64decode(self._tiles()[0]["mvtBase64"])
        try:
            decode_tile(raw[: len(raw) // 3], 52, 101, 8)
        except ValueError:
            pass  # a declared decode failure is fine; a crash is not
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"unexpected {type(exc).__name__}: {exc}") from exc

    def test_empty_input_is_empty_output(self):
        assert decode_tile(b"", 0, 0, 0) == {}


class TestNoDependencyClaim:
    """MVT decoder - the no-dependency claim in core/mvt.py"""

    def test_decoder_imports_nothing_outside_the_standard_library(self):
        # The module's whole justification is that it adds nothing to the Lambda
        # bundle. That claim silently rots the moment someone adds an import.
        #
        # Parsed with ast rather than matched by line prefix: the module is heavily
        # commented, and prose like "from the wire format alone" reads as an import
        # statement to a string match. Same reasoning as check-portability.sh using
        # Python's own tokenizer instead of grep.
        import ast
        import pathlib

        tree = ast.parse(
            (pathlib.Path(__file__).parent.parent / "corridor_event_hub/core/mvt.py").read_text()
        )
        allowed = {
            "math",
            "struct",
            "dataclasses",
            "typing",
            "collections",
            "__future__",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in allowed, f"unexpected import: {alias.name}"
            # level > 0 is a relative import inside this package, which costs the
            # bundle nothing.
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                root = node.module.split(".")[0]
                assert root in allowed, f"unexpected import from: {node.module}"

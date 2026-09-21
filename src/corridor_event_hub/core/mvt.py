"""Mapbox Vector Tile decoder - protobuf wire format, standard library only.

WHY THIS EXISTS RATHER THAN A DEPENDENCY: the pipeline's one compiled dependency
is shapely (see pyproject.toml on why that is deliberate), and every addition to
the Lambda bundle has to be cross-built for linux/aarch64 by
scripts/build-lambda.sh. ``mapbox-vector-tile`` pulls in protobuf and
``protobuf`` ships platform wheels, so adding it doubles the number of things
that can fail at INVOKE time rather than build time - the expensive place.

The MVT format is small enough that decoding it is less code than the machinery
needed to depend on someone else's decoder. This module reads the ~6 protobuf
wire constructs the spec actually uses and nothing else.

Spec: https://github.com/mapbox/vector-tile-spec/tree/master/2.1

WHAT THIS DOES NOT DO: polygon winding rules, ClosePath geometry, or feature
``id`` deduplication across layers. Tiles this pipeline reads carry LineStrings
and Points. A polygon layer would decode as its exterior ring, which is wrong in
the general case and is why ``decode_tile`` reports the geometry type rather than
silently normalizing it.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

# Wire types (protobuf spec).
_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LENGTH_DELIMITED = 2
_WIRE_32BIT = 5

# Geometry commands (MVT spec 4.3.3).
_CMD_MOVE_TO = 1
_CMD_LINE_TO = 2
_CMD_CLOSE_PATH = 7

# MVT geometry type enum (spec 4.3.4). 0 is UNKNOWN.
GEOMETRY_TYPES = {1: "Point", 2: "LineString", 3: "Polygon"}

# Tiles declare their own extent; 4096 is the near-universal default.
_DEFAULT_EXTENT = 4096


@dataclass
class TileFeature:
    """One decoded feature. ``coordinates`` is lon/lat, GeoJSON order."""

    geometry_type: str
    coordinates: list[tuple[float, float]]
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class TileLayer:
    name: str
    extent: int
    features: list[TileFeature] = field(default_factory=list)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read one base-128 varint. Returns ``(value, new_pos)``."""
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _zigzag(value: int) -> int:
    """Undo protobuf zigzag encoding, used for sint32/sint64 and MVT deltas."""
    return (value >> 1) ^ -(value & 1)


def _iter_fields(buf: bytes) -> Iterator[tuple[int, int, Any]]:
    """Walk a protobuf message, yielding ``(field_number, wire_type, value)``.

    Length-delimited payloads come back as raw ``bytes`` for the caller to
    interpret - a nested message, a string, or a packed scalar array, none of
    which are distinguishable from the wire format alone.
    """
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = _read_varint(buf, pos)
        field_number, wire_type = key >> 3, key & 0x07

        if wire_type == _WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
            yield field_number, wire_type, value
        elif wire_type == _WIRE_LENGTH_DELIMITED:
            length, pos = _read_varint(buf, pos)
            if pos + length > end:
                raise ValueError("truncated length-delimited field")
            yield field_number, wire_type, buf[pos : pos + length]
            pos += length
        elif wire_type == _WIRE_32BIT:
            yield field_number, wire_type, struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif wire_type == _WIRE_64BIT:
            yield field_number, wire_type, struct.unpack_from("<d", buf, pos)[0]
            pos += 8
        else:
            # Wire types 3 and 4 are deprecated groups; MVT never emits them.
            raise ValueError(f"unsupported protobuf wire type {wire_type}")


def _decode_value(buf: bytes) -> Any:
    """A ``Tile.Value`` - exactly one of seven typed fields is set (spec 4.1)."""
    for field_number, _wire, value in _iter_fields(buf):
        if field_number == 1:
            return value.decode("utf-8", errors="replace")
        if field_number in (2, 3):  # float, double
            return value
        if field_number in (4, 5):  # int64, uint64
            return value
        if field_number == 6:  # sint64
            return _zigzag(value)
        if field_number == 7:  # bool
            return bool(value)
    return None


def _unpack_varints(buf: bytes) -> list[int]:
    out: list[int] = []
    pos = 0
    while pos < len(buf):
        value, pos = _read_varint(buf, pos)
        out.append(value)
    return out


def _decode_geometry(
    commands: list[int],
    extent: int,
    tile_x: int,
    tile_y: int,
    zoom: int,
) -> list[tuple[float, float]]:
    """Turn the command/parameter stream into lon/lat pairs.

    Deltas accumulate across commands, so a MoveTo following a LineTo continues
    from the current cursor. Rings and multi-part geometries are flattened into
    one coordinate list: the traffic layers this decodes are single-part
    LineStrings and Points, and flattening keeps the conflation input simple.
    ``decode_tile`` reports the declared geometry type so a caller can tell when
    that assumption stops holding.
    """
    coords: list[tuple[float, float]] = []
    cursor_x = 0
    cursor_y = 0
    index = 0
    total = len(commands)

    while index < total:
        command_integer = commands[index]
        index += 1
        command_id = command_integer & 0x07
        count = command_integer >> 3

        if command_id == _CMD_CLOSE_PATH:
            # Closes the current ring by repeating its first point. Meaningless
            # for the line geometry here, and harmful if it appends a duplicate.
            continue

        if command_id not in (_CMD_MOVE_TO, _CMD_LINE_TO):
            raise ValueError(f"unknown MVT geometry command {command_id}")

        for _ in range(count):
            if index + 1 >= total:
                raise ValueError("truncated MVT geometry parameters")
            cursor_x += _zigzag(commands[index])
            cursor_y += _zigzag(commands[index + 1])
            index += 2
            coords.append(tile_point_to_lonlat(cursor_x, cursor_y, extent, tile_x, tile_y, zoom))

    return coords


def tile_point_to_lonlat(
    px: float,
    py: float,
    extent: int,
    tile_x: int,
    tile_y: int,
    zoom: int,
) -> tuple[float, float]:
    """Tile-local pixel -> lon/lat (Web Mercator, EPSG:3857 -> EPSG:4326)."""
    scale = extent * (2.0**zoom)
    world_x = (tile_x * extent + px) / scale
    world_y = (tile_y * extent + py) / scale

    lon = world_x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * world_y))))
    return lon, lat


def decode_tile(raw: bytes, tile_x: int, tile_y: int, zoom: int) -> dict[str, TileLayer]:
    """Decode one vector tile into layers keyed by name.

    ``tile_x``/``tile_y``/``zoom`` are required because a tile's coordinates are
    local to itself - the bytes carry no georeference at all. Passing the wrong
    tile address yields plausible coordinates in the wrong place, which is why
    the caller that fetched the tile is the one that supplies them.
    """
    layers: dict[str, TileLayer] = {}

    for field_number, _wire, value in _iter_fields(raw):
        if field_number != 3:  # Tile.layers
            continue

        name = ""
        extent = _DEFAULT_EXTENT
        keys: list[str] = []
        values: list[Any] = []
        raw_features: list[bytes] = []

        for layer_field, _lwire, layer_value in _iter_fields(value):
            if layer_field == 1:  # name
                name = layer_value.decode("utf-8", errors="replace")
            elif layer_field == 2:  # features
                raw_features.append(layer_value)
            elif layer_field == 3:  # keys
                keys.append(layer_value.decode("utf-8", errors="replace"))
            elif layer_field == 4:  # values
                values.append(_decode_value(layer_value))
            elif layer_field == 5:  # extent
                extent = layer_value or _DEFAULT_EXTENT

        layer = TileLayer(name=name, extent=extent)

        for raw_feature in raw_features:
            tags: list[int] = []
            geometry_type = "Unknown"
            geometry_commands: list[int] = []

            for feature_field, _fwire, feature_value in _iter_fields(raw_feature):
                if feature_field == 2:  # tags, packed
                    tags.extend(_unpack_varints(feature_value))
                elif feature_field == 3:  # type enum
                    geometry_type = GEOMETRY_TYPES.get(feature_value, "Unknown")
                elif feature_field == 4:  # geometry, packed
                    geometry_commands.extend(_unpack_varints(feature_value))

            properties: dict[str, Any] = {}
            # Tags are (key_index, value_index) pairs into the layer dictionaries.
            # A malformed pair is skipped rather than raising: one bad feature
            # must not cost the whole tile.
            for i in range(0, len(tags) - 1, 2):
                key_index, value_index = tags[i], tags[i + 1]
                if 0 <= key_index < len(keys) and 0 <= value_index < len(values):
                    properties[keys[key_index]] = values[value_index]

            try:
                coordinates = _decode_geometry(
                    geometry_commands, extent, tile_x, tile_y, zoom
                )
            except ValueError:
                # Undecodable geometry: keep the feature so the adapter can report
                # it as a mapping issue rather than dropping it silently.
                coordinates = []

            layer.features.append(
                TileFeature(
                    geometry_type=geometry_type,
                    coordinates=coordinates,
                    properties=properties,
                )
            )

        layers[name] = layer

    return layers

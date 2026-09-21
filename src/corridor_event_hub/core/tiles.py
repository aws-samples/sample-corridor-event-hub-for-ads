"""Slippy-map tile arithmetic - which tiles cover the corridor.

Tiled sources invert the usual fetch model. Every other feed in this pipeline
answers "give me your records"; a tile source answers "give me what is inside
this square", so the adapter has to work out which squares the corridor touches
before it can fetch anything.

That calculation is pure arithmetic over the configured centerline, so it lives
in core rather than in an adapter: it names no state, no agency, and no route.
Any tiled source on any corridor uses the same function.

WHY ZOOM MATTERS MORE THAN IT LOOKS: tile count grows as 4^zoom, and so does
cost. Going from z8 to z12 for the same corridor is a ~250x increase in requests
for geometry that is already accurate to well under the corridor buffer. The
sweet spot is the LOWEST zoom at which the source still publishes the features
you need - which is a property of the source, not of this module, so it is a
parameter.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .geo import Coord


@dataclass(frozen=True)
class TileAddress:
    """One slippy-map tile. ``z/x/y``, XYZ scheme (y=0 at the north edge)."""

    z: int
    x: int
    y: int

    def __str__(self) -> str:  # handy in log lines and issue details
        return f"{self.z}/{self.x}/{self.y}"


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Web Mercator lon/lat -> tile x/y at ``zoom``.

    Latitude is clamped to the Mercator limit (~85.051deg): beyond it the
    projection diverges and ``tan`` blows up. No road corridor reaches it, but a
    malformed coordinate can, and a ValueError deep in a fetch loop is a poor way
    to learn that.
    """
    n = 2.0**zoom
    clamped_lat = max(min(lat, 85.05112878), -85.05112878)
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(clamped_lat))) / math.pi) / 2.0 * n)
    # A point exactly on the antimeridian or pole lands one tile past the grid.
    max_index = int(n) - 1
    return max(0, min(x, max_index)), max(0, min(y, max_index))


def tiles_covering_line(
    coordinates: Sequence[Coord],
    zoom: int,
    *,
    pad: int = 0,
) -> list[TileAddress]:
    """Every tile the polyline passes through, in west-to-east order.

    Consecutive vertices are joined by walking the tiles between them, so a long
    segment spanning several tiles does not leave gaps.

    THE WALK IS STILL NEEDED, though no longer for the original reason. It was
    written for a 40-point centerline whose successive vertices were often many
    tiles apart, where naive per-vertex tiling would have missed most of the
    corridor. Real LRS geometry averages 0.07 mi between vertices, well under a
    tile at the zooms used here, so consecutive hits are now usually adjacent -
    but the walk is what guarantees that rather than assumes it, and it costs
    nothing when the gap is zero.

    ``pad`` adds a ring of neighbouring tiles around each hit. Useful when the
    corridor buffer is wide relative to a tile: a feature just over the edge is
    otherwise invisible.
    """
    if not coordinates:
        return []

    seen: dict[tuple[int, int], None] = {}
    ordered: list[tuple[int, int]] = []

    def add(x: int, y: int) -> None:
        for dx in range(-pad, pad + 1):
            for dy in range(-pad, pad + 1):
                key = (x + dx, y + dy)
                if key[0] < 0 or key[1] < 0:
                    continue
                if key not in seen:
                    seen[key] = None
                    ordered.append(key)

    first = lonlat_to_tile(coordinates[0][0], coordinates[0][1], zoom)
    add(*first)

    for index in range(1, len(coordinates)):
        start = lonlat_to_tile(
            coordinates[index - 1][0], coordinates[index - 1][1], zoom
        )
        end = lonlat_to_tile(coordinates[index][0], coordinates[index][1], zoom)
        for x, y in _walk_tiles(start, end):
            add(x, y)

    return [TileAddress(z=zoom, x=x, y=y) for x, y in ordered]


def _walk_tiles(
    start: tuple[int, int], end: tuple[int, int]
) -> Iterable[tuple[int, int]]:
    """Tiles along a straight line between two tile addresses.

    A Bresenham-style walk in tile space. This is an approximation of the true
    great-circle path, and deliberately so: at the zooms a corridor feed uses,
    the error is a fraction of a tile, and ``pad`` covers it. The alternative -
    densifying the geodesic then tiling each point - costs more code to fix an
    error smaller than a tile.

    Real LRS geometry made this cheaper rather than more urgent: vertices now sit
    0.07 mi apart on average, so most walks are a single step and the straight-line
    approximation has almost no distance over which to diverge.
    """
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    dy = abs(y1 - y0)
    step_x = 1 if x1 >= x0 else -1
    step_y = 1 if y1 >= y0 else -1
    error = dx - dy

    x, y = x0, y0
    while True:
        yield x, y
        if x == x1 and y == y1:
            return
        doubled = error * 2
        if doubled > -dy:
            error -= dy
            x += step_x
        if doubled < dx:
            error += dx
            y += step_y

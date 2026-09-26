"""Coastline geometry for the static map, fetched once and embedded.

The page needs geographic context: a scatter of dots in an empty rectangle shows
clustering and nothing else, and it is impossible to tell which dot is near the
water or which side of town it is on.

Real map tiles are still ruled out. OpenStreetMap's tile usage policy forbids
systematic use by third-party sites, and a published GitHub Pages page pulling
tiles on every view is exactly that. Coastline *geometry* is a different thing: it
is an ordinary ODbL data extract, fetched once at build time, simplified, and
written into the HTML. No runtime requests, nothing to rate-limit, works offline.
The attribution the licence requires is already in the footer.
"""

from __future__ import annotations

from . import osm

# Coastlines do not move. Cache effectively forever.
CACHE_DAYS = 3650

# Above this the fetch covers too much of the globe to be either quick or useful.
MAX_SPAN_DEG = 3.0


def _perpendicular_distance(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    (x, y), (x1, y1), (x2, y2) = point, start, end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
    # Twice the triangle area over the base length.
    return abs(dy * x - dx * y + x2 * y1 - y2 * x1) / (dx * dx + dy * dy) ** 0.5


def simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Douglas-Peucker, iterative so a long coastline can't blow the stack.

    Preferred over dropping every nth point, which cuts detail where the line is
    busy and wastes budget where it is straight. Tolerance is in the same units as
    the points, so callers pass a value derived from what one pixel is worth.
    """
    if len(points) < 3:
        return points

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst, index = tolerance, -1
        for i in range(first + 1, last):
            d = _perpendicular_distance(points[i], points[first], points[last])
            if d > worst:
                worst, index = d, i
        if index != -1:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))

    return [p for p, k in zip(points, keep) if k]


def coastline(
    south: float, west: float, north: float, east: float, tolerance_deg: float
) -> list[list[tuple[float, float]]]:
    """Coastline ways crossing a bounding box, as simplified (lat, lon) paths.

    Returns an empty list rather than raising if OpenStreetMap is unreachable: the
    basemap is decoration, and `build` must keep working offline.
    """
    if (north - south) > MAX_SPAN_DEG or (east - west) > MAX_SPAN_DEG:
        return []

    query = (
        f"[out:json][timeout:120];\n"
        f'way["natural"="coastline"]({south:.4f},{west:.4f},{north:.4f},{east:.4f});\n'
        f"out geom;"
    )
    try:
        data = osm.request(
            "overpass",
            "https://overpass-api.de/api/interpreter",
            method="POST",
            data=query,
            max_age_days=CACHE_DAYS,
            timeout=180.0,
            attempts=2,
        )
    except (osm.TransientError, osm.OSMError, osm.MissingContactError):
        return []

    paths: list[list[tuple[float, float]]] = []
    for way in data.get("elements", []):
        geometry = way.get("geometry") or []
        points = [(g["lat"], g["lon"]) for g in geometry if "lat" in g and "lon" in g]
        if len(points) < 2:
            continue
        simplified = simplify(points, tolerance_deg)
        if len(simplified) >= 2:
            paths.append(simplified)
    return paths

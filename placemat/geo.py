"""Distance, projection, and OpenStreetMap links. Stdlib only.

haversine_km is the *only* location-matching primitive in this codebase. Zip
strings are never compared: many venues have no addr:postcode, and a
reverse-geocoded one is approximate enough to be wrong (Zuni Cafe came back as
94143 against a true 94102).
"""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088

OSM_BASE = "https://www.openstreetmap.org"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def format_km(km: float) -> str:
    return f"{km:.1f}km"


def project(
    points: list[tuple[float, float]], width: float, height: float, pad: float = 8.0
) -> list[tuple[float, float]]:
    """Equirectangular projection into an SVG box, x scaled by cos(mean latitude).

    Fine at city scale, which is all we ever draw; wrong at continental scale.
    Returns (x, y) pairs with y already flipped for SVG's downward axis.
    """
    if not points:
        return []

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    mean_lat = sum(lats) / len(lats)
    xscale = math.cos(math.radians(mean_lat)) or 1.0

    xs = [lon * xscale for lon in lons]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(lats), max(lats)

    span_x = (max_x - min_x) or 1e-6
    span_y = (max_y - min_y) or 1e-6
    # One scale for both axes keeps the aspect ratio honest.
    scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)

    # Centre whatever slack the shared scale leaves over.
    off_x = (width - span_x * scale) / 2
    off_y = (height - span_y * scale) / 2

    out = []
    for lat, lon in points:
        x = (lon * xscale - min_x) * scale + off_x
        y = height - ((lat - min_y) * scale + off_y)  # SVG y grows downward
        out.append((x, y))
    return out


def scale_bar_km(points: list[tuple[float, float]]) -> float:
    """A round number of km that spans roughly a quarter of the plotted width."""
    if len(points) < 2:
        return 1.0
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    span = haversine_km(min(lats), min(lons), min(lats), max(lons))
    target = max(span / 4, 0.1)
    for step in (0.5, 1, 2, 5, 10, 20, 50, 100):
        if step >= target:
            return float(step)
    return 100.0


def osm_element_url(osm_type: str | None, osm_id: int | None) -> str | None:
    if not osm_type or not osm_id:
        return None
    return f"{OSM_BASE}/{osm_type}/{osm_id}"


def osm_point_url(lat: float, lon: float, zoom: int = 18) -> str:
    return f"{OSM_BASE}/?mlat={lat:.5f}&mlon={lon:.5f}#map={zoom}/{lat:.5f}/{lon:.5f}"

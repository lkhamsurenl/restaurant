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


class Viewport:
    """Maps lat/lon onto an SVG box sized to fit the data.

    Replaces forcing every map into one fixed rectangle. The restaurants in a city
    span roughly as far north-south as east-west, so squeezing them into a wide box
    left a third of the width empty while the points crowded the middle. Here the
    box takes its shape from the data, within limits so one outlier cannot produce
    a sliver.

    A minimum span also matters: nine places inside half a kilometre would
    otherwise zoom until the map implied a precision the dots do not have.
    """

    MIN_SPAN_DEG = 0.02  # roughly 2km, so a tight cluster doesn't zoom absurdly
    MIN_ASPECT, MAX_ASPECT = 0.5, 1.6

    def __init__(
        self,
        points: list[tuple[float, float]],
        width: float = 560.0,
        pad_fraction: float = 0.12,
        margin: float = 14.0,
    ) -> None:
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        mean_lat = sum(lats) / len(lats)
        self._xscale = math.cos(math.radians(mean_lat)) or 1.0

        south, north = min(lats), max(lats)
        west, east = min(lons), max(lons)

        # Pad outwards so nothing sits on the frame, then enforce a floor.
        span_lat = max((north - south) * (1 + 2 * pad_fraction), self.MIN_SPAN_DEG)
        span_lon = max(
            (east - west) * (1 + 2 * pad_fraction), self.MIN_SPAN_DEG / self._xscale
        )
        mid_lat, mid_lon = (north + south) / 2, (east + west) / 2

        self.south, self.north = mid_lat - span_lat / 2, mid_lat + span_lat / 2
        self.west, self.east = mid_lon - span_lon / 2, mid_lon + span_lon / 2

        # Ground width and height of the view, in comparable units.
        ground_w = span_lon * self._xscale
        ground_h = span_lat
        aspect = min(max(ground_h / ground_w, self.MIN_ASPECT), self.MAX_ASPECT)

        self.width = width
        self.height = width * aspect
        self.margin = margin

        inner_w = self.width - 2 * margin
        inner_h = self.height - 2 * margin
        # One scale for both axes keeps the shape honest; whichever axis binds wins.
        self.scale = min(inner_w / ground_w, inner_h / ground_h)
        self._off_x = (self.width - ground_w * self.scale) / 2
        self._off_y = (self.height - ground_h * self.scale) / 2

    def xy(self, lat: float, lon: float) -> tuple[float, float]:
        x = (lon - self.west) * self._xscale * self.scale + self._off_x
        y = self.height - ((lat - self.south) * self.scale + self._off_y)
        return x, y

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east

    @property
    def tolerance_deg(self) -> float:
        """Simplification tolerance worth about half a pixel at this scale."""
        return 0.5 / self.scale

    @property
    def km_per_px(self) -> float:
        return haversine_km(self.south, self.west, self.south, self.east) / max(
            (self.east - self.west) * self._xscale * self.scale, 1e-9
        )


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

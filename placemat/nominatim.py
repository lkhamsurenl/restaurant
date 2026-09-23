"""Nominatim: turn a place name into a point, and a point back into labels.

Used only for geocoding areas and filling in labels. Restaurant *name* lookup goes
through Overpass instead, because Nominatim's free-text search is unreliable for
venue names: q="Zuni Cafe, San Francisco" returns zero results, since OSM stores
the name with an acute e.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from . import osm

BASE = "https://nominatim.openstreetmap.org"

# A geocode never goes stale: a zip's centroid does not move.
_CACHE_FOREVER = None

_ZIP = re.compile(r"^\d{5}(-\d{4})?$")

# Resolving one of these to a point and drawing a radius around it produces a
# circle in a random field, and nothing downstream can tell that it happened.
_TOO_COARSE = {"country", "state", "region", "continent"}


class Place(BaseModel):
    """A geocoded search area."""

    query: str
    label: str
    lat: float
    lon: float
    kind: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None


class ReverseResult(BaseModel):
    """Labels derived from a coordinate.

    Approximate, and verifiably so: reversing Zuni Cafe's coordinates returns
    postcode 94143 when the true zip is 94102, and suburb "Mission" for an address
    in Hayes Valley. Every consumer treats these as display labels. No code path
    matches on them.
    """

    city: str | None = None
    state: str | None = None
    neighbourhood: str | None = None
    zip: str | None = None
    road: str | None = None
    house_number: str | None = None

    @property
    def address(self) -> str | None:
        if self.road and self.house_number:
            return f"{self.house_number} {self.road}"
        return self.road


def _city_of(address: dict[str, Any]) -> str | None:
    """The most specific settlement name available.

    County is last and deliberately so: a US zip often has no city/town field, and
    falling straight to "Honolulu County" labels a Kapolei restaurant with the name
    of the whole island's county.
    """
    for field in ("city", "town", "village", "municipality", "hamlet", "suburb", "county"):
        if address.get(field):
            return address[field]
    return None


def _neighbourhood_of(address: dict[str, Any]) -> str | None:
    for field in ("neighbourhood", "quarter", "suburb", "city_district"):
        if address.get(field):
            return address[field]
    return None


def _strategies(query: str, country: str) -> list[dict[str, Any]]:
    """Query shapes to try, most precise first.

    Ordering matters, and so does `countrycodes`. A bare US zip is globally
    ambiguous: without the country filter, "94110" resolves to Arcueil, France,
    and OSM's own boundary=postal_code areas make the same mistake. So a zip is
    always sent through the dedicated `postalcode` field with the country pinned.
    """
    base: dict[str, Any] = {"format": "jsonv2", "addressdetails": 1, "limit": 1}
    shapes: list[dict[str, Any]] = []

    if _ZIP.match(query):
        shapes.append({"postalcode": query[:5], "countrycodes": country or "us"})

    if country:
        shapes.append({"city": query, "countrycodes": country})
        shapes.append({"q": query, "countrycodes": country})
    else:
        # Only reachable when the caller explicitly opts out of the country filter,
        # so the default path can never reopen the ambiguity above.
        shapes.append({"q": query})

    return [{**base, **shape} for shape in shapes]


def geocode(query: str, country: str = "us") -> Place | None:
    """Resolve a zip, city, or neighbourhood to a point. None if nothing credible."""
    for params in _strategies(query, country):
        results = osm.request(
            "nominatim", f"{BASE}/search", params=params, max_age_days=_CACHE_FOREVER
        )
        if not results:
            continue

        found = results[0]
        kind = found.get("addresstype")
        if kind in _TOO_COARSE:
            raise osm.OSMError(
                f"{query!r} resolves to a whole {kind} ({found.get('display_name')}). "
                "That is too coarse to search around - name a city, "
                "neighbourhood, or zip instead."
            )

        address = found.get("address") or {}
        return Place(
            query=query,
            label=found.get("display_name", query),
            lat=float(found["lat"]),
            lon=float(found["lon"]),
            kind=kind,
            city=_city_of(address),
            state=address.get("state"),
            zip=address.get("postcode"),
        )
    return None


_US_TAIL = re.compile(r",\s*([^,]+?),\s*([A-Za-z]{2})\.?\s*(\d{5})?(?:-\d{4})?\s*$")

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def parse_us_address(address: str) -> tuple[str | None, str | None, str | None]:
    """Pull (city, state, zip) out of a written US address.

    Preferred over Nominatim's own labels when the user typed the address, because
    the user's version is the one they recognize: geocoding "1000 Auahi St,
    Honolulu, HI 96814" returns city "East Honolulu", a census designation nobody
    would use for a Ward Village restaurant.
    """
    match = _US_TAIL.search(address.strip())
    if not match:
        return None, None, None
    city, abbrev, zipcode = match.groups()
    return city.strip(), STATES.get(abbrev.upper(), abbrev.upper()), zipcode


_HAWAII_NUMBER = re.compile(r"^\s*\d{1,3}-(\d+)\b")
_UNIT = re.compile(
    r",?\s*\b(?:apt|unit|ste|suite|#|fl|floor|ground floor|bldg)\b[^,]*", re.IGNORECASE
)


def _address_variants(address: str) -> list[str]:
    """Progressively coarser forms of an address to try in turn.

    Written addresses defeat a geocoder in predictable ways, so each variant drops
    the part most likely to be the obstacle while keeping enough to stay precise:

    - Hawaii's hyphenated house numbers ("92-1220 Aliinui Dr") are not in
      Nominatim's index in that form, though the plain number often is.
    - Unit and suite fragments ("Apt 100", "Ground Floor") are noise to a geocoder.
    - Failing both, the street without a number still lands on the right block, and
      the city and zip alone still land in the right town.
    """
    address = address.strip()
    variants = [address]

    stripped = _UNIT.sub("", address)
    if stripped != address:
        variants.append(stripped)

    hawaii = _HAWAII_NUMBER.sub(lambda m: m.group(1), stripped)
    if hawaii != stripped:
        variants.append(hawaii)

    parts = [p.strip() for p in stripped.split(",") if p.strip()]
    if len(parts) >= 2:
        street = re.sub(r"^[\d-]+\s+", "", parts[0])
        if street and street != parts[0]:
            variants.append(", ".join([street] + parts[1:]))
        # Last resort: the town and zip, which at least puts it in the right place.
        variants.append(", ".join(parts[1:]))

    seen: set[str] = set()
    return [v for v in variants if v and not (v in seen or seen.add(v))]


def geocode_address(address: str, country: str = "us") -> Place | None:
    """Resolve a full street address to a point.

    Much faster and far more reliable than searching for a venue by name: an
    address lookup is one indexed Nominatim query taking about a second, where a
    name search is an unindexed Overpass scan of every venue in the radius. Newer
    or smaller restaurants are frequently absent from OSM entirely while their
    street address resolves fine.
    """
    found = None
    for attempt in _address_variants(address):
        results = osm.request(
            "nominatim",
            f"{BASE}/search",
            params={
                "q": attempt,
                "format": "jsonv2",
                "addressdetails": 1,
                "limit": 1,
                **({"countrycodes": country} if country else {}),
            },
            max_age_days=_CACHE_FOREVER,
        )
        if results:
            found = results[0]
            break

    if found is None:
        return None
    written_city, written_state, written_zip = parse_us_address(address)
    resolved = found.get("address") or {}
    return Place(
        query=address,
        label=found.get("display_name", address),
        lat=float(found["lat"]),
        lon=float(found["lon"]),
        kind=found.get("addresstype"),
        city=written_city or _city_of(resolved),
        state=written_state or resolved.get("state"),
        zip=written_zip or resolved.get("postcode"),
    )


def reverse(lat: float, lon: float) -> ReverseResult:
    """Fill city/state/neighbourhood/zip from a coordinate. Approximate - see the class."""
    found = osm.request(
        "nominatim",
        f"{BASE}/reverse",
        params={
            "lat": lat,
            "lon": lon,
            "format": "jsonv2",
            "addressdetails": 1,
            "zoom": 18,
        },
        max_age_days=_CACHE_FOREVER,
    )
    address = (found or {}).get("address") or {}
    return ReverseResult(
        city=_city_of(address),
        state=address.get("state"),
        neighbourhood=_neighbourhood_of(address),
        zip=address.get("postcode"),
        road=address.get("road"),
        house_number=address.get("house_number"),
    )

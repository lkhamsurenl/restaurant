"""Overpass: enumerate food venues near a point, and find one by name.

This replaces Nominatim for name lookup. Two findings drive the regex work here,
both verified against the live API:

- Overpass regexes are POSIX ERE, which has no \\b. A bare ["name"~"hai",i] in
  Seattle matched 114 venues, almost all of them "Thai". Wrapping the pattern in
  POSIX character classes brought that to 0, which is the correct answer.
- Overpass does no case folding beyond the ,i flag and no accent folding at all,
  but bracket classes work: c[aou...]f[e-with-accents] matches "Cafe" with an
  acute e in a single request, with no client-side over-fetching.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from . import osm
from .geo import haversine_km
from .models import Candidate, fold

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    # A public mirror, used only once the primary's retries are exhausted. Still
    # donated hardware, so the same throttle applies to it.
    "https://overpass.kumi.systems/api/interpreter",
]

# An area enumeration goes stale as places close, so it expires. A six-month-old
# candidate list recommends restaurants that shut in the spring.
AREA_CACHE_DAYS = 30

KIND_FILTERS = {
    "restaurant": '["amenity"="restaurant"]',
    "cafe": '["amenity"="cafe"]',
    "fast_food": '["amenity"="fast_food"]',
    "bar": '["amenity"="bar"]',
    "pub": '["amenity"="pub"]',
    "ice_cream": '["amenity"="ice_cream"]',
    # Bakeries and delis carry no amenity tag at all in OSM - Tartine Bakery is
    # shop=bakery - so they need their own filter to be findable.
    "bakery": '["shop"="bakery"]',
    "deli": '["shop"="deli"]',
}

# Used when searching for a place the user says they visited. Deliberately wider
# than the recommendation scope: strict about what we suggest, permissive about
# what we can record.
ANY_FOOD = (
    '["amenity"~"^(restaurant|cafe|fast_food|bar|pub|ice_cream|biergarten|food_court)$"]'
)
ANY_SHOP = '["shop"~"^(bakery|deli|patisserie|confectionery)$"]'

_ACCENTS = {
    "a": "aáàâäãå",
    "c": "cç",
    "e": "eéèêë",
    "i": "iíìîï",
    "n": "nñ",
    "o": "oóòôöõø",
    "u": "uúùûü",
    "y": "yý",
}

# Words that identify nothing on their own - "Cafe Zuni" and "Zuni Cafe" share
# only the useless half.
_STOPWORDS = {
    "cafe", "restaurant", "bar", "grill", "grille", "kitchen", "house", "bistro",
    "the", "and", "of", "la", "le", "el", "los", "las", "de", "du", "and",
    "co", "inc", "ltd", "llc",
}


def coords(element: dict[str, Any]) -> tuple[float, float] | None:
    """Extract a position from an Overpass element.

    Nodes carry top-level lat/lon; ways and relations carry a `center` dict
    (because of `out center`). Reading only element["lat"] silently drops every
    venue mapped as a building outline - Zuni Cafe is a way - so this must be the
    single place coordinates are read.
    """
    if "lat" in element and "lon" in element:
        return float(element["lat"]), float(element["lon"])
    center = element.get("center")
    if center and "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


def _query(body: str, timeout: int = 120) -> str:
    return f"[out:json][timeout:{timeout}];\n{body}\nout tags center;"


def _run(
    query: str, *, max_age_days: float | None, attempts: int = 6
) -> list[dict[str, Any]]:
    """POST a query, falling back to the mirror if the primary is exhausted."""
    last: Exception | None = None
    for endpoint in ENDPOINTS:
        try:
            payload = osm.request(
                "overpass",
                endpoint,
                method="POST",
                data=query,
                max_age_days=max_age_days,
                timeout=180.0,
                attempts=attempts,
            )
            return payload.get("elements", [])
        except osm.TransientError as exc:
            last = exc
            continue
    raise last or osm.TransientError("overpass unreachable")


def _filters(kinds: tuple[str, ...]) -> list[str]:
    unknown = [k for k in kinds if k not in KIND_FILTERS]
    if unknown:
        raise osm.OSMError(
            f"unknown venue kind(s): {', '.join(unknown)}. "
            f"Choose from: {', '.join(sorted(KIND_FILTERS))}"
        )
    return [KIND_FILTERS[k] for k in kinds]


def nearby(
    lat: float,
    lon: float,
    radius_m: int,
    kinds: tuple[str, ...] = ("restaurant",),
) -> list[dict[str, Any]]:
    """Every venue of the given kinds within radius_m of a point."""
    clauses = "\n ".join(
        f"nwr{f}(around:{radius_m},{lat:.6f},{lon:.6f});" for f in _filters(kinds)
    )
    query = _query(f"(\n {clauses}\n);")
    return _run(query, max_age_days=AREA_CACHE_DAYS)


def _expand(token: str) -> str:
    """Accent-insensitive ERE for one folded token."""
    return "".join(
        f"[{_ACCENTS[c]}]" if c in _ACCENTS else re.escape(c) for c in token
    )


def _bounded(pattern: str) -> str:
    """Wrap a pattern in word boundaries.

    POSIX ERE has no \\b, so this uses character classes. Without it, searching
    "hai" matches every "Thai" restaurant in the city.
    """
    return f"(^|[^[:alnum:]]){pattern}([^[:alnum:]]|$)"


def _tokens(name: str) -> list[str]:
    return [t for t in fold(name).split() if t]


def _significant(tokens: list[str]) -> list[str]:
    keep = [t for t in tokens if t not in _STOPWORDS and len(t) > 2]
    return sorted(keep, key=len, reverse=True)


def _name_strategies(name: str) -> list[str]:
    """Overpass tag-filter strings to try, most precise first."""
    tokens = _tokens(name)
    if not tokens:
        return []

    strategies: list[str] = []

    # 1. The full phrase, punctuation-tolerant and accent-expanded.
    phrase = "[^[:alnum:]]+".join(_expand(t) for t in tokens)
    strategies.append(f'["name"~"{_bounded(phrase)}",i]')

    # 2. Every token present, order-free. Repeated filters on one key AND in
    #    Overpass, so this catches "Cafe Zuni" for a query of "Zuni Cafe".
    if len(tokens) > 1:
        strategies.append(
            "".join(f'["name"~"{_bounded(_expand(t))}",i]' for t in tokens)
        )

    significant = _significant(tokens)

    # 3. The two longest distinctive tokens, for when word order differs.
    if len(significant) >= 2:
        strategies.append(
            "".join(f'["name"~"{_bounded(_expand(t))}",i]' for t in significant[:2])
        )

    # 4. The single most distinctive token - the loose net.
    if significant:
        strategies.append(f'["name"~"{_bounded(_expand(significant[0]))}",i]')

    return strategies


def search_name(
    name: str,
    lat: float,
    lon: float,
    radius_m: int = 12_000,
    limit: int = 5,
    attempts: int = 6,
) -> list[dict[str, Any]]:
    """Find venues whose name matches, nearest-best first.

    Overpass has no name index, so this scans every matching venue in the radius
    and its cost grows with the *area* - measured at roughly 6s for a 5km radius
    and 11s for 12km, per query. Two consequences shape the code below:

    - The radius default is 12km rather than city-wide. A 25km search in San
      Francisco ran long enough to look hung.
    - Food venues are searched first and bakeries/delis only as a last resort,
      rather than unioning both into every query. Unioning doubled the scan on
      every strategy to serve a case that almost never applies.
    """
    strategies = _name_strategies(name)
    if not strategies:
        return []

    def attempt(tag_filter: str, venue_filter: str) -> list[dict[str, Any]]:
        query = _query(
            f"nwr{venue_filter}{tag_filter}(around:{radius_m},{lat:.6f},{lon:.6f});"
        )
        elements = _run(query, max_age_days=AREA_CACHE_DAYS, attempts=attempts)
        return [e for e in elements if (e.get("tags") or {}).get("name")]

    for tag_filter in strategies:
        named = attempt(tag_filter, ANY_FOOD)
        if named:
            return _rank(named, name, lat, lon)[:limit]

    # Bakeries and delis carry no amenity tag at all - Tartine Bakery is
    # shop=bakery - so they need a pass of their own. Only the most precise
    # strategy is retried, to keep the cost of this fallback to one query.
    named = attempt(strategies[0], ANY_SHOP)
    return _rank(named, name, lat, lon)[:limit] if named else []


def _rank(
    elements: list[dict[str, Any]], query: str, lat: float, lon: float
) -> list[dict[str, Any]]:
    """Best name match first, with distance as a mild tiebreak."""
    target = fold(query)

    def score(element: dict[str, Any]) -> float:
        name = (element.get("tags") or {}).get("name", "")
        similarity = difflib.SequenceMatcher(None, target, fold(name)).ratio()
        position = coords(element)
        distance = haversine_km(lat, lon, *position) if position else 100.0
        return similarity - min(distance / 200.0, 0.2)

    return sorted(elements, key=score, reverse=True)


_STATE_SUFFIX = re.compile(r"^(.*?)[,\s]+([A-Z]{2})$")


def _split_city(city: str | None, state: str | None) -> tuple[str | None, str | None]:
    """Separate a state abbreviation that got typed into addr:city.

    OSM tags are hand-entered and inconsistent: Monkeypod Kitchen carries
    addr:city="Kapolei, HI", which would otherwise be stored as the city name and
    render as "Kapolei, HI, Hawaii".
    """
    from .nominatim import STATES  # local import keeps the module graph one-way

    def full(value: str | None) -> str | None:
        """Two-letter codes and full names both occur in addr:state; store one form."""
        return STATES.get(value.upper(), value) if value and len(value) == 2 else value

    if not city:
        return city, full(state)
    match = _STATE_SUFFIX.match(city.strip())
    if match:
        return match.group(1).strip(), full(state or match.group(2))
    return city.strip(), full(state)


def to_candidate(
    element: dict[str, Any], origin_lat: float, origin_lon: float
) -> Candidate | None:
    """Build a Candidate from an Overpass element, or None if it has no position."""
    tags = element.get("tags") or {}
    position = coords(element)
    if position is None or not tags.get("name"):
        return None

    lat, lon = position
    cuisine = [c.strip() for c in (tags.get("cuisine") or "").split(";") if c.strip()]
    diet = [
        key.split(":", 1)[1]
        for key, value in tags.items()
        if key.startswith("diet:") and value in ("yes", "only")
    ]
    street, number = tags.get("addr:street"), tags.get("addr:housenumber")
    address = f"{number} {street}" if street and number else street
    city, state = _split_city(tags.get("addr:city"), tags.get("addr:state"))

    return Candidate(
        osm_type=element.get("type", "node"),
        osm_id=int(element["id"]),
        name=tags["name"],
        amenity=tags.get("amenity") or (f"shop:{tags['shop']}" if tags.get("shop") else None),
        cuisine=cuisine,
        lat=lat,
        lon=lon,
        address=address,
        city=city,
        state=state,
        zip=tags.get("addr:postcode"),
        website=tags.get("website") or tags.get("contact:website"),
        wikidata=tags.get("wikidata") or tags.get("wikipedia"),
        brand=tags.get("brand") or tags.get("brand:wikidata"),
        has_hours=bool(tags.get("opening_hours")),
        check_date=tags.get("check_date") or tags.get("survey:date"),
        diet=diet,
        outdoor_seating=tags.get("outdoor_seating") in ("yes", "seasonal"),
        takeaway=tags.get("takeaway") in ("yes", "only"),
        distance_km=haversine_km(origin_lat, origin_lon, lat, lon),
    )

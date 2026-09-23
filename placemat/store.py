"""Load and save library.json.

One file for the whole library. Entries are written in a stable order with sorted
keys so git diffs stay readable and edits to different places don't collide.
"""

from __future__ import annotations

import json
from pathlib import Path

from .geo import haversine_km
from .models import Library, Restaurant, Status, fold

LIBRARY_PATH = Path("library.json")


def load(path: Path = LIBRARY_PATH) -> Library:
    if not path.exists():
        return Library()
    return Library.model_validate_json(path.read_text())


def save(library: Library, path: Path = LIBRARY_PATH) -> None:
    library.restaurants.sort(key=_sort_key)
    payload = library.model_dump(mode="json", exclude_none=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _sort_key(r: Restaurant) -> tuple[str, str, str]:
    """Group by place, then name.

    State and city first because geography is what you group restaurants by - the
    equivalent of the books library sorting by author. Unlabelled entries sort to
    the end rather than the top, so the unresolved tail doesn't head every diff.
    """
    return (fold(r.state or "zzz"), fold(r.city or "zzz"), fold(r.name))


def upsert(library: Library, restaurant: Restaurant) -> bool:
    """Add a place, or update it in place if already present.

    Returns True if the place was newly added.
    """
    existing = find(library, restaurant)
    if existing is None and restaurant.has_location:
        # OSM ids are not stable across remapping - a node re-mapped as a way gets a
        # new id - so key matching alone would eventually double-enter a place.
        existing = find_near(
            library, restaurant.name, restaurant.lat, restaurant.lon  # type: ignore[arg-type]
        )

    if existing is None:
        library.restaurants.append(restaurant)
        return True

    # Merge: keep fields already set by hand unless the incoming record has a value.
    incoming = restaurant.model_dump(exclude_none=True, exclude_defaults=True)
    for field, value in incoming.items():
        setattr(existing, field, value)
    return False


def find(library: Library, restaurant: Restaurant) -> Restaurant | None:
    for candidate in library.restaurants:
        if candidate.key == restaurant.key:
            return candidate
    return None


def find_near(
    library: Library, name: str, lat: float, lon: float, *, within_m: float = 150
) -> Restaurant | None:
    """Same folded name within `within_m`.

    Catches the library entry that predates its OSM id, and the venue OSM has
    re-mapped under a new id.
    """
    target = fold(name)
    for candidate in library.restaurants:
        if not candidate.has_location or fold(candidate.name) != target:
            continue
        if haversine_km(lat, lon, candidate.lat, candidate.lon) * 1000 <= within_m:  # type: ignore[arg-type]
            return candidate
    return None


def find_by_name(
    library: Library, name: str, city: str | None = None
) -> Restaurant | None:
    target = fold(name)
    for candidate in library.restaurants:
        if fold(candidate.name) != target:
            continue
        if city and fold(candidate.city or "") != fold(city):
            continue
        return candidate
    return None


def skipped_keys(library: Library) -> set[str]:
    return {r.key for r in library.restaurants if r.status is Status.NOT_INTERESTED}


def skipped_names(library: Library) -> set[str]:
    return {fold(r.name) for r in library.restaurants if r.status is Status.NOT_INTERESTED}

"""The schema contract, shared by the CLI, the store, retrieval, and the recommender."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, Field


class Status(StrEnum):
    """What happened, not what you thought of it."""

    VISITED = "visited"

    WANT_TO_TRY = "want-to-try"
    """On the list, never been. Intent rather than a verdict, so it teaches no
    taste - but a recommendation slot spent on one is wasted."""

    NOT_INTERESTED = "not-interested"
    """Seen it, passed on it, never ate there. Suppresses the place from future
    recommendations without implying any judgment at all."""


class LocationSource(StrEnum):
    """Where a place's city/state/zip labels came from.

    Worth recording because the three differ sharply in trustworthiness, and an
    undisclosed approximation reads as a bug. Reverse geocoding put Zuni Cafe in
    94143 when it is really 94102, so labels derived that way are marked.
    """

    OSM_TAGS = "osm-tags"
    REVERSE = "reverse"
    MANUAL = "manual"


class Restaurant(BaseModel):
    # Bibliographic-ish, mostly filled from OpenStreetMap.
    name: str
    osm_type: str | None = None  # node | way | relation
    osm_id: int | None = None  # (osm_type, osm_id) is OSM's primary key
    amenity: str | None = None
    cuisine: list[str] = Field(default_factory=list)
    brand: str | None = None  # presence means chain outlet
    website: str | None = None
    wikidata: str | None = None  # rough proxy for "well-known enough to be judged"

    # Location. lat/lon is authoritative; everything under it is a label.
    lat: float | None = None
    lon: float | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    neighbourhood: str | None = None
    zip: str | None = None
    location_source: LocationSource = LocationSource.MANUAL

    # Personal. Hand-edited, and what actually drives recommendations.
    rating: int | None = Field(default=None, ge=1, le=5)
    status: Status = Status.VISITED
    price: int | None = Field(default=None, ge=1, le=4)
    date_visited: str | None = None
    notes: str | None = None
    dishes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    permanently_closed: bool = False

    @property
    def is_positive(self) -> bool:
        """Worth extending the pattern of."""
        return self.rating is not None and self.rating >= 4

    @property
    def is_negative(self) -> bool:
        """Worth steering away from."""
        return self.rating is not None and self.rating <= 2

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def place_label(self) -> str:
        """City and state, the primary way a place is identified to a human.

        Deliberately not the zip: many venues have no addr:postcode at all, and a
        reverse-geocoded one is only approximate.
        """
        parts = [p for p in (self.city, self.state) if p]
        return ", ".join(parts) if parts else "location unknown"

    @property
    def key(self) -> str:
        """Identity for dedupe: prefer OSM's key, fall back to name plus place.

        The fallback has to include the place. Book titles are near-unique but
        restaurant names are not - one small query returned "Mission's Kitchen"
        twice - so folding on the name alone would silently merge distinct venues.
        """
        if self.osm_type and self.osm_id:
            return f"osm:{self.osm_type}/{self.osm_id}"
        return f"np:{fold(self.name)}|{fold(self.city or '')}|{fold(self.state or '')}"


class Library(BaseModel):
    restaurants: list[Restaurant] = Field(default_factory=list)


class Target(BaseModel):
    """A resolved search area. Persisted so the site and `skip` know the context."""

    query: str  # what the user typed: "96707" or "Kapolei, HI"
    label: str  # Nominatim's display_name
    lat: float
    lon: float
    radius_m: int

    @property
    def radius_km(self) -> float:
        return self.radius_m / 1000.0


class Candidate(BaseModel):
    """A real OSM venue offered to the model. Not persisted in library.json."""

    osm_type: str
    osm_id: int
    name: str
    amenity: str | None = None
    cuisine: list[str] = Field(default_factory=list)
    lat: float
    lon: float
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    website: str | None = None
    wikidata: str | None = None
    brand: str | None = None
    has_hours: bool = False
    check_date: str | None = None
    diet: list[str] = Field(default_factory=list)
    outdoor_seating: bool = False
    takeaway: bool = False
    distance_km: float = 0.0
    score: float = 0.0
    wildcard: bool = False  # deliberately outside the user's usual cuisines
    freshness: str | None = None  # "fresh" | "stale" | None

    @property
    def ref(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"


class Recommendation(BaseModel):
    """One suggestion from the model, before verification."""

    name: str
    osm_ref: str | None = Field(
        default=None,
        description=(
            'The "node/123" reference copied exactly from the candidate line, or '
            "null for an off-list pick you know independently."
        ),
    )
    cuisine: str | None = None
    reason: str = Field(
        description="Why this fits, citing specific restaurants from their library by name"
    )
    avoids: str = Field(
        description="Which disliked trait this steers clear of, naming the place it comes from"
    )
    dish: str | None = Field(
        default=None,
        description="One specific thing worth ordering, only if you actually know the place",
    )
    confidence: float = Field(ge=0.0, le=1.0)


class RecommendationList(BaseModel):
    """The structured-output schema for the recommend call."""

    recommendations: list[Recommendation]


class VerifiedRecommendation(Recommendation):
    """A recommendation resolved to a real OSM venue inside the target area."""

    osm_type: str | None = None
    osm_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    website: str | None = None
    distance_km: float | None = None  # recomputed by us, never taken from the model
    source: str = "candidate"  # candidate | ref-corrected | off-list-verified


class Recommendations(BaseModel):
    generated_at: str
    model: str
    target: Target
    radius_used_m: int
    candidates_considered: int
    candidates_available: int
    recommendations: list[VerifiedRecommendation] = Field(default_factory=list)
    dropped: list[str] = Field(
        default_factory=list,
        description="Places the model proposed that did not survive verification",
    )


_PUNCT = re.compile(r"[^\w\s]")


def fold(value: str) -> str:
    """Accent- and punctuation-insensitive form for matching.

    NFKD then dropping combining marks is what makes "Zuni Cafe" match OSM's
    "Zuni Cafe" with an acute e: OSM stores the accent and nobody types it.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(_PUNCT.sub(" ", stripped).lower().split())

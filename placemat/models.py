"""The schema contract, shared by the CLI, the store, retrieval, and the recommender."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import ClassVar

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


class Ratings(BaseModel):
    """Per-attribute scores, to sit alongside the overall rating and the note.

    Two rules hold this together:

    Every score runs 1-5 and **higher is always better**, which is why the noise
    dimension is stored as `quiet` rather than `noise`. Mixed directions make an
    average meaningless, and averaging across attributes is the point - it is what
    lets the prompt say "they mark vibe harshly and food generously".

    Every score is optional, and a blank is not a zero. These exist to capture
    dimensions that recur often enough to compare; anything idiosyncratic stays in
    `notes`, where "dog friendly" and "they have a tablet you can use to order"
    live. The attribute set was chosen from what actually appeared in the notes
    three or more times, because a wider schema is mostly empty and a sparse score
    is worse than no score - it reads as a judgment that was never made.
    """

    food: int | None = Field(default=None, ge=1, le=5)
    vibe: int | None = Field(default=None, ge=1, le=5, description="Room, decor, atmosphere")
    quiet: int | None = Field(default=None, ge=1, le=5, description="5 = pleasantly quiet")
    service: int | None = Field(default=None, ge=1, le=5, description="Staff, and the wait")
    value: int | None = Field(default=None, ge=1, le=5, description="What you got for the price")

    FIELDS: ClassVar[tuple[str, ...]] = ("food", "vibe", "quiet", "service", "value")

    def scored(self) -> dict[str, int]:
        """Only the attributes actually filled in."""
        return {f: v for f in self.FIELDS if (v := getattr(self, f)) is not None}

    @property
    def any_set(self) -> bool:
        return bool(self.scored())

    def summary(self) -> str:
        """Compact form for a prompt or a list line: "food 5 · vibe 2 · value 3"."""
        return " · ".join(f"{f} {v}" for f, v in self.scored().items())


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
    ratings: Ratings = Field(default_factory=Ratings)
    status: Status = Status.VISITED
    price: int | None = Field(default=None, ge=1, le=4)
    date_visited: str | None = None
    notes: str | None = None
    dishes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    permanently_closed: bool = False

    boycott: bool = False
    """Won't go back for reasons that say nothing about the food.

    Labor practices, ownership, how staff are treated - objections that are about
    the business rather than the meal. This needs its own flag because a low rating
    is a statement about the cooking, and the recommender reads it that way: it
    infers what to avoid from the cuisines and attributes of poorly-rated places.

    Sushi Bay is the case that forced this. Rating it 2 for wage theft, with food 5
    and kind staff, would have pushed the `japanese` cuisine weight to -1.5 and
    quietly suppressed every good Japanese restaurant in the candidate pool, while
    also diluting the signal that noise is what actually ruins a meal here.

    So a boycotted place is excluded from taste inference entirely - cuisine
    weights, attribute averages, the dealbreaker calculation - and listed for the
    model under its own heading as somewhere never to suggest. It still shows in the
    library, because remembering why is the whole point.
    """

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


class Household(BaseModel):
    """Standing requirements that apply to every recommendation, not to one place.

    Diets live here rather than on each restaurant because they are a property of
    who is eating, and they are a *constraint* rather than a taste: a place that
    cannot feed someone in the party is disqualified however good it is, which is a
    different kind of statement from a low score.

    Recording it per-restaurant would also carry no signal. Every place already
    visited has something a pescatarian can eat - that is why it got visited - so
    the field would be uniformly true and would discriminate nothing.
    """

    diets: list[str] = Field(default_factory=list)
    """e.g. ["pescatarian"]. Free text, since real diets don't fit an enum."""

    notes: str | None = None
    """Nuance a label can't hold - how strict, who it applies to, what the bar is."""

    @property
    def has_constraints(self) -> bool:
        return bool(self.diets)

    def describe(self) -> str:
        label = " and ".join(self.diets)
        return f"{label}{f' ({self.notes})' if self.notes else ''}"


class Library(BaseModel):
    household: Household = Field(default_factory=Household)
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
    diet_fit: str | None = Field(
        default=None,
        description=(
            "Required when the household has a dietary constraint: name the actual "
            "dishes someone on that diet could order here. Leave null only if there "
            "is no constraint. A place you cannot answer this for is disqualified."
        ),
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

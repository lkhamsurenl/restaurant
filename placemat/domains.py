"""What differs between the kinds of place this tool tracks.

Everything hard about this project is domain-neutral: geocoding a target, the
Overpass name regex, coordinates on ways, coastline simplification, the retrieve
-> filter -> rank -> verify pipeline. Only three things actually change between
tracking restaurants and tracking matcha - which OpenStreetMap venues count, what
you judge a visit on, and what the model needs told about the subject. Those live
here, so adding a domain is a config entry rather than a second copy of the client.

Each domain keeps its own file. A cafe you rate for its matcha and a cafe you rate
for its food are two separate judgments on different axes, and merging them into
one row would muddle both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Attribute:
    key: str
    help: str


@dataclass(frozen=True)
class Domain:
    name: str
    """Short identifier, also the CLI's own name for the domain."""

    noun: str
    """What one entry is, for prose: "restaurant", "matcha spot"."""

    plural: str
    data_file: Path
    recommendations_file: Path
    page_title: str
    page_file: str
    """Filename under docs/. Restaurants keep index.html so the already-published
    GitHub Pages URL does not break."""

    kinds: tuple[str, ...]
    """Default OpenStreetMap venue kinds for the candidate pool."""

    attributes: tuple[Attribute, ...]
    """The 1-5 axes a visit is scored on. All higher-is-better, so that averaging
    across them means anything - which is why a noise dimension is stored as
    `quiet` rather than `noise`."""

    subject: str
    """A paragraph for the prompt describing what is being judged and what
    separates a good one from a bad one. This is where domain knowledge that the
    OpenStreetMap tags cannot carry gets stated."""

    tag_caveat: str
    """The specific way tags mislead in this domain, so the model doesn't read a
    missing or generic tag as a verdict."""

    @property
    def attribute_keys(self) -> tuple[str, ...]:
        return tuple(a.key for a in self.attributes)

    def validate(self, scores: dict[str, int]) -> dict[str, int]:
        unknown = set(scores) - set(self.attribute_keys)
        if unknown:
            raise ValueError(
                f"{self.name} has no attribute(s) {', '.join(sorted(unknown))}. "
                f"Choose from: {', '.join(self.attribute_keys)}"
            )
        return scores


RESTAURANTS = Domain(
    name="restaurants",
    noun="restaurant",
    plural="restaurants",
    data_file=Path("library.json"),
    recommendations_file=Path("recommendations.json"),
    page_title="Where We've Eaten",
    page_file="index.html",
    kinds=("restaurant",),
    attributes=(
        Attribute("food", "The cooking itself. 1-5."),
        Attribute("vibe", "Room, decor, atmosphere. 1-5."),
        Attribute("quiet", "5 = pleasantly quiet. 1-5."),
        Attribute("service", "Staff and the wait. 1-5."),
        Attribute("value", "What you got for the price. 1-5."),
    ),
    subject=(
        "You recommend restaurants - places to eat a full meal. Judge them on the "
        "cooking first, then the room."
    ),
    tag_caveat=(
        "A cuisine tag is one coarse word: `italian` covers a twelve-dollar slice "
        "and a tasting menu, and a tonkatsu specialist is tagged plain `japanese`. "
        "Use the tags to know a venue exists, and your own knowledge of the place "
        "to judge what it actually is."
    ),
)

MATCHA = Domain(
    name="matcha",
    noun="matcha spot",
    plural="matcha spots",
    data_file=Path("matcha.json"),
    recommendations_file=Path("matcha-recommendations.json"),
    page_title="Where We've Had Matcha",
    page_file="matcha.html",
    # Deliberately wider than restaurants. Matcha is served across cafes, tea
    # houses, bakeries and dessert shops, and OpenStreetMap has almost no
    # `shop=tea` - one in the whole 30km around Kapolei.
    kinds=("cafe", "tea_house", "bakery", "ice_cream", "deli"),
    attributes=(
        Attribute("quality", "The matcha itself - grade, depth, bitterness. 1-5."),
        Attribute("preparation", "Made properly, or sweet powder in milk? 1-5."),
        Attribute("vibe", "Room, decor, atmosphere. 1-5."),
        Attribute("value", "What you got for the price. 1-5."),
    ),
    subject=(
        "You recommend places to drink matcha. What is being judged is the matcha, "
        "not the food: a cafe with excellent pastries and a sweet green powder "
        "latte is a bad recommendation, and a plain counter that whisks "
        "ceremonial-grade tea properly is a good one.\n"
        "- `quality` is the tea - grade, depth, whether the bitterness is pleasant "
        "or dusty. `preparation` is the craft - whether it is whisked to order, "
        "whether unsweetened is possible, whether the temperature and ratio are "
        "handled with care.\n"
        "- A place known for serving real ceremonial or first-harvest matcha, or "
        "for sourcing from a named Japanese producer, is worth far more than a "
        "chain with matcha on the menu.\n"
        "- Bubble tea shops mostly serve sweetened matcha powder. Suggest one only "
        "when you have reason to think its matcha is genuinely good, and say so."
    ),
    tag_caveat=(
        "OpenStreetMap tagging is close to useless for this. There is almost no "
        "`shop=tea`, `cuisine=tea` is rare, and `cuisine=bubble_tea` says nothing "
        "about matcha quality. A dedicated matcha cafe in this very dataset is "
        "tagged `ice_cream`. Treat the candidate list as a list of venues that "
        "exist near the target, nothing more, and rely on what you know about each "
        "named place to judge whether its matcha is worth the trip."
    ),
)

ALL = {d.name: d for d in (RESTAURANTS, MATCHA)}


def get(name: str) -> Domain:
    try:
        return ALL[name]
    except KeyError:
        raise ValueError(
            f"unknown domain {name!r}. Choose from: {', '.join(ALL)}"
        ) from None

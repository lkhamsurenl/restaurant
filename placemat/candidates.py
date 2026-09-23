"""Retrieve real nearby venues, then filter and rank them into a shortlist.

This is the largest departure from the books repo it is modelled on. There, the
external catalogue *verifies* titles the model named; here it *supplies* the
venues the model may name. The model never invents a restaurant, because it only
ever chooses from this list.

The list has to be narrowed, in both directions. A 30km radius around Kapolei
returns 683 independent restaurants, and a wide query over central San Francisco
returned 2,564 elements - too many for a prompt. A 5km radius around Kapolei
returns 31, which is too few to choose well from.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter, defaultdict

from . import overpass, store
from .models import Candidate, Library, Restaurant, Status, Target, fold

# Below this many candidates, widen the search rather than hand the model a pool
# too thin to choose from.
MIN_POOL = 40
MAX_RADIUS_M = 80_000

# Chains that carry no `brand` tag in OSM. The brand tag caught 206 chain outlets
# cleanly in San Francisco, but California Pizza Kitchen, Panda Express, Domino's,
# Chick-fil-A and Taco Bell all slipped through it in Kapolei - so a name list is
# needed as well. Folded form, matched exactly.
CHAIN_NAMES = {
    "california pizza kitchen", "panda express", "dominos", "dominos pizza",
    "chick fil a", "taco bell", "mcdonalds", "burger king", "subway", "wendys",
    "kfc", "pizza hut", "papa johns", "little caesars", "jack in the box",
    "dennys", "ihop", "applebees", "olive garden", "chilis", "red lobster",
    "outback steakhouse", "cheesecake factory", "buffalo wild wings", "arbys",
    "popeyes", "sonic", "five guys", "in n out burger", "shake shack",
    "chipotle", "chipotle mexican grill", "qdoba", "moes southwest grill",
    "starbucks", "dunkin", "dunkin donuts", "peets coffee", "coffee bean tea leaf",
    "jamba juice", "smoothie king", "baskin robbins", "dairy queen", "sonic drive in",
    "zippys", "l l hawaiian barbecue", "l l hawaiian bbq", "ll hawaiian barbecue",
    "jollibee", "panda inn", "round table pizza", "papa murphys", "quiznos",
    "jimmy johns", "firehouse subs", "jersey mikes subs", "potbelly",
    "raising canes", "wingstop", "del taco", "carls jr", "whataburger",
    "texas roadhouse", "longhorn steakhouse", "red robin", "tgi fridays",
}

# Deliberately excluded from the taste-affinity signal: too generic to say anything
# about what someone likes.
_VAGUE_CUISINES = {"regional", "international", "local", "fusion", "american"}


def _is_chain(candidate: Candidate) -> bool:
    return bool(candidate.brand) or fold(candidate.name) in CHAIN_NAMES


def _freshness(candidate: Candidate) -> str | None:
    """How recently a human confirmed this venue exists.

    OSM's closest thing to a liveness signal. A third of the San Francisco pull had
    no check_date at all, so absence means nothing either way.
    """
    if not candidate.check_date:
        return None
    try:
        checked = dt.date.fromisoformat(candidate.check_date[:10])
    except ValueError:
        return None
    age_days = (dt.date.today() - checked).days
    if age_days <= 730:
        return "fresh"
    if age_days >= 1825:
        return "stale"
    return None


def _richness(candidate: Candidate) -> float:
    """Metadata completeness as a proxy for "a real place someone cared about".

    OSM carries no ratings, reviews or popularity at all, so this is the only
    quality-adjacent signal available. Weights are set against measured tag
    coverage in a 377-restaurant San Francisco pull. Nothing scores on
    price_range or contact:website: both were present on 0 of 377.
    """
    score = 0.0
    if candidate.cuisine:
        score += 2.0  # 324/377 - by far the most useful field
    if candidate.website:
        score += 1.0  # 206/377
    if candidate.has_hours:
        score += 1.0  # 190/377
    if candidate.wikidata:
        score += 0.7  # notable enough to have an article, so likely judgeable
    if candidate.address:
        score += 0.5
    if candidate.diet:
        score += 0.4
    if candidate.outdoor_seating or candidate.takeaway:
        score += 0.3

    freshness = candidate.freshness
    if freshness == "fresh":
        score += 0.6
    elif freshness == "stale":
        score -= 0.8
    return score


def _taste(library: Library) -> tuple[Counter, set[str]]:
    """Cuisine preference weights, and the set of cuisines already represented."""
    weights: Counter = Counter()
    known: set[str] = set()
    for r in library.restaurants:
        if r.status is Status.NOT_INTERESTED or r.rating is None:
            continue
        # A boycott is about the business, not the cooking. Letting it feed the
        # cuisine weights would read "never going back" as "this cuisine is bad".
        if r.boycott:
            continue
        for token in r.cuisine:
            token = token.lower()
            if token in _VAGUE_CUISINES:
                continue
            known.add(token)
            if r.rating >= 5:
                weights[token] += 2.0
            elif r.rating == 4:
                weights[token] += 1.0
            elif r.rating <= 2:
                weights[token] -= 1.5
    return weights, known


def _affinity(candidate: Candidate, weights: Counter) -> float:
    raw = sum(weights.get(c.lower(), 0.0) for c in candidate.cuisine)
    return max(-2.0, min(3.0, raw * 0.5))


def _owned(library: Library) -> tuple[set[str], list[Restaurant]]:
    return {r.key for r in library.restaurants}, [
        r for r in library.restaurants if r.has_location
    ]


def _drop_reasons(
    candidates: list[Candidate], library: Library, chains: bool
) -> list[Candidate]:
    """Hard exclusions. These are disqualifications, not penalties."""
    owned_keys, located = _owned(library)
    closed = {
        fold(r.name) for r in library.restaurants if r.permanently_closed
    }

    # Unbranded mini-chains: the same name three or more times in one radius. The
    # threshold is 3 so that a genuine coincidence of two survives.
    counts = Counter(fold(c.name) for c in candidates)

    kept: list[Candidate] = []
    seen_repeat: dict[str, Candidate] = {}
    for candidate in candidates:
        if not chains and _is_chain(candidate):
            continue
        if fold(candidate.name) in closed:
            continue

        as_restaurant = Restaurant(
            name=candidate.name,
            osm_type=candidate.osm_type,
            osm_id=candidate.osm_id,
            city=candidate.city,
            state=candidate.state,
        )
        if as_restaurant.key in owned_keys:
            continue
        if store.find_near(library, candidate.name, candidate.lat, candidate.lon):
            continue

        folded = fold(candidate.name)
        if counts[folded] >= 3:
            # Keep only the nearest of a repeated name.
            best = seen_repeat.get(folded)
            if best is None or candidate.distance_km < best.distance_km:
                seen_repeat[folded] = candidate
            continue

        kept.append(candidate)

    kept.extend(seen_repeat.values())
    return kept


def _select(
    scored: list[Candidate], limit: int, known_cuisines: set[str], radius_km: float
) -> list[Candidate]:
    """Take the top `limit`, with three reserves that the raw score would crush.

    The score alone produces a list that is all one cuisine (affinity compounding)
    and entirely far away (richer metadata in the city centre). Both defeat the
    point: the first leaves the model unable to avoid a nearest-neighbour pick, the
    second means a walkable option can never be suggested at all.
    """
    # Stable tiebreak, so identical requests produce byte-identical prompt text -
    # which is what makes comparing two prompt versions meaningful.
    scored.sort(key=lambda c: (-c.score, fold(c.name), c.osm_id))

    per_cuisine_cap = max(3, math.ceil(limit * 0.15))
    wildcard_slots = max(2, int(limit * 0.15))
    nearby_slots = max(2, int(limit * 0.10))

    chosen: list[Candidate] = []
    chosen_refs: set[str] = set()
    used: Counter = Counter()
    leftovers: list[Candidate] = []

    def take(candidate: Candidate) -> None:
        chosen.append(candidate)
        chosen_refs.add(candidate.ref)

    main_slots = limit - wildcard_slots - nearby_slots
    for candidate in scored:
        if len(chosen) >= main_slots:
            leftovers.append(candidate)
            continue
        primary = candidate.cuisine[0].lower() if candidate.cuisine else "_none"
        if used[primary] >= per_cuisine_cap:
            leftovers.append(candidate)
            continue
        used[primary] += 1
        take(candidate)

    # Reserve slots for places deliberately outside the user's usual cuisines,
    # ranked on richness alone so affinity can't crowd them out.
    novel = [
        c
        for c in leftovers
        if c.ref not in chosen_refs
        and c.cuisine
        and not any(x.lower() in known_cuisines for x in c.cuisine)
    ]
    novel.sort(key=lambda c: (-_richness(c), fold(c.name)))
    for candidate in novel[:wildcard_slots]:
        candidate.wildcard = True
        take(candidate)

    # Reserve slots for genuinely close venues, best-scoring first.
    #
    # Without this the list is purely the highest-scoring venues anywhere in the
    # radius, and city-centre entries win on metadata completeness every time: at a
    # 30km radius around Kapolei, only 2 of 28 venues within 10km survived, so the
    # nearest suggestion was 6.5km away and everything walkable was invisible.
    # Distance still must not *drive* the ranking - the user will drive for a good
    # fit - so this is a floor on local representation, not a distance preference.
    near_band = min(10.0, max(radius_km * 0.25, 1.0))
    nearby = [
        c for c in leftovers if c.ref not in chosen_refs and c.distance_km <= near_band
    ]
    nearby.sort(key=lambda c: (-c.score, fold(c.name)))
    for candidate in nearby[:nearby_slots]:
        take(candidate)

    # Top up from whatever is left if a reserve went unfilled.
    if len(chosen) < limit:
        for candidate in leftovers:
            if len(chosen) >= limit:
                break
            if candidate.ref not in chosen_refs:
                take(candidate)

    return chosen[:limit]


def retrieve(
    target: Target,
    library: Library,
    *,
    limit: int = 140,
    kinds: tuple[str, ...] = ("restaurant",),
    chains: bool = False,
    adaptive: bool = True,
) -> tuple[list[Candidate], int, int]:
    """Fetch, filter, score and shortlist venues near the target.

    Returns (shortlist, radius actually used in metres, pool size before the cap).
    """
    weights, known_cuisines = _taste(library)
    radius = target.radius_m
    pool: list[Candidate] = []

    for _ in range(4):
        elements = overpass.nearby(target.lat, target.lon, radius, kinds)
        raw = [
            c
            for c in (overpass.to_candidate(e, target.lat, target.lon) for e in elements)
            if c is not None
        ]
        for candidate in raw:
            candidate.freshness = _freshness(candidate)
        pool = _drop_reasons(raw, library, chains)

        if len(pool) >= MIN_POOL or not adaptive or radius >= MAX_RADIUS_M:
            break
        radius = min(int(radius * 1.5), MAX_RADIUS_M)

    radius_km = max(radius / 1000.0, 0.1)
    for candidate in pool:
        # Distance is a weak tiebreak by design, capped at -0.3 against a richness
        # range of roughly 0-8. A meaningful distance penalty would quietly re-rank
        # the list by geography and bury the better places further out, which is the
        # whole reason for using a wide radius.
        distance_penalty = -0.3 * min(candidate.distance_km / radius_km, 1.0)
        candidate.score = (
            _richness(candidate) + _affinity(candidate, weights) + distance_penalty
        )

    return _select(pool, limit, known_cuisines, radius_km), radius, len(pool)


def serialize(candidates: list[Candidate], target: Target, radius_m: int, pool: int) -> str:
    """Compact one-line-per-venue form for the prompt.

    Hours and websites collapse to one-word flags: the literal
    "Mo-Th 06:30-19:00; Fr-Su 06:30-20:00" is twenty tokens that contribute nothing
    to a taste decision.
    """
    header = [
        f"# CANDIDATES - real OpenStreetMap venues within {radius_m / 1000:.0f}km of "
        f"{target.label} ({len(candidates)} of {pool})",
        "# ref | name | cuisine | distance | flags",
        "# flags: web=has a website, hrs=posted hours, fresh=surveyed within 2y,",
        "#        stale=not surveyed in 5y and may have closed, wiki=has an article,",
        "#        veg/vegan=diet tags, outdoor=outdoor seating,",
        "#        wildcard=deliberately outside your usual cuisines",
    ]

    lines = []
    for c in sorted(candidates, key=lambda c: c.distance_km):
        flags = []
        if c.website:
            flags.append("web")
        if c.has_hours:
            flags.append("hrs")
        if c.freshness:
            flags.append(c.freshness)
        if c.wikidata:
            flags.append("wiki")
        for diet in ("vegan", "vegetarian"):
            if diet in c.diet:
                flags.append("vegan" if diet == "vegan" else "veg")
                break
        if c.outdoor_seating:
            flags.append("outdoor")
        if c.wildcard:
            flags.append("wildcard")

        cuisine = "/".join(c.cuisine[:3]) if c.cuisine else "-"
        lines.append(
            f"{c.ref} | {c.name} | {cuisine} | {c.distance_km:.1f}km"
            + (f" | {' '.join(flags)}" if flags else "")
        )

    return "\n".join(header + lines)


def histogram(candidates: list[Candidate]) -> list[tuple[str, int]]:
    """Primary-cuisine counts, for checking the diversity guards actually bind."""
    counts: Counter = Counter()
    for c in candidates:
        counts[c.cuisine[0].lower() if c.cuisine else "(none)"] += 1
    return counts.most_common()

"""Recommend restaurants by reasoning over the library, choosing from real venues.

The value here over a ratings matrix is that a personal library carries *why* a
place landed - free text alongside the score - which a star rating cannot
represent. So the prompt leans on notes, and low-rated places are sent as an
explicit avoid-list rather than filtered out.

What differs from the book version this is modelled on: the model does not name
restaurants from memory. It picks from a candidate list retrieved from
OpenStreetMap moments earlier, because restaurant knowledge is thin, local, and
goes stale as places close. Verification is therefore exact set membership rather
than a fuzzy round-trip.
"""

from __future__ import annotations

import datetime as dt
import difflib

import anthropic

from . import candidates as candidates_mod
from . import overpass, store
from .geo import haversine_km
from .models import (
    Candidate,
    Library,
    Ratings,
    Recommendation,
    RecommendationList,
    Recommendations,
    Restaurant,
    Status,
    Target,
    VerifiedRecommendation,
    fold,
)

MODEL = "claude-opus-5"

# How far out counts as "near the target" for the local-history section. Three
# times the search radius: you want what was rated on this island, not what was
# rated inside this exact circle.
LOCAL_MULTIPLE = 3

SYSTEM = """You recommend restaurants to one specific eater, in one specific \
place, choosing from a list of real venues just retrieved from OpenStreetMap near \
their target location.

What you are given:
- GLOBAL TASTE: everywhere they have eaten and rated, with their own notes.
- LOCAL HISTORY: what they rated in or near the target area, with distances.
- CANDIDATES: real OpenStreetMap venues near the target, one per line, each with \
a `node/123` style reference.

How to choose:
- Pick from CANDIDATES, and copy the reference from the line exactly into \
`osm_ref`.
- Extend the pattern you infer, do not mirror it. Their fifth Thai place is a \
wasted slot when the notes say what they actually like is the chili heat and the \
herbs. The obvious canonical restaurant in a cuisine they already eat weekly is a \
wasted slot too.
- Ratings are 1-5. Treat 4-5 as the taste to extend, 3 as lukewarm - note what \
was missing rather than chasing more of it - and 1-2 as a hard avoid-list, just \
as informative as the high scores. Weight a 5 more heavily than a 4.
- Some places carry per-attribute `scores` (food, vibe, quiet, service, value), \
all 1-5 and all higher-is-better, so `quiet 1` means unpleasantly loud. Read them \
against the HOW THEY SCORE averages rather than absolutely: a 4 on an attribute \
they average 3.0 on is warm praise. A blank is not a zero, it means they did not \
record a view.
- WHAT SINKS A PLACE FOR THEM, when present, is the most actionable thing you are \
given. The top entry there is close to a dealbreaker, and a candidate you suspect \
fails on it is a bad pick however well the cuisine matches. Say in `avoids` how \
your pick handles that specific attribute.
- LOCAL HISTORY is the sharper instrument. A 5 they gave twenty minutes away \
tells you more about what is good *here* than a 5 from another city. But do not \
confine yourself to the local pattern - GLOBAL TASTE is what generalizes.
- Distance within the listed radius is not a reason to prefer a place. This eater \
will drive for a good fit, so a better restaurant 25km away beats a mediocre one \
nearby. Use the distance column only to avoid stacking every pick in one town.
- If an ALREADY PASSED ON list is present, never pick anything on it, and do not \
substitute the near-identical place three doors down, since that is the same \
suggestion wearing a hat. Do not read taste into that list.
- The candidate list is OpenStreetMap data. It carries no ratings, no reviews and \
no popularity signal whatsoever. What you know about these specific named venues \
is the only quality information in the room. Use it, and say plainly when you do \
not have it rather than dressing up a guess.
- A `stale` flag means nobody has surveyed the venue in five years and it may have \
closed. Prefer `fresh` when a pick is otherwise a coin flip, and if you pick a \
stale one anyway, say why in `reason`.
- Spread the picks. Do not return eight versions of the same meal.

For each recommendation:
- `osm_ref` must be copied exactly from the candidate line. A pick whose reference \
is not in the list is dropped afterward, so a mistyped one costs a slot.
- `reason` must cite specific restaurants from their library by name and say what \
thread connects them. Generic praise of the restaurant is useless.
- `avoids` must name the concrete trait this pick steers clear of, referencing a \
low-rated place of theirs by name. If they have no low-rated places yet, say that \
plainly rather than inventing a trait to avoid.
- `dish` is one specific thing worth ordering, and only if you actually know the \
place. Leave it null rather than invent a menu.
- `confidence` is your honest read, not a sales pitch. A venue you know nothing \
about beyond its OpenStreetMap tags should be well under 0.5.

Off-list picks: if you know a place near the target that belongs on this list and \
is not in CANDIDATES, you may return it with `osm_ref` set to null. Each one is \
looked up in OpenStreetMap afterward and dropped if it does not resolve inside the \
target area. Stay within the off-list budget given in the request, and use it only \
when you are confident the place exists and is still open."""


def attribute_profile(library: Library) -> list[str]:
    """Describe how this eater uses the attribute scores, and what sinks a place.

    This is what the structured scores buy that a note cannot. Two things fall out
    of them that the model would otherwise have to guess at:

    - Calibration. Knowing someone marks vibe at 3.0 and food at 4.6 on average
      says a 4 for vibe from them is warm praise, not a shrug.
    - The dealbreaker. Comparing each attribute's average on their poorly-rated
      places against its average on the good ones surfaces the dimension that
      actually sinks a meal for them, rather than the one they write about most.

    Counts are always printed alongside, because on a library this small a gap of
    two points can rest on two places, and the model should weigh it accordingly.
    """
    rated = [
        r
        for r in library.restaurants
        if r.rating is not None
        and r.status is not Status.NOT_INTERESTED
        and r.ratings.any_set
    ]
    if len(rated) < 3:
        return []

    lines: list[str] = []
    averages: dict[str, tuple[float, int]] = {}
    for attribute in Ratings.FIELDS:
        scores = [
            getattr(r.ratings, attribute)
            for r in rated
            if getattr(r.ratings, attribute) is not None
        ]
        if scores:
            averages[attribute] = (sum(scores) / len(scores), len(scores))

    if not averages:
        return []

    lines.append(
        "# HOW THEY SCORE - average per attribute, with how many places each rests on."
    )
    lines.append(
        "# Read these as calibration: an attribute they mark low across the board is "
        "a high bar, not a complaint about any one place."
    )
    for attribute, (mean, count) in sorted(averages.items(), key=lambda kv: -kv[1][0]):
        lines.append(f"- {attribute}: {mean:.1f} average (from {count} places)")

    # Which attribute separates their bad experiences from their good ones.
    good = [r for r in rated if r.rating >= 4]
    poor = [r for r in rated if r.rating <= 3]
    gaps: list[tuple[float, str, str]] = []
    for attribute in Ratings.FIELDS:
        poor_scores = [
            getattr(r.ratings, attribute) for r in poor
            if getattr(r.ratings, attribute) is not None
        ]
        good_scores = [
            getattr(r.ratings, attribute) for r in good
            if getattr(r.ratings, attribute) is not None
        ]
        if not poor_scores:
            continue
        poor_mean = sum(poor_scores) / len(poor_scores)
        if good_scores:
            good_mean = sum(good_scores) / len(good_scores)
            gap = good_mean - poor_mean
            detail = (
                f"{poor_mean:.1f} on the {len(poor_scores)} place(s) they rated 3 or "
                f"below, against {good_mean:.1f} on the {len(good_scores)} they rated 4+"
            )
        elif poor_mean <= 2.5:
            # Recorded only where things went wrong, which is itself informative.
            gap = 5.0 - poor_mean
            detail = (
                f"{poor_mean:.1f} on the {len(poor_scores)} place(s) they rated 3 or "
                f"below, and not scored anywhere they rated 4+ - they appear to note "
                f"it only when it is a problem"
            )
        else:
            continue
        if gap >= 1.0:
            gaps.append((gap, attribute, detail))

    if gaps:
        gaps.sort(reverse=True)
        lines.append("")
        lines.append("# WHAT SINKS A PLACE FOR THEM, strongest signal first")
        lines.append(
            "# Treat the top entry as close to a dealbreaker: it is the attribute that "
            "best separates the meals they liked from the ones they didn't. Weight it "
            "above cuisine when choosing."
        )
        for _, attribute, detail in gaps:
            lines.append(f"- {attribute}: {detail}")

    return lines


def _format(restaurant: Restaurant, distance_km: float | None = None) -> str:
    parts = [f"- {restaurant.name}"]
    if restaurant.place_label != "location unknown":
        parts.append(f"— {restaurant.place_label}")
    if distance_km is not None:
        parts.append(f"— {distance_km:.1f}km from target")
    if restaurant.rating:
        parts.append(f"[{restaurant.rating}/5]")
    if restaurant.price:
        parts.append("$" * restaurant.price)
    if restaurant.cuisine:
        parts.append(f"({', '.join(restaurant.cuisine[:3])})")

    line = " ".join(parts)
    if restaurant.ratings.any_set:
        line += f"\n    scores: {restaurant.ratings.summary()}"
    if restaurant.dishes:
        line += f"\n    dishes: {', '.join(restaurant.dishes)}"
    if restaurant.notes:
        line += f"\n    notes: {restaurant.notes}"
    return line


def build_profile(library: Library, target: Target) -> str:
    """Serialize the library into global taste, local history, and suppression lists.

    A place appears in exactly one section. Local entries are omitted from the
    global list with a pointer line rather than repeated: duplicating them doubles
    the tokens and invites double-counting.
    """
    local_radius_km = target.radius_km * LOCAL_MULTIPLE

    def distance(r: Restaurant) -> float | None:
        if not r.has_location:
            return None
        return haversine_km(target.lat, target.lon, r.lat, r.lon)  # type: ignore[arg-type]

    rated = [
        r
        for r in library.restaurants
        if r.rating is not None and r.status is not Status.NOT_INTERESTED
    ]
    local, globals_ = [], []
    for r in rated:
        d = distance(r)
        (local if d is not None and d <= local_radius_km else globals_).append((r, d))

    globals_.sort(key=lambda pair: -(pair[0].rating or 0))
    local.sort(key=lambda pair: -(pair[0].rating or 0))

    cities = {r.city for r, _ in globals_ if r.city}
    sections: list[str] = []

    if globals_:
        sections.append(
            f"# GLOBAL TASTE - rated 1-5, best first ({len(globals_)} places"
            + (f" across {len(cities)} cities" if len(cities) > 1 else "")
            + ")"
        )
        if local:
            sections.append(
                f"# {len(local)} more are near the target and appear under "
                "LOCAL HISTORY instead."
            )
        sections.extend(_format(r) for r, _ in globals_)
    elif local:
        # Everything they have rated is near this target - common for someone
        # asking about their own area. Saying "(none rated yet)" here would read as
        # having no taste data at all, when in fact all of it is in the next
        # section.
        sections.append(
            f"# GLOBAL TASTE - every place they have rated is near this target, so "
            f"it is all listed under LOCAL HISTORY below. There is no separate "
            f"out-of-area history to generalize from."
        )
    else:
        sections.append("# GLOBAL TASTE - nothing rated yet.")

    if local:
        sections += [
            "",
            f"# LOCAL HISTORY - rated places within {local_radius_km:.0f}km of "
            f"{target.label} ({len(local)} places)",
            "# This is the sharper signal for this request: these are the actual "
            "options here.",
            *(_format(r, d) for r, d in local),
        ]
    else:
        sections += [
            "",
            f"# LOCAL HISTORY - none. They have never eaten near {target.label}. "
            "Lean entirely on GLOBAL TASTE and on what you know about these "
            "specific venues.",
        ]

    skipped = [r for r in library.restaurants if r.status is Status.NOT_INTERESTED]
    if skipped:
        sections += [
            "",
            f"# ALREADY PASSED ON - never pick any of these ({len(skipped)} places)",
            "# Seen and declined. Not a verdict on quality, so infer no taste from "
            "this list - just don't suggest them, or near-identical substitutes.",
            *(
                f"- {r.name} — {r.place_label}"
                + (f" ({r.notes})" if r.notes else "")
                for r in skipped
            ),
        ]

    attributes = attribute_profile(library)
    if attributes:
        sections += ["", *attributes]

    want = [r for r in library.restaurants if r.status is Status.WANT_TO_TRY]
    if want:
        sections += [
            "",
            f"# WANT TO TRY - already on their list; a slot spent here is wasted "
            f"({len(want)} places)",
            *(f"- {r.name} — {r.place_label}" for r in want),
        ]

    return "\n".join(sections)


def generate(
    library: Library,
    target: Target,
    shortlist: list[Candidate],
    radius_used_m: int,
    pool: int,
    *,
    count: int = 8,
    off_list: int = 2,
) -> tuple[Recommendations, object]:
    """Ask the model to choose from the shortlist, then verify what came back."""
    liked = [
        r
        for r in library.restaurants
        if r.is_positive and r.status is not Status.NOT_INTERESTED
    ]
    if not liked:
        raise ValueError(
            "No restaurants rated 4 or 5 yet. Add some with "
            '`eats add "<name>" --city "<city>" --rating 5` before asking for '
            "recommendations."
        )
    if not shortlist:
        raise ValueError(
            f"No candidate venues found near {target.label}. Try a wider "
            "--radius-km, or --kind cafe to include other venue types."
        )

    profile = build_profile(library, target)
    listing = candidates_mod.serialize(shortlist, target, radius_used_m, pool)

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{profile}\n\n{listing}\n\n"
                    f"Recommend {count} places to eat near {target.label}. "
                    + (
                        f"At most {off_list} may be off-list picks with a null osm_ref."
                        if off_list
                        else "Every pick must come from CANDIDATES; no off-list picks."
                    )
                ),
            }
        ],
        output_format=RecommendationList,
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(f"The model declined this request: {response.stop_details}")

    verified, dropped = _verify(
        response.parsed_output.recommendations, library, shortlist, target, off_list
    )

    result = Recommendations(
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        model=MODEL,
        target=target,
        radius_used_m=radius_used_m,
        candidates_considered=len(shortlist),
        candidates_available=pool,
        recommendations=verified,
        dropped=dropped,
    )
    return result, response.usage


def _hydrate(
    rec: Recommendation, candidate: Candidate, target: Target, source: str
) -> VerifiedRecommendation:
    """Build the output record from the *candidate*, never from the model.

    The model supplies judgment - reason, avoids, confidence - and nothing else.
    Names, coordinates and distances all come from OSM data, so a confident
    hallucination cannot smuggle a wrong address into the output.
    """
    return VerifiedRecommendation(
        **rec.model_dump(exclude={"name", "osm_ref", "cuisine"}),
        name=candidate.name,
        osm_ref=candidate.ref,
        cuisine=", ".join(candidate.cuisine) or None,
        osm_type=candidate.osm_type,
        osm_id=candidate.osm_id,
        lat=candidate.lat,
        lon=candidate.lon,
        address=candidate.address,
        city=candidate.city,
        state=candidate.state,
        website=candidate.website,
        distance_km=round(
            haversine_km(target.lat, target.lon, candidate.lat, candidate.lon), 2
        ),
        source=source,
    )


def _verify(
    proposed: list[Recommendation],
    library: Library,
    shortlist: list[Candidate],
    target: Target,
    off_list: int,
) -> tuple[list[VerifiedRecommendation], list[str]]:
    """Drop anything not on the list, already known, or outside the area.

    Enforced twice, as in the book version: the prompt keeps the model from wasting
    slots, and this makes it a guarantee rather than a request.
    """
    by_ref = {c.ref: c for c in shortlist}
    by_name = {fold(c.name): c for c in shortlist}

    owned_keys = {r.key for r in library.restaurants}
    skipped_keys = store.skipped_keys(library)
    skipped_names = store.skipped_names(library)
    closed_names = {fold(r.name) for r in library.restaurants if r.permanently_closed}

    verified: list[VerifiedRecommendation] = []
    dropped: list[str] = []
    seen: set[str] = set()
    off_list_used = 0
    max_km = target.radius_km * 1.5

    for rec in proposed:
        folded = fold(rec.name)
        candidate = None
        source = "candidate"

        if rec.osm_ref and rec.osm_ref in by_ref:
            candidate = by_ref[rec.osm_ref]
        elif folded in by_name:
            # A transposed digit in the reference shouldn't cost a good pick.
            candidate = by_name[folded]
            source = "ref-corrected"
        else:
            if off_list_used >= off_list:
                dropped.append(f"{rec.name} - off-list budget exceeded")
                continue
            match = _resolve_off_list(rec.name, target)
            if match is None:
                dropped.append(
                    f"{rec.name} - not found in OpenStreetMap near {target.label}"
                )
                continue
            off_list_used += 1
            candidate = match
            source = "off-list-verified"

        if fold(candidate.name) in skipped_names:
            dropped.append(f"{rec.name} - already passed on")
            continue
        if fold(candidate.name) in closed_names:
            dropped.append(f"{rec.name} - you marked this permanently closed")
            continue

        as_restaurant = Restaurant(
            name=candidate.name,
            osm_type=candidate.osm_type,
            osm_id=candidate.osm_id,
            city=candidate.city,
            state=candidate.state,
        )
        if as_restaurant.key in owned_keys or as_restaurant.key in skipped_keys:
            dropped.append(f"{rec.name} - already in your library")
            continue
        if store.find_near(library, candidate.name, candidate.lat, candidate.lon, within_m=120):
            dropped.append(f"{rec.name} - already in your library")
            continue

        if candidate.ref in seen:
            dropped.append(f"{rec.name} - duplicate pick")
            continue

        distance = haversine_km(target.lat, target.lon, candidate.lat, candidate.lon)
        if distance > max_km:
            dropped.append(f"{rec.name} - outside the target area ({distance:.0f}km)")
            continue

        seen.add(candidate.ref)
        verified.append(_hydrate(rec, candidate, target, source))

    return verified, dropped


def _resolve_off_list(name: str, target: Target) -> Candidate | None:
    """Look up a place the model named that wasn't in the shortlist.

    Requires a close name match inside the target area. This proves the venue
    exists in OpenStreetMap; it cannot prove the venue is still open, which is why
    off-list picks are capped and marked in the output.
    """
    elements = overpass.search_name(
        name, target.lat, target.lon, radius_m=int(target.radius_m * 1.5), limit=3
    )
    for element in elements:
        candidate = overpass.to_candidate(element, target.lat, target.lon)
        if candidate is None:
            continue
        similarity = difflib.SequenceMatcher(None, fold(name), fold(candidate.name)).ratio()
        if similarity >= 0.8:
            return candidate
    return None

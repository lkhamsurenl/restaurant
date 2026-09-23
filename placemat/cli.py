"""Command wiring: add, skip, list, recommend, build."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import click

from . import candidates as candidates_mod
from . import nominatim, osm, overpass, store
from .geo import haversine_km
from .models import (
    LocationSource,
    Ratings,
    Recommendations,
    Restaurant,
    Status,
    Target,
    fold,
)

RECOMMENDATIONS_PATH = Path("recommendations.json")


@click.group()
def main() -> None:
    """Track restaurants you've eaten at and get recommendations for where to go next."""


def _resolve_area(city: str | None, zipcode: str | None, country: str):
    """Geocode the area to search in. Zip or city, either is fine as input."""
    query = zipcode or city
    if not query:
        raise click.ClickException(
            'I need a place to search in: try --city "Kapolei, HI" or --zip 96707.'
        )
    place = nominatim.geocode(query, country=country)
    if place is None:
        raise click.ClickException(f"Couldn't find {query!r}. Try a nearby larger city.")
    return place


def _fill_labels(restaurant: Restaurant, area=None, *, state: str | None = None) -> None:
    """Populate city/state/neighbourhood, cheapest trustworthy source first.

    Precedence: the venue's own addr:* tags, then a reverse geocode, then the
    geocoded search area as a last resort. Skipping the reverse call whenever tags
    suffice is the main saving against Nominatim's 1 request/second budget.

    Note what is *not* a source here: the --city string the user typed. That locates
    the search and nothing more. Treating it as a label writes "San Francisco, CA"
    into a field that should read "San Francisco", because the text people type to
    find a city is not the city's name.

    location_source records where the labels actually came from, so a value the user
    can't verify is never presented as one they set.
    """
    if state:
        restaurant.state = state

    if restaurant.city and restaurant.state:
        restaurant.location_source = LocationSource.OSM_TAGS
        return

    if restaurant.has_location:
        found = nominatim.reverse(restaurant.lat, restaurant.lon)  # type: ignore[arg-type]
        had_tags = bool(restaurant.city)
        restaurant.city = restaurant.city or found.city
        restaurant.state = restaurant.state or found.state
        restaurant.neighbourhood = restaurant.neighbourhood or found.neighbourhood
        restaurant.zip = restaurant.zip or found.zip
        restaurant.address = restaurant.address or found.address
        restaurant.location_source = (
            LocationSource.OSM_TAGS if had_tags and restaurant.state else LocationSource.REVERSE
        )

    if area is not None:
        # Whatever is still blank falls back to the area we searched in, which
        # Nominatim has already normalized.
        restaurant.city = restaurant.city or area.city
        restaurant.state = restaurant.state or area.state


def _add_by_address(restaurant: Restaurant, name: str, address: str, country: str) -> None:
    """Locate a place from a written street address, then try to link its OSM record.

    Two steps, cheap to expensive. The address geocode is one indexed query of about
    a second and almost always succeeds. The venue lookup afterwards is an
    unindexed scan, so it is restricted to a 400m radius - enough to find the venue
    standing at that address, and small enough to stay fast even when the place is
    not in OSM at all, which for newer restaurants is common.
    """
    place = nominatim.geocode_address(address, country=country)
    if place is None:
        raise click.ClickException(
            f"Couldn't locate {address!r}. Try --lat/--lon, or --city to search by name."
        )

    restaurant.lat, restaurant.lon = place.lat, place.lon
    restaurant.city, restaurant.state, restaurant.zip = place.city, place.state, place.zip
    restaurant.address = address.split(",")[0].strip()
    restaurant.location_source = LocationSource.MANUAL

    # Enrichment only. The address already gave us everything essential, so an
    # Overpass outage or a venue OSM has never heard of must not lose the entry.
    try:
        matches = overpass.search_name(
            name, place.lat, place.lon, radius_m=400, limit=1, attempts=2
        )
    except osm.TransientError:
        click.echo("  Located from the address; OpenStreetMap was unreachable for details.")
        return

    venue = next(
        (
            c
            for c in (overpass.to_candidate(m, place.lat, place.lon) for m in matches)
            if c is not None
        ),
        None,
    )
    if venue is None:
        click.echo("  Located from the address; OpenStreetMap has no venue record here.")
        return

    # Keep the user's address and labels; take only what OSM knows better.
    restaurant.name = venue.name
    restaurant.osm_type, restaurant.osm_id = venue.osm_type, venue.osm_id
    restaurant.amenity = venue.amenity
    restaurant.cuisine = venue.cuisine
    restaurant.website = venue.website
    restaurant.wikidata = venue.wikidata
    restaurant.brand = venue.brand
    restaurant.lat, restaurant.lon = venue.lat, venue.lon
    click.echo(f"  Matched OpenStreetMap: {venue.name}")


@main.command()
@click.argument("name")
@click.option("--address", "-a",
              help="Full street address. The fastest and most reliable way to locate a place.")
@click.option("--city", "-c", help="Where to look, e.g. 'Kapolei, HI'. Locates the search only.")
@click.option("--zip", "zipcode", help="Where to look, as a US zip. Locates the search only.")
@click.option("--state", help="Override the stored state label.")
@click.option("--rating", "-r", type=click.IntRange(1, 5),
              help="How much you liked it overall, 1-5. The main signal.")
@click.option("--note", "-n", help="Why it landed (or didn't). Still the most useful field.")
@click.option("--food", type=click.IntRange(1, 5), help="Attribute score, 1-5.")
@click.option("--vibe", type=click.IntRange(1, 5), help="Room, decor, atmosphere. 1-5.")
@click.option("--quiet", type=click.IntRange(1, 5), help="5 = pleasantly quiet. 1-5.")
@click.option("--service", type=click.IntRange(1, 5), help="Staff and the wait. 1-5.")
@click.option("--value", type=click.IntRange(1, 5), help="What you got for the price. 1-5.")
@click.option("--dish", "-d", multiple=True, help="Something worth ordering. Repeatable.")
@click.option("--price", type=click.IntRange(1, 4), help="Your read, 1-4 ($ to $$$$).")
@click.option("--status", type=click.Choice([s.value for s in Status]), default="visited")
@click.option("--date", "date_visited", help="When you went. Stored as given.")
@click.option("--tag", "tags", multiple=True, help="Freeform tag. Repeatable.")
@click.option("--lat", type=float, help="Set coordinates by hand; skips the OSM lookup.")
@click.option("--lon", type=float, help="Set coordinates by hand; skips the OSM lookup.")
@click.option("--radius-km", default=12.0, help="How far around the city to search.")
@click.option("--no-lookup", is_flag=True, help="Record exactly what you typed; no network.")
@click.option("--yes", "-y", is_flag=True, help="Accept the top match without confirming.")
@click.option("--country", default="us", help="Country filter for geocoding.")
def add(name, address, city, zipcode, state, rating, note, food, vibe, quiet, service,
        value, dish, price, status, date_visited, tags, lat, lon, radius_km, no_lookup,
        yes, country) -> None:
    """Add a restaurant to your library, enriched from OpenStreetMap."""
    if (lat is None) != (lon is None):
        raise click.ClickException("Pass both --lat and --lon, or neither.")

    restaurant = Restaurant(
        name=name,
        status=Status(status),
        ratings=Ratings(food=food, vibe=vibe, quiet=quiet, service=service, value=value),
    )

    if address and lat is None and not no_lookup:
        _add_by_address(restaurant, name, address, country)
    elif no_lookup:
        restaurant.city, restaurant.state, restaurant.zip = city, state, zipcode
        restaurant.location_source = LocationSource.MANUAL
        click.echo("Recorded as typed, with no lookup.")
    elif lat is not None:
        restaurant.lat, restaurant.lon = lat, lon
        _fill_labels(restaurant, state=state)
    else:
        place = _resolve_area(city, zipcode, country)
        click.echo(f"Searching OpenStreetMap near {place.label} for {name!r}...")
        matches = overpass.search_name(
            name, place.lat, place.lon, radius_m=int(radius_km * 1000)
        )

        chosen = None
        if matches:
            options = [
                c
                for c in (
                    overpass.to_candidate(m, place.lat, place.lon) for m in matches
                )
                if c is not None
            ]
            if options and not yes:
                for i, option in enumerate(options, 1):
                    marker = "→" if i == 1 else " "
                    where = option.address or option.city or ""
                    click.echo(
                        f" {marker} {i}. {option.name} — "
                        f"{option.amenity or 'venue'}"
                        + (f" · {'/'.join(option.cuisine[:2])}" if option.cuisine else "")
                        + (f" — {where}" if where else "")
                        + f" ({option.distance_km:.1f}km)"
                    )
                pick = click.prompt(
                    "Which one? (number, or 0 for none of these)",
                    type=click.IntRange(0, len(options)),
                    default=1,
                )
                chosen = options[pick - 1] if pick else None
            elif options:
                chosen = options[0]

        if chosen is not None:
            restaurant.name = chosen.name
            restaurant.osm_type, restaurant.osm_id = chosen.osm_type, chosen.osm_id
            restaurant.amenity = chosen.amenity
            restaurant.cuisine = chosen.cuisine
            restaurant.website = chosen.website
            restaurant.wikidata = chosen.wikidata
            restaurant.brand = chosen.brand
            restaurant.lat, restaurant.lon = chosen.lat, chosen.lon
            restaurant.address = chosen.address
            restaurant.city, restaurant.state = chosen.city, chosen.state
            restaurant.zip = chosen.zip
            _fill_labels(restaurant, place, state=state)
        else:
            # OpenStreetMap does not have every restaurant - this is a main path,
            # not an edge case - and refusing to record a place we can't find would
            # lose real information.
            click.echo(
                f"OpenStreetMap has no record of {name!r} near {place.label}."
            )
            if not (yes or click.confirm(
                f"Record it anyway, at the {place.city or place.label} centre?",
                default=True,
            )):
                click.echo("Cancelled.")
                return
            restaurant.lat, restaurant.lon = place.lat, place.lon
            restaurant.city = place.city
            restaurant.state = state or place.state
            restaurant.zip = place.zip
            restaurant.location_source = LocationSource.MANUAL
            click.echo(
                f'  To pin it precisely later: eats add "{name}" '
                f"--lat <lat> --lon <lon>"
            )

    restaurant.rating = rating
    restaurant.notes = note
    restaurant.dishes = list(dish)
    restaurant.price = price
    restaurant.date_visited = date_visited
    restaurant.tags = list(tags)

    library = store.load()
    is_new = store.upsert(library, restaurant)
    store.save(library)

    bits = []
    if rating:
        bits.append(f"{rating}/5")
    if restaurant.status is not Status.VISITED:
        bits.append(restaurant.status.value)
    label = f"  [{', '.join(bits)}]" if bits else ""
    verb = "Added" if is_new else "Updated"
    click.echo(f"{verb}: {restaurant.name} — {restaurant.place_label}{label}")
    if restaurant.ratings.any_set:
        click.echo(f"  {restaurant.ratings.summary()}")


@main.command()
@click.argument("name")
@click.option("--city", "-c", help="City, to disambiguate.")
@click.option("--zip", "zipcode", help="US zip, to disambiguate.")
@click.option("--note", "-n", help="Why you passed. Optional, but helps you remember.")
@click.option("--closed", is_flag=True, help="Also mark it permanently closed.")
@click.option("--country", default="us")
def skip(name, city, zipcode, note, closed, country) -> None:
    """Mark a place as not interested, so it stops being recommended.

    For restaurants you've heard of but never ate at - including ones this tool
    suggested and you passed on. Unlike a low rating, this implies no judgment of
    the place, it just suppresses it.
    """
    # A previously recommended place is already described in recommendations.json,
    # so dismissing one costs no lookup at all.
    restaurant = _from_recommendations(name)
    source = "last recommendation"

    if restaurant is None and (city or zipcode):
        source = "OpenStreetMap"
        place = _resolve_area(city, zipcode, country)
        matches = overpass.search_name(name, place.lat, place.lon)
        best = next(
            (
                c
                for c in (
                    overpass.to_candidate(m, place.lat, place.lon) for m in matches
                )
                if c is not None
            ),
            None,
        )
        if best is not None:
            restaurant = Restaurant(
                name=best.name,
                osm_type=best.osm_type,
                osm_id=best.osm_id,
                lat=best.lat,
                lon=best.lon,
                cuisine=best.cuisine,
                city=best.city or place.city,
                state=best.state or place.state,
                zip=best.zip,
            )

    if restaurant is None:
        # Suppression shouldn't depend on the place being findable.
        source = "as given"
        restaurant = Restaurant(name=name, city=city, state=None, zip=zipcode)

    restaurant.status = Status.NOT_INTERESTED
    restaurant.rating = None
    restaurant.notes = note
    if closed:
        restaurant.permanently_closed = True

    library = store.load()
    is_new = store.upsert(library, restaurant)
    store.save(library)

    verb = "Skipping" if is_new else "Updated, now skipping"
    click.echo(f"{verb}: {restaurant.name} — {restaurant.place_label}  ({source})")
    if closed:
        click.echo("Marked permanently closed; it won't be offered again.")
    else:
        click.echo("It won't be recommended again.")


def _from_recommendations(name: str) -> Restaurant | None:
    """Reuse metadata from the last recommend run, if the name is in it."""
    if not RECOMMENDATIONS_PATH.exists():
        return None
    recs = Recommendations.model_validate_json(RECOMMENDATIONS_PATH.read_text())
    target = fold(name)
    for rec in recs.recommendations:
        if fold(rec.name) != target:
            continue
        return Restaurant(
            name=rec.name,
            osm_type=rec.osm_type,
            osm_id=rec.osm_id,
            lat=rec.lat,
            lon=rec.lon,
            address=rec.address,
            city=rec.city,
            state=rec.state,
            website=rec.website,
            cuisine=[c.strip() for c in (rec.cuisine or "").split(",") if c.strip()],
        )
    return None


@main.command(name="list")
@click.option("--min-rating", type=click.IntRange(1, 5), help="Only places rated at least this.")
@click.option("--skipped", is_flag=True, help="Show the not-interested list instead.")
@click.option("--want", is_flag=True, help="Show the want-to-try list instead.")
@click.option("--city", "-c", help="Only places in this city.")
@click.option("--cuisine", help="Only places with this cuisine.")
@click.option("--near", help="Only places near this zip or city, nearest first.")
@click.option("--radius-km", default=30.0, help="Radius for --near.")
@click.option("--country", default="us")
def list_places(min_rating, skipped, want, city, cuisine, near, radius_km, country) -> None:
    """Show what's in your library."""
    library = store.load()

    if skipped:
        wanted_status = Status.NOT_INTERESTED
    elif want:
        wanted_status = Status.WANT_TO_TRY
    else:
        wanted_status = None

    if wanted_status is not None:
        places = [r for r in library.restaurants if r.status is wanted_status]
    else:
        # The skip list is suppression bookkeeping, not part of the collection, so
        # it stays out of the default view.
        places = [r for r in library.restaurants if r.status is not Status.NOT_INTERESTED]

    if min_rating:
        places = [r for r in places if r.rating and r.rating >= min_rating]
    if city:
        places = [r for r in places if fold(r.city or "") == fold(city)]
    if cuisine:
        places = [r for r in places if any(fold(c) == fold(cuisine) for c in r.cuisine)]

    origin = None
    if near:
        place = _resolve_area(None, near, country) if near.isdigit() else _resolve_area(near, None, country)
        origin = (place.lat, place.lon)
        places = [
            r
            for r in places
            if r.has_location
            and haversine_km(place.lat, place.lon, r.lat, r.lon) <= radius_km
        ]

    if not places:
        click.echo("Nothing matches." if (min_rating or city or cuisine or near) else "Library is empty.")
        return

    def line(r: Restaurant) -> str:
        stars = f"  {r.rating}/5" if r.rating else "   - "
        price = " " + "$" * r.price if r.price else ""
        cuisines = f" · {', '.join(r.cuisine[:2])}" if r.cuisine else ""
        approx = " ~" if r.location_source is LocationSource.REVERSE else ""
        dist = ""
        if origin and r.has_location:
            dist = f"  ({haversine_km(*origin, r.lat, r.lon):.1f}km)"
        scores = f"\n        {r.ratings.summary()}" if r.ratings.any_set else ""
        note = f"\n        {r.notes}" if r.notes else ""
        return f"{stars}{price}  {r.name}{cuisines}{approx}{dist}{scores}{note}"

    if origin:
        places.sort(key=lambda r: haversine_km(*origin, r.lat, r.lon))
        for r in places:
            click.echo(line(r))
    else:
        grouped: dict[str, list[Restaurant]] = defaultdict(list)
        for r in places:
            grouped[r.place_label].append(r)
        for label in sorted(grouped, key=lambda k: (-len(grouped[k]), k)):
            click.echo(f"\n{label}  ({len(grouped[label])})")
            for r in sorted(grouped[label], key=lambda r: (-(r.rating or 0), r.name)):
                click.echo(line(r))

    label = {Status.NOT_INTERESTED: "skipped", Status.WANT_TO_TRY: "on the want-to-try list"}.get(
        wanted_status, "places"
    )
    click.echo(f"\n{len(places)} {label}")
    if wanted_status is None:
        n = sum(1 for r in library.restaurants if r.status is Status.NOT_INTERESTED)
        if n:
            click.echo(f"({n} skipped — see `eats list --skipped`)")


@main.command()
@click.argument("target")
@click.option("--count", "-c", default=20, help="How many to ask for.")
@click.option("--radius-km", default=30.0, help="How far you're willing to go.")
@click.option("--limit", default=140, help="Max candidate venues to put in the prompt.")
@click.option("--kind", "kinds", multiple=True,
              help="Venue kinds to consider. Default: restaurant. Repeatable.")
@click.option("--chains/--no-chains", default=False, help="Include chain outlets.")
@click.option("--off-list", default=2, help="How many picks may come from outside OSM data.")
@click.option("--dry-run", is_flag=True,
              help="Build and print the prompt without calling the model.")
@click.option("--country", default="us")
def recommend(target, count, radius_km, limit, kinds, chains, off_list, dry_run, country) -> None:
    """Recommend places to eat near a zip or city, chosen from real OSM venues."""
    from . import recommend as engine

    place = nominatim.geocode(target, country=country)
    if place is None:
        raise click.ClickException(f"Couldn't find {target!r}.")

    resolved = Target(
        query=target,
        label=place.label,
        lat=place.lat,
        lon=place.lon,
        radius_m=int(radius_km * 1000),
    )

    library = store.load()
    click.echo(f"Reading {len(library.restaurants)} places from library.json...")
    click.echo(f"Finding venues near {resolved.label}...")

    shortlist, radius_used, pool = candidates_mod.retrieve(
        resolved,
        library,
        limit=limit,
        kinds=tuple(kinds) or ("restaurant",),
        chains=chains,
    )

    if radius_used != resolved.radius_m:
        click.echo(
            f"  Widened to {radius_used / 1000:.0f}km — only "
            f"{pool} venues were within {radius_km:.0f}km."
        )
    click.echo(f"  {pool} candidates, using the best {len(shortlist)}.")
    if pool < candidates_mod.MIN_POOL:
        click.echo(
            "  Note: OpenStreetMap coverage here is sparse, so these are drawn from "
            "a small pool and good places are likely missing entirely."
        )

    if dry_run:
        prompt = (
            engine.build_profile(library, resolved)
            + "\n\n"
            + candidates_mod.serialize(shortlist, resolved, radius_used, pool)
        )
        click.echo("\n" + prompt)
        click.echo(f"\n--- ~{len(prompt) // 4} tokens, no API call made ---")
        click.echo("\nCuisine spread of the shortlist:")
        for name, n in candidates_mod.histogram(shortlist)[:12]:
            click.echo(f"  {name:22} {n:3}  ({n / len(shortlist):.0%})")
        return

    try:
        result, usage = engine.generate(
            library, resolved, shortlist, radius_used, pool,
            count=count, off_list=off_list,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    RECOMMENDATIONS_PATH.write_text(
        json.dumps(
            result.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False
        )
        + "\n"
    )

    click.echo(f"\n{len(result.recommendations)} recommendations near {resolved.label}:\n")
    for rec in result.recommendations:
        tail = "  (off-list)" if rec.source == "off-list-verified" else ""
        click.echo(
            f"  {rec.name} — {rec.cuisine or 'restaurant'} — "
            f"{rec.distance_km:.1f}km  [{rec.confidence:.0%}]{tail}"
        )
        click.echo(f"    why:    {rec.reason}")
        click.echo(f"    avoids: {rec.avoids}")
        if rec.dish:
            click.echo(f"    order:  {rec.dish}")
        click.echo()

    if result.dropped:
        click.echo(f"Dropped {len(result.dropped)}:")
        for item in result.dropped:
            click.echo(f"  - {item}")

    click.echo(f"\nWritten to {RECOMMENDATIONS_PATH}")
    if usage:
        click.echo(f"Tokens: {usage.input_tokens} in / {usage.output_tokens} out")


@main.command()
@click.option("--title", default="Where We've Eaten", help="Page heading.")
@click.option("--limit", default=20,
              help="Most recommendations to publish, highest confidence first. 0 for all.")
def build(title, limit) -> None:
    """Render the static site to docs/index.html."""
    from . import site

    library = store.load()
    recs = None
    if RECOMMENDATIONS_PATH.exists():
        recs = Recommendations.model_validate_json(RECOMMENDATIONS_PATH.read_text())

    output = site.render(library, recs, title=title, limit=limit or None)
    shown = [r for r in library.restaurants if r.status is not Status.NOT_INTERESTED]
    cities = {r.place_label for r in shown}
    click.echo(f"Wrote {output} ({len(shown)} places, {len(cities)} cities)")


if __name__ == "__main__":
    main()

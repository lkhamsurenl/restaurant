"""Render library.json + recommendations.json into a single static page."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from jinja2 import Template

from . import basemap
from .geo import Viewport, osm_element_url, osm_point_url, scale_bar_km
from .models import Library, LocationSource, Recommendations, Restaurant, Status, fold

# GitHub Pages only serves from the repo root or /docs when deploying from a
# branch - an arbitrary folder like site/ is not selectable.
OUTPUT_DIR = Path("docs")

RATING_COLORS = {5: "#2f7d5c", 4: "#6aa84f", 3: "#c9a227", 2: "#c1663c", 1: "#a63d3d"}


# Rough width of one character at the label's 9.5px sans-serif size. Good enough to
# keep text inside the frame without measuring glyphs.
_CHAR_PX = 5.3
_LINE_PX = 11.0


def _label_box(
    x: float, y: float, dx: float, dy: float, anchor: str, width: float
) -> tuple[float, float, float, float]:
    """The rectangle a label would occupy, for overlap and edge tests."""
    left = {"start": x + dx, "end": x + dx - width, "middle": x + dx - width / 2}[anchor]
    return left, y + dy - _LINE_PX * 0.8, left + width, y + dy + _LINE_PX * 0.2


def _place_label(
    placed: list[tuple[float, float, float, float]],
    x: float,
    y: float,
    text: str,
    view_w: float,
    view_h: float,
) -> tuple[float, float, str] | None:
    """Choose a side for a label that stays in frame and clear of the others.

    Tries right, left, above, below, then the diagonals. Returns None when every
    position collides, which is better than stacking unreadable text: the name is
    still in the circle's tooltip.
    """
    width = len(text) * _CHAR_PX
    for dx, dy, anchor in (
        (9, 3.5, "start"),
        (-9, 3.5, "end"),
        (0, -9, "middle"),
        (0, 15, "middle"),
        (9, -8, "start"),
        (-9, -8, "end"),
        (9, 14, "start"),
        (-9, 14, "end"),
    ):
        box = _label_box(x, y, dx, dy, anchor, width)
        # Must sit fully inside the frame - an overflowing label is simply cut off.
        if box[0] < 4 or box[2] > view_w - 4 or box[1] < 2 or box[3] > view_h - 22:
            continue
        if any(
            box[0] < p[2] and p[0] < box[2] and box[1] < p[3] and p[1] < box[3]
            for p in placed
        ):
            continue
        return dx, dy, anchor
    return None


def _svg(places: list[Restaurant]) -> str | None:
    """A labelled map for one city, with a real coastline, generated at build time.

    Still no tiles and no JavaScript: OpenStreetMap's tile policy rules out a
    published page fetching tiles on every view, and a CDN dependency would break
    the page offline. The coastline is ODbL *data*, pulled once at build time and
    embedded, so it costs nothing at view time and is already covered by the
    attribution in the footer.

    The coastline is what makes this legible. Without it the map was a handful of
    dots in an empty rectangle, with no way to tell which side of town anything was
    on - the honest ceiling noted when this started as a bare scatter.
    """
    located = [r for r in places if r.has_location]
    if len(located) < 2:
        return None

    points = [(r.lat, r.lon) for r in located]  # type: ignore[misc]
    view = Viewport(points)

    layers: list[str] = []

    # Coastline first, so it sits under the dots.
    paths = basemap.coastline(
        view.south, view.west, view.north, view.east, view.tolerance_deg
    )
    for path in paths:
        xy = [view.xy(lat, lon) for lat, lon in path]
        # Keep any way that passes near the view; clipping exactly is not worth it
        # when the SVG viewBox already crops.
        if not any(-80 <= x <= view.width + 80 and -80 <= y <= view.height + 80
                   for x, y in xy):
            continue
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in xy)
        layers.append(
            f'<path d="{d}" fill="none" stroke="var(--coast)" stroke-width="1.4" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )

    dots: list[str] = []
    labels: list[str] = []
    placed: list[tuple[float, float, float, float]] = []
    # Best-rated last, so they end up on top of the pile, and labelled first, so
    # they win the argument over where a label goes.
    ordered = sorted(located, key=lambda r: -(r.rating or 0))
    for restaurant in ordered:
        x, y = view.xy(restaurant.lat, restaurant.lon)  # type: ignore[arg-type]
        name = _shorten(restaurant.name)
        spot = _place_label(placed, x, y, name, view.width, view.height)
        if spot is None:
            continue
        dx, dy, anchor = spot
        placed.append(_label_box(x, y, dx, dy, anchor, len(name) * _CHAR_PX))
        labels.append(
            f'<text x="{x + dx:.1f}" y="{y + dy:.1f}" text-anchor="{anchor}" '
            f'class="lbl">{_escape(name)}</text>'
        )

    for restaurant in reversed(ordered):
        x, y = view.xy(restaurant.lat, restaurant.lon)  # type: ignore[arg-type]
        color = RATING_COLORS.get(restaurant.rating or 0, "#9b9892")
        radius = 6 if restaurant.is_positive else 4.5
        title = restaurant.name + (f" ({restaurant.rating}/5)" if restaurant.rating else "")
        dots.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}" '
            f'fill-opacity=".9" stroke="var(--card)" stroke-width="1.5">'
            f"<title>{_escape(title)}</title></circle>"
        )

    bar_km = scale_bar_km(points)
    bar_px = min(bar_km / max(view.km_per_px, 1e-9), view.width - 80)
    baseline = view.height - 16

    return (
        f'<svg class="scatter" viewBox="0 0 {view.width:.0f} {view.height:.0f}" '
        f'role="img" aria-label="Where these places are, with the coastline for reference">'
        f'<rect x="1" y="1" width="{view.width - 2:.0f}" height="{view.height - 2:.0f}" '
        f'fill="none" stroke="var(--line)" rx="6"/>'
        + "".join(layers)
        + "".join(dots)
        + "".join(labels)
        + f'<line x1="18" y1="{baseline:.1f}" x2="{18 + bar_px:.1f}" '
        f'y2="{baseline:.1f}" stroke="var(--muted)" stroke-width="2"/>'
        f'<text x="18" y="{baseline - 6:.1f}" class="lbl">{bar_km:g}km</text>'
        f"</svg>"
    )


_NOISE_WORDS = (
    " Restaurant", " Cuisine", " & Lounge", " Ristorante", " Kitchen & Bar",
)


def _shorten(name: str, limit: int = 20) -> str:
    """Trim a name to something a map label can carry.

    Drops the generic half first - "Olay's Thai Lao Cuisine" reads fine as "Olay's
    Thai Lao" - and only falls back to clipping mid-word when that isn't enough.
    """
    for word in _NOISE_WORDS:
        if len(name) > limit and word in name:
            name = name.replace(word, "", 1).strip()
    if len(name) <= limit:
        return name
    return name[: limit - 1].rstrip(" ,-") + "…"


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


TEMPLATE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #fbf9f6; --fg: #1c1a18; --muted: #6f6a63;
    --card: #ffffff; --line: #e7e1d8; --accent: #b4552d; --coast: #c7d6da;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #17161a; --fg: #ecebe8; --muted: #9b9892;
            --card: #201f25; --line: #34313b; --accent: #e08a5f;
            --coast: #3c4f56; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
         font: 16px/1.6 ui-serif, Georgia, serif; }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 3rem 1.5rem 5rem; }
  h1 { font-size: 2rem; margin: 0 0 .25rem; letter-spacing: -.02em; }
  .sub { color: var(--muted); margin: 0 0 2.5rem; font-size: .95rem; }
  h2 { font-size: 1.05rem; text-transform: uppercase; letter-spacing: .1em;
       color: var(--muted); font-weight: 600; margin: 3rem 0 .5rem;
       padding-bottom: .5rem; border-bottom: 1px solid var(--line); }
  h2 .qual { text-transform: none; letter-spacing: 0; font-weight: 400; }
  h3 { font-size: 1.15rem; margin: 2rem 0 1rem; font-weight: 600; }
  h3 .count { color: var(--muted); font-weight: 400; font-size: .85rem; }
  .grid { display: grid; gap: 1rem;
          grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }
  .card { background: var(--card); border: 1px solid var(--line);
          border-radius: 10px; padding: 1rem 1.1rem; }
  .card .name { font-weight: 600; font-size: 1.05rem; }
  .card a.name { color: inherit; text-decoration: none; }
  .card a.name:hover { color: var(--accent); }
  .meta { color: var(--muted); font-size: .85rem; margin-top: .15rem; }
  .note { margin-top: .6rem; font-size: .95rem; }
  .dishes { margin-top: .4rem; font-size: .85rem; color: var(--muted); font-style: italic; }
  .stars { color: var(--accent); font-weight: 600; font-variant-numeric: tabular-nums; }
  .pill { display: inline-block; font-size: .7rem; text-transform: uppercase;
          letter-spacing: .06em; padding: .12rem .45rem; border-radius: 4px;
          border: 1px solid var(--line); color: var(--muted); margin-left: .35rem;
          vertical-align: .1em; }
  .pill.off { border-color: var(--accent); color: var(--accent); }
  .why { margin-top: .6rem; font-size: .92rem; }
  .why b { font-weight: 600; color: var(--muted); font-size: .78rem;
           text-transform: uppercase; letter-spacing: .06em; display: block; }
  .scatter { width: 100%; height: auto; margin: .5rem 0 1.5rem;
             background: var(--card); border-radius: 10px; }
  .scatter .lbl { font-size: 9.5px; fill: var(--muted);
                  font-family: ui-sans-serif, system-ui, sans-serif; }
  .chips span { display: inline-block; font-size: .75rem; padding: .1rem .4rem;
                border: 1px solid var(--line); border-radius: 999px;
                margin: .2rem .2rem 0 0; color: var(--muted); }
  .scores { display: flex; flex-wrap: wrap; gap: .1rem .9rem; margin-top: .6rem;
            font-size: .78rem; color: var(--muted); }
  .scores div { display: flex; align-items: center; gap: .3rem; }
  .scores i { font-style: normal; letter-spacing: .06em; color: var(--accent); }
  .scores i .off { color: var(--line); }
  .stats { display: flex; flex-wrap: wrap; gap: 1.5rem; margin: 0 0 1rem;
           color: var(--muted); font-size: .9rem; }
  .stats b { color: var(--fg); font-size: 1.3rem; display: block;
             font-variant-numeric: tabular-nums; }
  footer { margin-top: 4rem; padding-top: 1.5rem; border-top: 1px solid var(--line);
           color: var(--muted); font-size: .82rem; }
  footer a { color: var(--muted); }
  table { width: 100%; }
</style>
</head>
<body>
<div class="wrap">
  <h1>{{ title }}</h1>
  <p class="sub">A personal record of where we've eaten, and what was worth it.
  {%- if household.has_constraints %} Every suggestion below has to feed a
  {{ household.diets|join(' and ') }} diner.{% endif %}</p>

  <div class="stats">
    <div><b>{{ places|length }}</b> places</div>
    <div><b>{{ cities|length }}</b> cities</div>
    <div><b>{{ loved|length }}</b> rated 4+</div>
    {% if avg %}<div><b>{{ avg }}</b> average</div>{% endif %}
  </div>

  {% if picks %}
  <h2>Recommended near {{ recs.target.label }}
    <span class="qual">— {{ picks|length }} picks within
    {{ (recs.radius_used_m / 1000)|round|int }}km,
    from {{ recs.candidates_considered }} of {{ recs.candidates_available }} venues
    {%- if withheld %}; {{ withheld }} lower-confidence picks not shown{% endif %}</span>
  </h2>
  <div class="grid">
  {% for rec in picks %}
    <div class="card">
      {% if rec.osm_url %}
      <a class="name" href="{{ rec.osm_url }}">{{ rec.name }}</a>
      {% else %}<span class="name">{{ rec.name }}</span>{% endif %}
      {% if rec.source == 'off-list-verified' %}<span class="pill off">off-list</span>{% endif %}
      <div class="meta">
        {{ rec.cuisine or 'restaurant' }}
        {% if rec.distance_km is not none %} · {{ '%.1f'|format(rec.distance_km) }}km{% endif %}
        · {{ (rec.confidence * 100)|round|int }}% confidence
      </div>
      <div class="why"><b>Why</b>{{ rec.reason }}</div>
      <div class="why"><b>Avoids</b>{{ rec.avoids }}</div>
      {% if rec.dish %}<div class="why"><b>Order</b>{{ rec.dish }}</div>{% endif %}
      {% if rec.diet_fit %}<div class="why"><b>{{ diet_label }}</b>{{ rec.diet_fit }}</div>{% endif %}
    </div>
  {% endfor %}
  </div>
  {% endif %}

  <h2>The library</h2>
  {% for group in groups %}
    <h3>{{ group.label }} <span class="count">— {{ group.places|length }} places</span></h3>
    {% if group.svg %}{{ group.svg }}{% endif %}
    <div class="grid">
    {% for r in group.places %}
      <div class="card">
        {% if r.osm_url %}
        <a class="name" href="{{ r.osm_url }}">{{ r.name }}</a>
        {% else %}<span class="name">{{ r.name }}</span>{% endif %}
        {% if r.status.value == 'want-to-try' %}<span class="pill">want to try</span>{% endif %}
        {% if r.boycott %}<span class="pill off">won't return</span>{% endif %}
        <div class="meta">
          {% if r.rating %}<span class="stars">{{ r.rating }}/5</span> · {% endif %}
          {% if r.price %}{{ '$' * r.price }} · {% endif %}
          {{ r.neighbourhood or r.city or '' }}
          {% if r.approx %} · <span title="Location label is approximate">approx</span>{% endif %}
        </div>
        {% if r.cuisine %}
        <div class="chips">{% for c in r.cuisine[:4] %}<span>{{ c }}</span>{% endfor %}</div>
        {% endif %}
        {% if r.score_bars %}
        <div class="scores">
          {% for label, filled in r.score_bars %}
          <div><span>{{ label }}</span><i>{{ '●' * filled }}<span class="off">{{
            '●' * (5 - filled) }}</span></i></div>
          {% endfor %}
        </div>
        {% endif %}
        {% if r.notes %}<div class="note">{{ r.notes }}</div>{% endif %}
        {% if r.dishes %}<div class="dishes">{{ r.dishes|join(', ') }}</div>{% endif %}
      </div>
    {% endfor %}
    </div>
  {% endfor %}

  <footer>
    {% if recs %}Recommendations generated {{ recs.generated_at }} with {{ recs.model }},
    chosen from real venues near {{ recs.target.label }}.<br>{% endif %}
    Venue data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>
    contributors, <a href="https://opendatacommons.org/licenses/odbl/">ODbL</a>.
  </footer>
</div>
</body>
</html>
"""
)


class _View:
    """A model plus presentation extras, since Pydantic rejects stray attributes."""

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self.osm_url = osm_element_url(wrapped.osm_type, wrapped.osm_id)

    def __getattr__(self, item):
        return getattr(self._wrapped, item)


class _Shown(_View):
    """A restaurant as the page presents it."""

    def __init__(self, restaurant: Restaurant) -> None:
        super().__init__(restaurant)
        if not self.osm_url and restaurant.has_location:
            self.osm_url = osm_point_url(restaurant.lat, restaurant.lon)  # type: ignore[arg-type]
        self.approx = restaurant.location_source is LocationSource.REVERSE
        # Only the attributes actually scored. An unscored one is left out entirely
        # rather than drawn empty, since a blank is not a zero.
        self.score_bars = list(restaurant.ratings.scored().items())


def render(
    library: Library,
    recs: Recommendations | None = None,
    output_dir: Path = OUTPUT_DIR,
    title: str = "Where We've Eaten",
    limit: int | None = 20,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Skipped places are suppression bookkeeping, not part of the collection.
    shown = [r for r in library.restaurants if r.status is not Status.NOT_INTERESTED]

    by_city: dict[str, list[Restaurant]] = defaultdict(list)
    for restaurant in shown:
        by_city[restaurant.place_label].append(restaurant)

    groups = []
    for label in sorted(by_city, key=lambda k: (-len(by_city[k]), k)):
        places = sorted(by_city[label], key=lambda r: (-(r.rating or 0), r.name))
        groups.append(
            {"label": label, "places": [_Shown(r) for r in places], "svg": _svg(places)}
        )

    # Publish the most confident picks first, and cap how many reach the page.
    # How many to *ask* for and how many are worth *showing* are separate calls: a
    # long run is useful for choosing from, but twenty-odd cards ahead of the
    # library would bury it.
    picks: list[_View] = []
    stale = 0
    if recs:
        # A recommendation goes stale the moment you act on it. Adding a suggested
        # place to the library leaves it sitting in recommendations.json, and
        # publishing "you should try this" for somewhere already rated is plainly
        # wrong - so drop those here rather than making a fresh API call the price of
        # an accurate page.
        known = {r.key for r in library.restaurants}
        known_names = {fold(r.name) for r in library.restaurants}
        fresh = [
            r
            for r in recs.recommendations
            if fold(r.name) not in known_names
            and f"osm:{r.osm_type}/{r.osm_id}" not in known
        ]
        stale = len(recs.recommendations) - len(fresh)
        ranked = sorted(fresh, key=lambda r: -r.confidence)
        picks = [_View(r) for r in (ranked[:limit] if limit else ranked)]

    scores = [r.rating for r in shown if r.rating]
    html = TEMPLATE.render(
        title=title,
        places=shown,
        groups=groups,
        cities=set(by_city),
        loved=[r for r in shown if r.is_positive],
        avg=f"{sum(scores) / len(scores):.1f}" if scores else None,
        recs=recs,
        picks=picks,
        withheld=(len(recs.recommendations) - stale - len(picks)) if recs else 0,
        household=library.household,
        diet_label=(
            " / ".join(library.household.diets)
            if library.household.has_constraints
            else "Diet"
        ),
    )
    output = output_dir / "index.html"
    output.write_text(html, encoding="utf-8")
    return output

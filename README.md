# placemat

Track the restaurants you've eaten at, and get recommendations reasoned from what you
actually said about them.

Your library is a single `library.json` in this repo — plain text, easy to diff, easy to
hand-edit. Recommendations come from Claude reading your notes about *why* a place landed,
then choosing from a list of real venues pulled from OpenStreetMap near wherever you're
asking about.

## Why not just use Yelp

The interesting signal in a personal list isn't the star rating — it's the sentence you'd
write about why a place worked. A ratings matrix can't represent "the wood oven does all
the work; noisy in a good way," but that sentence is exactly what makes a good
recommendation possible.

Knowing what didn't land matters just as much. Places rated 1-2 are sent to the model as an
explicit avoid-list, and each recommendation has to name which trait it steers clear of.

## Notes and scores do different jobs

Each place carries an overall `rating`, a free-text `note`, and optional per-attribute
scores: **food, vibe, quiet, service, value**. All five run 1-5 and all are
higher-is-better — which is why the noise dimension is stored as `quiet`, where 1 means
unpleasantly loud. Mixed directions would make averaging meaningless, and averaging is the
whole point.

They are not redundant, and neither replaces the other:

- **Scores make comparison possible.** With them, the prompt can state *calibration* — if
  you average 3.5 on vibe and 4.4 on food, a vibe 4 from you is warm praise, not a shrug.
  More usefully, comparing each attribute's average on your poorly-rated places against
  your good ones surfaces **what actually sinks a meal for you**, which is not necessarily
  the thing you write about most.
- **The note keeps what no schema anticipates.** "Dog friendly", "they have a tablet you
  can use to order", "never a line", "authentic Italian" — a fixed set of attributes would
  have discarded all of it, and those specifics are what let a recommendation cite
  something concrete instead of praising a restaurant in general.

The attribute set is deliberately small. It was chosen from the dimensions that recurred
three or more times in real notes, because a wider schema sits mostly empty and **a sparse
score is worse than no score**: a missing `service` value reads like a judgment that was
made and came out blank, when in fact none was ever formed. Add scores where you have a
view and leave the rest alone — a blank is not a zero anywhere in the code.

Every score is optional, so nothing forces you to fill in five numbers per meal. In
practice one or two per place, plus the sentence, is plenty.

## Why the model doesn't pick from memory

This is the one real difference from a book recommender, and it shapes everything.

A model knows books broadly and permanently, so you can ask it blind and check its answers
afterwards. Restaurants aren't like that: knowledge is thinner, intensely local, and goes
stale every time a place closes. So `recommend` works the other way around — it pulls the
real venues near your target out of OpenStreetMap first, then asks the model to choose among
them and explain why.

The model supplies judgment. OpenStreetMap supplies the facts. A recommendation for a
restaurant that doesn't exist is therefore not a thing that can happen, rather than
something filtered out after the fact.

The tradeoff is that OpenStreetMap has no ratings, no reviews and no popularity data at all.
Whether a place is any *good* comes from your own notes plus whatever the model knows about
that specific named venue — which is why `confidence` is often honestly low, and why the
prompt tells it to say so rather than dress up a guess.

## Setup

```bash
python3.11 -m venv .venv          # 3.11+; the repo uses StrEnum
.venv/bin/pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
export RESTAURANT_CONTACT_EMAIL=you@example.com
```

`RESTAURANT_CONTACT_EMAIL` is **required**, not optional. OpenStreetMap's usage policy makes
an identifying User-Agent a condition of access to the public Nominatim instance, and
unidentified clients get blocked. Requests are throttled to 1/sec and cached in `.cache/`
to stay inside that policy.

## Usage

Add places you've eaten. The rating is the main signal, and `--note` is the most valuable
field you can fill in:

```bash
eats add "Onkee Korean Grill House" --address "1000 Auahi St, Honolulu, HI 96814" \
  --rating 5 --food 5 --service 5 --value 5 \
  --note "very good food, there is never a line; lunch specials are good deals"

eats add "Monkeypod Kitchen" --address "92-1048 Olani St, Kapolei, HI 96707" \
  --rating 3 --food 4 --quiet 1 \
  --note "food was good, music was too loud"
```

**`--address` is the fast path.** A street address is one indexed geocode taking about a
second, where searching by venue name is an unindexed scan of every venue in the radius and
can take a minute — newer or smaller restaurants are frequently missing from OpenStreetMap
entirely while their address resolves fine. `--city` or `--zip` still work when you don't
have the address; either one locates the search only.

Ratings are 1-5; 4-5 is the taste to extend, 1-2 the avoid-list. Attribute scores
(`--food`, `--vibe`, `--quiet`, `--service`, `--value`) are all optional — fill in the ones
you have a view on. Use `-y` to accept the top match without confirming.

**Places are identified by city and state, not zip.** Many venues have no postcode in
OpenStreetMap at all, and a zip inferred from coordinates is only approximate — reverse
geocoding puts Zuni Café in 94143 when it's really 94102. Entries whose labels came from
that inference are marked `approx` in `list` and on the page. Nothing in the code ever
matches on a zip; proximity is always computed from coordinates.

If OpenStreetMap has never heard of somewhere, it still gets recorded — at the city centre,
with a note telling you how to pin it:

```bash
eats add "Some Neighbourhood Spot" --city Kapolei --rating 4 --lat 21.3357 --lon -158.0847
```

This happens more than you'd expect. OpenStreetMap does not have every restaurant the way
a book catalogue has every book.

Pass on a place so it stops coming up:

```bash
eats skip "Loud Brunch Place"
eats skip "That Closed Bistro" --closed    # also stops OSM from re-offering it
```

Skipping something from the last `recommend` run reuses metadata already in
`recommendations.json`, so it costs no lookup.

Two distinctions worth keeping straight:

| | Meaning | Effect on recommendations |
|---|---|---|
| `--rating 1-2` | You ate there and it wasn't good | Avoid-list: the model infers what was wrong |
| `eats skip` | Never went, not interested | Suppressed, teaches nothing about taste |

See what you have:

```bash
eats list
eats list --min-rating 4
eats list --near 96707        # nearest first
eats list --skipped
```

Get recommendations:

```bash
eats recommend 96707                    # 20 places within 30km
eats recommend "Kapolei, HI" --count 5
eats recommend 96707 --radius-km 10     # keep it close
eats recommend 96707 --kind restaurant --kind cafe
eats recommend 96707 --dry-run          # see the prompt, spend nothing
```

Expect quality to taper down the list. The first few picks are the ones the reasoning really
supports; by the twentieth the model is working further from your notes, and the
`confidence` values should show it. Read a long run as a shortlist to browse, not twenty
equally good suggestions.

**The default radius is 30km, and that number matters more than any other setting.** Within
10km of 96707 there are 29 independent restaurants; within 30km there are 683, because the
drive reaches Honolulu. If results feel thin, widen before concluding the data is bad.

Distance is deliberately almost absent from the ranking — a better fit 25km away beats a
mediocre one nearby. A small share of slots is still reserved for genuinely close places, so
a walkable option is always on the list; without that reserve, city-centre venues won on
metadata completeness every time and the nearest suggestion was 6.5km away.

`--dry-run` is worth using before any real run. It prints the assembled prompt, a token
estimate and the cuisine spread of the shortlist, without calling the API.

Render the static site:

```bash
eats build                  # -> docs/index.html, top 20 picks
eats build --limit 8        # publish fewer
eats build --limit 0        # publish every pick from the last run
```

`--limit` orders picks by confidence and caps how many reach the page. How many to *ask*
for and how many are worth *showing* are separate decisions — a long run is useful to browse
privately, but a wall of cards ahead of the library buries it. The heading says how many
were withheld.

Open `docs/index.html` directly, or publish it with GitHub Pages: Settings → Pages → deploy
from branch `main`, folder `/docs`.

The output goes to `docs/` rather than `site/` because Pages only offers the repo root or
`/docs` when deploying from a branch. The generated page is committed rather than ignored:
it contains nothing that isn't already in the JSON files, and committing it is what lets
Pages serve the site without a build step. The tradeoff is that `docs/index.html` shows up in
the diff whenever you run `eats build` — run it as the last step before committing, not after
every `add`.

## Files

| Path | What it is |
|---|---|
| `library.json` | Your places. The thing worth backing up. |
| `recommendations.json` | Last run's output, with reasons and anything dropped. |
| `placemat/models.py` | Schema — start here to add a field. |
| `placemat/candidates.py` | Retrieval, filtering and ranking. The heart of it. |
| `placemat/recommend.py` | The prompt and the verification pass. |
| `placemat/overpass.py` | Venue search. Read the regex comments before touching them. |
| `placemat/nominatim.py` | Geocoding, and the accuracy caveats. |
| `placemat/site.py` | The HTML template. |

OpenStreetMap responses are cached in `.cache/`. Area listings expire after 30 days, because
a stale list recommends restaurants that have closed. Safe to delete.

## Customizing

- **`SYSTEM` in `placemat/recommend.py`** — the prompt. If recommendations feel too safe,
  this is the dial.
- **Scoring in `placemat/candidates.py`** — which venues make the shortlist at all. The
  weights are set against measured OpenStreetMap tag coverage, and the three reserves
  (cuisine cap, wildcard, nearby) exist to stop the raw score collapsing the list into one
  cuisine or one neighbourhood.
- **`placemat/models.py`** — add a field to `Restaurant` and it flows through the CLI, the
  store and the prompt.

To check whether a prompt change actually helped, run `--dry-run` twice — once normally,
once with your low-rated places removed from the profile — and compare. If the two look the
same, the avoid-list isn't influencing anything, however convincing the `avoids` text sounds.

Costs roughly $0.05 per `recommend` run on Claude Opus 5.

## Notes

Venue data and coordinates come from [OpenStreetMap](https://www.openstreetmap.org), under
the [ODbL](https://opendatacommons.org/licenses/odbl/). No API key needed; attribution is a
licence condition, not a courtesy, so leave the footer credit in place.

The map on the generated page is a build-time SVG scatter rather than an interactive map.
That's deliberate: an embedded map would mean a CDN script dependency and every page view
hitting OpenStreetMap's tile servers, which their tile usage policy forbids for third-party
sites. The scatter shows clustering and nothing else; each place links through to OSM for a
real map.

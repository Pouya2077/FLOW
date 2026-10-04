# FLOW: Flood-Obstructed Roads

A website for first responders that shows which streets are flooded, and how much of each, using
radar satellite data. It also marks critical buildings (hospitals, fire and police stations,
shelters, care homes and more) and says whether water was detected at each one. Built for
StormHacks 2026 (ALEASAT track). See [CLAUDE.md](CLAUDE.md) for the full product context, data
contracts and radar caveats.

Radar shows where water is, not how deep it is, so the site says "water detected", never
"impassable". Every result shows when the satellite took the picture.

## Getting started

```
uv sync                                  # install dependencies (like npm install)
uv run python manage.py runserver        # http://127.0.0.1:8000
uv run pre-commit install                # run Ruff automatically on each commit (once per clone)
```

API docs are generated automatically at http://127.0.0.1:8000/api/docs.

The Abbotsford demo works straight away: its data is committed in `data/locations/`. **Searching
a new place** downloads radar images, which needs a free
[Copernicus Data Space](https://dataspace.copernicus.eu/) account. Put its login in a `.env` file
in the repo root (gitignored):

```
CDSE_USERNAME=you@example.com
CDSE_PASSWORD=...
```

## Commands

Python has no `"scripts"` section like `package.json`, so these are the commands to use. Prefix
everything with `uv run` so it runs inside the project's environment.

| Task | Command |
|---|---|
| Install / sync dependencies | `uv sync` |
| Dev server | `uv run python manage.py runserver` |
| Tests | `uv run python manage.py test` |
| Django checks | `uv run python manage.py check` |
| Lint | `uv run ruff check .` (add `--fix` to auto-fix) |
| Format | `uv run ruff format .` |
| Pre-commit on all files | `uv run pre-commit run --all-files` |
| Add a dependency | `uv add <pkg>` (dev-only: `uv add --dev <pkg>`) |
| Rebuild the Abbotsford demo from real radar (~3–4 min) | `uv run python -m pipeline.detect_flood` |
| Rebuild only its critical buildings | `uv run python -m pipeline.build_facilities sumas-prairie --mask data/masks/sumas-prairie_s1.tif` |
| Make a hand-drawn test flood mask | `uv run python -m pipeline.map_mask sumas-prairie` |

Restart the dev server after rebuilding data: files are read once per process.

## Using the map

The layout follows Google Maps: a full-screen map with a few controls floating over it. Controls
are see-through until you hover or use them.

**Roads have three states, never two.** Blue means water was detected. Solid grey means the road
was observed and is clear. Dashed grey means the satellite didn't see it. The window where flood
data exists is outlined in slate blue. Everything outside it is hatched, so a road there never
looks safe, and roads leading into the window get short dotted stubs.

**Search (top left).**
- Type a place. While you're typing, the magnifying glass turns into a green Enter button, for
  anyone who'd rather click than press Enter. Suggestions appear as you type.
- After a search the map fits to the whole 10 km analysis window around the place, not the
  address itself, so the window's outline stays in view.
- While the satellite analysis runs (1–2 minutes), a dashed outline marks the window and a spinner
  sits in its middle: "Analysing the latest satellite pass for **place**. This may take a minute."
- When it finishes, the outline turns solid and the flooded roads appear.
- Click a window's outline to see its size, e.g. "Satellite window: 10 km × 10 km".
- There's no message box under the search bar: a search with no results, or a failed analysis,
  currently ends without a message.

**Recent.** Clicking into the empty search box opens "Recent". It holds at most 5 areas, newest at
the top and oldest at the bottom. The Abbotsford demo counts as the oldest and drops off after
five searches. Choosing an entry flies back to that window. There are no user accounts, so
everyone using the same server sees the same list.

**Critical buildings.** Each one is a lucide icon on a round badge. The badge's ring uses the same
three states as roads: a blue ring for water detected at the site, a plain badge for observed
clear, and a dashed, faded badge for not observed. Click an icon for its name, address, phone,
email and website (whatever OpenStreetMap has), the share of the site where water was detected,
and the satellite and map dates.

**Settings (gear, top right).** Hovering turns the gear; clicking it unrolls two options. One
circle switches between light and dark mode; its icon shows the current mode, and the choice is
remembered in a cookie. The other shows or hides the critical buildings, with a thick outline
while they're shown. Click the gear again, or press Escape, to close it.

## How the project fits together

In short: **`config/` sets up the server, `flood/` is the website, `pipeline/` turns radar into
data, and `data/` holds the files the website serves.**

```
config/                 project settings and top-level routing
flood/                  the app: API, page, data loading, background searches, tests
pipeline/               offline Python: radar -> flood mask -> flooded roads + critical buildings
data/locations/<slug>/  committed demo data, one folder per location
data/searches/<slug>/   areas people searched (gitignored, same layout)
data/masks/, data/radar/, cache/   intermediate files and OpenStreetMap cache (gitignored)
manage.py               Django's command-line tool (run server, tests)
pyproject.toml          dependencies + Ruff config (Python's package.json)
uv.lock                 locked versions (Python's package-lock.json)
```

### `config/`: settings and routing

Every Django project has one of these. Think of it as the app shell, like `next.config.js` plus
the root router in a React app.

- `settings.py` is global configuration:
  - which apps are installed, and that there's no database
  - where the demo data lives (`FLOOD_DATA_DIR`) and where searches are saved (`SEARCH_DATA_DIR`)
  - which geocoder to call (Photon, the free service that turns a typed place into coordinates)
  - the basemap styles for light and dark mode, and the theme cookie
- `urls.py` is the top-level router: `/` goes to the page view, `/api/...` to the API.
- `wsgi.py` / `asgi.py` are entry points for production servers. You won't need to touch them.

The app reads `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` (`1` or `0`) and `DJANGO_ALLOWED_HOSTS`
(comma-separated) from the environment. The defaults work for local development.

### `flood/`: the app

Django splits a project into "apps", meaning feature modules. This project has just one.

**`data.py`: reads the files.** It's the only code that reads location data from disk.
- `locations()` lists every location (demo and searched) with its name, bounding box and footprint.
- `segments(slug)`, `streets(slug)` and `facilities(slug)` load one location's road segments,
  street totals and critical buildings.
- `searched()` lists searched areas, newest first; it feeds the Recent list.
- `locations_in()` finds which locations overlap the current map view.
- Files are read once and kept in memory. `reload()` forgets them when a search finishes; after
  rebuilding the demo data by hand, **restart the server**.

**`api.py`: the JSON endpoints, the backend the frontend `fetch()`es from.** Built with Django
Ninja.

| Route | What it returns |
|---|---|
| `GET /api/locations` | Every location we have data for |
| `GET /api/meta?location=<slug>` | One location's details: observation time, time zone, sensor, footprint |
| `GET /api/flood?bbox=minx,miny,maxx,maxy&location=<slug>` | Road segments in view, each `flooded`, `clear` or `no_data`, as GeoJSON MapLibre draws directly |
| `GET /api/facilities?bbox=...&location=<slug>` | Critical buildings in view, with their flood status |
| `GET /api/streets?bbox=...&location=<slug>` | Per-street totals ("640 m of 800 m flooded"), most flooded first |
| `POST /api/analyze?name=&lon=&lat=` | Starts analysing the 10 km square around a searched place, or reuses an area that already covers it |
| `GET /api/analyze/<slug>` | That analysis's status: `queued`, `running`, `done` or `failed` |
| `GET /api/geocode?q=<text>` | Turns typed text into places using Photon, cached for a day |

The `bbox` ("bounding box") parameter is the visible map rectangle in longitude/latitude, so the
backend only returns what's on screen. `location=` limits results to one window, because the map
shows one window at a time.

**`jobs.py`: background searches.** An analysis takes 1–2 minutes, far too long for a web request,
so a search starts a job on a background thread (one at a time) and the page polls its status.
Results are written to `data/searches/<slug>/` and survive a restart; job status doesn't.

**`views.py`: serves the HTML page.** `index` picks the theme, the window to open on and the
Recent list, then renders the template.

**`templates/flood/index.html` and `static/flood/`: the page.** `map.js` holds all the map
logic: the MapLibre map, search and Recent, the analysis polling and spinner, building icons and
popups, and the settings menu. `app.css` holds every colour as a token derived from the basemap,
in light and dark versions, so map colours change in CSS only. Icons are lucide, rendered by
the template as inline SVG. (The `templates/flood/` nesting is a Django convention to avoid name
clashes between apps.)

**`tests.py`: tests that need no network or database.** They cover the API routes, data folders,
background jobs (run inline), flood detection, the Recent list, themes and the critical-buildings
pipeline. Geocoder and satellite calls are mocked.

### `pipeline/`: from radar to map data

Plain Python run as modules from the repo root, never during a web request.

- `regions.py` lists named regions (the Abbotsford demo) and builds the 10 km square around a
  searched place.
- `detect_flood.py` finds a Sentinel-1 radar image of the flood and a pre-flood reference from the
  same orbit track, streams just the area needed, and marks pixels that turned dark as water
  (change detection). Then it runs the road overlay.
- `build_location.py` fetches roads from OpenStreetMap, splits them into ~20 m pieces, scores each
  piece against the water mask, merges the pieces back into runs and writes the location's files.
- `build_facilities.py` finds critical buildings in the window, using OpenStreetMap as it was on
  the observation date, scores each site against the same mask, and writes `facilities.geojson`.
  Building types, their OpenStreetMap tags and icons are all in `facility_kinds.py`.
- `map_mask.py` turns a hand-drawn test flood (`test_floods/<slug>.geojson`) into a mask, so the
  website can be built without waiting on real radar.

### `data/`: the files the website serves

Each location folder holds:

| File | Contents |
|---|---|
| `meta.json` | `name`, `bbox`, `observed_utc`, `timezone`, `sensor`, `synthetic`, `footprint` (the outlined window) |
| `segments.geojson` | Road segments with `name`, `highway`, `osm_way_ids`, `status`, `flooded_fraction`, `confidence`, `observed_utc` |
| `streets.json` | Per-street summaries: `name`, `total_m`, `flooded_m`, `no_data_m`, `pct`, `observed_utc`, `bbox` |
| `facilities.geojson` | Critical buildings: `kind`, `name`, `address`, contacts, `flood_status`, `flooded_fraction`, `osm_date`; optional |

`data/locations/` is committed so the demo works without running anything. Only the pipeline
owner regenerates it, in its own commit.

## What happens when someone searches

1. The page geocodes the text (`/api/geocode`) and asks `/api/analyze` to analyse the 10 km square
   around the first result. If an existing window already covers the place, that one is reused.
2. The map fits to the square, shows a dashed outline with a spinner, and polls
   `/api/analyze/<slug>` every 3 seconds.
3. On the server, `jobs.py` runs the pipeline in the background: newest radar pass and reference
   image, change detection, roads, then critical buildings. That uses OpenStreetMap as of the
   observation date, which adds up to ~2 minutes the first time an area is analysed.
4. When the job is `done`, the outline turns solid and the page loads `/api/flood` and
   `/api/facilities` for whatever's on screen, again on every pan and zoom.

The server never does raster maths for a page view: pages and API calls only filter files the
pipeline already wrote.

## Who works where

- **Frontend:** `flood/templates/flood/index.html` and `flood/static/flood/`. Interface rules (three
  road states, colours, controls) are in CLAUDE.md.
- **Data / pipeline:** `pipeline/` and the files in `data/locations/<slug>/`.
- **Backend:** `flood/api.py`, `flood/data.py` and `flood/jobs.py`. Conventions for routes, errors,
  tests and style are in the Claude skill at `.claude/skills/django/SKILL.md`.

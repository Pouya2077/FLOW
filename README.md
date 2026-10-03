# FLOW: Flood-Obstructed Roads

A website for first responders that shows which streets are flooded, and how much of each, using
radar satellite data. Built for StormHacks 2026 (ALEASAT track). See [CLAUDE.md](CLAUDE.md) for
the full product context, data contracts and radar caveats.

## Getting started

```
uv sync                                  # install dependencies (like npm install)
uv run python manage.py runserver        # http://127.0.0.1:8000
uv run pre-commit install                # run Ruff automatically on each commit (once per clone)
```

API docs are generated automatically at http://127.0.0.1:8000/api/docs.

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

## How the project fits together

In short: **`config/` sets up the server, `flood/` is the feature, and `data/` holds the files it
serves.**

```
config/                 project settings and top-level routing
flood/                  the app: API, page, data loading, tests
data/locations/<slug>/  precomputed flood data, one folder per demo location
manage.py               Django's command-line tool (run server, tests)
pyproject.toml          dependencies + Ruff config (Python's package.json)
uv.lock                 locked versions (Python's package-lock.json)
```

### `config/`: settings and routing

Every Django project has one of these. Think of it as the app shell, like `next.config.js` plus
the root router in a React app.

- `settings.py` is global configuration:
  - which apps are installed
  - where templates are found
  - that there's no database
  - where the flood data lives (`FLOOD_DATA_DIR`)
  - which geocoder to call (the free service that turns a typed place name into coordinates)
- `urls.py` is the top-level router with two routes:
  - `/` goes to the page view
  - `/api/...` goes to the API
- `wsgi.py` / `asgi.py` are entry points for production servers. You won't need to touch them.

The app reads configuration from these environment variables:
- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG` (`1` or `0`)
- `DJANGO_ALLOWED_HOSTS` (comma-separated)

The defaults work for local development.

### `flood/`: the app

Django splits a project into "apps", meaning feature modules. This project has just one because it
does one thing: serve flood data.

**`data.py`: reads the files.** It's the only code that touches disk.
- `locations()` scans `data/locations/*/meta.json` and returns every demo location with its name
  and bounding box.
- `segments(slug)` loads one location's road segments. It also works out each segment's bounding
  box so they can be filtered quickly.
- `streets(slug)` loads one location's per-street summaries.
- `intersects()` is a simple rectangle-overlap test, and `locations_in()` uses it to find which
  locations overlap the current map view.
- Each file is read once and then kept in memory, so **restart the server after the data changes**.

**`api.py`: the JSON endpoints, the backend the frontend `fetch()`es from.** It's built with
Django Ninja.

| Route | What it returns |
|---|---|
| `GET /api/locations` | The list of demo locations we have data for |
| `GET /api/meta?location=<slug>` | One location's details: satellite observation time, sensor, coverage area |
| `GET /api/flood?bbox=minx,miny,maxx,maxy` | Every road segment visible in that map rectangle, each tagged `flooded`, `clear` or `no_data`, as GeoJSON that MapLibre can draw directly |
| `GET /api/streets?bbox=...` | Per-street totals ("640 m of 800 m flooded") for the sidebar, most flooded first |
| `GET /api/geocode?q=<text>` | Turns typed text into a place and a bounding box using Photon, a free geocoder, with results cached for a day |

The `bbox` ("bounding box") parameter is the visible map rectangle in longitude/latitude. The
frontend sends it, and the backend returns only what falls inside it.

**`views.py`: serves the HTML page.** It has a single function, `index`, which renders the
template. Pages come from views and JSON comes from `api.py`.

**`templates/flood/index.html`: the page itself.** It has the light/dark theme toggle and a
"Coming soon" square where the MapLibre map, search box and sidebar will go. The folder is nested (`templates/flood/`) because that's a
Django convention to avoid name clashes between apps. Static files (JS and CSS) go in
`flood/static/flood/`.

**`tests.py`: smoke tests.** Each test writes a fake "Test Area" location (one flooded road) to a
temporary folder, points the app at it, then checks five things:
- the page loads
- `/api/locations` lists the fake location
- `/api/flood` returns the road when the map view covers it
- `/api/flood` returns nothing when the view is elsewhere
- a malformed `bbox` gets a 400 error

**`apps.py` / `__init__.py`**: boilerplate that registers `flood` as a Django app. You can ignore
them.

### `data/locations/`: the hand-off from the data team

The offline pipeline writes one folder per demo location (for example
`data/locations/sumas-prairie/`). Each folder holds three files:

| File | Contents |
|---|---|
| `meta.json` | `name`, `bbox`, `observed_utc`, `sensor`; optionally `footprint` (radar coverage polygon) and `synthetic: true` for test data |
| `segments.geojson` | Road segments with `name`, `highway`, `osm_way_ids`, `status`, `flooded_fraction`, `confidence`, `observed_utc` |
| `streets.json` | Per-street summaries: `name`, `total_m`, `flooded_m`, `pct`, `observed_utc`, `bbox` |

Until a location folder exists, the API returns empty results.

## What happens when someone searches

The backend never does heavy maths per request. The data team's pipeline does the hard part once
and saves files; Django just filters those files and passes them along.

1. The user types "Abbotsford" into the search box. The frontend calls
   `/api/geocode?q=Abbotsford` and gets back a bounding box.
2. The map zooms to that box (MapLibre's `fitBounds`).
3. The frontend calls `/api/flood?bbox=...` with the visible area and draws the returned roads:
   blue for water detected, grey for observed clear, hatched for not observed.
4. It calls `/api/streets?bbox=...` to fill the sidebar with the most-flooded streets.
5. It calls `/api/meta` to show the "Satellite observation: <time>" banner.

## Who works where

- **Frontend:** `flood/templates/flood/index.html` and `flood/static/flood/`, built against the
  endpoints above.
- **Data team:** produces the three files per location in `data/locations/<slug>/`.
- **Backend:** extends `flood/api.py` and `flood/data.py`. Conventions for routes, errors, tests
  and style are in the Claude skill at `.claude/skills/django/SKILL.md`.

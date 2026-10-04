# CLAUDE.md — Flood-Obstructed Roads Platform (StormHacks 2026)

Project context and working rules for every Claude session in this repo. Keep this file under ~200 lines;
put long reference material in separate files and link it.

## What we are building
A website for **first responders** that shows exactly **which streets are flooded and how much of each**.
The user types a street or area into a search box at the top; the map fits to that area and draws
**blue traces along the road stretches where radar satellite data shows water**. If a 10 km street is
flooded for 8 km, only those 8 km are blue. Clicking a trace shows street name, flooded length / total,
percentage, confidence and the satellite observation time.

- **Hazard scope:** flooding only. No wildfires, earthquakes or other disasters.
- **Output scope:** water *extent* only. Radar cannot measure depth, so never say "impassable";
  say "water detected".
- **No LLMs or agents** anywhere in the product. The team considered an agent fleet and dropped it.
  Do not propose agentic designs.

## Hackathon context
- Event: StormHacks 2026, SFU Burnaby, 24 hours, Oct 3–4 2026. Confirm the exact submission deadline
  on Devpost (the event was listed as ending around noon PT on Oct 4).
- Track: **ALEASAT** — use open Earth observation data that is free to download and readable with free
  Python libraries; the data must do real work; extra merit for disaster relief.
- Team split: a **data team** owns finding open radar data and the flood-detection algorithm. **We**
  (this repo) own the tech stack: overlay pipeline, API and website. Assume the data team
  delivers a flood mask (see contract below).

## Positioning vs Google Maps
Google surfaces floods as area alerts, a coarse forecast grid (Flood Hub urban flash floods use a
~20 km × 20 km grid) and generic "road closed" marks; observed flood maps depend on satellite passes and
disappear when imagery is older than ~72 h. None of it is exposed to developers: the Google Routes API
can only avoid tolls, highways, ferries and indoor segments, as soft preferences. Our differentiators:
street-level extent, explicit observation time, a confidence value, and (stretch) routes that avoid
flooded segments.

## Architecture
Two loosely coupled halves:
1. **Offline pipeline (Python, once per satellite pass):** flood mask GeoTIFF + OpenStreetMap roads →
   flooded road segments + per-street summaries, written as GeoJSON.
2. **Online web app (per request):** serves the precomputed GeoJSON and draws it on a map.
   No raster computation happens per request.

## Tech stack and why
| Layer | Tool | Why |
|---|---|---|
| Input | Flood mask GeoTIFF from data team | The single hand-off between the halves |
| Raster I/O | rasterio (rioxarray optional) | Free Python raster library; satisfies ALEASAT rule |
| Roads | OSMnx + OpenStreetMap | Free, global, carries street names needed for per-street totals |
| Geometry | GeoPandas, Shapely 2, pyproj | Splitting, buffering, merging; metre-accurate lengths need a projected CRS |
| Sampling | rasterstats (or rasterio.mask) | Flooded fraction of pixels under each buffered segment |
| Storage | GeoJSON files | No DB needed; MapLibre reads it natively |
| Backend | Django + Django Ninja | Team knows Django; Ninja gives typed `/api` routes and docs at `/api/docs` |
| Map | MapLibre GL JS **6.12.0** (ES module from jsDelivr; v6 ships no classic `<script>` build) | Free, WebGL, data-driven styling for blue flood traces |
| Basemap | OpenFreeMap **Positron** (light) / **Dark** (dark), URLs in `settings.BASEMAP_STYLES` | No API key or limits; muted, grey water, so blue flood lines stand out |
| Icons | lucide (`{% lucide "name" %}`, inline SVG) | Bundled in the package: no CDN or icon font; colour follows `currentColor` |
| Search | Photon (autocomplete) or Nominatim | Nominatim forbids autocomplete and allows ~1 req/s; call geocoders from the backend with an identifying User-Agent and cache results |
| Stretch | Valhalla, self-hosted | Routing that excludes flooded segments (`exclude_locations` / `exclude_polygons`) |

**Cost rule:** everything must stay free. No paid APIs, no billing accounts, no Google Maps Platform.

### Web framework (decided: Django + Django Ninja)
One Django project serves both the page and the API. No database: the `flood` app reads the pipeline's
GeoJSON from `data/locations/<slug>/` (`meta.json`, `segments.geojson`, `streets.json`).
- `config/` — settings and URLs. `flood/api.py` — Ninja routes. `flood/data.py` — file loading.
- `flood/templates/flood/index.html` — the page. `flood/static/flood/map.js` — the MapLibre map (a module:
  loads locations + meta, draws `/api/flood` for the visible area, wires search). `flood/static/flood/app.css`
  holds the theme colour variables (`--flood`, `--road-clear`, `--road-nodata`, `--unobserved`, …);
  `map.js` reads them, so map colours change in CSS only.

### Tooling conventions
- **uv** for environments and dependencies (`pyproject.toml` + `uv.lock`; teammates run `uv sync`).
- **Ruff** for linting and formatting; **pre-commit** with Ruff hooks.
- Dev server: `uv run python manage.py runserver`; tests: `uv run python manage.py test`.
- Avoid heavy starters (cookiecutter-django, FastAPI's full-stack template): too much for 24 hours.

## Data contracts
**Flood mask (input):** GeoTIFF/COG, one band. `uint8` 0 = dry, 1 = water, 255 = no data
(or `float32` probability 0–1). CRS stated, ~10 m pixels. Metadata: acquisition time (UTC), sensor,
orbit direction, pre-event reference date.

**Road segment (output):** GeoJSON LineString (lon/lat) with properties `name`, `highway`,
`osm_way_ids`, `status` (`flooded` | `clear` | `no_data`), `flooded_fraction`, `confidence`,
`observed_utc`. One feature per run of same-status road, not per 20 m piece. `flooded_fraction` =
water share of observed corridor pixels; `confidence` = share agreeing with `status` (fraction if
flooded, 1 − fraction if clear); both `null` for `no_data`. Missing OSM tags are `null`; never write NaN.

**Street summary (output):** `name`, `total_m`, `flooded_m`, `no_data_m`, `pct`, `observed_utc`,
`bbox`. Named streets only, sorted by `flooded_m`. `no_data_m` exists so an unobserved street never
reads as "0% flooded". Divided highways count both carriageways (OSM maps each direction).

**Location meta (output):** `name`, `bbox`, `observed_utc`, `timezone` (IANA, from `regions.py`; the UI
shows the observation in local time), `sensor`, `synthetic` (not shown in the UI — team decision,
Oct 4), `footprint` (GeoJSON Polygon of observed pixels: the outlined window; outside it is hatched).

**API** (also `GET /api/locations` — demo locations with data):
- `GET /api/geocode?q=` — proxy to Photon/Nominatim, cached
- `GET /api/flood?bbox=minx,miny,maxx,maxy` — road segments in view
- `GET /api/streets?bbox=` — per-street summaries
- `GET /api/meta` — pass time, coverage

### Pipeline (`pipeline/`, plain Python, run as modules from the repo root)
- `regions.py` — `REGIONS` (slug, name, lon/lat bbox, timezone) and `get_region(slug)`. The only place a city is
  hardcoded; may later move to a data file or DB, so scripts must only go through `get_region`.
- `fetch_data.py` — downloads one raw Sentinel-1 VV scene from the Copernicus Data Space (free account;
  `CDSE_USERNAME`/`CDSE_PASSWORD` in `.env`) into `data/radar/` (gitignored). Raw backscatter, not a
  mask; not yet wired to the steps below. Bbox/dates are hardcoded to Abbotsford, Nov 2021.
  Run: `uv run python -m pipeline.fetch_data`.
- `map_mask.py <slug>` — rasterizes `test_floods/<slug>.geojson` (hand-drawn test water) into
  `data/masks/<slug>_synthetic.tif` (gitignored), with an unobserved strip and `synthetic=true` tag.
- `build_location.py <slug> [--mask path] [--out dir]` — the overlay below; writes
  `data/locations/<slug>/`. Defaults to the synthetic mask; the data team's mask is passed with
  `--mask`. Use `--out` for experiments so the committed data isn't overwritten.
- `map_mask.py <slug> --speckle 0.3` writes a noisy `<slug>_synthetic_speckle.tif` for testing step 5.
- Run e.g. `uv run python -m pipeline.build_location sumas-prairie` (~40 s). OSMnx caches to `cache/`.
- **Output in `data/locations/` is committed** so the UI and demo work without running the pipeline.
  Only the pipeline owner regenerates it, in its own commit; on a conflict, rerun rather than merge.
  Restart runserver after regenerating (files are cached per process).

## Road–flood overlay algorithm
1. Fetch drivable roads for the area with OSMnx; reproject to the **mask's CRS**. Masks use the UTM
   zone of the region's centre (EPSG:32610 for the Lower Mainland), so any region gets metre units.
2. Remove permanent water bodies and OSM `bridge=yes` segments (they always read as flooded).
   Fetch with `simplify=False`, then `ox.simplify_graph(edge_attrs_differ=["bridge", "name"])`:
   OSMnx's default merge makes a whole stretch inherit `bridge=yes` from a short bridge (dropped 55 km,
   not 1.8 km, at Sumas), and merging across a name change gave stretches a list of names in random
   order, so street labels changed between runs. Output must be identical on rerun.
   Permanent water = OSM water polygons; their pixels are ignored when scoring.
3. Split each road into **~20 m** segments (equal pieces per stretch, keeping `road_id` + `seq`
   order); buffer **~5 m** each side (~10 m corridor).
4. Score each segment (`score_segments`): count mask pixels touching its corridor (rasterstats,
   `all_touched`), with permanent-water pixels relabelled so they count as neither wet nor dry.
   More unobserved than observed pixels → `no_data` (never guess clear).
5. Flooded if fraction **≥ 0.5**; `remove_speckle` resets flooded runs shorter than **~40 m** to
   clear, but only when clear road lies on both sides within the same stretch (runs at a stretch end
   or beside `no_data` are kept). It is a backstop, not a speckle filter: on a mask with 30% random
   pixel noise it removed ~30 km of false flooding but ~40 km remained. **The data team must
   despeckle the mask itself** (speckle filter on the radar image, then e.g. a morphological opening /
   minimum patch size on the water mask). Test with `map_mask --speckle 0.3` and `build_location --out`.
6. Merge consecutive same-status segments of each road into one line (`merge_runs`), total by street
   name, reproject to lon/lat, write the three files.
All thresholds (`SEGMENT_M`, `BUFFER_M`, `FLOODED_AT`) are tunable constants — tune on the demo event.
Too wide a corridor picks up water in fields beside raised roads and marks dry roads flooded.

## Interface requirements
**Follow Google Maps conventions** — it's what users already know. Deviate only where noted.
- **Layout:** full-bleed map filling the window; controls float over it. No page chrome, headers or
  footers.
- **Map opens fitted to the observation window**, then zooms and pans freely like Google Maps (scroll,
  pinch, double-click, drag, arrow keys, +/− buttons bottom-right). No rotating or tilting. The window
  is just where flood data exists; its outline is fixed to the observed area's coordinates.
- **Floating controls are translucent at rest** (frosted, map shows through) and turn **solid with a
  Maps-style shadow** on hover, focus or while typing. Applies to every overlay control (`.overlay`).
- **Search:** pill (48 px, ~392 px) in the **top-left**, magnifying-glass button on its right.
  Submitting fits the map to the first `/api/geocode` result (max zoom 16); the query is kept in
  `?q=`. Errors / "no places found" show in a solid message under the box.
- **Recent searches:** focusing the empty search box opens a "Recent" panel (WAI-ARIA combobox +
  listbox; ↑/↓, Enter, Escape). Each entry: clock icon, address, observation time in the location's
  time zone (`views.observed_local`). Choosing one flies back to that observation window
  (`?location=`); the entry for the window on the map has the `--accent` border. **Hardcoded for now**
  (`views.RECENT_SEARCHES = ["sumas-prairie"]`); real recent searches must come from the user's
  history, not be predetermined.
- **Theme toggle:** round 48 px button in the **top-right**, moon in light mode, sun in dark mode.
  Order: `?theme=` URL parameter (also saved to the `theme` cookie, 1 year, so the server renders the
  right theme), then the cookie, then light. Toggling reloads the page.
- **Colours come from the basemap:** every UI colour is a token in `flood/static/flood/app.css` derived
  from Positron (light) / Dark (dark) — Positron's greys for text and borders, its slate water-label
  blue `#495E91` as the UI accent. Don't introduce colours that aren't in the map's palette, except
  `--flood`.
- **Type:** Roboto (the Google Maps face) with a system-font fallback. Icons: lucide, 20 px, stroke in
  `currentColor`.
- Quality floor: works at phone width, visible keyboard focus, honours `prefers-reduced-motion`.
- **Three road states, never two:** blue = water observed (`--flood`, widest, drawn on top), solid
  grey = observed clear, dashed grey = not observed / no data. A road outside the satellite footprint
  must never look safe: everything outside `meta.footprint` is covered by a diagonal hatch.
- **Window outline:** the footprint is outlined 3 px in `--accent`, matching the selected Recent entry.
- **One window at a time:** only the current location's window, hatch hole and road traces are drawn.
  A new search (or choosing a Recent entry) moves the window there; the previous one is not rendered.
- **Approach dots:** basemap roads crossing the window edge get a dotted stub outside it (~70 screen
  px, fading out in 5 steps), computed in `map.js` from the basemap's `transportation` tiles.
- **Road labels by importance:** the basemap's road-name layers are hidden and replaced by tiers —
  motorway/trunk/primary always, secondary/tertiary from zoom 12.5, minor/service from 14.5. Route
  shields stay.
- **Flood layers sit above the basemap's roads and below its labels** (inserted before the first label
  layer after the last non-label layer; Dark has a label layer under its roads).
- Persistent data-age banner: "Satellite observation: <date time UTC> (N hours ago)".
- Click popup with street stats; sidebar listing affected streets sorted by flooded length.
- Optional faint flood-extent raster under the traces.

## Radar (SAR) pitfalls — always account for these
Radar is chosen because it sees through cloud and at night. But it maps how much signal bounces back,
and water is dark because it reflects the signal away like a mirror.
- **Dry asphalt looks like water** (smooth → dark). Single-image thresholding flags dry roads as
  flooded. Mitigation: change detection against a pre-flood reference image. Most important pitfall.
- **Pixel size ≈ road width** (~10 m Sentinel-1 GRD vs ~7–10 m roads): mixed pixels. Use buffered
  corridors, fractional scores and confidence; never claim lane-level detail.
- **Urban double bounce:** water + building walls bounce back → flooded streets look bright and get
  missed. Prefer open terrain; mark built-up areas low confidence.
- **Shadow and layover** from tall buildings and slopes. Mask steep slopes with a DEM.
- **Speckle:** filter in preprocessing; minimum run length.
- **Wind / vegetation:** rough water looks dry; canopy hides water. Use VV polarisation; flag tree-lined
  roads.
- **Bridges and permanent water** read as flooded. Mask them.
- **Revisit timing:** Sentinel-1 ~6–12 days; passes may miss the peak. Always show observation time.
  Pitch "near real time: updated each satellite pass", never "live".
- **Preprocessing and size:** calibration, noise removal, terrain correction; scenes are large. Clip to
  the area of interest early.
- **Geolocation offset** of a few metres between OSM and imagery; the buffer absorbs it.

## Demo plan
- Demo event: **November 2021 Fraser Valley floods (Sumas Prairie, Abbotsford)** — local, severe,
  flat open farmland where radar works best.
- Build against a **synthetic mask** first (draw a polygon over Sumas Prairie, rasterize it) so the
  web app does not wait on the data team; swapping in the real mask changes only the input path.
- Fallbacks: cache geocoder results, serve precomputed GeoJSON as static files, record a backup demo
  video.
- **Beyond the demo:** any city/region, refreshed each pass. Planned shape: a list of watched regions
  and a scheduled job (STAC search for new Sentinel-1 passes → detection → overlay → write that
  region's folder). Never compute on a user's request; unwatched areas show as no data.
  `flood/data.py` caches files per process and will need reloading once data updates live.

## Related free data sources (context, optional)
- **DriveBC Open511 API** (`https://api.open511.gov.bc.ca/events`): public, no auth; BC road events
  including flooding, landslides and washouts with geometry. Mainly provincial roads.
- Production vision: the team's own satellites deliver fresher radar than Google gets.

## Working rules for Claude
- Keep everything free and open; favour the simplest thing that works in a 24-hour build.
- Use Django + Django Ninja (not FastAPI), uv and Ruff.
- Respect the three-state rule and observation timestamps in any UI work.
- Be honest about radar limits in code comments, UI copy and pitch material.
- Don't reintroduce agents, Google APIs or paid services.
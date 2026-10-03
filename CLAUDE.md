# CLAUDE.md — Flood-Obstructed Roads Platform (StormHacks 2026)

Project context and working rules for every Claude session in this repo. Keep this file under ~200 lines;
put long reference material in separate files and link it.

## What we are building
A website for **first responders** that shows exactly **which streets are flooded and how much of each**.
The user types a street or area into a search box at the top; the map fits to that area and draws
**red traces along the road stretches where radar satellite data shows water**. If a 10 km street is
flooded for 8 km, only those 8 km are red. Clicking a trace shows street name, flooded length / total,
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
| Map | MapLibre GL JS | Free, WebGL, data-driven styling for red traces |
| Basemap | OpenFreeMap (`https://tiles.openfreemap.org/styles/liberty`) | No API key, no registration, no request limits |
| Search | Photon (autocomplete) or Nominatim | Nominatim forbids autocomplete and allows ~1 req/s; call geocoders from the backend with an identifying User-Agent and cache results |
| Stretch | Valhalla, self-hosted | Routing that excludes flooded segments (`exclude_locations` / `exclude_polygons`) |

**Cost rule:** everything must stay free. No paid APIs, no billing accounts, no Google Maps Platform.

### Web framework (decided: Django + Django Ninja)
One Django project serves both the page and the API. No database: the `flood` app reads the pipeline's
GeoJSON from `data/locations/<slug>/` (`meta.json`, `segments.geojson`, `streets.json`).
- `config/` — settings and URLs. `flood/api.py` — Ninja routes. `flood/data.py` — file loading.
- `flood/templates/flood/index.html` — the page; the map is MapLibre JavaScript.

### Tooling conventions
- **uv** for environments and dependencies (`pyproject.toml` + `uv.lock`; teammates run `uv sync`).
- **Ruff** for linting and formatting; **pre-commit** with Ruff hooks.
- Dev server: `uv run python manage.py runserver`; tests: `uv run python manage.py test`.
- Avoid heavy starters (cookiecutter-django, FastAPI's full-stack template): too much for 24 hours.

## Data contracts
**Flood mask (input):** GeoTIFF/COG, one band. `uint8` 0 = dry, 1 = water, 255 = no data
(or `float32` probability 0–1). CRS stated, ~10 m pixels. Metadata: acquisition time (UTC), sensor,
orbit direction, pre-event reference date.

**Road segment (output):** GeoJSON LineString with properties `name`, `highway`, `osm_way_ids`,
`status` (`flooded` | `clear` | `no_data`), `flooded_fraction`, `confidence`, `observed_utc`.

**Street summary (output):** `name`, `total_m`, `flooded_m`, `pct`, `observed_utc`, `bbox`.

**API** (also `GET /api/locations` — demo locations with data):
- `GET /api/geocode?q=` — proxy to Photon/Nominatim, cached
- `GET /api/flood?bbox=minx,miny,maxx,maxy` — road segments in view
- `GET /api/streets?bbox=` — per-street summaries
- `GET /api/meta` — pass time, coverage

## Road–flood overlay algorithm
1. Fetch drivable roads for the area with OSMnx; reproject to **EPSG:32610** (UTM 10N, Lower Mainland).
2. Remove permanent water bodies and OSM `bridge=yes` segments (they always read as flooded).
3. Split each road into **~20 m** segments; buffer **~5 m** each side (~10 m corridor).
4. Score each segment by fraction of water pixels; no-data pixels → `status = no_data`.
5. Flooded if fraction **≥ 0.5**; drop isolated flooded runs shorter than **~40 m** (speckle);
   keep the fraction as confidence.
6. Merge adjacent flooded segments into continuous lines; group by street name for totals.
All thresholds are tunable constants — tune on the demo event.

## Interface requirements
- Search box at top; on select, fit map to the result's bounding box and load that area's layers.
- **Three road states, never two:** red = water observed, grey = observed clear,
  hatched = not observed / no data. A road outside the satellite footprint must never look safe.
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
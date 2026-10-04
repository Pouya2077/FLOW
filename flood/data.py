"""Read the pipeline's output from FLOOD_DATA_DIR (committed demo) and SEARCH_DATA_DIR (searches).

Each location is a folder:
    <dir>/<slug>/meta.json         name, bbox, observed_utc, sensor, footprint
    <dir>/<slug>/segments.geojson  road segments (see CLAUDE.md "Road segment")
    <dir>/<slug>/streets.json      per-street summaries (see CLAUDE.md "Street summary")
    <dir>/<slug>/facilities.geojson critical buildings (see CLAUDE.md "Critical building"); optional

Files are read once per process; reload() picks up a finished search. Restart runserver after
regenerating the committed demo data.
"""

import json
from functools import cache
from pathlib import Path

from django.conf import settings

BBox = tuple[float, float, float, float]  # minx, miny, maxx, maxy (lon/lat)


# slug -> folder. Searches first, so the committed demo wins if both have the same slug.
@cache
def _folders() -> dict[str, Path]:
    out = {}
    for root in (settings.SEARCH_DATA_DIR, settings.FLOOD_DATA_DIR):
        for meta_path in root.glob("*/meta.json"):
            out[meta_path.parent.name] = meta_path.parent
    return out


# Slugs of searched areas, most recently analysed first (by when their files were written)
def searched() -> list[str]:
    folders = _folders()
    slugs = [s for s, folder in folders.items() if folder.parent == settings.SEARCH_DATA_DIR]
    return sorted(slugs, key=lambda s: (folders[s] / "meta.json").stat().st_mtime, reverse=True)


@cache
def locations() -> dict[str, dict]:
    out = {}
    for slug, folder in sorted(_folders().items()):
        meta = json.loads((folder / "meta.json").read_text())
        meta["slug"] = slug
        out[slug] = meta
    return out


@cache
def segments(slug: str) -> list[dict]:
    path = _folders()[slug] / "segments.geojson"
    features = json.loads(path.read_text())["features"]
    for f in features:
        f["bbox"] = _geometry_bbox(f["geometry"])
    return features


@cache
def streets(slug: str) -> list[dict]:
    path = _folders()[slug] / "streets.json"
    return json.loads(path.read_text())


@cache
def facilities(slug: str) -> list[dict]:
    path = _folders()[slug] / "facilities.geojson"
    if not path.exists():  # built before critical buildings existed, or their query failed
        return []
    features = json.loads(path.read_text())["features"]
    for f in features:
        f["bbox"] = _geometry_bbox(f["geometry"])
    return features


# Forget cached files, e.g. after a search wrote a new folder
def reload() -> None:
    for cached in (_folders, locations, segments, streets, facilities):
        cached.cache_clear()


def intersects(a: BBox, b: BBox) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def locations_in(bbox: BBox) -> list[str]:
    return [slug for slug, meta in locations().items() if intersects(bbox, meta["bbox"])]


def _geometry_bbox(geometry: dict) -> BBox:
    coords = geometry["coordinates"]
    if geometry["type"] == "Point":
        coords = [coords]
    elif geometry["type"] == "MultiLineString":
        coords = [pt for line in coords for pt in line]
    xs = [pt[0] for pt in coords]
    ys = [pt[1] for pt in coords]
    return (min(xs), min(ys), max(xs), max(ys))

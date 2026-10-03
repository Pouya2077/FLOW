"""Read the offline pipeline's output from FLOOD_DATA_DIR.

Each demo location is a folder:
    data/locations/<slug>/meta.json         name, bbox, observed_utc, sensor, footprint
    data/locations/<slug>/segments.geojson  road segments (see CLAUDE.md "Road segment")
    data/locations/<slug>/streets.json      per-street summaries (see CLAUDE.md "Street summary")

Files are read once per process; restart runserver after regenerating them.
"""

import json
from functools import cache

from django.conf import settings

BBox = tuple[float, float, float, float]  # minx, miny, maxx, maxy (lon/lat)


@cache
def locations() -> dict[str, dict]:
    out = {}
    for meta_path in sorted(settings.FLOOD_DATA_DIR.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        meta["slug"] = meta_path.parent.name
        out[meta["slug"]] = meta
    return out


@cache
def segments(slug: str) -> list[dict]:
    path = settings.FLOOD_DATA_DIR / slug / "segments.geojson"
    features = json.loads(path.read_text())["features"]
    for f in features:
        f["bbox"] = _geometry_bbox(f["geometry"])
    return features


@cache
def streets(slug: str) -> list[dict]:
    path = settings.FLOOD_DATA_DIR / slug / "streets.json"
    return json.loads(path.read_text())


def intersects(a: BBox, b: BBox) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def locations_in(bbox: BBox) -> list[str]:
    return [slug for slug, meta in locations().items() if intersects(bbox, meta["bbox"])]


def _geometry_bbox(geometry: dict) -> BBox:
    coords = geometry["coordinates"]
    if geometry["type"] == "MultiLineString":
        coords = [pt for line in coords for pt in line]
    xs = [pt[0] for pt in coords]
    ys = [pt[1] for pt in coords]
    return (min(xs), min(ys), max(xs), max(ys))

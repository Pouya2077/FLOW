import json
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import cache
from ninja import NinjaAPI
from ninja.errors import HttpError

from . import data

api = NinjaAPI(title="FLOW API", version="0.1.0")


def _parse_bbox(bbox: str) -> data.BBox:
    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HttpError(400, "bbox must be minx,miny,maxx,maxy") from None
    return (minx, miny, maxx, maxy)


@api.get("/locations")
def list_locations(request):
    """Demo locations the pipeline has produced data for."""
    return [
        {"slug": m["slug"], "name": m["name"], "bbox": m["bbox"]} for m in data.locations().values()
    ]


@api.get("/meta")
def meta(request, location: str):
    """Observation time, sensor and coverage footprint for one location."""
    try:
        return data.locations()[location]
    except KeyError:
        raise HttpError(404, f"unknown location {location!r}") from None


@api.get("/flood")
def flood(request, bbox: str):
    """Road segments (flooded, clear and no_data) intersecting the view, as a FeatureCollection."""
    view = _parse_bbox(bbox)
    features = [
        f
        for slug in data.locations_in(view)
        for f in data.segments(slug)
        if data.intersects(view, f["bbox"])
    ]
    return {"type": "FeatureCollection", "features": features}


@api.get("/streets")
def streets(request, bbox: str):
    """Per-street summaries in view, most flooded first."""
    view = _parse_bbox(bbox)
    rows = [
        s
        for slug in data.locations_in(view)
        for s in data.streets(slug)
        if data.intersects(view, s["bbox"])
    ]
    return sorted(rows, key=lambda s: s["flooded_m"], reverse=True)


@api.get("/geocode")
def geocode(request, q: str):
    """Proxy to Photon, cached for a day. Returns [{name, bbox, center}]."""
    q = q.strip()
    if len(q) < 3:
        return []
    key = "geocode:" + q.lower()
    if (hit := cache.get(key)) is not None:
        return hit

    url = settings.GEOCODER_URL + "?" + urllib.parse.urlencode({"q": q, "limit": 5})
    req = urllib.request.Request(url, headers={"User-Agent": settings.GEOCODER_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.load(resp)
    except OSError:
        raise HttpError(502, "geocoder unavailable") from None

    results = []
    for f in payload.get("features", []):
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        # Photon extent is [minx, maxy, maxx, miny]; points have none, so pad ~500 m.
        if ext := p.get("extent"):
            bbox = [ext[0], ext[3], ext[2], ext[1]]
        else:
            bbox = [lon - 0.007, lat - 0.0045, lon + 0.007, lat + 0.0045]
        label = ", ".join(
            v for v in (p.get("name"), p.get("city"), p.get("state"), p.get("country")) if v
        )
        results.append({"name": label, "bbox": bbox, "center": [lon, lat]})

    cache.set(key, results, 60 * 60 * 24)
    return results

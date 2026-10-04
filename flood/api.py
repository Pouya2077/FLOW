import json
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import cache
from ninja import NinjaAPI
from ninja.errors import HttpError

from pipeline.regions import region_around

from . import data, jobs

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


# Locations intersecting the view; only `location` if given (the map shows one window at a time)
def _slugs_in(view: data.BBox, location: str | None) -> list[str]:
    slugs = data.locations_in(view)
    return [s for s in slugs if s == location] if location else slugs


@api.get("/flood")
def flood(request, bbox: str, location: str | None = None):
    """Road segments (flooded, clear and no_data) intersecting the view, as a FeatureCollection.
    With location=, only that location's segments."""
    view = _parse_bbox(bbox)
    features = [
        f
        for slug in _slugs_in(view, location)
        for f in data.segments(slug)
        if data.intersects(view, f["bbox"])
    ]
    return {"type": "FeatureCollection", "features": features}


@api.get("/streets")
def streets(request, bbox: str, location: str | None = None):
    """Per-street summaries in view, most flooded first. With location=, only that location's."""
    view = _parse_bbox(bbox)
    rows = [
        s
        for slug in _slugs_in(view, location)
        for s in data.streets(slug)
        if data.intersects(view, s["bbox"])
    ]
    return sorted(rows, key=lambda s: s["flooded_m"], reverse=True)


@api.post("/analyze")
def analyze(request, name: str, lon: float, lat: float):
    """Start analysing the newest satellite pass around a searched place (takes 1-2 minutes).
    Returns {slug, name, bbox, status, message}; poll GET /api/analyze/{slug} until status is
    "done" (then the area is in /api/locations) or "failed" (message says why)."""
    if not (-180 <= lon <= 180 and -85 <= lat <= 85):
        raise HttpError(400, "lon must be -180..180 and lat -85..85")
    name = name.strip()[:120] or "Searched area"
    return jobs.start(region_around(name, lon, lat))


@api.get("/analyze/{slug}")
def analysis(request, slug: str):
    """Status of an analysis: queued, running, done or failed."""
    job = jobs.status(slug)
    if job is None:
        raise HttpError(404, f"no analysis for {slug!r}")
    return job


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

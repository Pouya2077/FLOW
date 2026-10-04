"""Background analysis of searched areas.

A run streams two radar images and overlays the roads: 1-2 minutes, far too long for a request.
So a search starts a job here and the page polls its status. One run at a time: each is heavy on
memory and on the free Copernicus and OpenStreetMap services.

Job status lives in memory, so it's lost on restart; finished results are files in
SEARCH_DATA_DIR and survive it.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from django.conf import settings

from pipeline.detect_flood import NoImagery, fetch_sentinel_radar
from pipeline.regions import Region

from . import data

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")
_lock = threading.Lock()
_jobs: dict[str, dict] = {}  # slug -> {slug, name, bbox, status, message}

FAILED_MESSAGE = "The satellite analysis failed. Try again in a few minutes."

# The free services a run depends on, by a word in their host name. They are sometimes slow or
# down for a while, which is worth telling the user apart from a bug.
SERVICES = {
    "copernicus": "The Copernicus satellite data service",
    "overpass": "OpenStreetMap's road server",
}


# What to tell the user when a service didn't respond or answered with an error
def service_message(error: requests.RequestException) -> str:
    url = error.request.url if error.request else None
    host = (urlparse(url).hostname if url else None) or ""
    service = next((name for key, name in SERVICES.items() if key in host), "A data service")
    return (
        f"{service} isn't responding, so this area couldn't be analysed. "
        "Try again in a few minutes."
    )


# Start analysing a region unless it's already queued, running or done. Returns the job status.
def start(region: Region) -> dict:
    with _lock:
        job = _jobs.get(region.slug)
        if job and job["status"] in ("queued", "running", "done"):  # only failed ones re-run
            return dict(job)
        if region.slug in data.locations() and not job:
            return _finished(region.slug)
        job = {
            "slug": region.slug,
            "name": region.name,
            "bbox": list(region.bbox),
            "status": "queued",
            "message": "",
        }
        _jobs[region.slug] = job
    _submit(_run, region, job)
    return dict(job)


# An analysed (or in-progress) area containing the point, so searching anywhere inside it reuses
# it, whatever the geocoder called the place. The one whose centre is nearest if several do.
def covering(lon: float, lat: float) -> dict | None:
    with _lock:
        candidates = [dict(j) for j in _jobs.values() if j["status"] != "failed"]
    known = {c["slug"] for c in candidates}
    candidates += [_finished(slug) for slug in data.locations() if slug not in known]

    def inside(bbox) -> bool:
        return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]

    def distance(bbox) -> float:  # to the centre, in degrees; fine for ranking nearby boxes
        return ((bbox[0] + bbox[2]) / 2 - lon) ** 2 + ((bbox[1] + bbox[3]) / 2 - lat) ** 2

    hits = [c for c in candidates if inside(c["bbox"])]
    return min(hits, key=lambda c: distance(c["bbox"])) if hits else None


# Status of a job, or of an area analysed before this process started; None if unknown
def status(slug: str) -> dict | None:
    with _lock:
        if job := _jobs.get(slug):
            return dict(job)
    if slug in data.locations():
        return _finished(slug)
    return None


def _finished(slug: str) -> dict:
    meta = data.locations()[slug]
    return {
        "slug": slug,
        "name": meta["name"],
        "bbox": meta["bbox"],
        "status": "done",
        "message": "",
    }


# Separate so tests can run jobs inline
def _submit(fn, *args) -> None:
    _executor.submit(fn, *args)


def _run(region: Region, job: dict) -> None:
    _update(job, status="running")
    try:
        fetch_sentinel_radar(region, date_range=None, out_dir=settings.SEARCH_DATA_DIR)
    except NoImagery as e:  # nothing to compare: tell the user why
        _update(job, status="failed", message=str(e))
        return
    except requests.RequestException as e:  # a service was down: say which
        log.warning("analysis of %s failed: %s", region.slug, e)
        _update(job, status="failed", message=service_message(e))
        return
    except Exception:
        log.exception("analysis of %s failed", region.slug)
        _update(job, status="failed", message=FAILED_MESSAGE)
        return
    data.reload()
    _update(job, status="done")


def _update(job: dict, **changes) -> None:
    with _lock:
        job.update(changes)

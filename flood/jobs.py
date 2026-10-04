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

from django.conf import settings

from pipeline.detect_flood import NoImagery, fetch_sentinel_radar
from pipeline.regions import Region

from . import data

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")
_lock = threading.Lock()
_jobs: dict[str, dict] = {}  # slug -> {slug, name, bbox, status, message}

FAILED_MESSAGE = "The satellite analysis failed. Try again in a few minutes."


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
    except Exception:
        log.exception("analysis of %s failed", region.slug)
        _update(job, status="failed", message=FAILED_MESSAGE)
        return
    data.reload()
    _update(job, status="done")


def _update(job: dict, **changes) -> None:
    with _lock:
        job.update(changes)

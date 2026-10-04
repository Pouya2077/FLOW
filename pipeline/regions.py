"""Lists regions that the pipeline is able to process."""

import math
import re
from dataclasses import dataclass
from functools import cache

from timezonefinder import TimezoneFinder

SEARCH_BOX_KM = 10  # side of the square analysed around a searched place; keeps a run ~1-2 min


@dataclass(frozen=True)
class Region:
    slug: str
    name: str  # name appearing in the UI
    bbox: tuple[float, float, float, float]  # W, S, E, N in lon/lat
    timezone: str  # IANA name; the UI shows the observation time in the region's local time


REGIONS = {
    r.slug: r
    for r in [
        Region(
            slug="sumas-prairie",
            name="Sumas Prairie, Abbotsford",
            bbox=(-122.28, 49.00, -122.08, 49.10),
            timezone="America/Vancouver",
        )
    ]
}


def get_region(slug: str) -> Region:
    try:
        return REGIONS[slug]
    except KeyError:
        raise SystemExit(f"unknown region {slug!r}; choose from {','.join(REGIONS)}") from None


# Create a Region for a place that was searched by a user: a SEARCH_BOX_KM square centred on it
def region_around(name: str, lon: float, lat: float, box_km: float = SEARCH_BOX_KM) -> Region:
    half_lat = box_km / 2 / 110.574  # km per degree of latitude
    half_lon = box_km / 2 / (111.320 * math.cos(math.radians(lat)))
    corners = (lon - half_lon, lat - half_lat, lon + half_lon, lat + half_lat)
    bbox = tuple(round(v, 5) for v in corners)
    timezone = _timezones().timezone_at(lng=lon, lat=lat) or "UTC"  # UTC out at sea
    return Region(slug=slugify(name), name=name, bbox=bbox, timezone=timezone)


# "Chilliwack, British Columbia" -> "chilliwack-british-columbia": the folder name and URL value
def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "place"


@cache
def _timezones() -> TimezoneFinder:
    return TimezoneFinder()  # loads its data once

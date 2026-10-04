from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.shortcuts import render

from . import data
from .utils import get_10km_range
from pipeline.fetch_data import fetch_sentinel_radar

THEMES = ("light", "dark")


def index(request):
    requested = request.GET.get("theme")
    if requested in THEMES:
        theme = requested
    elif request.COOKIES.get(settings.THEME_COOKIE) in THEMES:
        theme = request.COOKIES[settings.THEME_COOKIE]
    else:
        theme = "light"
    
    search_query = request.GET.get("q")
    bbox = None
    radar_file = None

    if search_query:
        result = get_10km_range(search_query)
        if isinstance(result, list):
            bbox = result
            # should change bbox in fetch_sentinel_data
            radar_file = fetch_sentinel_radar(bbox=bbox)

    locations = [
        {"slug": slug, "label": location_label(meta)} for slug, meta in data.locations().items()
    ]
    selected = next(
        (loc for loc in locations if loc["slug"] == request.GET.get("location")),
        locations[0] if locations else None,
    )

    response = render(
        request,
        "flood/index.html",
        {
            "theme": theme,
            "other_theme": "dark" if theme == "light" else "light",
            "basemap_style": settings.BASEMAP_STYLES[theme],
            "locations": locations,
            "selected": selected,
            "bbox": bbox,
            "radar_file": radar_file,
        },
    )
    if requested in THEMES:
        response.set_cookie(
            settings.THEME_COOKIE,
            requested,
            max_age=settings.THEME_COOKIE_MAX_AGE,
            samesite="Lax",
        )
    return response


def location_label(meta: dict) -> str:
    """'<name> | <observation time in the location's time zone>', e.g.
    'Sumas Prairie, Abbotsford | Nov 16, 2021, 6:25 AM PST'."""
    if not meta.get("observed_utc"):
        return meta["name"]
    observed = datetime.fromisoformat(meta["observed_utc"])
    local = observed.astimezone(ZoneInfo(meta.get("timezone") or "UTC"))
    hour = local.hour % 12 or 12
    when = f"{local:%b} {local.day}, {local.year}, {hour}:{local:%M %p %Z}"
    return f"{meta['name']} | {when}"

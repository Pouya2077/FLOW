from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.shortcuts import render
from django.utils.http import urlencode

from pipeline.detect_flood import fetch_sentinel_radar

from . import data
from .utils import get_10km_range

THEMES = ("light", "dark")

# Hardcoded for now: the Abbotsford flood (the local-development data set) is always offered as a
# "recent" search. In the future, recent searches must come from what the user actually searched,
# not be predetermined here.
RECENT_SEARCHES = ["sumas-prairie"]


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
    other_theme = "dark" if theme == "light" else "light"

    locations = data.locations()
    location = request.GET.get("location")
    if location not in locations:
        location = next(iter(locations), None)  # the window the map opens on
    query = request.GET.get("q", "").strip()

    recent = [
        {"slug": slug, "place": locations[slug]["name"], "when": observed_local(locations[slug])}
        for slug in RECENT_SEARCHES
        if slug in locations
    ]
    # Switching theme reloads the page, so carry the current view over.
    theme_params = {"theme": other_theme, "location": location, "q": query}
    theme_href = "?" + urlencode({k: v for k, v in theme_params.items() if v})

    response = render(
        request,
        "flood/index.html",
        {
            "theme": theme,
            "other_theme": other_theme,
            "theme_href": theme_href,
            "basemap_style": settings.BASEMAP_STYLES[theme],
            "location": location,
            "query": query,
            "recent": recent,
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


def observed_local(meta: dict) -> str:
    """Observation time in the location's time zone, e.g. 'Nov 16, 2021, 6:25 AM PST'."""
    if not meta.get("observed_utc"):
        return ""
    observed = datetime.fromisoformat(meta["observed_utc"])
    local = observed.astimezone(ZoneInfo(meta.get("timezone") or "UTC"))
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {local.year}, {hour}:{local:%M %p %Z}"

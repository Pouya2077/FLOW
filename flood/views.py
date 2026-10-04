from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.shortcuts import render
from django.utils.http import urlencode

from . import data

THEMES = ("light", "dark")

# The Recent panel lists areas searched on this server, newest first, then the demo flood (always
# offered). There are no user accounts, so everyone using this server sees the same searches.
RECENT_MAX = 5
DEMO_LOCATIONS = ["sumas-prairie"]


def index(request):
    requested = request.GET.get("theme")
    if requested in THEMES:
        theme = requested
    elif request.COOKIES.get(settings.THEME_COOKIE) in THEMES:
        theme = request.COOKIES[settings.THEME_COOKIE]
    else:
        theme = "light"

    # A ?q= search is handled in the browser: map.js geocodes it and starts a background analysis
    # (/api/analyze), so the page never waits on the pipeline.
    other_theme = "dark" if theme == "light" else "light"

    locations = data.locations()
    location = request.GET.get("location")
    if location not in locations:
        location = next(iter(locations), None)  # the window the map opens on
    query = request.GET.get("q", "").strip()

    searched = data.searched()[:RECENT_MAX]
    demo = [s for s in DEMO_LOCATIONS if s in locations and s not in searched]
    recent = [
        {"slug": slug, "place": locations[slug]["name"], "when": observed_local(locations[slug])}
        for slug in searched + demo
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

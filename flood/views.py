from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.shortcuts import render
from django.utils.http import urlencode

from pipeline.facility_kinds import KINDS

from . import data

THEMES = ("light", "dark")

# The Recent panel lists areas searched on this server, newest first, then the demo flood, which
# counts as the oldest: at most RECENT_MAX in all, so the demo drops off after that many searches.
# There are no user accounts, so everyone using this server sees the same searches.
RECENT_MAX = 5
DEMO_LOCATIONS = ["sumas-prairie"]


def resolve_theme(request) -> str:
    """The page's theme: ?theme=, then the theme cookie, then light."""
    requested = request.GET.get("theme")
    if requested in THEMES:
        return requested
    if request.COOKIES.get(settings.THEME_COOKIE) in THEMES:
        return request.COOKIES[settings.THEME_COOKIE]
    return "light"


def theme_toggle_for(theme: str, params: dict) -> dict:
    """One toggle: its icon shows the current mode, clicking switches to the other. Switching
    reloads the page, so `params` carries the current view over."""
    other_theme = "dark" if theme == "light" else "light"
    icon = "sun" if theme == "light" else "moon"
    return {
        "theme": other_theme,
        "label": f"Switch to {other_theme} mode",
        "icon": icon,
        "icon_class": f"icon-{icon}",
        "href": "?" + urlencode({k: v for k, v in {"theme": other_theme, **params}.items() if v}),
    }


def remember_theme(request, response):
    """Save a ?theme= choice in the cookie, so the server renders the right theme next time."""
    requested = request.GET.get("theme")
    if requested in THEMES:
        response.set_cookie(
            settings.THEME_COOKIE,
            requested,
            max_age=settings.THEME_COOKIE_MAX_AGE,
            samesite="Lax",
        )
    return response


def index(request):
    theme = resolve_theme(request)

    # A ?q= search is handled in the browser: map.js geocodes it and starts a background analysis
    # (/api/analyze), so the page never waits on the pipeline.
    locations = data.locations()
    location = request.GET.get("location")
    if location not in locations:
        location = next(iter(locations), None)  # the window the map opens on
    query = request.GET.get("q", "").strip()

    searched = data.searched()
    demo = [s for s in DEMO_LOCATIONS if s in locations and s not in searched]
    recent = [
        {"slug": slug, "place": locations[slug]["name"], "when": observed_local(locations[slug])}
        for slug in (searched + demo)[:RECENT_MAX]
    ]
    theme_toggle = theme_toggle_for(theme, {"location": location, "q": query})

    response = render(
        request,
        "flood/index.html",
        {
            "theme": theme,
            "theme_toggle": theme_toggle,
            "facility_kinds": KINDS,
            "basemap_style": settings.BASEMAP_STYLES[theme],
            "location": location,
            "query": query,
            "recent": recent,
        },
    )
    return remember_theme(request, response)


# Credited on the About page, each name linking to their GitHub profile.
CONTRIBUTORS = [
    {"name": "Pouya Khoshnavazi", "github": "Pouya2077"},
    {"name": "Kevin Low", "github": "kevin18low"},
    {"name": "Iden Huang", "github": "IdenHuang"},
    {"name": "Serena", "github": "aaneres"},
]


def about(request):
    theme = resolve_theme(request)
    response = render(
        request,
        "flood/about.html",
        {
            "theme": theme,
            "theme_toggle": theme_toggle_for(theme, {}),
            "contributors": CONTRIBUTORS,
        },
    )
    return remember_theme(request, response)


def observed_local(meta: dict) -> str:
    """Observation time in the location's time zone, e.g. 'Nov 16, 2021, 6:25 AM PST'."""
    if not meta.get("observed_utc"):
        return ""
    observed = datetime.fromisoformat(meta["observed_utc"])
    local = observed.astimezone(ZoneInfo(meta.get("timezone") or "UTC"))
    hour = local.hour % 12 or 12
    return f"{local:%b} {local.day}, {local.year}, {hour}:{local:%M %p %Z}"

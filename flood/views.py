from django.conf import settings
from django.shortcuts import render
from .utils import get_10km_range
from .fetch_data import fetch_sentinel_radar

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

    response = render(
        request,
        "flood/index.html",
        {
            "theme": theme,
            "other_theme": "dark" if theme == "light" else "light",
            "basemap_style": settings.BASEMAP_STYLES[theme],
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

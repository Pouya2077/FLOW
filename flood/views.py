from django.conf import settings
from django.shortcuts import render

THEMES = ("light", "dark")


def index(request):
    requested = request.GET.get("theme")
    if requested in THEMES:
        theme = requested
    elif request.COOKIES.get(settings.THEME_COOKIE) in THEMES:
        theme = request.COOKIES[settings.THEME_COOKIE]
    else:
        theme = "light"

    response = render(
        request,
        "flood/index.html",
        {
            "theme": theme,
            "other_theme": "dark" if theme == "light" else "light",
            "basemap_style": settings.BASEMAP_STYLES[theme],
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

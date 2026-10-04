"""Django settings for FLOW.

The app is file-backed: precomputed GeoJSON lives in data/locations/, so there is
no database and none of the DB-backed contrib apps (admin, auth, sessions).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "lucide",
    "flood",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {}

# Geocoder results are cached so a live demo survives Photon being slow or down.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"

# Output of the offline pipeline: one folder per demo location.
FLOOD_DATA_DIR = BASE_DIR / "data" / "locations"

# Photon allows autocomplete; Nominatim does not. Identify ourselves either way.
GEOCODER_URL = "https://photon.komoot.io/api/"
GEOCODER_USER_AGENT = "FLOW-StormHacks2026/0.1 (flood-obstructed roads demo)"

# Page theme: ?theme= in the URL wins and is saved to this cookie; otherwise the cookie, else light.
THEME_COOKIE = "theme"
THEME_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # browsers cap cookie lifetime (~400 days in Chrome)

# OpenFreeMap basemap style per page theme. Both are muted so blue flood lines stand out.
BASEMAP_STYLES = {
    "light": "https://tiles.openfreemap.org/styles/positron",
    "dark": "https://tiles.openfreemap.org/styles/dark",
}

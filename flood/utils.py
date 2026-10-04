from django.conf import settings
from django.core.cache import cache
from geopy import distance
from geopy.geocoders import Nominatim


def get_10km_range(address):
    # Nominatim allows ~1 request/s and requires an identifying User-Agent (CLAUDE.md), so cache
    # lookups for a day and identify the app.
    print("flag")
    key = "nominatim:" + address.strip().lower()
    if (cached := cache.get(key)) is not None:
        return cached

    geolocator = Nominatim(user_agent=settings.GEOCODER_USER_AGENT)
    location = geolocator.geocode(address)

    if not location:
        return "Address not found."

    center_lat = location.latitude
    center_lon = location.longitude
    center_point = (center_lat, center_lon)

    radius = distance.distance(kilometers=10)

    north_lat = radius.destination(point=center_point, bearing=0).latitude
    south_lat = radius.destination(point=center_point, bearing=180).latitude
    east_lon = radius.destination(point=center_point, bearing=90).longitude
    west_lon = radius.destination(point=center_point, bearing=270).longitude

    bbox = [west_lon, south_lat, east_lon, north_lat]
    cache.set(key, bbox, 60 * 60 * 24)
    return bbox

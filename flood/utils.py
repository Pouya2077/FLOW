from geopy.geocoders import Nominatim
from geopy import distance

def get_10km_range(address):
    geolocator = Nominatim(user_agent="flow")
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

    return [west_lon, south_lat, east_lon, north_lat]
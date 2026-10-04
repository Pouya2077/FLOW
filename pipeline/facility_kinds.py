"""Kinds of critical buildings drawn on the map: their OpenStreetMap tags, label and lucide icon.

The one place a kind is defined. The pipeline uses `tags` to query OSM; the web app uses `label`
and `icon`. Order matters: a feature matching several kinds gets the first (a hospital that is also
tagged as a clinic is a hospital).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Kind:
    slug: str
    label: str  # shown in the popup
    tags: dict[str, list[str]]  # OSM key -> values; any match counts
    icon: str  # lucide icon name
    line: bool = False  # mapped as lines (dykes), not buildings


KINDS = [
    Kind("hospital", "Hospital", {"amenity": ["hospital"], "healthcare": ["hospital"]}, "hospital"),
    Kind(
        "ambulance_station", "Ambulance station", {"emergency": ["ambulance_station"]}, "ambulance"
    ),
    Kind("fire_station", "Fire station", {"amenity": ["fire_station"]}, "flame"),
    Kind("police", "Police", {"amenity": ["police"]}, "shield"),
    Kind(
        "water_rescue",
        "Water rescue",
        {"emergency": ["water_rescue", "lifeboat_station"]},
        "life-buoy",
    ),
    Kind("clinic", "Clinic", {"amenity": ["clinic"], "healthcare": ["clinic"]}, "stethoscope"),
    Kind(
        "shelter",
        "Emergency shelter",
        # amenity=shelter is left out: it's mostly bus stops and picnic shelters
        {"social_facility": ["shelter"], "emergency": ["assembly_point"]},
        "tent",
    ),
    Kind(
        "care_home",
        "Care home",
        {"social_facility": ["nursing_home", "assisted_living"]},
        "house-heart",
    ),
    Kind("school", "School", {"amenity": ["school"]}, "school"),
    Kind("community_centre", "Community centre", {"amenity": ["community_centre"]}, "users"),
    Kind("helipad", "Helipad", {"aeroway": ["helipad", "heliport"]}, "helicopter"),
    Kind("substation", "Power substation", {"power": ["substation"]}, "zap"),
    Kind(
        "water_infrastructure",
        "Water infrastructure",
        {"man_made": ["water_works", "wastewater_plant", "pumping_station"]},
        "droplet",
    ),
    Kind("pharmacy", "Pharmacy", {"amenity": ["pharmacy"]}, "pill"),
    Kind("fuel", "Fuel station", {"amenity": ["fuel"]}, "fuel"),
    Kind("dyke", "Dyke or dam", {"man_made": ["dyke"], "waterway": ["dam"]}, "dam", line=True),
]

KINDS_BY_SLUG = {k.slug: k for k in KINDS}


# All kinds' tags in one OSMnx query: {key: [values]}
def query_tags() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for kind in KINDS:
        for key, values in kind.tags.items():
            out.setdefault(key, [])
            out[key] += [v for v in values if v not in out[key]]
    return out


# The first kind whose tags the OSM feature carries, or None
def classify(tags: dict) -> Kind | None:
    for kind in KINDS:
        if any(tags.get(key) in values for key, values in kind.tags.items()):
            return kind
    return None

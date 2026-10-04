"""Find critical buildings (hospitals, police, fire stations, ...) in a location's observation
window and score how flooded each site is. Writes data/locations/<slug>/facilities.geojson.

OSM is queried as it was at the satellite observation (Overpass "attic" data), so the map shows
what was mapped at the time of the flood. Data only goes back to Sep 2012, and it's what had been
*mapped* by then, not what existed. If the dated query fails, today's map is used and `osm_date`
says so.

Runs at the end of build_location, or on its own with the same mask:
    uv run python -m pipeline.build_facilities sumas-prairie --mask data/masks/sumas-prairie_s1.tif
"""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import rasterio
import shapely
from osmnx._errors import InsufficientResponseError
from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import linemerge

from pipeline.build_location import (
    MASK_DIR,
    OUT_DIR,
    clean,
    fetch_permanent_water,
    rounded,
    score_areas,
)
from pipeline.facility_kinds import KINDS, classify, query_tags
from pipeline.regions import Region, get_region

POINT_RADIUS_M = 25  # site scored around a facility mapped only as a point
LINE_BUFFER_M = 10  # half-width scored along a dyke or dam
DEDUP_M = 50  # same kind within this distance is one facility (node + building outline)
PRIVATE = {"private", "no"}  # access/operator:type/ownership values meaning "not public"


# Drop facilities the public can't use or that are privately run
def is_public(tags: dict) -> bool:
    return not (
        tags.get("access") in PRIVATE
        or tags.get("operator:type") == "private"
        or tags.get("ownership") == "private"
    )


def address(tags: dict) -> str | None:
    street = " ".join(t for t in (tags.get("addr:housenumber"), tags.get("addr:street")) if t)
    parts = [p for p in (street, tags.get("addr:city"), tags.get("addr:postcode")) if p]
    return ", ".join(parts) if street else tags.get("addr:full")


# Contact details from the plain tag or its contact:* variant
def contact(tags: dict, key: str) -> str | None:
    return tags.get(key) or tags.get(f"contact:{key}")


# Whether a hospital or clinic has an emergency department; None if not mapped
def has_emergency(kind_slug: str, tags: dict) -> bool | None:
    if kind_slug not in ("hospital", "clinic"):
        return None  # for other kinds `emergency` holds the kind itself (ambulance_station, ...)
    return {"yes": True, "no": False}.get(tags.get("emergency"))


# Query the tags for every kind inside `polygon` (lon/lat), as OSM was at `when` (UTC)
def fetch_osm(polygon, when: datetime | None) -> gpd.GeoDataFrame:
    default = ox.settings.overpass_settings
    if when:
        ox.settings.overpass_settings = default + f'[date:"{when:%Y-%m-%dT%H:%M:%SZ}"]'
    try:
        return ox.features_from_polygon(polygon, query_tags())
    except InsufficientResponseError:  # nothing mapped there
        return gpd.GeoDataFrame(geometry=[], crs=4326)
    finally:
        ox.settings.overpass_settings = default


# OSM at the observation time, falling back to today's map. Returns the features and their date
def fetch_facilities_osm(polygon, observed_utc: str | None) -> tuple[gpd.GeoDataFrame, str]:
    if observed_utc:
        when = datetime.fromisoformat(observed_utc.replace("Z", "+00:00")).astimezone(UTC)
        try:
            return fetch_osm(polygon, when), when.date().isoformat()
        except Exception as e:  # attic queries are slow and only some Overpass servers have them
            print(f"historical OSM query failed ({e}); using today's map")
    return fetch_osm(polygon, None), datetime.now(UTC).date().isoformat()


# The point an icon is drawn at: inside a building's outline, or halfway along a line
def marker(geom):
    if isinstance(geom, LineString | MultiLineString):
        line = linemerge(geom) if isinstance(geom, MultiLineString) else geom
        if isinstance(line, MultiLineString):
            line = max(line.geoms, key=lambda g: g.length)
        return line.interpolate(0.5, normalized=True)
    return geom.point_on_surface()


# One row per facility in the window: kind, tags and geometry in `crs`, clipped to `window`
def facilities_in_window(osm: gpd.GeoDataFrame, window, crs) -> list[dict]:
    if osm.empty:
        return []
    osm = osm.to_crs(crs)
    rows = []
    for (element, osm_id), row in osm.iterrows():
        tags = {k: v for k, v in row.items() if k != "geometry" and clean(v) is not None}
        kind = classify(tags)
        if kind is None or not is_public(tags):
            continue
        geom = row.geometry
        if kind.line:
            if geom.geom_type in ("Polygon", "MultiPolygon"):  # dams mapped as areas
                geom = geom.boundary
            geom = geom.intersection(window)
            if geom.is_empty or geom.length == 0:
                continue
        elif not window.contains(marker(geom)):
            continue
        rows.append({"osm_id": f"{element}/{osm_id}", "kind": kind, "tags": tags, "geometry": geom})
    return rows


# The same facility is often mapped twice (a node and its building). Keep the outline, fill gaps
# in its tags from the other
def dedupe(rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda r: (-r["geometry"].area, r["osm_id"]))
    kept: list[dict] = []
    for row in rows:
        twin = None
        if not row["kind"].line:
            twin = next(
                (
                    k
                    for k in kept
                    if k["kind"] == row["kind"]
                    and k["geometry"].distance(row["geometry"]) < DEDUP_M
                ),
                None,
            )
        if twin is None:
            kept.append(row)
        else:
            twin["tags"] = {**row["tags"], **twin["tags"]}
    return kept


# The area whose mask pixels say how flooded a site is
def site(geom):
    if geom.geom_type == "Point":
        return geom.buffer(POINT_RADIUS_M)
    if geom.geom_type in ("LineString", "MultiLineString"):
        return geom.buffer(LINE_BUFFER_M)
    return geom


def build_facilities(
    region: Region,
    mask,
    transform,
    crs,
    water: gpd.GeoDataFrame,
    footprint,
    observed_utc: str | None,
    out_dir: Path = OUT_DIR,
) -> Path:
    """Query, score and write facilities.geojson. `footprint` is the observed window in lon/lat."""
    osm, osm_date = fetch_facilities_osm(footprint, observed_utc)
    window = gpd.GeoSeries([footprint], crs=4326).to_crs(crs).iloc[0]
    rows = dedupe(facilities_in_window(osm, window, crs))
    print(f"{len(rows)} critical buildings in the window (OSM as of {osm_date})")

    status, fraction, confidence = (
        score_areas([site(r["geometry"]) for r in rows], mask, transform, water)
        if rows
        else ([], [], [])
    )

    order = {k.slug: i for i, k in enumerate(KINDS)}
    features = []
    for r, s, f, c in zip(rows, status, fraction, confidence, strict=True):
        kind, tags, geom = r["kind"], r["tags"], r["geometry"]
        props = {
            "osm_id": r["osm_id"],
            "kind": kind.slug,
            "name": tags.get("name"),
            "address": address(tags),
            "phone": contact(tags, "phone"),
            "emergency_phone": tags.get("emergency:phone"),
            "email": contact(tags, "email"),
            "website": contact(tags, "website"),
            "emergency": has_emergency(kind.slug, tags),
            "flood_status": s,
            "flooded_fraction": rounded(f),
            "confidence": rounded(c),
            "length_m": round(geom.length) if kind.line else None,
            "observed_utc": observed_utc,
            "osm_date": osm_date,
        }
        lonlat = gpd.GeoSeries([marker(geom), geom], crs=crs).to_crs(4326)
        lonlat = shapely.set_precision(lonlat.values, 1e-6)  # ~0.1 m
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(lonlat[0]),
                "properties": props | {"shape": "point"},
            }
        )
        if kind.line:  # the dyke itself, drawn when its icon is selected
            features.append(
                {
                    "type": "Feature",
                    "geometry": mapping(lonlat[1]),
                    "properties": props | {"shape": "line"},
                }
            )

    features.sort(
        key=lambda f: (
            order[f["properties"]["kind"]],
            f["properties"]["name"] or "￿",  # unnamed last
            f["properties"]["osm_id"],
            f["properties"]["shape"],
        )
    )
    out = out_dir / region.slug
    out.mkdir(parents=True, exist_ok=True)
    collection = {"type": "FeatureCollection", "features": features}
    (out / "facilities.geojson").write_text(json.dumps(collection, separators=(",", ":")))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("region", help="region slug from pipeline/regions.py")
    parser.add_argument("--mask", type=Path, help="flood mask GeoTIFF (default: synthetic)")
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_DIR,
        help="output folder (default: data/locations, committed)",
    )
    args = parser.parse_args()

    region = get_region(args.region)
    meta = json.loads((OUT_DIR / region.slug / "meta.json").read_text())
    mask_path = args.mask or MASK_DIR / f"{region.slug}_synthetic.tif"
    with rasterio.open(mask_path) as src:
        crs, transform = src.crs, src.transform
        mask = src.read(1)

    water = fetch_permanent_water(region, crs)
    footprint = shape(meta["footprint"])
    out = build_facilities(
        region, mask, transform, crs, water, footprint, meta.get("observed_utc"), args.out
    )
    print(f"wrote facilities to {out}")


if __name__ == "__main__":
    main()

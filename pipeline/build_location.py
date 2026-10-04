"""Overlay a flood mask on OpenStreetMap roads and write the region's map data.
Ignore bridges and permanent water bodies, so they are not interpreted as floods.
Break roads into x meter chunks, so that flood locations can be more specific.
"""

import argparse
import json
import math
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import rasterio
import shapely
from osmnx._errors import InsufficientResponseError
from rasterio.features import rasterize, shapes
from rasterstats import zonal_stats
from shapely.geometry import mapping, shape
from shapely.ops import linemerge, substring, unary_union

from pipeline.overpass import use_reachable_server
from pipeline.regions import Region, get_region

ROOT = Path(__file__).resolve().parent.parent
MASK_DIR = ROOT / "data" / "masks"
OUT_DIR = ROOT / "data" / "locations"

SEGMENT_M = 20  # target chunk length in meters; ~2 radar pixels
PERMANENT_WATER_TAGS = {
    "natural": "water",
    "waterway": ["riverbank", "canal", "dock"],
    "landuse": ["reservoir", "basin"],
}

BUFFER_M = 5  # road half-width: ~10 m strip, about one road width plus OSM/imagery offset
FLOODED_AT = 0.5  # minimum threshold of pixels covered to be considered flooded
MIN_FLOOD_RUN_M = 40  # shorter isolated flooded runs are treated as radar speckle
DRY, WATER, NO_DATA = 0, 1, 255  # mask values
PERMANENT = 254  # value we give permanent-water pixels so they count as neither wet nor dry


# OSMnx stores mixed tags of merged pieces as a list in no fixed order; sort so reruns agree
def first(value):
    return sorted(value, key=str)[0] if isinstance(value, list) else value


# Turn pandas NaN values into null for JSON
def clean(value):
    return None if isinstance(value, float) and math.isnan(value) else value


def rounded(value, digits=2):
    value = clean(value)
    return None if value is None else round(value, digits)


# Returns all roads cars can drive on in the region, one row per road stretch
def fetch_roads(region: Region, crs) -> gpd.GeoDataFrame:
    graph = ox.graph_from_bbox(region.bbox, network_type="drive", simplify=False)
    # Never merge across a bridge or a name change, so each stretch has one true name
    graph = ox.simplify_graph(graph, edge_attrs_differ=["bridge", "name"])
    graph = ox.convert.to_undirected(graph)
    roads = ox.graph_to_gdfs(graph, nodes=False).reset_index(drop=True)
    roads = roads.reindex(columns=["osmid", "name", "highway", "bridge", "geometry"])
    for col in ["name", "highway", "bridge"]:
        roads[col] = roads[col].map(first)
    return roads.to_crs(crs)


# Return all rivers, lakes and canals as polygons and ignore them when scoring roads
def fetch_permanent_water(region: Region, crs) -> gpd.GeoDataFrame:
    try:
        water = ox.features_from_bbox(region.bbox, PERMANENT_WATER_TAGS)
    except InsufficientResponseError:  # region has no mapped water
        return gpd.GeoDataFrame(geometry=[], crs=crs)
    polygons = water[water.geom_type.isin(["Polygon", "MultiPolygon"])]
    return polygons[["geometry"]].reset_index(drop=True).to_crs(crs)


# Do not consider bridges so they are not interpreted as flooded
def drop_bridges(roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    is_bridge = roads["bridge"].notna() & (roads["bridge"] != "no")
    return roads[~is_bridge].reset_index(drop=True)


# Cut each road into equal pieces of ~SEGMENT_M metres, keeping their order along the road
def split_roads(roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    rows = []
    for road in roads.itertuples():
        line = road.geometry
        n = max(1, round(line.length / SEGMENT_M))
        step = line.length / n
        ids = road.osmid if isinstance(road.osmid, list) else [road.osmid]
        for i in range(n):
            rows.append(
                {
                    "road_id": road.Index,
                    "seq": i,
                    "name": road.name,
                    "highway": road.highway,
                    "osm_way_ids": [int(x) for x in ids],
                    "geometry": substring(line, i * step, (i + 1) * step),
                }
            )
    return gpd.GeoDataFrame(rows, crs=roads.crs)


# Label each chunk as flooded/clear/no_data based on the mask pixels
def score_segments(segments, mask, transform, water) -> gpd.GeoDataFrame:
    corridors = segments.geometry.buffer(BUFFER_M, cap_style="flat")
    status, fraction, confidence = score_areas(corridors, mask, transform, water)
    return segments.assign(status=status, flooded_fraction=fraction, confidence=confidence)


# Status, flooded fraction and confidence for each area (polygons in the mask's CRS), from the
# mask pixels touching it. Permanent-water pixels count as neither wet nor dry
def score_areas(areas, mask, transform, water) -> tuple[list, list, list]:
    grid = mask.astype("int16")  # allows no_data = -1
    if len(water):
        permanent = rasterize(water.geometry, out_shape=mask.shape, transform=transform)
        grid[permanent == 1] = PERMANENT

    counts = zonal_stats(
        areas, grid, affine=transform, categorical=True, all_touched=True, nodata=-1
    )

    status, fraction, confidence = [], [], []
    for c in counts:
        wet, dry, unseen = c.get(WATER, 0), c.get(DRY, 0), c.get(NO_DATA, 0)
        seen = wet + dry
        if seen == 0 or unseen > seen:  # do not assume 'clear'
            status.append("no_data")
            fraction.append(None)
            confidence.append(None)
            continue
        f = wet / seen
        flooded = f >= FLOODED_AT
        status.append("flooded" if flooded else "clear")
        fraction.append(f)
        confidence.append(f if flooded else 1 - f)  # how strongly the pixels agree with status

    return status, fraction, confidence


# Mark short flooded runs with clear road on both sides as clear: on real radar these are
# usually speckle. Runs touching a stretch's end are kept, since the flood may carry on past the
# intersection. Runs next to no_data are kept
def remove_speckle(segments: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    segments = segments.sort_values(["road_id", "seq"]).reset_index(drop=True)
    status, road = segments["status"], segments["road_id"]
    run_ids = ((status != status.shift()) | (road != road.shift())).cumsum()
    lengths = segments.length

    def clear_on_same_road(i: int, road_id) -> bool:
        return 0 <= i < len(segments) and road[i] == road_id and status[i] == "clear"

    flip = []
    for _, run in segments.groupby(run_ids):
        first_i, last_i = run.index[0], run.index[-1]
        if (
            status[first_i] == "flooded"
            and lengths[run.index].sum() < MIN_FLOOD_RUN_M
            and clear_on_same_road(first_i - 1, road[first_i])
            and clear_on_same_road(last_i + 1, road[first_i])
        ):
            flip.extend(run.index)

    segments.loc[flip, "status"] = "clear"
    segments.loc[flip, "confidence"] = 1 - segments.loc[flip, "flooded_fraction"]
    return segments, len(flip)


# Join consecutive chunks of a road with the same status into one line
def merge_runs(segments: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    rows = []
    for _, road in segments.groupby("road_id", sort=False):
        road = road.sort_values("seq")
        run_ids = (road["status"] != road["status"].shift()).cumsum()
        for _, run in road.groupby(run_ids):
            head = run.iloc[0]
            lengths = run.length
            row = {
                "name": head["name"],
                "highway": head["highway"],
                "osm_way_ids": head["osm_way_ids"],
                "status": head["status"],
                "flooded_fraction": None,
                "confidence": None,
                "length_m": lengths.sum(),
                "geometry": linemerge(list(run.geometry)),
            }
            if head["status"] != "no_data":  # length-weighted average over the run
                for col in ["flooded_fraction", "confidence"]:
                    row[col] = float((run[col] * lengths).sum() / lengths.sum())
            rows.append(row)
    return gpd.GeoDataFrame(rows, crs=segments.crs)


# Outline of the area the satellite actually observed (mask pixels that aren't no-data)
def observed_area(mask, transform, crs):
    seen = (mask != NO_DATA).astype("uint8")
    polygons = [shape(g) for g, v in shapes(seen, mask=seen == 1, transform=transform) if v == 1]
    outline = unary_union(polygons).simplify(transform.a)  # tolerance: one pixel
    return gpd.GeoSeries([outline], crs=crs).to_crs(4326).iloc[0]


# Summary of flooded streets, from most to least flooded
def street_summaries(runs: gpd.GeoDataFrame, observed_utc) -> list[dict]:
    lonlat = runs.to_crs(4326)
    out = []
    for name, street in lonlat[lonlat["name"].notna()].groupby("name"):
        by_status = street.groupby("status")["length_m"].sum()
        total = street["length_m"].sum()
        flooded = by_status.get("flooded", 0.0)
        out.append(
            {
                "name": name,
                "total_m": round(total),
                "flooded_m": round(flooded),
                "no_data_m": round(by_status.get("no_data", 0.0)),
                "pct": round(100 * flooded / total, 1),
                "observed_utc": observed_utc,
                "bbox": [round(v, 6) for v in street.total_bounds],
            }
        )
    return sorted(out, key=lambda s: s["flooded_m"], reverse=True)


# Convert regions to lon/lat and write to JSON for rendering
def write_location(region, runs, tags, footprint, out_dir: Path = OUT_DIR) -> Path:
    out = out_dir / region.slug
    out.mkdir(parents=True, exist_ok=True)
    observed_utc = tags.get("observed_utc")

    lonlat = runs.to_crs(4326)
    lonlat["geometry"] = shapely.set_precision(lonlat.geometry.values, 1e-6)  # ~0.1 m
    features = [
        {
            "type": "Feature",
            "geometry": mapping(r.geometry),
            "properties": {
                "name": clean(r.name),
                "highway": clean(r.highway),
                "osm_way_ids": r.osm_way_ids,
                "status": r.status,
                "flooded_fraction": rounded(r.flooded_fraction),
                "confidence": rounded(r.confidence),
                "observed_utc": observed_utc,
            },
        }
        for r in lonlat.itertuples()
    ]
    segments = {"type": "FeatureCollection", "features": features}
    (out / "segments.geojson").write_text(json.dumps(segments, separators=(",", ":")))

    streets = street_summaries(runs, observed_utc)
    (out / "streets.json").write_text(json.dumps(streets, indent=2))

    meta = {
        "name": region.name,
        "bbox": list(region.bbox),
        "observed_utc": observed_utc,
        "timezone": region.timezone,
        "sensor": tags.get("sensor"),
        "synthetic": tags.get("synthetic") == "true",
        "footprint": mapping(shapely.set_precision(footprint, 1e-6)),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
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
    build(region, args.mask or MASK_DIR / f"{region.slug}_synthetic.tif", args.out)


# Overlay one mask on the region's roads and write <out_dir>/<slug>/
def build(region: Region, mask_path: Path, out_dir: Path = OUT_DIR) -> Path:
    with rasterio.open(mask_path) as src:
        crs, transform, tags = src.crs, src.transform, src.tags()
        mask = src.read(1)

    use_reachable_server()
    roads = fetch_roads(region, crs)
    print(f"{len(roads)} road stretches, {roads.length.sum() / 1000:.1f} km total")

    water = fetch_permanent_water(region, crs)
    print(f"{len(water)} permanent water polygons, {water.area.sum() / 1e6:.2f} km² total")

    roads = drop_bridges(roads)
    print(f"{len(roads)} road stretches after dropping bridges")

    segments = split_roads(roads)
    print(f"{len(segments)} segments, mean length {segments.length.mean():.1f} m")

    segments = score_segments(segments, mask, transform, water)
    km = segments.assign(m=segments.length).groupby("status")["m"].sum() / 1000
    print("km by status:", ", ".join(f"{s} {v:.1f}" for s, v in km.items()))

    segments, flipped = remove_speckle(segments)
    print(f"{flipped} segments in isolated flooded runs < {MIN_FLOOD_RUN_M} m reset to clear")

    runs = merge_runs(segments)
    footprint = observed_area(mask, transform, crs)
    out = write_location(region, runs, tags, footprint, out_dir)
    print(f"wrote {len(runs)} road stretches to {out}")

    # Critical buildings, scored against the same mask so they never disagree with the roads.
    # Optional extra: if OSM can't be reached the roads above are still written and served.
    from pipeline.build_facilities import build_facilities  # imports this module

    try:
        build_facilities(
            region, mask, transform, crs, water, footprint, tags.get("observed_utc"), out_dir
        )
    except Exception as e:
        print(f"critical buildings skipped: {e}")
    return out


if __name__ == "__main__":
    main()

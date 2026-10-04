"""Overlay a flood mask on OpenStreetMap roads and write the region's map data.
Ignore bridges and permanent water bodies, so they are not interpreted as floods.
Break roads into x meter chunks, so that flood locations can be more specific.
"""

import argparse
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import rasterio
from osmnx._errors import InsufficientResponseError
from shapely.ops import substring

from pipeline.regions import Region, get_region

ROOT = Path(__file__).resolve().parent.parent
MASK_DIR = ROOT / "data" / "masks"

SEGMENT_M = 20  # target chunk length in meters; ~2 radar pixels
PERMANENT_WATER_TAGS = {
    "natural": "water",
    "waterway": ["riverbank", "canal", "dock"],
    "landuse": ["reservoir", "basin"],
}


def first(value):
    return value[0] if isinstance(value, list) else value


# Returns all roads cars can drive on in the region, one row per road stretch
def fetch_roads(region: Region, crs) -> gpd.GeoDataFrame:
    graph = ox.graph_from_bbox(region.bbox, network_type="drive", simplify=False)
    graph = ox.simplify_graph(graph, edge_attrs_differ=["bridge"])
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
                    "osm_way_ids": ids,
                    "geometry": substring(line, i * step, (i + 1) * step),
                }
            )
    return gpd.GeoDataFrame(rows, crs=roads.crs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("region", help="region slug from pipeline/regions.py")
    parser.add_argument("--mask", type=Path, help="flood mask GeoTIFF (default: synthetic)")
    args = parser.parse_args()

    region = get_region(args.region)
    mask_path = args.mask or MASK_DIR / f"{region.slug}_synthetic.tif"
    with rasterio.open(mask_path) as src:
        crs = src.crs

    roads = fetch_roads(region, crs)
    print(f"{len(roads)} road stretches, {roads.length.sum() / 1000:.1f} km total")

    water = fetch_permanent_water(region, crs)
    print(f"{len(water)} permanent water polygons, {water.area.sum() / 1e6:.2f} km² total")

    roads = drop_bridges(roads)
    print(f"{len(roads)} road stretches after dropping bridges")

    segments = split_roads(roads)
    print(f"{len(segments)} segments, mean length {segments.length.mean():.1f} m")
    print(segments.head())


if __name__ == "__main__":
    main()

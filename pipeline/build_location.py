"""Overlay a flood mask on OpenStreetMap roads and write the region's map data"""

import argparse
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import rasterio

from pipeline.regions import Region, get_region

ROOT = Path(__file__).resolve().parent.parent
MASK_DIR = ROOT / "data" / "masks"


def first(value):
    return value[0] if isinstance(value, list) else value


# Returns all roads cars can drive on in the region, one row per road stretch
def fetch_roads(region: Region, crs) -> gpd.GeoDataFrame:
    graph = ox.graph_from_bbox(region.bbox, network_type="drive")
    graph = ox.convert.to_undirected(graph)
    roads = ox.graph_to_gdfs(graph, nodes=False).reset_index(drop=True)
    roads = roads.reindex(columns=["osmid", "name", "highway", "bridge", "geometry"])
    for col in ["name", "highway", "bridge"]:
        roads[col] = roads[col].map(first)
    return roads.to_crs(crs)


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
    print(roads.head())


if __name__ == "__main__":
    main()

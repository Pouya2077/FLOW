"""Write a location's road graph for routing: data/locations/<slug>/graph.json.

The directed drivable network (one-way streets kept, bridges kept) with a travel time per road and
how much of it the satellite saw flooded, scored against the same mask and the same way as the
road segments. The web app routes on it (/api/route); see CLAUDE.md "Road graph".

Runs at the end of build_location, or on its own with the same mask:
    uv run python -m pipeline.build_graph sumas-prairie --mask data/masks/sumas-prairie_s1.tif
"""

import argparse
import json
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import rasterio
import shapely

from pipeline.build_location import (
    MASK_DIR,
    OUT_DIR,
    clean,
    fetch_permanent_water,
    first,
    remove_speckle,
    score_segments,
    split_roads,
)
from pipeline.regions import Region, get_region


# The drivable network as directed edges in `crs`, with OSM speed limits and travel times
def fetch_graph_edges(region: Region, crs) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    graph = ox.graph_from_bbox(region.bbox, network_type="drive", simplify=False)
    graph = ox.simplify_graph(graph, edge_attrs_differ=["bridge", "name"])  # as build_location
    graph = ox.add_edge_speeds(graph)  # missing limits: mean of the same road type
    graph = ox.add_edge_travel_times(graph)
    nodes, edges = ox.graph_to_gdfs(graph)  # edges get a straight line where OSM has no shape
    edges = edges.reset_index()  # u, v, key columns
    for col in ["name", "highway", "bridge"]:
        edges[col] = edges[col].map(first) if col in edges else None
    return nodes, edges.to_crs(crs)


def is_bridge(value) -> bool:
    return clean(value) is not None and value != "no"


# Where along each edge the satellite saw what: runs of [from, to, status, water share], from and to
# as fractions of the edge's length (0 at its source junction). ~20 m pieces scored like the road
# segments, then merged. Routing uses the runs to treat part of a road correctly when a picked
# point splits it. Bridges get no runs: not scored, never treated as flooded.
def score_edges(edges: gpd.GeoDataFrame, mask, transform, water) -> list[dict]:
    pieces = split_roads(edges[~edges["bridge"].map(is_bridge)])  # road_id = edge's row number
    pieces = score_segments(pieces, mask, transform, water)
    pieces, _ = remove_speckle(pieces)
    pieces = pieces.assign(m=pieces.length)

    runs: dict[int, list] = {}
    for road_id, group in pieces.groupby("road_id"):
        group = group.sort_values("seq")
        total = group["m"].sum()
        edge_runs, start = [], 0.0
        for _, run in group.groupby((group["status"] != group["status"].shift()).cumsum()):
            m = run["m"].sum()
            status = run["status"].iloc[0]
            share = None
            if status != "no_data":
                share = round(float((run["m"] * run["flooded_fraction"]).sum() / m), 2)
            end = start + m / total
            edge_runs.append([round(start, 4), round(end, 4), status, share])
            start = end
        edge_runs[-1][1] = 1.0
        runs[road_id] = edge_runs

    out = []
    for i in range(len(edges)):
        if i not in runs:
            out.append({"status": "bridge"})
            continue
        statuses = {r[2] for r in runs[i]}
        status = next(s for s in ("flooded", "no_data", "clear") if s in statuses)
        out.append({"status": status, "runs": runs[i]})
    return out


def build_graph(region: Region, mask, transform, crs, water, out_dir: Path = OUT_DIR) -> Path:
    """Fetch, score and write graph.json for `region`."""
    nodes, edges = fetch_graph_edges(region, crs)
    scores = score_edges(edges, mask, transform, water)
    shapes = edges.geometry.to_crs(4326)
    shapes = shapely.set_precision(shapes.values, 1e-6)  # ~0.1 m, as the segments

    graph_edges = []
    for edge, score, line in zip(edges.itertuples(), scores, shapes, strict=True):
        graph_edges.append(
            {
                "source": int(edge.u),
                "target": int(edge.v),
                "key": int(edge.key),
                "name": clean(edge.name),
                "length_m": round(float(edge.length), 1),
                "travel_time_s": round(float(edge.travel_time), 1),
                **score,
                "geometry": [list(c) for c in line.coords],
            }
        )
    graph_edges.sort(key=lambda e: (e["source"], e["target"], e["key"]))
    graph_nodes = [
        {"id": int(n), "x": round(float(row.x), 6), "y": round(float(row.y), 6)}
        for n, row in nodes.sort_index().iterrows()
    ]
    graph = {"directed": True, "multigraph": True, "nodes": graph_nodes, "edges": graph_edges}

    out = out_dir / region.slug
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(graph, separators=(",", ":")))
    flooded = sum(1 for e in graph_edges if e["status"] == "flooded")
    print(f"road graph: {len(graph_nodes)} junctions, {len(graph_edges)} edges ({flooded} flooded)")
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
    mask_path = args.mask or MASK_DIR / f"{region.slug}_synthetic.tif"
    with rasterio.open(mask_path) as src:
        crs, transform = src.crs, src.transform
        mask = src.read(1)
    water = fetch_permanent_water(region, crs)
    out = build_graph(region, mask, transform, crs, water, args.out)
    print(f"wrote graph to {out}")


if __name__ == "__main__":
    main()

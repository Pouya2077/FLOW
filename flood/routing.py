"""Fastest routes on a location's road graph that avoid roads where water was detected.

The graph comes from the pipeline (graph.json, see CLAUDE.md "Road graph"); this module only
searches it, per request, in a few milliseconds.

- First search: fastest by travel time with flooded roads left out. Roads the satellite didn't see
  are used like clear roads, but make the route cautionary.
- Fallback: if nothing is left, flooded roads are allowed with a penalty per metre of water, so the
  route trades a little time for much less flooding.
A route is "clear" only when every road on it was observed clear (or is a bridge); otherwise it's
"cautionary" with its reasons. Times come from speed limits: an estimate, not live traffic.
"""

import math

import networkx as nx
import numpy as np

# Extra cost of a flooded road, in seconds per metre of water on it (length x water share). At
# 0.6, cutting 500 m of fully flooded road is worth 5 extra minutes. Tune on the demo event.
FLOOD_PENALTY_S_PER_M = 0.6
MAX_SNAP_M = 1000  # a picked point further than this from any road can't start or end a route
AT_JUNCTION_M = 1  # a point this close to a road's end starts at that junction, not on the road

EDGE_TOTALS = ("observed_m", "water_m", "flooded_m", "flooded_water_m", "unobserved_m")
STATUS_ORDER = ("flooded", "no_data", "clear")  # a road part takes the worst status it contains


class RouteError(Exception):
    """A route can't be found; the message is shown to the user as is."""


# The graph plus a flat array of every road piece, for finding the road nearest a point
class RoadGraph:
    def __init__(self, data: dict):
        self.graph = nx.node_link_graph(data, edges="edges")
        for _, _, d in self.graph.edges(data=True):
            d.update(_span(d, 0.0, 1.0))
        lat = np.mean([n["y"] for n in data["nodes"]]) if data["nodes"] else 0.0
        self.kx, self.ky = 111_320 * math.cos(math.radians(lat)), 110_574  # metres per degree
        pieces = []
        for u, v, k, d in self.graph.edges(keys=True, data=True):
            coords = d["geometry"]
            for i in range(len(coords) - 1):
                pieces.append((u, v, k, i, *coords[i], *coords[i + 1]))
        self.pieces = pieces
        arr = np.array([p[4:] for p in pieces], dtype=float).reshape(-1, 4)
        self.a = arr[:, :2] * (self.kx, self.ky)
        self.b = arr[:, 2:] * (self.kx, self.ky)

    # (edge, piece index, share along that piece, distance in metres) of the road nearest a point
    def nearest(self, lon: float, lat: float):
        if not self.pieces:
            raise RouteError("Routing isn't available for this area yet.")
        p = np.array([lon * self.kx, lat * self.ky])
        ab = self.b - self.a
        length2 = (ab**2).sum(axis=1)
        t = np.clip(((p - self.a) * ab).sum(axis=1) / np.where(length2 == 0, 1, length2), 0, 1)
        closest = self.a + ab * t[:, None]
        dist = np.hypot(*(closest - p).T)
        i = int(dist.argmin())
        u, v, k, piece = self.pieces[i][:4]
        return (u, v, k), piece, float(t[i]), float(dist[i])


def route(road_graph: RoadGraph, start: tuple[float, float], end: tuple[float, float]) -> dict:
    """Route between two (lon, lat) points; a GeoJSON Feature, or RouteError."""
    graph = road_graph.graph.copy()
    a, a_at = _attach(graph, road_graph, start, "start")
    b, b_at = _attach(graph, road_graph, end, "end")
    if a_at and b_at and a_at[0] == b_at[0]:  # both on the same road: link them along it too
        _link_same_road(graph, a_at, b_at)
    if a == b:
        raise RouteError("The start and destination are the same place.")

    path = _search(graph, a, b, allow_flooded=False)
    if path is None:
        path = _search(graph, a, b, allow_flooded=True)
    if path is None:
        raise RouteError("No route found between these points, even through flooded roads.")
    flooded = [graph.nodes[n].get("flooded", False) for n in (a, b)]  # junctions: never
    return _feature(path, a_flooded=flooded[0], b_flooded=flooded[1])


# Add the picked point as a node on its nearest road, with the road split in two at that point
# (both directions, if the road is two-way), so the route starts exactly where the user clicked.
def _attach(graph, road_graph: RoadGraph, point, name: str):
    (u, v, k), piece, t, dist = road_graph.nearest(*point)
    if dist > MAX_SNAP_M:
        raise RouteError("Couldn't find a road near this point.")
    edge = graph.edges[u, v, k]
    coords = edge["geometry"]
    a, b = coords[piece], coords[piece + 1]
    at = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
    head, tail = coords[: piece + 1] + [at], [at] + coords[piece + 1 :]
    whole = _length(coords)
    share = _length(head) / max(whole, 1e-9)
    # At a junction: start there. Splitting would tie the point to whichever road came first,
    # possibly a flooded one, though several roads meet there.
    if share * whole < AT_JUNCTION_M:
        return u, None
    if (1 - share) * whole < AT_JUNCTION_M:
        return v, None

    graph.add_node(name, x=at[0], y=at[1], flooded=_status_at(edge, share) == "flooded")
    graph.add_edge(u, name, **_part(edge, head, 0.0, share))
    graph.add_edge(name, v, **_part(edge, tail, share, 1.0))
    back = _reverse(graph, u, v, coords)
    if back is not None:  # positions along the reverse road run the other way
        graph.add_edge(v, name, **_part(back, tail[::-1], 0.0, 1 - share))
        graph.add_edge(name, u, **_part(back, head[::-1], 1 - share, 1.0))
    return name, ((u, v, k), piece, t, share, at)


# The same road in the opposite direction, if it's two-way
def _reverse(graph, u, v, coords):
    for back in (graph.get_edge_data(v, u) or {}).values():
        if back["geometry"] == coords[::-1]:
            return back
    return None


# Two picks on one road: a direct edge between them, in each direction the road allows
def _link_same_road(graph, a_at, b_at):
    (u, v, k), *_ = a_at
    edge = graph.edges[u, v, k]
    first, second = sorted([a_at, b_at], key=lambda at: at[3])  # by share along u -> v
    (_, p1, _, s1, at1), (_, p2, _, s2, at2) = first, second
    middle = [at1] + edge["geometry"][p1 + 1 : p2 + 1] + [at2]
    lo, hi = ("start", "end") if first is a_at else ("end", "start")
    graph.add_edge(lo, hi, **_part(edge, middle, s1, s2))
    back = _reverse(graph, u, v, edge["geometry"])
    if back is not None:
        graph.add_edge(hi, lo, **_part(back, middle[::-1], 1 - s2, 1 - s1))


# The part of an edge between two positions along it (fractions of its length), with its time
# scaled and its flood totals and status taken from the runs that fall inside that part
def _part(edge: dict, coords: list, lo: float, hi: float) -> dict:
    whole = edge.get("full", edge)  # always measure against the original road
    part = {**whole, "geometry": coords, "full": whole}
    part["length_m"] = whole["length_m"] * (hi - lo)
    part["travel_time_s"] = whole["travel_time_s"] * (hi - lo)
    part.update(_span(whole, lo, hi))
    return part


# Flood totals (metres) and status of an edge between positions lo and hi
def _span(edge: dict, lo: float, hi: float) -> dict:
    totals = dict.fromkeys(EDGE_TOTALS, 0.0)
    if edge["status"] == "bridge":
        return totals
    seen = set()
    for start, end, status, share in edge.get("runs", []):
        overlap = (min(end, hi) - max(start, lo)) * edge["length_m"]
        if overlap <= 0:
            continue
        seen.add(status)
        if status == "no_data":
            totals["unobserved_m"] += overlap
            continue
        totals["observed_m"] += overlap
        totals["water_m"] += overlap * share
        if status == "flooded":
            totals["flooded_m"] += overlap
            totals["flooded_water_m"] += overlap * share
    totals["status"] = next((s for s in STATUS_ORDER if s in seen), "clear")
    return totals


def _status_at(edge: dict, position: float) -> str:
    for start, end, status, _ in edge.get("runs", []):
        if start <= position <= end:
            return status
    return edge["status"]


def _length(coords) -> float:
    lat = math.radians(coords[0][1]) if coords else 0.0
    kx = 111_320 * math.cos(lat)
    return sum(
        math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110_574)
        for a, b in zip(coords, coords[1:], strict=False)
    )


def _cost(edge: dict, allow_flooded: bool) -> float | None:
    if edge["status"] == "flooded":
        if not allow_flooded:
            return None  # hidden from the search
        return edge["travel_time_s"] + FLOOD_PENALTY_S_PER_M * edge.get("flooded_water_m", 0)
    return edge["travel_time_s"]


# Cheapest path as a list of edge attribute dicts, or None
def _search(graph, a, b, allow_flooded: bool) -> list[dict] | None:
    def weight(u, v, keys):  # a multigraph passes every parallel road between u and v
        costs = [c for d in keys.values() if (c := _cost(d, allow_flooded)) is not None]
        return min(costs) if costs else None

    try:
        nodes = nx.dijkstra_path(graph, a, b, weight=weight)
    except nx.NetworkXNoPath:
        return None
    edges = []
    for u, v in zip(nodes, nodes[1:], strict=False):
        options = graph.get_edge_data(u, v).values()
        usable = [d for d in options if _cost(d, allow_flooded) is not None]
        edges.append(min(usable, key=lambda d: _cost(d, allow_flooded)))
    return edges


def _feature(edges: list[dict], a_flooded: bool, b_flooded: bool) -> dict:
    coords = []
    for e in edges:
        coords += e["geometry"] if not coords else e["geometry"][1:]
    total = {key: sum(e.get(key, 0) for e in edges) for key in EDGE_TOTALS}
    reasons = []
    if total["flooded_m"] > 0:
        reasons.append("flooded")
    if total["unobserved_m"] > 0:
        reasons.append("unobserved")
    if a_flooded or b_flooded:
        reasons.append("flooded_endpoint")
    observed = total["observed_m"]
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[round(x, 6), round(y, 6)] for x, y in coords],
        },
        "properties": {
            "kind": "cautionary" if reasons else "clear",
            "reasons": reasons,
            "duration_s": round(sum(e["travel_time_s"] for e in edges)),
            "distance_m": round(sum(e["length_m"] for e in edges)),
            "flooded_m": round(total["flooded_m"]),
            "unobserved_m": round(total["unobserved_m"]),
            # Length-weighted share of the observed road covered by water
            "avg_flooded_fraction": round(total["water_m"] / observed, 2) if observed else None,
        },
    }

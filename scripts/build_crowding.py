# -*- coding: utf-8 -*-
"""Assign origin->destination gravity flows onto the real route network.

v2 (O-D model): origins = 10 district centroids weighted by official population;
destinations = OSM attraction points (universities, offices, malls, hospitals,
industry, hubs) aggregated to ~1.2 km grid cells, weighted by assumed daily
attraction. T_oc = P_o * W_c / d^2. A pair's flow splits across routes that
serve BOTH ends, proportionally to trips/day; metro absorbs METRO_SHARE of
pairs it serves. Load index = demand / (trips * capacity), mean-normalized.
"""
import json
import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
GTFS = BASE / "data" / "gtfs"
OUT = BASE / "kyiv_route_load_map.html"

SERVE_KM_ORIGIN = 3.0   # route serves a district if within this of centroid
CELL_DEG_LAT = 0.011    # ~1.2 km grid
CELL_DEG_LON = 0.017
MIN_D_KM = 1.5          # clamp: shorter trips assumed walkable
METRO_SHARE = 0.6
CAPACITY = {"bus": 100, "trolleybus": 110, "tram": 180}

DISTRICTS = [
    ("Голосіївський", 247600, 50.3565, 30.5210),
    ("Дарницький", 314700, 50.4021, 30.6550),
    ("Деснянський", 358300, 50.5265, 30.6110),
    ("Дніпровський", 354700, 50.4520, 30.6110),
    ("Оболонський", 319000, 50.5060, 30.4980),
    ("Печерський", 152000, 50.4230, 30.5470),
    ("Подільський", 198100, 50.4860, 30.4410),
    ("Святошинський", 340700, 50.4570, 30.3720),
    ("Солом'янський", 383259, 50.4270, 30.4700),
    ("Шевченківський", 218900, 50.4600, 30.4880),
]
KMLAT = 111.32
KMLON = 111.32 * math.cos(math.radians(50.45))


def dist_km(lat1, lon1, lat2, lon2):
    return math.hypot((lat1 - lat2) * KMLAT, (lon1 - lon2) * KMLON)


def cell_of(lat, lon):
    return (round(lat / CELL_DEG_LAT), round(lon / CELL_DEG_LON))


def cell_center(cell):
    return cell[0] * CELL_DEG_LAT, cell[1] * CELL_DEG_LON


def cells_with_ring(points, step=1):
    """cells touched by a polyline (plus 1-ring => ~1.2km reach)"""
    out = set()
    for lat, lon in points[::step]:
        ci, cj = cell_of(lat, lon)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out.add((ci + di, cj + dj))
    return out


# --- GTFS aggregation ---
routes = pd.read_csv(GTFS / "routes.txt")
trips = pd.read_csv(GTFS / "trips.txt")
calendar = pd.read_csv(GTFS / "calendar.txt")
shapes = pd.read_csv(GTFS / "shapes.txt")

MODE = {0: "tram", 3: "bus", 11: "trolleybus", 800: "trolleybus"}
routes["mode"] = routes.route_type.map(MODE).fillna("bus")
weekday_services = set(calendar.loc[calendar.monday == 1, "service_id"])
wtrips = trips[trips.service_id.isin(weekday_services)]
routes = routes.merge(wtrips.groupby("route_id").size().rename("trips_day"), on="route_id")

shape_pick = (
    wtrips.groupby(["route_id", "shape_id"]).size().reset_index(name="n")
    .sort_values("n", ascending=False).drop_duplicates("route_id")[["route_id", "shape_id"]]
)
shapes = shapes.sort_values(["shape_id", "shape_pt_sequence"])
shape_coords = {sid: list(zip(g.shape_pt_lat, g.shape_pt_lon)) for sid, g in shapes.groupby("shape_id")}
routes = routes.merge(shape_pick, on="route_id", how="inner").reset_index(drop=True)


def served_districts(coords, step=3):
    out = set()
    for i, (_, _, clat, clon) in enumerate(DISTRICTS):
        for lat, lon in coords[::step]:
            if dist_km(lat, lon, clat, clon) <= SERVE_KM_ORIGIN:
                out.add(i)
                break
    return out


routes["serves_o"] = routes.shape_id.map(lambda s: served_districts(shape_coords.get(s, [])))
routes["serves_c"] = routes.shape_id.map(lambda s: cells_with_ring(shape_coords.get(s, [])))

# metro coverage
metro = json.loads((BASE / "data" / "metro_lines.geojson").read_text(encoding="utf-8-sig"))
metro_by_line = defaultdict(list)
for f in metro["features"]:
    if f.get("geometry", {}).get("type") == "LineString":
        metro_by_line[f["properties"].get("num_r_eng", "?")].extend(
            (y, x) for x, y in f["geometry"]["coordinates"])
metro_cov = [(served_districts(p, step=1), cells_with_ring(p)) for p in metro_by_line.values()]

# --- destinations -> cells ---
dests = json.loads((BASE / "data" / "destinations_osm.json").read_text(encoding="utf-8"))
cell_w = defaultdict(float)
for d in dests:
    cell_w[cell_of(d["lat"], d["lon"])] += d["w"]
print(f"destinations: {len(dests)} points -> {len(cell_w)} cells")

# --- O-D gravity + assignment ---
# invert: for each route, which (origin, cell) pairs can it carry?
cell_routes = defaultdict(list)
for idx, r in routes.iterrows():
    for c in r.serves_c:
        cell_routes[c].append(idx)

routes["demand"] = 0.0
total = 0.0
metro_absorbed = 0.0
unassigned = 0.0
trips_arr = routes.trips_day.to_numpy(dtype=float)
serves_o_list = list(routes.serves_o)

for c, w in cell_w.items():
    clat, clon = cell_center(c)
    metro_here = [mo for mo, mc in metro_cov if c in mc]
    cand_idx = cell_routes.get(c, [])
    for o, (dname, pop, olat, olon) in enumerate(DISTRICTS):
        d = max(dist_km(olat, olon, clat, clon), MIN_D_KM)
        t = pop * w / (d * d)
        total += t
        if any(o in mo for mo in metro_here):
            metro_absorbed += t * METRO_SHARE
            t *= 1 - METRO_SHARE
        cands = [i for i in cand_idx if o in serves_o_list[i]]
        if not cands:
            unassigned += t
            continue
        tsum = sum(trips_arr[i] for i in cands)
        for i in cands:
            routes.at[i, "demand"] += t * trips_arr[i] / tsum

surface = total - metro_absorbed  # flow left for surface routes after metro absorption
print(f"metro absorbed: {100*metro_absorbed/total:.1f}% of total")
print(f"unassigned: {100*unassigned/surface:.1f}% of surface flow")

routes["capacity_day"] = routes.trips_day * routes["mode"].map(CAPACITY)
routes["load_raw"] = routes.demand / routes.capacity_day
served_mask = routes.demand > 0
mean_load = routes.loc[served_mask, "load_raw"].mean()
routes["load"] = (routes.load_raw / mean_load).round(2)
print(f"routes with demand: {served_mask.sum()} of {len(routes)}")
print("load quantiles:", routes.loc[served_mask, "load"].quantile([0.5, 0.9, 1.0]).tolist())

D_IDX = {i: DISTRICTS[i][0] for i in range(len(DISTRICTS))}
routes_json = []
for _, r in routes.iterrows():
    coords = [[round(a, 5), round(b, 5)] for a, b in shape_coords[r.shape_id]]
    routes_json.append({
        "name": str(r.route_short_name),
        "long": r.route_long_name,
        "mode": r["mode"],
        "trips": int(r.trips_day),
        "cap": int(r.capacity_day),
        "load": float(r.load) if r.demand > 0 else None,
        "share": round(100 * r.demand / surface, 2),
        "districts": [D_IDX[i] for i in sorted(r.serves_o)],
        "path": coords,
    })

top10 = sorted([r for r in routes_json if r["load"]], key=lambda r: -r["load"])[:10]
stats = {
    "routes": len(routes_json),
    "with_demand": int(served_mask.sum()),
    "unassigned_pct": round(100 * unassigned / surface, 1),
    "metro_share": int(METRO_SHARE * 100),
    "metro_abs_pct": round(100 * metro_absorbed / total, 1),
    "serve_km": SERVE_KM_ORIGIN,
    "n_dests": len(dests),
    "n_cells": len(cell_w),
    "top10": [{"name": r["name"], "mode": r["mode"], "load": r["load"]} for r in top10],
}

metro_json = [
    {"c": {"Sviatoshynsko-Brovarska": "#E4222C", "Obolonsko-Teremkivska": "#0F63B6",
           "Syretsko-Pecherska": "#00A651"}.get(line, "#888"),
     "path": [[round(a, 5), round(b, 5)] for a, b in pts]}
    for line, pts in metro_by_line.items()
]

# demand per route for downstream tools (simulation)
demand_dump = {
    str(r.route_id): {"demand": float(r.demand), "load": float(r.load) if r.demand > 0 else None}
    for _, r in routes.iterrows()
}
(BASE / "data" / "route_demand.json").write_text(
    json.dumps({"total": total, "routes": demand_dump}, ensure_ascii=False), encoding="utf-8")

TEMPLATE = (BASE / "scripts" / "crowding_template.html").read_text(encoding="utf-8")
html = (TEMPLATE
        .replace("__ROUTES__", json.dumps(routes_json, ensure_ascii=False))
        .replace("__METRO__", json.dumps(metro_json, ensure_ascii=False))
        .replace("__STATS__", json.dumps(stats, ensure_ascii=False)))
OUT.write_text(html, encoding="utf-8")
print(f"written {OUT} ({OUT.stat().st_size/1e6:.1f} MB)")

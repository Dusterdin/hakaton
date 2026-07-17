# -*- coding: utf-8 -*-
"""Before/after upgrade scenario — the Kyiv analog of London's proposed-upgrades
simulation + comparison dashboards.

Baseline: the O-D gravity assignment as in build_crowding.py.
Intervention: every route with baseline load >= 1.5x network mean gets extra
trips (frequency x min(load/1.2, 2.0), i.e. capped at doubling), then the
WHOLE assignment re-runs — flow re-splits toward the upgraded routes, so
relief emerges from the model rather than being assumed.
Loads before and after share the baseline normalization so they are comparable."""
import json
import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
GTFS = BASE / "data" / "gtfs"
OUT = BASE / "kyiv_upgrades_comparison.html"

SERVE_KM_ORIGIN = 3.0
CELL_DEG_LAT = 0.011
CELL_DEG_LON = 0.017
MIN_D_KM = 1.5
METRO_SHARE = 0.6
CAPACITY = {"bus": 100, "trolleybus": 110, "tram": 180}
LOAD_TRIGGER = 1.5   # routes at/above this baseline load get upgraded
LOAD_TARGET = 1.2    # frequency scaled by load/target, capped at 2x

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


def cells_with_ring(points, step=1):
    out = set()
    for lat, lon in points[::step]:
        ci, cj = cell_of(lat, lon)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out.add((ci + di, cj + dj))
    return out


routes = pd.read_csv(GTFS / "routes.txt")
trips = pd.read_csv(GTFS / "trips.txt")
calendar = pd.read_csv(GTFS / "calendar.txt")
shapes = pd.read_csv(GTFS / "shapes.txt")
stop_times = pd.read_csv(GTFS / "stop_times.txt")

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

metro = json.loads((BASE / "data" / "metro_lines.geojson").read_text(encoding="utf-8-sig"))
metro_by_line = defaultdict(list)
for f in metro["features"]:
    if f.get("geometry", {}).get("type") == "LineString":
        metro_by_line[f["properties"].get("num_r_eng", "?")].extend(
            (y, x) for x, y in f["geometry"]["coordinates"])
metro_cov = [(served_districts(p, step=1), cells_with_ring(p)) for p in metro_by_line.values()]

dests = json.loads((BASE / "data" / "destinations_osm.json").read_text(encoding="utf-8"))
cell_w = defaultdict(float)
for d in dests:
    cell_w[cell_of(d["lat"], d["lon"])] += d["w"]

cell_routes = defaultdict(list)
for idx, r in routes.iterrows():
    for c in r.serves_c:
        cell_routes[c].append(idx)
serves_o_list = list(routes.serves_o)

# precompute the O-D pairs once (they don't depend on frequencies)
pairs = []   # (cell, origin, flow_after_metro)
for c, w in cell_w.items():
    clat = c[0] * CELL_DEG_LAT
    clon = c[1] * CELL_DEG_LON
    metro_here = [mo for mo, mc in metro_cov if c in mc]
    for o, (_, pop, olat, olon) in enumerate(DISTRICTS):
        d = max(dist_km(olat, olon, clat, clon), MIN_D_KM)
        t = pop * w / (d * d)
        if any(o in mo for mo in metro_here):
            t *= 1 - METRO_SHARE
        pairs.append((c, o, t))


def assign(trips_arr):
    demand = [0.0] * len(routes)
    for c, o, t in pairs:
        cands = [i for i in cell_routes.get(c, []) if o in serves_o_list[i]]
        if not cands:
            continue
        tsum = sum(trips_arr[i] for i in cands)
        for i in cands:
            demand[i] += t * trips_arr[i] / tsum
    return demand


caps = routes["mode"].map(CAPACITY).to_numpy(dtype=float)
base_trips = routes.trips_day.to_numpy(dtype=float)
base_demand = assign(base_trips)
base_raw = [d / (tr * cp) for d, tr, cp in zip(base_demand, base_trips, caps)]
with_d = [x for x, d in zip(base_raw, base_demand) if d > 0]
mean_load = sum(with_d) / len(with_d)
base_load = [x / mean_load if d > 0 else None for x, d in zip(base_raw, base_demand)]

# intervention
factor = [min(bl / LOAD_TARGET, 2.0) if bl is not None and bl >= LOAD_TRIGGER else 1.0
          for bl in base_load]
new_trips = [t * f for t, f in zip(base_trips, factor)]
new_demand = assign(new_trips)
new_raw = [d / (tr * cp) for d, tr, cp in zip(new_demand, new_trips, caps)]
new_load = [x / mean_load if d > 0 else None for x, d in zip(new_raw, new_demand)]  # SAME scale

upgraded = [i for i, f in enumerate(factor) if f > 1.0]
added_trips = sum(new_trips[i] - base_trips[i] for i in upgraded)
print(f"upgraded {len(upgraded)} routes, +{added_trips:.0f} trips/day")

def kpi(loads):
    vals = [float(l) for l in loads if l is not None]
    over2 = sum(1 for l in vals if l >= 2.0)
    over15 = sum(1 for l in vals if l >= 1.5)
    top10 = sorted(vals, reverse=True)[:max(1, len(vals)//10)]
    return {"over2": over2, "over15": over15, "mean": round(sum(vals)/len(vals), 2),
            "top_decile": round(sum(top10)/len(top10), 2), "max": round(max(vals), 2)}

rows = []
for i, r in routes.iterrows():
    rows.append({
        "name": str(r.route_short_name), "mode": r["mode"],
        "before": round(float(base_load[i]), 2) if base_load[i] is not None else None,
        "after": round(float(new_load[i]), 2) if new_load[i] is not None else None,
        "trips_b": int(base_trips[i]), "trips_a": int(round(new_trips[i])),
        "upgraded": bool(factor[i] > 1.0),
    })
rows_sorted = sorted([r for r in rows if r["before"] is not None], key=lambda x: -x["before"])

# 24h supply/demand curves
st_first = stop_times.sort_values("stop_sequence").groupby("trip_id").first().reset_index()
st_first["h"] = st_first.departure_time.str.split(":").str[0].astype(int)
wt = wtrips.merge(st_first[["trip_id", "h"]], on="trip_id")
wt = wt.merge(routes[["route_id", "mode"]], on="route_id")
hourly = {m: [0]*24 for m in ("bus", "trolleybus", "tram")}
for (m, h), n in wt.groupby(["mode", "h"]).size().items():
    hourly[m][h % 24] += n
HOUR_W = {4:0.3,5:1.2,6:3.5,7:7.5,8:8.5,9:6.5,10:4.5,11:4.0,12:4.2,13:4.4,14:4.6,
          15:5.2,16:6.2,17:8.0,18:8.3,19:6.0,20:4.0,21:2.8,22:1.8,23:1.0,0:0.4,1:0.2,2:0.1,3:0.1}
demand_curve = [HOUR_W.get(h, 0) for h in range(24)]

payload = {
    "kpi_before": kpi(base_load), "kpi_after": kpi(new_load),
    "n_upgraded": len(upgraded), "added_trips": int(round(added_trips)),
    "trigger": LOAD_TRIGGER, "target": LOAD_TARGET,
    "rows": rows_sorted[:25],
    "hourly": hourly, "demand_curve": demand_curve,
}
TEMPLATE = (BASE / "scripts" / "upgrades_template.html").read_text(encoding="utf-8")
OUT.write_text(TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False)), encoding="utf-8")
print("kpi before:", payload["kpi_before"])
print("kpi after:", payload["kpi_after"])
print(f"written {OUT}")

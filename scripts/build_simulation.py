# -*- coding: utf-8 -*-
"""Kyiv surface transport flow simulation — every weekday bus/trolleybus/tram
trip animated along its route shape, colored by modeled crowding.

Vehicle motion: each GTFS trip runs from its first-timepoint departure for its
timepoint-derived duration, at constant speed along the route shape (Kyiv GTFS
publishes ~4 timepoints per trip, so intermediate dwell is not modeled).

Crowding: the O-D gravity demand per route (route_demand.json, built by
build_crowding.py) is spread across the day with an assumed hourly commute
profile and split evenly between that hour's trips. Load = passengers-equivalent
per vehicle / capacity, normalized so the network mean = 1.0 (relative index —
Kyiv publishes no per-route counts to calibrate absolute numbers)."""
import json
import math
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
GTFS = BASE / "data" / "gtfs"
OUT = BASE / "kyiv_bus_simulation.html"

CAPACITY = {"bus": 100, "trolleybus": 110, "tram": 180}
# assumed weekday demand profile (share per hour, 04:00..27:00), commute-peaked
HOUR_W = {
    4: 0.3, 5: 1.2, 6: 3.5, 7: 7.5, 8: 8.5, 9: 6.5, 10: 4.5, 11: 4.0,
    12: 4.2, 13: 4.4, 14: 4.6, 15: 5.2, 16: 6.2, 17: 8.0, 18: 8.3, 19: 6.0,
    20: 4.0, 21: 2.8, 22: 1.8, 23: 1.0, 24: 0.4, 25: 0.2, 26: 0.1, 27: 0.1,
}

MODE = {0: "tram", 3: "bus", 11: "trolleybus", 800: "trolleybus"}

routes = pd.read_csv(GTFS / "routes.txt")
trips = pd.read_csv(GTFS / "trips.txt")
stop_times = pd.read_csv(GTFS / "stop_times.txt")
calendar = pd.read_csv(GTFS / "calendar.txt")
shapes = pd.read_csv(GTFS / "shapes.txt")

routes["mode"] = routes.route_type.map(MODE).fillna("bus")
weekday_services = set(calendar.loc[calendar.monday == 1, "service_id"])
wtrips = trips[trips.service_id.isin(weekday_services)].copy()

st = stop_times.copy()
hh = st.departure_time.str.split(":").str[0].astype(int)
mm = st.departure_time.str.split(":").str[1].astype(int)
ss = st.departure_time.str.split(":").str[2].astype(int)
st["dep_min"] = hh * 60 + mm + ss / 60.0
ag = st.groupby("trip_id").dep_min.agg(["min", "max"]).rename(columns={"min": "start", "max": "end"})
wtrips = wtrips.merge(ag, left_on="trip_id", right_index=True, how="inner")
wtrips["dur"] = wtrips["end"] - wtrips["start"]

# duration sanity: replace implausible durations with the route median
med_dur = wtrips.groupby("route_id").dur.transform("median")
bad = (wtrips.dur < 5) | (wtrips.dur > 180)
print(f"trips with implausible duration: {bad.sum()} of {len(wtrips)} (fixed to route median)")
wtrips.loc[bad, "dur"] = med_dur[bad]
wtrips = wtrips[(wtrips.dur >= 5) & (wtrips.dur <= 180)]

# one shape per route+direction, fall back to route pick
wtrips["shape_id"] = wtrips.shape_id.fillna("")
shapes = shapes.sort_values(["shape_id", "shape_pt_sequence"])
shape_coords = {sid: [[round(a, 5), round(b, 5)] for a, b in zip(g.shape_pt_lat, g.shape_pt_lon)]
                for sid, g in shapes.groupby("shape_id")}

# demand per route from the O-D model
dem = json.loads((BASE / "data" / "route_demand.json").read_text(encoding="utf-8"))["routes"]

route_meta = {}
shape_list, shape_index = [], {}
trips_json = []
wtrips["hour"] = (wtrips.start // 60).astype(int).clip(4, 27)

for rid, g in wtrips.groupby("route_id"):
    r = routes.loc[routes.route_id == rid]
    if r.empty:
        continue
    r = r.iloc[0]
    demand = dem.get(str(rid), {}).get("demand", 0.0)
    wsum = sum(HOUR_W.get(h, 0.1) * n for h, n in g.hour.value_counts().items())
    # passengers-equivalent per trip in hour h: demand * w_h / wsum
    cap = CAPACITY[r["mode"]]
    if rid not in route_meta:
        route_meta[rid] = {"name": str(r.route_short_name), "mode": r["mode"],
                           "cap": cap, "long": r.route_long_name}
    for _, t in g.iterrows():
        sid = t.shape_id
        if sid not in shape_coords or len(shape_coords[sid]) < 2:
            continue
        if sid not in shape_index:
            shape_index[sid] = len(shape_list)
            shape_list.append(shape_coords[sid])
        pax = demand * HOUR_W.get(int(t.hour), 0.1) / wsum if wsum else 0.0
        trips_json.append({
            "r": rid, "s": shape_index[sid],
            "t0": round(t.start, 1), "d": round(t.dur, 1),
            "dir": int(t.direction_id) if pd.notna(t.direction_id) else 0,
            "pax": pax,
        })

# normalize load so mean over trips with demand = 1.0
loads = [tj["pax"] / route_meta[tj["r"]]["cap"] for tj in trips_json if tj["pax"] > 0]
mean_load = sum(loads) / len(loads) if loads else 1.0
for tj in trips_json:
    tj["l"] = round(tj["pax"] / route_meta[tj["r"]]["cap"] / mean_load, 2) if tj["pax"] > 0 else None
    del tj["pax"]

# compact routes: reindex route ids to ints
rid_index = {rid: i for i, rid in enumerate(route_meta)}
routes_out = [None] * len(rid_index)
for rid, m in route_meta.items():
    routes_out[rid_index[rid]] = m
for tj in trips_json:
    tj["r"] = rid_index[tj["r"]]

by_mode = {}
for m in ("bus", "trolleybus", "tram"):
    by_mode[m] = sum(1 for tj in trips_json if routes_out[tj["r"]]["mode"] == m)
stats = {"trips": len(trips_json), "routes": len(routes_out), "by_mode": by_mode,
         "shapes": len(shape_list)}
print(stats)
print(f"trips without demand info: {sum(1 for t in trips_json if t['l'] is None)}")

metro = json.loads((BASE / "data" / "metro_lines.geojson").read_text(encoding="utf-8-sig"))
metro_json = []
for f in metro["features"]:
    if f.get("geometry", {}).get("type") == "LineString":
        c = {"Sviatoshynsko-Brovarska": "#E4222C", "Obolonsko-Teremkivska": "#0F63B6",
             "Syretsko-Pecherska": "#00A651"}.get(f["properties"].get("num_r_eng"), "#888")
        metro_json.append({"c": c, "path": [[round(y, 5), round(x, 5)]
                                            for x, y in f["geometry"]["coordinates"]]})

TEMPLATE = (BASE / "scripts" / "simulation_template.html").read_text(encoding="utf-8")
html = (TEMPLATE
        .replace("__ROUTES__", json.dumps(routes_out, ensure_ascii=False))
        .replace("__SHAPES__", json.dumps(shape_list))
        .replace("__TRIPS__", json.dumps(trips_json))
        .replace("__METRO__", json.dumps(metro_json))
        .replace("__STATS__", json.dumps(stats, ensure_ascii=False)))
OUT.write_text(html, encoding="utf-8")
print(f"written {OUT} ({OUT.stat().st_size/1e6:.1f} MB)")

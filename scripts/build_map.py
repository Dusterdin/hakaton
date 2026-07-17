# -*- coding: utf-8 -*-
"""Kyiv surface transport prototype: parse Kyivpastrans GTFS, compute weekday
service frequency per route and per stop, emit a self-contained Leaflet map."""
import json
import re
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
GTFS = BASE / "data" / "gtfs"
OUT = BASE / "kyiv_bus_network_map.html"

routes = pd.read_csv(GTFS / "routes.txt")
trips = pd.read_csv(GTFS / "trips.txt")
stop_times = pd.read_csv(GTFS / "stop_times.txt")
stops = pd.read_csv(GTFS / "stops.txt")
calendar = pd.read_csv(GTFS / "calendar.txt")
shapes = pd.read_csv(GTFS / "shapes.txt")

print("route_type counts:\n", routes.route_type.value_counts())

MODE = {0: "tram", 3: "bus", 11: "trolleybus", 800: "trolleybus"}
MODE_UA = {"tram": "Трамвай", "bus": "Автобус", "trolleybus": "Тролейбус"}
routes["mode"] = routes.route_type.map(MODE).fillna("bus")

# weekday services (Monday=1)
weekday_services = set(calendar.loc[calendar.monday == 1, "service_id"])
wtrips = trips[trips.service_id.isin(weekday_services)].copy()
print(f"weekday trips: {len(wtrips)} of {len(trips)}")

# departures per trip: first stop_time row
st = stop_times.copy()
st["dep_min"] = (
    st.departure_time.str.split(":").str[0].astype(int) * 60
    + st.departure_time.str.split(":").str[1].astype(int)
)
first_dep = st.sort_values("stop_sequence").groupby("trip_id").first().reset_index()
wtrips = wtrips.merge(first_dep[["trip_id", "dep_min"]], on="trip_id", how="left")

# stops per trip coverage check
per_trip_stops = st.groupby("trip_id").size()
print("stops per trip: median", per_trip_stops.median(), "max", per_trip_stops.max())

# official headway from route_desc, e.g. "Частота відправлення ... 4-9 хв"
def parse_headway(desc):
    if not isinstance(desc, str):
        return None
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*хв", desc)
    return f"{m.group(1)}-{m.group(2)} хв" if m else None

routes["official_headway"] = routes.route_desc.apply(parse_headway)

# per-route weekday stats
grp = wtrips.groupby("route_id")
route_stats = pd.DataFrame(
    {
        "trips_day": grp.size(),
        "first_dep": grp.dep_min.min(),
        "last_dep": grp.dep_min.max(),
        "n_dir": grp.direction_id.nunique(),
    }
).reset_index()
route_stats["span_h"] = (route_stats.last_dep - route_stats.first_dep) / 60.0
# average headway per direction across the service span
route_stats["calc_headway"] = (
    route_stats.span_h * 60.0 / (route_stats.trips_day / route_stats.n_dir.clip(lower=1))
).round(0)

routes = routes.merge(route_stats, on="route_id", how="left")
routes = routes[routes.trips_day.notna()].copy()

# most common shape per route (draw one direction to keep it light)
shape_pick = (
    wtrips.groupby(["route_id", "shape_id"]).size().reset_index(name="n")
    .sort_values("n", ascending=False)
    .drop_duplicates("route_id")[["route_id", "shape_id"]]
)
shapes = shapes.sort_values(["shape_id", "shape_pt_sequence"])
shape_coords = {
    sid: [[round(a, 5), round(b, 5)] for a, b in zip(g.shape_pt_lat, g.shape_pt_lon)]
    for sid, g in shapes.groupby("shape_id")
}

# per-stop weekday departures (timepoints only — GTFS lists ~4 timepoints/trip)
wst = st[st.trip_id.isin(set(wtrips.trip_id))]
stop_deps = wst.groupby("stop_id").size().rename("departures").reset_index()
stop_routes = (
    wst.merge(wtrips[["trip_id", "route_id"]], on="trip_id")
    .merge(routes[["route_id", "route_short_name", "mode"]], on="route_id")
    .groupby("stop_id")
    .agg(routes_list=("route_short_name", lambda s: sorted(set(s), key=str)),
         modes=("mode", lambda s: sorted(set(s))))
    .reset_index()
)
stops_out = stops.merge(stop_deps, on="stop_id").merge(stop_routes, on="stop_id")

routes_json = []
for _, r in routes.iterrows():
    sid = shape_pick.loc[shape_pick.route_id == r.route_id, "shape_id"]
    coords = shape_coords.get(sid.iloc[0]) if len(sid) else None
    if not coords:
        continue
    routes_json.append(
        {
            "name": str(r.route_short_name),
            "long": r.route_long_name,
            "mode": r["mode"],
            "trips": int(r.trips_day),
            "headway": r.official_headway or (f"~{int(r.calc_headway)} хв" if pd.notna(r.calc_headway) else "н/д"),
            "span": f"{int(r.first_dep // 60):02d}:{int(r.first_dep % 60):02d}–{int(r.last_dep // 60):02d}:{int(r.last_dep % 60):02d}",
            "path": coords,
        }
    )

stops_json = [
    {
        "name": s.stop_name,
        "lat": round(s.stop_lat, 5),
        "lon": round(s.stop_lon, 5),
        "deps": int(s.departures),
        "routes": ", ".join(s.routes_list[:12]) + ("…" if len(s.routes_list) > 12 else ""),
        "modes": s.modes,
    }
    for s in stops_out.itertuples()
]

# --- metro + full stops from city GIS API ---
def load_geojson(name):
    return json.loads((BASE / "data" / name).read_text(encoding="utf-8-sig"))

METRO_COLORS = {
    "Sviatoshynsko-Brovarska": "#E4222C",
    "Obolonsko-Teremkivska": "#0F63B6",
    "Syretsko-Pecherska": "#00A651",
}
metro_lines_json = [
    {
        "line": f["properties"].get("num_route", ""),
        "c": METRO_COLORS.get(f["properties"].get("num_r_eng"), "#888"),
        "path": [[round(y, 5), round(x, 5)] for x, y in f["geometry"]["coordinates"]],
    }
    for f in load_geojson("metro_lines.geojson")["features"]
    if f.get("geometry", {}).get("type") == "LineString"
]
metro_stations_json = [
    {
        "name": f["properties"].get("name", ""),
        "line": f["properties"].get("line", ""),
        "c": METRO_COLORS.get(f["properties"].get("line_eng"), "#888"),
        "lat": round(f["geometry"]["coordinates"][1], 5),
        "lon": round(f["geometry"]["coordinates"][0], 5),
    }
    for f in load_geojson("metro_stations.geojson")["features"]
    if f.get("geometry", {}).get("type") == "Point"
]
all_stops_json = [
    {
        "name": f["properties"].get("name", ""),
        "lat": round(f["geometry"]["coordinates"][1], 5),
        "lon": round(f["geometry"]["coordinates"][0], 5),
    }
    for f in load_geojson("stops_all.geojson")["features"]
    if f.get("geometry", {}).get("type") == "Point"
]

stats = {
    "routes": len(routes_json),
    "by_mode": routes.groupby("mode").size().to_dict(),
    "stops": len(all_stops_json),
    "metro": len(metro_stations_json),
    "trips": int(routes.trips_day.sum()),
}
print(stats)

TEMPLATE = (BASE / "scripts" / "map_template.html").read_text(encoding="utf-8")
html = (
    TEMPLATE.replace("__ROUTES__", json.dumps(routes_json, ensure_ascii=False))
    .replace("__STOPS__", json.dumps(stops_json, ensure_ascii=False))
    .replace("__ALLSTOPS__", json.dumps(all_stops_json, ensure_ascii=False))
    .replace("__METROLINES__", json.dumps(metro_lines_json, ensure_ascii=False))
    .replace("__METROSTATIONS__", json.dumps(metro_stations_json, ensure_ascii=False))
    .replace("__STATS__", json.dumps(stats, ensure_ascii=False))
)
OUT.write_text(html, encoding="utf-8")
print(f"written {OUT} ({OUT.stat().st_size/1e6:.1f} MB)")

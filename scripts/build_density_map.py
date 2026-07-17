# -*- coding: utf-8 -*-
"""Kyiv residential density heatmap — the analog of London's residential map.

OSM residential buildings x floor counts give the SHAPE of where people live;
official district populations give the SCALE: each building's weight = floors
(median-imputed when untagged), cells are assigned to the nearest district
centroid, and cell weights are scaled so every district sums to its official
population. Result: estimated residents per ~500 m cell."""
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "kyiv_residential_map.html"

CELL_LAT = 0.0045   # ~500 m
CELL_LON = 0.0070

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
CELL_KM2 = (CELL_LAT * KMLAT) * (CELL_LON * KMLON)

bldgs = json.loads((BASE / "data" / "residential_osm.json").read_text(encoding="utf-8"))
med_lv = {t: statistics.median([b["lv"] for b in bldgs if b["t"] == t and b["lv"]] or [5])
          for t in {b["t"] for b in bldgs}}
print("median levels per tag:", med_lv)

cells = defaultdict(float)
for b in bldgs:
    w = b["lv"] if b["lv"] else med_lv[b["t"]]
    cells[(round(b["lat"] / CELL_LAT), round(b["lon"] / CELL_LON))] += w

# nearest-district assignment (approximate — no district polygons in open data)
def nearest_district(lat, lon):
    return min(range(len(DISTRICTS)),
               key=lambda i: math.hypot((lat - DISTRICTS[i][2]) * KMLAT,
                                        (lon - DISTRICTS[i][3]) * KMLON))

cell_d = {}
d_weight = defaultdict(float)
for c, w in cells.items():
    lat, lon = c[0] * CELL_LAT, c[1] * CELL_LON
    di = nearest_district(lat, lon)
    cell_d[c] = di
    d_weight[di] += w

scale = {i: DISTRICTS[i][1] / d_weight[i] for i in d_weight}
points = []
maxpop = 0.0
for c, w in cells.items():
    lat, lon = c[0] * CELL_LAT, c[1] * CELL_LON
    pop = w * scale[cell_d[c]]
    maxpop = max(maxpop, pop)
    points.append([round(lat, 5), round(lon, 5), round(pop, 1)])

total = sum(p[2] for p in points)
dens_max = maxpop / CELL_KM2
print(f"buildings {len(bldgs)}, cells {len(points)}, total pop {total:,.0f} (calibrated), "
      f"max cell {maxpop:,.0f} (~{dens_max:,.0f}/km2)")

top = sorted(points, key=lambda p: -p[2])[:15]

metro = json.loads((BASE / "data" / "metro_lines.geojson").read_text(encoding="utf-8-sig"))
metro_json = [
    {"c": {"Sviatoshynsko-Brovarska": "#E4222C", "Obolonsko-Teremkivska": "#0F63B6",
           "Syretsko-Pecherska": "#00A651"}.get(f["properties"].get("num_r_eng"), "#888"),
     "path": [[round(y, 5), round(x, 5)] for x, y in f["geometry"]["coordinates"]]}
    for f in metro["features"] if f.get("geometry", {}).get("type") == "LineString"
]

nodes = [{"name": n, "pop": p, "lat": la, "lon": lo} for n, p, la, lo in DISTRICTS]
stats = {"buildings": len(bldgs), "cells": len(points), "total": round(total),
         "cell_km2": round(CELL_KM2, 3), "max_cell": round(maxpop)}

TEMPLATE = (BASE / "scripts" / "density_template.html").read_text(encoding="utf-8")
html = (TEMPLATE
        .replace("__POINTS__", json.dumps(points))
        .replace("__TOP__", json.dumps(top))
        .replace("__NODES__", json.dumps(nodes, ensure_ascii=False))
        .replace("__METRO__", json.dumps(metro_json))
        .replace("__STATS__", json.dumps(stats, ensure_ascii=False)))
OUT.write_text(html, encoding="utf-8")
print(f"written {OUT} ({OUT.stat().st_size/1e6:.1f} MB)")

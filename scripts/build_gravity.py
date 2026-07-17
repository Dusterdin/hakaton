# -*- coding: utf-8 -*-
"""Kyiv gravity flow model — illustrative district-to-district flows.
T_ij = P_i * P_j / d_ij^2, like the London gravity_flow_map.
Populations: official Kyiv city portal (kyivcity.gov.ua, raiony_kyieva page).
"""
import json
import math
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "kyiv_gravity_flow_map.html"

# name, population (kyivcity.gov.ua), approx centroid lat/lon
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


def dist_km(a, b):
    dlat = (a[2] - b[2]) * 111.32
    dlon = (a[3] - b[3]) * 111.32 * math.cos(math.radians(50.45))
    return math.hypot(dlat, dlon)


flows = []
for i in range(len(DISTRICTS)):
    for j in range(i + 1, len(DISTRICTS)):
        a, b = DISTRICTS[i], DISTRICTS[j]
        d = dist_km(a, b)
        t = a[1] * b[1] / (d * d)
        flows.append({"a": a[0], "b": b[0], "pa": [a[2], a[3]], "pb": [b[2], b[3]],
                      "d_km": round(d, 1), "t": t})

tmax = max(f["t"] for f in flows)
for f in flows:
    f["w"] = round(f["t"] / tmax, 4)
flows.sort(key=lambda f: -f["w"])

nodes = [{"name": n, "pop": p, "lat": la, "lon": lo} for n, p, la, lo in DISTRICTS]

# metro lines as context backdrop
metro = json.loads((BASE / "data" / "metro_lines.geojson").read_text(encoding="utf-8-sig"))
METRO_COLORS = {"Sviatoshynsko-Brovarska": "#E4222C", "Obolonsko-Teremkivska": "#0F63B6",
                "Syretsko-Pecherska": "#00A651"}
metro_json = [
    {"c": METRO_COLORS.get(f["properties"].get("num_r_eng"), "#888"),
     "path": [[round(y, 5), round(x, 5)] for x, y in f["geometry"]["coordinates"]]}
    for f in metro["features"] if f.get("geometry", {}).get("type") == "LineString"
]

TEMPLATE = (BASE / "scripts" / "gravity_template.html").read_text(encoding="utf-8")
html = (TEMPLATE
        .replace("__NODES__", json.dumps(nodes, ensure_ascii=False))
        .replace("__FLOWS__", json.dumps(flows, ensure_ascii=False))
        .replace("__METRO__", json.dumps(metro_json, ensure_ascii=False)))
OUT.write_text(html, encoding="utf-8")
print(f"nodes {len(nodes)}, flows {len(flows)}, metro segs {len(metro_json)}")
print(f"written {OUT} ({OUT.stat().st_size/1e3:.0f} KB)")
print("top-5 flows:", [(f['a'], f['b'], f['w']) for f in flows[:5]])

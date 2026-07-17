# -*- coding: utf-8 -*-
"""Fetch Kyiv residential buildings (with floor counts) from Overpass,
querying per-quadrant to stay under rate limits."""
import json
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "residential_osm.json"

S, W, N, E = 50.21, 30.23, 50.63, 30.83
MIDLAT, MIDLON = (S + N) / 2, (W + E) / 2
QUADS = [(S, W, MIDLAT, MIDLON), (S, MIDLON, MIDLAT, E),
         (MIDLAT, W, N, MIDLON), (MIDLAT, MIDLON, N, E)]
TAGS = ["apartments", "dormitory", "residential"]

MIRRORS = ["https://overpass-api.de/api/interpreter",
           "https://overpass.kumi.systems/api/interpreter"]
HDRS = {"User-Agent": "kyiv-transit-prototype/1.0"}

def run(q):
    for attempt in range(4):
        for url in MIRRORS:
            try:
                r = requests.post(url, data={"data": q}, headers=HDRS, timeout=150)
                r.raise_for_status()
                return r.json()["elements"]
            except Exception as e:
                print(f"  {url}: {type(e).__name__}")
        print(f"  retry {attempt+2} in 25s…")
        time.sleep(25)
    return None

bldgs = []
failed = 0
for tag in TAGS:
    for qi, (s, w, n, e) in enumerate(QUADS):
        q = f'[out:json][timeout:90];(way["building"="{tag}"]({s},{w},{n},{e}););out center 40000;'
        got = run(q)
        if got is None:
            print(f"{tag} quad{qi}: FAILED")
            failed += 1
            continue
        print(f"{tag} quad{qi}: {len(got)}")
        for el in got:
            c = el.get("center")
            if not c:
                continue
            t = el.get("tags", {})
            try:
                levels = float(t.get("building:levels", "nan"))
            except ValueError:
                levels = float("nan")
            bldgs.append({"lat": round(c["lat"], 5), "lon": round(c["lon"], 5),
                          "lv": levels if levels == levels and 1 <= levels <= 40 else None,
                          "t": tag})
        time.sleep(8)

OUT.write_text(json.dumps(bldgs), encoding="utf-8")
n_lv = sum(1 for b in bldgs if b["lv"])
print(f"saved {len(bldgs)} buildings ({n_lv} with levels, {failed} failed queries) -> {OUT}")

# -*- coding: utf-8 -*-
"""Retry the destination categories that got rate-limited, merge into the JSON."""
import json
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "destinations_osm.json"
BBOX = "50.21,30.23,50.63,30.83"

RETRY = [
    ("university", f'nwr["amenity"="university"]({BBOX});', 4000),
    ("college", f'nwr["amenity"="college"]({BBOX});', 1200),
    ("attraction", f'nwr["tourism"="attraction"]({BBOX});', 400),
    ("attraction", f'nwr["tourism"="museum"]({BBOX});', 400),
    ("transport_hub", f'nwr["building"="train_station"]({BBOX});', 3000),
]
MIRRORS = ["https://overpass-api.de/api/interpreter",
           "https://overpass.kumi.systems/api/interpreter"]
HDRS = {"User-Agent": "kyiv-transit-prototype/1.0"}

dests = json.loads(OUT.read_text(encoding="utf-8"))
have = {(d["cat"], d["lat"], d["lon"]) for d in dests}

for cat, sub, w in RETRY:
    q = f"[out:json][timeout:90];({sub});out center 8000;"
    got = None
    for attempt in range(3):
        for url in MIRRORS:
            try:
                r = requests.post(url, data={"data": q}, headers=HDRS, timeout=120)
                r.raise_for_status()
                got = r.json()["elements"]
                break
            except Exception as e:
                print(f"  {url}: {type(e).__name__}")
        if got is not None:
            break
        print(f"  waiting 20s before retry {attempt+2}…")
        time.sleep(20)
    print(f"{cat}: {len(got) if got is not None else 'FAILED'}")
    if not got:
        continue
    for e in got:
        tags = e.get("tags", {})
        lat = e.get("lat") or e.get("center", {}).get("lat")
        lon = e.get("lon") or e.get("center", {}).get("lon")
        if lat is None:
            continue
        key = (cat, round(lat, 5), round(lon, 5))
        if key in have:
            continue
        have.add(key)
        dests.append({"name": tags.get("name", tags.get("name:uk", "")) or f"({cat})",
                      "cat": cat, "w": w, "lat": round(lat, 5), "lon": round(lon, 5)})
    time.sleep(10)

OUT.write_text(json.dumps(dests, ensure_ascii=False), encoding="utf-8")
from collections import Counter
print(Counter(d["cat"] for d in dests))
print(f"total {len(dests)}")

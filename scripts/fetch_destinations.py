# -*- coding: utf-8 -*-
"""Fetch Kyiv trip destinations from OpenStreetMap Overpass API,
mirroring the London project's fetch_destinations.py / destinations_osm.csv."""
import json
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "destinations_osm.json"

BBOX = "50.21,30.23,50.63,30.83"  # south,west,north,east — Kyiv
SUBQUERIES = [
    f'nwr["amenity"="university"]({BBOX});',
    f'nwr["amenity"="college"]({BBOX});',
    f'nwr["amenity"="hospital"]({BBOX});',
    f'nwr["shop"="mall"]({BBOX});',
    f'nwr["office"="government"]({BBOX});',
    f'nwr["office"="company"]({BBOX});',
    f'nwr["office"="it"]({BBOX});',
    f'nwr["tourism"="attraction"]({BBOX});',
    f'nwr["tourism"="museum"]({BBOX});',
    f'way["landuse"="industrial"]({BBOX});',
    f'nwr["aeroway"="terminal"]({BBOX});',
    f'nwr["building"="train_station"]({BBOX});',
]

# assumed daily-attraction weights per POI type (labeled in the UI)
WEIGHTS = [
    ("university", lambda t: t.get("amenity") == "university", 4000),
    ("college", lambda t: t.get("amenity") == "college", 1200),
    ("hospital", lambda t: t.get("amenity") == "hospital", 1500),
    ("mall", lambda t: t.get("shop") == "mall", 2500),
    ("office", lambda t: "office" in t, 250),
    ("attraction", lambda t: t.get("tourism") in ("attraction", "museum", "zoo", "theme_park"), 400),
    ("industrial", lambda t: t.get("landuse") == "industrial", 700),
    ("transport_hub", lambda t: t.get("aeroway") == "terminal" or t.get("building") == "train_station", 3000),
]

import time

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HDRS = {"User-Agent": "kyiv-transit-prototype/1.0"}

def run_query(sub):
    q = f"[out:json][timeout:60];({sub});out center 8000;"
    for url in MIRRORS:
        try:
            r = requests.post(url, data={"data": q}, headers=HDRS, timeout=90)
            r.raise_for_status()
            return r.json()["elements"]
        except Exception as e:
            print(f"  {url} failed: {type(e).__name__}")
    return []

elements = []
for sub in SUBQUERIES:
    print("querying:", sub.split("(")[0])
    got = run_query(sub)
    print(f"  -> {len(got)}")
    elements.extend(got)
    time.sleep(2)
print(f"got {len(elements)} elements")

dests = []
for e in elements:
    tags = e.get("tags", {})
    lat = e.get("lat") or e.get("center", {}).get("lat")
    lon = e.get("lon") or e.get("center", {}).get("lon")
    if lat is None:
        continue
    for cat, test, w in WEIGHTS:
        if test(tags):
            dests.append(
                {
                    "name": tags.get("name", tags.get("name:uk", "")) or f"({cat})",
                    "cat": cat,
                    "w": w,
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                }
            )
            break

OUT.write_text(json.dumps(dests, ensure_ascii=False), encoding="utf-8")
from collections import Counter
print(Counter(d["cat"] for d in dests))
print(f"saved {len(dests)} destinations -> {OUT}")

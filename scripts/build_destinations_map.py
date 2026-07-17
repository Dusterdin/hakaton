# -*- coding: utf-8 -*-
"""Map of Kyiv trip destinations (OSM) — 'where people go':
universities, offices, malls, hospitals, industry, attractions, hubs."""
import json
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "kyiv_destinations_map.html"

dests = json.loads((BASE / "data" / "destinations_osm.json").read_text(encoding="utf-8"))
counts = Counter(d["cat"] for d in dests)
print(counts)

stats = {"total": len(dests), "by_cat": dict(counts)}

TEMPLATE = (BASE / "scripts" / "destinations_template.html").read_text(encoding="utf-8")
html = (TEMPLATE
        .replace("__DESTS__", json.dumps(dests, ensure_ascii=False))
        .replace("__STATS__", json.dumps(stats, ensure_ascii=False)))
OUT.write_text(html, encoding="utf-8")
print(f"written {OUT} ({OUT.stat().st_size/1e6:.2f} MB)")

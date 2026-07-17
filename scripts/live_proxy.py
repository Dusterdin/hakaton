# -*- coding: utf-8 -*-
"""Tiny local proxy for the Kyivpastrans GTFS-Realtime feed.
Serves http://localhost:8902/realtime.json (decoded, CORS-open, cached 12 s)
so the live tracker page can poll it from the browser."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

import csv

BASE = Path(__file__).resolve().parent.parent
UPSTREAM = "http://193.23.225.214:732/api/realtime"
PORT = 8902
CACHE_S = 12

MODE = {"0": "tram", "3": "bus", "11": "trolleybus", "800": "trolleybus"}
routes_info = {}
with open(BASE / "data" / "gtfs" / "routes.txt", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        routes_info[row["route_id"]] = {
            "name": row["route_short_name"],
            "long": row["route_long_name"],
            "mode": MODE.get(row["route_type"], "bus"),
        }

_lock = threading.Lock()
_cache = {"at": 0.0, "body": b"{}"}


def fetch():
    r = requests.get(UPSTREAM, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    now = int(time.time())
    out = []
    for e in feed.entity:
        v = e.vehicle
        if not v.position.latitude:
            continue
        info = routes_info.get(v.trip.route_id, {})
        out.append({
            "lat": round(v.position.latitude, 5),
            "lon": round(v.position.longitude, 5),
            "route": info.get("name", v.trip.route_id),
            "long": info.get("long", ""),
            "mode": info.get("mode", "bus"),
            "veh": v.vehicle.label,
            "age": max(0, now - v.timestamp) if v.timestamp else None,
        })
    return json.dumps({"at": now, "vehicles": out}, ensure_ascii=False).encode("utf-8")


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/realtime.json":
            self.send_error(404)
            return
        with _lock:
            if time.time() - _cache["at"] > CACHE_S:
                try:
                    _cache["body"] = fetch()
                    _cache["at"] = time.time()
                except Exception as e:
                    print("fetch failed:", e)
            body = _cache["body"]
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"live proxy on http://localhost:{PORT}/realtime.json (upstream: {UPSTREAM})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

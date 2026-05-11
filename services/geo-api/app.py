"""Resolve stop names to coordinates via Nominatim (OSM)."""

import os
import time

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

NOMINATIM_URL = os.environ.get(
    "NOMINATIM_URL", "https://nominatim.openstreetmap.org"
).rstrip("/")
USER_AGENT = "bus-route-demo-university-course/1.0"
PAUSE_SEC = float(os.environ.get("NOMINATIM_PAUSE_SEC", "1.05"))

_CACHE: dict[str, dict | None] = {}


def geocode_one(query: str) -> dict | None:
    global _CACHE
    key = query.strip().lower()
    if key in _CACHE:
        return _CACHE[key]

    params = {"q": query, "format": "json", "limit": "1"}
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(
            f"{NOMINATIM_URL}/search", params=params, headers=headers, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            _CACHE[key] = None
            return None
        hit = data[0]
        out = {
            "lat": float(hit["lat"]),
            "lon": float(hit["lon"]),
            "display_name": hit.get("display_name"),
        }
        _CACHE[key] = out
        return out
    except (requests.RequestException, KeyError, ValueError, IndexError):
        _CACHE[key] = None
        return None


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/resolve-route")
def resolve_route():
    body = request.get_json(silent=True) or {}
    names = body.get("stops") or []
    if not isinstance(names, list) or not names:
        return jsonify({"error": 'Provide JSON body {"stops": ["…", …]}'}), 400

    points = []
    for q in names:
        if not isinstance(q, str) or not q.strip():
            points.append(None)
            continue
        was_cached = q.strip().lower() in _CACHE
        coord = geocode_one(q.strip())
        points.append(coord)
        if not was_cached:
            time.sleep(PAUSE_SEC)

    return jsonify({"points": points})

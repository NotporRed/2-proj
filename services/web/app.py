"""Gateway: serves static HTML/JS and aggregates bus + geo microservices."""

import os

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

BUS_API_URL = os.environ.get("BUS_API_URL", "http://localhost:5001").rstrip("/")
GEO_API_URL = os.environ.get("GEO_API_URL", "http://localhost:5002").rstrip("/")
GEO_SUFFIX = os.environ.get("GEO_QUERY_SUFFIX", ", Lithuania")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def api_health():
    return jsonify({"status": "ok"})


@app.get("/api/bus/<line_id>/exists")
def bus_exists_proxy(line_id: str):
    try:
        r = requests.get(f"{BUS_API_URL}/lines/{line_id}/exists", timeout=120)
    except requests.RequestException as e:
        return jsonify({"error": "bus-api unavailable", "detail": str(e)}), 502
    return Response(
        r.content,
        status=r.status_code,
        headers={"Content-Type": r.headers.get("Content-Type", "application/json")},
    )


@app.get("/api/bus/<line_id>/preview")
def bus_preview_proxy(line_id: str):
    """Stotelės be geokodavimo (iš GTFS tiesiogiai iš stop_lat/lon arba iš NeTEx vardų)."""
    direction = (request.args.get("direction") or "").strip()
    q = {"direction": direction} if direction else None
    try:
        r = requests.get(f"{BUS_API_URL}/lines/{line_id}/preview", params=q, timeout=300)
    except requests.RequestException as e:
        return jsonify({"error": "bus-api unavailable", "detail": str(e)}), 502
    return Response(
        r.content,
        status=r.status_code,
        headers={"Content-Type": r.headers.get("Content-Type", "application/json")},
    )


@app.get("/api/bus/<line_id>/stops/<stop_id>/timetable")
def bus_stop_timetable_proxy(line_id: str, stop_id: str):
    direction = (request.args.get("direction") or "").strip()
    q = {"direction": direction} if direction else None
    try:
        r = requests.get(
            f"{BUS_API_URL}/lines/{line_id}/stops/{stop_id}/timetable",
            params=q,
            timeout=300,
        )
    except requests.RequestException as e:
        return jsonify({"error": "bus-api unavailable", "detail": str(e)}), 502
    return Response(
        r.content,
        status=r.status_code,
        headers={"Content-Type": r.headers.get("Content-Type", "application/json")},
    )


def _coords_from_stop(s: dict) -> dict | None:
    lat, lon = s.get("lat"), s.get("lon")
    try:
        if lat is None or lon is None:
            return None
        return {"lat": float(lat), "lon": float(lon), "display_name": s.get("name")}
    except (TypeError, ValueError):
        return None


@app.get("/api/bus/<line_id>/route")
def bus_route_geo(line_id: str):
    direction = (request.args.get("direction") or "").strip()
    q = {"direction": direction} if direction else None
    try:
        r = requests.get(f"{BUS_API_URL}/lines/{line_id}/route", params=q, timeout=300)
    except requests.RequestException as e:
        return jsonify({"error": "bus-api unavailable", "detail": str(e)}), 502
    if r.status_code != 200:
        return Response(
            r.content,
            status=r.status_code,
            headers={"Content-Type": r.headers.get("Content-Type", "application/json")},
        )

    payload = r.json()
    raw_stops = payload.get("stops") or []

    shape_path = payload.get("shape_path")
    if isinstance(shape_path, list) and len(shape_path) >= 2:
        merged = []
        for stop in raw_stops:
            pt = _coords_from_stop(stop)
            merged.append(
                {
                    "order": stop.get("order"),
                    "name": stop.get("name"),
                    "stop_id": stop.get("stop_id"),
                    "coord": pt,
                }
            )
        poly = []
        for p in shape_path:
            try:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    poly.append([float(p[0]), float(p[1])])
            except (TypeError, ValueError):
                continue
        if len(poly) >= 2:
            return jsonify(
                {
                    "line": payload.get("line"),
                    "line_name": payload.get("line_name"),
                    "transport_mode": payload.get("transport_mode"),
                    "source_url": payload.get("source_url"),
                    "data_source": payload.get("data_source"),
                    "direction_id": payload.get("direction_id"),
                    "available_directions": payload.get("available_directions") or [],
                    "can_flip": bool(payload.get("can_flip")),
                    "stops": merged,
                    "path": poly,
                }
            )

    geo_queries: list[str] = []
    for stop in raw_stops:
        if _coords_from_stop(stop):
            continue
        name = (stop.get("name") or "").strip()
        geo_queries.append(name + GEO_SUFFIX if name else "")

    geo_points: list[dict | None] = []
    if geo_queries:
        geo_timeout = 30 + int(
            len(geo_queries) * float(os.environ.get("GEO_POLL_SEC_PER_STOP", "2.5"))
        )
        gr = requests.post(
            f"{GEO_API_URL}/resolve-route",
            json={"stops": geo_queries},
            timeout=max(geo_timeout, 180),
            headers={"Content-Type": "application/json"},
        )
        if gr.status_code != 200:
            return jsonify(
                {
                    "line": payload.get("line"),
                    "line_name": payload.get("line_name"),
                    "stops": raw_stops,
                    "geo_error": gr.text,
                }
            ), 502
        geo_points = gr.json().get("points") or []

    merged = []
    geo_iter = iter(geo_points)
    for stop in raw_stops:
        pt = _coords_from_stop(stop)
        if pt is None:
            pt = next(geo_iter, None)
        merged.append(
            {
                "order": stop.get("order"),
                "name": stop.get("name"),
                "stop_id": stop.get("stop_id"),
                "coord": pt,
            }
        )

    path: list[list[float]] = []
    for m in merged:
        c = m.get("coord")
        if c and "lat" in c and "lon" in c:
            path.append([c["lat"], c["lon"]])

    return jsonify(
        {
            "line": payload.get("line"),
            "line_name": payload.get("line_name"),
            "transport_mode": payload.get("transport_mode"),
            "source_url": payload.get("source_url"),
            "data_source": payload.get("data_source"),
            "direction_id": payload.get("direction_id"),
            "available_directions": payload.get("available_directions") or [],
            "can_flip": bool(payload.get("can_flip")),
            "stops": merged,
            "path": path,
        }
    )

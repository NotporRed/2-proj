"""Bus routes: GTFS ZIP (prioritetas) arba Visų maršrutų NeTEx."""

import os
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

_GTFS_ZIP_RAW = os.environ.get("GTFS_ZIP_PATH", "").strip()
_GTFS_ZIP = Path(_GTFS_ZIP_RAW) if _GTFS_ZIP_RAW else Path("")


def _use_gtfs() -> bool:
    return bool(_GTFS_ZIP_RAW) and _GTFS_ZIP.is_file()


def _serialize_payload(data: dict) -> dict:
    stops = data.get("stops") or []
    return {
        "line": data.get("line"),
        "line_name": data.get("line_name"),
        "transport_mode": data.get("transport_mode"),
        "source_url": data.get("source_url"),
        "data_source": data.get("data_source"),
        "route_id": data.get("route_id"),
        "trip_id": data.get("trip_id"),
        "shape_id": data.get("shape_id"),
        "direction_id": data.get("direction_id"),
        "available_directions": data.get("available_directions") or [],
        "can_flip": bool(data.get("can_flip")),
        "shape_path": data.get("shape_path"),
        "stops": [
            {
                "order": s.get("order"),
                "name": s.get("name"),
                "lat": s.get("lat"),
                "lon": s.get("lon"),
                "stop_id": s.get("stop_id"),
                "quay_ref": s.get("quay_ref"),
            }
            for s in stops
        ],
    }


@app.errorhandler(Exception)
def handle_unexpected_error(err: Exception):
    return jsonify({"error": "Internal bus-api error", "detail": str(err)}), 500


@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "backend": "gtfs" if _use_gtfs() else "netex",
            "gtfs_zip_path": _GTFS_ZIP_RAW or None,
            "gtfs_zip_exists": _GTFS_ZIP.is_file() if _GTFS_ZIP_RAW else False,
        }
    )


@app.get("/lines/<line_id>/exists")
def line_exists(line_id: str):
    if _GTFS_ZIP_RAW and not _GTFS_ZIP.is_file():
        return (
            jsonify(
                {
                    "error": "GTFS ZIP path configured but file is not mounted",
                    "gtfs_zip_path": _GTFS_ZIP_RAW,
                    "exists": False,
                    "line": line_id,
                }
            ),
            500,
        )
    if _use_gtfs():
        from gtfs_loader import gtfs_route_exists

        ok, display = gtfs_route_exists(line_id)
    else:
        from netex_loader import route_exists

        ok, display = route_exists(line_id)

    if not ok:
        return jsonify({"exists": False, "line": line_id}), 404
    return jsonify({"exists": True, "line": display})


def _route_data(line_id: str) -> dict | None:
    if _GTFS_ZIP_RAW and not _GTFS_ZIP.is_file():
        return {
            "error": "GTFS ZIP path configured but file is not mounted",
            "gtfs_zip_path": _GTFS_ZIP_RAW,
            "line": line_id,
        }
    if _use_gtfs():
        from gtfs_loader import gtfs_route_detail

        return gtfs_route_detail(line_id)
    from netex_loader import route_for_line

    return route_for_line(line_id)


@app.get("/lines/<line_id>/preview")
def line_preview(line_id: str):
    """Visos stotelės iš šaltinio; be papildomo geokodavimo (žemėlapiui tik patvirtinus)."""
    direction = request.args.get("direction")
    try:
        if _use_gtfs():
            from gtfs_loader import gtfs_route_detail

            data = gtfs_route_detail(line_id, direction)
        else:
            data = _route_data(line_id)
    except Exception as e:
        return jsonify({"error": "Failed to load preview", "detail": str(e)}), 500
    if data is None:
        return jsonify({"error": "Line not found", "line": line_id}), 404
    if data.get("error"):
        return jsonify(data), 500
    return jsonify(_serialize_payload(data))


@app.get("/lines/<line_id>/route")
def line_route(line_id: str):
    direction = request.args.get("direction")
    try:
        if _use_gtfs():
            from gtfs_loader import gtfs_route_detail

            data = gtfs_route_detail(line_id, direction)
        else:
            data = _route_data(line_id)
    except Exception as e:
        return jsonify({"error": "Failed to load route", "detail": str(e)}), 500
    if data is None:
        return jsonify({"error": "Line not found", "line": line_id}), 404
    if data.get("error"):
        return jsonify(data), 500
    return jsonify(_serialize_payload(data))


@app.get("/lines/<line_id>/stops/<stop_id>/timetable")
def line_stop_timetable(line_id: str, stop_id: str):
    if not _use_gtfs():
        return (
            jsonify(
                {
                    "error": "Timetable by stop is available only in GTFS mode",
                    "line": line_id,
                    "stop_id": stop_id,
                }
            ),
            400,
        )
    from gtfs_loader import gtfs_stop_timetable

    direction = request.args.get("direction")
    data = gtfs_stop_timetable(line_id, stop_id, direction)
    if data is None:
        return jsonify({"error": "Line or stop not found", "line": line_id, "stop_id": stop_id}), 404
    if data.get("error"):
        return jsonify(data), 500
    return jsonify(data)

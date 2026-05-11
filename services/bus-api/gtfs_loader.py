"""Load static GTFS from a ZIP (routes, trips, stop_times, stops, shapes)."""

from __future__ import annotations

import csv
import os
import threading
import zipfile
from collections import defaultdict
from io import TextIOWrapper
from pathlib import Path
from typing import Any

_zip_path = Path(os.environ.get("GTFS_ZIP_PATH", "").strip()).resolve()
_loaded: dict[str, Any] | None = None
_lock = threading.Lock()


def _csv_rows(zip_ref: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with zip_ref.open(name, "r") as raw:
        tr = TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        return list(csv.DictReader(tr))


def _norm_key(val: str) -> str:
    return "".join(str(val).strip().upper().replace(" ", ""))


def _is_trolley_route_id(route_id: str) -> bool:
    rid = (route_id or "").lower()
    return "_trol_" in rid or "trolley" in rid


def ensure_loaded() -> None:
    global _loaded
    if _loaded is not None:
        return
    if not _zip_path.is_file():
        raise FileNotFoundError(f"GTFS ZIP not found: {_zip_path}")
    with _lock:
        if _loaded is not None:
            return
        _loaded = _load_gtfs_zip(_zip_path)


def _load_gtfs_zip(zip_path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            routes_rows = _csv_rows(z, "routes.txt")
            trips_rows = _csv_rows(z, "trips.txt")
            stop_times_rows = _csv_rows(z, "stop_times.txt")
            stops_rows = _csv_rows(z, "stops.txt")
            try:
                shape_rows = _csv_rows(z, "shapes.txt")
            except KeyError:
                shape_rows = []
    except (KeyError, OSError, zipfile.BadZipFile) as e:
        return {"load_error": str(e)}

    stops_by_id: dict[str, dict[str, str]] = {
        row["stop_id"]: row for row in stops_rows if row.get("stop_id")
    }

    trip_stop_count: dict[str, int] = defaultdict(int)
    for row in stop_times_rows:
        tid = (row.get("trip_id") or "").strip()
        if tid:
            trip_stop_count[tid] += 1

    shape_points: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for row in shape_rows:
        sid = (row.get("shape_id") or "").strip()
        if not sid:
            continue
        try:
            seq = int(row.get("shape_pt_sequence") or 0)
            lat = float(row["shape_pt_lat"])
            lon = float(row["shape_pt_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        shape_points[sid].append((seq, lat, lon))
    shape_paths: dict[str, list[list[float]]] = {}
    for sid, pts in shape_points.items():
        pts.sort(key=lambda t: t[0])
        shape_paths[sid] = [[lat, lon] for _, lat, lon in pts]

    # route_short_name variants -> route_id
    aliases: dict[str, set[str]] = defaultdict(set)
    route_meta: dict[str, tuple[str, str]] = {}
    route_is_trolley: dict[str, bool] = {}
    trips_by_route: dict[str, list[str]] = defaultdict(list)
    trip_index: dict[str, dict[str, str]] = {}

    for r in routes_rows:
        rid = (r.get("route_id") or "").strip()
        if not rid:
            continue
        rs = (r.get("route_short_name") or "").strip()
        rn = (r.get("route_long_name") or "").strip()
        route_meta[rid] = (rs, rn)
        route_is_trolley[rid] = _is_trolley_route_id(rid)
        if rs:
            aliases[_norm_key(rs)].add(rid)
            if "".join(rs.split()).isdigit():
                aliases[str(int("".join(rs.split())))].add(rid)

    for t in trips_rows:
        tid = (t.get("trip_id") or "").strip()
        rid = (t.get("route_id") or "").strip()
        if not tid or not rid:
            continue
        trip_index[tid] = dict(t)
        trips_by_route[rid].append(tid)

    return {
        "aliases": dict(aliases),
        "route_meta": route_meta,
        "route_is_trolley": route_is_trolley,
        "trips_by_route": dict(trips_by_route),
        "trip_index": trip_index,
        "trip_stop_count": dict(trip_stop_count),
        "stop_times_rows": stop_times_rows,
        "stops_by_id": stops_by_id,
        "shape_paths": shape_paths,
    }


def _candidate_route_ids(user_raw: str) -> set[str]:
    global _loaded
    assert _loaded is not None and "aliases" in _loaded
    u = user_raw.strip()
    if not u:
        return set()
    want_trolley = False
    # Input style: T10, t19, T 3 => force trolleybus route selection.
    compact_u = "".join(u.split()).upper()
    if compact_u.startswith("T") and len(compact_u) > 1:
        want_trolley = True
        u = compact_u[1:]

    aliases = _loaded["aliases"]
    keys = {_norm_key(u)}
    compact = "".join(u.split())
    if compact.isdigit():
        keys.add(str(int(compact)))
    out: set[str] = set()
    for k in keys:
        out |= aliases.get(k, set())
    if want_trolley:
        trolley_map = _loaded.get("route_is_trolley", {})
        out = {rid for rid in out if trolley_map.get(rid, False)}
    return out


def _trip_variant_label(trip_meta: dict[str, str]) -> str:
    d = (trip_meta.get("direction_id") or "").strip()
    if d != "":
        return "dir:" + d
    h = (trip_meta.get("trip_headsign") or "").strip()
    if h:
        return "head:" + h
    return "default"


def _route_direction_code_map(data: dict[str, Any], route_id: str) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for tid in data["trips_by_route"].get(route_id, ()):
        if data["trip_stop_count"].get(tid, 0) < 1:
            continue
        trip_meta = data["trip_index"].get(tid, {})
        grouped[_trip_variant_label(trip_meta)].append(tid)
    labels = sorted(grouped.keys())
    coded: dict[str, list[str]] = {}
    for idx, lbl in enumerate(labels):
        coded[str(idx)] = grouped[lbl]
    return coded


def _pick_best_trip(
    data: dict[str, Any], cand_routes: set[str], preferred_direction: str | None = None
) -> tuple[str, str, str, str | None] | None:
    best: tuple[int, int, str, str, str] | None = None
    best_direction: str | None = None
    # (stop_count, has_shape 0/1, trip_id, route_id, shape_id)
    for rid in cand_routes:
        code_map = _route_direction_code_map(data, rid)
        allowed_trips: set[str] | None = None
        if preferred_direction is not None and preferred_direction != "":
            tids = code_map.get(preferred_direction)
            if tids:
                allowed_trips = set(tids)
            else:
                continue
        for tid in data["trips_by_route"].get(rid, ()):
            if allowed_trips is not None and tid not in allowed_trips:
                continue
            c = data["trip_stop_count"].get(tid, 0)
            if c < 1:
                continue
            trip_meta = data["trip_index"].get(tid, {})
            shp = (trip_meta.get("shape_id") or "").strip()
            key = (c, 1 if shp else 0)
            if best is None or key > (best[0], best[1]):
                best = (c, 1 if shp else 0, tid, rid, shp)
                for code, tids in code_map.items():
                    if tid in tids:
                        best_direction = code
                        break
    if best is None:
        return None
    return (best[2], best[3], best[4], best_direction)


def gtfs_route_exists(user_raw: str) -> tuple[bool, str | None]:
    try:
        ensure_loaded()
    except FileNotFoundError:
        return False, None
    global _loaded
    if _loaded is None or _loaded.get("load_error"):
        return False, None
    rids = _candidate_route_ids(user_raw)
    if not rids:
        return False, None
    rid0 = next(iter(rids))
    short, _ = _loaded["route_meta"].get(rid0, ("", ""))
    return True, short or user_raw.strip()


def gtfs_route_detail(user_raw: str, preferred_direction: str | None = None) -> dict[str, Any] | None:
    try:
        ensure_loaded()
    except FileNotFoundError:
        return None
    global _loaded
    if _loaded is None:
        return None
    err = _loaded.get("load_error")
    if err:
        return {"error": str(err), "line": user_raw.strip()}

    cand_routes = _candidate_route_ids(user_raw)
    if not cand_routes:
        return None

    data = _loaded
    picked = _pick_best_trip(data, cand_routes, preferred_direction)
    if picked is None and preferred_direction is not None:
        picked = _pick_best_trip(data, cand_routes, None)
    if picked is None:
        return None
    best_trip, best_route, shape_id_pick, selected_direction = picked

    rows = [
        dict(r)
        for r in data["stop_times_rows"]
        if (r.get("trip_id") or "").strip() == best_trip
    ]
    rows.sort(key=lambda x: int(x.get("stop_sequence") or 0))

    stops_out: list[dict[str, Any]] = []
    for i, row in enumerate(rows, start=1):
        sid = (row.get("stop_id") or "").strip()
        meta = data["stops_by_id"].get(sid, {})
        name = (meta.get("stop_name") or meta.get("stop_desc") or sid).strip()
        lat_s = meta.get("stop_lat") or ""
        lon_s = meta.get("stop_lon") or ""
        lat_f = lon_f = None
        try:
            if lat_s and lon_s:
                lat_f = float(lat_s)
                lon_f = float(lon_s)
        except ValueError:
            pass
        stops_out.append(
            {
                "order": i,
                "name": name,
                "stop_id": sid,
                "lat": lat_f,
                "lon": lon_f,
            }
        )

    rshort, rlong = data["route_meta"].get(best_route, ("", ""))
    directions = sorted(_route_direction_code_map(data, best_route).keys())
    shape_poly = data["shape_paths"].get(shape_id_pick) if shape_id_pick else None
    if shape_poly and len(shape_poly) >= 2:
        map_path = shape_poly
    else:
        map_path = []
        for s in stops_out:
            if s["lat"] is not None and s["lon"] is not None:
                map_path.append([s["lat"], s["lon"]])

    return {
        "line": rshort or user_raw.strip(),
        "line_name": rlong,
        "transport_mode": "bus",
        "route_id": best_route,
        "trip_id": best_trip,
        "shape_id": shape_id_pick or None,
        "direction_id": selected_direction,
        "available_directions": directions,
        "can_flip": len(directions) >= 2,
        "data_source": "gtfs",
        "stops": stops_out,
        "shape_path": map_path if len(map_path) >= 2 else None,
    }


def _time_to_seconds(raw: str) -> int:
    parts = (raw or "").strip().split(":")
    if len(parts) != 3:
        return 10**9
    try:
        hh, mm, ss = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return 10**9
    return hh * 3600 + mm * 60 + ss


def gtfs_stop_timetable(
    user_raw: str, stop_id: str, preferred_direction: str | None = None
) -> dict[str, Any] | None:
    try:
        ensure_loaded()
    except FileNotFoundError:
        return None
    global _loaded
    if _loaded is None:
        return None
    err = _loaded.get("load_error")
    if err:
        return {"error": str(err), "line": user_raw.strip(), "stop_id": stop_id}

    sid = (stop_id or "").strip()
    if not sid:
        return {"error": "Missing stop_id", "line": user_raw.strip()}

    cand_routes = _candidate_route_ids(user_raw)
    if not cand_routes:
        return None

    data = _loaded
    picked = _pick_best_trip(data, cand_routes, preferred_direction)
    if picked is None and preferred_direction is not None:
        picked = _pick_best_trip(data, cand_routes, None)
    if picked is None:
        return None
    _trip, best_route, _shape, selected_direction = picked

    route_trip_ids = set(data["trips_by_route"].get(best_route, ()))
    if not route_trip_ids:
        return None

    dep_rows: list[dict[str, str]] = []
    for row in data["stop_times_rows"]:
        if (row.get("stop_id") or "").strip() != sid:
            continue
        tid = (row.get("trip_id") or "").strip()
        if tid not in route_trip_ids:
            continue
        dep = (row.get("departure_time") or row.get("arrival_time") or "").strip()
        if not dep:
            continue
        trip = data["trip_index"].get(tid, {})
        dep_rows.append(
            {
                "trip_id": tid,
                "departure_time": dep,
                "arrival_time": (row.get("arrival_time") or "").strip() or dep,
                "headsign": (trip.get("trip_headsign") or "").strip(),
                "service_id": (trip.get("service_id") or "").strip(),
            }
        )

    dep_rows.sort(key=lambda x: (_time_to_seconds(x["departure_time"]), x["trip_id"]))

    stop_meta = data["stops_by_id"].get(sid, {})
    stop_name = (stop_meta.get("stop_name") or stop_meta.get("stop_desc") or sid).strip()
    short, long_name = data["route_meta"].get(best_route, ("", ""))

    return {
        "line": short or user_raw.strip(),
        "line_name": long_name,
        "route_id": best_route,
        "direction_id": selected_direction,
        "stop_id": sid,
        "stop_name": stop_name,
        "departures": dep_rows,
        "data_source": "gtfs",
    }

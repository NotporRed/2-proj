"""Load NeTEx ZIPs from https://www.visimarsrutai.lt/netex/ (Visų maršrutai)."""

from __future__ import annotations

import os
import re
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

NS = "{http://www.netex.org.uk/netex}"

FILENAME_LINE = re.compile(
    r"^VTR-Line-(.+?)-(bus|trolleyBus)\.xml$", re.IGNORECASE
)

_download_lock = threading.Lock()
_initialized = False
_quay_coords: dict[str, tuple[float, float]] | None = None
_line_files: dict[str, list[Path]] | None = None

DATA_DIR = Path(os.environ.get("NETEX_DATA_DIR", str(Path(__file__).resolve().parent / "netex_cache")))
DEFAULT_PRIMARY_ZIP = "https://www.visimarsrutai.lt/netex/Vilniausm.zip"
DEFAULT_EXTRA_ZIPS = ("https://www.visimarsrutai.lt/netex/LTSA.zip",)


def _q(tag: str) -> str:
    return f"{NS}{tag}"


def _local(tag: str) -> str:
    return tag.partition("}")[2] if tag.startswith("{") else tag


def _norm_code(s: str) -> str:
    return "".join(s.upper().replace(" ", "").split())


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(
        url,
        headers={
            "User-Agent": "BusRouteStudyProject/1.0 (+https://www.visimarsrutai.lt/netex/)"
        },
    )
    with urlopen(req, timeout=180) as resp:
        dest.write_bytes(resp.read())


def _extract_zip(zip_path: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(into)


def _coords_from_quay_element(quay_el: ET.Element) -> tuple[float, float] | None:
    for gp in quay_el.iter():
        if _local(gp.tag) != "Centroid":
            continue
        for loc in gp.iter():
            if _local(loc.tag) != "Location":
                continue
            lat_el = loc.find(_q("Latitude"))
            lon_el = loc.find(_q("Longitude"))
            if lat_el is None or lon_el is None or not lat_el.text or not lon_el.text:
                continue
            try:
                return (float(lat_el.text.strip()), float(lon_el.text.strip()))
            except ValueError:
                return None
    return None


def _index_quays_from_tree(tree: ET.ElementTree) -> dict[str, tuple[float, float]]:
    coords: dict[str, tuple[float, float]] = {}
    for elem in tree.iter():
        if _local(elem.tag) != "Quay":
            continue
        qid = elem.get("id")
        if not qid:
            continue
        c = _coords_from_quay_element(elem)
        if c is not None and qid not in coords:
            coords[qid] = c
    return coords


def _merge_stops_from_dir(root_dir: Path, into: dict[str, tuple[float, float]]) -> None:
    for stops_path in root_dir.rglob("_stops.xml"):
        try:
            tree = ET.parse(stops_path)
        except ET.ParseError:
            continue
        into.update(_index_quays_from_tree(tree))


def _pick_line_variant(paths: list[Path]) -> Path:
    vilnius = [p for p in paths if "vilniausm" in str(p).lower()]
    pool = vilnius if vilnius else paths
    return sorted(pool, key=lambda p: ("trolley" in p.name.lower(), p.name))[0]


def _lines_root_index(lines_root: Path) -> dict[str, list[Path]]:
    idx: dict[str, list[Path]] = {}
    for p in lines_root.rglob("VTR-Line-*.xml"):
        if p.name.startswith("."):
            continue
        if "validation" in p.name.lower():
            continue
        m = FILENAME_LINE.match(p.name)
        if not m:
            continue
        idx.setdefault(_norm_code(m.group(1)), []).append(p)
    return idx


def ensure_dataset() -> None:
    global _initialized, _quay_coords, _line_files
    if _initialized:
        return
    with _download_lock:
        if _initialized:
            return

        urls: list[str] = []
        primary = os.environ.get("NETEX_ZIP_URL", DEFAULT_PRIMARY_ZIP).strip()
        if primary:
            urls.append(primary)
        extra = os.environ.get("NETEX_EXTRA_ZIP_URLS", "")
        if extra.strip():
            urls.extend(u.strip() for u in extra.split(",") if u.strip())
        else:
            urls.extend(DEFAULT_EXTRA_ZIPS)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        extract_root = DATA_DIR / "extracted"
        extract_root.mkdir(parents=True, exist_ok=True)

        coords: dict[str, tuple[float, float]] = {}
        seen_urls: list[str] = []
        for url in urls:
            if url in seen_urls:
                continue
            seen_urls.append(url)
            name = Path(url.split("?")[0]).name or "bundle.zip"
            slug = Path(name).stem.replace(" ", "_")
            dest_zip = DATA_DIR / name
            if not dest_zip.is_file():
                _download(url, dest_zip)

            target = extract_root / slug
            marker = target / ".extracted_marker"
            if not marker.is_file():
                if target.exists():
                    for old in target.iterdir():
                        if old.name != ".extracted_marker":
                            if old.is_dir():
                                shutil.rmtree(old)
                            else:
                                old.unlink()
                else:
                    target.mkdir(parents=True, exist_ok=True)
                _extract_zip(dest_zip, target)
                marker.write_text("ok", encoding="utf-8")

            _merge_stops_from_dir(target, coords)

        _line_files = _lines_root_index(extract_root)
        _quay_coords = coords
        _initialized = True


def _passenger_stop_to_quay(root: ET.Element) -> dict[str, str]:
    m: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) != "PassengerStopAssignment":
            continue
        ssp = el.find(_q("ScheduledStopPointRef"))
        quay = el.find(_q("QuayRef"))
        if ssp is None or quay is None:
            continue
        sid = (ssp.get("ref") or "").strip()
        qref = (quay.get("ref") or "").strip()
        if sid and qref and sid not in m:
            m[sid] = qref
    return m


def _route_points_to_ssp(root: ET.Element) -> dict[str, str]:
    rp_to_ssp: dict[str, str] = {}
    for rp in root.iter():
        if _local(rp.tag) != "RoutePoint":
            continue
        rid = rp.get("id")
        if not rid:
            continue
        projections = rp.find(_q("projections"))
        if projections is None:
            continue
        projected = None
        for proj in projections:
            if _local(proj.tag) != "PointProjection":
                continue
            for sub in proj:
                if _local(sub.tag) == "ProjectedPointRef":
                    projected = sub
                    break
            if projected is not None:
                break
        if projected is None:
            continue
        ref = (projected.get("ref") or "").strip()
        if ref:
            rp_to_ssp[rid] = ref
    return rp_to_ssp


def _scheduled_stop_names(root: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) != "ScheduledStopPoint":
            continue
        sid = el.get("id")
        nm = el.find(_q("Name"))
        if sid and nm is not None and nm.text and sid not in out:
            out[sid] = nm.text.strip()
    return out


def _route_sequence_length(route_el: ET.Element) -> int:
    seq_el = route_el.find(_q("pointsInSequence"))
    if seq_el is None:
        return 0
    return sum(1 for por in seq_el if _local(por.tag) == "PointOnRoute")


def _parse_line_route(xml_path: Path) -> dict[str, Any]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    passenger = _passenger_stop_to_quay(root)
    rp_to_ssp = _route_points_to_ssp(root)
    names = _scheduled_stop_names(root)

    routes = [el for el in root.iter() if _local(el.tag) == "Route"]
    if not routes:
        return {"error": "Maršrutas XML faile nerastas (Route)", "path": str(xml_path)}

    routes.sort(key=_route_sequence_length, reverse=True)
    seq_el = routes[0].find(_q("pointsInSequence"))
    if seq_el is None:
        return {"error": "Nėra pointsInSequence", "path": str(xml_path)}

    triples: list[tuple[int, int, str]] = []
    for doc_idx, por in enumerate(seq_el):
        if _local(por.tag) != "PointOnRoute":
            continue
        ord_attr = por.get("order")
        rpref = por.find(_q("RoutePointRef"))
        if ord_attr is None or rpref is None:
            continue
        try:
            order_val = int(ord_attr.strip())
        except ValueError:
            continue
        rpid = (rpref.get("ref") or "").strip()
        ssp = rp_to_ssp.get(rpid)
        if not ssp:
            continue
        triples.append((order_val, doc_idx, ssp))

    triples.sort(key=lambda t: (t[0], t[1]))
    by_order: dict[int, str] = {}
    for order_val, _doc_idx, ssp in triples:
        by_order.setdefault(order_val, ssp)

    public_code = ""
    line_name = ""
    for el in root.iter():
        if _local(el.tag) != "Line":
            continue
        pc = el.find(_q("PublicCode"))
        nm_el = el.find(_q("Name"))
        if pc is not None and pc.text:
            public_code = pc.text.strip()
        if nm_el is not None and nm_el.text:
            line_name = nm_el.text.strip()
        break

    stops: list[dict[str, Any]] = []
    quay_coords = _quay_coords or {}
    m = FILENAME_LINE.match(xml_path.name)
    mode = m.group(2) if m else ""

    for i, order_key in enumerate(sorted(by_order.keys()), start=1):
        ssp = by_order[order_key]
        nm = names.get(ssp, ssp.split(":")[-1])
        qref = passenger.get(ssp)
        lat: float | None = None
        lon: float | None = None
        if qref and qref in quay_coords:
            lat, lon = quay_coords[qref]

        stops.append(
            {
                "order": i,
                "name": nm,
                "scheduled_stop_ref": ssp,
                "quay_ref": qref,
                "lat": lat,
                "lon": lon,
            }
        )

    if not stops:
        return {
            "error": "Nepavyko išparsinti stočių sekų iš pasirinkto Route maršruto.",
            "path": str(xml_path),
        }

    return {
        "line": public_code,
        "line_name": line_name,
        "transport_mode": mode,
        "stops": stops,
        "xml_file": xml_path.name,
    }


def route_for_line(raw_code: str) -> dict[str, Any] | None:
    ensure_dataset()
    assert _line_files is not None
    key = _norm_code(raw_code)
    paths = _line_files.get(key)
    if paths is None:
        return None
    data = _parse_line_route(_pick_line_variant(paths))
    data["source_url"] = "https://www.visimarsrutai.lt/netex/"
    return data


def route_exists(raw_code: str) -> tuple[bool, str | None]:
    ensure_dataset()
    assert _line_files is not None
    key = _norm_code(raw_code)
    paths = _line_files.get(key)
    if not paths:
        return False, None
    m = FILENAME_LINE.match(_pick_line_variant(paths).name)
    display = m.group(1) if m else raw_code.strip()
    return True, display

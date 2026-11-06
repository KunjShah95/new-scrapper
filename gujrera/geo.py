"""
District-wide scheme-location harvest.

Every GujRERA-registered project in a district, with its **exact boundary** (the
polygon the promoter drew on the portal's map) reduced to a centroid + bounding
box, plus the complete public detail set (promoter, type, status, cost, dates,
land areas, approving authority, survey numbers, ...).

Where the data comes from
-------------------------
| Source | Endpoint | Gives |
|---|---|---|
| discovery | `project_reg/public/global-search` | every project whose registration number carries the district prefix |
| boundary  | `maplocation/public/getProjectMapView` | polygons for a whole district in a handful of batched POSTs |
| details   | `project_reg/public/alldatabyprojectid/{pid}` | registration number, promoter, approval date |
| details   | `project_reg/public/getproject-details/{pid}` | address, pinCode, sub-district, dates, land areas, authority |
| details   | `maplocation/public/getProjectLocations/{pid}` | published address, project status, declared cost |

Projects already scraped by `run.py` are read from their local `raw/` folder, so
a re-run costs no requests for those.

Output layout (`output/geo/`)
-----------------------------
    cache/discovery.json            raw search rows per project
    cache/locations.json            raw map-view rows (polygons)
    cache/details/{pid}.json        trimmed detail payload per project
    schemes.json                    the flat dataset the map is built from
    schemes.csv                     one row per scheme, scalars only
    schemes.geojson                 centroids (all properties)
    boundaries.geojson              project boundary polygons
    summary.json                    counts + coverage, for the map header
    <district>.json                per-district slice

The map page itself is produced by `build_map.py`.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .api import GujreraClient, payload
from .report import inventory_summary

log = logging.getLogger("gujrera.geo")

# --------------------------------------------------------------------------------------
# Districts. `reg_prefix` is the registration-number head GujRERA uses per district, so a
# single search for it enumerates the district (the portal's text search matches regNo).
# `hints` are belt-and-braces queries: anything they surface that the prefix search missed
# is counted as a recovery, which is how completeness stays measurable.
# --------------------------------------------------------------------------------------
DISTRICTS: Dict[str, Dict[str, Any]] = {
    "ahmedabad": {
        "label": "Ahmedabad",
        "code": "AHME",
        "reg_prefix": "PR/GJ/AHMEDABAD",
        "hints": [
            "Ahmedabad City", "Daskroi", "Sanand", "Dholka", "Bavla", "Viramgam",
            "Detroj", "Mandal", "Bopal", "Chandkheda", "Maninagar", "Gota",
            "SG Highway", "Sarkhej", "Nikol", "Vatva",
        ],
    },
    "gandhinagar": {
        "label": "Gandhinagar",
        "code": "GANDHI",
        "reg_prefix": "PR/GJ/GANDHINAGAR",
        "hints": [
            "Gandhinagar Municipal", "Kalol", "Dehgam", "Mansa", "Randesan", "Kudasan",
            "Sector 1", "GIFT City", "Sargasan", "Adalaj", "Pethapur",
        ],
    },
    "mehsana": {
        "label": "Mehsana",
        "code": "MEHS",
        "reg_prefix": "PR/GJ/MEHSANA",
        "hints": [
            "Mehsana Municipal", "Kadi", "Visnagar", "Unjha", "Vadnagar", "Vijapur",
            "Kheralu", "Becharaji", "Jotana", "Gojariya",
        ],
    },
}

DEFAULT_DISTRICTS = list(DISTRICTS)

# Generous envelopes for each district. A handful of portal records (~0.5%) carry a
# location that falls outside these - clearly wrong geocodes, or a project filed under
# one district whose plot sits in another. They are kept (the portal's own claim) but
# flagged so the map can say so.
DISTRICT_ENVELOPE = {
    "ahmedabad": (22.30, 23.60, 71.75, 73.10),
    "gandhinagar": (22.95, 23.62, 72.28, 73.10),
    "mehsana": (23.25, 24.35, 71.60, 72.95),
}
BOUNDARY_SPAN_LIMIT = 0.08  # degrees (~9 km) - wider than any single scheme plot
GUJARAT_ENVELOPE = (20.0, 24.9, 68.0, 74.6)

# Bulk map-view batches. 400 project ids per POST keeps the response around 350 KB.
MAP_BATCH = 400

# Stages the map UI can filter on.
STAGES = ("ongoing", "new", "completed", "unknown")


# ---------------------------------------------------------------------------- helpers ---
def _norm_district(value: Any) -> str:
    text = re.sub(r"[^a-z]", "", str(value or "").lower())
    return text


def district_for_row(row: Dict[str, Any]) -> Optional[str]:
    """Which of our districts a global-search row belongs to.

    The portal's own `distCode` wins: a handful of projects carry a registration
    number whose district segment disagrees with the district the portal files them
    under (e.g. an AUDA project in Ahmedabad City bearing a `PR/GJ/GANDHINAGAR/...`
    number). `distCode` is what the portal's own filters use, so that is the truth
    for "which district is this scheme in"; the regNo head is the fallback for rows
    with no district code at all.
    """
    code = str(row.get("distCode") or "").strip()
    if code:
        upper = code.upper()
        for key, meta in DISTRICTS.items():
            if upper == meta["code"]:
                return key
        norm = _norm_district(code)
        for key, meta in DISTRICTS.items():
            if norm and norm == _norm_district(meta["label"]):
                return key
    reg = str(row.get("regNo") or "").upper()
    for key, meta in DISTRICTS.items():
        if reg.startswith(meta["reg_prefix"].upper()):
            return key
    return None


def portal_stage(status_text: Any) -> str:
    """The portal's own status wording, normalised."""
    text = str(status_text or "").strip().lower()
    if "complet" in text or "occupation" in text or "handed" in text:
        return "completed"
    if "ongoing" in text or "under construction" in text or "progress" in text:
        return "ongoing"
    if "new" in text or "launch" in text or "saved" in text or "approved" in text:
        return "new"
    return "unknown"


def timeline_stage(status_text: Any, start: Any, completion: Any) -> str:
    """Where the scheme sits on its own declared timeline.

    The portal's status field is stale for most projects - only ~20 of 7,400 are marked
    "Completed" while thousands sit past their declared completion date - so the stage
    shown on the map is derived from the dates the promoter declared, and the portal
    wording is kept separately as `portalStage`.

    upcoming             possession date has not arrived yet
    under-construction   inside the declared start..completion window
    past-due             past the declared completion date, no completion recorded
    completed            the portal records completion
    unknown              no usable dates
    """
    if portal_stage(status_text) == "completed":
        return "completed"
    today = _dt.date.today()
    due = _parse_day(completion)
    live = _parse_day(start)
    if live and live > today:
        return "upcoming"
    if due and due < today:
        return "past-due"
    if live or due:
        return "under-construction"
    return "unknown"


def _parse_day(value: Any) -> Optional[_dt.date]:
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return _dt.date(*map(int, match.groups()))
        except ValueError:
            return None
    match = re.match(r"^(\d{2})[-/](\d{2})[-/](\d{4})", text)
    if match:
        day, month, year = map(int, match.groups())
        try:
            return _dt.date(year, month, day)
        except ValueError:
            return None
    return None


def _iso_day(value: Any) -> Optional[str]:
    day = _parse_day(value)
    return day.isoformat() if day else None


def _num(value: Any) -> Optional[float]:
    if value is None or value == "" or value == "--":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coord(value: Any) -> Optional[float]:
    """Parse a portal coordinate string, tolerating stray separators."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "null", "NA", "NIL", "-", "--", "null,null"):
        return None
    return text


# ----------------------------------------------------------------------- geometry maths ---
def _ring(coords: Iterable[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """(lat, lng) pairs from the portal's coordinate dicts, duplicates dropped.

    Two portal quirks are handled here: stray commas in the coordinate strings
    (``",23.0079…"``) and vertices whose latitude and longitude are swapped - about
    thirty plots come back with the pair the wrong way round, which would otherwise
    stretch a 40 m boundary across half the state.
    """
    ring: List[Tuple[float, float]] = []
    for point in coords:
        if not isinstance(point, dict):
            continue
        lat, lng = _coord(point.get("lat")), _coord(point.get("lang") or point.get("lng"))
        if lat is None or lng is None or not (lat or lng):
            continue
        if GUJARAT_ENVELOPE[2] <= lat <= GUJARAT_ENVELOPE[3] and \
                GUJARAT_ENVELOPE[0] <= lng <= GUJARAT_ENVELOPE[1]:
            lat, lng = lng, lat
        ring.append((round(lat, 6), round(lng, 6)))
    deduped: List[Tuple[float, float]] = []
    for point in ring:
        if not deduped or deduped[-1] != point:
            deduped.append(point)
    if len(deduped) > 2 and deduped[0] == deduped[-1]:
        deduped.pop()
    return deduped


def _centroid(ring: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """Area centroid (shoelace) with a mean-of-vertices fallback."""
    if not ring:
        return None
    if len(ring) < 3:
        return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
    area = 0.0
    cx = cy = 0.0
    for index, (lat, lng) in enumerate(ring):
        lat2, lng2 = ring[(index + 1) % len(ring)]
        cross = lat * lng2 - lat2 * lng
        area += cross
        cx += (lat + lat2) * cross
        cy += (lng + lng2) * cross
    area *= 0.5
    if abs(area) < 1e-12:
        return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
    return (round(cx / (6 * area), 6), round(cy / (6 * area), 6))


def _area_sqm(ring: List[Tuple[float, float]]) -> Optional[float]:
    """Planar area in m² using an equirectangular projection about the polygon's mean."""
    if len(ring) < 3:
        return None
    lat0 = sum(p[0] for p in ring) / len(ring)
    lng0 = sum(p[1] for p in ring) / len(ring)
    metres_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * math.radians(lat0)) \
        + 1.175 * math.cos(4 * math.radians(lat0))
    metres_per_deg_lng = 111412.84 * math.cos(math.radians(lat0)) \
        - 93.5 * math.cos(3 * math.radians(lat0))
    points = [((lng - lng0) * metres_per_deg_lng, (lat - lat0) * metres_per_deg_lat)
              for lat, lng in ring]
    area = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        area += x1 * y2 - x2 * y1
    return round(abs(area) / 2, 1)


def _median_point(ring: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """Robust centre that tolerates a couple of garbage vertices."""
    if not ring:
        return None
    lats = sorted(p[0] for p in ring)
    lngs = sorted(p[1] for p in ring)
    middle = len(ring) // 2
    return (round(lats[middle], 6), round(lngs[middle], 6))


def _bbox(ring: List[Tuple[float, float]]) -> Optional[List[float]]:
    if not ring:
        return None
    lats = [p[0] for p in ring]
    lngs = [p[1] for p in ring]
    return [round(min(lats), 6), round(min(lngs), 6), round(max(lats), 6), round(max(lngs), 6)]


def coordinate_warning(lat: Optional[float], lng: Optional[float], district_key: str) -> Optional[str]:
    """Flag a location that does not sit where the district says it should."""
    if lat is None or lng is None:
        return None
    low_lat, high_lat, low_lng, high_lng = GUJARAT_ENVELOPE
    if not (low_lat <= lat <= high_lat and low_lng <= lng <= high_lng):
        return "location falls outside Gujarat"
    low_lat, high_lat, low_lng, high_lng = DISTRICT_ENVELOPE[district_key]
    if not (low_lat <= lat <= high_lat and low_lng <= lng <= high_lng):
        return f"location falls outside {DISTRICTS[district_key]['label']} district"
    return None


# ---------------------------------------------------------------------- local raw reuse ---
def local_raw_index(output_root: Path) -> Dict[int, Path]:
    """pid -> project folder for every project already scraped by run.py."""
    index: Dict[int, Path] = {}
    roots = [output_root] + [p for p in output_root.iterdir() if p.is_dir()]
    for root in roots:
        if not root.is_dir() or root.name == "geo":
            continue
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            match = re.match(r"^(\d+)_", folder.name)
            if match and (folder / "raw").is_dir():
                index[int(match.group(1))] = folder
    return index


def _read_local(folder: Optional[Path], name: str) -> Any:
    if not folder:
        return None
    path = folder / "raw" / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


# ------------------------------------------------------------------------------ discovery ---
# Every registration number contains its district, so one search for the district head
# enumerates that district. The remaining sweeps exist to catch the small number of
# records whose number does not follow the pattern - `PR/GJ/AHME/...` (code instead of
# name), `PR/GJ//DASKROI/...` (district segment blank), or a number whose district
# disagrees with the district the portal files the project under.
DISTRICT_CODE_PREFIX = "PR/GJ/{code}"

# Any scheme, anywhere: filtered down to the target districts by distCode.
GENERIC_QUERIES = [
    "residential", "flat", "plot", "villa", "tower", "nagar", "shop", "office",
    "bungalow", "township", "society", "heights", "homes", "park", "complex",
    "industrial", "farm", "warehouse", "tenament", "house",
]


def _search(client: GujreraClient, query: str) -> List[Dict[str, Any]]:
    data, error = client.global_search(query, data_size=10000)
    if error:
        log.warning("  search %-26r failed: %s", query, error)
        return []
    rows = (data or {}).get("data") or []
    return [row for row in rows if isinstance(row, dict) and row.get("entityType") == "PROJECT"]


def discover(client: GujreraClient, districts: List[str]
             ) -> Tuple[Dict[str, Dict[int, Dict[str, Any]]], Dict[str, Dict[str, str]]]:
    """Every project in each district, with the provenance of each pid recorded.

    Returns {district_key: {pid: row}} plus a report of how many projects each sweep
    contributed, so completeness is measurable rather than assumed.
    """
    found: Dict[str, Dict[int, Dict[str, Any]]] = {key: {} for key in districts}
    provenance: Dict[str, Dict[str, str]] = {key: {} for key in districts}

    def add(key: str, row: Dict[str, Any], source: str) -> bool:
        pid = int(row["entityId"])
        if pid in found[key]:
            return False
        found[key][pid] = row
        provenance[key][str(pid)] = source
        return True

    # 1. district registration-number head, plus the two-letter code variant
    for key in districts:
        meta = DISTRICTS[key]
        for query in (meta["reg_prefix"], DISTRICT_CODE_PREFIX.format(code=meta["code"])):
            for row in _search(client, query):
                if district_for_row(row) == key:
                    add(key, row, "regNo prefix")
        log.info("  %-11s %5d projects from registration-number sweeps", key, len(found[key]))

    # 2. one generic sweep for the whole state, split by the portal's own district code
    before = {key: len(found[key]) for key in districts}
    for query in GENERIC_QUERIES:
        for row in _search(client, query):
            key = district_for_row(row)
            if key in found:
                add(key, row, "keyword sweep")
    for key in districts:
        gained = len(found[key]) - before[key]
        if gained:
            log.info("  %-11s %5d extra projects recovered by the keyword sweep", key, gained)

    # 3. area hints (talukas / towns), for numbers with no usable district segment
    for key in districts:
        meta = DISTRICTS[key]
        before_hint = len(found[key])
        for hint in meta["hints"]:
            for row in _search(client, hint):
                if district_for_row(row) == key:
                    add(key, row, "area hint")
        gained = len(found[key]) - before_hint
        if gained:
            log.info("  %-11s %5d extra projects recovered by area hints", key, gained)

    for key in districts:
        log.info("  %-11s %5d projects total", key, len(found[key]))
    return found, provenance


# --------------------------------------------------------------------------- harvesting ---
def bulk_locations(client: GujreraClient, pids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Polygons for a whole batch of projects in one POST - the cheapest way to get boundaries."""
    out: Dict[int, Dict[str, Any]] = {}
    for start in range(0, len(pids), MAP_BATCH):
        chunk = pids[start:start + MAP_BATCH]
        data, error = client.post_json("maplocation/public/getProjectMapView", chunk)
        if error:
            log.warning("  map-view batch %d failed: %s", start // MAP_BATCH + 1, error)
            continue
        for row in (data or {}).get("data") or []:
            if isinstance(row, dict) and row.get("projectId"):
                out[int(row["projectId"])] = row
        log.info("  map-view batch %2d/%d  (%d ids, %d boundaries so far)",
                 start // MAP_BATCH + 1, math.ceil(len(pids) / MAP_BATCH), len(chunk), len(out))
    return out


def fetch_details(client: GujreraClient, pid: int) -> Dict[str, Any]:
    """core + profile + location for one project (3 requests)."""
    details: Dict[str, Any] = {}
    core, core_err = client.get_json("core", pid=pid)
    if not core_err:
        details["core"] = payload(core) or {}
    profile, profile_err = client.get_json("profile", pid=pid)
    if not profile_err:
        details["profile"] = ((payload(profile) or {}).get("projectDetail")) or {}
    location, loc_err = client.get_json("location", pid=pid)
    if not loc_err:
        details["location"] = payload(location) or {}
    details["errors"] = {k: v for k, v in
                         (("core", core_err), ("profile", profile_err), ("location", loc_err)) if v}
    return details


def _trim(details: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the fields the dataset needs, so the cache stays small."""
    core = details.get("core") or {}
    profile = details.get("profile") or {}
    location = details.get("location") or {}
    return {
        "core": {k: core.get(k) for k in (
            "projectName", "projRegNo", "projectType", "promoterName", "promoterType",
            "promoterEmailId", "promoterMobileNo", "approvedDate", "appSubmissionDate",
            "projectAckNo", "projectStatus",
        ) if core.get(k) is not None},
        "profile": {k: profile.get(k) for k in (
            "projectAddress", "projectAddress2", "pinCode", "distName", "distCode",
            "subDistName", "projectStatus", "projectDesc", "startDate", "completionDate",
            "approvingAuthority", "moje", "revenueSurveyNo", "citySurveyNo",
            "validTpName", "tPNo", "tPName", "finalPlotNo", "subPlotNumber",
            "totLandAreaForProjectUnderReg", "totAreaOfLandLayout", "totCarpetArea",
            "totCarpetAreaForProjectUnderReg", "totCoverdArea", "totOpenArea",
            "totalProjectCost", "costOfLand", "estimatedCost",
        ) if profile.get(k) is not None},
        "location": {k: location.get(k) for k in (
            "address", "projectStatus", "projectCost", "promoterName", "promoterType",
            "subDistrictName", "districtName", "districtCode", "processType", "waterLevel",
        ) if location.get(k) is not None},
        "errors": details.get("errors") or {},
    }


# ----------------------------------------------------------------------------- records ---
def build_record(pid: int, district_key: str, row: Dict[str, Any],
                 details: Dict[str, Any], local: Optional[Path] = None,
                 ring: Optional[List[Tuple[float, float]]] = None,
                 output_root: Optional[Path] = None,
                 discovery_source: Optional[str] = None) -> Dict[str, Any]:
    meta = DISTRICTS[district_key]
    core = details.get("core") or {}
    profile = details.get("profile") or {}
    location = details.get("location") or {}

    ring = ring if ring is not None else _ring(location.get("coordinates") or [])
    box = _bbox(ring)
    boundary_warning = None
    # A single scheme plot is never nine kilometres across. When the portal hands back a
    # polygon that wide the vertices are mixed up beyond repair, so keep a robust centre
    # and drop the shape rather than draw a boundary over half the district.
    if ring and box and max(box[2] - box[0], box[3] - box[1]) > BOUNDARY_SPAN_LIMIT:
        boundary_warning = "portal boundary geometry is inconsistent"
        centre = _median_point(ring)
        ring, box = [], None
    else:
        centre = _centroid(ring)

    name = _text(core.get("projectName")) or _text(row.get("entityName")) \
        or _text(location.get("projectName")) or f"Project {pid}"
    reg_no = _text(core.get("projRegNo")) or _text(row.get("regNo"))
    status_text = _text(location.get("projectStatus")) or _text(profile.get("projectStatus"))
    completion = profile.get("completionDate")

    cost = _num(location.get("projectCost")) or _num(profile.get("totalProjectCost")) \
        or _num(row.get("maxcost"))
    cost_source = None
    if _num(location.get("projectCost")):
        cost_source = "portal (project location record)"
    elif _num(profile.get("totalProjectCost")):
        cost_source = "portal (declared project cost)"
    elif _num(row.get("maxcost")):
        cost_source = "portal (search index, max unit cost)"

    address = _text(location.get("address")) or _text(profile.get("projectAddress"))
    if address and _text(profile.get("projectAddress2")):
        address = f"{address}, {_text(profile.get('projectAddress2'))}"

    progress = None
    units = booked = None
    if local:
        score = payload(_read_local(local, "progress_score")) or {}
        if isinstance(score, dict):
            progress = _num(score.get("score"))
        inventory = inventory_summary(payload(_read_local(local, "form_three")))
        if inventory:
            units = sum(r["Total Units"] for r in inventory)
            booked = sum(r["Booked"] for r in inventory)

    record: Dict[str, Any] = {
        "pid": pid,
        "name": name,
        "regNo": reg_no,
        "districtKey": district_key,
        "district": meta["label"],
        "districtCode": _text(location.get("districtCode")) or meta["code"],
        "subDistrict": (_text(profile.get("subDistName"))
                        or _text(location.get("subDistrictName"))
                        or _text(row.get("taluka"))),
        "taluka": _text(row.get("taluka")),
        "address": address,
        "pincode": _text(profile.get("pinCode")) or _text(row.get("pinCode")),
        "projectType": _text(core.get("projectType")) or _text(row.get("ptype")),
        "projectStatus": status_text,
        "stage": timeline_stage(status_text, profile.get("startDate"), completion),
        "portalStage": portal_stage(status_text),
        "processType": _text(location.get("processType")),
        "alteration": bool(re.search(r"/A\d*R/", str(reg_no or ""), re.IGNORECASE)),
        "promoter": _text(core.get("promoterName")) or _text(location.get("promoterName")),
        "promoterType": _text(core.get("promoterType")) or _text(location.get("promoterType")),
        "promoterEmail": _text(core.get("promoterEmailId")),
        "promoterMobile": _text(core.get("promoterMobileNo")),
        "cost": cost,
        "costSource": cost_source,
        "approvedOn": _iso_day(core.get("approvedDate")),
        "submittedOn": _iso_day(core.get("appSubmissionDate")),
        "startDate": _iso_day(profile.get("startDate")),
        "completionDate": _iso_day(completion),
        "landAreaSqm": _num(profile.get("totLandAreaForProjectUnderReg"))
        or _num(profile.get("totAreaOfLandLayout")),
        "carpetAreaSqm": _num(profile.get("totCarpetAreaForProjectUnderReg"))
        or _num(profile.get("totCarpetArea")),
        "coveredAreaSqm": _num(profile.get("totCoverdArea")),
        "openAreaSqm": _num(profile.get("totOpenArea")),
        "approvingAuthority": _text(profile.get("approvingAuthority")),
        "projectDesc": _text(profile.get("projectDesc")),
        "moje": _text(profile.get("moje")),
        "surveyNo": _text(profile.get("revenueSurveyNo")),
        "citySurveyNo": _text(profile.get("citySurveyNo")),
        "tpName": _text(profile.get("tPName")) or _text(profile.get("validTpName")),
        "tpNo": _text(profile.get("tPNo")),
        "finalPlotNo": _text(profile.get("finalPlotNo")),
        "subPlotNo": _text(profile.get("subPlotNumber")),
        "waterLevel": _num(location.get("waterLevel")),
        "progress": progress,
        "units": units,
        "booked": booked,
        "lat": centre[0] if centre else None,
        "lng": centre[1] if centre else None,
        "bbox": box,
        "boundarySqm": _area_sqm(ring),
        "boundary": [[p[0], p[1]] for p in ring] or None,
        "hasBoundary": len(ring) >= 3,
        "boundaryWarning": boundary_warning,
        "coordinateWarning": coordinate_warning(centre[0] if centre else None,
                                                centre[1] if centre else None, district_key),
        "searchAddress": _text(row.get("address")),
        "unitCostMin": min(filter(None, (_num(row.get("mincost")), _num(row.get("maxcost")))), default=None),
        "unitCostMax": max(filter(None, (_num(row.get("mincost")), _num(row.get("maxcost")))), default=None),
        "declaredTimeline": _text(row.get("pdate")),
        "imageUid": _text(row.get("imageUid")),
        "discoverySource": discovery_source,
        "localFolder": local.name if local else None,
        "localReport": _relative(local, output_root) if local else None,
        "errors": details.get("errors") or {},
    }
    return record


def _relative(path: Optional[Path], root: Optional[Path]) -> Optional[str]:
    if not path or not root:
        return None
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


# ---------------------------------------------------------------------------- orchestration ---
def _write_json(path: Path, data: Any, indent: Optional[int] = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent, ensure_ascii=False, default=str,
                      separators=(",", ":") if indent is None else None)
    path.write_text(text, encoding="utf-8")


CSV_FIELDS = [
    "pid", "name", "regNo", "district", "districtKey", "subDistrict", "taluka",
    "address", "pincode", "projectType", "projectStatus", "stage", "portalStage",
    "processType", "alteration", "promoter", "promoterType", "promoterEmail",
    "promoterMobile", "cost", "approvedOn", "submittedOn", "startDate", "completionDate",
    "landAreaSqm", "carpetAreaSqm", "coveredAreaSqm", "openAreaSqm",
    "boundarySqm", "approvingAuthority", "projectDesc", "moje", "surveyNo",
    "finalPlotNo", "tpName", "tpNo", "progress", "units", "booked",
    "lat", "lng", "hasBoundary", "coordinateWarning", "boundaryWarning",
    "unitCostMin", "unitCostMax", "discoverySource",
]


def write_outputs(geo_root: Path, records: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    geo_root.mkdir(parents=True, exist_ok=True)
    records.sort(key=lambda r: (r["districtKey"], (r["name"] or "").upper()))

    _write_json(geo_root / "schemes.json", {"summary": summary, "records": records},
                indent=None)

    with (geo_root / "schemes.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({k: record.get(k) for k in CSV_FIELDS})

    point_features = []
    polygon_features = []
    for record in records:
        properties = {k: v for k, v in record.items() if k not in ("boundary", "errors")}
        if record.get("lat") is not None and record.get("lng") is not None:
            point_features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [record["lng"], record["lat"]]},
                "properties": properties,
            })
        boundary = record.get("boundary")
        if record.get("hasBoundary") and boundary:
            ring = [[lng, lat] for lat, lng in boundary]
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            polygon_features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": properties,
            })

    _write_json(geo_root / "schemes.geojson",
                {"type": "FeatureCollection", "features": point_features}, indent=None)
    _write_json(geo_root / "boundaries.geojson",
                {"type": "FeatureCollection", "features": polygon_features}, indent=None)

    for key, meta in DISTRICTS.items():
        slice_records = [r for r in records if r["districtKey"] == key]
        if not slice_records:
            continue
        _write_json(geo_root / f"{key}.json", {
            "district": meta["label"],
            "code": meta["code"],
            "generated_at": summary["generated_at"],
            "records": slice_records,
        })

    _write_json(geo_root / "summary.json", summary)


def summarise(records: List[Dict[str, Any]], generated_at: str) -> Dict[str, Any]:
    districts: Dict[str, Dict[str, Any]] = {}
    for key, meta in DISTRICTS.items():
        subset = [r for r in records if r["districtKey"] == key]
        districts[key] = {
            "label": meta["label"],
            "code": meta["code"],
            "total": len(subset),
            "withCoordinates": sum(1 for r in subset if r["lat"] is not None),
            "withBoundary": sum(1 for r in subset if r["hasBoundary"]),
            "upcoming": sum(1 for r in subset if r["stage"] == "upcoming"),
            "underConstruction": sum(1 for r in subset if r["stage"] == "under-construction"),
            "pastDue": sum(1 for r in subset if r["stage"] == "past-due"),
            "completed": sum(1 for r in subset if r["stage"] == "completed"),
            "unknown": sum(1 for r in subset if r["stage"] == "unknown"),
            "withLocalReport": sum(1 for r in subset if r["localFolder"]),
            "centre": _district_centre(subset),
        }
    costs = [r["cost"] for r in records if r.get("cost")]
    return {
        "generated_at": generated_at,
        "total": len(records),
        "withCoordinates": sum(1 for r in records if r["lat"] is not None),
        "withBoundary": sum(1 for r in records if r["hasBoundary"]),
        "alterations": sum(1 for r in records if r["alteration"]),
        "withLocalReport": sum(1 for r in records if r["localFolder"]),
        "totalDeclaredCost": sum(costs) if costs else None,
        "districts": districts,
        "types": _count_by(records, "projectType"),
        "stages": _count_by(records, "stage"),
        "discoverySources": _count_by(records, "discoverySource"),
        "coordinateWarnings": sum(1 for r in records if r.get("coordinateWarning")),
        "boundaryWarnings": sum(1 for r in records if r.get("boundaryWarning")),
    }


def _count_by(records: List[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for record in records:
        value = record.get(field) or "Unknown"
        counts[str(value)] = counts.get(str(value), 0) + 1
    return [{"value": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def _district_centre(records: List[Dict[str, Any]]) -> Optional[List[float]]:
    points = [(r["lat"], r["lng"]) for r in records if r["lat"] is not None]
    if not points:
        return None
    return [round(sum(p[0] for p in points) / len(points), 5),
            round(sum(p[1] for p in points) / len(points), 5)]


def run_geo(output_root: Path, districts: Optional[List[str]] = None, *,
            workers: int = 12, refresh: bool = False, limit: Optional[int] = None,
            reuse_local: bool = True) -> Dict[str, Any]:
    """Harvest every scheme in the given districts and write the geo dataset."""
    districts = [d.strip().lower() for d in (districts or DEFAULT_DISTRICTS) if d.strip()]
    unknown = [d for d in districts if d not in DISTRICTS]
    if unknown:
        raise KeyError(f"Unknown district(s) {unknown}. Known: {', '.join(DISTRICTS)}")

    geo_root = output_root / "geo"
    cache = geo_root / "cache"
    details_dir = cache / "details"
    details_dir.mkdir(parents=True, exist_ok=True)

    client = GujreraClient(delay=0.2, pool_maxsize=max(32, workers * 4))

    # ------------------------------------------------------------------ 1. discovery ---
    discovery_path = cache / "discovery.json"
    found: Dict[str, Dict[str, Any]] = {}
    provenance: Dict[str, Dict[str, Any]] = {}
    if discovery_path.exists() and not refresh:
        cached = json.loads(discovery_path.read_text(encoding="utf-8"))
        cached_districts = cached.get("districts") or {}
        if all(key in cached_districts for key in districts):
            found = {key: {int(pid): row for pid, row in cached_districts[key].items()}
                     for key in districts}
            provenance = {key: (cached.get("provenance") or {}).get(key) or {}
                          for key in districts}
            log.info("discovery: restored from cache (%s)",
                     {k: len(v) for k, v in found.items()})

    if not found:
        log.info("discovery: sweeping %d district(s)", len(districts))
        found, provenance = discover(client, districts)
        _write_json(discovery_path, {
            "generated_at": _dt.datetime.now().strftime("%d-%m-%Y %H:%M"),
            "districts": {k: {str(pid): row for pid, row in v.items()}
                          for k, v in found.items()},
            "provenance": provenance,
        }, indent=None)

    # Local scrapes are authoritative for registration numbers we already hold.
    local_index = local_raw_index(output_root) if reuse_local else {}

    # ------------------------------------------------------------- 2. boundaries (bulk) ---
    all_pids = sorted({pid for rows in found.values() for pid in rows})
    if limit:
        all_pids = all_pids[:limit]
    log.info("%d projects to place", len(all_pids))

    locations_path = cache / "locations.json"
    locations: Dict[int, Dict[str, Any]] = {}
    if locations_path.exists() and not refresh:
        locations = {int(k): v for k, v in
                     json.loads(locations_path.read_text(encoding="utf-8")).items()}
        log.info("boundaries: %d cached", len(locations))
    missing = [pid for pid in all_pids if pid not in locations]
    if missing:
        log.info("boundaries: fetching %d projects from the map-view endpoint", len(missing))
        locations.update(bulk_locations(client, missing))
        _write_json(locations_path, {str(k): v for k, v in sorted(locations.items())},
                    indent=None)

    # ------------------------------------------------------------------ 3. details ---
    def harvest(pid: int) -> Tuple[int, Dict[str, Any], Optional[Path]]:
        folder = local_index.get(pid)
        cache_file = details_dir / f"{pid}.json"

        details: Optional[Dict[str, Any]] = None
        if folder and reuse_local:
            core = _read_local(folder, "core")
            profile = _read_local(folder, "profile")
            location = _read_local(folder, "location")
            if core or profile or location:
                details = {
                    "core": payload(core) or {},
                    "profile": ((payload(profile) or {}).get("projectDetail")) or {},
                    "location": payload(location) or {},
                    "errors": {},
                }
        if details is None and cache_file.exists() and not refresh:
            try:
                details = json.loads(cache_file.read_text(encoding="utf-8"))
            except ValueError:
                details = None
        if details is None:
            details = fetch_details(client, pid)
        details = _trim(details)
        if not cache_file.exists():
            cache_file.write_text(json.dumps(details, ensure_ascii=False, default=str),
                                  encoding="utf-8")
        return pid, details, folder

    record_by_pid: Dict[int, Dict[str, Any]] = {}
    district_by_pid = {pid: key for key, rows in found.items() for pid in rows}

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(harvest, pid): pid for pid in all_pids}
        for future in as_completed(futures):
            pid = futures[future]
            try:
                pid, details, folder = future.result()
            except Exception:  # noqa: BLE001 - one bad project must not stop the sweep
                log.exception("details failed for %s", pid)
                continue
            bulk = locations.get(pid) or {}
            ring = _ring(bulk.get("coordinates") or []) or \
                _ring((details.get("location") or {}).get("coordinates") or [])
            record_by_pid[pid] = build_record(
                pid, district_by_pid[pid], found[district_by_pid[pid]].get(pid, {}),
                details if isinstance(details, dict) else {}, folder, ring,
                output_root=output_root,
                discovery_source=(provenance.get(district_by_pid[pid]) or {}).get(str(pid)),
            )
            done += 1
            if done % 250 == 0:
                log.info("details: %d/%d projects", done, len(all_pids))

    records = list(record_by_pid.values())
    generated_at = _dt.datetime.now().strftime("%d-%m-%Y %H:%M")

    # Writing must never silently shrink the canonical dataset: a `--limit` run is a
    # sample, and a single-district run keeps the districts it did not touch.
    out_root = geo_root
    if limit:
        out_root = geo_root / "sample"
        log.info("sample run (%d projects) — writing to %s, canonical dataset untouched",
                 limit, out_root)
    elif set(districts) != set(DISTRICTS):
        existing = geo_root / "schemes.json"
        if existing.exists():
            try:
                previous = json.loads(existing.read_text(encoding="utf-8")).get("records") or []
            except ValueError:
                previous = []
            kept = [r for r in previous if r.get("districtKey") not in districts]
            if kept:
                log.info("keeping %d records from districts not in this run", len(kept))
                records = kept + records

    summary = summarise(records, generated_at)
    summary["sample"] = bool(limit)
    summary["datasetPath"] = str((out_root / "schemes.json").as_posix())
    write_outputs(out_root, records, summary)

    log.info("geo dataset written to %s", out_root)
    for key, info in summary["districts"].items():
        log.info("  %-11s %5d schemes | %5d mapped | %5d with boundary | %5d under construction",
                 info["label"], info["total"], info["withCoordinates"],
                 info["withBoundary"], info["underConstruction"])
    return summary

"""
Import everything already scraped into the state store.

Before the delta engine existed, projects were scraped and uploaded by
`cloudflare/r2_sync.py`, which knew nothing about `state/gujrera.db`. Those
documents are in R2, but the state store cannot see them — so `--coverage`
reports "24 captured" for a district whose documents are all present and
paying rent.

This walks `output/**` and reconstructs the same facts the delta engine would
have recorded:

  * one `projects` row per folder (pid, district, name, capture time),
  * one `documents` row per manifest entry, with the **legacy path-based key**
    as its blob location, so it is addressable immediately,
  * one `endpoint_hashes` row per `raw/*.json`, hashed exactly the way the probe
    hashes a live payload.

Which means: after an import, a project is only "not scraped" if it genuinely
never was. And nothing is downloaded again — see the note on `sha256` below.

Two layouts are on disk and both are handled: district folders
(`output/AHMEDABAD/1110_NORTH_PARK_PHASE_5/`) and older flat folders
(`output/1110_NORTH_PARK_PHASE_5/`). The legacy R2 keys include whichever
prefix the folder had, so the key is derived from the folder's real position
relative to `output/`, not rebuilt from the pid.

Known limitation: these rows only carry `sha256` when the scrape that produced
them computed one, which the oldest runs did not. That is fine for identity —
the VDMS UID is the document's identity and is always present — but it means
those blobs cannot participate in deduplication until they are next downloaded
for some other reason. Importing is therefore cheap and never re-downloads.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .delta import endpoint_hashes
from .state import State

log = logging.getLogger("gujrera.legacy")

# Identical to `cloudflare/r2_sync.py:BINARY_EXT`. If these two sets ever
# disagree, an imported blob_key points at an object that was never uploaded,
# so it is deliberately a copy rather than a guess.
BINARY_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp", ".gif", ".zip",
              ".tif", ".tiff"}

DISTRICT_DIRS = {"AHMEDABAD", "GANDHINAGAR", "MEHSANA", "RAJKOT", "SURAT",
                 "VADODARA", "BHAVNAGAR", "JAMNAGAR", "GANDHINAGAR"}


def route_key(relative: str, pdf_prefix: str = "", json_prefix: str = "") -> str:
    """The key `r2_sync.destination()` would have produced for a file in output/.

    Reimplemented rather than imported: `r2_sync` reads its environment at import
    time and drags in boto3, neither of which an import pass should require.
    """
    suffix = ("." + relative.rsplit(".", 1)[-1].lower()) if "." in relative else ""
    prefix = pdf_prefix if suffix in BINARY_EXT else json_prefix
    prefix = (prefix or "").strip("/")
    return f"{prefix}/{relative}" if prefix else relative


def _parse_captured_at(raw: str) -> Optional[str]:
    """`scraped_at` in project_bundle.json is 'DD-MM-YYYY HH:MM'."""
    if not raw:
        return None
    for fmt in ("%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S"):
        try:
            return _dt.datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return None


def _bundle(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def district_from_reg_no(reg_no: Optional[str]) -> Optional[str]:
    """`PR/GJ/AHMEDABAD/DASKROI/AUDA/MAA00044/010917` -> `Ahmedabad`.

    The registration number is the only district record some very old scrapes
    kept: their `project_bundle.json` has `target.district` unset, so a flat
    folder leaves the project with no district and `--coverage` invents an
    `UNKNOWN` bucket. The reg no is issued by the district office, so it is a
    reliable fallback — and it is the same string the portal would have shown.
    """
    if not reg_no:
        return None
    parts = [p.strip() for p in reg_no.split("/") if p.strip()]
    if len(parts) >= 3 and parts[1].upper() == "GJ":
        return parts[2].title()
    return None


def _project_facts(folder: Path, bundle: Dict[str, Any]) -> Dict[str, Any]:
    summary = bundle.get("summary") or {}
    target = bundle.get("target") or {}
    summary = summary if isinstance(summary, dict) else {}
    target = target if isinstance(target, dict) else {}
    reg_no = summary.get("regNo") or target.get("regNo")
    return {
        "name": summary.get("name") or target.get("name"),
        "reg_no": reg_no,
        "district": target.get("district") or district_from_reg_no(reg_no),
        "taluka": summary.get("taluka"),
        "ptype": summary.get("type"),
        "cost": summary.get("cost"),
        "end_date": summary.get("endDate"),
        "captured_at": _parse_captured_at(bundle.get("scraped_at") or ""),
    }


def scan_project(folder: Path, output_root: Path,
                 pdf_prefix: str = "", json_prefix: str = "") -> Optional[Dict[str, Any]]:
    """Reconstruct one project's state from what is on disk. None if unusable."""
    pid_text = folder.name.split("_", 1)[0]
    if not pid_text.isdigit():
        return None
    pid = int(pid_text)

    bundle = _bundle(folder / "project_bundle.json")
    info: Dict[str, Any] = {"pid": pid}
    info.update({k: v for k, v in _project_facts(folder, bundle).items()
                 if v not in (None, "")})

    # The folder's own parent is the district when the layout has district
    # folders; the bundle is the fallback for the older flat layout.
    parent = folder.parent
    if parent != output_root:
        info["district"] = parent.name.replace("_", " ").title()

    # Documents from the manifest, keyed the way the uploader keyed them.
    documents: List[Dict[str, Any]] = []
    manifest_path = folder / "documents_manifest.json"
    if manifest_path.exists():
        try:
            rows = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            if not isinstance(row, dict) or row.get("status") != "ok" or not row.get("uid"):
                continue
            filename = row.get("file") or ""
            blob_key = ""
            if filename:
                try:
                    relative = (folder / "files" / filename).relative_to(output_root).as_posix()
                except ValueError:  # filename escapes the folder; skip the key
                    relative = ""
                if relative:
                    blob_key = route_key(relative, pdf_prefix, json_prefix)
            documents.append({
                "uid": row["uid"],
                "label": row.get("label") or "",
                "file": filename,
                "bytes": row.get("bytes") or 0,
                "content_type": row.get("content_type") or "",
                "sha256": row.get("sha256") or None,
                "blob_key": blob_key or None,
                "status": "ok",
            })

    # Endpoint fingerprints from raw/, using the same hashing the probe uses, so
    # an imported project compares correctly against a live one.
    hashes: Dict[str, Any] = {}
    raw_dir = folder / "raw"
    if raw_dir.is_dir():
        raw: Dict[str, Any] = {}
        for path in sorted(raw_dir.glob("*.json")):
            try:
                raw[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        if raw:
            hashes = endpoint_hashes(raw)

    info["documents"] = documents
    info["hashes"] = hashes
    return info


def iter_projects(output_root: Path, pdf_prefix: str = "", json_prefix: str = ""
                  ) -> Iterator[Dict[str, Any]]:
    """Every project folder under output/, each pid once.

    A pid can appear twice while a layout migration is half-done (a flat folder
    and its district-folder successor). The one with more documents wins, since
    it is the more complete capture of the same immutable pid.
    """
    best: Dict[int, Dict[str, Any]] = {}
    for manifest in sorted(output_root.rglob("documents_manifest.json")):
        folder = manifest.parent
        found = scan_project(folder, output_root, pdf_prefix, json_prefix)
        if not found:
            continue
        seen = best.get(found["pid"])
        if seen is None or len(found["documents"]) > len(seen["documents"]):
            best[found["pid"]] = found
    for pid in sorted(best):
        yield best[pid]


def import_legacy(state: State, output_root: Path, pdf_prefix: str = "",
                  json_prefix: str = "", dry_run: bool = False,
                  hash_raw: bool = True) -> Dict[str, Any]:
    """Upsert every project found under output/ into the state store."""
    totals = {
        "projects": 0, "new_projects": 0, "documents": 0, "blobs": 0,
        "hashes": 0, "bytes": 0, "skipped": 0, "overwritten_hashes": 0,
    }

    for found in iter_projects(output_root, pdf_prefix, json_prefix):
        pid = found["pid"]
        existing = state.get_project(pid)
        if existing is None:
            totals["new_projects"] += 1
        totals["projects"] += 1

        if dry_run:
            totals["documents"] += len(found["documents"])
            totals["hashes"] += len(found["hashes"])
            totals["bytes"] += sum(int(d.get("bytes") or 0) for d in found["documents"])
            totals["blobs"] += sum(1 for d in found["documents"] if d.get("blob_key"))
            continue

        state.upsert_project({
            "pid": pid,
            "name": found.get("name"),
            "regNo": found.get("reg_no"),
            "district": found.get("district") or (existing or {}).get("district"),
            "taluka": found.get("taluka"),
            "type": found.get("ptype"),
            "cost": found.get("cost"),
            "endDate": found.get("end_date"),
        })
        captured = found.get("captured_at")
        if captured:
            state.backdate(pid, captured)

        if hash_raw and found["hashes"]:
            # Never overwrite hashes the delta engine recorded from a live
            # response: the on-disk copy is exactly as old as the scrape, and a
            # live probe may already have moved past it.
            if state.endpoint_hashes(pid):
                totals["overwritten_hashes"] += 1
            else:
                state.set_endpoint_hashes(pid, found["hashes"])
                totals["hashes"] += len(found["hashes"])

        known = state.known_documents(pid)  # once per project, not per document
        for doc in found["documents"]:
            if doc["uid"] in known:
                totals["skipped"] += 1
                continue
            state.upsert_document(pid, doc, uploaded=bool(doc.get("blob_key")))
            totals["documents"] += 1
            if doc.get("blob_key"):
                totals["blobs"] += 1
            totals["bytes"] += int(doc.get("bytes") or 0)

        if totals["projects"] % 250 == 0:
            log.info("  scanned %d projects, %d documents (%d new)",
                     totals["projects"], len(found["documents"]), totals["documents"])

    return totals

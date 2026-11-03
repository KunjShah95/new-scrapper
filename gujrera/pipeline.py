"""
End-to-end pipeline: discover Ahmedabad projects -> scrape every tab -> download files ->
write Markdown.

Run `python run.py --help` for the CLI.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .api import GujreraClient, payload
from .documents import download_all, harvest, is_uid, write_manifest
from .report import (
    build_master_markdown,
    build_project_markdown,
    fmt_date,
    inventory_summary,
)
from . import report as report_mod

log = logging.getLogger("gujrera.pipeline")

AHMEDABAD_CODES = {"AHME"}

# Endpoints that are fetched as part of a full project bundle, in report order.
BUNDLE_ENDPOINTS = [
    "core",
    "applications",
    "collection_bank",
    "project_banks",
    "profile",
    "blocks",
    "location",
    "promoter",
    "previous_projects",
    "form_one",
    "form_two",
    "form_three",
    "form_three_loan",
    "form_one_photos",
    "joint_development",
    "progress_score",
    "block_progress",
    "block_chart",
    "area_price",
    "qpr_progress",
    "form_four",
    "qe_enquiry",
    "form_eight",
    "quarters",
    "annual_compliance",
]


def slugify(text: str, max_len: int = 48) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return (text[:max_len] or "project").upper()


# ------------------------------------------------------------------------------- discovery ---
def discover_projects(client: GujreraClient, queries: Optional[List[str]] = None,
                      only_ongoing: bool = True) -> List[Dict[str, Any]]:
    """Return Ahmedabad projects harvested from the public global-search endpoint."""
    queries = queries or [
        "residential", "flats", "apartment", "project", "heights", "park",
        "avenue", "complex", "green", "construction",
    ]
    seen: Dict[int, Dict[str, Any]] = {}
    for query in queries:
        data, error = client.global_search(query)
        if error:
            log.warning("global-search '%s' failed: %s", query, error)
            continue
        for item in (data or {}).get("data") or []:
            if item.get("entityType") != "PROJECT":
                continue
            if str(item.get("distCode", "")).upper() not in AHMEDABAD_CODES:
                continue
            seen[int(item["entityId"])] = item
        log.info("query '%s' -> %d unique Ahmedabad projects so far", query, len(seen))

    today = _dt.date.today()
    projects = []
    for item in seen.values():
        end = _parse_end_date(item.get("pdate"))
        if only_ongoing and (end is None or end <= today):
            continue
        projects.append(
            {
                "pid": int(item["entityId"]),
                "name": (item.get("entityName") or "").strip(),
                "regNo": item.get("regNo"),
                "type": item.get("ptype"),
                "taluka": item.get("taluka"),
                "district": item.get("distName"),
                "address": item.get("address"),
                "cost": item.get("maxcost"),
                "endDate": end.isoformat() if end else None,
            }
        )
    projects.sort(key=lambda p: -(p.get("cost") or 0))
    return projects


def _parse_end_date(pdate: Optional[str]) -> Optional[_dt.date]:
    if not pdate or "-" not in pdate:
        return None
    tail = pdate.split("- ")[-1].strip()
    try:
        return _dt.datetime.strptime(tail, "%d/%m/%Y").date()
    except ValueError:
        return None


def resolve_target(client: GujreraClient, needle: str) -> Optional[Dict[str, Any]]:
    """Resolve a project by numeric id, registration number or name fragment."""
    needle = needle.strip()
    if needle.isdigit():
        return {"pid": int(needle), "name": None, "regNo": None}
    data, _ = client.global_search(needle, data_size=50)
    for item in (data or {}).get("data") or []:
        if item.get("entityType") != "PROJECT":
            continue
        if needle.upper() in (item.get("regNo") or "").upper() or \
                needle.lower() in (item.get("entityName") or "").lower():
            return {
                "pid": int(item["entityId"]),
                "name": (item.get("entityName") or "").strip(),
                "regNo": item.get("regNo"),
            }
    return None


# ---------------------------------------------------------------------------------- scrape ---
def scrape_project(client: GujreraClient, target: Dict[str, Any], output_root: Path,
                   download_files: bool = True) -> Dict[str, Any]:
    pid = int(target["pid"])
    log.info("=" * 78)
    log.info("Scraping project %s (%s)", pid, target.get("name") or "?")

    raw: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    def fetch(name: str, **params: Any) -> Any:
        data, error = client.get_json(name, **params)
        if error:
            errors[name] = error
            log.warning("  %-22s FAILED (%s)", name, error)
        else:
            log.info("  %-22s ok", name)
        raw[name] = data
        return payload(data)

    core = fetch("core", pid=pid) or {}
    if not core:
        raise RuntimeError(f"Project {pid} returned no core data")

    form_one_id = core.get("formOneId") or 0
    form_two_id = core.get("formTwoId") or 0
    form_three_id = core.get("formThreeId") or 0
    promoter_id = core.get("promoterId") or 0
    ack_no = core.get("projectAckNo") or ""

    # Search results carry the registration number; the core payload is authoritative.
    params = {
        "pid": pid,
        "form_one_id": form_one_id,
        "form_two_id": form_two_id,
        "form_three_id": form_three_id,
        "promoter_id": promoter_id,
        "ack_no": ack_no,
        "app_reg_id": 0,
    }

    applications = fetch("applications", pid=pid) or []
    registration = next(
        (a for a in applications if str(a.get("applicationType", "")).lower().startswith("registration")),
        applications[0] if applications else {},
    )
    params["app_reg_id"] = registration.get("appRegId") or 0

    for name in BUNDLE_ENDPOINTS:
        if name in raw:
            continue
        fetch(name, **params)

    # Optional extras that need ids discovered above.
    noc, noc_err = client.get_json("form_one_noc", **params)
    if noc_err:
        errors["form_one_noc"] = noc_err
    raw["form_one_noc"] = noc

    scanned, scanned_err = client.get_json("scanned_docs", ack_no=ack_no)
    if scanned_err:
        errors["scanned_docs"] = scanned_err
    raw["scanned_docs"] = scanned

    # ------------------------------------------------------------------ document index ---
    uid_pairs = harvest(raw)
    # Explicit extras: Form 5 PDF links and progress photographs are plain lists.
    for record in payload(raw.get("annual_compliance")) or []:
        if isinstance(record, dict) and is_uid(record.get("pdfUid")):
            uid_pairs.append((f"Form 5 Annual Report FY {record.get('projectFinancialYear')}", record["pdfUid"]))
    photos = payload(raw.get("form_one_photos"))
    if isinstance(photos, list):
        for index, uid in enumerate(photos, start=1):
            if is_uid(uid):
                uid_pairs.append((f"Site Progress Photograph {index}", uid))
    form_eight_uid = payload(raw.get("form_eight"))
    if is_uid(form_eight_uid):
        uid_pairs.append(("Form 8 (Annual Report) PDF", form_eight_uid))

    project_folder = output_root / f"{pid}_{slugify(core.get('projectName') or target.get('name') or str(pid))}"
    project_folder.mkdir(parents=True, exist_ok=True)
    (project_folder / "raw").mkdir(exist_ok=True)

    for name, data in raw.items():
        if data is None:
            continue
        (project_folder / "raw" / f"{name}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    documents: List[Dict[str, Any]] = []
    if download_files:
        documents = download_all(client, uid_pairs, project_folder / "files")
    write_manifest(documents, project_folder / "documents_manifest.json")

    # ------------------------------------------------------------------------- bundle ---
    bundle: Dict[str, Any] = {
        "target": target,
        "params": params,
        "raw": raw,
        "errors": errors,
        "documents": documents,
        "folder": project_folder.name,
        "scraped_at": _dt.datetime.now().strftime("%d-%m-%Y %H:%M"),
        "source_url": "https://gujrera.gujarat.gov.in/#/project-preview",
        "markdown_name": f"{slugify(core.get('projectName') or str(pid))}.md",
        "summary": {},
    }
    bundle["summary"] = build_summary_row(bundle)

    markdown = build_project_markdown(bundle)
    (project_folder / bundle["markdown_name"]).write_text(markdown, encoding="utf-8")

    (project_folder / "project_bundle.json").write_text(
        json.dumps({k: v for k, v in bundle.items() if k != "raw"}, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    log.info("Saved %s", project_folder)
    return bundle


def bundle_stub(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {"raw": raw}



def build_summary_row(bundle: Dict[str, Any]) -> Dict[str, Any]:
    def data(name: str) -> Any:
        return payload(bundle["raw"].get(name))

    core = data("core") or {}
    detail = ((data("profile") or {}).get("projectDetail")) or {}
    progress = data("progress_score") or {}
    area = (report_mod._first_list(data("area_price")) or [{}])[0]
    inventory = inventory_summary(data("form_three"))
    form_two = data("form_two") or {}
    quarters = report_mod._first_list(data("quarters"))

    units = sum(r["Total Units"] for r in inventory)
    booked = sum(r["Booked"] for r in inventory)
    unbooked = sum(r["Un-booked"] for r in inventory)

    cost, _cost_source = report_mod.project_cost(detail, area, form_two)

    qpr_filed = sum(
        1 for q in quarters
        if str(q.get("status") or "").upper() in ("SUBMITTED", "APPROVED", "ACCEPTED")
    )

    return {
        "name": core.get("projectName"),
        "regNo": core.get("projRegNo"),
        "taluka": detail.get("subDistName"),
        "type": core.get("projectType") or detail.get("projectType"),
        "endDate": fmt_date(detail.get("completionDate")),
        "progress": progress.get("score"),
        "cost": cost,
        "units": units,
        "booked": booked,
        "unbooked": unbooked,
        "qpr_filed": f"{qpr_filed}/{len(quarters)}",
        "docs_ok": len([d for d in bundle.get("documents") or [] if d.get("status") == "ok"]),
    }


# ------------------------------------------------------------------------------------ main ---
def run(targets_file: Path, output_root: Path, download_files: bool = True,
        limit: Optional[int] = None) -> List[Dict[str, Any]]:
    client = GujreraClient()
    targets = json.loads(targets_file.read_text(encoding="utf-8"))
    if limit:
        targets = targets[:limit]

    output_root.mkdir(parents=True, exist_ok=True)
    bundles: List[Dict[str, Any]] = []
    for index, target in enumerate(targets, start=1):
        if not target.get("pid"):
            resolved = resolve_target(client, target.get("regNo") or target.get("name") or "")
            if not resolved:
                log.error("Could not resolve target %s", target)
                continue
            target.update(resolved)
        log.info("[%d/%d] %s", index, len(targets), target.get("name") or target["pid"])
        try:
            bundles.append(scrape_project(client, target, output_root, download_files))
        except Exception as exc:  # noqa: BLE001
            log.exception("Project %s failed: %s", target.get("pid"), exc)

    if bundles:
        master = output_root / "AHMEDABAD_10_PROJECTS_REPORT.md"
        master.write_text(build_master_markdown(bundles), encoding="utf-8")
        log.info("Master report: %s", master)
        (output_root / "index.json").write_text(
            json.dumps(
                [{"folder": b["folder"], "markdown": b["markdown_name"], "summary": b["summary"]} for b in bundles],
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
    return bundles

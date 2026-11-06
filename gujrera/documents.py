"""
Document harvesting + download.

Every file attached to a GujRERA project is referenced by a VDMS UID (a long
base64-looking string) and served from  https://gujrera.gujarat.gov.in/vdms/download/<UID>.

Instead of hard-coding the couple of hundred document fields the portal uses, we walk
every JSON payload we downloaded and collect *any* value that looks like a VDMS UID,
keeping the field name as a human readable label. That way nothing is missed and new
fields added by GujRERA are picked up automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import r2_direct

log = logging.getLogger("gujrera.docs")

# Field-name -> readable label. Anything not listed here is prettified automatically.
LABELS: Dict[str, str] = {
    "certificateUid": "Registration Certificate (Form C)",
    "certificate": "Registration Certificate (Form C)",
    "altcertificateUid": "Alteration Certificate",
    "extcertificateUid": "Extension Certificate",
    "altsec15certificateUid": "Section 15 Alteration Certificate",
    "projectBroucherUId": "Project Brochure",
    "projectImageUId": "Project Photograph",
    "imageUid": "Project Photograph",
    "registrationCertificateUId": "Promoter Registration Certificate",
    "companyRegCertificateDocUId": "Promoter Company Registration Certificate",
    "panDocUId": "Promoter PAN Card",
    "individualPhotoDocUId": "Promoter Photograph",
    "authsignCerificateUId": "Authorised Signatory Certificate",
    "authsignPANColorPDFUId": "Authorised Signatory PAN (colour)",
    "authsignPhotoUId": "Authorised Signatory Photograph",
    "commencementCertificateUId": "Commencement Certificate",
    "photoUId": "Block Photograph",
    "elevationViewUID": "Block Elevation View",
    "leftSideViewUID": "Block Left Side View",
    "rightSideViewUId": "Block Right Side View",
    "backViewUId": "Block Back View",
    "floorPlanUId": "Block Floor Plan",
    "soilReportUid": "Soil Report",
    "passbookUid": "Collection Account Passbook",
    "pdfUid": "Form 5 (Annual Report) PDF",
    "formOnePdfUId": "Form 1 PDF",
    "sanctionedLayoutPlanDocUId": "Sanctioned Layout Plan",
    "approveLayoutPlanDocUId": "Approved Layout Plan",
    "approveBuildingPlanPlottingPlanUId": "Approved Building / Plotting Plan",
    "approveSacPlanDocUId": "Approved Service & Amenities Plan",
    "performaForSaleOfAgreementUId": "Performa for Sale Agreement",
    "performaOfAllotmentLetterDocUId": "Performa of Allotment Letter",
    "performaforSaledeedUId": "Performa of Sale Deed",
    "brochureOfCurrentProjectDocUId": "Brochure of Current Project",
    "allNOCsfromAuthoritiesUId": "NOCs from Authorities",
    "agreementFileDocUId": "Development Agreement",
    "landLocationFileDocUId": "Land Location / Title File",
    "titleClearanceCertificateUId": "Title Clearance Certificate",
    "titleReportUId": "Title Report",
    "encumbranceCertificateDocUId": "Encumbrance Certificate",
    "propertyCardId2": "Property Card",
    "propertyCard3UId": "Property Card (additional)",
    "declarationFormbDocUId": "Declaration Form B",
    "declarationFormB1UId": "Declaration Form B-1",
    "declarationFormB2UId": "Declaration Form B-2",
    "developmentAgreementFormB1": "Development Agreement Form B-1",
    "developmentAgreementFormB2": "Development Agreement Form B-2",
    "jointDevAgreementData": "Joint Development Agreement",
    "drainageAffidavitUid": "Drainage Affidavit",
    "ganttchartForm1AUId": "Gantt Chart (Form 1A)",
    "projectphotoUId": "Project Photograph",
    "projectSpecificDocUId": "Project Specific Document",
    "auditedBalSheetDoc1UId": "Audited Balance Sheet (latest)",
    "auditedBalSheetDoc2_UId": "Audited Balance Sheet (Y-1)",
    "auditedBalSheetDoc3UId": "Audited Balance Sheet (Y-2)",
    "auditedProfitLossSheetDoc1UId": "Audited Profit & Loss (latest)",
    "auditedProfitLossSheetDoc2UId": "Audited Profit & Loss (Y-1)",
    "auditedProfitLossSheetDoc3UId": "Audited Profit & Loss (Y-2)",
    "cashFlowStmtFileDoc1UId": "Cash Flow Statement (latest)",
    "cashFlowStmtFileDoc2UId": "Cash Flow Statement (Y-1)",
    "cashFlowStmtFileDoc3UId": "Cash Flow Statement (Y-2)",
    "directorReportDoc1UId": "Director's Report (latest)",
    "directorReportDoc2UId": "Director's Report (Y-1)",
    "directorReportDoc3UId": "Director's Report (Y-2)",
    "auditorsDoc1UId": "Auditor's Report (latest)",
    "auditorsDoc2UId": "Auditor's Report (Y-1)",
    "auditorsDoc3UId": "Auditor's Report (Y-2)",
    "incomeTaxReturn1UId": "Income Tax Return (latest)",
    "incomeTaxReturn2UId": "Income Tax Return (Y-1)",
    "incomeTaxReturn3UId": "Income Tax Return (Y-2)",
    "certificateUidBlock": "Block Certificate",
    "pdfArray": "Quarterly Report PDF",
    "formOneFormTwo": "Form 1 & Form 2 Combined PDF",
    # ---- Forms & quarterly filings ---------------------------------------------------
    "formTwoPdfUId": "Form 2 PDF",
    "formAnnxPdfUId": "Form 2 Annexure PDF",
    "formThreePdfUId": "Form 3 PDF",
    "formMofPdfUId": "MoF (Memorandum of Form) PDF",
    "fhotoUId": "Form 1-A Site Photograph",
    "data": "Form 8 (Annual Report) PDF",
    "qt_addidavit_docuid": "Affidavit-cum-Declaration",
    "qt_form_four_a_docuid": "Form 4 — Architect Certificate",
    "qt_revenue_docuid": "Revenue Record / Property Card",
    "qt_society_reg_docuid": "Society Registration Document",
    # ---- Associates / authorised signatory -------------------------------------------
    "assocaitePANColorPDFUId": "Associate PAN Card",
    "assocaitePhotoUId": "Associate Photograph",
    "assocaiteADHARPDFUId": "Associate Aadhaar",
    "assocaiteCertificateUId": "Associate Certificate",
    "authsignPanColorPDUId": "Authorised Signatory PAN Card",
    "authsignPhotUId": "Authorised Signatory Photograph",
    "authsignCerificatUId": "Authorised Signatory Authority Letter",
    "authsignAdharDoUId": "Authorised Signatory Aadhaar",
    # ---- Form 1-A progress photographs ----------------------------------------------
    "photoExternalUId": "Form 1-A Photo — External",
    "photoFlooringUId": "Form 1-A Photo — Flooring",
    "photoWallsUId": "Form 1-A Photo — Walls",
    "photoTerraceUId": "Form 1-A Photo — Terrace",
    "photoCommonUId": "Form 1-A Photo — Common Area",
    "photoApprovalsUId": "Form 1-A Photo — Approvals",
    # ---- Financial --------------------------------------------------------------------
    "loanUploadPDFUID": "Loan Sanction Document",
    "mortgageDeedPDFUId": "Mortgage Deed",
    "aggreimentUId": "Development Agreement",
    # ---- Statutory NOCs ---------------------------------------------------------------
    "airportDocUId": "NOC — Airport Authority",
    "railwayDocUId": "NOC — Railways",
    "environmentDocUId": "Environment Clearance",
    "fireDocUId": "Fire NOC",
    "highTensionLineDocUId": "NOC — High Tension Line",
    "gasLineDocUId": "NOC — Gas Pipeline",
    "highwaydocUId": "NOC — Highway Authority",
    "metrodocUId": "NOC — Metro Rail",
    "irrigationdepartmentdocUId": "NOC — Irrigation Department",
    "ashantDharodocUId": "NOC — Ashant Dharo",
    "ongcdocUId": "NOC — ONGC",
    "jaildocUId": "NOC — Jail Authority",
    "coastalRegulationZonedocUId": "NOC — Coastal Regulation Zone",
    "asiDocUId": "NOC — Archaeological Survey of India",
    "forestDocUId": "NOC — Forest Department",
    "nonAgriculturedocUId": "Non-Agricultural (NA) Order",
}

_UID_RE = re.compile(r"^[A-Za-z0-9]{55,200}$")


def is_uid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UID_RE.match(value.strip()))


def prettify(key: str) -> str:
    if key in LABELS:
        return LABELS[key]
    base = re.sub(r"(_?U[Ii][Dd]|_?UID|Uid)$", "", key)
    base = re.sub(r"_+$", "", base).replace("_", " ")
    # Split camelCase but keep acronym runs together ("PAN PDF", not "P A N P D F").
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", base)
    words = re.sub(r"\s{2,}", " ", words).strip()
    return words[:1].upper() + words[1:] if words else key


def label_index(pairs: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    """De-duplicate UIDs, keeping the most descriptive label for each."""
    best: Dict[str, str] = {}
    for label, uid in pairs:
        uid = (uid or "").strip()
        if not is_uid(uid):
            continue
        if len(label) > len(best.get(uid, "")):
            best[uid] = label
    return best


def harvest(obj: Any, prefix: str = "") -> List[Tuple[str, str]]:
    """Recursively collect (label, uid) pairs from any JSON structure."""
    found: List[Tuple[str, str]] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            label = prettify(key) if key.lower().endswith(("uid", "id")) or key in LABELS else key
            if is_uid(value):
                found.append((prettify(key), value.strip()))
            else:
                found.extend(harvest(value, prefix))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            if is_uid(item):
                label = f"{prefix} {index + 1}".strip()
                found.append((label, item.strip()))
            else:
                found.extend(harvest(item, prefix))
    return found


def safe_name(text: str, max_len: int = 90) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return (text[:max_len] or "document").strip(" .")


def extension_for(content_type: str, filename: str) -> str:
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()[:5]
    mapping = {
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "application/zip": "zip",
        "application/vnd.ms-excel": "xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "text/plain": "txt",
        "text/csv": "csv",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower(), "bin")


def _download_one(client, out_dir: Path, skip_existing: bool, lock: threading.Lock,
                   uid: str, label: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "label": label,
        "uid": uid,
        "url": client.url("download", uid=uid),
        "file": "",
        "bytes": 0,
        "sha256": "",
        "content_type": "",
        "status": "",
    }
    try:
        content, content_type, raw_name = client.get_document(uid)
        if not content:
            # The portal answers HTTP 200 with an empty body for some UIDs.
            # Writing that as a 0-byte "ok" file hides a missing document, so
            # record it as a failure instead and let the next run retry it.
            raise ValueError("empty response (0 bytes)")
        ext = extension_for(content_type, raw_name)
        nice = safe_name(raw_name.rsplit(".", 1)[0]) if raw_name else safe_name(label)
        filename = f"{nice}.{ext}"
        with lock:  # filesystem collision check must be atomic across worker threads
            target = out_dir / filename
            counter = 1
            while target.exists() and target.stat().st_size != len(content):
                target = out_dir / f"{nice} ({counter}).{ext}"
                counter += 1
            if not (skip_existing and target.exists()):
                target.write_bytes(content)
                # With direct-to-R2 on, the document is sent to the bucket now and
                # removed from disk, so a long crawl never fills the laptop.
                r2_key = r2_direct.offload(target)
                if r2_key:
                    row["r2_key"] = r2_key
                    row["storage"] = "r2"
        row.update(
            file=str(target.name),
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content_type=content_type.split(";")[0],
            status="ok",
        )
    except Exception as exc:  # noqa: BLE001 - a dead link must not kill the run
        row["status"] = f"failed: {exc}"
    return row


def _reuse_row(uid: str, label: str, known: Dict[str, Any], client) -> Dict[str, Any]:
    """Build a manifest row from state without touching the network.

    A UID is GujRERA's upload identity, so a UID we have already downloaded and
    uploaded cannot have changed. This is what turns a full re-download into a
    no-op for the ~98% of documents that are stable day to day.
    """
    return {
        "label": label,
        "uid": uid,
        "url": client.url("download", uid=uid),
        "file": known.get("file_name") or "",
        "bytes": known.get("bytes") or 0,
        "sha256": known.get("sha256") or "",
        "content_type": known.get("content_type") or "",
        "blob_key": known.get("blob_key") or "",
        "status": "ok",
        "reused": True,
    }


def download_all(
    client,
    uid_pairs: Iterable[Tuple[str, str]],
    out_dir: Path,
    skip_existing: bool = True,
    max_workers: int = 8,
    known: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Download every unique UID, in parallel. Returns the manifest rows.

    `known` maps uid -> a previously recorded document row. Any UID present with
    a successful status and a sha256 is reused as-is: no request, no bytes.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    seen = label_index(uid_pairs)
    total = len(seen)
    if not total:
        return []

    known = known or {}
    lock = threading.Lock()
    manifest: List[Dict[str, Any]] = []

    to_fetch: Dict[str, str] = {}
    for uid, label in seen.items():
        prior = known.get(uid)
        if prior and prior.get("status") == "ok" and prior.get("sha256") and prior.get("blob_key"):
            manifest.append(_reuse_row(uid, label, prior, client))
        else:
            to_fetch[uid] = label

    reused = len(manifest)
    pending = len(to_fetch)
    if reused:
        log.info("reusing %d already-captured documents, %d to fetch", reused, pending)

    if not pending:
        return manifest

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(_download_one, client, out_dir, skip_existing, lock, uid, label): label
            for uid, label in to_fetch.items()
        }
        for index, future in enumerate(as_completed(futures), start=1):
            label = futures[future]
            row = future.result()
            manifest.append(row)
            if row["status"] == "ok":
                log.info("[%d/%d] %-45s -> %s (%d KB)", index, pending, label[:45],
                         row["file"], row["bytes"] // 1024)
            else:
                log.warning("[%d/%d] %-45s -> FAILED (%s)", index, pending, label[:45], row["status"])
    return manifest


def write_manifest(manifest: List[Dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

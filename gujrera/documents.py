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

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

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


def download_all(
    client,
    uid_pairs: Iterable[Tuple[str, str]],
    out_dir: Path,
    skip_existing: bool = True,
) -> List[Dict[str, Any]]:
    """Download every unique UID once. Returns the manifest rows."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: List[Dict[str, Any]] = []
    seen = label_index(uid_pairs)

    total = len(seen)
    for index, (uid, label) in enumerate(seen.items(), start=1):
        row: Dict[str, Any] = {
            "label": label,
            "uid": uid,
            "url": client.url("download", uid=uid),
            "file": "",
            "bytes": 0,
            "content_type": "",
            "status": "",
        }
        try:
            content, content_type, raw_name = client.get_document(uid)
            ext = extension_for(content_type, raw_name)
            nice = safe_name(raw_name.rsplit(".", 1)[0]) if raw_name else safe_name(label)
            filename = f"{nice}.{ext}"
            target = out_dir / filename
            counter = 1
            while target.exists() and target.stat().st_size != len(content):
                target = out_dir / f"{nice} ({counter}).{ext}"
                counter += 1
            if not (skip_existing and target.exists()):
                target.write_bytes(content)
            row.update(
                file=str(target.name),
                bytes=len(content),
                content_type=content_type.split(";")[0],
                status="ok",
            )
            log.info("[%d/%d] %-45s -> %s (%d KB)", index, total, label[:45], target.name, len(content) // 1024)
        except Exception as exc:  # noqa: BLE001 - a dead link must not kill the run
            row["status"] = f"failed: {exc}"
            log.warning("[%d/%d] %-45s -> FAILED (%s)", index, total, label[:45], exc)
        manifest.append(row)
    return manifest


def write_manifest(manifest: List[Dict[str, Any]], path: Path) -> None:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

#!/usr/bin/env python3
"""Produce a register of every document that is not fit for client delivery.

Writes three artefacts into output/, so they are picked up by the normal R2 sync
and land next to the data they describe:

    output/BROKEN_DOCUMENTS.csv    one row per affected file, spreadsheet-ready
    output/BROKEN_DOCUMENTS.json   same rows plus full detail, for tooling
    output/BROKEN_DOCUMENTS.md     human summary + methodology note

Each row carries the VDMS `uid` and the direct portal `url`, so a broken document
can be re-fetched and re-checked later without re-scraping the project.

Severity
    broken      content is unusable as delivered (empty, truncated, not a document)
    mislabeled  content is valid but the extension disagrees with the bytes

Usage
    python make_broken_report.py
    python make_broken_report.py --verify-upstream   # re-fetch each one and record
                                                     # whether the portal matches
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUTPUT = REPO / "output"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "cloudflare"))

PORTAL = "https://gujrera.gujarat.gov.in/vdms/download"


def sniff(path: Path) -> str:
    head = path.open("rb").read(16)
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:5] == b"%PDF-":
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:5].lower() in (b"<!doc", b"<html"):
        return "html"
    return "unknown"


def classify(path: Path) -> tuple[str, str] | None:
    """Return (severity, issue) or None when the file is fine."""
    size = path.stat().st_size
    suffix = path.suffix.lower()

    if size == 0:
        return "broken", "empty file (portal returned 0 bytes)"

    if suffix in (".jpg", ".jpeg", ".jfif"):
        with path.open("rb") as fh:
            if fh.read(2) != b"\xff\xd8":
                return "mislabeled", f"declared image/jpeg but content is {sniff(path)}"
            fh.seek(max(0, size - 4096))
            if b"\xff\xd9" not in fh.read():
                return "broken", "truncated JPEG (no end-of-image marker)"
        return None

    if suffix == ".png":
        with path.open("rb") as fh:
            if fh.read(8) != b"\x89PNG\r\n\x1a\n":
                return "mislabeled", f"declared image/png but content is {sniff(path)}"
            fh.seek(max(0, size - 64))
            if b"IEND" not in fh.read():
                return "broken", "truncated PNG (no IEND chunk)"
        return None

    if suffix == ".pdf":
        with path.open("rb") as fh:
            head = fh.read(1024)
            fh.seek(max(0, size - 4096))
            tail = fh.read()
        if b"%PDF-" not in head:
            return "broken", f"not a PDF (content is {sniff(path)})"
        if b"%%EOF" not in tail:
            return "broken", "truncated PDF (no %%EOF trailer)"
        return None

    return None


def r2_key_for(path: Path) -> str:
    try:
        import r2_sync

        r2_sync.load_env(REPO / "cloudflare" / ".env")
        return r2_sync.destination(path)
    except Exception:  # noqa: BLE001 - the report must not fail on this
        return ""


def project_of(path: Path) -> tuple[str, str]:
    """Return (project folder path relative to output/, pid or '')."""
    rel = path.relative_to(OUTPUT)
    parts = rel.parts[:-2] if len(rel.parts) >= 3 else rel.parts[:1]
    project = Path(*parts).as_posix() if parts else ""
    name = parts[-1] if parts else ""
    pid = name.split("_", 1)[0] if name[:1].isdigit() else ""
    return project, pid


def main() -> int:
    parser = argparse.ArgumentParser(description="Register every non-deliverable document")
    parser.add_argument("--verify-upstream", action="store_true",
                        help="re-fetch each affected document and record whether the portal matches")
    args = parser.parse_args()

    rows: list[dict] = []
    files = [p for p in OUTPUT.rglob("*") if p.is_file()]
    for path in files:
        rel = path.relative_to(OUTPUT).as_posix()
        if "raw" in rel.split("/")[:-1]:
            continue
        verdict = classify(path)
        if not verdict:
            continue
        severity, issue = verdict
        project, pid = project_of(path)
        rows.append({
            "project": project,
            "pid": pid,
            "label": "",
            "file": path.name,
            "r2_key": r2_key_for(path),
            "bytes": path.stat().st_size,
            "severity": severity,
            "issue": issue,
            "uid": "",
            "url": "",
            "portal_matches_disk": "",
            "verified_at": "",
        })

    # enrich with manifest metadata (label, uid)
    for row in rows:
        manifest = OUTPUT / row["project"] / "documents_manifest.json"
        if not manifest.exists():
            continue
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            continue
        match = next((e for e in entries if e.get("file") == row["file"]), None)
        if match:
            row["label"] = match.get("label", "")
            row["uid"] = match.get("uid", "")
            row["url"] = match.get("url") or (f"{PORTAL}/{row['uid']}" if row["uid"] else "")

    if args.verify_upstream and rows:
        print(f"re-fetching {len(rows)} document(s) to check the portal ...")
        from gujrera.api import GujreraClient

        client = GujreraClient()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i, row in enumerate(rows, 1):
            if not row["uid"]:
                continue
            try:
                content, _, _ = client.get_document(row["uid"])
            except Exception:  # noqa: BLE001
                row["portal_matches_disk"] = "fetch-failed"
                continue
            disk = (OUTPUT / row["project"] / "files" / row["file"]).read_bytes()
            row["portal_matches_disk"] = (
                "yes" if hashlib.md5(content).hexdigest() == hashlib.md5(disk).hexdigest() else "no"
            )
            row["verified_at"] = stamp
            print(f"  {i}/{len(rows)}  {row['portal_matches_disk']:4}  {row['file'][:60]}")

    rows.sort(key=lambda r: (r["severity"], r["project"], r["file"]))

    fieldnames = ["severity", "issue", "project", "pid", "label", "file", "bytes",
                  "uid", "url", "r2_key", "portal_matches_disk", "verified_at"]
    csv_path = OUTPUT / "BROKEN_DOCUMENTS.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    (OUTPUT / "BROKEN_DOCUMENTS.json").write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "portal": PORTAL,
        "counts": {
            "total": len(rows),
            "broken": sum(1 for r in rows if r["severity"] == "broken"),
            "mislabeled": sum(1 for r in rows if r["severity"] == "mislabeled"),
        },
        "documents": rows,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    broken = [r for r in rows if r["severity"] == "broken"]
    mislabeled = [r for r in rows if r["severity"] == "mislabeled"]
    issues: dict[str, int] = {}
    for row in rows:
        issues[row["issue"]] = issues.get(row["issue"], 0) + 1
    projects: dict[str, int] = {}
    for row in broken:
        projects[row["project"]] = projects.get(row["project"], 0) + 1
    upstream_yes = sum(1 for r in rows if r["portal_matches_disk"] == "yes")

    md = [
        "# Documents not fit for client delivery",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%d-%m-%Y %H:%M UTC')} "
        f"from a full scan of `output/`.",
        "",
        "## Summary",
        "",
        "| | Count |",
        "|---|---|",
        f"| **Broken** (unusable as delivered) | **{len(broken)}** |",
        f"| Mislabeled (valid content, wrong extension) | {len(mislabeled)} |",
        f"| Total in register | {len(rows)} |",
        "",
    ]
    if upstream_yes:
        md += [
            f"{upstream_yes} of these were re-fetched from the portal and returned "
            "**byte-identical** content, so the defect is at the source, not in this archive.",
            "",
        ]
    md += ["## Issues by type", "", "| Issue | Count |", "|---|---|"]
    md += [f"| {issue} | {count} |" for issue, count in sorted(issues.items(), key=lambda kv: -kv[1])]
    md += ["", "## Broken documents by project", "", "| Project | Count |", "|---|---|"]
    md += [f"| `{proj}` | {count} |" for proj, count in sorted(projects.items(), key=lambda kv: -kv[1])]
    md += [
        "",
        "## Method",
        "",
        "Every file under `output/` (excluding `raw/`) was read and checked against its format:",
        "",
        "- **PDF** — `%PDF-` must appear in the first 1024 bytes (the spec permits leading bytes),"
        " and `%%EOF` in the last 4096 bytes or the file is truncated.",
        "- **JPEG** — must start with the SOI marker `FFD8` and end with the EOI marker `FFD9`.",
        "- **PNG** — must start with the PNG signature and end with an `IEND` chunk.",
        "- **Zero bytes** — treated as broken regardless of type.",
        "",
        "Each row records the VDMS `uid` and the direct portal `url`, so a document can be",
        "re-fetched and re-testable later without re-scraping the project:",
        "",
        "```",
        f"{PORTAL}/<uid>",
        "```",
        "",
        "Re-check any of them with:",
        "",
        "```powershell",
        "python verify_upstream.py      # re-fetch and compare against disk",
        "python audit_output.py         # re-scan everything",
        "```",
        "",
        "_`verify_upstream.py` prints, per file, whether the portal returns the same bytes._",
        "",
    ]
    (OUTPUT / "BROKEN_DOCUMENTS.md").write_text("\n".join(md), encoding="utf-8")

    print(f"\nbroken     : {len(broken)}")
    print(f"mislabeled : {len(mislabeled)}")
    print(f"total      : {len(rows)}")
    print(f"\nwritten:")
    for name in ("BROKEN_DOCUMENTS.csv", "BROKEN_DOCUMENTS.json", "BROKEN_DOCUMENTS.md"):
        target = OUTPUT / name
        print(f"  {target.relative_to(REPO)}  ({target.stat().st_size / 1024:,.1f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

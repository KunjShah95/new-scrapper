#!/usr/bin/env python3
"""Register documents that were fetched outside the delta engine into state.

`repair_documents.py --gaps` (and any other tool that writes a manifest without
going through `delta.py`) fills documents into `output/` and uploads them to R2,
but it does not write to `state/gujrera.db`. Without this step the next delta
run treats those UIDs as never-seen and downloads them again -- exactly the
waste this project exists to avoid.

What it does, per project folder under `output/`:
  * reads `documents_manifest.json`
  * derives the R2 key for every `status == ok` row (explicit `r2_key` /
    `blob_key` first, then the path-based key the uploader/`r2_sync.py` uses)
  * upserts the row into `documents` as captured + uploaded, so
    `known_documents()` reuses it forever with zero bytes
  * records a `document_versions` entry when the UID is new for that label

Read-only unless `--apply` is passed. Safe to run repeatedly; existing rows are
kept, never deleted.

Usage
    python register_documents.py --dry-run
    python register_documents.py --apply
    python register_documents.py --apply --district AHMEDABAD
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Optional

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from gujrera.legacy import route_key  # noqa: E402
from gujrera.state import State  # noqa: E402
from gujrera import r2_direct  # noqa: E402

log = logging.getLogger("register_documents")
OUTPUT = REPO / "output"


def pid_of(folder: Path) -> Optional[int]:
    head = folder.name.split("_", 1)[0]
    return int(head) if head.isdigit() else None


def r2_key_for(folder: Path, row: Dict, pdf_prefix: str, json_prefix: str) -> str:
    key = row.get("r2_key") or row.get("blob_key")
    if key:
        return str(key)
    filename = row.get("file") or ""
    if not filename:
        return ""
    target = folder / "files" / filename
    if target.exists():
        # Same key the uploader and r2_sync.py would produce, prefix included.
        key = r2_direct.key_for(target)
        if key:
            return key
    try:
        relative = target.relative_to(OUTPUT).as_posix()
    except ValueError:
        return ""
    return route_key(relative, pdf_prefix, json_prefix)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write rows to state")
    parser.add_argument("--dry-run", action="store_true", help="report only (default)")
    parser.add_argument("--district", default="", help="only this output sub-folder")
    parser.add_argument("--state", default=str(REPO / "state" / "gujrera.db"))
    parser.add_argument("--pdf-prefix", default="")
    parser.add_argument("--json-prefix", default="")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("choose --apply or --dry-run, not both")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    state = State(args.state)
    roots = sorted(OUTPUT.iterdir()) if OUTPUT.exists() else []
    prefixes_known = bool(args.pdf_prefix)
    totals = {"projects": 0, "rows": 0, "registered": 0, "skipped": 0,
              "no_key": 0, "no_pid": 0, "bytes": 0}

    for root in roots:
        if not root.is_dir():
            continue
        if args.district and root.name.upper() != args.district.upper():
            continue
        for manifest in sorted(root.rglob("documents_manifest.json")):
            folder = manifest.parent
            if "raw" in manifest.relative_to(root).parts[:-1]:
                continue
            pid = pid_of(folder)
            if pid is None:
                totals["no_pid"] += 1
                continue
            if state.get_project(pid) is None:
                totals["no_pid"] += 1
                continue
            try:
                rows = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rows, list):
                continue
            prefix = args.pdf_prefix
            if not prefixes_known:
                prefix = ""
            totals["projects"] += 1
            for row in rows:
                if not isinstance(row, dict) or not row.get("uid"):
                    continue
                if row.get("status") != "ok":
                    continue
                totals["rows"] += 1
                key = r2_key_for(folder, row, prefix, args.json_prefix)
                if not key:
                    totals["no_key"] += 1
                    continue
                if state.get_project(pid) is None:
                    continue
                if args.apply:
                    state.upsert_document(pid, {
                        "uid": row["uid"],
                        "label": row.get("label") or "",
                        "file": row.get("file") or "",
                        "bytes": row.get("bytes") or 0,
                        "content_type": row.get("content_type") or "",
                        "sha256": row.get("sha256") or None,
                        "blob_key": key,
                        "status": "ok",
                    }, uploaded=True)
                    state.record_version(pid, row.get("label") or "", row["uid"],
                                         row.get("sha256"))
                totals["registered"] += 1
                totals["bytes"] += int(row.get("bytes") or 0)

    verb = "registered" if args.apply else "would register"
    print("\n--- summary ---")
    print(f"  project folders scanned : {totals['projects']:,}")
    print(f"  ok manifest rows        : {totals['rows']:,}")
    print(f"  rows {verb:<17}: {totals['registered']:,}"
          f"  ({totals['bytes'] / 2**30:,.2f} GiB)")
    print(f"  rows without an R2 key  : {totals['no_key']:,}")
    print(f"  folders with no project : {totals['no_pid']:,}")
    if not args.apply:
        print("  (dry run - state untouched; re-run with --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

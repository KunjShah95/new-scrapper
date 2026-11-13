#!/usr/bin/env python3
"""Delete local document files that are provably already in R2.

For every file under output/**/files/ it looks for the object in R2 in both
layouts the pipeline uses:

    path key     <R2_PDF_PREFIX or R2_JSON_PREFIX>/<path under output/>   (r2_sync / --direct-r2)
    content key  <R2_PDF_PREFIX>/blobs/ab/cd/<sha256>                    (delta engine)

A file is deleted only when one of those objects exists with exactly the same
size. Anything else (missing, size differs, in-flight .part files) is kept and
counted as "still to upload". raw/ JSON and manifests are never touched: the
delta engine diffs against raw/, and the legacy importer reads the manifests.

Read-only unless --apply.

Usage
    python prune_uploaded.py            # report
    python prune_uploaded.py --apply    # delete the verified copies
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gujrera.sink import blob_key, make_sink  # noqa: E402

REPO = Path(__file__).resolve().parent
OUTPUT = REPO / "output"
BINARY_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".jfif", ".webp", ".gif", ".zip", ".tif", ".tiff"}


def gb(n: float) -> str:
    return f"{n / 1e9:,.2f} GB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="delete verified local copies")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sink = make_sink("r2")
    started = time.time()
    remote = {}
    for prefix in sorted({sink.pdf_prefix, sink.json_prefix}):
        for page in sink.client.get_paginator("list_objects_v2").paginate(
                Bucket=sink.bucket, Prefix=prefix + "/"):
            for obj in page.get("Contents") or []:
                remote[obj["Key"]] = obj["Size"]
    print(f"R2: {len(remote):,} objects, {gb(sum(remote.values()))} "
          f"(listed in {time.time() - started:,.0f}s)", flush=True)

    stats = collections.Counter()
    pending_by_project = collections.Counter()
    pending_examples = []
    for files_dir in OUTPUT.rglob("files"):
        if not files_dir.is_dir() or "raw" in files_dir.parts:
            continue
        project = files_dir.parent
        sha_by_name = {}
        manifest = project / "documents_manifest.json"
        if manifest.exists():
            try:
                for row in json.loads(manifest.read_text(encoding="utf-8")):
                    if isinstance(row, dict) and row.get("file") and row.get("sha256"):
                        sha_by_name[row["file"]] = row["sha256"]
            except (OSError, ValueError):
                pass
        for path in files_dir.iterdir():
            if not path.is_file():
                continue
            if path.name.endswith(".part"):
                stats["in_flight"] += 1
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue  # removed by a running job mid-scan
            rel = path.relative_to(OUTPUT).as_posix()
            base = sink.pdf_prefix if path.suffix.lower() in BINARY_EXT else sink.json_prefix
            candidates = [f"{base}/{rel}"]
            if path.name in sha_by_name:
                candidates.append(f"{sink.pdf_prefix}/{blob_key(sha_by_name[path.name])}")
            if any(remote.get(key) == size for key in candidates):
                stats["uploaded_files"] += 1
                stats["uploaded_bytes"] += size
                if args.apply:
                    try:
                        path.unlink()
                        stats["deleted_files"] += 1
                        stats["deleted_bytes"] += size
                    except OSError:
                        stats["delete_failed"] += 1
            else:
                stats["pending_files"] += 1
                stats["pending_bytes"] += size
                pending_by_project[project.relative_to(OUTPUT).as_posix()] += size
                if len(pending_examples) < 10:
                    reason = "size differs" if any(k in remote for k in candidates) else "not in R2"
                    pending_examples.append(f"{rel}  ({size / 1e6:,.1f} MB, {reason})")

    print(f"\nlocal documents already in R2 (same size) : {stats['uploaded_files']:,} files, "
          f"{gb(stats['uploaded_bytes'])}")
    print(f"local documents NOT yet in R2             : {stats['pending_files']:,} files, "
          f"{gb(stats['pending_bytes'])}  in {len(pending_by_project):,} project(s)")
    print(f"in-flight downloads (.part)               : {stats['in_flight']:,}")
    if args.apply:
        print(f"deleted                                   : {stats['deleted_files']:,} files, "
              f"{gb(stats['deleted_bytes'])}  (failed: {stats['delete_failed']:,})")
    else:
        print("(report only - re-run with --apply to delete the verified copies)")
    for line in pending_examples:
        print("  pending:", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

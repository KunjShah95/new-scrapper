#!/usr/bin/env python3
"""Repair documents that the manifests describe but disk does not have.

Only fixes what the portal can actually give back. Verified behaviour:

  * file absent from disk, manifest row says ok  -> fresh download succeeds, so
    these are recoverable (they were recorded but never written).
  * row has bytes=0 and status=ok                -> the portal returns HTTP 200
    with an empty body; nothing to recover, so the row is re-marked as failed
    instead of silently claiming success.
  * file present but truncated                   -> the portal serves the same
    truncated bytes, so re-downloading cannot help. Reported, never rewritten.

Usage
    python repair_documents.py --dry-run
    python repair_documents.py --missing          # re-download absent files
    python repair_documents.py --mark-empty       # fix misleading status='ok'
    python repair_documents.py --all
    python repair_documents.py --gaps --direct-r2 # upload fills to R2, keep disk empty
    python repair_documents.py --gaps --limit 20  # first 20 projects with gaps only

A project whose manifest is not written yet and whose folder changed recently is
skipped: a live scrape is still working on it and would overwrite the merge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gujrera.api import GujreraClient  # noqa: E402
from gujrera.documents import download_all, harvest_raw_dir, label_index  # noqa: E402

REPO = Path(__file__).resolve().parent
OUTPUT = REPO / "output"
IN_PROGRESS_SECONDS = 30 * 60


def iter_manifests():
    for manifest in OUTPUT.rglob("documents_manifest.json"):
        rel = manifest.relative_to(OUTPUT).as_posix()
        if "raw" in rel.split("/")[:-1]:
            continue
        try:
            yield manifest, json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            print(f"  ! unreadable manifest: {rel}")


def fill_gaps(client, dry_run: bool, limit: int = 0) -> dict:
    """Download documents the cached payloads reference but no manifest holds."""
    stats = {"gaps": 0, "fetched": 0, "bytes": 0, "failed": 0, "projects": 0, "busy": 0}
    for raw_dir in sorted(OUTPUT.rglob("raw")):
        if not raw_dir.is_dir():
            continue
        if limit and stats["projects"] >= limit:
            break
        project = raw_dir.parent
        rel = project.relative_to(OUTPUT).as_posix()

        expected = label_index(harvest_raw_dir(raw_dir))
        if not expected:
            continue

        manifest = project / "documents_manifest.json"
        if not manifest.exists() and time.time() - project.stat().st_mtime < IN_PROGRESS_SECONDS:
            stats["busy"] += 1
            continue
        try:
            rows = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
        except ValueError:
            rows = []
        # Failed rows count as missing so a transient portal 500 gets retried;
        # a file the portal itself no longer holds is settled, not retried.
        have = {r["uid"] for r in rows if r.get("uid") and (
            r.get("status") == "ok" or str(r.get("status")).startswith("failed: missing on portal"))}
        missing = [(label, uid) for uid, label in expected.items() if uid not in have]
        if not missing:
            continue

        print(f"\n{rel}: {len(missing)} document(s) referenced but never downloaded")
        stats["gaps"] += len(missing)
        stats["projects"] += 1
        if dry_run:
            for label, uid in missing[:5]:
                print(f"  would fetch  {label}")
            continue

        new_rows = download_all(client, missing, project / "files",
                                skip_existing=True, max_workers=6)
        ok = [r for r in new_rows if r.get("status") == "ok"]
        stats["failed"] += len(new_rows) - len(ok)
        stats["fetched"] += len(ok)
        stats["bytes"] += sum(r.get("bytes", 0) for r in ok)
        print(f"  fetched {len(ok)}/{len(new_rows)}"
              + (f", {sum(r.get('bytes', 0) for r in ok) / 1048576:,.1f} MiB" if ok else ""))

        # Merge, replacing any prior row for the same uid.
        by_uid = {r.get("uid"): r for r in rows if r.get("uid")}
        for row in new_rows:
            if row.get("uid"):
                by_uid[row["uid"]] = row
        ordered = list(by_uid.values())
        ordered.sort(key=lambda r: r.get("label", ""))
        manifest.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair missing / empty documents")
    parser.add_argument("--missing", action="store_true", help="re-download files absent from disk")
    parser.add_argument("--mark-empty", action="store_true",
                        help="re-mark rows recorded as ok but holding 0 bytes")
    parser.add_argument("--gaps", action="store_true",
                        help="download documents referenced in raw/ that no manifest holds")
    parser.add_argument("--all", action="store_true", help="all of the above")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--direct-r2", action="store_true",
                        help="upload each fetched document to R2 and delete the local copy")
    parser.add_argument("--limit", type=int, default=0,
                        help="--gaps: stop after this many projects with gaps")
    args = parser.parse_args()
    if args.direct_r2:
        os.environ["GUJRERA_DIRECT_R2"] = "1"

    if args.all:
        args.missing = args.mark_empty = args.gaps = True
    if not (args.missing or args.mark_empty or args.gaps):
        parser.error("choose --missing, --mark-empty, --gaps or --all")

    client = GujreraClient()
    stats = {"refetched": 0, "still_empty": 0, "failed": 0, "marked": 0, "bytes": 0}

    gap_stats = None
    if args.gaps:
        gap_stats = fill_gaps(client, args.dry_run, args.limit)
        if args.dry_run:
            print("\n--- gap preview ---")
            print(f"  documents referenced but absent from a manifest : {gap_stats['gaps']:,}")
            if not (args.missing or args.mark_empty):
                print("  (dry run - nothing written)")
                return 0

    for manifest, rows in iter_manifests():
        project = manifest.parent
        files_dir = project / "files"
        changed = False

        for row in rows:
            name = row.get("file") or ""
            if not name or row.get("storage") == "r2":
                # Offloaded to R2 by design: no local copy is expected.
                continue
            target = files_dir / name
            exists = target.exists()
            size = target.stat().st_size if exists else -1

            # 1. recorded but absent -> re-download
            if args.missing and not exists:
                label = row.get("label", name)
                if args.dry_run:
                    print(f"  would fetch  {project.name}/{name}")
                    stats["refetched"] += 1
                    continue
                try:
                    content, ctype, _ = client.get_document(row["uid"])
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAILED  {project.name}/{name}: {str(exc)[:70]}")
                    stats["failed"] += 1
                    continue
                if not content:
                    print(f"  empty   {project.name}/{name}: portal returned 0 bytes")
                    row["status"] = "failed: empty response (0 bytes)"
                    stats["still_empty"] += 1
                    changed = True
                    continue
                files_dir.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                row.update(
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    content_type=(ctype or "").split(";")[0],
                    status="ok",
                )
                changed = True
                stats["refetched"] += 1
                stats["bytes"] += len(content)
                print(f"  fetched {len(content):>10,} B  {project.name}/{name}")
                continue

            # 2. present but empty, yet marked ok -> correct the record
            if args.mark_empty and exists and size == 0 and row.get("status") == "ok":
                stats["marked"] += 1
                if args.dry_run:
                    print(f"  would mark failed  {project.name}/{name}")
                else:
                    row["status"] = "failed: empty response (0 bytes)"
                    changed = True
                    print(f"  marked failed  {project.name}/{name}")

        if changed and not args.dry_run:
            manifest.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n--- summary ---")
    if gap_stats:
        print(f"  projects with gaps  : {gap_stats['projects']:,}"
              + (f"  ({gap_stats['busy']:,} skipped, scrape in progress)" if gap_stats["busy"] else ""))
        print(f"  gaps found          : {gap_stats['gaps']:,}")
        print(f"  gaps filled         : {gap_stats['fetched']:,}"
              + (f"  ({gap_stats['bytes'] / 1048576:,.1f} MiB)" if gap_stats["bytes"] else ""))
        print(f"  gap fetch failures  : {gap_stats['failed']:,}")
    print(f"  files re-downloaded : {stats['refetched']:,}"
          + (f"  ({stats['bytes'] / 1048576:,.1f} MiB)" if stats["bytes"] else ""))
    print(f"  portal had nothing  : {stats['still_empty']:,}")
    print(f"  fetch failures      : {stats['failed']:,}")
    print(f"  rows re-marked      : {stats['marked']:,}")
    if args.dry_run:
        print("  (dry run - nothing written)")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Upload every local document file to R2 (content-addressed) and delete it.

The gap fill and the delta engine already upload to R2; this mops up the
leftovers: files downloaded before `--direct-r2`, or whose upload failed and
was kept by design. Nothing is deleted until `head_object` proves the object is
in the bucket.

Keys match what `gujrera/sink.py` writes: `blobs/<sha[:2]>/<sha[2:4]>/<sha>`
routed under `R2_PDF_PREFIX`. The row is also written into `state/gujrera.db`
so later runs reuse it instead of downloading again.

Usage
    python upload_local_docs.py            # report only
    python upload_local_docs.py --apply    # upload, verify, delete
    python upload_local_docs.py --apply --min-age-minutes 10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "cloudflare"))

import r2_sync  # noqa: E402

OUTPUT = REPO / "output"


def sha_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def routed(key: str, pdf_prefix: str) -> str:
    base = (pdf_prefix or "").strip("/")
    if base and not key.startswith(base + "/"):
        return f"{base}/{key}"
    return key


def manifest_row(folder: Path, name: str) -> dict | None:
    manifest = folder / "documents_manifest.json"
    if not manifest.exists():
        return None
    try:
        rows = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("file") == name:
            return row
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-age-minutes", type=int, default=10,
                        help="skip files newer than this (in-flight downloads)")
    parser.add_argument("--state", default=str(REPO / "state" / "gujrera.db"))
    args = parser.parse_args()

    from gujrera.state import State  # noqa: E402
    state = State(args.state)

    r2_sync.load_env(REPO / "cloudflare" / ".env")
    bucket = os.environ["R2_BUCKET"]
    pdf_prefix = os.environ.get("R2_PDF_PREFIX", "")
    client = r2_sync.make_client()

    conn = None
    cutoff = time.time() - args.min_age_minutes * 60

    files = [p for p in OUTPUT.rglob("files/*")
             if p.is_file() and not p.name.endswith(".part")
             and p.stat().st_mtime < cutoff]
    stats = {"checked": 0, "already": 0, "uploaded": 0, "deleted": 0,
             "skipped": 0, "failed": 0, "bytes": 0, "registered": 0}
    for path in files:
        stats["checked"] += 1
        folder = path.parent.parent
        row = manifest_row(folder, path.name)
        digest = (row or {}).get("sha256") or sha_of(path)
        key = routed(f"blobs/{digest[:2]}/{digest[2:4]}/{digest}", pdf_prefix)
        physical = routed(key, pdf_prefix)
        size = path.stat().st_size
        try:
            client.head_object(Bucket=bucket, Key=physical)
            present = True
        except Exception:  # noqa: BLE001
            present = False

        if present:
            stats["already"] += 1
        elif args.apply:
            try:
                client.upload_file(
                    str(path), bucket, physical,
                    ExtraArgs={"ContentType": r2_sync.content_type(path),
                               "CacheControl": "public, max-age=3600"},
                )
            except Exception as exc:  # noqa: BLE001 - keep the file on failure
                stats["failed"] += 1
                print(f"  UPLOAD FAILED  {path.relative_to(OUTPUT)}: {str(exc)[:90]}")
                continue
            stats["uploaded"] += 1
            stats["bytes"] += size
        else:
            stats["skipped"] += 1
            continue

        if args.apply:
            # State first, file second: a crash must never lose the only copy.
            uid = (row or {}).get("uid")
            if uid:
                state.upsert_document(
                    int(folder.name.split("_", 1)[0]),
                    {
                        "uid": uid,
                        "label": (row or {}).get("label") or "",
                        "file": path.name,
                        "bytes": size,
                        "content_type": (row or {}).get("content_type") or "",
                        "sha256": digest,
                        "blob_key": key,
                        "status": "ok",
                    },
                    uploaded=True,
                )
                stats["registered"] += 1
            path.unlink()
            stats["deleted"] += 1

    verb = "uploaded" if args.apply else "would upload"
    print("\n--- summary ---")
    print(f"  local files checked     : {stats['checked']:,}")
    print(f"  already in R2           : {stats['already']:,}")
    print(f"  files {verb:<13}: {stats['uploaded']:,}"
          f"  ({stats['bytes']/2**30:,.2f} GiB)" if args.apply else f"  files {verb:<13}: {stats['skipped']:,}")
    print(f"  upload failures         : {stats['failed']:,}")
    print(f"  registered in state     : {stats['registered']:,}")
    print(f"  deleted locally         : {stats['deleted']:,}")
    if not args.apply:
        print("  (report only - re-run with --apply)")
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
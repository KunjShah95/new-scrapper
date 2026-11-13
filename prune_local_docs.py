#!/usr/bin/env python3
"""Delete local document copies that are already safely stored in R2.

Every document row in `state/gujrera.db` that carries a `blob_key` was uploaded
before the row was written, so the object is supposed to exist. This script
proves it (`head_object`) before deleting anything -- a missing object is
reported and the local file is kept.

Only files under `output/**/files/` are considered. `raw/` payloads and the
manifests stay local: rebuilding a report needs them and they are small.

Usage
    python prune_local_docs.py            # report only
    python prune_local_docs.py --apply    # verify each object, then delete
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "cloudflare"))

import r2_sync  # noqa: E402

OUTPUT = REPO / "output"


def key_candidates(key: str, pdf_prefix: str, json_prefix: str) -> list[str]:
    """Internal keys are routed under a prefix at the storage boundary."""
    out = [key]
    for base in (pdf_prefix, json_prefix):
        base = (base or "").strip("/")
        if base and not key.startswith(base + "/"):
            out.append(f"{base}/{key}")
    return out


def object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


def pid_of(folder: Path) -> int | None:
    head = folder.name.split("_", 1)[0]
    return int(head) if head.isdigit() else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete verified local copies")
    parser.add_argument("--state", default=str(REPO / "state" / "gujrera.db"))
    args = parser.parse_args()

    r2_sync.load_env(REPO / "cloudflare" / ".env")
    bucket = os.environ["R2_BUCKET"]
    client = r2_sync.make_client()

    conn = sqlite3.connect(args.state)
    conn.row_factory = sqlite3.Row
    index: dict[tuple[int, str], dict] = {}
    for row in conn.execute("select pid, file_name, blob_key from documents "
                            "where blob_key is not null and blob_key <> ''"):
        if row["file_name"]:
            index[(int(row["pid"]), row["file_name"])] = dict(row)

    files = [p for p in OUTPUT.rglob("files/*")
             if p.is_file() and not p.name.endswith(".part")]
    stats = {"checked": 0, "verified": 0, "missing": 0, "no_row": 0,
             "deleted": 0, "bytes": 0}
    for path in files:
        folder = path.parent.parent
        pid = pid_of(folder)
        stats["checked"] += 1
        row = index.get((pid, path.name)) if pid is not None else None
        if not row:
            stats["no_row"] += 1
            continue
        key = row["blob_key"]
        candidates = key_candidates(key, os.environ.get("R2_PDF_PREFIX", ""),
                                    os.environ.get("R2_JSON_PREFIX", ""))
        if not any(object_exists(client, bucket, k) for k in candidates):
            stats["missing"] += 1
            print(f"  MISSING IN R2  {candidates[-1]}  <-- keeping {path}")
            continue
        stats["verified"] += 1
        size = path.stat().st_size
        if args.apply:
            path.unlink()
            stats["deleted"] += 1
            stats["bytes"] += size
            print(f"  deleted        {path.relative_to(OUTPUT)}  ({size/2**20:,.1f} MiB)")

    verb = "deleted" if args.apply else "would delete"
    print("\n--- summary ---")
    print(f"  local files checked     : {stats['checked']:,}")
    print(f"  verified present in R2  : {stats['verified']:,}")
    print(f"  missing in R2 (kept)    : {stats['missing']:,}")
    print(f"  no state row (kept)     : {stats['no_row']:,}")
    print(f"  files {verb:<15}: {stats['deleted']:,}"
          f"  ({stats['bytes']/2**30:,.2f} GiB)")
    if not args.apply:
        print("  (report only - re-run with --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
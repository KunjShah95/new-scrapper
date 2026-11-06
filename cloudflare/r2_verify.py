#!/usr/bin/env python3
"""Prove that what landed in R2 is bit-identical to what is on disk.

No downloads. Each object's ETag is recomputed locally from the source file and
compared with what R2 reports:

  * objects up to the multipart threshold -> ETag is the MD5 of the file
  * larger objects -> ETag is the S3 multipart form: md5(concat(part md5s)) + "-<parts>"

Since `r2_sync.py` uploads with a fixed 16 MiB part size, the composite value is
reproducible locally, so a match proves the bytes are identical.

Usage
-----
    python cloudflare/r2_verify.py --sample 500   # spot check while uploading
    python cloudflare/r2_verify.py --all          # full sweep after the upload
    python cloudflare/r2_verify.py --prefix main_pdfs/ircms/gujrera/
    python cloudflare/r2_verify.py --all --report cloudflare/verify.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from r2_sync import (  # noqa: E402
    CHUNK,
    MULTIPART_AFTER,
    REPO,
    collect,
    human,
    load_env,
    make_client,
)

STATE = REPO / "cloudflare" / "r2_state.json"


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def expected_etag(path: Path) -> str:
    """Recompute the ETag R2 should hold for this file."""
    size = path.stat().st_size
    if size <= MULTIPART_AFTER:
        return md5_file(path)
    parts = 0
    joined = hashlib.md5()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            parts += 1
            joined.update(hashlib.md5(block).digest())
    return f"{joined.hexdigest()}-{parts}"


def uploaded_keys() -> set[str]:
    if not STATE.exists():
        return set()
    try:
        return set(json.loads(STATE.read_text(encoding="utf-8")).keys())
    except ValueError:
        return set()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify R2 objects against local files")
    parser.add_argument("--env-file", default=str(REPO / "cloudflare" / ".env"))
    parser.add_argument("--sample", type=int, default=0, help="check N random uploaded objects")
    parser.add_argument("--all", action="store_true", help="check every object")
    parser.add_argument("--prefix", default=None, help="restrict to one R2 prefix")
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--report", default=None, help="write a JSON report here")
    parser.add_argument("--exclude", nargs="*", default=[], metavar="GLOB")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    client = make_client()
    bucket = os.environ["R2_BUCKET"]

    pairs = collect([], args.exclude, include_raw=False)
    if args.prefix:
        pairs = [(p, k) for p, k in pairs if k.startswith(args.prefix.strip("/") + "/")]

    if not args.all:
        done = uploaded_keys()
        candidates = [(p, k) for p, k in pairs if k in done]
        if not candidates:
            sys.exit("nothing uploaded yet for that prefix")
        n = args.sample or 200
        pairs = random.sample(candidates, min(n, len(candidates)))
        print(f"spot check: {len(pairs):,} random objects drawn from {len(candidates):,} uploaded")
    else:
        print(f"full sweep: {len(pairs):,} objects")

    results = {"ok": 0, "missing": 0, "size_mismatch": 0, "etag_mismatch": 0, "error": 0}
    bad: list[dict] = []
    lock = threading.Lock()
    started = time.monotonic()

    def check(path: Path, key: str) -> tuple[str, dict | None]:
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - classify, do not crash the sweep
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return "missing", {"key": key}
            return "error", {"key": key, "detail": str(exc).splitlines()[0][:160]}

        remote_size = head.get("ContentLength")
        local_size = path.stat().st_size
        if remote_size != local_size:
            return "size_mismatch", {"key": key, "local": local_size, "remote": remote_size}

        remote_etag = (head.get("ETag") or "").strip('"')
        local_etag = expected_etag(path)
        if remote_etag and remote_etag != local_etag:
            return "etag_mismatch", {"key": key, "local": local_etag, "remote": remote_etag}
        return "ok", None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check, p, k): k for p, k in pairs}
        for i, future in enumerate(as_completed(futures), start=1):
            key, problem = future.result()
            with lock:
                results[key] += 1
                if problem:
                    bad.append(problem)
                if i % 100 == 0 or i == len(pairs):
                    rate = i / max(time.monotonic() - started, 1e-6)
                    print(f"  {i:,}/{len(pairs):,} checked  {rate:,.0f}/s  "
                          f"{results['ok']:,} ok  {len(bad):,} problem(s)")

    print("\n--- result ---")
    for name, count in results.items():
        if count:
            print(f"  {name:15}: {count:,}")
    for problem in bad[:15]:
        print(f"    {problem}")
    if len(bad) > 15:
        print(f"    ... and {len(bad) - 15:,} more")

    if args.report:
        Path(args.report).write_text(json.dumps({"summary": results, "problems": bad}, indent=2),
                                     encoding="utf-8")
        print(f"\nreport -> {args.report}")

    clean = results["ok"] == sum(results.values())
    print("\nVERIFIED: every checked object matches its local file" if clean
          else "\nPROBLEMS FOUND: see above")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())

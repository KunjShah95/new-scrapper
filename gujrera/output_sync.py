"""Close the R2 gap between what `output/` holds and what the bucket holds.

Three uploaders have historically written to the bucket, and none of them
covers everything:

  * `r2_sync.py` — everything under output/ (JSON+MD+binary), path keys, but
    it ran once and the estate has grown since;
  * `gujrera/r2_direct.py` — only *document* files, during a scrape
    (`documents.py:offload`), so runs using it never publish records;
  * `gujrera/sink.py` — delta-run artifacts only.

The delta between them is precisely this module: walk `output/`, derive the
key `r2_sync.destination()` would produce, and push files the bucket does not
have. Listing the two prefixes once makes the check cheap; every upload goes
under the identical legacy key, so the worker/browser contract holds.

    python run.py --sync-output            # real
    python run.py --sync-output --dry-run  # diff only, no writes
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .legacy import route_key
from .sink import IMMUTABLE, REFRESH, Sink

log = logging.getLogger("gujrera.output_sync")

# Never push a file a concurrent scrape is actively writing: a documents
# manifest or bundle younger than this may be half-written.
FRESH_SECONDS = 120

PROJECT_FILES = {"project_bundle.json", "documents_manifest.json"}


def _recorded(root: Path, path: Path) -> bool:
    """A project artifact worth mirroring into the bucket.

    Only the per-project estate belongs here: bundles, manifests, reports,
    downloads, raw responses. `targets.json`, `changes/` and `geo/` have their
    own pipelines and pushing them from here would double-publish.
    """
    if not path.is_file() or not path.stat().st_size:
        return False
    name = path.name
    if name in PROJECT_FILES or name.endswith(".md"):
        return True
    # files/ and raw/ members are everything else a scrape saved
    return any(part in {"files", "raw"} for part in path.relative_to(root).parts[:-1])


def _fresh(path: Path) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) < FRESH_SECONDS
    except OSError:
        return False


def _bucket_keys(client, prefixes) -> set:
    keys: set = set()
    for prefix in prefixes:
        token = None
        while True:
            kw = {"Bucket": None, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            keys.update(obj["Key"] for obj in resp.get("Contents", []))
            if resp.get("IsTruncated"):
                token = resp["NextContinuationToken"]
            else:
                break
    return keys


def sync_output(sink: Sink, root: Path, dry_run: bool = False,
                workers: int = 12) -> dict:
    """Push every output/ artifact the bucket lacks. Resumable: nothing in
    the bucket is re-uploaded, so an interrupted run can simply be repeated.
    Pushes happen on a small thread pool — 75k tiny files is latency-bound,
    not bandwidth-bound. Returns counts by kind."""
    totals = {"found": 0, "pushed": 0, "failed": 0, "skipped": 0, "fresh": 0,
              "bytes": 0, "by": {}}
    lock = threading.Lock()

    # One LIST per prefix makes the whole diff cheap — anything already in the
    # bucket is left alone, whatever wrote it there.
    client = getattr(sink, "client", None)
    existing: set = set()
    if client is not None:
        for prefix in (getattr(sink, "pdf_prefix", ""), getattr(sink, "json_prefix", "")):
            token = None
            while True:
                kw = {"Bucket": sink.bucket, "Prefix": (prefix + "/") if prefix else "",
                      "MaxKeys": 1000}
                if token:
                    kw["ContinuationToken"] = token
                resp = client.list_objects_v2(**kw)
                existing.update(obj["Key"] for obj in resp.get("Contents", []))
                if resp.get("IsTruncated"):
                    token = resp["NextContinuationToken"]
                else:
                    break

    plan = []  # (rel, key, path, cache)
    for path in sorted(root.rglob("*")):
        if not _recorded(root, path):
            continue
        rel = path.relative_to(root)
        key = route_key(rel.as_posix(),
                        getattr(sink, "pdf_prefix", "") or "",
                        getattr(sink, "json_prefix", "") or "")
        totals["found"] += 1
        if _fresh(path):
            totals["fresh"] += 1
            continue
        if key in existing:
            totals["skipped"] += 1
            continue

        posix = f"/{rel.as_posix()}/"
        kind = ("files" if "/files/" in posix else
                "raw" if "/raw/" in posix else "records")
        sizes = totals["by"].setdefault(kind, {"n": 0, "bytes": 0})
        sizes["n"] += 1
        sizes["bytes"] += path.stat().st_size
        totals["bytes"] += path.stat().st_size
        if dry_run:
            totals["pushed"] += 1
            continue
        cache = IMMUTABLE if "/files/" in posix else REFRESH
        plan.append((rel, path, key, cache))

    def _push(item):
        rel, path, key, cache = item
        sink.put_file(key, path, cache_control=cache)
        return rel

    if plan:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_push, item): item for item in plan}
            done = 0
            for future in as_completed(futures):
                rel = futures[future][0]
                done += 1
                try:
                    future.result()
                except Exception as exc:  # keep going; report at the end
                    totals["failed"] += 1
                    log.warning("upload failed for %s: %s", rel, exc)
                    continue
                if done % 500 == 0:
                    log.info("pushed %d/%d (latest %s)", done, len(plan), rel)
        totals["pushed"] = len(plan) - totals["failed"]

    return totals

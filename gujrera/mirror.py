"""
Push the local mirror (`r2_mirror/`) into the current sink.

The pipeline writes the same artifacts to whichever sink a run was given.
Until the VPS necessarily ran in R2 mode, they landed in `r2_mirror/`; this
command republishes what is there with all of the sink's routing rules — the
steps a run takes upstream — so local and cloud end up byte-for-byte
equivalent:

    r2_mirror/blobs/ab/cd/<sha>       -> <R2_PDF_PREFIX>/blobs/ab/cd/<sha>
    r2_mirror/projects/<pid>/…        -> <R2_JSON_PREFIX>/projects/<pid>/…
    r2_mirror/changes/2026-…-runN.…   -> <R2_JSON_PREFIX>/changes/…
    r2_mirror/catalogue/projects.json -> <R2_JSON_PREFIX>/catalogue/projects.json

Blobs get the immutable cache policy, refreshed records the short one — the
same constants the steam itself uses. Keys are derived from the path, so this
is idempotent: nothing that already exists is uploaded twice.

    python run.py --sync-mirror            # real
    python run.py --sync-mirror --dry-run  # routes only, no network
"""

from __future__ import annotations

import logging
from pathlib import Path

from .sink import IMMUTABLE, REFRESH, Sink

log = logging.getLogger("gujrera.mirror")


def _target_of(sink: Sink, logical: str) -> str:
    route = getattr(sink, "_route", None) or getattr(sink, "_target", None)
    return route(logical) if route else logical


def sync_mirror(sink: Sink, root: Path, dry_run: bool = False) -> dict:
    """Route every file under root into the sink. Idempotent."""
    if not root.exists():
        log.info("no mirror at %s — nothing to sync", root)
        return {"found": 0, "pushed": 0, "skipped": 0, "bytes": 0}

    totals = {"found": 0, "pushed": 0, "skipped": 0, "bytes": 0}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        logical = path.relative_to(root).as_posix()
        totals["found"] += 1
        totals["bytes"] += path.stat().st_size

        if dry_run:
            totals["pushed"] += 1
            continue
        if sink.exists(logical):
            totals["skipped"] += 1
            continue

        cache = IMMUTABLE if logical.startswith("blobs/") else REFRESH
        sink.put_file(logical, path, cache_control=cache)
        log.info("pushed %s -> %s", logical, _target_of(sink, logical))
        totals["pushed"] += 1
    return totals

"""
Put the system of record on R2, not just its consequences.

Everything else that lands in the bucket — reports, catalogues, change
journals — is *derived* from `state/gujrera.db`. If the VPS drive dies, that
SQLite file is the thing that cannot be reconstructed. So every finished run
publishes a snapshot:

    state/latest.db.gz            right now, always
    state/<date>/run-<id>.db.gz   every run, kept forever

which is a genuine SQLite file after gunzip (`sqlite3 state.db .tables`), made
by `VACUUM INTO`, which compacts and checkpoints in one statement. A `runs/…`
JSON summary rides along so a snapshot browser never has to open the database.
"""

from __future__ import annotations

import gzip
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from .sink import Sink

log = logging.getLogger("gujrera.snapshot")


def _stamp() -> str:
    from .state import now
    return now()


def publish_state_snapshot(sink: Sink, state_path: Path, run_id: int,
                           extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compact the live database into a consistent copy, publish it, return sizes.

    `VACUUM INTO` runs on the *live* connection, so the copy is transactionally
    consistent even while other writers hold WAL: SQLite serialises schema and
    data pages together. A second connection would freeze mid-write instead.
    """
    if not Path(state_path).exists():
        log.warning("no state file at %s — snapshot skipped", state_path)
        return {"skipped": True}

    tmp = Path(state_path).with_suffix(f".snap-{run_id}.db")
    try:
        live = sqlite3.connect(str(state_path))
        try:
            tmp.unlink(missing_ok=True)
            live.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            live.close()

        db_bytes = tmp.read_bytes()
        day = _stamp()[:10]
        timestamped = f"state/{day}/run-{run_id}.db.gz"

        sink.put_bytes(timestamped, gzip.compress(db_bytes, compresslevel=6),
                       "application/gzip")
        sink.put_bytes("state/latest.db.gz", gzip.compress(db_bytes, compresslevel=6),
                       "application/gzip")

        summary = dict(extra or {})
        try:
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            summary.update({
                "run_id": run_id,
                "db_bytes": len(db_bytes),
                "tables": {row[0]: conn.execute(
                    "SELECT count(*) FROM " + row[0]).fetchone()[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'").fetchall()},
            })
            conn.close()
        except sqlite3.Error:
            summary.setdefault("run_id", run_id)
            summary.setdefault("db_bytes", len(db_bytes))
        summary["published_at"] = _stamp()
        sink.put_json(f"state/latest.json", summary)

        log.info("state snapshot published: run %d, %.2f MB", run_id,
                 len(db_bytes) / 1e6)
        return {"run": timestamped, "db_mb": round(len(db_bytes) / 1e6, 2)}
    finally:
        tmp.unlink(missing_ok=True)


def restore_state_snapshot(sink: Sink, state_path: Path, force: bool = False) -> Dict[str, Any]:
    """Fetch `state/latest.db.gz` into `state_path` — the VPS bootstrap.

    A fresh machine with no database treats all ~2,000 captured projects as new
    and re-downloads every document. Restoring the snapshot first makes it carry
    on exactly where the last machine stopped. Refuses to overwrite an existing
    database unless `force`, and the old one is kept as `.pre-restore`.
    """
    state_path = Path(state_path)
    if state_path.exists() and not force:
        return {"skipped": True, "reason": f"{state_path} already exists"}
    blob = sink.get_bytes("state/latest.db.gz")
    if blob is None:
        return {"skipped": True, "reason": "no state/latest.db.gz in the sink"}
    db_bytes = gzip.decompress(blob)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".restore.db")
    tmp.write_bytes(db_bytes)
    conn = sqlite3.connect(str(tmp))
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        projects = conn.execute("SELECT count(*) FROM projects").fetchone()[0]
    finally:
        conn.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"restored snapshot failed integrity_check: {check}")
    if state_path.exists():
        for suffix in ("-wal", "-shm"):
            Path(str(state_path) + suffix).unlink(missing_ok=True)
        state_path.replace(state_path.with_suffix(".db.pre-restore"))
    tmp.replace(state_path)
    meta = sink.get_bytes("state/latest.json")
    info = json.loads(meta) if meta else {}
    log.info("state restored: %d projects, %.2f MB, from run %s",
             projects, len(db_bytes) / 1e6, info.get("run_id"))
    return {"restored": True, "projects": projects, "db_mb": round(len(db_bytes) / 1e6, 2),
            "run_id": info.get("run_id"), "published_at": info.get("published_at")}

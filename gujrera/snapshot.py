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

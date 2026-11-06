"""
SQLite state store — what we already have, and what changed.

The scraper itself is stateless: it fetches and writes. Everything that makes a
*daily* run different from a full sweep lives here:

  * which projects we have already seen (and which have vanished upstream),
  * the hash of each endpoint's payload the last time we looked at it,
  * every document UID we have downloaded, with its sha256 and blob key,
  * a journal of every change, run by run.

The store is the system of record. R2 holds the bytes; this holds the knowledge
of which bytes we have and what they meant.

Concurrency: the pipeline fetches on a thread pool but records on the caller's
thread, so one connection guarded by a lock is enough.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("gujrera.state")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per project we have ever seen, including ones that later completed.
CREATE TABLE IF NOT EXISTS projects (
    pid             INTEGER PRIMARY KEY,
    reg_no          TEXT,
    name            TEXT,
    district        TEXT,
    taluka          TEXT,
    ptype           TEXT,
    cost            REAL,
    end_date        TEXT,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | completed | gone
    summary         TEXT,                             -- summary row as JSON
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    last_probed_at  TEXT,
    last_scraped_at TEXT,
    last_run_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_projects_district ON projects(district, status);
CREATE INDEX IF NOT EXISTS idx_projects_status   ON projects(status);

-- Hash of each endpoint payload as of the last full scrape. This is the entire
-- change-detection signal: same hash means the tab has not moved.
CREATE TABLE IF NOT EXISTS endpoint_hashes (
    pid        INTEGER NOT NULL,
    endpoint   TEXT NOT NULL,
    hash       TEXT NOT NULL,
    bytes      INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (pid, endpoint)
);

-- Every document UID ever seen for a project. The UID is GujRERA's upload
-- identity, so a new UID under a known label is a replaced document.
CREATE TABLE IF NOT EXISTS documents (
    pid            INTEGER NOT NULL,
    uid            TEXT NOT NULL,
    label          TEXT NOT NULL,
    file_name      TEXT,
    sha256         TEXT,
    bytes          INTEGER,
    content_type   TEXT,
    blob_key       TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | failed | removed
    error          TEXT,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    uploaded_at    TEXT,
    PRIMARY KEY (pid, uid)
);
CREATE INDEX IF NOT EXISTS idx_documents_sha  ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_stat ON documents(pid, status);

-- Label -> UID history, so "the brochure changed" is provable rather than
-- inferred from two file names.
CREATE TABLE IF NOT EXISTS document_versions (
    pid         INTEGER NOT NULL,
    label       TEXT NOT NULL,
    uid         TEXT NOT NULL,
    version     INTEGER NOT NULL,
    sha256      TEXT,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (pid, label, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    mode               TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    districts          TEXT,
    discovered         INTEGER DEFAULT 0,
    new_projects       INTEGER DEFAULT 0,
    changed_projects   INTEGER DEFAULT 0,
    probed_projects    INTEGER DEFAULT 0,
    docs_new           INTEGER DEFAULT 0,
    docs_reused        INTEGER DEFAULT 0,
    docs_failed        INTEGER DEFAULT 0,
    bytes_downloaded   INTEGER DEFAULT 0,
    error              TEXT
);

-- The daily journal. This is what the change report is rendered from.
CREATE TABLE IF NOT EXISTS changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id),
    pid        INTEGER,
    kind       TEXT NOT NULL,
    entity     TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_run ON changes(run_id);
"""


def now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def today() -> str:
    return _dt.date.today().isoformat()


def canonical_hash(obj: Any) -> str:
    """Stable sha256 of a JSON payload.

    `sort_keys` matters: the portal does not guarantee object key order between
    requests, so hashing the raw bytes would report a change on every run.
    """
    if obj is None:
        return ""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class State:
    """Thin, explicit wrapper over the SQLite file. No ORM, no surprises."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ plumbing ---
    def _query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _write(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, tuple(params))

    # ------------------------------------------------------------------- projects ---
    def get_project(self, pid: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM projects WHERE pid=?", (pid,))
        return dict(row) if row else None

    def project_pids(self) -> set:
        return {int(r["pid"]) for r in self._query("SELECT pid FROM projects")}

    def active_pids(self, district: Optional[str] = None) -> set:
        if district:
            rows = self._query(
                "SELECT pid FROM projects WHERE status='active' AND lower(district)=?",
                (district.strip().lower(),),
            )
        else:
            rows = self._query("SELECT pid FROM projects WHERE status='active'")
        return {int(r["pid"]) for r in rows}

    def count_active(self, district: str) -> int:
        row = self._one(
            "SELECT count(*) AS n FROM projects WHERE status='active' AND lower(district)=?",
            (district.strip().lower(),),
        )
        return int(row["n"]) if row else 0

    def upsert_project(self, info: Dict[str, Any], run_id: Optional[int] = None) -> None:
        pid = int(info["pid"])
        stamp = now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO projects(pid, reg_no, name, district, taluka, ptype, cost,
                                     end_date, status, first_seen_at, last_seen_at,
                                     last_run_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pid) DO UPDATE SET
                    reg_no       = COALESCE(excluded.reg_no, projects.reg_no),
                    name         = COALESCE(excluded.name, projects.name),
                    district     = COALESCE(excluded.district, projects.district),
                    taluka       = COALESCE(excluded.taluka, projects.taluka),
                    ptype        = COALESCE(excluded.ptype, projects.ptype),
                    cost         = COALESCE(excluded.cost, projects.cost),
                    end_date     = COALESCE(excluded.end_date, projects.end_date),
                    status       = CASE WHEN projects.status='gone' THEN 'active'
                                        ELSE projects.status END,
                    last_seen_at = excluded.last_seen_at,
                    last_run_id  = COALESCE(excluded.last_run_id, projects.last_run_id)
                """,
                (
                    pid,
                    info.get("regNo"),
                    info.get("name"),
                    (info.get("district") or "").strip() or None,
                    info.get("taluka"),
                    info.get("type"),
                    info.get("cost"),
                    info.get("endDate"),
                    "active",
                    stamp,
                    stamp,
                    run_id,
                ),
            )

    def mark_probed(self, pid: int) -> None:
        self._write("UPDATE projects SET last_probed_at=? WHERE pid=?", (now(), pid))

    def mark_scraped(self, pid: int, run_id: Optional[int] = None) -> None:
        self._write(
            "UPDATE projects SET last_scraped_at=?, last_probed_at=?, last_run_id=? WHERE pid=?",
            (now(), now(), run_id, pid),
        )

    def mark_status(self, pid: int, status: str) -> None:
        self._write("UPDATE projects SET status=? WHERE pid=?", (status, pid))

    def backdate(self, pid: int, captured_at: str) -> None:
        """Record when a project was *really* captured, for imported history.

        The importer writes rows for work done weeks ago; stamping them with
        "now" would make the whole legacy estate look freshly scraped and hide
        it from the staleness rotation. Existing values are widened, never
        replaced: the earliest sighting and the latest activity both survive,
        so a project captured by a live run after the import keeps that date.
        """
        self._write(
            "UPDATE projects SET "
            "  first_seen_at   = MIN(COALESCE(first_seen_at, ?), ?), "
            "  last_seen_at    = MAX(COALESCE(last_seen_at, ?), ?), "
            "  last_scraped_at = MAX(COALESCE(last_scraped_at, ?), ?), "
            "  last_probed_at  = MAX(COALESCE(last_probed_at, ?), ?) "
            "WHERE pid=?",
            (captured_at, captured_at) * 4 + (pid,),
        )

    def set_summary(self, pid: int, summary: Any) -> None:
        self._write(
            "UPDATE projects SET summary=? WHERE pid=?",
            (json.dumps(summary, ensure_ascii=False, default=str) if summary is not None else None, pid),
        )

    def project_meta(self) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM projects ORDER BY pid")
        return [dict(r) for r in rows]

    def pids_by_staleness(self, districts: Optional[Iterable[str]] = None) -> List[int]:
        """Active pids, least-recently-scraped first.

        A full pass that is capped by `--max-scrapes` needs a fair rotation,
        otherwise it re-scrapes the same first few hundred projects every time
        and never touches the tail.
        """
        names = [d.strip().lower() for d in (districts or []) if d.strip()]
        sql = "SELECT pid FROM projects WHERE status='active'"
        params: List[Any] = []
        if names:
            sql += f" AND lower(district) IN ({','.join('?' * len(names))})"
            params.extend(names)
        sql += " ORDER BY (last_scraped_at IS NOT NULL), last_scraped_at, pid"
        return [int(r["pid"]) for r in self._query(sql, params)]

    # ------------------------------------------------------------ endpoint hashes ---
    def endpoint_hashes(self, pid: int) -> Dict[str, str]:
        rows = self._query("SELECT endpoint, hash FROM endpoint_hashes WHERE pid=?", (pid,))
        return {r["endpoint"]: r["hash"] for r in rows}

    def set_endpoint_hashes(self, pid: int, hashes: Dict[str, Tuple[str, int]]) -> None:
        """`hashes` maps endpoint -> (hash, byte size of the canonical payload)."""
        stamp = now()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO endpoint_hashes(pid, endpoint, hash, bytes, fetched_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(pid, endpoint) DO UPDATE SET "
                "hash=excluded.hash, bytes=excluded.bytes, fetched_at=excluded.fetched_at",
                [(pid, name, digest, size, stamp) for name, (digest, size) in hashes.items()],
            )

    # ------------------------------------------------------------------ documents ---
    def known_documents(self, pid: int) -> Dict[str, Dict[str, Any]]:
        rows = self._query("SELECT * FROM documents WHERE pid=? AND status IN ('ok','pending')", (pid,))
        return {r["uid"]: dict(r) for r in rows}

    def documents_for(self, pid: int) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM documents WHERE pid=? ORDER BY label", (pid,))
        return [dict(r) for r in rows]

    def upsert_document(self, pid: int, row: Dict[str, Any], uploaded: bool = False) -> None:
        stamp = now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO documents(pid, uid, label, file_name, sha256, bytes,
                                      content_type, blob_key, status, error,
                                      first_seen_at, last_seen_at, uploaded_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pid, uid) DO UPDATE SET
                    label        = excluded.label,
                    file_name    = COALESCE(excluded.file_name, documents.file_name),
                    sha256       = COALESCE(excluded.sha256, documents.sha256),
                    bytes        = COALESCE(excluded.bytes, documents.bytes),
                    content_type = COALESCE(excluded.content_type, documents.content_type),
                    blob_key     = COALESCE(excluded.blob_key, documents.blob_key),
                    status       = CASE WHEN documents.status='ok' AND excluded.status='failed'
                                        THEN documents.status ELSE excluded.status END,
                    error        = excluded.error,
                    last_seen_at = excluded.last_seen_at,
                    uploaded_at  = COALESCE(excluded.uploaded_at, documents.uploaded_at)
                """,
                (
                    pid, row["uid"], row.get("label") or "", row.get("file") or None,
                    row.get("sha256"), row.get("bytes"), row.get("content_type"),
                    row.get("blob_key"), row.get("status") or "pending",
                    (row.get("status") or "").split(":", 1)[1].strip()
                    if str(row.get("status") or "").startswith("failed:") else None,
                    stamp, stamp, stamp if uploaded else None,
                ),
            )

    def mark_documents_removed(self, pid: int, uids: Iterable[str]) -> None:
        uids = list(uids)
        if not uids:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE documents SET status='removed' WHERE pid=? AND uid=?",
                [(pid, uid) for uid in uids],
            )

    def next_version(self, pid: int, label: str) -> int:
        row = self._one(
            "SELECT COALESCE(MAX(version), 0) AS v FROM document_versions WHERE pid=? AND label=?",
            (pid, label),
        )
        return int(row["v"]) + 1 if row else 1

    def record_version(self, pid: int, label: str, uid: str, sha256: Optional[str]) -> int:
        version = self.next_version(pid, label)
        self._write(
            "INSERT INTO document_versions(pid, label, uid, version, sha256, captured_at) "
            "VALUES(?,?,?,?,?,?)",
            (pid, label, uid, version, sha256, now()),
        )
        return version

    def document_history(self, pid: int) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM document_versions WHERE pid=? ORDER BY label, version", (pid,)
        )
        return [dict(r) for r in rows]

    def uploaded_shas(self) -> set:
        """sha256 values already present under this sink's content-addressed layout.

        Only `blobs/…` counts: rows imported from the legacy path-based layout
        (`<prefix>/<project>/files/<file>`) hold valid R2 objects, but *not* at
        the content-addressed key the delta engine derives from the hash. If
        those shas were trusted here, a reused legacy row would be marked with
        a `blobs/…` key that was never PUT — and the manifest would point at a
        404 forever. Use `migrate` tooling to copy legacy objects into
        `blobs/…`; until then such a row keeps its real, working key.
        """
        return {r["sha256"] for r in self._query(
            "SELECT DISTINCT sha256 FROM documents "
            "WHERE blob_key LIKE 'blobs/%' AND sha256 IS NOT NULL"
        )}

    # ---------------------------------------------------------------------- runs ---
    def start_run(self, mode: str, districts: Iterable[str] = ()) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs(mode, started_at, districts) VALUES(?,?,?)",
                (mode, now(), ",".join(districts)),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, error: Optional[str] = None, **counts: Any) -> None:
        fields = ", ".join(f"{k}=?" for k in counts)
        params: List[Any] = list(counts.values())
        sql = f"UPDATE runs SET finished_at=?, error=?{', ' + fields if fields else ''} WHERE id=?"
        params = [now(), error] + params + [run_id]
        self._write(sql, params)

    def update_run(self, run_id: int, **counts: Any) -> None:
        """Record progress without closing the run.

        Called as work completes, so a run that is killed mid-flight leaves real
        numbers behind instead of zeros. A `oneshot` process that dies never
        reaches its own cleanup code, and "0 scraped" for a run that in fact
        captured 900 documents is worse than useless — it hides the only evidence.
        """
        if not counts:
            return
        fields = ", ".join(f"{k}=?" for k in counts)
        self._write(f"UPDATE runs SET {fields} WHERE id=?",
                    [*counts.values(), run_id])

    def close_stale_runs(self, run_id: int) -> int:
        """Mark earlier runs that never finished as interrupted.

        Only safe while holding the run lock, which guarantees no other run is
        live — otherwise this would flag a concurrent run as dead.
        """
        rows = self._query(
            "SELECT id FROM runs WHERE finished_at IS NULL AND id<?", (run_id,)
        )
        for row in rows:
            self._write(
                "UPDATE runs SET finished_at=?, "
                "error=COALESCE(error, 'interrupted (process stopped before it could finish)') "
                "WHERE id=?",
                (now(), row["id"]),
            )
        return len(rows)

    def run(self, run_id: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        return dict(row) if row else None

    def recent_runs(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- changes ---
    def add_change(self, run_id: int, pid: Optional[int], kind: str,
                   entity: Optional[str] = None, detail: Any = None) -> None:
        self._write(
            "INSERT INTO changes(run_id, pid, kind, entity, detail, created_at) VALUES(?,?,?,?,?,?)",
            (
                run_id, pid, kind, entity,
                json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None,
                now(),
            ),
        )

    def add_changes(self, rows: Iterable[Tuple[Optional[int], str, Optional[str], Any]],
                    run_id: int) -> None:
        stamp = now()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO changes(run_id, pid, kind, entity, detail, created_at) VALUES(?,?,?,?,?,?)",
                [
                    (run_id, pid, kind, entity,
                     json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None,
                     stamp)
                    for pid, kind, entity, detail in rows
                ],
            )

    def changes_for_run(self, run_id: int) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM changes WHERE run_id=? ORDER BY pid, kind", (run_id,))
        out = []
        for row in rows:
            item = dict(row)
            if item.get("detail"):
                with contextlib.suppress(ValueError, TypeError):
                    item["detail"] = json.loads(item["detail"])
            out.append(item)
        return out

    def change_counts(self, run_id: int) -> Dict[str, int]:
        rows = self._query(
            "SELECT kind, count(*) AS n FROM changes WHERE run_id=? GROUP BY kind", (run_id,)
        )
        return {r["kind"]: int(r["n"]) for r in rows}

    def last_completed_run(self, before_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        if before_id is None:
            row = self._one(
                "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
            )
        else:
            row = self._one(
                "SELECT * FROM runs WHERE finished_at IS NOT NULL AND id<? ORDER BY id DESC LIMIT 1",
                (before_id,),
            )
        return dict(row) if row else None

    # ------------------------------------------------------------------ coverage ---
    def coverage(self, stale_after_days: int = 7) -> Dict[str, Any]:
        """How much of the estate we actually hold — the "did it get scraped?" view.

        Three independent things are counted, because they can disagree:

          * `captured` — a full scrape completed at least once (`last_scraped_at`)
          * `hashes`   — endpoint fingerprints held per project. A project with 25
                         hashes was captured whole; 8 means seventeen tabs were
                         empty or failed. This, not the timestamp, is the real
                         completeness signal.
          * `partial`  — captured, but holding fewer than the expected endpoints.
        """
        cutoff = (
            _dt.datetime.now() - _dt.timedelta(days=stale_after_days)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        districts = [dict(r) for r in self._query(
            """
            SELECT COALESCE(upper(district), 'UNKNOWN')                         AS district,
                   count(*)                                                     AS active,
                   sum(CASE WHEN last_scraped_at IS NOT NULL THEN 1 ELSE 0 END) AS captured,
                   sum(CASE WHEN last_scraped_at IS NULL     THEN 1 ELSE 0 END) AS never,
                   sum(CASE WHEN last_scraped_at < ?         THEN 1 ELSE 0 END) AS stale,
                   max(last_scraped_at)                                         AS last_capture
              FROM projects
             WHERE status='active'
             GROUP BY 1
             ORDER BY 1
            """,
            (cutoff,),
        )]

        documents = {r["status"]: int(r["n"]) for r in self._query(
            "SELECT status, count(*) AS n FROM documents GROUP BY status"
        )}
        blobs = self._one(
            "SELECT count(*) AS n, COALESCE(sum(bytes), 0) AS b FROM documents "
            "WHERE blob_key IS NOT NULL"
        ) or {"n": 0, "b": 0}

        per_project = {
            int(r["pid"]): int(r["c"])
            for r in self._query("SELECT pid, count(*) AS c FROM endpoint_hashes GROUP BY pid")
        }
        expected = max(per_project.values()) if per_project else 0
        # 25 endpoints exist; a handful are legitimately empty for any given
        # project, so "complete" means "close to the best we have ever seen".
        full_mark = max(1, int(expected * 0.8))
        partial = sorted(pid for pid, count in per_project.items() if count < full_mark)

        versions = self._one("SELECT count(*) AS n FROM document_versions") or {"n": 0}
        last = self._one("SELECT max(finished_at) AS t FROM runs WHERE finished_at IS NOT NULL")
        never_hashed = self._one(
            "SELECT count(*) AS n FROM projects p WHERE p.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM endpoint_hashes h WHERE h.pid=p.pid)"
        ) or {"n": 0}

        return {
            "districts": districts,
            "total_active": sum(d["active"] for d in districts),
            "total_captured": sum(d["captured"] for d in districts),
            "never_hashed": int(never_hashed["n"]),
            "documents": documents,
            "blobs": {"count": int(blobs["n"]), "bytes": int(blobs["b"])},
            "hash_endpoints_expected": expected,
            "partial_projects": partial,
            "versions": int(versions["n"]),
            "last_run": last["t"] if last else None,
            "stale_after_days": stale_after_days,
        }

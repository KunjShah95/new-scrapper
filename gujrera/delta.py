"""
Daily delta engine.

A full sweep of the three districts means ~3,000 projects x 25 endpoints plus
~140,000 document downloads. Doing that every day is wasteful, and the portal
gives us no help: `vdms/download/<UID>` returns no ETag, no Last-Modified, no
Content-Length and ignores Range, so there is no cheap way to ask "did this PDF
change?".

So change detection works on what *is* cheap and authoritative:

  1. **Discovery** tells us which projects exist. A pid we have never stored is
     new; a pid that disappears has completed or been withdrawn.
  2. **A probe set** of the endpoints that actually carry volatile data is
     fetched for every known project and hashed. Matching hashes mean nothing
     moved, and nothing else is fetched.
  3. **The UID is the document identity.** GujRERA mints a new VDMS UID on every
     upload, so a new UID under a known label is a replaced document, and a UID
     we already hold is by definition unchanged.

Everything that survives those three checks gets a full scrape, its blobs pushed
to the sink, and an entry in the change journal. That journal is the product of
a daily run: not a new copy of the data, but an answer to "what is different
from yesterday".

Invariant worth stating loudly: **hashes are only written after a full scrape.**
Writing them from the probe would make the project look unchanged to the diff
that follows, and the change would be silently lost.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .api import GujreraClient, payload
from .documents import write_manifest
from .pipeline import BUNDLE_ENDPOINTS, district_codes, discover_projects, scrape_project
from .report import build_master_markdown
from .runlock import LockHeld, RunLock
from .sink import IMMUTABLE, Sink, blob_key
from .state import State, canonical_hash, today

log = logging.getLogger("gujrera.delta")

# The tabs that move independently of `core`. Progress reports, quarterly
# filings, annual compliance and RERA applications are where a project actually
# changes; identity fields almost never do.
PROBE_ENDPOINTS: Tuple[str, ...] = (
    "core",
    "applications",
    "progress_score",
    "quarters",
    "annual_compliance",
    "block_progress",
)

# If a discovery pass returns less than this share of what we already hold for a
# district, the portal is having a bad day (or changed its search contract) and
# we must not treat the shortfall as "2,000 projects vanished".
DISCOVERY_FLOOR = 0.80
# Below this many known projects the floor is noise, not a signal.
DISCOVERY_FLOOR_MIN = 50

DEFAULT_DISTRICTS = ("ahmedabad", "gandhinagar", "mehsana")


# ---------------------------------------------------------------------------- hashing ---
def endpoint_hashes(raw: Dict[str, Any],
                    names: Iterable[str] = BUNDLE_ENDPOINTS) -> Dict[str, Tuple[str, int]]:
    """Hash the *unwrapped* payload of each endpoint.

    The envelope carries paging metadata (`totalPages`, `dataSize`) that we do
    not want in the fingerprint, and object key order is not guaranteed between
    requests — hence canonical hashing rather than hashing the raw bytes.
    """
    out: Dict[str, Tuple[str, int]] = {}
    for name in names:
        data = payload(raw.get(name))
        if data is None:
            continue
        digest = canonical_hash(data)
        if not digest:
            continue
        size = len(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))
        out[name] = (digest, size)
    return out


# ------------------------------------------------------------------------------ diffs ---
def diff_payloads(old: Any, new: Any, path: str = "", out: Optional[List[Dict[str, Any]]] = None,
                  limit: int = 60, depth: int = 0) -> List[Dict[str, Any]]:
    """A pragmatic structural diff, written for humans reading a daily report.

    Lists of records (compliance filings, QPR rows) are compared by length first:
    reporting forty shifted rows because one filing was inserted is noise, while
    "3 -> 4 records" is the actual news.
    """
    if out is None:
        out = []
    if len(out) >= limit or depth > 8:
        return out

    if type(old) is not type(new) and not (isinstance(old, (int, float))
                                          and isinstance(new, (int, float))):
        out.append({"path": path or "payload", "old": _short(old), "new": _short(new)})
        return out

    if isinstance(old, dict):
        for key in sorted(set(old) | set(new)):
            if len(out) >= limit:
                break
            child = f"{path}.{key}" if path else key
            if key not in old:
                out.append({"path": child, "old": None, "new": _short(new[key])})
            elif key not in new:
                out.append({"path": child, "old": _short(old[key]), "new": None})
            else:
                diff_payloads(old[key], new[key], child, out, limit, depth + 1)
        return out

    if isinstance(old, list):
        if len(old) != len(new):
            out.append({"path": f"{path} (records)", "old": len(old), "new": len(new)})
            return out
        for index, (a, b) in enumerate(zip(old, new)):
            if len(out) >= limit:
                break
            diff_payloads(a, b, f"{path}[{index}]", out, limit, depth + 1)
        return out

    if old != new:
        out.append({"path": path, "old": _short(old), "new": _short(new)})
    return out


def _short(value: Any, width: int = 90) -> Any:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= width else text[:width] + "…"
    if isinstance(value, str) and len(value) > width:
        return value[:width] + "…"
    return value


# ----------------------------------------------------------------------------- probe ---
def probe_project(client: GujreraClient, pid: int) -> Dict[str, Tuple[str, int]]:
    """Fetch only the volatile endpoints and hash them. Cheap by construction."""
    raw: Dict[str, Any] = {}
    for name in PROBE_ENDPOINTS:
        data, _error = client.get_json(name, pid=pid)
        if data is not None:
            raw[name] = data
    return endpoint_hashes(raw, PROBE_ENDPOINTS)


def probe_changed(previous: Dict[str, str], current: Dict[str, Tuple[str, int]]) -> List[str]:
    """Which probe endpoints differ from what we last recorded."""
    changed = []
    for name, (digest, _size) in current.items():
        prior = previous.get(name)
        # No prior hash means we never captured it: treat as changed so the
        # project gets a full scrape and the gap closes.
        if prior is None or prior != digest:
            changed.append(name)
    return changed


def _discover_strict(client: GujreraClient, district: str, codes: set) -> List[Dict[str, Any]]:
    """Discover, and refuse to continue on a partial result.

    One retry, because a single rate-limited query is usually transient; after
    that the run fails rather than letting a short list masquerade as deletions.
    """
    last: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            return discover_projects(client, only_ongoing=True, dist_codes=codes, strict=True)
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("discovery attempt %d for '%s' failed: %s", attempt, district, exc)
            time.sleep(5 * attempt)
    raise RuntimeError(f"discovery for '{district}' failed twice: {last}")


def load_previous_payloads(folder_root: Path, pid: int,
                           endpoints: Iterable[str]) -> Dict[str, Any]:
    """Read the raw snapshots a previous run left on disk, for before/after diffs.

    The project folder is named `{pid}_{slug}` and the slug follows the project
    name, so a rename orphans it — in that case we simply report the change
    without field detail rather than inventing one.
    """
    endpoints = set(endpoints)
    for folder in sorted(folder_root.glob(f"{pid}_*")):
        raw_dir = folder / "raw"
        if not raw_dir.is_dir():
            continue
        found: Dict[str, Any] = {}
        for name in endpoints:
            path = raw_dir / f"{name}.json"
            if not path.exists():
                continue
            try:
                found[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        if found:
            return found
    return {}


# ------------------------------------------------------------------------- change rows ---
Change = Tuple[Optional[int], str, Optional[str], Any]


def _document_changes(pid: int, prior: Dict[str, Dict[str, Any]],
                      rows: Sequence[Dict[str, Any]]) -> List[Change]:
    """Classify document deltas into new / replaced / removed.

    "Replaced" needs care: several labels legitimately repeat within one project
    (three associates, each with a PAN card; two bank accounts, each with a
    passbook). A UID is only treated as the successor of another when the label
    identifies exactly one document on each side — otherwise it is simply a new
    document that happens to share a label.
    """
    changes: List[Change] = []
    current_uids = {r["uid"] for r in rows}
    prior_by_label: Dict[str, List[str]] = {}
    for row in prior.values():
        prior_by_label.setdefault(row.get("label") or "", []).append(row["uid"])
    current_by_label: Dict[str, int] = {}
    for row in rows:
        if row.get("status") == "ok":
            label = row.get("label") or ""
            current_by_label[label] = current_by_label.get(label, 0) + 1

    for row in rows:
        uid = row["uid"]
        if row.get("status") != "ok" or uid in prior:
            continue
        label = row.get("label") or ""
        superseded = prior_by_label.get(label) or []
        unambiguous = len(superseded) == 1 and current_by_label.get(label) == 1
        if unambiguous:
            changes.append((
                pid, "document_replaced", label,
                {"new_uid": uid, "superseded_uids": superseded,
                 "bytes": row.get("bytes"), "sha256": row.get("sha256")},
            ))
        else:
            changes.append((
                pid, "document_new", label,
                {"uid": uid, "bytes": row.get("bytes"), "sha256": row.get("sha256"),
                 "file": row.get("file"), "shared_label": len(superseded) > 0},
            ))

    for uid, row in prior.items():
        if uid not in current_uids:
            changes.append((
                pid, "document_removed", row.get("label"),
                {"uid": uid, "sha256": row.get("sha256")},
            ))
    return changes


# ------------------------------------------------------------------------------ upload ---
class Uploader:
    """Pushes a scraped project to the sink, skipping bytes we already hold."""

    def __init__(self, sink: Sink, state: State, prune_local: bool = False):
        self.sink = sink
        self.state = state
        self.prune_local = prune_local
        self._uploaded: set = state.uploaded_shas()

    def push(self, pid: int, bundle: Dict[str, Any], folder: Path,
             documents: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        stats = {"blobs_new": 0, "blobs_skipped": 0, "bytes": 0}

        raw_only = {k: v for k, v in (bundle.get("raw") or {}).items() if v is not None}
        self.sink.put_json_gz(f"projects/{pid}/raw.json.gz", raw_only)
        self.sink.put_json(f"projects/{pid}/summary.json", bundle.get("summary") or {})

        report = folder / (bundle.get("markdown_name") or f"{pid}.md")
        if report.exists():
            self.sink.put_file(f"projects/{pid}/report.md", report, "text/markdown; charset=utf-8")

        for row in documents:
            if row.get("status") != "ok" or not row.get("sha256"):
                continue
            sha = row["sha256"]
            key = blob_key(sha)
            row["blob_key"] = key
            row["blob_url"] = self.sink.url_for(key)

            if sha in self._uploaded:
                stats["blobs_skipped"] += 1
                continue
            local = folder / "files" / (row.get("file") or "")
            if not local.exists():
                log.warning("blob %s referenced but %s is missing locally", sha[:12], local)
                row.pop("blob_key", None)
                continue
            self.sink.put_file(key, local, row.get("content_type") or "application/octet-stream",
                               cache_control=IMMUTABLE)
            self._uploaded.add(sha)
            stats["blobs_new"] += 1
            stats["bytes"] += int(row.get("bytes") or 0)

        # A --no-files run has no manifest; writing an empty one would erase the
        # document list already in the sink for this project.
        if documents:
            self.sink.put_json(f"projects/{pid}/documents_manifest.json", list(documents))

        if self.prune_local:
            files_dir = folder / "files"
            if files_dir.exists():
                shutil.rmtree(files_dir, ignore_errors=True)

        return stats


# ------------------------------------------------------------------------------ report ---
def render_change_report(state: State, run_id: int, run: Dict[str, Any],
                         districts: Sequence[str]) -> str:
    changes = state.changes_for_run(run_id)
    counts = state.change_counts(run_id)
    names = {row["pid"]: row.get("name") or str(row["pid"]) for row in state.project_meta()}

    lines: List[str] = []
    lines.append(f"# GujRERA change report — {today()}")
    lines.append("")
    lines.append(f"Run `#{run_id}` · {run.get('mode')} · districts: {', '.join(districts)}")
    lines.append("")
    lines.append(f"- Started: {run.get('started_at')}")
    lines.append(f"- Finished: {run.get('finished_at') or '—'}")
    lines.append(f"- Projects probed: {run.get('probed_projects') or 0:,}")
    lines.append(f"- Projects scraped in full: {run.get('changed_projects') or 0:,}")
    lines.append(f"- New documents captured: {run.get('docs_new') or 0:,}")
    lines.append(f"- Documents reused from state: {run.get('docs_reused') or 0:,}")
    lines.append(f"- Bytes downloaded: {_human_bytes(run.get('bytes_downloaded') or 0)}")
    if run.get("error"):
        lines.append(f"- **Run error:** {run['error']}")
    lines.append("")

    if not changes:
        lines.append("**No changes detected.** Every project and document matched what we "
                     "already had.")
        lines.append("")
        return "\n".join(lines)

    lines.append("| Change | Count |")
    lines.append("|---|---|")
    for kind in sorted(counts):
        lines.append(f"| `{kind}` | {counts[kind]:,} |")
    lines.append("")

    new_projects = [c for c in changes if c["kind"] == "project_new"]
    if new_projects:
        lines.append("## New projects")
        lines.append("")
        lines.append("| PID | Project | Reg. no. | District | Declared cost |")
        lines.append("|---|---|---|---|---|")
        for change in new_projects:
            detail = change.get("detail") or {}
            lines.append(
                f"| {change['pid']} | {change.get('entity') or '—'} | "
                f"{detail.get('regNo') or '—'} | {detail.get('district') or '—'} | "
                f"{_money(detail.get('cost'))} |"
            )
        lines.append("")

    grouped: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for change in changes:
        grouped.setdefault(change.get("pid"), []).append(change)

    updated = {pid: cs for pid, cs in grouped.items()
               if any(c["kind"] == "field_changed" for c in cs)}
    if updated:
        lines.append("## Updated projects")
        lines.append("")
        for pid, cs in sorted(updated.items(), key=lambda item: item[0] or 0):
            lines.append(f"### {names.get(pid, pid)} — `{pid}`")
            lines.append("")
            for change in cs:
                if change["kind"] != "field_changed":
                    continue
                diffs = change.get("detail") or []
                lines.append(f"**{change.get('entity')}** · {len(diffs)} field(s) changed")
                lines.append("")
                if diffs:
                    lines.append("| Field | Was | Now |")
                    lines.append("|---|---|---|")
                    for diff in diffs[:25]:
                        lines.append(f"| `{diff.get('path')}` | {_cell(diff.get('old'))} "
                                     f"| {_cell(diff.get('new'))} |")
                    if len(diffs) > 25:
                        lines.append(f"| … | {len(diffs) - 25} more | see the JSON |")
                    lines.append("")
                else:
                    lines.append("Payload changed, but no field-level difference could be "
                                 "reconstructed (previous snapshot unavailable).")
                    lines.append("")

    doc_changes = [c for c in changes if c["kind"].startswith("document_")]
    if doc_changes:
        lines.append("## Documents")
        lines.append("")
        lines.append("| Project | Change | Document | Detail |")
        lines.append("|---|---|---|---|")
        for change in sorted(doc_changes, key=lambda c: (c.get("pid") or 0, c["kind"])):
            detail = change.get("detail") or {}
            if change["kind"] == "document_replaced":
                extra = f"{len(detail.get('superseded_uids') or [])} prior version(s) kept"
            elif change["kind"] == "document_removed":
                extra = "no longer published"
            elif change["kind"] == "document_failed":
                extra = detail.get("error") or "download failed"
            else:
                extra = _human_bytes(detail.get("bytes") or 0)
                if detail.get("shared_label"):
                    extra += " · shared label"
            lines.append(
                f"| `{change.get('pid')}` | {change['kind'].replace('document_', '')} | "
                f"{change.get('entity') or '—'} | {extra} |"
            )
        lines.append("")

    lifecycle = [c for c in changes if c["kind"] in ("project_completed", "project_gone")]
    if lifecycle:
        lines.append("## Lifecycle")
        lines.append("")
        for change in lifecycle:
            label = ("completed" if change["kind"] == "project_completed"
                     else "**vanished from discovery**")
            lines.append(f"- `{change.get('pid')}` {names.get(change.get('pid'), '')} — {label}")
        lines.append("")

    failed_scrapes = [c for c in changes if c["kind"] == "scrape_failed"]
    if failed_scrapes:
        lines.append("## Failed scrapes")
        lines.append("")
        for change in failed_scrapes:
            lines.append(f"- `{change.get('pid')}` {names.get(change.get('pid'), '')} — "
                         f"{(change.get('detail') or {}).get('error')}")
        lines.append("")

    return "\n".join(lines)


def _cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 70 else text[:70] + "…"


def _money(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"₹{int(value):,}"


def _human_bytes(size: int) -> str:
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ------------------------------------------------------------------------------ catalog ---
def write_catalogues(sink: Sink, state: State) -> int:
    """Full snapshot, for anything that would rather read one file than 3,000."""
    projects = state.project_meta()
    for row in projects:
        if row.get("summary"):
            try:
                row["summary"] = json.loads(row["summary"])
            except (TypeError, ValueError):
                pass
    sink.put_json("catalogue/projects.json", projects, cache_control="public, max-age=60")
    rows = _document_catalogue(sink, state)
    sink.put_jsonl_gz("catalogue/documents.jsonl.gz", rows, cache_control="public, max-age=60")
    return len(rows)


def _document_catalogue(sink: Sink, state: State) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for project in state.project_meta():
        pid = int(project["pid"])
        for doc in state.documents_for(pid):
            if doc.get("status") != "ok":
                continue
            out.append({
                "pid": pid,
                "project": project.get("name"),
                "label": doc["label"],
                "uid": doc["uid"],
                "sha256": doc["sha256"],
                "bytes": doc["bytes"],
                "content_type": doc["content_type"],
                "blob_key": doc["blob_key"],
                "url": sink.url_for(doc["blob_key"]) if doc.get("blob_key") else None,
                "first_seen_at": doc["first_seen_at"],
            })
    return out


# --------------------------------------------------------------------------------- run ---
def run_delta(output_root: Path, state_path: Path, sink: Sink,
              districts: Sequence[str] = DEFAULT_DISTRICTS,
              download_files: bool = True, project_workers: int = 8,
              endpoint_workers: int = 8, doc_workers: int = 8,
              limit: Optional[int] = None, max_scrapes: Optional[int] = None,
              dry_run: bool = False, prune_local: bool = False,
              force_all: bool = False, backfill_only: bool = False,
              only_pids: Optional[Sequence[int]] = None,
              reverify: bool = False,
              lock_path: Optional[Path] = None) -> Dict[str, Any]:
    """One incremental pass: discover, probe, scrape only what moved, journal it."""
    started = time.monotonic()
    output_root.mkdir(parents=True, exist_ok=True)

    purpose = ("backfill" if backfill_only else
               "reverify" if reverify else
               "targeted" if only_pids else "daily")
    lock = RunLock(lock_path or (Path(state_path).parent / "run.lock"), purpose=purpose)
    lock.acquire()

    client = GujreraClient(pool_maxsize=max(32, project_workers * endpoint_workers))
    state = State(state_path)
    run_id = state.start_run(purpose if purpose != "targeted" else "daily", districts)
    # We hold the lock, so any earlier unfinished run is dead by definition.
    stale = state.close_stale_runs(run_id)
    if stale:
        log.warning("marked %d interrupted run(s) as finished-in-error", stale)
    uploader = Uploader(sink, state, prune_local=prune_local)

    counts = {
        "discovered": 0, "new_projects": 0, "changed_projects": 0, "probed_projects": 0,
        "docs_new": 0, "docs_reused": 0, "docs_failed": 0, "bytes_downloaded": 0,
    }
    scraped_bundles: List[Dict[str, Any]] = []
    to_scrape: List[Dict[str, Any]] = []
    completed_pids: List[int] = []
    gone_pids: List[int] = []
    failed_pids: List[int] = []

    try:
        # -- 1. discovery delta ------------------------------------------------
        if only_pids:
            # Targeted re-scrape: no discovery, no probe, no lifecycle. Used when
            # an operator needs one project refreshed right now.
            log.info("targeted run: %s", ", ".join(str(p) for p in only_pids))
            known = state.project_pids()
            for pid in only_pids:
                if int(pid) not in known:
                    raise SystemExit(
                        f"pid {pid} is not in state; run a discovery pass first "
                        f"(or `--daily --backfill`)"
                    )
                to_scrape.append({"pid": int(pid)})
            districts = tuple(districts)

        for district in (() if only_pids else districts):
            codes = district_codes(district)
            log.info("### %s — discovering ###", district)
            found = _discover_strict(client, district, codes)
            if limit:
                found = found[:limit]
            counts["discovered"] += len(found)

            known = state.project_pids()
            known_active = state.active_pids(district)
            found_pids = {int(p["pid"]) for p in found}

            if len(known_active) >= DISCOVERY_FLOOR_MIN and not limit:
                ratio = len(found_pids) / len(known_active)
                if ratio < DISCOVERY_FLOOR:
                    raise RuntimeError(
                        f"discovery sanity check failed for '{district}': found "
                        f"{len(found_pids)} projects but hold {len(known_active)} active "
                        f"({ratio:.0%} < {DISCOVERY_FLOOR:.0%}). Refusing to mark records "
                        f"as gone; check the portal before re-running."
                    )

            new = [p for p in found if int(p["pid"]) not in known]
            # `--limit` is a sampling tool; it must never be read as "everything
            # else disappeared", so the lifecycle pass is skipped when it is set.
            vanished = [] if limit else sorted(known_active - found_pids)
            for project in new:
                log.info("  NEW  %s  %s", project["pid"], project.get("name"))
                state.add_change(run_id, int(project["pid"]), "project_new",
                                 project.get("name"),
                                 {"regNo": project.get("regNo"),
                                  "district": project.get("district"),
                                  "type": project.get("type"),
                                  "cost": project.get("cost"),
                                  "endDate": project.get("endDate")})
            counts["new_projects"] += len(new)

            for project in found:  # refreshes name / cost / end date for known ones
                state.upsert_project(project, run_id)

            for pid in vanished:
                row = state.get_project(pid) or {}
                end = row.get("end_date")
                if end and end < today():
                    state.mark_status(pid, "completed")
                    completed_pids.append(pid)
                else:
                    state.mark_status(pid, "gone")
                    gone_pids.append(pid)

            to_scrape.extend({"pid": int(p["pid"])} for p in new)
            log.info("### %s — %d live, %d new, %d vanished (%d completed, %d gone) ###",
                     district, len(found), len(new), len(vanished),
                     sum(1 for p in vanished
                         if (state.get_project(p) or {}).get("status") == "completed"),
                     sum(1 for p in vanished
                         if (state.get_project(p) or {}).get("status") == "gone"))

        # -- 2. probe known projects ------------------------------------------
        # Checkpoint after discovery: if the process dies during the long probe,
        # the run still shows what it found.
        state.update_run(run_id, **counts)
        newly = {t["pid"] for t in to_scrape}
        if only_pids:
            probe_targets: List[int] = []
        elif backfill_only:
            never = [pid for pid in sorted(state.active_pids())
                     if not state.endpoint_hashes(pid)]
            log.info("backfill mode: probe skipped, %d project(s) never captured", len(never))
            to_scrape.extend({"pid": pid} for pid in never)
            probe_targets = []
        else:
            probe_targets = [pid for pid in sorted(state.active_pids()) if pid not in newly]
            if force_all or reverify:
                # The scheduled weekly pass: the probe only watches 6 of 25
                # endpoints, so a document added to, say, `form_one` or
                # `project_banks` would never trip it. Walking everything is
                # affordable at weekly cadence because unchanged documents are
                # reused by UID and cost no bandwidth. Oldest-scraped first, so
                # a pass cut short by --max-scrapes resumes from the tail.
                log.info("%s: re-verifying every active project (oldest first)",
                         "reverify" if reverify else "force_all")
                to_scrape.extend({"pid": pid} for pid in state.pids_by_staleness(districts))
                probe_targets = []

        log.info("probing %d known project(s) on %d endpoints each",
                 len(probe_targets), len(PROBE_ENDPOINTS))
        if probe_targets:
            with ThreadPoolExecutor(max_workers=max(1, project_workers)) as pool:
                futures = {pool.submit(probe_project, client, pid): pid
                           for pid in probe_targets}
                for index, future in enumerate(as_completed(futures), start=1):
                    pid = futures[future]
                    try:
                        current = future.result()
                    except Exception as exc:  # noqa: BLE001 - one probe must not stop the run
                        log.warning("probe failed for %s: %s", pid, exc)
                        continue
                    counts["probed_projects"] += 1
                    state.mark_probed(pid)
                    # NOTE: hashes are deliberately NOT written here. They are
                    # written after the full scrape, or the diff below would
                    # compare a payload against itself and see nothing.
                    if probe_changed(state.endpoint_hashes(pid), current):
                        to_scrape.append({"pid": pid})
                    if index % 250 == 0:
                        log.info("  probed %d/%d — %d need a full scrape",
                                 index, len(futures), len(to_scrape))

        # Deduplicate while preserving order.
        seen: set = set()
        to_scrape = [t for t in to_scrape if not (t["pid"] in seen or seen.add(t["pid"]))]
        pending = len(to_scrape)
        if max_scrapes is not None and pending > max_scrapes:
            log.info("capping this run at %d scrape(s); %d deferred to the next run",
                     max_scrapes, pending - max_scrapes)
            to_scrape = to_scrape[:max_scrapes]

        log.info("full-scraping %d project(s)%s", len(to_scrape),
                 " [dry run]" if dry_run else "")

        # -- 3. scrape what moved ---------------------------------------------
        for index, target in enumerate(to_scrape, start=1):
            pid = int(target["pid"])
            row = state.get_project(pid) or {}
            district = (row.get("district") or "Ahmedabad").strip().upper().replace(" ", "_")
            folder_root = output_root / district
            prior_hashes = state.endpoint_hashes(pid)
            prior_docs = state.known_documents(pid) if download_files else {}
            resolved = {**target, "name": row.get("name"), "regNo": row.get("reg_no")}
            previous = load_previous_payloads(folder_root, pid, prior_hashes)

            if dry_run:
                log.info("[%d/%d] would scrape %s (%s)", index, len(to_scrape), pid,
                         row.get("name") or "?")
                continue

            log.info("[%d/%d] scraping %s (%s)", index, len(to_scrape), pid,
                     row.get("name") or "?")
            try:
                bundle = scrape_project(
                    client, resolved, folder_root, download_files=download_files,
                    endpoint_workers=endpoint_workers, doc_workers=doc_workers,
                    known_documents=prior_docs,
                )
            except Exception as exc:  # noqa: BLE001 - one project must not stop the run
                log.exception("project %s failed: %s", pid, exc)
                state.add_change(run_id, pid, "scrape_failed", row.get("name"),
                                 {"error": str(exc)})
                failed_pids.append(pid)
                continue

            current_hashes = endpoint_hashes(bundle.get("raw") or {})
            for endpoint, (digest, _size) in current_hashes.items():
                prior = prior_hashes.get(endpoint)
                if prior is None or prior == digest:
                    continue
                diffs = diff_payloads(payload(previous.get(endpoint)),
                                      payload(bundle["raw"].get(endpoint)))
                state.add_change(run_id, pid, "field_changed", endpoint, diffs)
            state.set_endpoint_hashes(pid, current_hashes)
            state.mark_scraped(pid, run_id)
            state.set_summary(pid, bundle.get("summary"))

            documents = bundle.get("documents") or []
            if download_files:
                state.add_changes(_document_changes(pid, prior_docs, documents), run_id)
                current_uids = {d["uid"] for d in documents}
                state.mark_documents_removed(
                    pid, [uid for uid in prior_docs if uid not in current_uids]
                )

            folder = folder_root / bundle["folder"]
            stats = uploader.push(pid, bundle, folder, documents)
            counts["bytes_downloaded"] += stats["bytes"]

            for doc in documents:
                if doc.get("status") == "ok":
                    if doc["uid"] in prior_docs:
                        counts["docs_reused"] += 1
                    else:
                        counts["docs_new"] += 1
                        state.record_version(pid, doc.get("label") or "", doc["uid"],
                                             doc.get("sha256"))
                else:
                    counts["docs_failed"] += 1
                    state.add_change(run_id, pid, "document_failed", doc.get("label"),
                                     {"uid": doc["uid"], "error": doc.get("status")})
                state.upsert_document(pid, doc, uploaded=bool(doc.get("blob_key")))

            write_manifest(documents, folder / "documents_manifest.json")
            scraped_bundles.append(bundle)
            counts["changed_projects"] += 1
            # Persist progress per project: a killed run must still report what
            # it actually captured.
            state.update_run(run_id, **counts)

        # -- 4. lifecycle journal ---------------------------------------------
        for pid in completed_pids:
            state.add_change(run_id, pid, "project_completed", None, None)
        for pid in gone_pids:
            state.add_change(run_id, pid, "project_gone", None, None)

        # -- 5. local outputs --------------------------------------------------
        if scraped_bundles:
            master = output_root / "AHMEDABAD_10_PROJECTS_REPORT.md"
            master.write_text(build_master_markdown(scraped_bundles), encoding="utf-8")
            (output_root / "index.json").write_text(
                json.dumps(
                    [{"folder": b["folder"], "markdown": b["markdown_name"],
                      "summary": b["summary"]} for b in scraped_bundles],
                    indent=2, ensure_ascii=False, default=str,
                ),
                encoding="utf-8",
            )

        state.finish_run(run_id, **counts)

    except Exception as exc:
        state.finish_run(run_id, error=str(exc), **counts)
        run = state.run(run_id) or {}
        _write_journal_local(output_root, state, run_id, run, districts)
        raise
    else:
        run = state.run(run_id) or {}
        _write_journal_local(output_root, state, run_id, run, districts)
        if not dry_run:
            publish_journal(sink, state, run_id, run, districts)
            catalogued = write_catalogues(sink, state)
            log.info("catalogue: %d projects, %d documents", len(state.project_meta()),
                     catalogued)
    finally:
        lock.release()

    elapsed = time.monotonic() - started
    log.info("run #%d done in %.1fs — %d new, %d scraped, %d documents (%.1f MB)",
             run_id, elapsed, counts["new_projects"], counts["changed_projects"],
             counts["docs_new"], counts["bytes_downloaded"] / 1e6)

    result = {
        "run_id": run_id,
        "elapsed": elapsed,
        "counts": counts,
        "completed": completed_pids,
        "gone": gone_pids,
        "failed": failed_pids,
        "dry_run": dry_run,
        "pending_scrapes": pending,
        "deferred": max(0, pending - len(to_scrape)),
    }
    state.close()
    return result


def _write_journal_local(output_root: Path, state: State, run_id: int,
                         run: Dict[str, Any], districts: Sequence[str]) -> Path:
    """Always keep a local copy of the day's report, whatever the sink is.

    Written twice: pinned to the run id (never overwritten) and as the day's
    latest, so a job that runs three times in a day does not destroy the first
    two reports.
    """
    folder = Path(output_root) / "changes"
    folder.mkdir(parents=True, exist_ok=True)
    report = render_change_report(state, run_id, run, districts)
    for name in (f"{today()}.md", f"{today()}-run{run_id}.md"):
        (folder / name).write_text(report, encoding="utf-8")
    (folder / f"{today()}-run{run_id}.json").write_text(
        json.dumps({"run": run, "changes": state.changes_for_run(run_id)},
                   indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return folder / f"{today()}.md"


def publish_journal(sink: Sink, state: State, run_id: int, run: Dict[str, Any],
                    districts: Sequence[str]) -> None:
    day = today()
    report = render_change_report(state, run_id, run, districts)
    body = report.encode("utf-8")
    sink.put_bytes(f"changes/{day}.md", body, "text/markdown; charset=utf-8",
                   "public, max-age=3600")
    sink.put_bytes(f"changes/{day}-run{run_id}.md", body, "text/markdown; charset=utf-8", IMMUTABLE)
    payload = {"run": run, "changes": state.changes_for_run(run_id)}
    sink.put_json(f"changes/{day}.json", payload, cache_control="public, max-age=3600")
    sink.put_json(f"changes/{day}-run{run_id}.json", payload, cache_control=IMMUTABLE)
    sink.put_jsonl_gz(f"changes/{day}.jsonl.gz", state.changes_for_run(run_id),
                      cache_control="public, max-age=3600")
    sink.put_json(f"runs/{run_id}.json",
                  {"run": run, "counts": state.change_counts(run_id)},
                  cache_control="public, max-age=3600")
    log.info("journal published to changes/%s.md", day)

#!/usr/bin/env python3
"""What is actually in R2, per project, against what we think we hold.

Lists every object under R2_JSON_PREFIX and R2_PDF_PREFIX (no downloads, only
LIST calls) and reconciles four views of the same estate:

    portal   every scheme the geo harvest enumerated (output/geo/schemes.json)
    state    state/gujrera.db: projects, endpoint hashes, documents + blob keys
    local    project folders under output/
    r2       what the bucket really holds

Two layouts live side by side in the bucket and both are understood:

    delta    <json>/projects/<pid>/raw.json.gz, <pdf>/blobs/ab/cd/<sha256>
    legacy   <json|pdf>/<DISTRICT>/<pid>_<slug>/...   (cloudflare/r2_sync.py)

Every document row in state with a blob_key is checked against the listing, so
"uploaded" means "the object exists", not "we once recorded a key".

Writes output/r2_inventory.json (per-pid detail) and output/r2_inventory.md.

Usage
    python r2_inventory.py
    python r2_inventory.py --state state/gujrera.db
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gujrera.sink import R2Sink, make_sink  # noqa: E402

REPO = Path(__file__).resolve().parent
OUTPUT = REPO / "output"
PID_FOLDER = re.compile(r"^(\d+)_")


def list_prefix(sink: R2Sink, prefix: str):
    """Yield (key, size) for every object under prefix."""
    paginator = sink.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=sink.bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents") or []:
            yield obj["Key"], obj["Size"]


def gb(n: float) -> str:
    return f"{n / 1e9:,.2f} GB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state", type=Path, default=REPO / "state" / "gujrera.db")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sink = make_sink("r2")
    pdf_prefix, json_prefix = sink.pdf_prefix, sink.json_prefix
    started = time.time()

    # ------------------------------------------------------------------ R2 listing ---
    keys: dict = {}
    per_pid = collections.defaultdict(lambda: {"objects": 0, "bytes": 0, "delta_raw": False,
                                               "legacy": False})
    totals = collections.Counter()
    for prefix in sorted({pdf_prefix, json_prefix}):
        for key, size in list_prefix(sink, prefix):
            keys[key] = size
            rest = key[len(prefix) + 1:]
            parts = rest.split("/")
            area = "pdf" if prefix == pdf_prefix else "json"
            if parts[0] == "blobs":
                totals[f"{area}.blobs"] += 1
                totals[f"{area}.blobs_bytes"] += size
                continue
            if parts[0] == "projects" and len(parts) > 1 and parts[1].isdigit():
                pid = int(parts[1])
                totals[f"{area}.delta_project_objects"] += 1
                per_pid[pid]["objects"] += 1
                per_pid[pid]["bytes"] += size
                if parts[-1] == "raw.json.gz":
                    per_pid[pid]["delta_raw"] = True
                continue
            # <DISTRICT>/<pid>_<slug>/... or, from the oldest scrapes, <pid>_<slug>/...
            folder = parts[1] if len(parts) > 2 and PID_FOLDER.match(parts[1]) else                 parts[0] if len(parts) > 1 and PID_FOLDER.match(parts[0]) else None
            if folder:
                pid = int(PID_FOLDER.match(folder).group(1))
                totals[f"{area}.legacy_objects"] += 1
                totals[f"{area}.legacy_bytes"] += size
                per_pid[pid]["objects"] += 1
                per_pid[pid]["bytes"] += size
                per_pid[pid]["legacy"] = True
                continue
            totals[f"{area}.other_objects"] += 1
            totals[f"{area}.other_bytes"] += size
    total_bytes = sum(keys.values())
    print(f"R2 bucket {sink.bucket}: {len(keys):,} objects, {gb(total_bytes)} "
          f"(listed in {time.time() - started:,.0f}s)")

    # ------------------------------------------------------------------- state DB ---
    con = sqlite3.connect(f"file:{args.state}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    state = {r["pid"]: dict(r) for r in con.execute(
        "SELECT pid, name, district, status, last_scraped_at FROM projects")}
    hashed = {r[0] for r in con.execute("SELECT DISTINCT pid FROM endpoint_hashes")}
    doc_stats = collections.defaultdict(lambda: {"ok": 0, "keyed": 0, "in_r2": 0,
                                                 "missing_in_r2": 0, "failed": 0, "bytes": 0})
    missing_examples = []
    for r in con.execute("SELECT pid, uid, status, blob_key, bytes FROM documents"):
        d = doc_stats[r["pid"]]
        if r["status"] != "ok":
            if r["status"] == "failed":
                d["failed"] += 1
            continue
        d["ok"] += 1
        d["bytes"] += r["bytes"] or 0
        if not r["blob_key"]:
            continue
        d["keyed"] += 1
        if sink._route(r["blob_key"]) in keys:
            d["in_r2"] += 1
        else:
            d["missing_in_r2"] += 1
            if len(missing_examples) < 25:
                missing_examples.append({"pid": r["pid"], "uid": r["uid"], "key": r["blob_key"]})
    con.close()

    # ---------------------------------------------------------------- local + geo ---
    local = {}
    for folder in [*OUTPUT.glob("*/*"), *OUTPUT.glob("*")]:
        m = PID_FOLDER.match(folder.name)
        if m and folder.is_dir():
            local[int(m.group(1))] = folder.relative_to(OUTPUT).as_posix()
    geo = {}
    geo_file = OUTPUT / "geo" / "schemes.json"
    if geo_file.exists():
        for rec in json.loads(geo_file.read_text(encoding="utf-8"))["records"]:
            geo[int(rec["pid"])] = rec

    # -------------------------------------------------------------- reconciliation ---
    universe = set(geo) | set(state) | set(per_pid)
    rows = []
    for pid in sorted(universe):
        s = state.get(pid) or {}
        g = geo.get(pid) or {}
        r2 = per_pid.get(pid) or {}
        d = doc_stats.get(pid) or {}
        rows.append({
            "pid": pid,
            "name": s.get("name") or g.get("name"),
            "district": (s.get("district") or g.get("district") or "").title(),
            "portal_stage": g.get("portalStage"),
            "on_portal_list": pid in geo,
            "state_status": s.get("status"),
            "scraped": pid in hashed,
            "last_scraped_at": s.get("last_scraped_at"),
            "local_folder": local.get(pid),
            "r2_delta_raw": bool(r2.get("delta_raw")),
            "r2_legacy_folder": bool(r2.get("legacy")),
            "r2_project_objects": r2.get("objects", 0),
            "r2_project_bytes": r2.get("bytes", 0),
            "docs_ok": d.get("ok", 0),
            "docs_in_r2": d.get("in_r2", 0),
            "docs_missing_in_r2": d.get("missing_in_r2", 0),
            "docs_unkeyed": d.get("ok", 0) - d.get("keyed", 0),
            "docs_failed": d.get("failed", 0),
        })

    def in_r2(row):
        return row["r2_delta_raw"] or row["r2_legacy_folder"]

    by_district = collections.defaultdict(collections.Counter)
    for row in rows:
        c = by_district[row["district"] or "?"]
        c["universe"] += 1
        c["portal_list"] += row["on_portal_list"]
        c["scraped"] += row["scraped"]
        c["in_r2"] += bool(in_r2(row))
        c["local"] += bool(row["local_folder"])
        c["not_scraped"] += not row["scraped"]
        c["scraped_not_in_r2"] += row["scraped"] and not in_r2(row)
        c["docs_ok"] += row["docs_ok"]
        c["docs_in_r2"] += row["docs_in_r2"]
        c["docs_missing_in_r2"] += row["docs_missing_in_r2"]
        c["docs_unkeyed"] += row["docs_unkeyed"]

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bucket": sink.bucket,
        "r2_objects": len(keys),
        "r2_bytes": total_bytes,
        "r2_breakdown": dict(totals),
        "by_district": {k: dict(v) for k, v in sorted(by_district.items())},
        "missing_in_r2_examples": missing_examples,
    }
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / "r2_inventory.json").write_text(
        json.dumps({"summary": summary, "projects": rows}, indent=1, ensure_ascii=False),
        encoding="utf-8")

    cols = ["universe", "portal_list", "scraped", "in_r2", "local", "not_scraped",
            "scraped_not_in_r2", "docs_ok", "docs_in_r2", "docs_missing_in_r2", "docs_unkeyed"]
    lines = [f"# R2 inventory — {summary['generated_at']}", "",
             f"Bucket `{sink.bucket}`: **{len(keys):,} objects, {gb(total_bytes)}**", "",
             "| area | count / bytes |", "|---|---|"]
    lines += [f"| {k} | {v:,} |" for k, v in sorted(totals.items())]
    lines += ["", "| district | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    grand = collections.Counter()
    for district, c in sorted(by_district.items()):
        grand.update(c)
        lines.append(f"| {district} | " + " | ".join(f"{c[k]:,}" for k in cols) + " |")
    lines.append("| **total** | " + " | ".join(f"**{grand[k]:,}**" for k in cols) + " |")
    (OUTPUT / "r2_inventory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[4:]))
    print(f"\nper-project detail: {OUTPUT / 'r2_inventory.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

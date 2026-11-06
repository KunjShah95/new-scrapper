#!/usr/bin/env python
"""
GujRERA project scraper — CLI.

Examples
--------
    python run.py --list-output                 # show what is already downloaded
    python run.py --discover                    # list ongoing Ahmedabad projects
    python run.py --scrape                      # scrape targets.json (the 10 sites)
    python run.py --scrape --no-files           # JSON + Markdown only, skip document downloads
    python run.py --project 29742               # scrape a single project by GujRERA project id
    python run.py --project PR/GJ/AHMEDABAD/... # ...or by registration number / name
    python run.py --report-only                 # rebuild the master Markdown from saved bundles
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from gujrera.api import GujreraClient, mask_proxy, payload, proxy_from_env
from gujrera.delta import DEFAULT_DISTRICTS as DELTA_DISTRICTS
from gujrera.delta import run_delta
from gujrera.geo import DEFAULT_DISTRICTS as GEO_DISTRICTS
from gujrera.geo import run_geo
from gujrera.legacy import import_legacy
from gujrera.mirror import sync_mirror
from gujrera.pipeline import discover_projects, resolve_target, run, run_districts, scrape_project
from gujrera.report import build_master_markdown, build_project_markdown
from gujrera.runlock import LockHeld, RunLock, holder_info
from gujrera.scheduler import DEFAULT_DISTRICT_ORDER, Scheduler
from gujrera.sink import Sink, make_sink
from gujrera.state import State

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
TARGETS = ROOT / "targets.json"
STATE = ROOT / "state" / "gujrera.db"


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader — no dependency, no surprises.

    Real environment variables always win, so systemd EnvironmentFile and an
    exported shell variable both behave the way you would expect.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value



def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_discover(limit: int) -> None:
    client = GujreraClient()
    projects = discover_projects(client)
    print(f"{len(projects)} ongoing Ahmedabad projects found "
          f"(sorted by declared project cost, top {limit} shown)\n")
    for index, project in enumerate(projects[:limit], start=1):
        print(
            f"{index:>3}. id={project['pid']:<6} {project['name'][:44]:<46} "
            f"{project['type'][:26]:<28} ends {project['endDate']}  ₹{project['cost']:,}"
        )
    print("\nCopy the ids you want into targets.json.")


def cmd_scrape(targets: Path, download_files: bool, limit: int | None,
               project_workers: int, endpoint_workers: int, doc_workers: int) -> None:
    bundles = run(targets, OUTPUT, download_files=download_files, limit=limit,
                   project_workers=project_workers, endpoint_workers=endpoint_workers,
                   doc_workers=doc_workers)
    total_docs = sum(len([d for d in b["documents"] if d["status"] == "ok"]) for b in bundles)
    total_fail = sum(len([d for d in b["documents"] if d["status"] != "ok"]) for b in bundles)
    print("\n" + "=" * 78)
    print(f"Projects scraped : {len(bundles)}")
    print(f"Documents saved  : {total_docs} ({total_fail} unavailable)")
    print(f"Master report    : {OUTPUT / 'AHMEDABAD_10_PROJECTS_REPORT.md'}")
    print("=" * 78)


def cmd_project(needle: str, download_files: bool, endpoint_workers: int, doc_workers: int) -> None:
    client = GujreraClient()
    target = resolve_target(client, needle)
    if not target:
        sys.exit(f"Could not resolve project '{needle}'")
    scrape_project(client, target, OUTPUT, download_files=download_files,
                    endpoint_workers=endpoint_workers, doc_workers=doc_workers)


def cmd_daily(args: argparse.Namespace, districts: list[str]) -> None:
    sink = make_sink(args.sink, root=args.sink_root, public_base=args.public_base)
    only_pids = None
    if args.project:
        needle = str(args.project).strip()
        if needle.isdigit():
            only_pids = [int(needle)]
        else:
            resolved = resolve_target(GujreraClient(), needle)
            if not resolved:
                sys.exit(f"Could not resolve project '{needle}'")
            only_pids = [int(resolved["pid"])]
    try:
        result = run_delta(
            OUTPUT, args.state, sink, districts=districts,
            download_files=not args.no_files, project_workers=args.project_workers,
            endpoint_workers=args.endpoint_workers, doc_workers=args.doc_workers,
            limit=args.limit, max_scrapes=args.max_scrapes, dry_run=args.dry_run,
            prune_local=args.prune_uploaded, force_all=args.force_all,
            backfill_only=args.backfill, only_pids=only_pids, reverify=args.reverify,
        )
    except LockHeld as exc:
        sys.exit(f"\nrefusing to start — {exc}\n")
    counts = result["counts"]
    print("\n" + "=" * 78)
    print(f"Run #{result['run_id']}  ({result['elapsed']:.0f}s)"
          f"{'  [dry run]' if result['dry_run'] else ''}")
    print(f"Discovered        : {counts['discovered']:,}")
    print(f"Probed            : {counts['probed_projects']:,} known project(s)")
    print(f"New projects      : {counts['new_projects']:,}")
    print(f"Scraped in full   : {counts['changed_projects']:,}")
    print(f"Documents new     : {counts['docs_new']:,}  "
          f"(reused {counts['docs_reused']:,}, failed {counts['docs_failed']:,})")
    print(f"Downloaded        : {counts['bytes_downloaded'] / 1e6:.1f} MB")
    if result["deferred"]:
        print(f"Deferred          : {result['deferred']:,} (raise --max-scrapes to drain faster)")
    if result["gone"]:
        print(f"Vanished          : {len(result['gone']):,} (see the change report)")
    print(f"Change report     : {OUTPUT / 'changes'}")
    print("=" * 78)


def cmd_runs(state_path: Path) -> None:
    state = State(state_path)
    try:
        rows = state.recent_runs(12)
        if not rows:
            print("No runs recorded yet.")
            return
        print(f"{'#':>4}  {'started':<20} {'mode':<9} {'new':>4} {'scraped':>7} "
              f"{'docs':>5}  {'state':<11} error")
        for row in rows:
            if row["error"]:
                mark = "failed"
            elif row["finished_at"]:
                mark = "ok"
            else:
                mark = "RUNNING"
            print(f"{row['id']:>4}  {row['started_at']:<20} {row['mode']:<9} "
                  f"{row['new_projects'] or 0:>4} {row['changed_projects'] or 0:>7} "
                  f"{row['docs_new'] or 0:>5}  {mark:<11} {(row['error'] or '')[:26]}")
        print(f"\nState: {state_path}  ({state_path.stat().st_size / 1024:.0f} KB)")
    finally:
        state.close()

    holder = holder_info(state_path.parent / "run.lock")
    if holder:
        state_mark = "stale" if holder.get("stale") else "active"
        print(f"Lock : {state_mark} — pid {holder.get('pid')} on {holder.get('host')} "
              f"since {holder.get('started')} ({holder.get('purpose')})")
    else:
        print("Lock : free")


def cmd_coverage(state_path: Path, stale_days: int) -> None:
    state = State(state_path)
    try:
        cov = state.coverage(stale_after_days=stale_days)
    finally:
        state.close()

    print("\nProjects — is it scraped?")
    print(f"{'District':<14} {'Active':>8} {'Captured':>9} {'Never':>7} "
          f"{'Stale':>7}  Last capture")
    for row in cov["districts"]:
        print(f"{row['district']:<14} {row['active']:>8,} {row['captured']:>9,} "
              f"{row['never']:>7,} {row['stale']:>7,}  {row['last_capture'] or '—'}")
    print(f"{'TOTAL':<14} {cov['total_active']:>8,} {cov['total_captured']:>9,} "
          f"{cov['total_active'] - cov['total_captured']:>7,}")

    print(f"\nCompleteness — endpoint fingerprints held")
    print(f"  Best case per project : {cov['hash_endpoints_expected']} endpoints")
    print(f"  Active with no hashes : {cov['never_hashed']:,} (never fully scraped)")
    partial = cov["partial_projects"]
    print(f"  Partial captures      : {len(partial):,}"
          + (f"  e.g. {', '.join(str(p) for p in partial[:8])}" if partial else ""))

    docs = cov["documents"]
    ok = docs.get("ok", 0)
    failed = docs.get("failed", 0)
    removed = docs.get("removed", 0)
    print(f"\nDocuments")
    print(f"  ok / failed / removed : {ok:,} / {failed:,} / {removed:,}")
    print(f"  in R2 (blob_key set)  : {cov['blobs']['count']:,}"
          f"  ({cov['blobs']['bytes'] / 1e9:.2f} GB)")
    print(f"  label versions kept   : {cov['versions']:,}")
    print(f"\nLast run: {cov['last_run'] or '—'}")
    print(f"State   : {state_path}")
    print()


def cmd_import_legacy(state_path: Path, output_root: Path, dry_run: bool) -> None:
    """Fold work done before the delta engine existed into the state store.

    Reads no network and downloads nothing — it only turns on-disk manifests and
    raw responses into the rows the delta engine would have written.
    """
    sink = make_sink("auto")  # only to pick up prefixes from cloudflare/.env
    pdf_prefix = getattr(sink, "pdf_prefix", "") or os.environ.get("R2_PDF_PREFIX", "")
    json_prefix = getattr(sink, "json_prefix", "") or os.environ.get("R2_JSON_PREFIX", "")

    print(f"\nImporting {output_root}")
    print(f"  pdf prefix  : {pdf_prefix or '(none)'}")
    print(f"  json prefix : {json_prefix or '(none)'}")
    print(f"  mode        : {'DRY RUN' if dry_run else 'write'}\n")

    state = State(state_path)
    try:
        lock = RunLock(state_path.parent / "run.lock", purpose="import-legacy")
        lock.acquire()
        try:
            totals = import_legacy(state, output_root, pdf_prefix, json_prefix,
                                   dry_run=dry_run)
        finally:
            lock.release()
        cov = state.coverage() if not dry_run else None
    finally:
        state.close()

    print(f"  projects scanned      : {totals['projects']:,}"
          f"  ({totals['new_projects']:,} new to state)")
    print(f"  documents             : {totals['documents']:,} new"
          f"  ({totals['skipped']:,} already known)")
    print(f"  with an R2 key        : {totals['blobs']:,}"
          f"  ({totals['bytes'] / 1e9:.2f} GB)")
    print(f"  endpoint fingerprints : {totals['hashes']:,}"
          f"  ({totals['overwritten_hashes']:,} projects kept live hashes)")

    if cov:
        print(f"\nAfter import")
        for row in cov["districts"]:
            print(f"  {row['district']:<14} {row['captured']:>6,} of {row['active']:>6,} captured")
    print()


def cmd_sync_mirror(sink: Sink, root: Path, dry_run: bool) -> None:
    """Republish local delta artifacts in the current sink."""
    print(f"\nSyncing {root} -> {sink.name}\n")
    if dry_run:
        totals = sync_mirror(sink, root, dry_run=True)
    else:
        with RunLock(root.parent / "state" / "run.lock", purpose="sync-mirror"):
            totals = sync_mirror(sink, root, dry_run=False)

    verb = "would push" if dry_run else "pushed"
    print(f"  files found        : {totals['found']:,}"
          f"  ({totals['bytes'] / 1e9:.2f} GB)")
    print(f"  {verb:<19}: {totals['pushed']:,}")
    print(f"  already in the sink: {totals['skipped']:,}")
    print()


def cmd_audit(state_path: Path, pid: int) -> None:
    """Everything we know about one project, and every reason to doubt it."""
    state = State(state_path)
    try:
        project = state.get_project(pid)
        if not project:
            sys.exit(f"pid {pid} is not in state")
        hashes = state.endpoint_hashes(pid)
        docs = state.documents_for(pid)
        versions = state.document_history(pid)
    finally:
        state.close()

    print(f"\n{project.get('name')}  ({pid})")
    print(f"  reg no    : {project.get('reg_no')}")
    print(f"  district  : {project.get('district')}  taluka: {project.get('taluka')}")
    print(f"  status    : {project.get('status')}")
    print(f"  first seen: {project.get('first_seen_at')}   last seen: {project.get('last_seen_at')}")
    print(f"  scraped   : {project.get('last_scraped_at') or 'NEVER'}"
          f"   probed: {project.get('last_probed_at') or '—'}")

    print(f"\n  endpoint fingerprints: {len(hashes)}")
    for name in sorted(hashes):
        print(f"    {name:<22} {hashes[name][:16]}…")

    ok = [d for d in docs if d["status"] == "ok"]
    print(f"\n  documents: {len(ok)} ok of {len(docs)}"
          f"   (uploaded: {sum(1 for d in ok if d.get('blob_key'))})")
    for doc in docs:
        flag = "ok " if doc["status"] == "ok" else doc["status"]
        blob = "R2" if doc.get("blob_key") else "--"
        print(f"    [{flag:<7}] {blob}  {doc['label'][:44]:<46} "
              f"{doc['uid'][:10]}…  v{(doc['sha256'] or '')[:8] or '—'}")
    if versions:
        print(f"\n  version history: {len(versions)} entries")
        for version in versions:
            print(f"    {version['label'][:40]:<42} v{version['version']}  "
                  f"{version['uid'][:10]}…  {version['captured_at']}")
    print()


def cmd_schedule(interval: float, download_files: bool, limit: int | None,
                  project_workers: int, endpoint_workers: int, doc_workers: int,
                  districts: list[str]) -> None:
    scheduler = Scheduler(
        OUTPUT, interval=interval, project_workers=project_workers,
        endpoint_workers=endpoint_workers, doc_workers=doc_workers,
        download_files=download_files, limit=limit, district_order=districts,
    )
    print(f"Scheduler starting: districts {' -> '.join(districts)}, every {interval:.0f}s, "
          f"{project_workers} projects in parallel (Ctrl+C to stop)")
    scheduler.run_forever()


def cmd_scrape_districts(districts: list[str], download_files: bool, limit: int | None,
                          project_workers: int, endpoint_workers: int, doc_workers: int) -> None:
    print(f"Scraping districts in order: {' -> '.join(districts)}")
    all_bundles = run_districts(
        districts, OUTPUT, download_files=download_files, limit=limit,
        project_workers=project_workers, endpoint_workers=endpoint_workers,
        doc_workers=doc_workers,
    )
    print("\n" + "=" * 78)
    for district in districts:
        bundles = all_bundles.get(district, [])
        docs_ok = sum(len([d for d in b["documents"] if d["status"] == "ok"]) for b in bundles)
        print(f"{district:<14} {len(bundles):>3} projects  {docs_ok:>4} documents")
    print("=" * 78)


def cmd_geo(districts: list[str], workers: int, limit: int | None, refresh: bool) -> None:
    summary = run_geo(OUTPUT, districts, workers=workers, refresh=refresh, limit=limit)
    print("\n" + "=" * 78)
    if summary.get("sample"):
        print("SAMPLE RUN — the canonical dataset in output/geo was left untouched")
    print(f"Schemes harvested : {summary['total']}")
    print(f"Mapped (lat/lng)  : {summary['withCoordinates']}")
    print(f"Exact boundaries  : {summary['withBoundary']}")
    for key, info in summary["districts"].items():
        print(f"  {info['label']:<12} {info['total']:>5} schemes  "
              f"{info['withCoordinates']:>5} mapped  {info['withBoundary']:>5} with boundary  "
              f"{info['underConstruction']:>5} under construction  "
              f"{info['pastDue']:>5} past due")
    print(f"Dataset           : {summary.get('datasetPath')}")
    print("Next              : python build_map.py   (writes docs/map.html)")
    print("=" * 78)


def cmd_build_map() -> None:
    import build_map  # imported lazily: only needed for the static site step

    build_map.run()


def cmd_proxy_test(proxy: str | None) -> None:
    """Report what egress the scraper uses, and whether the portal is reachable that way."""
    import time

    import requests

    configured = proxy if proxy is not None else proxy_from_env()
    target = "https://gujrera.gujarat.gov.in/project_reg/public/alldatabyprojectid/29742"

    def probe(label: str, url: str, proxies: dict | None, timeout: int = 45) -> bool:
        started = time.time()
        try:
            resp = requests.get(url, proxies=proxies, timeout=timeout,
                                headers={"Accept": "application/json"})
            elapsed = (time.time() - started) * 1000
            snippet = resp.text.strip().replace("\n", " ")[:70]
            print(f"  {label:<26} HTTP {resp.status_code}  {elapsed:6.0f} ms  {snippet}")
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic
            reason = str(exc)
            if "SITE_PERMANENTLY_BLOCKED" in reason.upper():
                reason = "the proxy provider blocks this site"
            elif "Tunnel connection failed" in reason:
                reason = "the proxy refused to tunnel to this host"
            print(f"  {label:<26} FAILED      {type(exc).__name__}: {reason[:110]}")
            return False

    print("=" * 78)
    print(f"proxy configured : {mask_proxy(configured)}")
    print(f"target           : {target}")
    print("=" * 78)

    print("\ndirect connection")
    direct_ip = probe("egress IP (ipify)", "https://api.ipify.org?format=json", None)
    direct_ok = probe("portal", target, None)

    proxied_ok = False
    if configured:
        proxies = {"http": configured, "https": configured}
        print("\nthrough the proxy")
        probe("egress IP (ipify)", "https://api.ipify.org?format=json", proxies)
        proxied_ok = probe("portal", target, proxies)
    else:
        print("\nno proxy configured (set GUJRERA_PROXY or PROXY_HOST/PORT/USER/PASS)")

    print("\n" + "-" * 78)
    if proxied_ok:
        print("RESULT: the proxy can reach the portal — set it in .env and runs will use it.")
    elif configured and direct_ok:
        print("RESULT: the proxy cannot reach the portal, but a direct connection can.")
        print("        The scraper will detect this and fall back to direct automatically.")
        print("        If you need the portal proxied, ask the provider to unblock the domain")
        print("        (residential providers routinely block *.gov.in).")
    elif direct_ok:
        print("RESULT: direct connection works; no proxy is needed for this portal.")
    else:
        print("RESULT: neither route reached the portal — check the network / portal status.")
    print("-" * 78)


def cmd_report_only() -> None:
    bundles = []
    for folder in sorted(OUTPUT.glob("*/project_bundle.json")):
        bundle = json.loads(folder.read_text(encoding="utf-8"))
        project_dir = folder.parent
        raw = {}
        for raw_file in (project_dir / "raw").glob("*.json"):
            raw[raw_file.stem] = json.loads(raw_file.read_text(encoding="utf-8"))
        # Endpoints that failed were never written to raw/; keep them visible in the appendix.
        for name in bundle.get("errors") or {}:
            raw.setdefault(name, None)
        bundle["raw"] = raw
        manifest = project_dir / "documents_manifest.json"
        bundle["documents"] = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
        md_name = bundle.get("markdown_name") or f"{project_dir.name}.md"
        (project_dir / md_name).write_text(build_project_markdown(bundle), encoding="utf-8")
        bundles.append(bundle)
    if not bundles:
        sys.exit("No saved bundles found in output/")
    master = OUTPUT / "AHMEDABAD_10_PROJECTS_REPORT.md"
    master.write_text(build_master_markdown(bundles), encoding="utf-8")
    print(f"Rebuilt {master} from {len(bundles)} bundles")


def cmd_list() -> None:
    if not OUTPUT.exists():
        print("Nothing downloaded yet - run:  python run.py --scrape")
        return
    for folder in sorted(p for p in OUTPUT.iterdir() if p.is_dir()):
        files = list((folder / "files").glob("*")) if (folder / "files").exists() else []
        size = sum(f.stat().st_size for f in files) / 1e6
        md = next(folder.glob("*.md"), None)
        print(f"{folder.name:<58} {len(files):>3} files {size:>7.1f} MB  {md.name if md else ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape GujRERA project data (public pages).")
    parser.add_argument("--discover", action="store_true", help="list ongoing Ahmedabad projects")
    parser.add_argument("--scrape", action="store_true", help="scrape every project in targets.json")
    parser.add_argument("--project", help="scrape a single project (id, registration no. or name)")
    parser.add_argument("--report-only", action="store_true", help="rebuild master Markdown from saved data")
    parser.add_argument("--geo", action="store_true",
                         help="harvest every scheme's exact location + details for --districts "
                              "into output/geo (lat/lng, boundary, full detail set)")
    parser.add_argument("--build-map", action="store_true",
                         help="build docs/map.html from output/geo/schemes.json")
    parser.add_argument("--geo-workers", type=int, default=12,
                         help="concurrent detail requests while harvesting locations")
    parser.add_argument("--refresh-geo", action="store_true",
                         help="ignore the geo cache and re-harvest everything")
    parser.add_argument("--list-output", action="store_true", help="show downloaded projects")
    parser.add_argument("--daily", action="store_true",
                         help="incremental run: discover, probe, scrape only what changed, "
                              "push to the sink and write the change journal")
    parser.add_argument("--backfill", action="store_true",
                         help="with --daily: only scrape projects never captured before, "
                              "skipping the probe (use for the first multi-day crawl)")
    parser.add_argument("--sink", choices=("auto", "local", "r2"), default="auto",
                         help="where bytes go: auto picks r2 when R2_BUCKET is set, else local")
    parser.add_argument("--sink-root", type=Path, default=ROOT / "r2_mirror",
                         help="directory used by the local sink")
    parser.add_argument("--public-base", default=None,
                         help="public URL prefix for blobs (e.g. https://files.example.com)")
    parser.add_argument("--state", type=Path, default=STATE,
                         help="SQLite state file (default: state/gujrera.db)")
    parser.add_argument("--dry-run", action="store_true",
                         help="with --daily: discover and probe, report what would be scraped")
    parser.add_argument("--max-scrapes", type=int, default=None,
                         help="cap full scrapes in this run; the rest are deferred")
    parser.add_argument("--prune-uploaded", action="store_true",
                         help="delete local document files once they are safely in the sink")
    parser.add_argument("--direct-r2", action="store_true",
                         help="upload each document to R2 as it is scraped and delete the local copy, "
                              "so nothing accumulates on this machine (same keys as r2_sync.py)")
    parser.add_argument("--force-all", action="store_true",
                         help="with --daily: re-scrape every active project regardless of hashes")
    parser.add_argument("--reverify", action="store_true",
                         help="the scheduled weekly run: re-scrape every active project, walking "
                              "all endpoints (oldest-scraped first), so documents added to tabs the "
                              "probe does not watch are still caught. Documents already held are "
                              "reused by UID, so this costs JSON only")
    parser.add_argument("--runs", action="store_true", help="show recent run history")
    parser.add_argument("--coverage", action="store_true",
                         help="how much of the estate is captured, and how complete it is")
    parser.add_argument("--stale-days", type=int, default=7,
                         help="with --coverage: count captures older than N days as stale")
    parser.add_argument("--audit", type=int, metavar="PID", default=None,
                         help="show every endpoint fingerprint and document for one project")
    parser.add_argument("--import-legacy", action="store_true",
                         help="fold projects already in output/ (scraped before the delta "
                              "engine existed) into the state store; no network")
    parser.add_argument("--sync-mirror", action="store_true",
                         help="publish r2_mirror/ artifacts through the current sink "
                              "(dry-run with --dry-run)")
    parser.add_argument("--proxy", default=None, metavar="URL",
                         help="egress proxy, e.g. http://user:pass@host:port "
                              "(overrides GUJRERA_PROXY / PROXY_* in .env)")
    parser.add_argument("--no-proxy", action="store_true",
                         help="ignore any proxy configured in .env and go direct")
    parser.add_argument("--proxy-test", action="store_true",
                         help="check direct vs proxied egress and whether the portal is "
                              "reachable through the proxy, then exit")
    parser.add_argument("--targets", type=Path, default=TARGETS, help="targets JSON file")
    parser.add_argument("--no-files", action="store_true", help="skip document downloads")
    parser.add_argument("--limit", type=int, default=None, help="only the first N targets")
    parser.add_argument("--limit-list", type=int, default=25, help="rows shown by --discover")
    parser.add_argument("--schedule", action="store_true",
                         help="run forever: sweep --districts in order (each fully finished, "
                              "concurrently, before the next starts), every --interval seconds")
    parser.add_argument("--scrape-districts", action="store_true",
                         help="one-shot: sweep --districts in order, each fully finished "
                              "(all ongoing projects, concurrently) before the next starts")
    parser.add_argument("--districts", default=",".join(DEFAULT_DISTRICT_ORDER),
                         help="comma-separated district order, e.g. "
                              "ahmedabad,gandhinagar,mehsana (default: %(default)s)")
    parser.add_argument("--interval", type=float, default=3600.0,
                         help="seconds between scheduler cycles (default 3600 = 1h)")
    parser.add_argument("--project-workers", type=int,
                         default=int(os.environ.get("GUJRERA_PROJECT_WORKERS") or 4),
                         help="projects scraped concurrently (default: %(default)s)")
    parser.add_argument("--endpoint-workers", type=int,
                         default=int(os.environ.get("GUJRERA_ENDPOINT_WORKERS") or 8),
                         help="tabs/endpoints fetched concurrently per project (default: %(default)s)")
    parser.add_argument("--doc-workers", type=int,
                         default=int(os.environ.get("GUJRERA_DOC_WORKERS") or 8),
                         help="documents downloaded concurrently per project (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    load_env()

    # --direct-r2: documents go to the bucket and are removed locally.
    if getattr(args, "direct_r2", False):
        os.environ["GUJRERA_DIRECT_R2"] = "1"

    # Proxy selection: CLI wins over .env, --no-proxy clears it entirely.
    if args.no_proxy:
        for key in ("GUJRERA_PROXY", "PROXY_HOST", "PROXY_PORT", "PROXY_USER",
                    "PROXY_PASS", "PROXY_SCHEME", "PROXY_USER_SUFFIX"):
            os.environ.pop(key, None)
    elif args.proxy:
        os.environ["GUJRERA_PROXY"] = args.proxy

    districts = [d.strip() for d in args.districts.split(",") if d.strip()]

    if args.proxy_test:
        cmd_proxy_test(args.proxy)
        return

    if args.list_output:
        cmd_list()
    elif args.discover:
        cmd_discover(args.limit_list)
    elif args.runs:
        cmd_runs(args.state)
    elif args.coverage:
        cmd_coverage(args.state, args.stale_days)
    elif args.audit is not None:
        cmd_audit(args.state, args.audit)
    elif args.import_legacy:
        cmd_import_legacy(args.state, OUTPUT, args.dry_run)
    elif args.sync_mirror:
        cmd_sync_mirror(make_sink(args.sink, root=args.sink_root, public_base=args.public_base),
                        args.sink_root, args.dry_run)
    elif args.daily or args.backfill:
        cmd_daily(args, districts or list(DELTA_DISTRICTS))
    elif args.geo:
        cmd_geo(districts or GEO_DISTRICTS, args.geo_workers, args.limit, args.refresh_geo)
    elif args.build_map:
        cmd_build_map()
    elif args.report_only:
        cmd_report_only()
    elif args.schedule:
        cmd_schedule(args.interval, not args.no_files, args.limit,
                     args.project_workers, args.endpoint_workers, args.doc_workers, districts)
    elif args.scrape_districts:
        cmd_scrape_districts(districts, not args.no_files, args.limit,
                              args.project_workers, args.endpoint_workers, args.doc_workers)
    elif args.project:
        cmd_project(args.project, not args.no_files, args.endpoint_workers, args.doc_workers)
    elif args.scrape:
        cmd_scrape(args.targets, not args.no_files, args.limit,
                   args.project_workers, args.endpoint_workers, args.doc_workers)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

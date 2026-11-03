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
import sys
from pathlib import Path

from gujrera.api import GujreraClient, payload
from gujrera.pipeline import discover_projects, resolve_target, run, scrape_project
from gujrera.report import build_master_markdown, build_project_markdown

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
TARGETS = ROOT / "targets.json"


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


def cmd_scrape(targets: Path, download_files: bool, limit: int | None) -> None:
    bundles = run(targets, OUTPUT, download_files=download_files, limit=limit)
    total_docs = sum(len([d for d in b["documents"] if d["status"] == "ok"]) for b in bundles)
    total_fail = sum(len([d for d in b["documents"] if d["status"] != "ok"]) for b in bundles)
    print("\n" + "=" * 78)
    print(f"Projects scraped : {len(bundles)}")
    print(f"Documents saved  : {total_docs} ({total_fail} unavailable)")
    print(f"Master report    : {OUTPUT / 'AHMEDABAD_10_PROJECTS_REPORT.md'}")
    print("=" * 78)


def cmd_project(needle: str, download_files: bool) -> None:
    client = GujreraClient()
    target = resolve_target(client, needle)
    if not target:
        sys.exit(f"Could not resolve project '{needle}'")
    scrape_project(client, target, OUTPUT, download_files=download_files)


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
    parser.add_argument("--list-output", action="store_true", help="show downloaded projects")
    parser.add_argument("--targets", type=Path, default=TARGETS, help="targets JSON file")
    parser.add_argument("--no-files", action="store_true", help="skip document downloads")
    parser.add_argument("--limit", type=int, default=None, help="only the first N targets")
    parser.add_argument("--limit-list", type=int, default=25, help="rows shown by --discover")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.list_output:
        cmd_list()
    elif args.discover:
        cmd_discover(args.limit_list)
    elif args.report_only:
        cmd_report_only()
    elif args.project:
        cmd_project(args.project, not args.no_files)
    elif args.scrape:
        cmd_scrape(args.targets, not args.no_files, args.limit)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

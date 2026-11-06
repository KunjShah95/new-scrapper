#!/usr/bin/env python3
"""Completeness check: were the documents the portal told us about actually fetched?

The manifests only prove that what we *have* is consistent. This asks the harder
question: are there documents sitting in the cached API payloads (`raw/`) that
never made it into a manifest and therefore were never downloaded?

It re-runs the scraper's own UID harvester over every `raw/*.json`, so the
comparison is apples-to-apples with how the manifests were built.

Usage
    python check_completeness.py
    python check_completeness.py --examples 20
    python check_completeness.py --report cloudflare/completeness.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gujrera.documents import harvest_raw_dir, label_index  # noqa: E402

OUTPUT = Path(__file__).resolve().parent / "output"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check documents discovered vs downloaded")
    parser.add_argument("--examples", type=int, default=10)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    projects = 0
    no_manifest = []
    gaps = []          # (project, label, uid)
    totals = {"expected": 0, "in_manifest": 0, "in_both": 0}

    for raw_dir in sorted(OUTPUT.rglob("raw")):
        if not raw_dir.is_dir():
            continue
        project = raw_dir.parent
        rel = project.relative_to(OUTPUT).as_posix()

        expected = label_index(harvest_raw_dir(raw_dir))
        if not expected:
            continue

        projects += 1
        totals["expected"] += len(expected)

        manifest = project / "documents_manifest.json"
        if not manifest.exists():
            no_manifest.append((rel, len(expected)))
            continue

        try:
            rows = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            no_manifest.append((rel, len(expected)))
            continue

        have = {r["uid"] for r in rows if r.get("uid")}
        totals["in_manifest"] += len(have)
        both = set(expected) & have
        totals["in_both"] += len(both)

        for uid in sorted(set(expected) - have):
            gaps.append({"project": rel, "label": expected[uid], "uid": uid})

    print(f"projects with cached API payloads : {projects:,}")
    print(f"documents the portal referenced   : {totals['expected']:,}")
    print(f"documents in a manifest           : {totals['in_manifest']:,}")
    print(f"documents in both                 : {totals['in_both']:,}")
    print(f"\nnever downloaded                  : {len(gaps):,}")
    print(f"projects with no manifest at all  : {len(no_manifest):,}")

    if gaps:
        by_project: dict[str, int] = {}
        for gap in gaps:
            by_project[gap["project"]] = by_project.get(gap["project"], 0) + 1
        print("\ntop projects by missing documents:")
        for project, count in sorted(by_project.items(), key=lambda kv: -kv[1])[: args.examples]:
            print(f"  {count:>4}  {project}")
        print("\nexamples:")
        for gap in gaps[: args.examples]:
            print(f"  {gap['project']}\n      {gap['label']}  ({gap['uid'][:28]}...)")

    if no_manifest:
        print("\nprojects with cached payloads but no manifest (nothing was downloaded):")
        for rel, count in no_manifest[: args.examples]:
            print(f"  {count:>4} documents  {rel}")

    if args.report:
        Path(args.report).write_text(
            json.dumps({"totals": totals, "gaps": gaps,
                        "no_manifest": [{"project": p, "documents": n} for p, n in no_manifest]},
                       indent=2),
            encoding="utf-8",
        )
        print(f"\nreport -> {args.report}")

    complete = not gaps and not no_manifest
    print("\nCOMPLETE: every referenced document was downloaded" if complete
          else "\nGAPS FOUND: see above")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())

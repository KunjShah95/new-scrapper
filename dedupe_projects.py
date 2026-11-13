#!/usr/bin/env python3
"""Find and remove duplicate project folders (same pid scraped twice).

A pid can end up with two folders because the same project is scraped once by
`--scrape` (written to output/<pid>_<NAME>) and again by the district crawl
(written to output/<DISTRICT>/<pid>_<NAME>). The payloads are identical.

The canonical copy is the district one when present, because that matches the
layout of the other ~2,100 projects. Redundant copies are moved to
`_work/duplicates/` rather than deleted, so nothing is lost if this guess is
wrong.

Usage
    python dedupe_projects.py --dry-run
    python dedupe_projects.py --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUTPUT = REPO / "output"
QUARANTINE = REPO / "_work" / "duplicates"
PATTERN = re.compile(r"^(\d+)_")


def payload_bytes(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def scan() -> dict[int, list[Path]]:
    by_pid: dict[int, list[Path]] = defaultdict(list)
    for folder in OUTPUT.rglob("*"):
        if not folder.is_dir():
            continue
        match = PATTERN.match(folder.name)
        if not match:
            continue
        if not ((folder / "files").exists() or (folder / "raw").exists()
                or (folder / "documents_manifest.json").exists()):
            continue
        by_pid[int(match.group(1))].append(folder)
    return {pid: paths for pid, paths in by_pid.items() if len(paths) > 1}


def rank(folder: Path) -> tuple[int, int]:
    """Higher is better: district-nested beats root-level, then larger."""
    nested = len(folder.relative_to(OUTPUT).parts) > 1
    return (1 if nested else 0, payload_bytes(folder))


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove duplicate project folders")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--r2-keys", action="store_true",
                        help="also print the R2 keys that the redundant copies produced")
    args = parser.parse_args()
    if not (args.dry_run or args.apply):
        parser.error("choose --dry-run or --apply")

    dupes = scan()
    if not dupes:
        print("no duplicate pids found - every project is stored once")
        return 0

    print(f"duplicate pids: {len(dupes)}\n")
    wasted = 0
    planned: list[tuple[Path, Path]] = []

    for pid in sorted(dupes):
        paths = sorted(dupes[pid], key=rank, reverse=True)
        keep, drop = paths[0], paths[1:]
        print(f"pid {pid}")
        print(f"   keep  {keep.relative_to(OUTPUT).as_posix()}")
        for folder in drop:
            size = payload_bytes(folder)
            wasted += size
            print(f"   drop  {folder.relative_to(OUTPUT).as_posix()}   ({size / 1048576:,.1f} MB)")
            planned.append((folder, keep))
        print()

    print(f"redundant payload: {wasted / 1048576:,.1f} MB")

    if args.r2_keys:
        try:
            import sys as _sys
            _sys.path.insert(0, str(REPO / "cloudflare"))
            import r2_sync

            r2_sync.load_env(REPO / "cloudflare" / ".env")
            print("\nR2 keys that belong to the copies being dropped:")
            for folder, _ in planned:
                for f in folder.rglob("*"):
                    if f.is_file():
                        print(f"   {r2_sync.destination(f)}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(could not compute R2 keys: {exc})")

    if args.dry_run:
        print("\ndry run - nothing moved")
        return 0

    for folder, _ in planned:
        target = QUARANTINE / folder.relative_to(OUTPUT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(target))
        print(f"moved {folder.relative_to(OUTPUT).as_posix()} -> {target.relative_to(REPO).as_posix()}")
    print(f"\n{len(planned)} folder(s) quarantined under {QUARANTINE.relative_to(REPO)}")
    print("Delete them once you are satisfied, then re-run r2_sync to confirm nothing is missing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

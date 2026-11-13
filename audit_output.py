#!/usr/bin/env python3
"""Audit output/ for genuinely corrupt or missing documents.

Different from an upload check: this inspects the *source* files and the
documents_manifest.json rows that describe them. A file that is empty, truncated
or was never written is a scraper problem, not a transfer problem - but it ends
up in R2 just the same, so it is worth knowing about.

Checks per file type
    pdf     "%PDF-" inside the first 1024 bytes (the spec allows leading junk)
            and "%%EOF" inside the last 2048 bytes (catches truncation)
    jpeg    SOI marker FFD8 at the start, EOI marker FFD9 at the end
    png     PNG signature, IEND chunk at the end
    zip     "PK\\x03\\x04"
    json    parses as JSON
    other   non-empty

Checks per manifest row
    bytes recorded vs actual file size, file present at all, status vs reality

Usage
    python audit_output.py                 # summary + first examples
    python audit_output.py --examples 50
    python audit_output.py --no-json       # skip JSON parsing (faster)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUTPUT = REPO / "output"


def check_bytes(path: Path, suffix: str, parse_json: bool) -> str | None:
    """Return a problem description, or None when the file looks intact."""
    size = path.stat().st_size
    if size == 0:
        return "zero bytes"

    suffix = suffix.lower()
    if suffix == ".pdf":
        with open(path, "rb") as handle:
            head = handle.read(1024)
            if b"%PDF-" not in head:
                return "no %PDF- header in first 1024 bytes"
            handle.seek(max(0, size - 2048))
            tail = handle.read()
            if b"%%EOF" not in tail:
                return "no %%EOF trailer (truncated?)"
        return None

    if suffix in (".jpg", ".jpeg", ".jfif"):
        with open(path, "rb") as handle:
            head = handle.read(3)
            if head[:2] != b"\xff\xd8":
                return "not a JPEG (missing SOI marker)"
            handle.seek(max(0, size - 64))
            tail = handle.read().rstrip(b"\x00")
            if not tail.endswith(b"\xff\xd9"):
                return "JPEG has no EOI marker (truncated?)"
        return None

    if suffix == ".png":
        with open(path, "rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return "not a PNG (bad signature)"
            handle.seek(max(0, size - 32))
            if b"IEND" not in handle.read():
                return "PNG has no IEND chunk (truncated?)"
        return None

    if suffix == ".zip":
        with open(path, "rb") as handle:
            if handle.read(4) != b"PK\x03\x04":
                return "not a ZIP (bad signature)"
        return None

    if suffix in (".json", ".geojson") and parse_json:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return f"invalid JSON: {str(exc)[:70]}"
        return None

    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit output/ for corrupt documents")
    parser.add_argument("--examples", type=int, default=12, help="examples shown per category")
    parser.add_argument("--no-json", action="store_true", help="skip JSON parsing")
    args = parser.parse_args()

    if not OUTPUT.exists():
        sys.exit(f"no output folder at {OUTPUT}")

    problems: dict[str, list[str]] = defaultdict(list)
    checked = Counter()
    skipped_raw = 0

    files = [p for p in OUTPUT.rglob("*") if p.is_file()]
    print(f"scanning {len(files):,} files ...")

    for i, path in enumerate(files, 1):
        rel = path.relative_to(OUTPUT).as_posix()
        in_raw = "raw" in rel.split("/")[:-1]
        if in_raw:
            skipped_raw += 1
            continue
        checked[path.suffix.lower()] += 1
        try:
            problem = check_bytes(path, path.suffix, not args.no_json)
        except OSError as exc:
            problem = f"unreadable: {exc}"
        if problem:
            problems[problem].append(rel)
        if i % 20000 == 0:
            print(f"  {i:,}/{len(files):,} ...", end="\r")

    print(" " * 40, end="\r")

    total_problems = sum(len(v) for v in problems.values())
    print(f"\nchecked {sum(checked.values()):,} files (raw/ skipped: {skipped_raw:,})")
    print(f"file types: " + ", ".join(f"{k or '(none)'}={v:,}" for k, v in checked.most_common(8)))

    if total_problems:
        print(f"\n--- {total_problems:,} problematic file(s) ---")
        for reason, paths in sorted(problems.items(), key=lambda kv: -len(kv[1])):
            print(f"\n{reason}  ({len(paths):,})")
            for rel in paths[: args.examples]:
                print(f"    {rel}")
            if len(paths) > args.examples:
                print(f"    ... and {len(paths) - args.examples:,} more")
    else:
        print("\nno corrupt files found")

    # ---- manifest cross-check ------------------------------------------------
    print("\n--- manifest cross-check ---")
    bad_rows: list[str] = []
    manifests = 0
    rows_total = 0
    for manifest in OUTPUT.rglob("documents_manifest.json"):
        if "raw" in manifest.relative_to(OUTPUT).as_posix().split("/")[:-1]:
            continue
        manifests += 1
        try:
            rows = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            bad_rows.append(f"{manifest.relative_to(OUTPUT)}: unreadable manifest")
            continue
        project = manifest.parent
        for row in rows:
            rows_total += 1
            name = row.get("file") or ""
            size = row.get("bytes", 0)
            status = row.get("status", "")
            if not name:
                if not status.startswith("failed"):
                    bad_rows.append(f"{project.name}/{row.get('label')}: no file recorded, status={status!r}")
                continue
            target = project / "files" / name
            if not target.exists():
                bad_rows.append(f"{project.name}/files/{name}: recorded but missing on disk")
            elif target.stat().st_size != size:
                bad_rows.append(f"{project.name}/files/{name}: manifest says {size} B, disk has {target.stat().st_size} B")
            if size == 0 and status == "ok":
                bad_rows.append(f"{project.name}/files/{name}: 0 bytes but status='ok'")

    print(f"{manifests:,} manifests, {rows_total:,} document rows")
    if bad_rows:
        print(f"{len(bad_rows):,} inconsistent row(s):")
        for line in bad_rows[: args.examples]:
            print(f"    {line}")
        if len(bad_rows) > args.examples:
            print(f"    ... and {len(bad_rows) - args.examples:,} more")
    else:
        print("every manifest row matches the file on disk")

    return 0 if not total_problems and not bad_rows else 1


if __name__ == "__main__":
    sys.exit(main())

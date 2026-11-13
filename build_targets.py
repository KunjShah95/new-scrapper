"""Build a complete scrape target list from the geo dataset.

`discover_projects` only finds projects whose name matches 10 keywords, which is
why the daily crawl plateaus at ~29% coverage. The geo harvest enumerates by
registration-number prefix instead and already knows every scheme, so the pids
there are the authoritative work list.

Writes targets_full.json: [{pid, name, district}] for every scheme we do not yet
have, plus targets_missing.json holding just what is still outstanding.
"""
import json
import sqlite3
from pathlib import Path

OUTPUT = Path("output")
data = json.loads((OUTPUT / "geo" / "schemes.json").read_text(encoding="utf-8"))
records = data["records"]

con = sqlite3.connect("state/gujrera.db")
scraped = {int(r[0]) for r in con.execute("select pid from projects")}
con.close()

have_dirs = set()
for folder in OUTPUT.rglob("*"):
    if folder.is_dir() and folder.name[:1].isdigit() and "_" in folder.name:
        try:
            have_dirs.add(int(folder.name.split("_", 1)[0]))
        except ValueError:
            pass

targets, missing, dupes = [], [], 0
seen = set()
for rec in records:
    try:
        pid = int(rec.get("pid") or 0)
    except (TypeError, ValueError):
        continue
    if not pid or pid in seen:
        dupes += 1
        continue
    seen.add(pid)
    entry = {
        "pid": pid,
        "name": (rec.get("name") or rec.get("projectName") or "").strip(),
        "district": (rec.get("district") or "").strip(),
    }
    targets.append(entry)
    if pid not in scraped and pid not in have_dirs:
        missing.append(entry)

targets.sort(key=lambda e: e["pid"])
missing.sort(key=lambda e: e["pid"])

(OUTPUT / "targets_full.json").write_text(json.dumps(targets, indent=2), encoding="utf-8")
(OUTPUT / "targets_missing.json").write_text(json.dumps(missing, indent=2), encoding="utf-8")

print(f"schemes in geo dataset : {len(records):,}  (dupe pids skipped: {dupes})")
print(f"unique pids            : {len(targets):,}")
print(f"already in state       : {len(scraped):,}")
print(f"folders on disk        : {len(have_dirs):,}")
print(f"\nstill to scrape        : {len(missing):,}")
by_district = {}
for entry in missing:
    key = entry["district"] or "?"
    by_district[key] = by_district.get(key, 0) + 1
print("\nmissing by district:")
for district, count in sorted(by_district.items(), key=lambda kv: -kv[1]):
    print(f"  {district:16} {count:>5,}")
print("\nwrote output/targets_full.json and output/targets_missing.json")

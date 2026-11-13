"""Measure, don't infer: re-fetch every suspicious file and compare to disk.

For each file that looks truncated/empty, ask the portal again and compare MD5:

    identical  -> the portal itself serves these bytes; nothing to repair
    different  -> the copy on disk is stale/damaged and CAN be replaced
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from gujrera.api import GujreraClient

OUTPUT = Path("output")


def looks_bad(path: Path) -> str | None:
    size = path.stat().st_size
    if size == 0:
        return "zero bytes"
    suf = path.suffix.lower()
    if suf in (".jpg", ".jpeg", ".jfif"):
        with path.open("rb") as fh:
            if fh.read(2) != b"\xff\xd8":
                return None  # wrong extension, content fine
            fh.seek(max(0, size - 4096))
            if b"\xff\xd9" not in fh.read():
                return "truncated jpeg"
        return None
    if suf == ".pdf":
        with path.open("rb") as fh:
            head = fh.read(1024)
            fh.seek(max(0, size - 4096))
            tail = fh.read()
        if b"%PDF-" not in head:
            return "not a pdf"
        if b"%%EOF" not in tail:
            return "truncated pdf"
    return None


targets = []
for p in OUTPUT.rglob("*"):
    if not p.is_file():
        continue
    rel = p.relative_to(OUTPUT).as_posix()
    if "raw" in rel.split("/")[:-1]:
        continue
    reason = looks_bad(p)
    if reason:
        targets.append((p, rel, reason))

print(f"suspicious files: {len(targets)}")
by_reason = {}
for _, _, r in targets:
    by_reason[r] = by_reason.get(r, 0) + 1
for r, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
    print(f"  {r:16} {n}")

client = GujreraClient()
identical = different = failed = no_uid = 0
recovered = []

for i, (path, rel, reason) in enumerate(targets, 1):
    project = path.parent.parent
    manifest = project / "documents_manifest.json"
    uid = None
    if manifest.exists():
        try:
            rows = json.loads(manifest.read_text(encoding="utf-8"))
            row = next((r for r in rows if r.get("file") == path.name), None)
            uid = row.get("uid") if row else None
        except ValueError:
            pass
    if not uid:
        no_uid += 1
        print(f"  [{i}/{len(targets)}] NO UID  {rel}")
        continue
    try:
        content, _, _ = client.get_document(uid)
    except Exception as exc:  # noqa: BLE001
        failed += 1
        print(f"  [{i}/{len(targets)}] FETCH FAILED  {rel}: {str(exc)[:60]}")
        continue

    disk = path.read_bytes()
    if hashlib.md5(content).hexdigest() == hashlib.md5(disk).hexdigest():
        identical += 1
        print(f"  [{i}/{len(targets)}] upstream-identical  {reason:15} {path.name[:52]}")
    else:
        different += 1
        recovered.append((path, rel, len(content), reason))
        print(f"  [{i}/{len(targets)}] DIFFERENT  disk={len(disk):,} portal={len(content):,}  {rel}")

print("\n--- result ---")
print(f"  identical to portal (unfixable upstream): {identical}")
print(f"  differ from portal (repairable)         : {different}")
print(f"  fetch failures                          : {failed}")
print(f"  no uid in manifest                      : {no_uid}")

if recovered:
    out = Path("cloudflare/repairable.json")
    out.write_text(json.dumps(
        [{"path": rel, "portal_bytes": n, "reason": r} for _, rel, n, r in recovered], indent=2),
        encoding="utf-8")
    print(f"\n  repairable list -> {out}")

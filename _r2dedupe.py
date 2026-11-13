"""Delete the redundant R2 copies, but only after proving each one is identical
to the district copy we are keeping.

Safety rule: an object is deleted only when a counterpart exists under
<prefix>/AHMEDABAD/<NAME>/... with the same ETag and size. Anything ambiguous is
left alone and reported.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, "cloudflare")
import r2_sync
from r2_sync import make_client

r2_sync.load_env(Path("cloudflare/.env"))
bucket = os.environ["R2_BUCKET"]
client = make_client()
apply = "--apply" in sys.argv

keys = [k.strip() for k in Path("cloudflare/duplicate_r2_keys.txt").read_text(encoding="utf-8").splitlines() if k.strip()]
print(f"candidates: {len(keys):,}")

safe, unsafe = [], []
for key in keys:
    suffix = "." + key.rsplit(".", 1)[-1] if "." in key.rsplit("/", 1)[-1] else ""
    prefix = r2_sync.prefix_for(suffix)
    if not key.startswith(prefix + "/"):
        unsafe.append((key, "unexpected key shape"))
        continue
    rest = key[len(prefix) + 1:]          # "<NAME>/files/deed.pdf"
    district_key = f"{prefix}/AHMEDABAD/{rest}"
    try:
        src = client.head_object(Bucket=bucket, Key=key)
        dst = client.head_object(Bucket=bucket, Key=district_key)
    except Exception:
        unsafe.append((key, "counterpart missing"))
        continue
    same_etag = (src.get("ETag") == dst.get("ETag"))
    same_size = (src.get("ContentLength") == dst.get("ContentLength"))
    if same_etag and same_size:
        safe.append(key)
    else:
        unsafe.append((key, f"differs (etag {same_etag}, size {same_size})"))

print(f"proven identical to district copy : {len(safe):,}")
print(f"left untouched                    : {len(unsafe):,}")
for key, why in unsafe[:10]:
    print(f"   {why}: {key}")

if not apply:
    print("\ndry run - nothing deleted (pass --apply)")
    sys.exit(0)

deleted = 0
for i in range(0, len(safe), 500):
    chunk = safe[i:i + 500]
    resp = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk]})
    deleted += len(resp.get("Deleted", []))
    errors = resp.get("Errors") or []
    if errors:
        print(f"  {len(errors)} error(s) in this batch: {errors[:2]}")
print(f"\ndeleted {deleted:,} duplicate object(s)")

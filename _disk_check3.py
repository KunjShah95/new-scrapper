import sqlite3
import time
from pathlib import Path

OUT = Path('output')
files = [p for p in OUT.rglob('files/*') if p.is_file() and not p.name.endswith('.part')]
total = sum(p.stat().st_size for p in files)
print(f'local document files: {len(files):,}  ({total/2**30:,.2f} GiB)')

c = sqlite3.connect('state/gujrera.db')
c.row_factory = sqlite3.Row
rows = {r['uid']: dict(r) for r in c.execute("select uid, pid, file_name, blob_key, status, sha256 from documents")}

known = unknown = 0
known_bytes = 0
for p in files:
    name = p.name
    folder = p.parent.parent
    hit = None
    for r in rows.values():
        if r.get('file_name') == name:
            hit = r
            break
    if hit and hit.get('blob_key'):
        known += 1
        known_bytes += p.stat().st_size
    else:
        unknown += 1
print(f'  rows with an R2 key (by filename match): {known:,} ({known_bytes/2**30:,.2f} GiB)')
print(f'  rows with no R2 key                    : {unknown:,}')
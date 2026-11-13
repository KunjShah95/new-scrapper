import sqlite3, time
from pathlib import Path

OUT = Path('output')
conn = sqlite3.connect('state/gujrera.db')
conn.row_factory = sqlite3.Row
index = {(int(r['pid']), r['file_name']): dict(r) for r in
         conn.execute("select pid, file_name, blob_key, status from documents")}

left = []
for p in OUT.rglob('files/*'):
    if not p.is_file() or p.name.endswith('.part'):
        continue
    pid = int(p.parent.parent.name.split('_', 1)[0]) if p.parent.parent.name.split('_', 1)[0].isdigit() else None
    row = index.get((pid, p.name)) if pid else None
    left.append((p, row, p.stat().st_size))

print(f'remaining local files: {len(left)} ({sum(x[2] for x in left)/2**20:,.1f} MiB)')
for p, row, size in sorted(left, key=lambda x: -x[2])[:45]:
    print(f'  {size/2**20:>8,.1f} MiB  status={row["status"] if row else "NO ROW"}  '
          f'key={"yes" if row and row.get("blob_key") else "no"}  {p.relative_to(OUT)}')
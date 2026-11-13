import os, time
from pathlib import Path

OUT = Path('output')
now = time.time()
cutoff = now - 6 * 3600  # last 6 hours

recent_files = []
recent_bytes = 0
total_bytes = 0
for p in OUT.rglob('*'):
    try:
        if not p.is_file():
            continue
        st = p.stat()
    except OSError:
        continue
    total_bytes += st.st_size
    if st.st_mtime >= cutoff:
        recent_files.append((st.st_mtime, st.st_size, p))
        recent_bytes += st.st_size

recent_files.sort(reverse=True)
print(f'output total on disk : {total_bytes/2**30:,.2f} GiB')
print(f'files modified <6h   : {len(recent_files):,}  ({recent_bytes/2**20:,.1f} MiB)')
for mtime, size, p in recent_files[:15]:
    print(f'  {size:>12,}  {time.strftime("%H:%M:%S", time.localtime(mtime))}  {p}')
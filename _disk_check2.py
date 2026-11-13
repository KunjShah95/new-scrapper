import time
from pathlib import Path

OUT = Path('output')
now = time.time()
cutoff = now - 6 * 3600
part = other = 0
part_bytes = other_bytes = 0
for p in OUT.rglob('files/*'):
    try:
        if not p.is_file():
            continue
        st = p.stat()
    except OSError:
        continue
    if st.st_mtime < cutoff:
        continue
    if p.name.endswith('.part'):
        part += 1
        part_bytes += st.st_size
    else:
        other += 1
        other_bytes += st.st_size
print(f'completed docs kept locally (<6h): {other:,}  ({other_bytes/2**20:,.1f} MiB)')
print(f'temp .part downloads in flight   : {part:,}  ({part_bytes/2**20:,.1f} MiB)')
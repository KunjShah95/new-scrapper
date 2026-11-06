# What "perfect" means here — the scraping standard

This is the acceptance criteria the archive is measured against. Every claim below
is backed by a script in this repo that can be re-run to prove it again.

---

## 1. Source of truth

Nothing is invented. Every value comes from the same public endpoints the
gujrera.gujarat.gov.in web app calls (`alldatabyprojectid/{pid}`,
`getproject-details/{pid}`, `getProjectLocations/{pid}`, `vdms/download/{uid}`).
No scraping of rendered HTML, no guessing, no third-party aggregator.

Documents are addressed by their **VDMS UID**, not by filename, so a renamed or
re-uploaded file is still recognisable.

---

## 2. Completeness — "we have everything the portal published"

Two independent gates:

### 2a. Project discovery

The daily crawler originally found projects by **searching 10 keywords**
(`residential`, `flats`, `apartment`, …). Any project whose name matched none of
them was invisible — it capped coverage at 29%.

Discovery is therefore done by **registration-number enumeration** instead
(`run.py --geo`), which walks the district prefix `PR/GJ/AHMEDABAD/…` rather than
guessing names. Result: **7,398 schemes** across Ahmedabad, Gandhinagar, Mehsana,
of which 2,156 are deep-scraped and 5,242 are queued.

> Gate: `build_targets.py` diffs the geo dataset against what is on disk. The
> backlog count must reach zero.

### 2b. Document discovery

For each project the harvester walks **every field of every payload** and collects
any value shaped like a VDMS UID. It is not limited to a known list of document
fields, so a tab GujRERA adds tomorrow is picked up without a code change.

> Gate: `check_completeness.py` — every UID referenced in the cached payloads must
> exist in a manifest. Current state: **85,398 referenced = 85,398 held, 0 gaps.**

---

## 3. Correctness — "the bytes are what the portal served"

| Check | Script | Current result |
|---|---|---|
| Every object in R2 is byte-identical to disk (ETag/MD5, full sweep) | `cloudflare/r2_verify.py --all` | **84,987 / 84,987 pass** |
| Every manifest row matches reality (file exists, size agrees) | `audit_output.py` | **85,398 rows, 0 mismatches** |
| Anomalies are the portal's fault, not ours (re-fetch + compare MD5) | `verify_upstream.py` | **137 / 137 identical upstream** |
| Format sanity (PDF `%PDF-`+`%%EOF`, JPEG SOI/EOI, PNG sig/IEND, JSON parses) | `audit_output.py` | see exceptions below |

Multipart uploads are verified too: a >64 MiB object's composite ETag is
recomputed locally from the same 16 MiB part size, so a truncated part is caught.

---

## 4. Storage rules

- **One object per file.** Keys mirror the folder tree, so a URL maps predictably
  onto a path: `main_pdfs/ircms/gujrera/<PROJECT>/files/<document>`.
- **No duplicates.** `dedupe_projects.py` proves a pid is stored once; R2 copies
  are only deleted after an ETag+size match against the copy being kept.
- **R2 is authoritative for documents.** The laptop keeps manifests and reports
  (~730 MB), never the 135 GB of PDFs.
- **`raw/` is never uploaded.** It is the audit cache, not deliverable data.

---

## 5. Freshness

A weekly cycle (`cloudflare/weekly_scrape.ps1`, or `weekly_scrape.sh` on a VPS)
does: refresh targets → scrape up to N projects direct to R2 → sync + prune →
full verify → regenerate the exception register. One row per cycle is appended to
`_work/logs/history.csv` so drift is visible over time.

---

## 6. Known exceptions (do not block delivery)

`output/BROKEN_DOCUMENTS.csv` lists every document that is not usable as
delivered. Each row carries `uid`, portal `url`, `r2_key`, issue, and whether a
re-fetch returns identical bytes.

| Issue | Count | Cause |
|---|---|---|
| declared image/jpeg, content is png | 210 | portal mislabels the content-type |
| truncated JPEG | 128 | portal serves truncated bytes (86 share an exact size) |
| declared image/png, content is jpeg | 25 | portal mislabels |
| empty file (0 bytes) | 5 | portal returns HTTP 200 with an empty body |
| truncated PDF | 4 | portal serves truncated bytes |
| other mislabels | 3 | portal mislabels |

These are **proven upstream**: re-fetching returns byte-identical content, so no
amount of re-scraping will fix them. They must be disclosed, not silently
retried.

---

## 7. What this standard does NOT guarantee

Being explicit, because "perfect" has limits:

1. **Completeness is bounded by what the API exposes.** If a document exists on a
   portal page but never appears in any JSON payload, no harvester can see it.
   The gate proves "everything referenced was fetched", not "nothing exists that
   we never saw".
2. **Format checks are heuristics, not full parsers.** A PDF can be internally
   damaged yet structurally plausible. Byte-identity to the portal is the strong
   guarantee; document validity beyond that is the portal's responsibility.
3. **Only three districts are covered** — Ahmedabad, Gandhinagar, Mehsana. The
   other 30 are absent by configuration, not by failure.
4. **The portal is flaky.** It intermittently returns HTTP 500 through the egress
   proxy, which silently reduces document yield. Runs should be measured on yield,
   not just on completion.

---

## 8. Pre-delivery checklist

```powershell
python build_targets.py            # backlog should trend to 0
python check_completeness.py       # must print COMPLETE
python audit_output.py             # manifests must match disk
python cloudflare/r2_verify.py --all   # must print VERIFIED
python make_broken_report.py --verify-upstream
```

Deliver only when 1–4 pass, and ship `BROKEN_DOCUMENTS.csv` alongside the data.

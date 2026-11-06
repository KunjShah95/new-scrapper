# GujRERA Project Scraper — 10 Ahmedabad Construction Sites

Collects the **complete public record** of a GujRERA-registered project, exactly as a
visitor sees it on `https://gujrera.gujarat.gov.in/#/project-preview`:

| Portal tab | What gets captured |
|---|---|
| **Project Summary** | Project details, unit-type/inventory, promoter snapshot, quarterly + annual compliance, collection bank (Sec. 4(2)(l)(D)), construction booking status, RERA applications/complaints/penalties, Form 2 cost sheet |
| **Project Profile** | Location & land details, development details, professionals (architect / engineer / CA / contractor / agent), blocks, Form 1 (architect certification incl. Form 1-A schedule and Form 1-B amenities), Form 3 inventory |
| **Promoters** | Promoter profile, developer-group track record, partners/associates, authorised signatory, previous projects, project bank accounts |
| **Progress & RERA Actions** | Progress score, quarterly progress, block-wise progress, QPR timeline, Form 5 annual compliance, applications/judgements, enquiry flags |
| **Documents** | Every attached file downloaded to `files/` (certificate, brochure, Form 5 PDFs, site photographs, layout plans, audited statements, …) |

Output is one Markdown file per project plus one combined report.

## Scheme map — every registered scheme in three districts

Separate from the per-project reports, `--geo` harvests **all** GujRERA-registered
schemes in Ahmedabad, Gandhinagar and Mehsana with their **exact location**: the
polygon the promoter plotted on the portal, reduced to a centroid (latitude /
longitude), bounding box and boundary area. `build_map.py` turns that into
`docs/map.html` — a searchable, filterable map where each pin opens the full
public detail set: registration number, promoter and contacts, project type,
portal status, declared cost, approval / possession / completion dates, land,
carpet and covered areas, approving authority, survey / TP / final-plot numbers,
village (moje), pin code, unit-price band.

```powershell
python run.py --geo                 # harvest locations + details -> output/geo/
python build_map.py                 # -> docs/map.html  (+ CSV / GeoJSON downloads)
```

Discovery is by registration-number head (`PR/GJ/AHMEDABAD` → 5,515 projects), then
widened by a state-wide keyword sweep and per-district area hints, so records whose
registration number does not follow the pattern are still caught. Every pid keeps
the sweep that found it (`discoverySource`) in the dataset.

Boundaries come from `maplocation/public/getProjectMapView`, which returns polygons
for 400 projects per POST — a whole district in seconds. Details come from
`alldatabyprojectid/{pid}` + `getproject-details/{pid}` + `getProjectLocations/{pid}`,
cached per project, and anything already scraped by `--scrape` is read from its local
`raw/` folder instead of the network. A re-run therefore costs nothing for unchanged
projects.

### What a run covers

The figures below are from `output/geo/summary.json` (regenerate any time):

| | Ahmedabad | Gandhinagar | Mehsana | Total |
|---|---|---|---|---|
| schemes found | 5,515 | 1,680 | 203 | **7,398** |
| latitude/longitude | 5,406 | 1,663 | 202 | **7,271** |
| plotted boundary | 5,302 | 1,646 | 198 | **7,146** |

The gap is the portal's, not the scraper's: 127 projects have no location record at all,
and every pin carries the portal's own coordinates. Two data-quality guards keep
obviously broken records from lying to the map — vertices whose latitude and longitude
come back swapped are corrected, a boundary wider than 9 km is discarded (its centre is
kept), and any scheme whose pin falls outside its district is flagged
(`coordinateWarning`, 40 schemes) with the reason shown in its detail panel.

`stage` on the map is derived from the declared possession and completion dates, not the
portal's status field, which is stale: only 22 of 7,398 projects are marked "Completed"
while 4,358 sit past their declared completion date. Both values are in the dataset
(`stage` and `portalStage`).

## Daily delta runs

`--scrape` and `--schedule` re-fetch everything because they have no memory. A
full sweep of the three districts is ~3,000 projects x 25 endpoints plus ~140,000
documents — not something to repeat every day.

`--daily` is the incremental path. It keeps a SQLite journal in `state/gujrera.db`
and asks three cheap questions before it downloads anything:

1. **Which projects exist?** A single discovery pass per district. A pid we have
   never stored is new; a pid that disappears has completed or been withdrawn
   (recorded, never deleted).
2. **Which of the ones we know have moved?** Six volatile endpoints — `core`,
   `applications`, `progress_score`, `quarters`, `annual_compliance`,
   `block_progress` — are fetched and canonically hashed. Matching hashes mean
   nothing moved and nothing else is fetched.
3. **Which documents are new?** GujRERA mints a **new VDMS UID on every upload**,
   so a UID we already hold is by definition unchanged. A known label with a new
   UID is a replaced document; the old version is kept and linked.

Only projects that fail those checks get a full scrape. On a re-scrape of an
already-captured project the cost is **zero document bytes** — every file is
reused from state.

> Why not hash the PDFs? Because the portal makes it impossible: `vdms/download/<UID>`
> returns no `ETag`, no `Last-Modified`, no `Content-Length`, and ignores `Range`
> (verified against the live endpoint). Change detection has to come from the JSON
> side, with sha256 on the blob as a backstop if GujRERA ever overwrites in place.

### What a run produces

The point of a daily run is not another copy of the data — it is the answer to
"what is different from yesterday":

```
output/changes/2026-11-05.md          # the human change report
output/changes/2026-11-05-run12.json  # every change, machine readable
```

New projects, field-level diffs (`progress_score.data.score: 62 → 68`), new and
replaced documents, and anything that vanished upstream.

### R2 layout

Bytes go to a Cloudflare R2 bucket through the S3 API. Documents are
**content-addressed**: the key is the sha256, so the same PAN card shared by six
projects is stored once, and a re-upload can never overwrite a version.

```
blobs/ab/cd/abcd…                    document bytes, immutable, deduped
projects/<pid>/raw.json.gz           all 25 endpoint payloads in one object
projects/<pid>/report.md
projects/<pid>/summary.json
projects/<pid>/documents_manifest.json
catalogue/projects.json              full snapshot of every project
catalogue/documents.jsonl.gz         every document: pid, uid, sha256, blob url
changes/<date>.md | .json | .jsonl.gz
runs/<run_id>.json
```

`catalogue/documents.jsonl.gz` is the join key into any other system: `pid` +
`uid` + `sha256` are stable forever, so downstream code can point at a blob
without ever re-deriving it.

The logical keys above are routed at the storage boundary: `blobs/…` goes under
`R2_PDF_PREFIX` and everything else under `R2_JSON_PREFIX`, both from
`cloudflare/.env`. That keeps this data beside the documents `cloudflare/r2_sync.py`
already uploaded under path-based keys (`…/<project>/files/<filename>.pdf`) without
either layout having to know about the other. Path-based keys collide whenever a
project holds two documents with the same filename — H7 alone has three different
`project_cert.pdf` files — which is precisely why new captures are content-addressed.

### Guards

Three things stop a long run from lying about itself:

**Run lock.** `state/run.lock` holds the holder's pid, host and start time. A
second `--daily` refuses to start rather than double-scraping and writing two
conflicting journals. A lock is only broken automatically when the holder is
provably gone: dead pid on this host, or another host and older than 24h.

**Partial progress.** Counters are written as each project completes, not once
at the end. A `oneshot` that is killed never reaches its own cleanup, and "0
scraped" for a run that actually captured 11 projects hides the only evidence
that it did anything. Interrupted runs are closed on the next start, which is
safe because the lock proves no run is live.

**Strict discovery.** A rate-limited search query returns a *smaller* project
list, and a smaller list is indistinguishable from "these projects no longer
exist". Any failed query fails the run instead of recording deletions.

```powershell
python run.py --daily --reverify             # the weekly run: every endpoint of every
                                             # project, oldest-scraped first
python run.py --daily                        # light probe run (6 of 25 endpoints)
python run.py --daily --dry-run              # discover + probe, scrape nothing
python run.py --daily --max-scrapes 5000     # bound the run; rest defers
python run.py --daily --backfill             # only projects never captured
python run.py --daily --project 29742        # re-scrape exactly one project
python run.py --runs                         # last 12 runs + live lock holder
python run.py --coverage                     # how much is captured, and how completely
python run.py --audit 29742                  # every fingerprint and document for one project
python run.py --import-legacy                # fold pre-delta scrapes from output/ into state
python run.py --daily --sink local           # write to r2_mirror/ instead of R2
```

### Importing what was scraped before the delta engine

`output/` already held ~2,500 project folders from the era when
`cloudflare/r2_sync.py` did the uploading and nothing tracked state. Run this
once (and again if more legacy folders turn up — it is idempotent):

```bash
python run.py --import-legacy                  # dry run with --dry-run
```

It downloads nothing and touches no network. It only converts what is on disk
into the rows the delta engine would have written: projects with their *real*
capture dates (from `project_bundle.json`, not "now"), 85,616 documents keyed
exactly as `r2_sync.py` keyed them (verified 40-for-40 against live objects
with sizes matching), and 54k+ endpoint fingerprints hashed exactly the way the
probe hashes. A legacy document cannot be deduplicated — the oldest scrapes
never computed a `sha256` — so it will be deduped the next time it is
downloaded for any other reason.

Two caveats learned the hard way:

* Run it while nothing else is scraping. An import racing a live scrape is
  harmless (it only reads), but the resulting rows can briefly overlap, and
  `state/run.lock` exists precisely to prevent two writers.
* Projects no longer in the weekly discovery list (completed/withdrawn) are
  imported as `active` — the next full run's discovery diff retires them to
  `completed` or `gone` on its own.

### Why the weekly run does a full pass

The 6-endpoint probe exists to make a *daily* cadence affordable. At weekly
cadence that trade stops paying: a full walk of all 25 endpoints for 2,156
projects is ~54,000 requests — about 40 minutes of politeness delay — and it
costs **zero document bytes**, because documents are reused by UID.

So the scheduled job is `--reverify`: one pass that discovers new projects,
re-reads every endpoint of every existing one, downloads only UIDs never seen
before, and writes the change journal. There is no separate "new records" job —
discovery **is** the new-records job.

```bash
python run.py --daily --reverify --max-scrapes 5000
```

Projects are walked **oldest-scraped first**, so if a pass is ever cut short by
`--max-scrapes` or a reboot, the next one picks up the tail rather than
re-doing the same head.

### What lives in R2, and where

| Kind | Key | Notes |
|---|---|---|
| Legacy documents (pre-delta) | `<R2_PDF_PREFIX>/<project>/files/<file>` | uploaded by `r2_sync.py` and the direct pipeline; imported into state by `--import-legacy` |
| Content-addressed documents | `<R2_PDF_PREFIX>/blobs/<sha[:2]>/<sha[2:4]>/<sha256>` | what new captures write; identical bytes deduped across projects |
| Per-project records | `<R2_JSON_PREFIX>/projects/<pid>/raw.json.gz`, `summary.json`, `report.md`, `documents_manifest.json` | rewritten whenever the project changes |
| Change journals | `<R2_JSON_PREFIX>/changes/<date>.jsonl.gz`, `<date>-run<N>.md`/`.json` | the "what moved today" record |
| Catalogues | `<R2_JSON_PREFIX>/catalogue/projects.json`, `documents.jsonl.gz` | rewritten every run; join key is `pid` + `uid` + `sha256` |
| State snapshots | `<R2_JSON_PREFIX>/state/<date>/run-<id>.db.gz` + `state/latest.db.gz`, `latest.json` | the SQLite system of record itself |

The state snapshot is the one that matters most: every row above is *derived*
from `state/gujrera.db`, and a dead VPS drive cannot be reconstructed. The
snapshot is a real SQLite file (`VACUUM INTO` against the live WAL, so it is
transactionally consistent even mid-write):

```bash
curl -fsSL "<R2_PUBLIC_BASE>/scraper/json/gujrera/state/latest.db.gz" -o s.gz
gunzip s.gz && sqlite3 s ".tables"
```

A run whose sink is local writes the same keys under `r2_mirror/`;
`--sync-mirror` republishes whatever it finds there through the R2 sink with
identical routing, so switching a box between sinks strands nothing:

```bash
python run.py --sync-mirror            # real (takes the run lock)
python run.py --sync-mirror --dry-run  # routes only, no network
```

Idempotent by key: a repeat run finds 282/282 already present and pushes 0.

### Cost, measured

The estate was measured where the bytes already live: **~160 GB across 85,881
documents** (the three 20-projects sample extrapolation of 390 GB assumed
every legacy project would re-download at full size; the imported manifests
say otherwise). So the steady-state bill is smaller than first estimated:

| Item | Cost |
|---|---|
| Storage (~160 GB) | ~$2.40 / month |
| Mirror/journal/snapshot uploads | pennies (small objects, few dozen per week) |
| Weekly new documents | effectively zero (UID reuse) |
| Egress | free |

That is why `--prune-uploaded` matters: R2 is the store, the VPS is staging.
Keeping 160 GB on a 40 GB VPS is not an option.

Credentials come from `.env` (see `deploy/env.example`): `R2_ACCOUNT_ID`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE`.
Without them the sink falls back to `r2_mirror/` with the same key layout, which
is how the delta engine was verified before any credentials existed.

### Deploying to a VPS

```bash
sudo APP_DIR=/opt/gujrera GIT_URL=<repo> ./deploy/install.sh
```

Installs a service user, a venv, and `gujrera-weekly.timer` — Monday `02:30` with
`Persistent=true`, so a run missed while the box was down happens after boot
instead of silently skipping a week. systemd also refuses to overlap two runs of
the same unit, which is the main way a job like this corrupts its own state.

The first crawl is a backfill of ~3,000 projects (~390 GB, so plan on several
nights, not one). Run it in slices rather than one heroic job:

```bash
sudo systemctl start gujrera-backfill          # one 250-project slice
sudo ENABLE_BACKFILL=1 ./deploy/install.sh     # …or a slice every 20 minutes
```

Each slice resumes exactly where the last one stopped, because progress lives in
`state/gujrera.db` and not in a process. Turn the backfill timer off once
`run.py --runs` stops deferring — after that the weekly pass keeps it current.

## How it works

The portal is an Angular SPA, so the scraper does not drive a browser. It calls the same
public JSON endpoints the page calls (discovered from `main-es2015.*.js` and the lazy
`ProjectViewMod` chunk). No login, cookie, CAPTCHA or rate-limit bypass is involved —
every request is one a normal browser makes. Requests are spaced by a small delay.

Documents are served from `vdms/download/<UID>`. Instead of hard-coding the ~200 document
fields GujRERA uses, the harvester walks every JSON payload and collects any value that
looks like a VDMS UID, so newly added document fields are picked up automatically.

## Install

```powershell
pip install -r requirements.txt
```

Python 3.10+ (tested on 3.13).

## Proxy (optional)

Runs go out on your own connection by default. To route them through an egress proxy,
put it in `.env` (gitignored — never commit it) or pass `--proxy`:

```ini
PROXY_HOST=gw.dataimpulse.com
PROXY_PORT=823
PROXY_USER=...
PROXY_PASS=...
PROXY_SCHEME=http          # or socks5 / socks5h (needs PySocks)
PROXY_USER_SUFFIX=__cr.in  # provider-side targeting; empty = rotate anywhere
```

```powershell
python run.py --proxy-test                                   # what egress am I using?
python run.py --geo --proxy http://user:pass@host:823        # one-off override
python run.py --geo --no-proxy                               # ignore .env entirely
```

`--proxy-test` reports the direct egress IP, the proxied egress IP, and whether the
portal answers on each route — check it after changing plans or credentials.

Two behaviours worth knowing:

- **Fallback.** If the proxy cannot tunnel to the portal, the client says why once and
  continues on a direct connection instead of failing every request. Set
  `GUJRERA_PROXY_FALLBACK=0` to make that fatal instead.
- **`*.gov.in` is often blocked by residential providers.** DataImpulse, for instance,
  returns `403 SITE_PERMANENTLY_BLOCKED` for `gujrera.gujarat.gov.in` (and every other
  government domain) while working fine elsewhere. That is the provider's blocklist, not
  a configuration problem — the portal is reachable directly from this machine, and the
  scraper is deliberately slow (0.2–0.35 s between requests), so the proxy is not needed
  for it. Ask the provider to unblock the domain if you specifically need it proxied.

## Usage

```powershell
python run.py --list-output                 # what is already downloaded
python run.py --discover                    # list ongoing Ahmedabad projects (ids for targets.json)
python run.py --scrape                      # scrape targets.json + download all documents
python run.py --scrape --no-files           # JSON + Markdown only (fast)
python run.py --project 29742               # one project by id
python run.py --project "PR/GJ/AHMEDABAD/..."   # ...or by registration number / name fragment
python run.py --report-only                 # rebuild every Markdown from saved bundles (no network)

python run.py --geo                         # every scheme's location + details -> output/geo
python run.py --geo --districts ahmedabad,gandhinagar,mehsana
python run.py --geo --refresh-geo           # ignore the geo cache and re-harvest
python run.py --geo --limit 50              # quick sample (first 50 projects)
python run.py --build-map                   # docs/map.html from output/geo/schemes.json
```

Useful flags: `--limit N` (first N targets), `--targets file.json`, `--geo-workers N`
(concurrent detail requests, default 12), `-v` (debug log).

> On Windows set `$env:PYTHONIOENCODING="utf-8"` before running, otherwise the console
> logger can raise `UnicodeEncodeError` on the ₹ symbol. Report files themselves are
> always written as UTF-8.

## Choosing the sites

`targets.json` holds the 10 Ahmedabad (Ahmedabad City taluka) projects being tracked. It
is a plain list — edit it freely:

```json
[{ "pid": 29742, "name": "SINDUR VIENNA" }, ...]
```

A target may also be given as `regNo` or `name` only; it is then resolved through the
public search endpoint. To pick new ones, run `python run.py --discover` and copy ids.

## Output layout

```
output/
├── AHMEDABAD_10_PROJECTS_REPORT.md      # all 10 projects in one document
├── index.json                           # machine-readable summary of every project
├── geo/                                 # district-wide scheme locations (--geo)
│   ├── schemes.json                     # the dataset the map is built from
│   ├── schemes.csv                      # one row per scheme, scalars only
│   ├── schemes.geojson                  # centroids, all properties
│   ├── boundaries.geojson               # the plotted polygons
│   ├── summary.json                     # counts + coverage, per district
│   ├── ahmedabad.json                   # per-district slice
│   ├── gandhinagar.json
│   ├── mehsana.json
│   └── cache/                           # discovery rows, polygons, per-project details
└── 29742_SINDUR_VIENNA/
    ├── SINDUR_VIENNA.md                 # the full report for this project
    ├── documents_manifest.json          # every document: label, UID, file, size, status
    ├── project_bundle.json              # scrape metadata (params, errors, summary row)
    ├── files/                           # downloaded documents
    └── raw/                             # untouched JSON response per endpoint
```

`raw/` keeps the API responses verbatim, so a report can always be regenerated with
`--report-only` and audited against the source.

## Publish to Cloudflare

`output/` is far too big for static hosting (142,635 files, 138 GB), so it is published in two parts:
the **site** goes to Cloudflare Pages and the **document archive** goes to Cloudflare R2.

```powershell
pip install -r cloudflare/requirements-cloudflare.txt   # boto3
Copy-Item cloudflare/.env.example cloudflare/.env       # then fill in the R2 keys

wrangler r2 bucket create gujrera-output
python cloudflare/r2_sync.py --dry-run                  # what would be uploaded
python cloudflare/r2_sync.py --workers 16               # resumable, Ctrl-C safe

cd cloudflare/worker; wrangler deploy; cd ../..         # document browser over R2
.\cloudflare\deploy_pages.ps1                           # site -> Pages
```

> **Deploying the Worker needs the account that owns the bucket.** Wrangler's
> OAuth login and the S3 keys in `cloudflare/.env` are independent credentials,
> and they can be on different accounts. If `wrangler deploy` fails with
> `R2 bucket 'lrms-legal-pdfs' not found` or `code 10042` (R2 not enabled),
> `wrangler login` with the bucket's account — or set
> `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` scoped to it. Then point
> `R2_PUBLIC_BASE` at the deployed URL (`https://gujrera-docs.<subdomain>.workers.dev`,
> or the custom domain once one is attached); the catalogue fills every
> document's `"url"` from it, so until it is set those URLs stay `null`.

The upload is the long part (~135 GB); it records every completed object and resumes where it
stopped. See `cloudflare/README.md` for the full guide, costs and troubleshooting.

## Module map

| File | Responsibility |
|---|---|
| `run.py` | CLI entry point |
| `gujrera/api.py` | Base URL, endpoint table, HTTP client, response unwrapping |
| `gujrera/pipeline.py` | Per-project orchestration, discovery, summary row, master report |
| `gujrera/geo.py` | District-wide scheme discovery, polygons, lat/lng + detail harvest |
| `gujrera/report.py` | All Markdown builders and formatting helpers |
| `gujrera/documents.py` | UID harvesting, document labels, downloads, manifest |
| `gujrera/state.py` | SQLite state: known projects, endpoint hashes, document history, run journal |
| `gujrera/delta.py` | The daily incremental engine: discovery diff, probe, change report |
| `gujrera/sink.py` | Storage sinks — local mirror and Cloudflare R2 (S3 API) |
| `deploy/` | systemd units and the VPS installer |
| `build_map.py` | `docs/map.html` — the interactive scheme map |
| `build_html.py` | Static report site in `docs/` (landing page + project pages) |
| `cloudflare/` | Pages + R2 deployment: `r2_sync.py`, the document Worker, deploy scripts |
| `targets.json` | The 10 selected projects |

## Notes

- Numbers such as costs are rendered in Indian digit grouping (`₹ 12,00,00,000`) and
  dates as `DD-MM-YYYY`, matching what the portal displays.
- Where the portal publishes nothing for a field, the report shows `—` rather than
  guessing.
- Tables are curated to the readable fields; the complete raw payload is always in
  `raw/` if you need an unfiltered column.
- This is public regulatory data. Keep the request delay in `api.py` if you extend the
  scraper, and check GujRERA's terms before any large-scale harvesting.

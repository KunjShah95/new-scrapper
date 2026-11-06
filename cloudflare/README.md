# Cloudflare deployment

Two destinations, because `output/` cannot be static-hosted: **142,534 files / 138 GB** against
Pages' limit of **20,000 files / 25 MiB per file**.

| | Content | Cloudflare product |
|---|---|---|
| **Site** | `docs/` — 13 HTML pages, scheme map, bundled PDFs | **Pages** → <https://gujrera-projects.pages.dev> |
| **Archive** | every scraped document and record | **R2** → bucket `lrms-legal-pdfs` |

---

## R2 layout

Keys are routed by file type into two prefixes, so documents and records stay separable:

| Source (`output/…`) | R2 key |
|---|---|
| `…/files/Deed.pdf` | `main_pdfs/ircms/gujrera/…/files/Deed.pdf` |
| `…/files/photo.jpg` | `main_pdfs/ircms/gujrera/…/files/photo.jpg` |
| `…/project_bundle.json` | `scraper/json/gujrera/…/project_bundle.json` |
| `…/PROJECT.md` | `scraper/json/gujrera/…/PROJECT.md` |
| `geo/schemes.geojson` | `scraper/json/gujrera/geo/schemes.geojson` |

Binary documents (PDF, images, zip) → `R2_PDF_PREFIX`. Everything textual (JSON, Markdown, CSV,
GeoJSON) → `R2_JSON_PREFIX`. The `gujrera` segment keeps this scraper distinct from anything else
already living under `main_pdfs/ircms` or `scraper/json`.

**`raw/` folders are never uploaded.** They hold verbatim API responses kept only for local
auditing — 57,879 files / 423 MB that no consumer needs. `--include-raw` overrides this.

Current totals:

```
main_pdfs/ircms/gujrera   70,734 files   134.6 GiB
scraper/json/gujrera      13,921 files     287.1 MiB
                          --------
                          84,655 files   134.9 GiB      (57,879 raw/ files excluded)
```

## 1. Dependencies

```powershell
pip install -r requirements.txt
pip install -r cloudflare/requirements-cloudflare.txt   # boto3
npm install -g wrangler
```

## 2. Credentials

`cloudflare/.env` holds the account credentials and is **git-ignored**. Copy the template:

```powershell
Copy-Item cloudflare/.env.example cloudflare/.env
```

Both code paths read it: `r2_sync.py` reads it directly, and `gujrera/sink.py` loads it
automatically when the variables are not already in the environment.

| Variable | Meaning |
|---|---|
| `R2_ENDPOINT` | S3 endpoint for the bucket (`https://<account>.r2.cloudflarestorage.com`) |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 API token, *Object Read & Write* on the bucket only |
| `R2_BUCKET` | `lrms-legal-pdfs` |
| `R2_PDF_PREFIX` | `main_pdfs/ircms/gujrera` |
| `R2_JSON_PREFIX` | `scraper/json/gujrera` |

## 3. Upload what has already been scraped

```powershell
python cloudflare/r2_sync.py --dry-run     # shows the exact destinations and totals
python cloudflare/r2_sync.py --workers 16  # ~135 GB, resumable, Ctrl-C safe
```

Every completed object is recorded in `cloudflare/r2_state.json`, so an interrupted run resumes and
unchanged files are skipped by size + mtime.

```powershell
python cloudflare/r2_sync.py --only 29742_SINDUR_VIENNA   # one project
python cloudflare/r2_sync.py --verify                     # bucket vs disk, three-way diff
```

## 4. Future runs go straight to R2

`run.py --daily` writes through `gujrera/sink.py`. `make_sink("auto")` picks the R2 sink as soon as
`R2_BUCKET` plus an endpoint are present, which `cloudflare/.env` supplies:

```powershell
python run.py --daily                 # discovers, scrapes changes, uploads straight to R2
python run.py --daily --sink local    # force a local mirror instead
```

Internal keys (`blobs/…`, `projects/…`, `catalogue/…`) are unchanged — the prefixes are applied at
the storage boundary, so nothing downstream has to know about them. Blobs land under the PDF
prefix, records under the JSON prefix.

`--prune-uploaded` deletes local document files once they are safely in R2.

## 5. Document browser (optional)

```powershell
cd cloudflare/worker
wrangler deploy
```

Streams objects out of R2 with Range and `ETag`/`If-None-Match` support, browsable listings,
`?download=1`, `?list&format=json`.

> The bucket lives on account `8c6e95b1…`, while the Pages project is on `fe4ca2a8…`. A Worker can
> only bind a bucket in its own account, so deploy the Worker against the account that owns
> `lrms-legal-pdfs` (`wrangler login`, or `CLOUDFLARE_API_TOKEN` scoped to that account).

## 6. Site

```powershell
.\cloudflare\deploy_pages.ps1
```

Stages `docs/` into `_deploy/pages`, drops files over 25 MiB (recorded in `_deploy/excluded.json`),
adds a `404.html` so missing files return a real 404 instead of the homepage with status 200, then
deploys. `docs/` is never modified.

## Cost

| Item | Monthly |
|---|---|
| R2 storage 135 GB @ $0.015/GB | ~$2.05 |
| Class A writes — 85k on first run, 1M free/mo | $0 |
| Class B reads — 10M free/mo | $0 |
| Egress | $0 |
| Pages (free plan) | $0 |

## Troubleshooting

**`SignatureDoesNotMatch`** — the secret access key is wrong. R2 secrets are exactly 64 hex
characters; a secret of any other length has been mangled in transit.

**`error code 10042` on `wrangler r2`** — R2 is not enabled on that account. Enable it in the
dashboard (R2 → accept terms → payment method). Separate from having an API token.

**`NoSuchBucket` / 403 on upload** — the API token is scoped to a different bucket than `R2_BUCKET`.

**Pages: `file too large`** — a file over 25 MiB reached staging; re-run `deploy_pages.ps1`.

**Uploads stall** — lower `--workers` to 8. Re-running never repeats completed objects.

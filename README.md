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

## Usage

```powershell
python run.py --list-output                 # what is already downloaded
python run.py --discover                    # list ongoing Ahmedabad projects (ids for targets.json)
python run.py --scrape                      # scrape targets.json + download all documents
python run.py --scrape --no-files           # JSON + Markdown only (fast)
python run.py --project 29742               # one project by id
python run.py --project "PR/GJ/AHMEDABAD/..."   # ...or by registration number / name fragment
python run.py --report-only                 # rebuild every Markdown from saved bundles (no network)
```

Useful flags: `--limit N` (first N targets), `--targets file.json`, `-v` (debug log).

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
└── 29742_SINDUR_VIENNA/
    ├── SINDUR_VIENNA.md                 # the full report for this project
    ├── documents_manifest.json          # every document: label, UID, file, size, status
    ├── project_bundle.json              # scrape metadata (params, errors, summary row)
    ├── files/                           # downloaded documents
    └── raw/                             # untouched JSON response per endpoint
```

`raw/` keeps the API responses verbatim, so a report can always be regenerated with
`--report-only` and audited against the source.

## Module map

| File | Responsibility |
|---|---|
| `run.py` | CLI entry point |
| `gujrera/api.py` | Base URL, endpoint table, HTTP client, response unwrapping |
| `gujrera/pipeline.py` | Per-project orchestration, discovery, summary row, master report |
| `gujrera/report.py` | All Markdown builders and formatting helpers |
| `gujrera/documents.py` | UID harvesting, document labels, downloads, manifest |
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

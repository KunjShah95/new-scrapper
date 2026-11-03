#!/usr/bin/env python3
"""Convert the GujRERA scraper output (Markdown + JSON + PDFs) into client-ready static HTML.

Output folder: <repo>/docs  <- ready for GitHub Pages (set Pages source to '/docs').
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import markdown
from jinja2 import Template

REPO = Path(__file__).resolve().parent
OUTPUT = REPO / "output"
SITE = REPO / "docs"
INDEX = OUTPUT / "index.json"
MASTER_MD = OUTPUT / "AHMEDABAD_10_PROJECTS_REPORT.md"
ZIP_SRC = OUTPUT / "18991_MARUTI_360.zip"

GENERATED_AT = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M")


def inr(value):
    """Indian digit grouping, e.g. 420630000 -> 42,06,30,000."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number == 0:
        return "0"
    negative = number < 0
    number = abs(number)
    whole = f"{int(round(number)):,}"
    if len(whole.replace(",", "")) > 3:
        parts = whole.split(",")
        tail = parts[-1]
        grouped = []
        rest = ",".join(parts[:-1]).replace(",", "")
        while len(rest) > 2:
            grouped.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            grouped.insert(0, rest)
        whole = ",".join(grouped + [tail])
    return ("-" if negative else "") + whole


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} TB"


def clean(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, dict, tuple, set)):
        if not value:
            return "—"
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= 160 else text[:157] + "…"
    text = str(value).strip()
    if text in ("", "null", "NA", "NIL", "-", "--", "null,null"):
        return "—"
    return text


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_html(text):
    return markdown.markdown(text, extensions=["tables", "fenced_code", "nl2br"], output_format="html")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_index():
    return load_json(INDEX)


def header_fields(md_text):
    """Parse the '**Key:** value' header block of a project .md report."""
    fields = {}
    for line in md_text.splitlines()[:14]:
        match = re.match(r"^\*\*(.+?):\*\*\s*(.*)$", line.strip())
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


def build_project_data(folder):
    index_entry = next((e for e in load_index() if e["folder"] == folder.name), None)
    summary = index_entry["summary"] if index_entry else {}
    project_dir = folder
    md_path = project_dir / index_entry["markdown"] if index_entry else project_dir / "report.md"
    manifest_path = project_dir / "documents_manifest.json"
    bundle_path = project_dir / "project_bundle.json"

    md_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    header = header_fields(md_text)

    manifest = load_json(manifest_path) if manifest_path.exists() else []
    bundle = load_json(bundle_path) if bundle_path.exists() else {}
    raw_files = sorted((project_dir / "raw").glob("*.json")) if (project_dir / "raw").exists() else []

    documents = []
    for item in manifest:
        documents.append({
            "label": item.get("label"),
            "file": item.get("file"),
            "status": item.get("status"),
            "size": item.get("bytes"),
            "portal_url": item.get("url"),
        })

    progress_text = header.get("Construction Progress (as published)") or f"{round(float(summary.get('progress') or 0))}%"

    return {
        "folder": folder.name,
        "name": summary.get("name") or folder.name,
        "reg_no": header.get("GujRERA Registration No.") or summary.get("regNo") or "—",
        "project_id": re.search(r"Project ID:\s*(\d+)", md_text).group(1) if re.search(r"Project ID:\s*(\d+)", md_text) else folder.name.split("_")[0],
        "project_type": header.get("Project Type") or summary.get("type") or "—",
        "taluka": header.get("District / Taluka") or summary.get("taluka") or "—",
        "promoter": header.get("Promoter") or "—",
        "registered_on": header.get("Registered On") or "—",
        "declared_completion": header.get("Declared Completion") or "—",
        "progress_raw": progress_text,
        "progress": f"{round(float(summary.get('progress') or 0))}%",
        "cost": summary.get("cost"),
        "units": summary.get("units"),
        "booked": summary.get("booked"),
        "unbooked": summary.get("unbooked"),
        "docs_ok": summary.get("docs_ok"),
        "md_text": md_text,
        "md_body": md_to_html(md_text),
        "documents": sorted(documents, key=lambda d: (d["status"] != "ok", str(d["file"]))),
        "raw_files": raw_files,
        "raw_count": len(raw_files),
        "source_url": bundle.get("source_url") or "https://gujrera.gujarat.gov.in/#/project-preview",
        "scraped_at": bundle.get("scraped_at") or GENERATED_AT,
        "manifest_count": len(manifest),
    }
TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ name }} — GujRERA Project Report</title>
<style>
  :root {
    --bg: #f6f5f2; --surface: #ffffff; --ink: #1c1b19; --muted: #6b6862;
    --line: #e4e2dd; --brand: #1f4e5f; --brand-dark: #153747;
    --accent: #b3592a; --ok: #2e7d32; --warn: #b26a00; --err: #b3261e;
    --radius: 10px; --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; line-height: 1.6; -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 0 20px; }
  .topbar { background: var(--brand-dark); color: #fff; padding: 14px 0; }
  .topbar .wrap { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
  .brand { color: #fff; font-weight: 700; font-size: 15px; text-decoration: none; letter-spacing: .2px; }
  .kicker { font-size: 12px; opacity: .75; }
  .breadcrumb { font-size: 13px; color: var(--muted); margin: 18px 0 8px; }
  .breadcrumb a { color: var(--brand); text-decoration: none; }
  .hero { padding: 28px 0 10px; }
  .hero h1 { margin: 0 0 6px; font-size: 30px; letter-spacing: -.3px; }
  .hero .lead { margin: 0; color: var(--muted); }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 16px 0 22px; }
  .fact { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 14px; box-shadow: var(--shadow); }
  .fact .label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); font-weight: 600; }
  .fact b { display: block; font-size: 16px; margin-top: 2px; word-break: break-word; }
</style>
</head>
<body>
  <header class="topbar">
    <div class="wrap">
      <a class="brand" href="../index.html">GujRERA Ahmedabad — Project Reports</a>
      <span class="kicker">Generated {{ generated_at }}</span>
    </div>
  </header>

  <main class="wrap">
    <nav class="breadcrumb"><a href="../index.html">All projects</a> / {{ name }}</nav>

    <section class="hero">
      <h1>{{ name }}</h1>
      <p class="lead">{{ project_type }} · {{ taluka }}</p>
    </section>

    <section class="facts">
      <div class="fact"><span class="label">Project ID</span><b>{{ project_id }}</b></div>
      <div class="fact"><span class="label">Reg. No.</span><b>{{ reg_no }}</b></div>
      <div class="fact"><span class="label">Project Type</span><b>{{ project_type }}</b></div>
      <div class="fact"><span class="label">Taluka</span><b>{{ taluka }}</b></div>
      <div class="fact"><span class="label">Promoter</span><b>{{ promoter }}</b></div>
      <div class="fact"><span class="label">Registered On</span><b>{{ registered_on }}</b></div>
      <div class="fact"><span class="label">Declared Completion</span><b>{{ declared_completion }}</b></div>
      <div class="fact"><span class="label">Construction Progress</span><b>{{ progress_raw }}</b></div>
      <div class="fact"><span class="label">Total Cost</span><b>₹ {{ cost_str(cost) }}</b></div>
      <div class="fact"><span class="label">Total Units</span><b>{{ units }}</b></div>
      <div class="fact"><span class="label">Booked / Un-booked</span><b>{{ booked }} / {{ unbooked }}</b></div>
      <div class="fact"><span class="label">Documents available</span><b>{{ docs_ok }}</b></div>
    </section>

    <section class="block">
      <h2>1. Project Summary</h2>
      {{ md_body }}
    </section>

    <section class="block">
      <h2>2. Documents ({{ manifest_count }})</h2>
      <table>
        <thead><tr><th>#</th><th>Label</th><th>File</th><th class="c">Status</th><th class="c">Size</th><th>Source</th></tr></thead>
        <tbody>
        {% for d in documents %}
          <tr>
            <td class="c">{{ loop.index }}</td>
            <td>{{ d.label | e }}</td>
            <td><code>{{ d.file | e }}</code></td>
            <td class="c"><span class="tag {% if d.status == 'ok' %}ok{% elif d.status == 'fail' %}err{% else %}warn{% endif %}">{{ d.status | e }}</span></td>
            <td class="c">{{ size(d.size) }}</td>
            <td>
              <a class="btn ghost" href="{{ d.portal_url | e }}" target="_blank" rel="noopener">Open on portal</a>
              {% if d.file and d.file in local_map %}<a class="btn ghost" href="../{{ folder }}/files/{{ local_map[d.file] | e }}" download>Download</a>{% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      <div class="actions"><a class="btn secondary" href="../index.html">← Back to all projects</a></div>
    </section>

    {% if raw_count %}
    <section class="block">
      <h2>3. Raw API responses ({{ raw_count }})</h2>
      {% for rf in raw_files %}
      <details class="raw">
        <summary>{{ rf.name }}</summary>
        <div class="inner"><pre><code>{{ rf.read_text(encoding="utf-8") | e }}</code></pre></div>
      </details>
      {% endfor %}
    </section>
    {% endif %}

    <footer>
      <div class="wrap">
        <div>
          Scraped from <a href="{{ source_url }}">{{ source_url }}</a> on {{ scraped_at }}.
          Report generated {{ generated_at }} — data reflects the public GujRERA portal as of the scrape date.
        </div>
        <div>
          <a class="btn ghost" href="../report.html">View master report (all 10 projects)</a>
        </div>
      </div>
    </footer>
  </main>
</body>
</html>
"""

LANDING = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GujRERA Ahmedabad Project Reports — Ahmedabad City Projects</title>
<style>
  :root {
    --bg: #f6f5f2; --surface: #ffffff; --ink: #1c1b19; --muted: #6b6862;
    --line: #e4e2dd; --brand: #1f4e5f; --brand-dark: #153747;
    --accent: #b3592a; --ok: #2e7d32; --warn: #b26a00; --err: #b3261e;
    --radius: 10px; --shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; line-height: 1.6; }
  .wrap { max-width: 1160px; margin: 0 auto; padding: 0 20px; }
  .hero { padding: 40px 0 26px; }
  .hero h1 { margin: 0 0 8px; font-size: 32px; letter-spacing: -.4px; }
  .hero .sub { color: var(--muted); margin: 0 0 14px; }
  .hero .meta { font-size: 13px; color: var(--muted); }
  .search { display: flex; gap: 10px; max-width: 640px; margin: 14px auto 0; }
  .search input { flex: 1; padding: 12px 16px; border-radius: 8px; border: 1px solid var(--line); font-size: 15px; background: #fff; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 18px; padding: 8px 0 20px; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px 20px; box-shadow: var(--shadow); display: block; text-decoration: none; color: var(--ink); transition: box-shadow .15s, transform .05s; }
  .card:hover { box-shadow: 0 6px 16px rgba(0,0,0,.10); transform: translateY(-2px); }
  .card .name { font-size: 19px; font-weight: 700; }
  .card .type { font-size: 13px; color: var(--muted); margin: 2px 0; }
  .card .row { display: flex; justify-content: space-between; padding: 5px 0; border-top: 1px solid var(--line); font-size: 14px; }
  .card .row:first-of-type { border-top: none; }
  .card .row .num { font-weight: 600; text-align: right; }
  .card .linkrow { display: flex; gap: 10px; margin-top: 14px; }
  .card .linkrow a { color: var(--brand); font-size: 14px; font-weight: 600; }
  .footer { margin-top: 26px; padding: 20px 0 30px; color: var(--muted); font-size: 13px; border-top: 1px solid var(--line); }
  .footer .wrap { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
  .host-note { background: #eef3f5; border-radius: 8px; padding: 12px 16px; margin-top: 12px; }
  .host-note b { display: block; color: var(--brand-dark); }
  @media (max-width: 640px) { .hero h1 { font-size: 24px; } }
</style>
</head>
<body>
  <header class="topbar" style="background:#153747;">
    <div class="wrap">
      <a class="brand" href="index.html">GujRERA Ahmedabad — Project Reports</a>
      <span class="kicker">Static HTML · generated {{ generated_at }}</span>
    </div>
  </header>

  <main class="wrap">
    <section class="hero">
      <h1>GujRERA — Ahmedabad City Project Reports</h1>
      <p class="sub">Client-ready overview of every project scraped from the Gujarat RERA portal ({{ count }} projects).</p>
      <p class="meta">Source: <a href="https://gujrera.gujarat.gov.in/#/project-preview">gujrera.gujarat.gov.in</a> · Static site — no server required · Generated {{ generated_at }}</p>
    </section>

    <div class="search">
      <input id="search" type="search" placeholder="Search by project name or type…">
    </div>

    <div class="cards" id="cards">

      {% for p in projects %}
      <a class="card" href="{{ p.folder }}/index.html" data-name="{{ p.name | e }}" data-type="{{ p.type | e }}">
        <div class="name">{{ p.name }}</div>
        <div class="type">{{ p.type }} · {{ p.taluka }}</div>
        <div class="row"><span>Construction progress</span><span class="num">{{ p.progress }}</span></div>
        <div class="row"><span>Total cost</span><span class="num">₹ {{ cost_str(p.cost) }}</span></div>
        <div class="row"><span>Units / Booked / Un-booked</span><span class="num">{{ p.units }} / {{ p.booked }} / {{ p.unbooked }}</span></div>
        <div class="row"><span>Documents ok</span><span class="num">{{ p.docs_ok }}</span></div>
        <div class="linkrow">
          <a href="{{ p.folder }}/index.html">Report →</a>
          <a href="https://gujrera.gujarat.gov.in/#/project-preview" target="_blank" rel="noopener">Portal ↗</a>
        </div>
      </a>
      {% endfor %}
    </div>

    <div class="footer">
      <div class="wrap">
        <div>
          Master report (all 10 projects in one document):
          <a href="report.html">report.html</a>
        </div>
        <div class="host-note">
          <b>To host on GitHub Pages:</b> push this repo to GitHub and enable Pages → publish from the <code>/docs</code> folder (main branch).
        </div>
      </div>
    </div>
  </main>

  <script>
    (function () {
      var input = document.getElementById("search");
      var cards = document.querySelectorAll(".cards .card");
      input.addEventListener("input", function () {
        var q = input.value.toLowerCase();
        cards.forEach(function (c) {
          var name = c.dataset.name.toLowerCase();
          var type = c.dataset.type.toLowerCase();
          c.style.display = (!q || name.indexOf(q) > -1 || type.indexOf(q) > -1) ? "" : "none";
        });
      });
    })();
  </script>
</body>
</html>
"""

def render_page(data, local_map=None):
    context = {
        "name": data["name"],
        "project_type": data["project_type"],
        "taluka": data["taluka"],
        "project_id": data["project_id"],
        "reg_no": data["reg_no"],
        "promoter": data["promoter"],
        "registered_on": data["registered_on"],
        "declared_completion": data["declared_completion"],
        "progress_raw": data["progress_raw"],
        "progress": data["progress"],
        "cost": data["cost"],
        "units": data["units"],
        "booked": data["booked"],
        "unbooked": data["unbooked"],
        "docs_ok": data["docs_ok"],
        "documents": data["documents"],
        "local_map": local_map,
        "folder": data["folder"],
        "manifest_count": data["manifest_count"],
        "raw_count": data["raw_count"],
        "raw_files": data["raw_files"],
        "md_body": data["md_body"],
        "source_url": data["source_url"],
        "scraped_at": data["scraped_at"],
        "generated_at": GENERATED_AT,
        "size": fmt_bytes,
        "cost_str": cost_str,
    }
    return Template(TEMPLATE).render(context)


def render_landing(projects):
    cards = []
    for p in projects:
        cards.append({**p, "cost": p["cost"]})
    return Template(LANDING).render(count=len(projects), projects=cards, generated_at=GENERATED_AT, cost_str=cost_str)


def cost_str(value):
    return "₹ " + inr(value)


def build_cards(projects):
    cards = []
    for data in projects:
        cards.append({
            "folder": data["folder"],
            "name": data["name"],
            "type": data["project_type"],
            "taluka": data["taluka"],
            "progress": data["progress"],
            "cost": data["cost"],
            "units": data["units"],
            "booked": data["booked"],
            "unbooked": data["unbooked"],
            "docs_ok": data["docs_ok"],
        })
    return cards

def run():
    if not INDEX.exists():
        print(f"[error] {INDEX} not found — run the scraper first.", file=sys.stderr)
        return 1

    SITE.mkdir(parents=True, exist_ok=True)

    index = load_index()
    projects = []
    for entry in index:
        folder = OUTPUT / entry["folder"]
        if not folder.exists():
            print(f"[warn] folder {folder} missing for {entry['folder']}; skipping", file=sys.stderr)
            continue
        projects.append(build_project_data(folder))

    # --- per-project pages ---------------------------------------------------
    for data in projects:
        proj_dir = SITE / data["folder"]
        (proj_dir / "files").mkdir(parents=True, exist_ok=True)

        src_files = (OUTPUT / data["folder"] / "files").exists()
        local_map = {}
        if src_files:
            for pdf in (OUTPUT / data["folder"] / "files").glob("*.pdf"):
                name = pdf.name
                stat = local_map.setdefault(name, {"n": 0})
                stat["n"] += 1
                if stat["n"] == 1:
                    dest = name
                else:
                    stem = name[: name.rfind(".pdf")]
                    dest = f"{stem} ({stat['n']}).pdf"
                shutil.copy2(pdf, proj_dir / "files" / dest)

        page = render_page(data, local_map=local_map)
        (proj_dir / "index.html").write_text(page, encoding="utf-8")
        print(f"[ok] {data['folder']}/index.html  ({len(data['documents'])} documents)")

    # --- landing page ----------------------------------------------------------
    landing = render_landing(build_cards(projects))
    (SITE / "index.html").write_text(landing, encoding="utf-8")
    print(f"[ok] docs/index.html  ({len(projects)} projects)")

    # --- master report -----------------------------------------------------------
    if MASTER_MD.exists():
        master_html = md_to_html(MASTER_MD.read_text(encoding="utf-8"))
        report_page = Template(TEMPLATE).render(
            name="Ahmedabad — All 10 Projects",
            generated_at=GENERATED_AT,
            md_body=master_html,
            source_url="https://gujrera.gujarat.gov.in/#/project-preview",
            scraped_at=GENERATED_AT,
            cost_str=cost_str,
            cost=0,
        )
        (SITE / "report.html").write_text(report_page, encoding="utf-8")
        print(f"[ok] docs/report.html  (master report)")

    # --- archive zip ---------------------------------------------------------------
    if ZIP_SRC.exists():
        dest = SITE / "downloads"
        dest.mkdir(exist_ok=True)
        shutil.copy2(ZIP_SRC, dest / ZIP_SRC.name)
        print(f"[ok] docs/downloads/{ZIP_SRC.name}")

    # --- .nojekyll so GitHub Pages serves files without Jekyll preprocessing ----------
    (SITE / ".nojekyll").write_text("", encoding="utf-8")

    print(f"\n[done] {len(projects)} project pages + landing written to {SITE}")
    return 0


if __name__ == "__main__":
    sys.exit(run())


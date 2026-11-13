#!/usr/bin/env python3
"""
Build `docs/map.html` - an interactive map of every GujRERA scheme in Ahmedabad,
Gandhinagar and Mehsana, from the dataset produced by `python run.py --geo`.

The page is a single self-contained file: the whole dataset is embedded, so it works
straight from disk (no server, no CORS) as well as on GitHub Pages. Tiles come from
OpenStreetMap and Leaflet/markercluster from a CDN.

Run:
    python build_map.py
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUTPUT = REPO / "output"
GEO = OUTPUT / "geo"
SCHEMES = GEO / "schemes.json"
SITE = REPO / "docs"

GENERATED_AT = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M")

# Fields the detail panel renders. Everything else stays in the CSV/GeoJSON downloads.
EMBED_FIELDS = [
    "pid", "name", "regNo", "districtKey", "district", "subDistrict", "taluka",
    "address", "pincode", "projectType", "projectStatus", "stage", "portalStage",
    "processType", "alteration", "promoter", "promoterType", "promoterEmail",
    "promoterMobile", "cost", "approvedOn", "submittedOn", "startDate", "completionDate",
    "landAreaSqm", "carpetAreaSqm", "coveredAreaSqm", "openAreaSqm", "boundarySqm",
    "approvingAuthority", "projectDesc", "moje", "surveyNo", "citySurveyNo",
    "tpName", "tpNo", "finalPlotNo", "subPlotNo", "waterLevel", "progress",
    "units", "booked", "unitCostMin", "unitCostMax", "lat", "lng", "hasBoundary",
    "coordinateWarning", "boundaryWarning", "boundary", "discoverySource",
]


def _code(index: int) -> str:
    """A short, stable key for the embedded payload (k0, k1, ... k49)."""
    return f"k{index}"


def _norm_value(field, value):
    """Trim values for transport: integers stay integers, areas get 2 decimals."""
    if field == "boundary":
        return " ".join(f"{lat:.5f},{lng:.5f}" for lat, lng in value)
    if field in ("cost", "landAreaSqm", "carpetAreaSqm", "coveredAreaSqm", "openAreaSqm",
                 "boundarySqm", "unitCostMin", "unitCostMax"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        return int(round(number)) if field in ("cost", "unitCostMin", "unitCostMax") \
            else round(number, 2)
    return value


def embed_records(records):
    """Pack the dataset for inlining: short keys, dropped empties, compact boundaries.

    Short keys plus a `keys` map cut the page size roughly in half; the browser expands
    them back to the readable field names before anything else runs.
    """
    codes = {field: _code(index) for index, field in enumerate(EMBED_FIELDS)}
    rows = []
    for record in records:
        row = {}
        for field in EMBED_FIELDS:
            value = record.get(field)
            if value in (None, "", [], {}):
                continue
            row[codes[field]] = _norm_value(field, value)
        rows.append(row)
    return {"keys": {code: field for field, code in codes.items()}, "rows": rows}


def build(summary, records):
    packed = embed_records(records)
    payload = {"summary": summary, "keys": packed["keys"], "rows": packed["rows"]}
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    totals = {
        "total": summary["total"],
        "mapped": summary["withCoordinates"],
        "boundary": summary["withBoundary"],
        "missing": summary["total"] - summary["withCoordinates"],
    }
    return (PAGE
            .replace("__DATA__", data_json)
            .replace("__GENERATED_AT__", GENERATED_AT)
            .replace("__TOTALS__", json.dumps(totals))
            .replace("__DISTRICTS__", json.dumps(summary["districts"], ensure_ascii=False)))


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GujRERA Scheme Map — Ahmedabad · Gandhinagar · Mehsana</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<style>
  :root {
    --bg: #f6f5f2; --surface: #ffffff; --ink: #1c1b19; --muted: #6b6862;
    --line: #e4e2dd; --brand: #1f4e5f; --brand-dark: #153747; --accent: #b3592a;
    --ok: #2e7d32; --warn: #b26a00; --err: #b3261e;
    --ahmedabad: #1f6feb; --gandhinagar: #15803d; --mehsana: #b45309;
    --radius: 10px; --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.05);
    --sidebar: 392px; --header: 58px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    color: var(--ink); background: var(--bg); line-height: 1.5; overflow: hidden;
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--brand); }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }

  /* ---------- header ---------- */
  header {
    height: var(--header); background: var(--brand-dark); color: #fff;
    display: flex; align-items: center; gap: 16px; padding: 0 18px; position: relative; z-index: 1200;
  }
  header .brand { font-weight: 700; letter-spacing: .2px; color: #fff; text-decoration: none; font-size: 15px; white-space: nowrap; }
  header .sub { font-size: 12.5px; opacity: .78; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  header .stats { margin-left: auto; display: flex; gap: 18px; font-size: 12.5px; }
  header .stats b { font-size: 15px; display: block; line-height: 1.1; }
  header .stats span { opacity: .72; }
  header .nav { display: flex; gap: 8px; }
  header .nav a {
    color: #fff; text-decoration: none; font-size: 12.5px; font-weight: 600;
    border: 1px solid rgba(255,255,255,.28); border-radius: 999px; padding: 6px 12px; white-space: nowrap;
  }
  header .nav a:hover { background: rgba(255,255,255,.12); }

  /* ---------- layout ---------- */
  .shell { display: flex; height: calc(100% - var(--header)); }
  aside {
    width: var(--sidebar); min-width: var(--sidebar); background: var(--surface);
    border-right: 1px solid var(--line); display: flex; flex-direction: column;
    z-index: 1100; min-height: 0;
  }
  #map { flex: 1; height: 100%; background: #e8e6e1; }

  /* ---------- filters ---------- */
  .filters { padding: 14px 16px 12px; border-bottom: 1px solid var(--line); }
  .search { position: relative; }
  .search input {
    width: 100%; padding: 10px 30px 10px 34px; font-size: 14px; border-radius: 8px;
    border: 1px solid var(--line); background: #fbfaf8; color: var(--ink);
  }
  .search input:focus { outline: 2px solid rgba(31,78,95,.28); outline-offset: 1px; }
  .search svg { position: absolute; left: 10px; top: 10px; width: 16px; height: 16px; opacity: .45; }
  .search .clear {
    position: absolute; right: 6px; top: 5px; border: 0; background: none; cursor: pointer;
    font-size: 17px; color: var(--muted); padding: 4px 7px; line-height: 1;
  }
  .group { margin-top: 12px; }
  .group > .label {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: .09em;
    color: var(--muted); font-weight: 700; margin-bottom: 6px;
  }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    border: 1px solid var(--line); background: #fbfaf8; color: var(--ink); border-radius: 999px;
    padding: 5px 11px; font-size: 12.5px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
    font-weight: 600; user-select: none; font-family: inherit;
  }
  .chip .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; opacity: .9; }
  .chip[aria-pressed="true"] { background: var(--brand); border-color: var(--brand); color: #fff; }
  .chip[data-district="ahmedabad"][aria-pressed="true"] { background: var(--ahmedabad); border-color: var(--ahmedabad); }
  .chip[data-district="gandhinagar"][aria-pressed="true"] { background: var(--gandhinagar); border-color: var(--gandhinagar); }
  .chip[data-district="mehsana"][aria-pressed="true"] { background: var(--mehsana); border-color: var(--mehsana); }
  select {
    width: 100%; padding: 8px 10px; font-size: 13px; border-radius: 8px;
    border: 1px solid var(--line); background: #fbfaf8; color: var(--ink);
  }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .toggles { display: flex; flex-direction: column; gap: 7px; margin-top: 12px; }
  .toggle { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--ink); cursor: pointer; }
  .toggle input { accent-color: var(--brand); width: 15px; height: 15px; }

  .results-head {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 10px 16px; border-bottom: 1px solid var(--line); background: #fbfaf8;
  }
  .results-head b { font-size: 12.5px; }
  .results-head button {
    border: 1px solid var(--line); background: #fff; border-radius: 7px; padding: 5px 9px;
    font-size: 12px; cursor: pointer; color: var(--brand); font-weight: 650; font-family: inherit;
  }
  .results-head button:hover { background: #f1efeb; }
  .results { overflow-y: auto; flex: 1; min-height: 0; }
  .result {
    display: block; width: 100%; text-align: left; border: 0; border-bottom: 1px solid #f1efeb;
    background: none; padding: 10px 16px; cursor: pointer; font: inherit; color: inherit;
  }
  .result:hover, .result.active { background: #f4f7f8; }
  .result .name { font-weight: 650; font-size: 13.5px; line-height: 1.3; display: flex; gap: 6px; align-items: baseline; }
  .result .meta { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
  .result .tail { font-size: 11.5px; color: var(--muted); margin-top: 2px; display: flex; gap: 8px; flex-wrap: wrap; }
  .pill {
    display: inline-block; font-size: 10px; font-weight: 700; letter-spacing: .04em;
    text-transform: uppercase; padding: 1px 6px; border-radius: 999px;
    border: 1px solid currentColor; white-space: nowrap;
  }
  .pill.under-construction { color: var(--ok); }
  .pill.upcoming { color: var(--brand); }
  .pill.past-due { color: var(--err); }
  .pill.completed { color: var(--muted); }
  .pill.unknown { color: var(--warn); }
  .pill.nocoord { color: var(--err); }
  .empty { padding: 26px 18px; color: var(--muted); font-size: 13px; text-align: center; }

  /* ---------- detail panel ---------- */
  .detail { display: none; flex-direction: column; flex: 1; min-height: 0; }
  .detail.open { display: flex; }
  .detail .head {
    display: flex; align-items: center; gap: 8px; padding: 10px 14px;
    border-bottom: 1px solid var(--line); background: #fbfaf8;
  }
  .detail .head button {
    border: 1px solid var(--line); background: #fff; border-radius: 8px; padding: 5px 10px;
    cursor: pointer; font-size: 12px; font-weight: 650; color: var(--brand); font-family: inherit;
  }
  .detail .head button:hover { background: #f1efeb; }
  .detail .head .spacer { margin-left: auto; }
  .detail .head .count { font-size: 11.5px; color: var(--muted); }
  .detail .body { padding: 15px 16px 30px; overflow-y: auto; }
  .detail h2 { font-size: 17.5px; margin: 0 0 3px; line-height: 1.25; }
  .detail .reg { font-size: 11px; color: var(--muted); word-break: break-all; margin-bottom: 9px; }
  .detail .badges { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }
  .detail .badge {
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
    padding: 2px 7px; border-radius: 999px; background: #eef3f5; color: var(--brand-dark);
  }
  .detail .badge.st-under-construction { background: #e7f3e8; color: #1f6b24; }
  .detail .badge.st-upcoming { background: #e8effa; color: #174a9c; }
  .detail .badge.st-past-due { background: #fdecea; color: #a0201a; }
  .detail .badge.st-completed { background: #eeeeee; color: #55524d; }
  .detail .badge.st-unknown { background: #fdf1e0; color: #8a5300; }
  .detail .badge.warn { background: #fdecea; color: #a0201a; }
  .detail .addr { margin: 9px 0; padding: 8px 10px; background: #f7f6f3; border-radius: 8px; font-size: 12.5px; color: #3c3a36; }
  .detail table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .detail td { padding: 4px 0; vertical-align: top; border-top: 1px solid #f0eeea; }
  .detail tr:first-child td { border-top: 0; }
  .detail td.k { color: var(--muted); width: 44%; padding-right: 10px; }
  .detail td.v { font-weight: 600; word-break: break-word; }
  .detail .coords { font-size: 12px; color: var(--muted); margin-top: 9px; }
  .detail .coords code { color: var(--ink); background: #f2f0ec; padding: 2px 6px; border-radius: 5px; }
  .detail .acts { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 12px; }
  .detail .acts a, .detail .acts button {
    font-size: 12px; font-weight: 650; text-decoration: none; padding: 6px 10px; border-radius: 8px;
    border: 1px solid var(--line); background: #fbfaf8; color: var(--brand); cursor: pointer; font-family: inherit;
  }
  .detail .acts a:hover, .detail .acts button:hover { background: #f1efeb; }
  .detail .acts .primary { background: var(--brand); border-color: var(--brand); color: #fff; }
  .detail .acts .primary:hover { background: var(--brand-dark); }
  .detail .note { font-size: 12px; color: #45423d; margin-top: 11px; padding-top: 10px; border-top: 1px solid var(--line); }

  /* ---------- map furniture ---------- */
  .legend {
    background: rgba(255,255,255,.95); border-radius: 8px; box-shadow: var(--shadow);
    padding: 9px 12px; font-size: 12px; line-height: 1.7; color: var(--ink);
  }
  .legend i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; }
  .leaflet-popup-content { margin: 11px 13px 13px; width: auto !important; font-size: 12.5px; }
  .leaflet-popup-content-wrapper { border-radius: 10px; }

  .mini { font-size: 12.5px; min-width: 232px; }
  .mini h3 { margin: 0 0 2px; font-size: 14px; line-height: 1.3; }
  .mini .m-reg { font-size: 10px; color: var(--muted); word-break: break-all; margin-bottom: 6px; }
  .mini .m-row { display: flex; justify-content: space-between; gap: 10px; padding: 2px 0; }
  .mini .m-row span:first-child { color: var(--muted); }
  .mini .m-row span:last-child { font-weight: 650; text-align: right; }
  .mini button {
    margin-top: 8px; width: 100%; border: 0; background: var(--brand); color: #fff;
    border-radius: 7px; padding: 6px; font-size: 12px; font-weight: 650; cursor: pointer; font-family: inherit;
  }
  .mini button:hover { background: var(--brand-dark); }

  .mobile-bar { display: none; }

  @media (max-width: 900px) {
    :root { --header: auto; }
    header { flex-wrap: wrap; padding: 10px 14px; gap: 8px; }
    header .stats, header .sub { display: none; }
    .legend { display: none; }
    .shell { flex-direction: column; height: 100%; }
    aside {
      width: 100%; min-width: 0; border-right: 0; border-top: 1px solid var(--line);
      max-height: 54vh; order: 2;
    }
    #map { order: 1; min-height: 46vh; }
    .mobile-bar { display: flex; gap: 8px; padding: 8px 14px; background: var(--brand-dark); }
    .mobile-bar button {
      flex: 1; padding: 8px; border-radius: 8px; border: 1px solid rgba(255,255,255,.3);
      background: rgba(255,255,255,.08); color: #fff; font-weight: 600; font-size: 13px; cursor: pointer;
      font-family: inherit;
    }
  }
</style>
</head>
<body>
<header>
  <a class="brand" href="index.html">GujRERA Scheme Map</a>
  <span class="sub">Ahmedabad · Gandhinagar · Mehsana — every registered scheme, with its exact boundary</span>
  <div class="stats">
    <div><b id="stat-total">—</b><span>schemes</span></div>
    <div><b id="stat-mapped">—</b><span>mapped</span></div>
    <div><b id="stat-boundary">—</b><span>with boundary</span></div>
  </div>
  <nav class="nav">
    <a href="index.html">Reports</a>
    <a href="downloads/schemes.csv">CSV</a>
    <a href="downloads/boundaries.geojson">GeoJSON</a>
  </nav>
</header>

<div class="mobile-bar">
  <button id="toggle-filters">Filters &amp; results</button>
</div>

<div class="shell">
  <aside id="sidebar">
    <div class="filters" id="filters">
      <div class="search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>
        <input id="q" type="search" placeholder="Name, registration no., promoter, area, pin…" autocomplete="off">
        <button class="clear" id="q-clear" title="Clear" hidden>×</button>
      </div>

      <div class="group">
        <div class="label">District</div>
        <div class="chips" id="district-chips"></div>
      </div>

      <div class="row2 group">
        <div>
          <div class="label">Stage</div>
          <select id="stage">
            <option value="">All stages</option>
            <option value="under-construction">Under construction</option>
            <option value="upcoming">Possession not yet started</option>
            <option value="past-due">Past declared completion</option>
            <option value="completed">Declared complete</option>
            <option value="unknown">No dates published</option>
          </select>
        </div>
        <div>
          <div class="label">Project type</div>
          <select id="type"><option value="">All types</option></select>
        </div>
      </div>

      <div class="toggles">
        <label class="toggle"><input type="checkbox" id="only-boundary"> Only schemes with an exact boundary</label>
        <label class="toggle"><input type="checkbox" id="hide-alteration" checked> Merge/hide alteration records</label>
      </div>
    </div>

    <div class="results-head" id="results-head">
      <b id="result-count">—</b>
      <button id="fit" title="Zoom the map to the current results">Fit map</button>
    </div>
    <div class="results" id="results"></div>

    <div class="detail" id="detail">
      <div class="head">
        <button id="detail-back">← Results</button>
        <button id="detail-prev" title="Previous scheme">‹</button>
        <button id="detail-next" title="Next scheme">›</button>
        <span class="spacer"></span>
        <span class="count" id="detail-count"></span>
      </div>
      <div class="body" id="detail-body"></div>
    </div>
  </aside>

  <div id="map"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script id="dataset" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("dataset").textContent);
  var RECORDS = unpack(DATA.rows || [], DATA.keys || {});
  var SUMMARY = DATA.summary || {};
  var DISTRICTS = __DISTRICTS__;
  var TOTALS = __TOTALS__;
  var GENERATED_AT = "__GENERATED_AT__";

  var COLOURS = { ahmedabad: "#1f6feb", gandhinagar: "#15803d", mehsana: "#b45309" };
  var LIST_LIMIT = 250;

  // The dataset is shipped with short keys and boundaries as "lat,lng lat,lng" strings
  // to keep the page small; expand it once, up front, into plain records.
  function decodeBoundary(text) {
    if (typeof text !== "string" || !text) return null;
    return text.split(" ").map(function (pair) {
      var bits = pair.split(",");
      return [parseFloat(bits[0]), parseFloat(bits[1])];
    });
  }

  function unpack(rows, keys) {
    return rows.map(function (row) {
      var out = {};
      for (var code in row) {
        if (!Object.prototype.hasOwnProperty.call(row, code)) continue;
        var field = keys[code] || code;
        out[field] = field === "boundary" ? decodeBoundary(row[code]) : row[code];
      }
      return out;
    });
  }

  // ------------------------------------------------------------------ formatting ---
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function inr(value) {
    var n = Number(value);
    if (!isFinite(n) || n === 0) return "—";
    var whole = Math.round(Math.abs(n)).toString();
    var tail = whole.slice(-3), rest = whole.slice(0, -3);
    if (rest) { rest = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ","); whole = rest + "," + tail; }
    return "₹ " + whole;
  }
  function crore(value) {
    var n = Number(value);
    if (!isFinite(n) || n <= 0) return "—";
    if (n >= 1e7) return "₹ " + (n / 1e7).toFixed(2).replace(/\.00$/, "") + " Cr";
    if (n >= 1e5) return "₹ " + (n / 1e5).toFixed(2).replace(/\.00$/, "") + " L";
    return inr(n);
  }
  function area(m2) {
    var n = Number(m2);
    if (!isFinite(n) || n <= 0) return "—";
    var text = Math.round(n).toLocaleString("en-IN") + " m²";
    if (n >= 4046.86) text += " · " + (n / 4046.86).toFixed(2) + " acre";
    return text;
  }
  function day(iso) {
    if (!iso) return "—";
    var parts = String(iso).split("-");
    return parts.length === 3 ? parts[2] + "-" + parts[1] + "-" + parts[0] : String(iso);
  }
  function titleCase(text) {
    return String(text || "").toLowerCase().replace(/\b[a-z]/g, function (c) { return c.toUpperCase(); });
  }
  function num(value) {
    var n = Number(value);
    return isFinite(n) ? n.toLocaleString("en-IN") : "—";
  }

  // ------------------------------------------------------------------- the map ---
  var map = L.map("map", { zoomControl: true, preferCanvas: true })
    .setView([23.15, 72.62], 10);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
                 "contributors · Data: GujRERA public portal · built " + GENERATED_AT
  }).addTo(map);

  L.control.scale({ imperial: false }).addTo(map);

  var legend = L.control({ position: "bottomright" });
  legend.onAdd = function () {
    var div = L.DomUtil.create("div", "legend");
    div.innerHTML = Object.keys(DISTRICTS).map(function (key) {
      return '<div><i style="background:' + COLOURS[key] + '"></i>' +
             esc(DISTRICTS[key].label) + " — " + DISTRICTS[key].total + " schemes</div>";
    }).join("") + '<div style="margin-top:4px;color:#6b6862">Click a pin for details</div>';
    return div;
  };
  legend.addTo(map);

  function pinIcon(key) {
    return L.divIcon({
      className: "",
      html: '<span style="display:block;width:11px;height:11px;border-radius:50%;background:' +
            (COLOURS[key] || "#555") + ';border:1.5px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.18)"></span>',
      iconSize: [11, 11], iconAnchor: [5.5, 5.5], popupAnchor: [0, -6]
    });
  }

  var clusters = L.markerClusterGroup({
    maxClusterRadius: 46,
    chunkedLoading: true,
    spiderfyOnMaxZoom: true,
    disableClusteringAtZoom: 17,
    iconCreateFunction: function (cluster) {
      var count = cluster.getChildCount();
      var size = count > 500 ? 46 : count > 100 ? 40 : count > 20 ? 34 : 28;
      return L.divIcon({
        html: '<div style="width:' + size + "px;height:" + size + "px;line-height:" + size +
              "px;border-radius:50%;background:rgba(31,78,95,.86);color:#fff;font-weight:700;" +
              "font-size:" + (size > 36 ? 13 : 12) + 'px;text-align:center;border:2px solid rgba(255,255,255,.9)">' +
              count + "</div>",
        className: "", iconSize: [size, size]
      });
    }
  });
  map.addLayer(clusters);

  var boundaryLayer = null;
  var markerByPid = {};
  var byPid = {};
  RECORDS.forEach(function (r) { byPid[r.pid] = r; });

  function clearBoundary() {
    if (boundaryLayer) { map.removeLayer(boundaryLayer); boundaryLayer = null; }
  }

  function drawBoundary(record, fit) {
    clearBoundary();
    if (!record.boundary || record.boundary.length < 3) return false;
    var colour = COLOURS[record.districtKey] || "#1f4e5f";
    boundaryLayer = L.polygon(record.boundary, {
      color: colour, weight: 2, opacity: 0.95, fillOpacity: 0.18, fillColor: colour
    }).addTo(map);
    if (fit) map.fitBounds(L.latLngBounds(record.boundary).pad(0.45), { maxZoom: 17 });
    return true;
  }

  // --------------------------------------------------------------------- detail ---
  function detailHtml(r) {
    var rows = [];
    function add(label, value) {
      if (value === undefined || value === null || value === "" || value === "—") return;
      rows.push([label, value]);
    }

    add("District", esc(r.district) + (r.subDistrict ? " · " + esc(r.subDistrict) : ""));
    add("Project type", esc(r.projectType));
    add("Portal status", esc(r.projectStatus));
    add("Process", r.processType && r.processType !== "REG" ? esc(r.processType) : null);
    add("Promoter", esc(r.promoter) + (r.promoterType
        ? ' <span style="font-weight:400;color:#6b6862">(' + esc(titleCase(r.promoterType)) + ")</span>" : ""));
    add("Declared project cost", crore(r.cost));
    add("Registration approved", day(r.approvedOn));
    add("Application submitted", day(r.submittedOn));
    add("Site possession", day(r.startDate));
    add("Declared completion", day(r.completionDate));
    add("Land under registration", area(r.landAreaSqm));
    add("Boundary drawn on portal", r.hasBoundary ? area(r.boundarySqm) : "not drawn");
    add("Carpet area (saleable)", area(r.carpetAreaSqm));
    add("Covered / open area", (r.coveredAreaSqm || r.openAreaSqm)
        ? [area(r.coveredAreaSqm), area(r.openAreaSqm)].join(" / ") : null);
    add("Units / booked", r.units ? (num(r.units) + " / " + (r.booked == null ? "—" : num(r.booked))) : null);
    add("Construction progress", r.progress != null ? Math.round(r.progress) + "%" : null);
    add("Approving authority", esc(r.approvingAuthority));
    add("Survey no.", esc(r.surveyNo || r.citySurveyNo));
    add("TP / final plot", [r.tpName, r.tpNo ? "TP " + r.tpNo : null,
                            r.finalPlotNo ? "FP " + r.finalPlotNo : null,
                            r.subPlotNo ? "Sub-plot " + r.subPlotNo : null]
        .filter(Boolean).join(" · ") || null);
    add("Village (moje)", esc(r.moje));
    add("Pin code", esc(r.pincode));
    add("Unit price band", r.unitCostMin ? crore(r.unitCostMin) + " – " + crore(r.unitCostMax) : null);
    add("Promoter contact", [r.promoterMobile, r.promoterEmail].filter(Boolean).map(esc).join(" · ") || null);

    var html = "<h2>" + esc(r.name) + "</h2>";
    html += '<div class="reg">' + esc(r.regNo || "registration number not published") +
            " · Project ID " + r.pid + "</div>";
    html += '<div class="badges"><span class="badge st-' + (r.stage || "unknown") + '">' +
            esc(String(r.stage || "unknown").replace(/-/g, " ")) + "</span>";
    if (r.alteration) html += '<span class="badge">alteration</span>';
    if (r.portalStage) html += '<span class="badge">portal: ' + esc(r.portalStage) + "</span>";
    if (r.lat == null) html += '<span class="badge warn">no coordinates published</span>';
    html += "</div>";

    if (r.address) html += '<div class="addr">' + esc(r.address) + "</div>";

    html += "<table><tbody>";
    rows.forEach(function (pair) {
      html += '<tr><td class="k">' + pair[0] + '</td><td class="v">' + pair[1] + "</td></tr>";
    });
    html += "</tbody></table>";

    if (r.lat != null && r.lng != null) {
      html += '<div class="coords">Latitude <code>' + r.lat.toFixed(6) +
              '</code><br>Longitude <code>' + r.lng.toFixed(6) + "</code></div>";
    }
    if (r.coordinateWarning) {
      html += '<div class="note" style="color:#a0201a">⚠ ' + esc(r.coordinateWarning) +
              " — the portal's own coordinates for this scheme look inconsistent; treat the pin as indicative.</div>";
    }

    html += '<div class="acts">';
    if (r.hasBoundary) html += '<button class="primary" id="act-boundary">Show exact boundary</button>';
    if (r.lat != null) {
      html += '<a href="https://www.google.com/maps/search/?api=1&query=' + r.lat + "," + r.lng +
              '" target="_blank" rel="noopener">Google Maps ↗</a>';
      html += '<a href="https://www.openstreetmap.org/?mlat=' + r.lat + "&mlon=" + r.lng +
              '#map=17/' + r.lat + "/" + r.lng + '" target="_blank" rel="noopener">OSM ↗</a>';
    }
    html += "</div>";
    if (r.boundaryWarning) {
      html += '<div class="note" style="color:#a0201a">⚠ ' + esc(r.boundaryWarning) +
              " — only the centre point is shown; the plotted shape was discarded.</div>";
    }
    if (r.projectDesc) html += '<div class="note">' + esc(r.projectDesc) + "</div>";
    return html;
  }

  function popupHtml(r) {
    var html = '<div class="mini"><h3>' + esc(r.name) + "</h3>" +
      '<div class="m-reg">' + esc(r.regNo || "") + "</div>" +
      '<div class="m-row"><span>District</span><span>' + esc(r.subDistrict || r.district) + "</span></div>" +
      '<div class="m-row"><span>Type</span><span>' + esc(r.projectType || "—") + "</span></div>" +
      '<div class="m-row"><span>Cost</span><span>' + crore(r.cost) + "</span></div>" +
      (r.lat != null ? '<div class="m-row"><span>Coordinates</span><span>' + r.lat.toFixed(4) +
        ", " + r.lng.toFixed(4) + "</span></div>" : "") +
      '<button data-open="' + r.pid + '">Full details →</button></div>';
    return html;
  }

  var resultsEl = document.getElementById("results");
  var resultsHead = document.getElementById("results-head");
  var filtersEl = document.getElementById("filters");
  var detailEl = document.getElementById("detail");
  var detailBody = document.getElementById("detail-body");
  var detailCount = document.getElementById("detail-count");
  var countEl = document.getElementById("result-count");

  var current = [];
  var selected = null;

  function showDetail(record) {
    if (!record) return;
    selected = record;
    detailBody.innerHTML = detailHtml(record);
    detailBody.scrollTop = 0;
    filtersEl.style.display = "none";
    resultsHead.style.display = "none";
    resultsEl.style.display = "none";
    detailEl.classList.add("open");

    var index = current.indexOf(record);
    detailCount.textContent = index >= 0
      ? (index + 1).toLocaleString("en-IN") + " of " + current.length.toLocaleString("en-IN")
      : "";

    var boundaryButton = document.getElementById("act-boundary");
    if (boundaryButton) {
      boundaryButton.addEventListener("click", function () { drawBoundary(record, true); });
    }
    markActiveRow(record.pid);
  }

  function hideDetail() {
    selected = null;
    detailEl.classList.remove("open");
    filtersEl.style.display = "";
    resultsHead.style.display = "";
    resultsEl.style.display = "";
    clearBoundary();
    map.closePopup();
  }

  function step(offset) {
    if (!current.length) return;
    var index = selected ? current.indexOf(selected) : -1;
    var next = index < 0 ? 0 : (index + offset + current.length) % current.length;
    openRecord(current[next], true, true);
  }

  function openRecord(record, zoom, fromList) {
    if (!record) return;
    map.closePopup();
    showDetail(record);
    if (record.lat != null && record.lng != null) {
      if (record.hasBoundary && record.boundary && record.boundary.length > 2) {
        drawBoundary(record, true);
      } else {
        clearBoundary();
        map.setView([record.lat, record.lng], Math.max(map.getZoom(), 15), { animate: true });
      }
      var marker = markerByPid[record.pid];
      if (marker && fromList) marker.openPopup();
    }
  }

  map.on("popupopen", function (event) {
    var node = event.popup.getElement();
    if (!node) return;
    var button = node.querySelector("[data-open]");
    if (button) {
      button.addEventListener("click", function () {
        var record = byPid[Number(button.getAttribute("data-open"))];
        map.closePopup();
        showDetail(record);
        if (record && record.hasBoundary) drawBoundary(record, true);
      });
    }
  });

  // --------------------------------------------------------------------- state ---
  var state = {
    q: "",
    districts: { ahmedabad: true, gandhinagar: true, mehsana: true },
    stage: "",
    type: "",
    onlyBoundary: false,
    hideAlteration: true
  };

  function matches(record) {
    if (!state.districts[record.districtKey]) return false;
    if (state.stage && record.stage !== state.stage) return false;
    if (state.type && record.projectType !== state.type) return false;
    if (state.onlyBoundary && !record.hasBoundary) return false;
    if (state.hideAlteration && record.alteration) return false;
    if (state.q) {
      var haystack = [record.name, record.regNo, record.promoter, record.address,
                      record.subDistrict, record.taluka, record.pincode, record.moje,
                      record.approvingAuthority, record.pid].join(" ").toLowerCase();
      if (haystack.indexOf(state.q) === -1) return false;
    }
    return true;
  }

  // --------------------------------------------------------------------- list ---
  function markActiveRow(pid) {
    resultsEl.querySelectorAll(".result.active").forEach(function (node) {
      node.classList.remove("active");
    });
    if (pid == null) return;
    var row = resultsEl.querySelector('.result[data-pid="' + pid + '"]');
    if (row) row.classList.add("active");
  }

  function renderList(records) {
    countEl.textContent = records.length.toLocaleString("en-IN") + " matching scheme" +
                          (records.length === 1 ? "" : "s");
    var shown = records.slice(0, LIST_LIMIT);
    var html = "";
    shown.forEach(function (r) {
      html += '<button class="result" data-pid="' + r.pid + '">' +
        '<div class="name">' + esc(r.name) + '<span class="pill ' + (r.stage || "unknown") + '">' +
        esc(r.stage || "?") + "</span></div>" +
        '<div class="meta">' + esc(r.projectType || "—") +
        (r.subDistrict ? " · " + esc(r.subDistrict) : "") + " · " + esc(r.district) + "</div>" +
        '<div class="tail"><span>' + crore(r.cost) + "</span>" +
        (r.lat != null ? "<span>" + r.lat.toFixed(4) + ", " + r.lng.toFixed(4) + "</span>"
                       : '<span class="pill nocoord">no coords</span>') +
        "</div></button>";
    });
    if (records.length > shown.length) {
      html += '<div class="empty">' + (records.length - shown.length).toLocaleString("en-IN") +
              " more schemes match — narrow the search, or use the map.</div>";
    }
    if (!records.length) html = '<div class="empty">No scheme matches these filters.</div>';
    resultsEl.innerHTML = html;
    resultsEl.querySelectorAll(".result").forEach(function (node) {
      node.addEventListener("click", function () {
        openRecord(byPid[Number(node.dataset.pid)], true, false);
      });
    });
  }

  function renderMarkers(records) {
    clusters.clearLayers();
    markerByPid = {};
    var layer = [];
    records.forEach(function (r) {
      if (r.lat == null || r.lng == null) return;
      var marker = L.marker([r.lat, r.lng], { icon: pinIcon(r.districtKey), title: r.name });
      marker.bindPopup(popupHtml(r), { maxWidth: 300, minWidth: 250, autoPan: true, keepInView: true });
      markerByPid[r.pid] = marker;
      layer.push(marker);
    });
    clusters.addLayers(layer);
  }

  function apply() {
    current = RECORDS.filter(matches);
    renderList(current);
    renderMarkers(current);
    if (selected) {
      var still = current.indexOf(selected) >= 0;
      if (still) showDetail(selected); else hideDetail();
    }
  }

  // -------------------------------------------------------------------- wiring ---
  var qInput = document.getElementById("q");
  var qClear = document.getElementById("q-clear");
  var debounce;
  qInput.addEventListener("input", function () {
    qClear.hidden = !qInput.value;
    clearTimeout(debounce);
    debounce = setTimeout(function () {
      state.q = qInput.value.trim().toLowerCase();
      apply();
    }, 160);
  });
  qClear.addEventListener("click", function () {
    qInput.value = ""; qClear.hidden = true; state.q = ""; apply(); qInput.focus();
  });

  var chipsEl = document.getElementById("district-chips");
  Object.keys(DISTRICTS).forEach(function (key) {
    var info = DISTRICTS[key];
    var chip = document.createElement("button");
    chip.className = "chip";
    chip.dataset.district = key;
    chip.setAttribute("aria-pressed", "true");
    chip.innerHTML = '<span class="dot"></span>' + esc(info.label) +
                     ' <span style="opacity:.7;font-weight:500">' + info.total + "</span>";
    chip.addEventListener("click", function () {
      var pressed = chip.getAttribute("aria-pressed") === "true";
      var next = !pressed;
      var others = Object.keys(state.districts).filter(function (other) { return other !== key; });
      if (!next && !others.some(function (other) { return state.districts[other]; })) return;
      chip.setAttribute("aria-pressed", String(next));
      state.districts[key] = next;
      apply();
    });
    chipsEl.appendChild(chip);
  });

  document.getElementById("stage").addEventListener("change", function (event) {
    state.stage = event.target.value; apply();
  });

  var typeSelect = document.getElementById("type");
  (SUMMARY.types || []).forEach(function (entry) {
    if (!entry.value || entry.value === "Unknown") return;
    var option = document.createElement("option");
    option.value = entry.value;
    option.textContent = entry.value + " (" + entry.count + ")";
    typeSelect.appendChild(option);
  });
  typeSelect.addEventListener("change", function (event) {
    state.type = event.target.value; apply();
  });

  document.getElementById("only-boundary").addEventListener("change", function (event) {
    state.onlyBoundary = event.target.checked; apply();
  });
  document.getElementById("hide-alteration").addEventListener("change", function (event) {
    state.hideAlteration = event.target.checked; apply();
  });

  document.getElementById("fit").addEventListener("click", function () {
    var points = current.filter(function (r) { return r.lat != null && !r.coordinateWarning; })
                        .map(function (r) { return [r.lat, r.lng]; });
    if (!points.length) {
      points = current.filter(function (r) { return r.lat != null; })
                      .map(function (r) { return [r.lat, r.lng]; });
    }
    if (!points.length) return;
    if (points.length === 1) { map.setView(points[0], 15); return; }
    map.fitBounds(L.latLngBounds(points).pad(0.08));
  });

  document.getElementById("detail-back").addEventListener("click", hideDetail);
  document.getElementById("detail-prev").addEventListener("click", function () { step(-1); });
  document.getElementById("detail-next").addEventListener("click", function () { step(1); });

  document.getElementById("toggle-filters").addEventListener("click", function () {
    var sidebar = document.getElementById("sidebar");
    sidebar.style.maxHeight = sidebar.style.maxHeight === "none" ? "" : "none";
    map.invalidateSize();
  });

  document.addEventListener("keydown", function (event) {
    var typing = document.activeElement === qInput;
    if (event.key === "/" && !typing) { event.preventDefault(); qInput.focus(); }
    if (event.key === "Escape") {
      if (typing) {
        qInput.value = ""; qClear.hidden = true; state.q = ""; apply(); qInput.blur();
      } else if (selected) {
        hideDetail();
      }
    }
    if (!typing && selected) {
      if (event.key === "ArrowDown" || event.key === "j") { event.preventDefault(); step(1); }
      if (event.key === "ArrowUp" || event.key === "k") { event.preventDefault(); step(-1); }
    }
  });

  // --------------------------------------------------------------------- boot ---
  document.getElementById("stat-total").textContent = TOTALS.total.toLocaleString("en-IN");
  document.getElementById("stat-mapped").textContent = TOTALS.mapped.toLocaleString("en-IN");
  document.getElementById("stat-boundary").textContent = TOTALS.boundary.toLocaleString("en-IN");

  apply();

  // Fit to the dense core of the dataset: a handful of portal coordinates are clearly
  // wrong (flagged `coordinateWarning`) and would otherwise drag the view to world scale.
  var trusted = RECORDS.filter(function (r) { return r.lat != null && !r.coordinateWarning; });
  if (!trusted.length) trusted = RECORDS.filter(function (r) { return r.lat != null; });
  if (trusted.length) {
    map.fitBounds(L.latLngBounds(trusted.map(function (r) { return [r.lat, r.lng]; })).pad(0.04));
  }
})();
</script>
</body>
</html>
"""


def copy_downloads():
    """Publish the client-facing dataset next to the map page.

    `schemes.geojson` (the centroids) is left out of the site: it duplicates the CSV and
    is the one file heavy enough to bloat the repository. It stays in `output/geo/`.
    """
    target = SITE / "downloads"
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("schemes.csv", "boundaries.geojson", "summary.json"):
        source = GEO / name
        if source.exists():
            shutil.copy2(source, target / name)
            copied.append(name)
    return copied


def run() -> int:
    if not SCHEMES.exists():
        print(f"[error] {SCHEMES} not found — run:  python run.py --geo", file=sys.stderr)
        return 1

    data = json.loads(SCHEMES.read_text(encoding="utf-8"))
    records = data.get("records") or []
    summary = data.get("summary") or {}
    if not records:
        print("[error] the geo dataset is empty", file=sys.stderr)
        return 1

    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "map.html").write_text(build(summary, records), encoding="utf-8")

    copied = copy_downloads()
    (SITE / ".nojekyll").write_text("", encoding="utf-8")

    size_mb = (SITE / "map.html").stat().st_size / 1e6
    print(f"[ok] docs/map.html  ({len(records):,} schemes, {size_mb:.1f} MB)")
    if copied:
        print(f"[ok] docs/downloads  ({', '.join(copied)})")
    return 0


if __name__ == "__main__":
    sys.exit(run())

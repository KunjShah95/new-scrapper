/**
 * gujrera-docs -- serves the scraper's document archive out of Cloudflare R2.
 *
 * Object keys mirror `output/`, so a request path is a path inside that folder:
 *
 *   GET /29742_SINDUR_VIENNA/files/ORIGINAL%20%2B%20SUPPL%20DEED.pdf
 *       -> R2 key "29742_SINDUR_VIENNA/files/ORIGINAL + SUPPL DEED.pdf"
 *
 * Behaviour
 *   - existing object          -> streamed, Range + conditional requests honoured
 *   - directory-ish path       -> generated HTML (or JSON) listing
 *   - not found                -> 404 page with the nearest parent listing
 *
 * Routes are deliberately dumb: no auth, no writes. Public regulatory data.
 */

const LIST_LIMIT = 1000;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", {
        status: 405,
        headers: { allow: "GET, HEAD", ...cors() },
      });
    }

    const key = safeDecode(url.pathname.replace(/^\/+/, ""));
    const wantsJson = url.searchParams.get("format") === "json";
    const forceDownload = url.searchParams.get("download") === "1";

    // Root, or a path that looks like a folder -> listing.
    if (key === "" || key.endsWith("/") || url.searchParams.has("list")) {
      return listing(env, key.replace(/\/+$/, ""), wantsJson, request);
    }

    const object = await env.BUCKET.get(key, {
      range: request.headers,
      onlyIf: request.headers,
    });

    if (object) {
      if (request.method === "HEAD") {
        return new Response(null, {
          status: 200,
          headers: objectHeaders(object, key, forceDownload, request),
        });
      }
      // `object.body` is null when a conditional header short-circuited (304).
      if (!object.body) {
        return new Response(null, { status: 304, headers: objectHeaders(object, key, forceDownload, request) });
      }
      const headers = objectHeaders(object, key, forceDownload, request);
      if (object.range) {
        const offset = object.range.offset || 0;
        const length = object.range.length ?? object.size - offset;
        headers.set("content-range", `bytes ${offset}-${offset + length - 1}/${object.size}`);
        return new Response(object.body, { status: 206, headers });
      }
      return new Response(object.body, { status: 200, headers });
    }

    // A page inside a project folder? fall back to its index.html.
    const indexKey = `${key}/index.html`;
    const index = await env.BUCKET.get(indexKey);
    if (index) {
      return new Response(request.method === "HEAD" ? null : index.body, {
        status: 200,
        headers: objectHeaders(index, indexKey, false, request),
      });
    }

    // Anything else: treat the path as a folder and show what is in it.
    const parent = key.includes("/") ? key.slice(0, key.lastIndexOf("/")) : "";
    const listingResponse = await listing(env, key, wantsJson, request, { status: 404, parent });
    return listingResponse;
  },
};

/* ------------------------------------------------------------------ helpers */

function cors() {
  return {
    "access-control-allow-origin": "*",
    "access-control-expose-headers": "content-length, content-range, etag",
  };
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function objectHeaders(object, key, forceDownload, request) {
  const headers = new Headers();
  const contentType =
    object.httpMetadata?.contentType || guessType(key);

  headers.set("content-type", contentType);
  headers.set("content-length", String(object.size));
  headers.set("accept-ranges", "bytes");
  headers.set("etag", object.httpEtag || "");
  // Moderate cache: the archive is append-only, filenames are stable.
  headers.set("cache-control", "public, max-age=3600");
  for (const [k, v] of Object.entries(cors())) headers.set(k, v);

  if (forceDownload) {
    headers.set(
      "content-disposition",
      `attachment; filename="${key.split("/").pop().replace(/"/g, "")}"`,
    );
  }
  if (request.method === "HEAD") headers.delete("content-length");
  return headers;
}

function guessType(key) {
  const ext = (key.split(".").pop() || "").toLowerCase();
  const map = {
    pdf: "application/pdf",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    png: "image/png",
    webp: "image/webp",
    gif: "image/gif",
    svg: "image/svg+xml",
    json: "application/json",
    geojson: "application/geo+json",
    csv: "text/csv; charset=utf-8",
    md: "text/markdown; charset=utf-8",
    html: "text/html; charset=utf-8",
    txt: "text/plain; charset=utf-8",
    zip: "application/zip",
  };
  return map[ext] || "application/octet-stream";
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function humanSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = Number(bytes) || 0;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

/**
 * Render one prefix level. `delimiter` keeps a 140k-object bucket navigable:
 * the response only contains the immediate children of `prefix`.
 */
async function listing(env, prefix, wantsJson, request, options = {}) {
  const base = prefix ? `${prefix}/` : "";
  const result = await env.BUCKET.list({
    prefix: base,
    delimiter: "/",
    limit: LIST_LIMIT,
  });

  const folders = (result.delimitedPrefixes || []).map((p) => p.slice(base.length).replace(/\/$/, ""));
  const files = (result.objects || [])
    .filter((o) => o.key !== base)
    .map((o) => ({
      name: o.key.slice(base.length),
      size: o.size,
      uploaded: o.uploaded ? new Date(o.uploaded).toISOString() : null,
    }));

  const status = options.status || 200;

  if (wantsJson) {
    return new Response(
      JSON.stringify(
        {
          prefix: base,
          folders,
          files,
          truncated: Boolean(result.truncated),
          note: "pass ?list to browse, ?download=1 to force a download",
        },
        null,
        2,
      ),
      {
        status,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "public, max-age=300",
          ...cors(),
        },
      },
    );
  }

  const segments = base.split("/").filter(Boolean);
  const crumbs = [`<a href="/">archive</a>`];
  segments.forEach((segment, i) => {
    const href = "/" + segments.slice(0, i + 1).map(encodeURIComponent).join("/");
    crumbs.push(`<a href="${href}/">${escapeHtml(segment)}</a>`);
  });

  const rows = [
    ...folders.map(
      (name) => `<tr><td class="i">&#128193;</td><td><a href="/${encodeURIComponent([...segments, name].join("/"))}/">${escapeHtml(name)}/</a></td><td class="s">&mdash;</td></tr>`,
    ),
    ...files.map((file) => {
      const href = "/" + [...segments, file.name].map(encodeURIComponent).join("/");
      return `<tr><td class="i">${file.name.endsWith(".pdf") ? "&#128196;" : "&#128462;"}</td><td><a href="${href}">${escapeHtml(file.name)}</a></td><td class="s">${humanSize(file.size)}</td></tr>`;
    }),
  ].join("\n");

  const parentLink =
    segments.length > 0
      ? `<a class="up" href="/${segments.slice(0, -1).map(encodeURIComponent).join("/")}/">&larr; up</a>`
      : "";

  const notice =
    status === 404
      ? `<p class="notice">Nothing is stored at <code>${escapeHtml(request.url)}</code>. Showing the closest folder instead.</p>`
      : "";

  const html = `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml("/" + base)} &middot; GujRERA archive</title>
<style>
  :root { color-scheme: light dark; --line:#e4e4e7; --dim:#71717a; --accent:#1d4ed8 }
  @media (prefers-color-scheme: dark){ :root{ --line:#2a2a31; --dim:#a1a1aa; --accent:#7aa2f7 } }
  *{box-sizing:border-box}
  body{margin:0;padding:2rem 1.25rem 4rem;font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;max-width:64rem;margin-inline:auto}
  h1{font-size:1.05rem;font-weight:600;margin:0 0 .35rem}
  nav{font-size:.82rem;color:var(--dim);margin-bottom:1.25rem;display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}
  nav a{color:var(--accent);text-decoration:none}
  nav a:hover{text-decoration:underline}
  .up{margin-left:auto}
  .notice{background:#f59e0b1a;border:1px solid #f59e0b55;padding:.6rem .8rem;border-radius:8px;font-size:.85rem}
  table{width:100%;border-collapse:collapse}
  td{padding:.5rem .4rem;border-bottom:1px solid var(--line);vertical-align:top}
  td.i{width:1.4rem;opacity:.65}
  td.s{text-align:right;color:var(--dim);white-space:nowrap;font-variant-numeric:tabular-nums;font-size:.82rem}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  .empty{color:var(--dim);padding:1.5rem 0}
  footer{margin-top:2.5rem;font-size:.78rem;color:var(--dim)}
  code{font-size:.85em}
</style>
</head><body>
<h1>GujRERA document archive</h1>
<nav>${crumbs.join('<span>/</span>')}${parentLink}</nav>
${notice}
${rows ? `<table>${rows}</table>` : `<p class="empty">This folder is empty.</p>`}
<footer>
  Served from Cloudflare R2 &middot; ${folders.length} folder(s), ${files.length} file(s) at this level
  &middot; <a href="?list&amp;format=json">JSON</a>
</footer>
</body></html>`;

  return new Response(html, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": status === 404 ? "no-store" : "public, max-age=300",
      ...cors(),
    },
  });
}

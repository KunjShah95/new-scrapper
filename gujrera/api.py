"""
HTTP client for the public GujRERA API.

The GujRERA site (https://gujrera.gujarat.gov.in) is an Angular SPA. Everything it
renders on the "Project View" screen comes from plain public JSON endpoints under
`project_reg/`, `formone/`, `formtwo/`, `formthree/`, `quarter/`, `form_five/`,
`dashboard/`, `maplocation/`, `user_reg/` and `vdms/`.

No login, cookie or CSRF token is required for the endpoints used here.

Proxy support
-------------
An optional egress proxy can be configured (see `proxy_from_env`). It is **off** unless
`GUJRERA_PROXY` / `PROXY_HOST` is set, because the portal is reachable directly and the
scraper is deliberately polite. If the proxy fails to tunnel to the portal - residential
providers routinely block `*.gov.in` - the client falls back to a direct connection and
says so once, instead of failing every request.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("gujrera.api")

BASE_URL = "https://gujrera.gujarat.gov.in/"

# --------------------------------------------------------------------------------------
# Endpoint templates. `{...}` placeholders are filled from the project bundle.
# --------------------------------------------------------------------------------------
ENDPOINTS: Dict[str, str] = {
    # ---- Project Summary -------------------------------------------------------------
    "core": "project_reg/public/alldatabyprojectid/{pid}",
    "applications": "project_reg/public/projectAllApplications/{pid}",
    "collection_bank": "project_reg/public/bank/get_col_account/{pid}",
    "project_banks": "project_reg/public/project-app/getproject-banks/{pid}",
    # ---- Project Profile -------------------------------------------------------------
    "profile": "project_reg/public/getproject-details/{pid}",
    "blocks": "project_reg/project-app/getproject-blocklist/{pid}",
    "location": "maplocation/public/getProjectLocations/{pid}",
    "form_one": "formone/public/getfrom-one-byformone-id/{form_one_id}",
    "form_two": "formtwo/public/getform-two-by-id/{form_two_id}",
    "form_three": "formthree/public/getform-three-byid/{form_three_id}",
    "form_three_loan": "formthree/public/getform-three-loan-details/{pid}",
    "form_one_photos": "formone/public/getfrom-one-photos-byformone-id/{pid}",
    "form_one_noc": "formone/public/project/noc/getnoc-by-proj-proid-type/{pid}/{app_reg_id}",
    "joint_development": "project_reg/public/land-owner/project{pid}",
    # ---- Promoters -------------------------------------------------------------------
    "promoter": "user_reg/promoter/promoter{promoter_id}",
    "previous_projects": "project_reg/public/getprev-project-list/{pid}",
    # ---- Progress & RERA Actions -----------------------------------------------------
    "progress_score": "project_reg/public/getProjectProgressReports-ByPrjRegId/{pid}",
    "block_progress": "dashboard/project-block-progress-dtl-by-prj-id/{pid}",
    "block_chart": "dashboard/get-all-block-chart-details-by-id/{pid}",
    "area_price": "dashboard/get-all-view-unit-details-by-id/{pid}",
    "qpr_progress": "quarter/get-project-progress/{pid}",
    "form_four": "quarter/get-by-project-id/{pid}",
    "qe_enquiry": "quarter/getQEEnquiryStatus/{pid}",
    "form_eight": "quarter/getformpdf-by-id/{pid}",
    "quarters": "quarter/public/getprojectqtrs/{pid}",
    "annual_compliance": "form_five/get-formfive-records-till-date/{pid}",
    # ---- Documents -------------------------------------------------------------------
    "scanned_docs": "vdms/gujreraapi/api/GetACK?AckNo={ack_no}",
    "download": "vdms/download/{uid}",
    # Pre-2019 registrations: scanned filings listed by `scanned_docs`.
    "scanned_file": "vdms/gujreraapi/getfile?RootFile={root_file}&FileName={file_name}",
}

# Prefix marking a document reference that is a scanned filing, not a VDMS UID.
# The rest is "<RootFile>/<FileName>", both base64-decoded, e.g.
# "scan:RAA00727151117/RAA00727151117_1.pdf".
SCAN_PREFIX = "scan:"


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back rather than crashing."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("ignoring %s=%r (not a number), using %s", name, raw, default)
        return default


def mask_proxy(url: Optional[str]) -> str:
    """`http://user:secret@host:port` -> `http://user:***@host:port`, for logs."""
    if not url or not str(url).strip():
        return "(none)"
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def proxy_from_env(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Build a proxy URL from the environment.

    Either give the whole thing:

        GUJRERA_PROXY=http://user:pass@gw.dataimpulse.com:823

    or the parts, which is friendlier for a `.env` file and for rotating accounts:

        PROXY_HOST=gw.dataimpulse.com
        PROXY_PORT=823
        PROXY_USER=802657b73f44ff7dbf6c
        PROXY_PASS=...
        PROXY_SCHEME=http            # http (default) | socks5 | socks5h
        PROXY_USER_SUFFIX=__cr.in    # provider-side targeting, appended to the username

    `PROXY_USER_SUFFIX` exists because residential providers select the exit country
    through the username (DataImpulse uses `__cr.in`, others use `-country-in`); nothing
    here assumes one vendor.
    """
    env = os.environ if env is None else env

    whole = (env.get("GUJRERA_PROXY") or "").strip()
    if whole:
        return whole

    host = (env.get("PROXY_HOST") or "").strip()
    if not host:
        return None
    port = (env.get("PROXY_PORT") or "").strip()
    scheme = (env.get("PROXY_SCHEME") or "http").strip().lower()
    user = (env.get("PROXY_USER") or "").strip()
    password = env.get("PROXY_PASS") or ""
    suffix = (env.get("PROXY_USER_SUFFIX") or "").strip()
    if user and suffix:
        user = f"{user}{suffix}"

    credentials = ""
    if user:
        credentials = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    port_part = f":{port}" if port else ""
    return f"{scheme}://{credentials}{host}{port_part}"


class GujreraClient:
    """Small, polite wrapper around the public GujRERA JSON + document endpoints."""

    def __init__(self, delay: Optional[float] = None, timeout: int = 120, retries: int = 4,
                 pool_maxsize: int = 32, proxy: Optional[str] = None,
                 proxy_fallback: Optional[bool] = None):
        self.delay = _env_float("GUJRERA_DELAY", 0.35) if delay is None else delay
        self.timeout = timeout
        self.proxy = proxy if proxy is not None else proxy_from_env()
        self.proxy_fallback = (
            os.environ.get("GUJRERA_PROXY_FALLBACK", "1") != "0"
            if proxy_fallback is None else proxy_fallback
        )
        self.proxy_failed = False
        self.session = requests.Session()
        retry = Retry(
            total=retries,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            # A proxy tunnel that is refused (403) or blocked will never succeed; fail
            # fast so the caller can fall back to a direct connection immediately.
            other=0,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        # Scanned filings answer HTTP 500 "(No such file or directory)" when the
        # portal lists a page it no longer holds. That is permanent, so 500 is not
        # auto-retried here; get_document reads the body and decides.
        scan_retry = Retry(
            total=retries,
            backoff_factor=1.5,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            other=0,
        )
        self.session.mount(
            BASE_URL + ENDPOINTS["scanned_file"].split("?", 1)[0],
            HTTPAdapter(max_retries=scan_retry, pool_connections=pool_maxsize, pool_maxsize=pool_maxsize),
        )
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})
            log.info("egress proxy: %s", mask_proxy(self.proxy))
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://gujrera.gujarat.gov.in",
                "Referer": "https://gujrera.gujarat.gov.in/",
            }
        )

    # ------------------------------------------------------------------- proxy ----
    def disable_proxy(self, reason: str) -> None:
        """Stop using the proxy for the rest of this client's life, and say why once."""
        if not self.proxy or self.proxy_failed:
            return
        self.proxy_failed = True
        self.session.proxies.clear()
        log.warning("proxy unusable for this host (%s) — continuing WITHOUT a proxy: %s",
                    reason, mask_proxy(self.proxy))

    def _proxy_error_reason(self, exc: Exception) -> str:
        text = str(exc)
        if "SITE_PERMANENTLY_BLOCKED" in text.upper():
            return "the proxy provider blocks this site"
        if "Tunnel connection failed" in text:
            return "the proxy refused to open a tunnel to this host"
        if "407" in text:
            return "the proxy rejected the credentials"
        return type(exc).__name__

    # ---------------------------------------------------------------- low level ----
    def url(self, name: str, **kwargs: Any) -> str:
        template = ENDPOINTS[name]
        return BASE_URL + template.format(**kwargs)

    def get_json(self, name: str, **kwargs: Any) -> Tuple[Optional[Any], Optional[str]]:
        """GET an endpoint. Returns (payload, error_message)."""
        url = self.url(name, **kwargs)
        return self._request_json("GET", url)

    def post_json(self, path: str, payload: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
        return self._request_json("POST", BASE_URL + path, payload)

    def _request_json(self, method: str, url: str, payload: Optional[dict] = None):
        last_err = None
        for attempt in range(1, 4):
            try:
                if method == "GET":
                    resp = self.session.get(url, timeout=self.timeout)
                else:
                    resp = self.session.post(url, json=payload, timeout=self.timeout)
                time.sleep(self.delay)
                if resp.status_code == 200:
                    try:
                        return resp.json(), None
                    except ValueError:
                        return None, f"non-JSON response ({len(resp.content)} bytes)"
                last_err = f"HTTP {resp.status_code}"
                if resp.status_code in (400, 404, 403):
                    # Not retryable - the endpoint simply has no data for this project.
                    return None, last_err
            except requests.exceptions.ProxyError as exc:
                # A proxy that cannot tunnel here (or blocks the site) will never work
                # for this host; retry the same request directly instead of failing.
                self.disable_proxy(self._proxy_error_reason(exc))
                last_err = f"ProxyError: {self._proxy_error_reason(exc)}"
                continue
            except requests.RequestException as exc:  # pragma: no cover - network flake
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(1.2 * attempt)
        log.warning("Giving up on %s (%s)", url, last_err)
        return None, last_err

    def document_url(self, ref: str) -> str:
        """Public URL for a document reference: a VDMS UID or a `scan:` ref."""
        if ref.startswith(SCAN_PREFIX):
            root, _, name = ref[len(SCAN_PREFIX):].partition("/")
            return self.url(
                "scanned_file",
                root_file=base64.b64encode(root.encode("utf-8")).decode("ascii"),
                file_name=base64.b64encode(name.encode("utf-8")).decode("ascii"),
            )
        return self.url("download", uid=ref)

    def get_document(self, uid: str) -> Tuple[Optional[bytes], str, str]:
        """Download a document by its VDMS UID or `scan:` ref.

        Returns (bytes, content_type, filename).
        """
        url = self.document_url(uid)
        attempts = 2 if uid.startswith(SCAN_PREFIX) else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.exceptions.ProxyError as exc:
                self.disable_proxy(self._proxy_error_reason(exc))
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            time.sleep(self.delay)
            if resp.status_code == 500 and "No such file or directory" in resp.text[:500]:
                raise RuntimeError("missing on portal (HTTP 500: No such file or directory)")
            if resp.status_code == 200 or attempt == attempts:
                break
            time.sleep(1.5 * attempt)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        filename = ""
        disposition = resp.headers.get("Content-Disposition") or ""
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"')
        return resp.content, resp.headers.get("Content-Type", ""), filename

    def global_search(self, query: str, start_with: int = 0, data_size: int = 10000):
        return self.post_json(
            "project_reg/public/global-search",
            {"query": query, "startWith": start_with, "dataSize": data_size},
        )


def payload(response: Any) -> Any:
    """Unwrap the standard `{"status":..,"data":..}` envelope (some endpoints skip it)."""
    if isinstance(response, dict) and "data" in response and (
        "status" in response or "message" in response or "masssge" in response
    ):
        return response.get("data")
    return response

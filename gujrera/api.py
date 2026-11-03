"""
HTTP client for the public GujRERA API.

The GujRERA site (https://gujrera.gujarat.gov.in) is an Angular SPA. Everything it
renders on the "Project View" screen comes from plain public JSON endpoints under
`project_reg/`, `formone/`, `formtwo/`, `formthree/`, `quarter/`, `form_five/`,
`dashboard/`, `maplocation/`, `user_reg/` and `vdms/`.

No login, cookie or CSRF token is required for the endpoints used here.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

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
}


class GujreraClient:
    """Small, polite wrapper around the public GujRERA JSON + document endpoints."""

    def __init__(self, delay: float = 0.35, timeout: int = 120, retries: int = 4):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=retries,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
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
            except requests.RequestException as exc:  # pragma: no cover - network flake
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(1.2 * attempt)
        log.warning("Giving up on %s (%s)", url, last_err)
        return None, last_err

    def get_document(self, uid: str) -> Tuple[Optional[bytes], str, str]:
        """Download a document by its VDMS UID. Returns (bytes, content_type, filename)."""
        url = self.url("download", uid=uid)
        resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        time.sleep(self.delay)
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

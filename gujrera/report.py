"""
Markdown rendering for GujRERA project bundles.

The generated report mirrors the four tabs of
https://gujrera.gujarat.gov.in/#/project-preview
    1. Project Summary
    2. Project Profile
    3. Promoters
    4. Progress & RERA Actions
plus a document index (every attached file) and a method/source appendix.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .api import payload

NA = "—"


# ------------------------------------------------------------------------- formatting ---
def clean(value: Any) -> str:
    if value is None:
        return NA
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, (list, dict, tuple, set)):
        if not value:
            return NA
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= 160 else text[:157] + "…"
    text = str(value).strip()
    if text in ("", "null", "NA", "NIL", "- -", "-", "--", "null,null"):
        return NA
    return text


def inr(value: Any) -> str:
    """Indian digit grouping, e.g. 420630000 -> 42,06,30,000."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return NA
    if number == 0:
        return "0"
    negative = number < 0
    number = abs(number)
    whole = f"{int(round(number)):,}"
    # Convert 1,234,567 to 12,34,567
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


def crore(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return NA
    if number == 0:
        return NA
    return f"₹ {number / 1e7:,.2f} Cr"


def fmt_date(value: Any) -> str:
    if value in (None, "", "null"):
        return NA
    text = str(value).strip()
    if text.startswith(("Date Not", "0000")):
        return NA
    # ISO timestamps
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        year, month, day = match.groups()
        return f"{day}-{month}-{year}"
    return text


def num(value: Any, digits: int = 2) -> str:
    """Format a numeric string/number with at most `digits` decimals."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return NA
    text = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def _name(*parts: Any) -> str:
    bits = [str(p).strip() for p in parts if p and str(p).strip() not in ("", "null", "None", "NA")]
    return " ".join(bits) or NA


def md_escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rows = [list(r) for r in rows]
    if not rows:
        return f"*{NA} (no records)*\n"
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        cells = [md_escape(clean(c)) for c in row]
        cells += [""] * (len(headers) - len(cells))
        out.append("| " + " | ".join(cells[: len(headers)]) + " |")
    return "\n".join(out) + "\n"


def kv(pairs: Sequence[Sequence[Any]], left: str = "Field", right: str = "Value") -> str:
    return table([left, right], [[a, b] for a, b in pairs])


def table_compact(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    """Like `table`, but drops all-empty columns and rows that carry only a label."""
    rows = [list(r) for r in rows]
    if not rows:
        return f"*{NA} (no records)*\n"
    width = len(headers)
    keep_cols = [
        i for i in range(width)
        if any(clean(r[i] if i < len(r) else None) != NA for r in rows)
    ]
    if not keep_cols:
        return f"*{NA} (no records)*\n"
    value_cols = [i for i in keep_cols if i != 0]
    kept_rows = [
        r for r in rows
        if not value_cols or any(clean(r[i] if i < len(r) else None) != NA for i in value_cols)
    ]
    if not kept_rows:
        return f"*{NA} (no records)*\n"
    return table(
        [headers[i] for i in keep_cols],
        [[r[i] if i < len(r) else None for i in keep_cols] for r in kept_rows],
    )


def date_range(start: Any, end: Any) -> str:
    """Render `start → end`, collapsing to NA when neither side is published."""
    left, right = fmt_date(start), fmt_date(end)
    if left == NA and right == NA:
        return NA
    return f"{left} → {right}"


TECHNICAL_KEY = re.compile(
    r"(?:Id|ID|UId|Uid|uid)$|^id$|^(?:createdOn|createdBy|modifiedOn|modifiedBy|joinOn|separatedOn)$"
    r"|^hibernateLazyInitializer$"
)


def object_table(items: Sequence[dict], drop_empty_columns: bool = True,
                 drop_technical: bool = True) -> str:
    """Render a list of dicts as a table, dropping columns that are empty for all rows."""
    items = [i for i in items if isinstance(i, dict)]
    if not items:
        return f"*{NA} (no records)*\n"
    columns: List[str] = []
    for item in items:
        for key in item:
            if key not in columns:
                columns.append(key)
    if drop_technical:
        columns = [c for c in columns if not TECHNICAL_KEY.search(c)]
    if drop_empty_columns:
        columns = [
            c
            for c in columns
            if any(item.get(c) not in (None, "", [], {}, "null") for item in items)
        ]
    headers = [re.sub(r"(?<!^)(?=[A-Z])", " ", c).replace("_", " ").title() for c in columns]
    rows = [[item.get(c) for c in columns] for item in items]
    return table(headers, rows)


def pick_table(items: Sequence[Any], mapping: Sequence[Sequence[Any]]) -> str:
    """Render a curated column set.

    `mapping` is a sequence of `(header, key_or_callable)` pairs, so wide portal
    payloads can be shown readably instead of dumping every database column.
    """
    items = [i for i in items if isinstance(i, dict)]
    if not items:
        return f"*{NA} (no records)*\n"
    headers = [m[0] for m in mapping]
    rows = []
    for item in items:
        rows.append([m[1](item) if callable(m[1]) else item.get(m[1]) for m in mapping])
    return table_compact(headers, rows)


def h(level: int, text: str) -> str:
    return f"{'#' * level} {text}\n"


def section_list(values: Iterable[Any]) -> str:
    values = [clean(v) for v in values if clean(v) not in (NA, "")]
    if not values:
        return f"*{NA}*\n"
    return "\n".join(f"- {v}" for v in values) + "\n"


# --------------------------------------------------------------------------- extractors ---
def _d(bundle: dict, name: str) -> Any:
    return payload(bundle["raw"].get(name))


def _first_list(value: Any) -> List[dict]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def form_two_totals(form_two: Any) -> Dict[str, float]:
    """Sum Form 2-A (block costs) + Form 2-B (common infrastructure) estimates."""
    totals = {"estimated": 0.0, "incurred": 0.0, "balance": 0.0}
    if not isinstance(form_two, dict):
        return totals
    rows = _first_list(form_two.get("formTwoAList"))
    block_b = form_two.get("formTwoB")
    if isinstance(block_b, dict):
        rows = rows + [block_b]
    for row in rows:
        for key, target in (
            ("estimatedCost", "estimated"),
            ("costInncurred", "incurred"),
            ("balanceCompletionCost", "balance"),
        ):
            try:
                totals[target] += float(row.get(key) or 0)
            except (TypeError, ValueError):
                pass
    return totals


def project_cost(detail: dict, area: dict, form_two: Any) -> tuple:
    """Best available project cost plus a label describing where it came from."""
    declared = (detail or {}).get("totalProjectCost") or (area or {}).get("totcost")
    if declared:
        try:
            return float(declared), "declared on portal"
        except (TypeError, ValueError):
            pass
    estimated = form_two_totals(form_two)["estimated"]
    if estimated:
        return estimated, "Form 2-A + 2-B estimates"
    max_cost = (area or {}).get("maxcost")
    if max_cost:
        try:
            return float(max_cost), "highest declared unit price"
        except (TypeError, ValueError):
            pass
    return None, ""


F1A_STAGES = [
    ("Excavation", "excavation"),
    ("Plinth", "plinth"),
    ("Podium", "podium"),
    ("Stilt", "still"),
    ("Slab / RCC", "slab"),
    ("Internal Finishing", "internal"),
    ("Sanitary Fittings", "sanitary"),
    ("Staircase", "staircase"),
    ("External Development", "external"),
    ("Installation", "installation"),
    ("Common Area", "common"),
]


def _ratio(done: Any, total: Any) -> str:
    """Render a done/total pair, collapsing to a single number when no total exists."""
    total_text = num(total) if total not in (None, "", "null") else NA
    if total_text in (NA, "0"):
        return num(done) if done not in (None, "", "null") else NA
    return f"{num(done)} / {total_text}"


def _f1a_work(blocks: Sequence[dict]) -> str:
    """Flat table of the progress counters reported in Form 1-A."""
    return table_compact(
        ["Block", "Progress (%)", "Floors", "Lifts", "Units Booked", "Excavation", "Plinth",
         "Podiums", "Stilt Flooring", "RCC Slabs", "Internal", "Staircases", "Sanitary",
         "External", "Installation"],
        [[b.get("blockName"), num(b.get("blockProgress")), b.get("noOfFloors"), b.get("noOfLifts"),
          b.get("noOfUnitsBooked"), num(b.get("excavationWrokDone")),
          _ratio(b.get("plinthWorkDone"), b.get("totalNoOfPlinth")),
          _ratio(b.get("podiumsWorkDone"), b.get("totalNoOfPodiums")),
          num(b.get("stiltFloorWorkDone")),
          _ratio(b.get("slabsWorkDone"), b.get("totalNoOfSlabs")),
          num(b.get("internalDetailsWrkDone")), num(b.get("staircases")),
          num(b.get("sanitaryFittings")), num(b.get("externalDetail")),
          num(b.get("installationOfAll"))] for b in blocks],
    )


def _f1a_schedule(block: dict) -> str:
    """Planned vs actual dates for every construction stage of one block."""
    rows = [
        [label,
         fmt_date(block.get(key + "StartDate")),
         fmt_date(block.get(key + "EndDate")),
         fmt_date(block.get(key + "ActStartDate")),
         fmt_date(block.get(key + "ActEndDate"))]
        for label, key in F1A_STAGES
    ]
    return table(["Stage", "Planned Start", "Planned End", "Actual Start", "Actual End"], rows)


F1B_LABEL_FIXES = (("Useof", "Use Of"),)


def _pretty_key(key: str) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", " ", key).title()
    for bad, good in F1B_LABEL_FIXES:
        text = text.replace(bad, good)
    return text


def _f1b_matrix(form_b: dict) -> str:
    """Pivot Form 1-B's 190 flat columns into one row per common amenity."""
    prefixes = [k[: -len("StartActivityDate")] for k in form_b if k.endswith("StartActivityDate")]
    for extra in ("compoundWall", "plinthProtection", "waterTank", "otherRequirement"):
        if extra not in prefixes and any(k.startswith(extra) for k in form_b):
            prefixes.append(extra)
    rows = [
        [_pretty_key(prefix),
         form_b.get(prefix + "YesNo"),
         fmt_date(form_b.get(prefix + "StartActivityDate")),
         fmt_date(form_b.get(prefix + "EndActivityDate")),
         fmt_date(form_b.get(prefix + "StartActualDate")),
         fmt_date(form_b.get(prefix + "EndActualDate")),
         num(form_b.get(prefix + "WorkDone")),
         form_b.get(prefix + "Remarks")]
        for prefix in prefixes
    ]
    if not rows:
        return f"*{NA} (no amenity records)*\n"
    return table_compact(["Amenity / Facility", "Applicable", "Planned Start", "Planned End",
                          "Actual Start", "Actual End", "Work Done (%)", "Remarks"], rows)


def inventory_summary(form_three: Any) -> List[dict]:
    units = _first_list((form_three or {}).get("formThreeAList") if isinstance(form_three, dict) else None)
    buckets: Dict[tuple, Dict[str, Any]] = {}
    for unit in units:
        key = (clean(unit.get("usage")), clean(unit.get("blockName")))
        bucket = buckets.setdefault(key, {"total": 0, "booked": 0, "unbooked": 0, "area": 0.0})
        bucket["total"] += 1
        status = str(unit.get("status") or "").upper()
        if "UNBOOK" in status or "AVAILABLE" in status:
            bucket["unbooked"] += 1
        elif "BOOK" in status:
            bucket["booked"] += 1
        try:
            bucket["area"] += float(unit.get("carpetArea") or 0)
        except (TypeError, ValueError):
            pass
    rows = []
    for (usage, block), bucket in sorted(buckets.items()):
        rows.append(
            {
                "Unit Type": usage,
                "Block": block,
                "Total Units": bucket["total"],
                "Booked": bucket["booked"],
                "Un-booked": bucket["unbooked"],
                "Carpet Area (sq m)": round(bucket["area"], 2),
            }
        )
    return rows


# ----------------------------------------------------------------------------- sections ---
def build_header(bundle: dict) -> str:
    core = _d(bundle, "core") or {}
    target = bundle["target"]
    name = core.get("projectName") or target.get("name") or "Unknown project"
    reg_no = core.get("projRegNo") or target.get("regNo") or NA
    profile = _d(bundle, "profile") or {}
    detail = profile.get("projectDetail") or {}
    progress = _d(bundle, "progress_score") or {}

    lines = [
        f"# {name}",
        "",
        f"**GujRERA Registration No.:** `{reg_no}`  ",
        f"**Project ID:** {core.get('projRegId', target.get('pid', NA))}  ",
        f"**District / Taluka:** {clean(detail.get('distName'))} / {clean(detail.get('subDistName'))}  ",
        f"**Project Type:** {clean(core.get('projectType') or detail.get('projectType'))}  ",
        f"**Promoter:** {clean(core.get('promoterName'))} ({clean(core.get('promoterType'))})  ",
        f"**Registered On:** {fmt_date(core.get('approvedDate'))}  ",
        f"**Declared Completion:** {fmt_date(detail.get('completionDate'))}  ",
        f"**Construction Progress (as published):** {clean(progress.get('score'))}% "
        f"({clean(progress.get('progress'))})  ",
        f"**Source:** {bundle['source_url']}  ",
        f"**Scraped at:** {bundle['scraped_at']}  ",
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def build_summary(bundle: dict) -> str:
    core = _d(bundle, "core") or {}
    profile = _d(bundle, "profile") or {}
    detail = profile.get("projectDetail") or {}
    promoter = _d(bundle, "promoter") or {}
    collection = _d(bundle, "collection_bank") or {}
    area = (_first_list(_d(bundle, "area_price")) or [{}])[0]
    chart = _first_list(_d(bundle, "block_chart"))
    applications = _d(bundle, "applications") or []
    quarters = _first_list(_d(bundle, "quarters"))
    annual = _first_list(_d(bundle, "annual_compliance"))
    form_three = _d(bundle, "form_three") or {}
    form_two = _d(bundle, "form_two") or {}

    out = [h(2, "1. Project Summary")]

    cost_value, cost_source = project_cost(detail, area, form_two)
    cost_text = f"₹ {inr(cost_value)}" + (f" *({cost_source})*" if cost_source else "")
    price_range = (
        f"₹ {inr(area.get('mincost'))} – ₹ {inr(area.get('maxcost'))}"
        if area.get("mincost") or area.get("maxcost")
        else NA
    )

    # 1.1 basic details
    out.append(h(3, "1.1 Project Details"))
    address = " ".join(
        str(v) for v in (detail.get("projectAddress"), detail.get("projectAddress2")) if v
    )
    out.append(
        kv(
            [
                ("Project Name", core.get("projectName")),
                ("GUJRERA Reg. No.", core.get("projRegNo")),
                ("Acknowledgement No.", core.get("projectAckNo")),
                ("Project Address", address),
                ("Moje / Village", detail.get("moje")),
                ("Taluka", detail.get("subDistName")),
                ("District", detail.get("distName")),
                ("State", detail.get("stateName")),
                ("Pin Code", detail.get("pinCode")),
                ("Project Type", core.get("projectType") or detail.get("projectType")),
                ("About Property", detail.get("projectDesc")),
                ("Project Start Date", fmt_date(detail.get("startDate"))),
                ("Project End Date", fmt_date(detail.get("completionDate"))),
                ("Project Land Area (sq m)", detail.get("totAreaOfLandLayout")),
                ("Total Open Area (sq m)", detail.get("totOpenArea")),
                ("Total Covered Area (sq m)", detail.get("totCoverdArea")),
                ("Total Carpet Area (sq m)", detail.get("totCarpetArea")),
                ("Carpet Area Range (sq m)", f"{clean(area.get('mincararea'))} – {clean(area.get('maxcararea'))}"),
                ("Total Units", area.get("totunit")),
                ("Plan Passing Authority", detail.get("approvingAuthority")),
                ("Redevelopment Project", detail.get("redevelopmentFlag") or "NIL"),
                ("Affordable Housing", detail.get("affordableHousingFlag") or "NIL"),
                ("Survey / Block / TP Details",
                 f"RS {clean(detail.get('revenueSurveyNo'))} · Block {clean(detail.get('blockNo'))} · "
                 f"TP {clean(detail.get('tPNo'))} ({clean(detail.get('tPName'))}) · FP {clean(detail.get('finalPlotNo'))}"),
                ("Project Status", detail.get("projectStatus") or "New"),
                ("Project Cost (₹)", cost_text),
                ("Unit Price Range (₹)", price_range),
            ]
        )
    )

    # 1.2 inventory
    out.append(h(3, "1.2 Unit Type / Inventory Details"))
    out.append(table(["Unit Type", "Block", "Total Units", "Booked", "Un-booked", "Carpet Area (sq m)"],
                     [[r["Unit Type"], r["Block"], r["Total Units"], r["Booked"], r["Un-booked"], r["Carpet Area (sq m)"]]
                      for r in inventory_summary(form_three)]))
    if chart:
        out.append("**Block-wise booking status (portal chart data)**\n")
        out.append(table(["Block", "Booked", "Un-booked", "Booked %"],
                         [[c.get("bname"), c.get("booked"), c.get("unbooked"), num(c.get("perc"))] for c in chart]))

    # 1.3 promoter snapshot
    out.append(h(3, "1.3 Promoter Details (summary)"))
    out.append(
        kv(
            [
                ("Promoter Name", core.get("promoterName") or promoter.get("promoterName")),
                ("Promoter Type", core.get("promoterType") or promoter.get("promoterType")),
                ("PAN", promoter.get("panNo")),
                ("Registration No.", promoter.get("companyRegistrationNumber")),
                ("Office Address",
                 " ".join(str(v) for v in (promoter.get("address"), promoter.get("address2"),
                                           promoter.get("talukaName"), promoter.get("districtName"),
                                           promoter.get("stateName"), promoter.get("pinCode")) if v)),
                ("E-mail", core.get("promoterEmailId") or promoter.get("emailId")),
                ("Mobile", core.get("promoterMobileNo") or promoter.get("mobileNo")),
                ("Partners / Associates", str(len(promoter.get("assosiateList") or []))),
            ]
        )
    )

    # 1.4 compliance
    out.append(h(3, "1.4 Compliance"))
    total_q = len(quarters)
    submitted_q = sum(1 for q in quarters if str(q.get("status") or "").upper() in ("SUBMITTED", "APPROVED", "ACCEPTED"))
    defaulted_q = sum(1 for q in quarters if q.get("isDefault") in (True, "Yes", "Y", 1))
    submitted_a = sum(1 for a in annual if str(a.get("projectFinancialYearStatus") or "").upper() in ("SUBMITTED", "APPROVED", "ACCEPTED"))
    out.append(
        kv(
            [
                ("Total Quarterly Compliance Required", total_q),
                ("Total Complied Quarters", submitted_q),
                ("Total Quarterly Compliance Defaulted", defaulted_q),
                ("Total Annual Compliance Required", len(annual)),
                ("Total Complied Annual Compliance", submitted_a),
                ("Total Annual Compliance Defaulted", max(len(annual) - submitted_a, 0)),
            ]
        )
    )

    # 1.5 bank
    out.append(h(3, "1.5 Collection Bank Details (Section 4(2)(l)(D))"))
    if isinstance(collection, dict) and collection.get("bankName"):
        out.append(
            kv(
                [
                    ("Bank", collection.get("bankName")),
                    ("Branch", collection.get("branchName")),
                    ("Account Name", collection.get("accountName")),
                    ("Account Number", collection.get("accountNumber")),
                    ("IFSC", collection.get("ifscCode")),
                    ("District / Taluka", f"{clean(collection.get('districtName'))} / {clean(collection.get('talukaName'))}"),
                    ("Pincode", collection.get("pincode")),
                ]
            )
        )
    else:
        out.append("*No record found for collection bank details.*\n")

    # 1.6 construction booking status
    out.append(h(3, "1.6 Construction Booking Status"))
    blocks_progress = _first_list(_d(bundle, "block_progress"))
    if blocks_progress:
        rows = []
        for block in blocks_progress:
            name = block.get("blk_name")
            chart_row = next((c for c in chart if str(c.get("bname")) == str(name)), {})
            rows.append([
                name,
                num(block.get("block_progress")),
                num(block.get("form_b_prog_report")),
                chart_row.get("unbooked"),
                date_range(block.get("blk_dev_start_date"), block.get("blk_dev_end_date")),
            ])
        out.append(table(["Block", "Block Progress (%)", "Form-B Report (%)", "Un-booked Units", "Development Period"], rows))
    else:
        out.append(f"*{NA} (no construction progress records published)*\n")

    # 1.7 status / applications
    out.append(h(3, "1.7 Status & RERA Applications"))
    if applications:
        rows = []
        for app in applications:
            rows.append([
                app.get("applicationType"),
                app.get("applicationNumber"),
                fmt_date(app.get("submissionDate")),
                fmt_date(app.get("approvalDate")),
                app.get("promoterStatus"),
                app.get("caStatus"),
                app.get("finalStatus"),
                app.get("complaintNumber"),
                app.get("challanNumber"),
                app.get("challanAmount"),
            ])
        out.append(table(["Type", "Application No.", "Submitted", "Approved", "Promoter", "CA", "Final", "Complaint No.", "Challan No.", "Challan ₹"], rows))
    else:
        out.append(f"*{NA} (no applications found)*\n")

    # 1.8 form 2 cost sheet
    if isinstance(form_two, dict):
        cost_rows = _first_list(form_two.get("formTwoAList"))
        totals = form_two_totals(form_two)
        if cost_rows:
            out.append(h(3, "1.8 Estimated Cost & Work Done (Form 2)"))
            rows = [[c.get("blockName"), inr(c.get("estimatedCost")), inr(c.get("costInncurred")),
                     num(c.get("workDonePer")), inr(c.get("balanceCompletionCost")),
                     fmt_date(c.get("inncurredOn"))] for c in cost_rows]
            common = form_two.get("formTwoB")
            if isinstance(common, dict) and common:
                rows.append(["Form 2-B — common infrastructure", inr(common.get("estimatedCost")),
                             inr(common.get("costInncurred")), num(common.get("workDonePer")),
                             inr(common.get("balanceCompletionCost")), fmt_date(common.get("inncurredOn"))])
            overall_work = (totals["incurred"] / totals["estimated"] * 100) if totals["estimated"] else None
            rows.append(["**Total**", f"**{inr(totals['estimated'])}**", f"**{inr(totals['incurred'])}**",
                         f"**{num(overall_work)}**", f"**{inr(totals['balance'])}**", NA])
            out.append(table(
                ["Block", "Estimated Cost (₹)", "Cost Incurred (₹)", "Work Done (%)", "Balance Cost (₹)", "As On"],
                rows))

    out.append("\n---\n")
    return "\n".join(out)


def build_profile(bundle: dict) -> str:
    profile = _d(bundle, "profile") or {}
    detail = profile.get("projectDetail") or {}
    dev = (_first_list(profile.get("dev")) or [{}])[0]
    external = dev.get("externalDev") or {}
    internal = _first_list(dev.get("internalDev"))
    blocks = _first_list(_d(bundle, "blocks"))
    location = _d(bundle, "location") or {}
    form_one = _d(bundle, "form_one") or {}
    form_one_data = form_one.get("data") if isinstance(form_one, dict) and "data" in form_one else form_one
    if not isinstance(form_one_data, dict):
        form_one_data = {}
    form_three = _d(bundle, "form_three") or {}
    joint_dev = _d(bundle, "joint_development")

    out = [h(2, "2. Project Profile")]

    out.append(h(3, "2.1 Location & Land Details"))
    coordinates = location.get("coordinates") or []
    coords_text = "; ".join(f"{c.get('lat')}, {c.get('lang')}" for c in coordinates[:8] if isinstance(c, dict))
    out.append(
        kv(
            [
                ("Address (as registered)",
                 " ".join(str(v) for v in (detail.get("projectAddress"), detail.get("projectAddress2")) if v)),
                ("Moje", detail.get("moje")),
                ("Revenue Survey No.", detail.get("revenueSurveyNo")),
                ("City Survey No.", detail.get("citySurveyNo")),
                ("Block No.", detail.get("blockNo")),
                ("TP / Scheme", f"{clean(detail.get('tPNo'))} / {clean(detail.get('tPName'))}"),
                ("Final Plot No.", detail.get("finalPlotNo")),
                ("Approving Authority", detail.get("approvingAuthority")),
                ("Land Area for Project (sq m)", detail.get("totLandAreaForProjectUnderReg")),
                ("Total Land Area of Layout (sq m)", detail.get("totAreaOfLandLayout")),
                ("Total Carpet Area for Sale (sq m)", detail.get("totalCarpetAreaForSellable")),
                ("Covered Parking (nos / sq m)", f"{clean(detail.get('coveredParking'))} / {clean(detail.get('coveredParkingArea'))}"),
                ("Parking for Sale (nos)", detail.get("noOfParkinfForSale")),
                ("Open Parking Area (sq m)", detail.get("openParkingArea")),
                ("Map Coordinates", coords_text or NA),
                ("Water Level / Soil Report Date",
                 f"{clean(detail.get('waterLevel'))} / {fmt_date(detail.get('dateOfSoilReport'))}"),
                ("Joint Development Agreement", "Available" if joint_dev else "Not published"),
            ]
        )
    )

    out.append(h(3, "2.2 Development Details"))
    out.append(
        kv(
            [
                ("Road System Developed By", external.get("roadSysetmDevBy")),
                ("Water Supply By", external.get("waterSupplyBy")),
                ("Sewage & Drainage By", external.get("sewegeAndDrainageSystemDevBy")),
                ("Electricity & Transformer By", external.get("electricityAndTrasfomerSupply")),
                ("Solid Waste Management By", external.get("solidWasteSupplyBy")),
                ("Drinking Water Requirement (litres)", dev.get("totalDrinkWaterReq")),
                ("Water Proposed", dev.get("totalDrinkWaterProps")),
                ("Sewage Quantity (litres)", dev.get("totalQuantySew")),
                ("Sewage Treatment Source", dev.get("sewPrjTreatPrjSource")),
                ("Solid Waste Source", dev.get("sewSolidWasteSource")),
                ("Swimming Pool Capacity", dev.get("sewSwimCapLiter")),
                ("Water Supply Flag", dev.get("waterSplyFlag")),
                ("Sewage Disposal Flag", dev.get("sewDisposalFlag")),
            ]
        )
    )
    if internal:
        out.append("**Inventory declared in development plan**\n")
        out.append(table(
            ["Type of Inventory", "No. of Units", "Booked", "Carpet Area (sq m)", "Exclusive Area (sq m)", "Open Terrace (sq m)"],
            [[i.get("typeOfInventory"), i.get("noOfInventory"), i.get("noOfInventoryBooked"),
              i.get("carpetArea"), i.get("areaOfExclusive"), i.get("areaOfExclusiveOpenTerrace")]
             for i in internal]))

    out.append(h(3, "2.3 Professionals Associated"))
    architect = _first_list(profile.get("acrchlist"))
    engineer = _first_list(profile.get("englist"))
    ca = _first_list(profile.get("calist"))
    contractor = _first_list(profile.get("contr"))
    agent = _first_list(profile.get("agentlist"))
    out.append("**Architect(s)**\n")
    out.append(pick_table(architect, [
        ("Name", "name"), ("COA Reg. No.", "architectCoA"), ("Local Authority Licence No.", "localAuthorityLicenceNo"),
        ("PAN", "panNo"), ("Mobile", "mobileNo"), ("E-mail", "emailId"),
        ("Key Projects Completed", "noOfKeyProjectCompleted"), ("Experience (years)", "profExperience"),
        ("Address", "address"), ("Status", "status"),
    ]))
    out.append("**Structural Engineer(s)**\n")
    out.append(pick_table(engineer, [
        ("Name", "name"), ("Licence No.", "licenceNO"), ("PAN", "structuEnggPanNo"),
        ("Mobile", "mobileNo"), ("E-mail", "engemailId"),
        ("Key Projects Completed", "noOfKeyProjectsCompleted"), ("Experience (years)", "profExperience"),
        ("Address", "addressLine"), ("Status", "status"),
    ]))
    out.append("**Chartered Accountant(s)**\n")
    out.append(object_table(ca))
    out.append("**Contractor(s)**\n")
    out.append(object_table(contractor))
    out.append("**Real Estate Agent(s)**\n")
    out.append(object_table(agent))

    out.append(h(3, "2.4 Blocks / Buildings"))
    if blocks:
        out.append(table(
            ["Block", "Work Start", "Work End", "Height (m)", "FSI (sq m)", "Built-up Area (sq m)",
             "Commencement Certificate No.", "Certificate Date", "Issuing Authority"],
            [[b.get("blockName"), fmt_date(b.get("devStartDate")), fmt_date(b.get("devEndDate")),
              b.get("height"), b.get("fsi"), b.get("builtupArea"), b.get("commencementCertiNo"),
              fmt_date(b.get("dateIssuanceDoc")), b.get("issuerName")] for b in blocks]))
    else:
        out.append(f"*{NA} (no block records published)*\n")

    out.append(h(3, "2.5 Form 1 — Architect's Certification"))
    if form_one_data:
        out.append(
            kv(
                [
                    ("Form 1 ID", form_one_data.get("formOneId")),
                    ("Form Status", form_one_data.get("formOneStatus")),
                    ("Architect", form_one_data.get("architectName")),
                    ("COA Registration No.", form_one_data.get("coaRegNo")),
                    ("Valid Till", fmt_date(form_one_data.get("caoRegValidTill"))),
                    ("Physical Site Visit Date", fmt_date(form_one_data.get("physicalVisitDate"))),
                    ("Progress Reported (%)", form_one_data.get("progressReport")),
                    ("Time Lapsed", form_one_data.get("timeLapsed")),
                    ("Project Start", fmt_date(form_one_data.get("projectStartDate"))),
                    ("Project End", fmt_date(form_one_data.get("projectEndDate"))),
                    ("Remark", form_one_data.get("remark")),
                    ("Submitted On", fmt_date(form_one_data.get("submitedOn"))),
                ]
            )
        )
        form_a = _first_list(form_one_data.get("formOneAList"))
        if form_a:
            out.append("**Form 1-A — Work Completed per Block**\n")
            out.append(_f1a_work(form_a))
            for block in form_a:
                out.append(h(4, f"Form 1-A — Construction Schedule: Block {clean(block.get('blockName'))}"))
                out.append(_f1a_schedule(block))
            photos = sum(len(_first_list(b.get("photoDocList"))) for b in form_a)
            if photos:
                out.append(f"*{photos} site progress photograph{'s' if photos != 1 else ''} attached to "
                           f"Form 1-A — listed under Documents.*\n")
        form_b = form_one_data.get("formOneB")
        if isinstance(form_b, dict) and form_b:
            out.append("**Form 1-B — Common Amenities & Infrastructure**\n")
            overview = [
                ("Form B ID", form_b.get("formBId")),
                ("Status", form_b.get("status")),
                ("Compliance Valid Till", fmt_date(form_b.get("regValidTill"))),
                ("Physical Site Visit Date", fmt_date(form_b.get("physicalVisitDate"))),
                ("Form-B Period", date_range(form_b.get("formBStartDate"), form_b.get("formBEndDate"))),
                ("Form-B Reported Progress (%)", num(form_b.get("formBProgRepot"))),
                ("Other Details", form_b.get("oneBOtherDetails")),
            ]
            overview = [(k, v) for k, v in overview if clean(v) != NA]
            if overview:
                out.append(kv(overview))
            out.append(_f1b_matrix(form_b))
    else:
        out.append(f"*{NA}*\n")

    out.append(h(3, "2.6 Form 3 — Inventory Snapshot"))
    units = _first_list((form_three or {}).get("formThreeAList"))
    if units:
        preview = units[:25]
        out.append(table(
            ["Block", "Flat / Unit No.", "Usage", "Carpet Area (sq m)", "Balcony (sq m)", "Status",
             "Consideration (₹)", "Received (₹)", "Balance (₹)", "Encumbrance"],
            [[u.get("blockName"), u.get("flatNo"), u.get("usage"), u.get("carpetArea"), u.get("areaofExBalcony"),
              u.get("status"), inr(u.get("unitConsideration")), inr(u.get("receivedAmount")),
              inr(u.get("balanceAmount")), u.get("encumbranceStatus")] for u in preview]))
        if len(units) > len(preview):
            out.append(f"*Showing first {len(preview)} of {len(units)} units — full list in "
                       f"`raw/form_three.json`.*\n")
    else:
        out.append(f"*{NA} (no Form 3 inventory published)*\n")

    out.append("\n---\n")
    return "\n".join(out)


def build_promoters(bundle: dict) -> str:
    promoter = _d(bundle, "promoter") or {}
    previous = _d(bundle, "previous_projects") or {}
    banks = _d(bundle, "project_banks")

    out = [h(2, "3. Promoters")]

    out.append(h(3, "3.1 Promoter Profile"))
    out.append(
        kv(
            [
                ("Promoter Name", promoter.get("promoterName")),
                ("Promoter Type", promoter.get("promoterType")),
                ("PAN", promoter.get("panNo")),
                ("Company / Firm Registration No.", promoter.get("companyRegistrationNumber")),
                ("Date of Incorporation", fmt_date(promoter.get("dateOfIncorporation"))),
                ("Status", promoter.get("status")),
                ("Registered Address",
                 " ".join(str(v) for v in (promoter.get("address"), promoter.get("address2")) if v)),
                ("Taluka / District / State",
                 f"{clean(promoter.get('talukaName'))} / {clean(promoter.get('districtName'))} / {clean(promoter.get('stateName'))}"),
                ("Pin Code", promoter.get("pinCode")),
                ("E-mail", promoter.get("emailId")),
                ("Mobile", promoter.get("mobileNo")),
                ("Telephone", promoter.get("telephoneNo")),
                ("Website", promoter.get("entities_websiteUrl")),
                ("Registered On", fmt_date(promoter.get("registeredOn"))),
            ]
        )
    )

    group_fields = [
        ("Developer Group", promoter.get("entities_developerGroupName")),
        ("Promoter Enterprise", promoter.get("entities_promoterEnterprise")),
        ("Total Projects", promoter.get("entities_totalProjects")),
        ("Projects Completed", promoter.get("entities_noOfProjectsCompleted")),
        ("Ongoing Projects", promoter.get("entities_ongoingProjects")),
        ("Total Area Constructed", promoter.get("entities_totalAreaConstructed")),
        ("Experience in Gujarat", promoter.get("entities_experienceInState")),
        ("Experience in Other States", promoter.get("entities_experienceInOtherState")),
        ("Group History", promoter.get("entities_groupHistory")),
    ]
    if any(v for _, v in group_fields):
        out.append(h(3, "3.2 Developer Group / Track Record"))
        out.append(kv(group_fields))

    associates = _first_list(promoter.get("assosiateList"))
    out.append(h(3, "3.3 Partners / Associates"))
    out.append(pick_table(associates, [
        ("Name", lambda a: _name(a.get("associateFirstName"), a.get("associateMiddleName"),
                                a.get("associateLastName"))),
        ("PAN", "associatePan"),
        ("Mobile", "assocaiteMobileNumber"),
        ("E-mail", "assocaiteEmailId"),
        ("Address", lambda a: _name(a.get("associateAddress"), a.get("associateAddress2"))),
        ("Taluka", "assocaiteTalukaName"),
        ("District", "assocaiteDistrictName"),
        ("State", "assocaiteStateName"),
        ("Pin", "assocaitePinCode"),
        ("Status", "status"),
    ]))
    if associates:
        out.append(f"*{len(associates)} associate(s) / partner(s) registered against the promoter.*\n")

    signatories = _first_list(promoter.get("authorizedSignatoryList"))
    out.append(h(3, "3.4 Authorised Signatory"))
    out.append(pick_table(signatories, [
        ("Name", lambda a: _name(a.get("authsignFirstName"), a.get("authsignMiddleName"),
                                a.get("authsignLastName"))),
        ("PAN", "authsignPan"),
        ("Mobile", "authsignMobileNumber"),
        ("E-mail", "authsignEmailId"),
        ("Address", lambda a: _name(a.get("authsignAddress"), a.get("authsignAddress2"))),
        ("Taluka", "authsignTalukaName"),
        ("District", "authsignDistrictName"),
        ("State", "authsignStateName"),
        ("Pin", "authsignPinCode"),
        ("Status", "status"),
    ]))

    out.append(h(3, "3.5 Previous / Other Projects by the Promoter"))
    gujrera_projects = _first_list((previous or {}).get("gujrera"))
    other_projects = _first_list((previous or {}).get("pervlist"))
    combined = gujrera_projects + other_projects
    if combined:
        out.append(table(
            ["Project", "Registration No.", "Type", "Status", "Promoter", "Start", "End", "District", "Taluka"],
            [[p.get("projectName"), p.get("registrationNo"), p.get("projectType"), p.get("projectStatus") or p.get("status"),
              p.get("promotorName"), fmt_date(p.get("projectStartDate")), fmt_date(p.get("projectEndDate")),
              p.get("distName"), p.get("talukaName")] for p in combined]))
    else:
        out.append("*No other projects published against this promoter.*\n")

    if banks:
        out.append(h(3, "3.6 Project Bank Accounts"))
        out.append(pick_table(_first_list(banks) if isinstance(banks, list) else [banks], [
            ("Bank", "bankName"),
            ("Branch", "branchName"),
            ("Account Name", "accountName"),
            ("Account No.", "accountNumber"),
            ("IFSC", "ifscCode"),
            ("District / Taluka", lambda b: f"{clean(b.get('districtName'))} / {clean(b.get('talukaName'))}"),
            ("Pincode", "pincode"),
            ("Opening Balance (₹)", lambda b: inr(b.get("openingBal"))),
            ("Closing Balance (₹)", lambda b: inr(b.get("closingBal"))),
        ]))

    out.append("\n---\n")
    return "\n".join(out)


def build_progress(bundle: dict) -> str:
    score = _d(bundle, "progress_score") or {}
    qpr = _first_list(_d(bundle, "qpr_progress"))
    block_progress = _first_list(_d(bundle, "block_progress"))
    quarters = _first_list(_d(bundle, "quarters"))
    annual = _first_list(_d(bundle, "annual_compliance"))
    applications = _d(bundle, "applications") or []
    qe = _d(bundle, "qe_enquiry")
    form_eight = _d(bundle, "form_eight")
    form_three_loan = _d(bundle, "form_three_loan")

    out = [h(2, "4. Progress & RERA Actions")]

    out.append(h(3, "4.1 Construction Progress Score"))
    if isinstance(score, dict) and score:
        out.append(
            kv(
                [
                    ("Overall Progress (%)", score.get("score")),
                    ("Architect Reported Progress (%)", score.get("archScore")),
                    ("Engineer Reported Progress (%)", score.get("enggScore")),
                    ("Time Lapsed (%)", score.get("timeLaps")),
                    ("RERA Assessment", score.get("progress")),
                ]
            )
        )
    else:
        out.append(f"*{NA} (no progress score published)*\n")

    out.append(h(3, "4.2 Quarterly Progress Reported by Promoter"))
    out.append(table(["Quarter", "Reported Progress (%)"],
                     [[q.get("qtr_name"), num(q.get("project_progress"))] for q in qpr]))

    out.append(h(3, "4.3 Block-wise Construction Progress"))
    out.append(table(
        ["Block", "Development Start", "Development End", "Block Progress (%)", "Form-B Report (%)", "Form-B Period"],
        [[b.get("blk_name"), fmt_date(b.get("blk_dev_start_date")), fmt_date(b.get("blk_dev_end_date")),
          num(b.get("block_progress")), num(b.get("form_b_prog_report")),
          date_range(b.get("form_b_start_date"), b.get("form_b_end_date"))] for b in block_progress]))

    out.append(h(3, "4.4 Quarterly Compliance (QPR) Timeline"))
    out.append(table(
        ["Quarter", "Period", "Due", "Extended Till", "Submitted On", "Status", "Default", "Penalty (₹)", "Enquiry", "Authority Remark"],
        [[q.get("quarterName"), f"{clean(q.get('startDateStr'))} – {clean(q.get('endDateStr'))}",
          fmt_date(q.get("endDate")), fmt_date(q.get("extendedDate")), fmt_date(q.get("submittedOn")),
          q.get("status"), q.get("isDefault"), inr(q.get("penaltyAmt")), q.get("enquiryStatus"), q.get("authRemarks")]
         for q in quarters]))

    out.append(h(3, "4.5 Annual Compliance (Form 5)"))
    out.append(table(
        ["Financial Year", "Status", "Submitted On", "Document"],
        [[a.get("projectFinancialYear"), a.get("projectFinancialYearStatus"), fmt_date(a.get("submittedOn")),
          "Form 5 PDF" if a.get("pdfUid") else NA] for a in annual]))

    out.append(h(3, "4.6 RERA Applications, Complaints & Penalties"))
    if applications:
        out.append(table(
            ["Type", "Application No.", "Submitted", "Approved", "Complaint No.", "Complaint Status",
             "Challan No.", "Challan ₹", "Challan Status", "Judgment No.", "Final Status"],
            [[a.get("applicationType"), a.get("applicationNumber"), fmt_date(a.get("submissionDate")),
              fmt_date(a.get("approvalDate")), a.get("complaintNumber"), a.get("complaintStatus"),
              a.get("challanNumber"), a.get("challanAmount"), a.get("challanStatus"),
              a.get("judgmentNumber"), a.get("finalStatus")] for a in applications]))
    else:
        out.append(f"*{NA} (no applications / complaints / penalties recorded)*\n")

    out.append(h(3, "4.7 Enquiry & Escalation Flags"))
    out.append(
        kv(
            [
                ("Quarterly Enquiry (QE) Active", (qe or {}).get("data") if isinstance(qe, dict) else qe),
                ("Form 8 (Annual Report) Document", "Available" if form_eight else NA),
                ("Form 3 Loan Details", "Published" if form_three_loan else "Not published"),
            ]
        )
    )

    out.append("\n---\n")
    return "\n".join(out)


def build_documents(bundle: dict) -> str:
    documents = bundle.get("documents") or []
    out = [h(2, "5. Documents Downloaded")]
    ok = [d for d in documents if d.get("status") == "ok"]
    failed = [d for d in documents if d.get("status") != "ok"]
    out.append(f"**{len(ok)} file(s) downloaded**, {len(failed)} unavailable. "
               f"Files are stored in `files/` next to this report.\n")
    out.append(table(
        ["#", "Document", "File", "Type", "Size (KB)"],
        [[i + 1, d["label"], f"[{d['file']}](files/{d['file'].replace(' ', '%20')})" if d.get("file") else NA,
          d.get("content_type") or NA, round((d.get("bytes") or 0) / 1024, 1)]
         for i, d in enumerate(ok)]))
    if failed:
        out.append("\n**Listed on the portal but not downloadable**\n")
        out.append(table(["#", "Document", "UID", "Status"],
                         [[i + 1, d["label"], d["uid"][:24] + "…", d["status"]] for i, d in enumerate(failed)]))
    out.append("\n---\n")
    return "\n".join(out)


def build_appendix(bundle: dict) -> str:
    raw = bundle["raw"]
    errors = bundle.get("errors") or {}
    out = [h(2, "6. Source & Method Appendix")]
    out.append(
        "Data was collected from the public GujRERA endpoints that power "
        "`#/project-preview`. No authentication, CAPTCHA or bulk-scraping bypass was used — "
        "each request is the same one a normal visitor's browser makes.\n"
    )
    out.append(table(["Endpoint", "Path", "Result"],
                     [[name, _path_for(name, bundle), _result_for(name, bundle)]
                      for name in sorted(raw.keys())]))
    out.append("\nRaw JSON responses are stored in `raw/` and the document manifest in `documents_manifest.json`.\n")
    return "\n".join(out)


def _result_for(name: str, bundle: dict) -> str:
    if bundle["raw"].get(name) is not None:
        return "ok"
    error = (bundle.get("errors") or {}).get(name)
    if not error:
        return "empty"
    if "RetryError" in error or "Max retries" in error or "5xx" in error:
        return "portal returned HTTP 5xx"
    return error if len(error) <= 60 else error[:57] + "…"


def _path_for(name: str, bundle: dict) -> str:
    from .api import ENDPOINTS

    template = ENDPOINTS.get(name, "")
    values = dict(bundle.get("params") or {})
    try:
        return "`" + template.format(**values) + "`"
    except (KeyError, IndexError):
        return "`" + template + "`"


def build_project_markdown(bundle: dict) -> str:
    parts = [
        build_header(bundle),
        build_summary(bundle),
        build_profile(bundle),
        build_promoters(bundle),
        build_progress(bundle),
        build_documents(bundle),
        build_appendix(bundle),
    ]
    return "\n".join(parts)


def build_master_markdown(bundles: List[dict]) -> str:
    today = _dt.date.today().strftime("%d-%m-%Y")
    lines = [
        "# GujRERA — 10 Ahmedabad Construction Sites",
        "",
        f"**Compiled:** {today}  ",
        f"**Projects covered:** {len(bundles)}  ",
        "**Source:** https://gujrera.gujarat.gov.in/#/project-preview (public data, no login)  ",
        "",
        "Each section below is the complete record published on the GujRERA project page — "
        "Project Summary, Project Profile, Promoters and Progress & RERA Actions — plus an index "
        "of every attached document, all of which were downloaded next to this report.",
        "",
        "## Index",
        "",
        "| # | Project | GujRERA Reg. No. | Taluka | Type | Declared End | Progress | Report |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for index, bundle in enumerate(bundles, start=1):
        summary = bundle.get("summary", {})
        lines.append(
            "| {i} | {name} | `{reg}` | {taluka} | {ptype} | {end} | {prog} | [open]({folder}/{md}) |".format(
                i=index,
                name=md_escape(summary.get("name") or "Unknown"),
                reg=summary.get("regNo") or NA,
                taluka=md_escape(summary.get("taluka") or NA),
                ptype=md_escape(summary.get("type") or NA),
                end=summary.get("endDate") or NA,
                prog=f"{num(summary.get('progress'))}%" if summary.get("progress") is not None else NA,
                folder=bundle["folder"],
                md=bundle["markdown_name"],
            )
        )
    lines.append("")
    lines.append("## Snapshot Comparison")
    lines.append("")
    lines.append("| # | Project | Est. Project Cost (₹) | Units | Booked | Un-booked | QPR Filed | Docs |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for index, bundle in enumerate(bundles, start=1):
        summary = bundle.get("summary", {})
        lines.append(
            f"| {index} | {md_escape(summary.get('name') or '')} | {inr(summary.get('cost'))} | "
            f"{clean(summary.get('units'))} | {clean(summary.get('booked'))} | {clean(summary.get('unbooked'))} | "
            f"{clean(summary.get('qpr_filed'))} | {summary.get('docs_ok', 0)} |"
        )
    lines.append("")
    lines.append("---")
    lines.append("")

    for index, bundle in enumerate(bundles, start=1):
        content = build_project_markdown(bundle)
        lines.append(f'<a id="project-{index}"></a>')
        lines.append("")
        lines.append(demote_headings(content))
        lines.append("\n---\n")
    return "\n".join(lines)


def demote_headings(markdown: str) -> str:
    """Shift every heading one level down so project reports nest under the master doc."""
    return re.sub(r"(?m)^(#+ )", r"#\1", markdown)


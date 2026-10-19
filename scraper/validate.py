"""Data-integrity rules, applied when the site JSON is built.

Every comparison shown on the site goes through `compare()`; it returns a percentage only when
both figures are the same quantity, and None ("N/A" on the site) otherwise. All findings go to
data/validation.json so they can be reviewed; the site itself only ever sees the % or N/A.

Rules
  basis       every figure says whether it is BE, RE or actuals; comparisons are actuals vs RE only
  fy          every state figure carries its FY, and it must be the site's FY
  definition  numerator and denominator must use the same definition (prs.EXP_STD, prs.OWN_TAX, ...)
  heads       a derived actual's BE (same formula on the BE column of the same CAG table) must equal
              the BE that PRS publishes for that definition; if they differ, the two sources group
              accounting heads differently and the comparison is meaningless
  ratio       actual / RE outside RATIO_RANGE is treated as an error until someone looks at it
  derived     a calculated figure keeps its formula, inputs and source URL
  source      seed figures must still equal the PRS rows they were copied from (never corrected here)
  missing     pilot states without an RE or actual are listed
  disbursed   a benefits-disbursed figure needs basis 'actuals', a source URL, reporting period and
              as-of date; otherwise it is reported and not shown
  schemes     a scheme amount (`announced`) needs status verified (with a government source) or secondary;
              a 'reported' scheme carries no amount; funding type and news-report types are from the known set
"""
from __future__ import annotations

from .prs import EXP_INCL_DEBT, EXP_STD, OWN_TAX, SW_NUTRITION, SW_NUTRITION_ROW

RATIO_RANGE = (0.60, 1.20)      # full-year actual / revised estimate
HEADS_TOL = (2.0, 0.001)        # ₹ crore absolute, or 0.1 % – PRS rounds to whole crores
PRS_ROW = {EXP_STD: "Net Expenditure (E)", EXP_INCL_DEBT: "Total Expenditure", OWN_TAX: "State's Own Tax",
           SW_NUTRITION: SW_NUTRITION_ROW}
FIGURES = ("budget", "expenditure", "tax", "beneficiary_announced", "budget_actual", "tax_actual")


def _issue(issues, code, field, level, detail):
    issues.append({"state": code, "field": field, "level": level, "detail": detail})


DISBURSEMENT_FIELDS = ("url", "period", "as_of")


def _disb_src(fig: dict | None) -> dict:
    """A disbursement's provenance: a scheme's `despatched_source`, or a state figure's own fields."""
    if not isinstance(fig, dict):
        return {}
    return {"url": (fig.get("source") or {}).get("url", fig.get("url")), "period": fig.get("period"),
            "as_of": fig.get("as_of"), "basis": fig.get("basis")}


def disbursement_ok(fig: dict | None) -> bool:
    """Benefits disbursed are shown only with a source URL, reporting period and as-of date, and only
    as actual transfers – never a budget allocation."""
    src = _disb_src(fig)
    return bool(src) and all(src.get(k) for k in DISBURSEMENT_FIELDS) and src.get("basis") in (None, "actuals")


def check_disbursements(states: list[dict], schemes: list[dict], fy: str, issues: list) -> None:
    for st in states:
        fig = st.get("beneficiary_despatched")
        if fig is not None and not disbursement_ok(fig):
            _issue(issues, st["code"], "beneficiary_despatched", "error",
                   "disbursed: needs basis 'actuals', source URL, period and as-of date – not shown")
    for s in schemes:
        if s.get("fy") == fy and s.get("despatched") is not None and not disbursement_ok(s.get("despatched_source")):
            _issue(issues, s["state"], f"scheme {s['name']}", "error",
                   "disbursed: needs despatched_source with URL, period and as-of date – not shown")


SCHEME_STATUSES = ("verified", "secondary", "reported")
FUNDING_TYPES = ("state", "centrally_sponsored", "central", "unknown")
REPORT_KINDS = ("new_scheme", "existing_scheme", "revised_allocation", "disbursement_update")


def check_schemes(schemes: list[dict], fy: str, issues: list) -> None:
    for s in schemes:
        if s.get("fy") != fy:
            continue
        field, status = f"scheme {s['name']}", s.get("status")
        if status not in SCHEME_STATUSES:
            _issue(issues, s["state"], field, "error", f"schemes: unknown status {status!r}")
        elif s.get("announced") is not None:
            if status == "reported":
                _issue(issues, s["state"], field, "error",
                       "schemes: amount on a reported scheme – only officially verified amounts are shown")
            elif status == "verified" and not any(x.get("kind") == "govt" for x in s.get("sources", [])):
                _issue(issues, s["state"], field, "error", "schemes: verified amount without a government source")
        if s.get("funding") is not None and s["funding"] not in FUNDING_TYPES:
            _issue(issues, s["state"], field, "error", f"schemes: unknown funding type {s['funding']!r}")
        for r in s.get("reports", []):
            if r.get("kind") not in REPORT_KINDS:
                _issue(issues, s["state"], field, "error", f"schemes: unknown report type {r.get('kind')!r}")


def compare(st: dict, actual_key: str, estimate_key: str, fy: str, issues: list) -> float | None:
    """Actual as % of revised estimate, or None when the comparison would be invalid."""
    code, field = st["code"], f"{actual_key}/{estimate_key}"
    a, e = st.get(actual_key), st.get(estimate_key)
    if not (a and e and isinstance(a.get("value"), (int, float)) and isinstance(e.get("value"), (int, float))):
        return None
    why = None
    if a.get("basis") != "provisional actuals" or e.get("basis") != "RE":
        why = f"basis: comparing {a.get('basis')} with {e.get('basis')}"
    elif a.get("fy") != fy or e.get("fy") != fy:
        why = f"fy: {a.get('fy')} vs {e.get('fy')} (site FY {fy})"
    elif not a.get("definition") or a.get("definition") != e.get("definition"):
        why = f"definition: {a.get('definition')} vs {e.get('definition')}"
    else:
        prs_be = ((st.get("prs_table") or {}).get("rows", {}).get(PRS_ROW.get(e["definition"], "")) or {}).get("BE")
        cag_be = (a.get("derived") or {}).get("be")
        if prs_be is None or cag_be is None:
            why = "heads: no BE in both sources to confirm the accounting heads match"
        elif abs(prs_be - cag_be) > max(HEADS_TOL[0], HEADS_TOL[1] * prs_be):
            why = (f"heads: BE {fy} differs – CAG {cag_be:,.2f} vs PRS {prs_be:,.0f} "
                   f"({100 * (cag_be - prs_be) / prs_be:+.1f}%), so the sources group heads differently")
    if why:
        _issue(issues, code, field, "blocked", why)
        return None
    ratio = a["value"] / e["value"]
    if not RATIO_RANGE[0] <= ratio <= RATIO_RANGE[1]:
        _issue(issues, code, field, "blocked", f"ratio: {100 * ratio:.1f}% is outside "
                                               f"{100 * RATIO_RANGE[0]:.0f}–{100 * RATIO_RANGE[1]:.0f}% – check before publishing")
        return None
    return round(100 * ratio, 1)


def check_figures(st: dict, fy: str, issues: list) -> None:
    code = st["code"]
    for key in FIGURES:
        fig = st.get(key)
        if not isinstance(fig, dict) or fig.get("value") is None:
            continue
        if not fig.get("basis"):
            _issue(issues, code, key, "error", "basis: figure does not say whether it is BE, RE or actuals")
        if not fig.get("fy"):
            _issue(issues, code, key, "error", "fy: figure has no financial year")
        elif fig["fy"] != fy:
            _issue(issues, code, key, "error", f"fy: {fig['fy']} but the site reports {fy}")
        if not fig.get("definition"):
            _issue(issues, code, key, "error", "definition: figure has no definition")
        d = fig.get("derived")
        if key.endswith("_actual") and not (d and d.get("formula") and d.get("inputs") and d.get("source_url")):
            _issue(issues, code, key, "error", "derived: calculated figure without formula / inputs / source URL")
    # Source preservation: hand-entered seed figures must still equal the PRS rows they came from.
    rows = (st.get("prs_table") or {}).get("rows", {})
    for key in ("budget", "tax", "expenditure", "beneficiary_announced"):
        fig = st.get(key)
        row = rows.get(PRS_ROW.get((fig or {}).get("definition"), ""))
        if fig and row and fig.get("basis") == "RE" and fig["value"] != row["RE"]:
            _issue(issues, code, key, "error", f"source: {fig['value']} ≠ PRS {PRS_ROW[fig['definition']]} RE {row['RE']}")
    if st.get("pilot"):
        for key in ("expenditure", "tax", "budget_actual", "tax_actual"):
            if not (st.get(key) or {}).get("value"):
                _issue(issues, code, key, "missing", "no figure")


def run(states: list[dict], fy: str, schemes: list[dict] = ()) -> tuple[dict[str, dict], list[dict]]:
    """{code: {'budget': pct|None, 'tax': pct|None}} and the list of issues."""
    issues, comparisons = [], {}
    check_disbursements(states, list(schemes), fy, issues)
    check_schemes(list(schemes), fy, issues)
    for st in states:
        check_figures(st, fy, issues)
        comparisons[st["code"]] = {"budget": compare(st, "budget_actual", "expenditure", fy, issues),
                                   "tax": compare(st, "tax_actual", "tax", fy, issues)}
    return comparisons, issues

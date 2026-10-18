"""PRS state budget analyses → the published key-figure rows, kept exactly as printed.

The page's 'Budget <next year> - Key figures' and 'Break-up of receipts' tables have columns
  <FY-1> Actuals | <FY> Budgeted | <FY> Revised | % change | <FY+1> Budgeted | % change
We keep the BE and RE of a few rows, after confirming the column headers name the right FY.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

ROWS = ["Total Expenditure", "(-) Repayment of debt", "Net Expenditure (E)",
        "State's Own Tax", "Share in Central Taxes"]
# PRS's "Net Expenditure (E)" = Total Expenditure − Repayment of debt
#                              = revenue expenditure + capital outlay + loans & advances.
EXP_STD = "exp_std"                 # revenue expenditure + capital outlay + loans & advances disbursed
EXP_INCL_DEBT = "exp_incl_debt"     # the above + repayment of debt
OWN_TAX = "own_tax"                 # state's own tax revenue (excludes share in central taxes)


def _n(s: str) -> float | None:
    s = s.replace(",", "").strip()
    return float(s) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def key_rows(html: bytes, fy: str) -> tuple[dict, list[str]]:
    """{row: {'BE': x, 'RE': y}} for ROWS, plus problems found."""
    soup = BeautifulSoup(html, "html.parser")
    rows, problems = {}, []
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        head = [c.get_text(" ", strip=True) for c in trs[0].find_all(["th", "td"])]
        try:
            be, re_ = head.index(f"{fy} Budgeted"), head.index(f"{fy} Revised")
        except ValueError:
            continue  # not a table for this FY
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            label = cells[0].replace("’", "'") if cells else ""
            if label in ROWS and label not in rows and len(cells) > max(be, re_):
                vals = {"BE": _n(cells[be]), "RE": _n(cells[re_])}
                if None not in vals.values():
                    rows[label] = vals
    for r in ROWS:
        if r not in rows:
            problems.append(f"PRS row '{r}' for {fy} not found")
    t, d, n = (rows.get(k) for k in ROWS[:3])
    if t and d and n:
        for col in ("BE", "RE"):
            if abs(t[col] - d[col] - n[col]) > 1:
                problems.append(f"PRS {col}: Total Expenditure − Repayment of debt ≠ Net Expenditure")
    return rows, problems


def update_reference(fetcher, seed: dict, fy: str, today: str) -> dict[str, list[str]]:
    """For each state citing a PRS analysis: store the PRS rows, label the original figures with
    their definition, and add the standardized expenditure as a separate figure.
    Original values are never changed; a mismatch with PRS is reported, not corrected."""
    problems = {}
    for st in seed["states"]:
        src = st.get("source") or {}
        if "prsindia.org" not in src.get("url", ""):
            continue
        got = fetcher.get(src["url"], max_age_h=24 * 7)
        if not got:
            problems[st["code"]] = ["PRS page could not be fetched"]
            continue
        rows, probs = key_rows(got[0], fy)
        st["prs_table"] = {"source": src, "fy": fy, "columns": {"BE": f"{fy} Budgeted", "RE": f"{fy} Revised"},
                           "rows": rows, "checked_on": today}
        for key, definition in (("budget", EXP_INCL_DEBT), ("tax", OWN_TAX)):
            if isinstance(st.get(key), dict):
                st[key].setdefault("definition", definition)
                st[key].setdefault("fy", fy)
        net = rows.get("Net Expenditure (E)")
        if net and not any("≠ Net" in p for p in probs):
            st["expenditure"] = {
                "value": net["RE"], "basis": "RE", "fy": fy, "definition": EXP_STD, "status": "secondary",
                "label": "Total expenditure excl. debt repayment (revenue + capital outlay + loans)",
                "source": src, "source_row": "Net Expenditure (E)",
                "published_formula": "Total Expenditure − Repayment of debt (as printed by PRS)"}
        if probs:
            problems[st["code"]] = probs
    return problems

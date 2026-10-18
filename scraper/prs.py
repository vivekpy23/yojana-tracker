"""PRS state budget analyses → the published key-figure rows, kept exactly as printed.

The page's 'Budget <next year> - Key figures' and 'Break-up of receipts' tables have columns
  <FY-1> Actuals | <FY> Budgeted | <FY> Revised | % change | <FY+1> Budgeted | % change
We keep the BE and RE of a few rows, after confirming the column headers name the right FY.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

SW_NUTRITION_ROW = "Social Welfare and Nutrition"
ROWS = ["Total Expenditure", "(-) Repayment of debt", "Net Expenditure (E)",
        "State's Own Tax", "Share in Central Taxes", SW_NUTRITION_ROW]
# PRS's "Net Expenditure (E)" = Total Expenditure − Repayment of debt
#                              = revenue expenditure + capital outlay + loans & advances.
EXP_STD = "exp_std"                 # revenue expenditure + capital outlay + loans & advances disbursed
EXP_INCL_DEBT = "exp_incl_debt"     # the above + repayment of debt
OWN_TAX = "own_tax"                 # state's own tax revenue (excludes share in central taxes)
SW_NUTRITION = "sw_nutrition"       # 'Social Welfare and Nutrition' sector expenditure


def _n(s: str) -> float | None:
    s = s.replace(",", "").strip()
    return float(s) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("’", "'").replace("&", "and").replace("\xa0", " ")).strip().lower()


def _col(head: list[str], fy: str, names: tuple[str, ...]) -> int | None:
    want = {_norm(f"{fy} {n}") for n in names}
    return next((i for i, h in enumerate(head) if _norm(h) in want), None)


def key_rows(html: bytes, fy: str) -> tuple[dict, list[str]]:
    """{row: {'BE': x, 'RE': y}} for ROWS, plus problems found.
    Column headers vary by state ('2025-26 Revised' / '2025-26 RE'); both are accepted."""
    soup = BeautifulSoup(html, "html.parser")
    rows, problems = {}, []
    wanted = {_norm(r): r for r in ROWS}
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        head = [c.get_text(" ", strip=True) for c in trs[0].find_all(["th", "td"])]
        be, re_ = _col(head, fy, ("Budgeted", "BE")), _col(head, fy, ("Revised", "RE"))
        if be is None or re_ is None:
            continue  # not a table with this FY's BE and RE
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            label = wanted.get(_norm(cells[0])) if cells else None
            if label and label not in rows and len(cells) > max(be, re_):
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


# seed key -> (definition, PRS row, label) for figures read straight from a PRS row (RE column)
FROM_ROWS = {
    "budget": (EXP_INCL_DEBT, "Total Expenditure", "Total expenditure incl. debt repayment"),
    "tax": (OWN_TAX, "State's Own Tax", "State's own tax revenue"),
    "beneficiary_announced": (SW_NUTRITION, SW_NUTRITION_ROW, "Social Welfare & Nutrition sector"),
}


def next_fy(fy: str) -> str:
    return f"{int(fy[:4]) + 1}-{int(fy[5:]) + 1:02d}"


def analysis_url(cfg: dict, code: str, fy: str) -> str | None:
    """PRS analysis of the *next* year's budget, which carries this FY's revised estimates."""
    slug = cfg["prs"]["slugs"].get(code)
    return cfg["prs"]["url"].format(slug=slug, year=next_fy(fy)) if slug else None


def update_reference(fetcher, seed: dict, fy: str, today: str, cfg: dict | None = None) -> dict[str, list[str]]:
    """For each state with a PRS analysis: store the PRS rows, label existing figures with their
    definition, create figures that are missing from the PRS rows as printed, and add the
    standardized expenditure as a separate figure.
    Existing values are never changed; a mismatch with PRS is reported (validate.py), not corrected."""
    problems = {}
    for st in seed["states"]:
        url = (analysis_url(cfg, st["code"], fy) if cfg else None) or (st.get("source") or {}).get("url", "")
        if "prsindia.org" not in url:
            continue
        got = fetcher.get(url, max_age_h=24 * 7)
        if not got:
            problems[st["code"]] = ["PRS page could not be fetched"]
            continue
        rows, probs = key_rows(got[0], fy)
        src = st.get("source") if (st.get("source") or {}).get("url") == url else \
            {"title": f"PRS – {st['name']} Budget Analysis {next_fy(fy)}", "url": url}
        st.setdefault("source", src)
        st["prs_table"] = {"source": src, "fy": fy, "columns": {"BE": f"{fy} BE", "RE": f"{fy} RE"},
                           "rows": rows, "checked_on": today}
        for key, (definition, row, label) in FROM_ROWS.items():
            if isinstance(st.get(key), dict):            # entered earlier: keep the value, label it
                st[key].setdefault("definition", definition)
                st[key].setdefault("fy", fy)
            elif row in rows:                            # missing: take the PRS row as printed
                st[key] = {"value": rows[row]["RE"], "basis": "RE", "fy": fy, "definition": definition,
                           "status": "secondary", "label": label, "source": src, "source_row": row}
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

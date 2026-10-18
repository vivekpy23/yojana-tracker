"""CAG 'Monthly Key Indicators' (state accounts at a glance) → provisional actuals for a full FY.

Each state's page on cag.gov.in has a 'Monthly Key Indicators' tab listing one PDF per month.
The March report of a FY is cumulative for the whole year. Several states publish more than one
March version; we prefer March(Sup) (after supplementary/closing entries), then plain March,
then March(Pre).

Pages 1–2 of every report hold the 'Accounts at a Glance' table:
    Description | Budget Estimates | Actuals up to March | % current year | % previous year
Layouts differ between AG offices, so a figure is only accepted when it passes an arithmetic
cross-check against other rows of the same table; otherwise it stays None ("Pending" on the site).
Both figures are derived (calculated from printed rows) and stored with their formula and inputs.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .prs import EXP_STD, OWN_TAX

log = logging.getLogger(__name__)

NUM = re.compile(r"^-?[\d,]+(?:\.\d+)?%?$")
TOL = 1.0  # ₹ crore; figures are printed to 2 decimals, so components must add up within rounding


# ---------------------------------------------------------------- listing pages
def state_pages(html: bytes, base: str) -> dict[str, str]:
    """{'Maharashtra': 'https://cag.gov.in/en/state-accounts-report?defuat_state_id=79', ...}"""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        if "defuat_state_id=" in a["href"]:
            href = a["href"] if a["href"].startswith("http") else "https://cag.gov.in" + a["href"]
            out.setdefault(a.get_text(" ", strip=True), href)
    return out


def _march_rank(label: str) -> int:
    lab = label.lower()
    if "sup" in lab or "(s" in lab:
        return 0
    if "pre" in lab or "(p" in lab:
        return 2
    return 1


def march_report(html: bytes, year_end: int, pdf_hosts: list[str]) -> tuple[str, str] | None:
    """(link label, PDF url) of the best March report for the FY ending in `year_end`, or None."""
    soup = BeautifulSoup(html, "html.parser")
    tab = soup.find(id="tab-360")  # 'Monthly Key Indicators'
    if tab is None:
        return None
    pat = re.compile(rf"^Mar\w*\s*(\(\w+\.?\))?\s*,\s*{year_end}$", re.I)
    found = []
    for a in tab.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        if pat.match(label) and urlparse(a["href"]).netloc.lower() in pdf_hosts:
            found.append((label, a["href"]))
    return sorted(dict.fromkeys(found), key=lambda x: _march_rank(x[0]))[0] if found else None


# ---------------------------------------------------------------- PDF table
@dataclass
class Row:
    label: str
    budget: float
    actual: float
    pct: float  # printed '% of actual to budget estimates', current year

    def pct_consistent(self) -> bool:
        return self.budget != 0 and abs(100 * self.actual / self.budget - self.pct) <= 0.15


def _num(tok: str) -> float:
    return float(tok.replace(",", "").rstrip("%"))


def _split(line: str) -> tuple[str, list[str]]:
    """Label and the run of numeric tokens at the end of the line."""
    toks = line.split()
    i = len(toks)
    while i > 0 and NUM.match(toks[i - 1]):
        i -= 1
    return " ".join(toks[:i]), toks[i:]


def table_rows(text: str) -> list[Row]:
    """Rows of the 'Accounts at a Glance' table that carry exactly BE, actual, %, %.

    A row whose numbers wrapped onto the next line (all four, or the two percentages) is joined
    with it. Rows with any other number of trailing figures (blank, '…', digits split by spaces)
    are dropped, never guessed.
    """
    lines = [re.sub(r"(\d)\s+%", r"\1%", ln.strip()) for ln in text.splitlines() if ln.strip()]  # '92.09 %'
    rows, i = [], 0
    while i < len(lines):
        label, nums = _split(lines[i])
        if label and len(nums) in (0, 2) and i + 1 < len(lines):
            nlabel, nnums = _split(lines[i + 1])
            if not nlabel and len(nums) + len(nnums) == 4:
                nums, i = nums + nnums, i + 1
        if label and len(nums) == 4:
            rows.append(Row(label, _num(nums[0]), _num(nums[1]), _num(nums[2])))
        i += 1
    return rows


def _find(rows: list[Row], pattern: str) -> tuple[int, Row] | tuple[None, None]:
    rx = re.compile(pattern, re.I)
    for i, r in enumerate(rows):
        if rx.search(r.label):
            return i, r
    return None, None


@dataclass
class Derived:
    """A value we calculated from printed rows. `be` is the same formula applied to the BE column."""
    value: float
    be: float
    formula: str
    inputs: dict          # name -> {"row": printed label, "be": x, "actual": y}
    checks: list[str] = field(default_factory=list)


@dataclass
class Indicators:
    expenditure: Derived | None = None   # revenue expenditure + capital outlay + loans & advances
    own_tax: Derived | None = None       # tax revenue − state's share of Union taxes
    problems: list[str] = field(default_factory=list)


def _inp(r: Row) -> dict:
    return {"row": r.label, "be": r.budget, "actual": r.actual}


def parse_mki(text: str, year_end: int) -> Indicators:
    """Full-year standardized expenditure and own tax revenue from a March MKI report."""
    out = Indicators()
    head = text[:700].lower()
    if str(year_end) not in head or not re.search(rf"march|03/{year_end}|\b13\b", head):
        out.problems.append("page 1 is not a March report for this year")
        return out
    rows = table_rows(text)

    # Expenditure on the standard definition, computed from rows (4), (5) and loans disbursed.
    # The printed 'Total Expenditure' is kept only as a check that the component rows were read right:
    # states print it as (4+5) or (4+5+8), so it must equal one of those sums.
    _, total = _find(rows, r"^\(?\d+\)?\.?\s*Total Expenditure")
    _, rev = _find(rows, r"^\(?4\)?\.?\s*Revenue Expenditure")
    _, cap = _find(rows, r"^\(?5\)?\.?\s*Capital Expenditure")
    _, loans = _find(rows, r"Loans? and Advances Disbursed")
    if not (total and rev and cap and loans):
        out.problems.append("expenditure: revenue / capital / loans / total row missing or unreadable")
    else:
        printed = {"4+5": (rev.actual + cap.actual, rev.budget + cap.budget),
                   "4+5+loans": (rev.actual + cap.actual + loans.actual, rev.budget + cap.budget + loans.budget)}
        ok = [k for k, (a, _) in printed.items() if abs(a - total.actual) <= TOL]
        # When the printed total excludes loans, the loans row is checked against its own % column.
        loans_ok = "4+5+loans" in ok or loans.pct_consistent() or (loans.budget == 0 and loans.actual == 0)
        if ok and not loans_ok:
            out.problems.append("expenditure: loans row's % column does not match its BE and actual")
        elif ok:
            out.expenditure = Derived(
                value=round(rev.actual + cap.actual + loans.actual, 2),
                be=round(rev.budget + cap.budget + loans.budget, 2),
                formula="Revenue Expenditure (4) + Capital Expenditure (5) + Loans and Advances Disbursed",
                inputs={"revenue_expenditure": _inp(rev), "capital_outlay": _inp(cap),
                        "loans_and_advances": _inp(loans), "printed_total": _inp(total)},
                checks=[f"printed Total Expenditure = {ok[0]}",
                        "loans row: in printed total" if "4+5+loans" in ok else "loans row: % column consistent"])
        else:
            out.problems.append(f"expenditure: printed total {total.actual} matches no sum of its components")

    # Own tax revenue = (a) Tax Revenue − (vi) State's share of Union taxes, both as printed.
    ti, tax = _find(rows, r"^\(?a\)\s*Tax Revenue")
    _, share = _find(rows, r"share of union taxes")
    ni, _ = _find(rows, r"^\(?b\)\s*Non")
    if not (tax and share and ni is not None and ni > ti):
        out.problems.append("own tax: tax revenue / union share rows missing or unreadable")
    else:
        items = [r for r in rows[ti + 1:ni] if re.match(r"^\(?[ivxl]+\)", r.label, re.I)]
        if items and abs(sum(r.actual for r in items) - tax.actual) <= TOL:
            out.own_tax = Derived(
                value=round(tax.actual - share.actual, 2), be=round(tax.budget - share.budget, 2),
                formula="(a) Tax Revenue − (vi) State's share of Union taxes",
                inputs={"tax_revenue": _inp(tax), "union_share": _inp(share)},
                checks=[f"{len(items)} tax items add up to the tax revenue row"])
        else:
            out.problems.append("own tax: tax items do not add up to the tax revenue row")
    return out


def glance_text(body: bytes) -> str:
    """Text of the 'Accounts at a Glance' table: page 1, plus page 2 where the table runs over,
    stopping where the 'Monthly Trend' pages begin."""
    import pdfplumber
    with pdfplumber.open(io.BytesIO(body)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
    m = re.search(r"monthly trend|format of the progressive", text, re.I)
    return text[:m.start()] if m else text


# ---------------------------------------------------------------- pipeline step
def update_actuals(fetcher, cfg: dict, seed: dict, fy: str, today: str) -> dict:
    """Fill seed state 'budget_actual' / 'tax_actual'. Never clears a figure already stored."""
    c = cfg["cag"]
    year_end = int(fy[:4]) + 1
    tally = {"filled": [], "no_report": [], "unreadable": {}}
    pages = {}
    for idx in c["index_pages"]:
        got = fetcher.get(idx, max_age_h=24 * 7)
        if got:
            pages.update(state_pages(got[0], idx))
    for st in seed["states"]:
        name = c["state_names"].get(st["code"])
        page = pages.get(name) if name else None
        got = fetcher.get(page, max_age_h=24 * 7) if page else None
        report = march_report(got[0], year_end, c["pdf_hosts"]) if got else None
        if not report:
            tally["no_report"].append(st["code"])
            continue
        label, url = report
        pdf = fetcher.get(url, max_age_h=24 * 90)
        if not pdf or not pdf[0].startswith(b"%PDF"):
            tally["unreadable"][st["code"]] = "download did not return a PDF"
            continue
        try:
            ind = parse_mki(glance_text(pdf[0]), year_end)
        except Exception as e:  # malformed PDFs happen
            tally["unreadable"][st["code"]] = f"pdf parse failed: {e}"
            continue
        src = {"title": f"CAG – {name} Monthly Key Indicators, {label} (Accounts at a Glance)", "url": url}
        base = {"basis": "provisional actuals", "fy": fy, "period": f"April {year_end - 1} – March {year_end}",
                "status": "verified", "source": src, "report": label, "listing": page, "checked_on": today}
        for key, d, definition, lab in (
                ("budget_actual", ind.expenditure, EXP_STD,
                 "Total expenditure excl. debt repayment (revenue + capital outlay + loans)"),
                ("tax_actual", ind.own_tax, OWN_TAX, "Own tax revenue")):
            if d is not None:
                st[key] = {"value": d.value, **base, "definition": definition, "label": lab,
                           "derived": {"formula": d.formula, "inputs": d.inputs, "be": d.be,
                                       "checks": d.checks, "source_url": url}}
        if ind.expenditure is not None or ind.own_tax is not None:
            tally["filled"].append(st["code"])
        if ind.problems:
            tally["unreadable"][st["code"]] = "; ".join(ind.problems)
    return tally

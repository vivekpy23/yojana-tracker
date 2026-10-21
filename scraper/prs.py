"""PRS state budget analyses → the published key-figure rows, kept exactly as printed.

The page's 'Budget <next year> - Key figures' and 'Break-up of receipts' tables have columns
  <FY-1> Actuals | <FY> Budgeted | <FY> Revised | % change | <FY+1> Budgeted | % change
We keep the BE and RE of a few rows, after confirming the column headers name the right FY.
"""
from __future__ import annotations

import io
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .extract import ALLOC_CUES, SENTENCE_SPLIT, WELFARE, category, find_amounts, find_schemes
from .store import name_score

SW_NUTRITION_ROW = "Social Welfare and Nutrition"
ROWS = ["Total Expenditure", "(-) Repayment of debt", "Net Expenditure (E)",
        "State's Own Tax", "Share in Central Taxes", SW_NUTRITION_ROW, "Revenue Receipts"]
# PRS's "Net Expenditure (E)" = Total Expenditure − Repayment of debt
#                              = revenue expenditure + capital outlay + loans & advances.
EXP_STD = "exp_std"                 # revenue expenditure + capital outlay + loans & advances disbursed
EXP_INCL_DEBT = "exp_incl_debt"     # the above + repayment of debt
OWN_TAX = "own_tax"                 # state's own tax revenue (excludes share in central taxes)
SW_NUTRITION = "sw_nutrition"       # 'Social Welfare and Nutrition' sector expenditure
REV_RECEIPTS = "revenue_receipts"   # total revenue receipts (excludes borrowings and capital receipts)


def _n(s: str) -> float | None:
    s = s.replace(",", "").strip().rstrip("*")  # a footnote marker on a printed figure
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
        rev = rows.get("Revenue Receipts")
        if rev and not isinstance(st.get("revenue"), dict):  # the BE as printed; never computed
            st["revenue"] = {"value": rev["BE"], "basis": "BE", "fy": fy, "definition": REV_RECEIPTS,
                             "status": "secondary", "label": f"Revenue receipts, FY {fy} Budget Estimates",
                             "source": src, "source_row": "Revenue Receipts"}
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


# ---------------------------------------------------------------- schemes from the budget analyses
# The analysis of a year's budget (listed at prs.listing_url) names that year's schemes and announcements:
#   web page  'Policy Highlights': one bullet per announcement, "<b>Topic:</b> text"
#   PDF       sector table, 'Budget Provisions' column: "Rs X crore has been allocated towards <scheme>."
# Every named scheme is kept, with or without an allocation. An unnamed announcement is kept (named by its
# topic) only when it is a welfare measure. An amount is the scheme's allocation only when one sentence
# names the scheme (or says "the scheme" in a bullet about one scheme), carries an allocation cue and has
# a single ₹ crore figure that isn't a group total.
ANALYSIS_RE = re.compile(r"/budgets/states/([a-z-]+)-budget-analysis-(\d{4}-\d{2})/?$")
_THE_SCHEME = re.compile(r"\b(?:the|this) (?:scheme|yojana|programme|mission)\b", re.I)
_GROUP = re.compile(r"\b(?:including|such as|like|among|and other)\b", re.I)


def listing(html: bytes, base: str, slugs: dict[str, str], fy: str) -> tuple[dict[str, str], list[str]]:
    """{state code: analysis URL} for `fy` on the PRS listing page, plus slugs with no known state."""
    by_slug = {v: k for k, v in slugs.items()}
    found, unknown = {}, []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(base, a["href"]).split("?")[0].split("#")[0]
        m = ANALYSIS_RE.search(url)
        if not m or m.group(2) != fy:
            continue
        code = by_slug.get(m.group(1))
        if code:
            found.setdefault(code, url)
        elif m.group(1) not in unknown:
            unknown.append(m.group(1))
    return found, unknown


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace("\xa0", " ").replace("", " ")).strip()


def highlights(html: bytes) -> list[tuple[str, str]]:
    """(topic, text) for each 'Policy Highlights' bullet on the web page."""
    soup = BeautifulSoup(html, "html.parser")
    head = soup.find(lambda t: t.name in ("strong", "b", "h2", "h3", "h4") and
                     _clean(t.get_text()).lower() == "policy highlights")
    if not head:
        return []
    out = []
    for sib in (head.find_parent("p") or head).find_next_siblings():
        if sib.name not in ("ul", "ol"):
            break  # the bullets end where the next section (a table or heading) starts
        for li in sib.find_all("li"):
            text = _clean(li.get_text(" "))
            b = li.find(["strong", "b"])
            topic = _clean(b.get_text(" ")).rstrip(":").strip() if b else ""
            if topic and text.startswith(topic):
                text = text[len(topic):].lstrip(" :")
            out.append((topic, text))
    return out


def pdf_link(html: bytes, base: str) -> str | None:
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = urljoin(base, a["href"])
        if "prsindia.org/files/" in href and href.lower().endswith(".pdf") and "analysis" in href.lower():
            return href
    return None


_NUM = re.compile(r"^-?[\d,.]+%?$")


def provisions(pdf: bytes) -> list[tuple[str, str]]:
    """(sector row, sentence) for the 'Budget Provisions' column of the PDF's sector-expenditure table(s).
    Cropped by position: the column starts at its leftmost 'Rs' bullet, right of the figures; each row runs
    from one bullet to the next, and its sector label is the text left of the row's first figure."""
    import pdfplumber
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    out = []
    try:
        with pdfplumber.open(io.BytesIO(pdf)) as doc:
            for page in doc.pages:
                words = page.extract_words()
                head = [w for i, w in enumerate(words) if w["text"] == "Budget" and i + 1 < len(words)
                        and words[i + 1]["text"].startswith("Provision")]
                if not head:
                    continue
                top = head[0]["bottom"]
                foot = [w for w in words if w["text"].startswith("Source") and w["top"] > top]
                bottom = foot[0]["top"] if foot else page.height
                bullets = [w for w in words if w["text"] in ("Rs", "") and top < w["top"] < bottom
                           and w["x0"] > page.width * 0.45]
                if not bullets:
                    continue
                left = min(w["x0"] for w in bullets) - 2
                starts = sorted({round(w["top"]) for w in bullets if w["x0"] - left < 15})
                for i, y in enumerate(starts):
                    y_end = starts[i + 1] - 1 if i + 1 < len(starts) else bottom
                    text = _clean(page.within_bbox((left, y - 2, page.width, y_end)).extract_text() or "")
                    row = [w for w in words if y - 8 <= w["top"] < y_end and w["x1"] < left]
                    nums = [w["x0"] for w in row if _NUM.match(w["text"])]
                    sector = " ".join(w["text"] for w in row if w["x1"] < min(nums, default=left))
                    # one provision per 'Rs X crore' (some PDFs lose the full stop between them)
                    out += [(sector, s.strip()) for s in
                            re.split(r"\s+(?=Rs\.?\s*[\d,.]+\s*(?:lakh\s*)?crore)", text) if s.startswith("Rs")]
    except Exception:  # an unreadable PDF leaves the web page's highlights
        logging.getLogger(__name__).warning("PRS PDF could not be read")
    return out


_LOWER_SCHEME = re.compile(
    r"((?:(?:[A-Z][\w'’-]*|from|of|for|and)\s+(?:\([\w\s]+\)\s+)?){1,7})(scheme|yojana)\b")
_NOT_A_NAME = {"the", "this", "a", "an", "new", "under", "state", "such", "every", "each", "its", "their",
               "from", "of", "for", "and"}
# 'Under the Mera Pani, Meri Viraasat Yojana': a name with commas, recognised by the 'the' before it
_COMMA_HEAD = re.compile(r"\bthe ((?:[A-Z][\w'’-]*,? (?:and )?)+)$")


def names_in(text: str) -> list[str]:
    """Scheme names in PRS's text, sentence by sentence: the news extractor's, plus names written with a
    lower-case 'scheme' ('Talliki Vandanam scheme', 'Freedom from Hunger scheme') and names with commas."""
    found = []
    for sent in SENTENCE_SPLIT.split(text):
        names = [n for _, n in find_schemes(sent)]
        for m in _LOWER_SCHEME.finditer(sent):
            words = m.group(1).split()
            while words and words[0].lower() in _NOT_A_NAME:
                words = words[1:]
            name = " ".join(words + [m.group(2).capitalize()])
            if len(words) and not any(name.lower() in n.lower() or n.lower() in name.lower() for n in names):
                names.append(name)
        for i, n in enumerate(names):
            at = sent.find(n)
            head = _COMMA_HEAD.search(sent[:at]) if at > 0 else None
            if head and "," in head.group(1):
                names[i] = head.group(1) + n
        found += names
    return list(dict.fromkeys(found))


def _allocation(name: str, text: str, single: bool, others: list[str] = ()) -> float | None:
    """The one ₹ crore figure of a sentence with an allocation cue that names this scheme (or "the
    scheme", when the text is about one scheme) and no other scheme nearer to the figure."""
    for sent in SENTENCE_SPLIT.split(text):
        amounts = find_amounts(sent)
        if len(amounts) != 1 or not ALLOC_CUES.search(sent):
            continue
        if name in sent:
            pos = sent.index(name)
        elif single and _THE_SCHEME.search(sent):
            pos = _THE_SCHEME.search(sent).start()
        else:
            continue
        at = amounts[0][0]
        if _GROUP.search(sent[min(at, pos):max(at, pos)]):
            continue  # a group total, not this scheme's
        if any(o != name and o in sent and abs(sent.index(o) - at) < abs(pos - at) for o in others):
            continue  # the figure belongs to another scheme in the sentence
        return amounts[0][1]
    return None


def schemes_on_page(code: str, fy: str, url: str, title: str, page: list[tuple[str, str]],
                    provisions_: list[tuple[str, str]], pdf_url: str | None) -> list[dict]:
    """Scheme items from one analysis, deduplicated within the page (same scheme under two spellings, or
    a short and a full name): the first wording is kept, an allocation and a welfare reading win.
    kind 'scheme' – a named scheme; 'announcement' – an unnamed welfare measure, named by its topic."""
    items: list[dict] = []

    def add(name, text, allocation, section, src_url, kind="scheme", context=""):
        cat = category(f"{name}. {context}. {text}")
        it = next((i for i in items if name_score(i["name"], name) >= 88), None)
        if it:
            if it["allocation"] is None and allocation is not None:
                it.update(allocation=allocation, allocation_source=src_url)
            if cat == WELFARE:
                it["category"] = WELFARE
            if len(name) > len(it["name"]) and name.lower().endswith(it["name"].lower()):
                it["name"] = name  # 'Scholarship Scheme' → 'Chief Minister's Scholarship Scheme'
            return
        items.append({"state": code, "fy": fy, "name": name, "kind": kind, "benefit": text,
                      "allocation": allocation, "allocation_source": src_url if allocation is not None else None,
                      "category": cat, "section": section, "source": {"kind": "prs", "title": title, "url": url}})

    for topic, text in page:
        named = names_in(f"{topic}: {text}")
        for n in named:
            add(n, text, _allocation(n, text, len(named) == 1, named), "Policy Highlights", url)
        if not named and topic and category(f"{topic}. {text}") == WELFARE:
            add(topic, text, None, "Policy Highlights", url, kind="announcement")
    for sector, sent in provisions_:
        named = names_in(sent)
        for n in named:
            add(n, sent, _allocation(n, sent, len(named) == 1, named), f"Sector expenditure: {sector}".strip(),
                pdf_url or url, context=sector)
    return items


def scheme_items(fetcher, cfg: dict, fy: str) -> tuple[list[dict], dict]:
    """Discover the PRS analyses of `fy`, fetch each and extract its schemes. Returns (items, stats)."""
    pcfg = cfg["prs"]
    list_url = pcfg["listing_url"].format(fy=fy)
    stats = {"listing": list_url, "discovered": 0, "fetched": 0, "pdf_read": 0, "extracted": 0,
             "with_allocation": 0, "by_category": {}, "failed": [], "unknown_slugs": [], "no_schemes": []}
    got = fetcher.get(list_url, max_age_h=24)
    if not got:
        stats["failed"].append("listing page")
        return [], stats
    pages, stats["unknown_slugs"] = listing(got[0], list_url, pcfg["slugs"], fy)
    stats["discovered"] = len(pages)
    items = []
    for code, url in pages.items():
        page = fetcher.get(url, max_age_h=24 * 7)
        if not page:
            stats["failed"].append(code)
            continue
        stats["fetched"] += 1
        h1 = BeautifulSoup(page[0], "html.parser").find("h1")
        title = "PRS – " + (_clean(h1.get_text()) if h1 else f"{code} Budget Analysis {fy}")
        pdf_url, prov = pdf_link(page[0], url), []
        if pdf_url:
            pdf = fetcher.get(pdf_url, max_age_h=24 * 30)
            if pdf:
                prov = provisions(pdf[0])
                stats["pdf_read"] += 1
        found = schemes_on_page(code, fy, url, title, highlights(page[0]), prov, pdf_url)
        if not found:
            stats["no_schemes"].append(code)
        items += found
    stats["extracted"] = len(items)
    stats["with_allocation"] = sum(i["allocation"] is not None for i in items)
    for i in items:
        stats["by_category"][i["category"]] = stats["by_category"].get(i["category"], 0) + 1
    return items, stats

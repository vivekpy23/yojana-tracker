"""Financial claims in an article or press release: every ₹ crore figure, what kind of figure it is, for which
scheme, and as of when.

Types (`type`):
  budget_allocation        an amount set aside in a budget ("allocated", "provision", "बजट")
  revised_allocation       a changed allocation ("revised", "supplementary", "बढ़ा")
  disbursement_event       one transfer on one day ("released today", "आज ... अंतरण")
  cumulative_disbursement  everything transferred so far ("so far", "अब तक", "कुल मिलाकर"); `as_of` is the date the
                           total runs to. A total the text gives for an earlier, unstated date ("इसके पूर्व …
                           अंतरित की जा चुकी है", "had already been transferred") is kept with `as_of: None` and
                           never shown – a later total supersedes it.

Cues are read around each figure: from the previous figure up to the next one, so "अब तक … ₹18,100 Cr" and
"आज … ₹2,500 Cr" in one sentence get different types. A figure in a sentence that names no scheme belongs to the
article's scheme when the article is about one scheme (named in its headline, or clearly the most mentioned).

Dates: `as_of` is the dateline ("पटना, 15 फरवरी 2026", "Patna, Feb 15") or else the publication date; `published`
is the page's own date. `period_start` (for cumulative totals) is the earliest dated transfer the text mentions.
"""
from __future__ import annotations

import re

from .extract import (ALLOC_CUES, FY_RE, SENTENCE_SPLIT, _KIND_CUES, DISBURSEMENT, REVISED_ALLOCATION, find_amounts,
                      find_beneficiaries, find_schemes, fy_of)

BUDGET, REVISED, EVENT, CUMULATIVE = ("budget_allocation", "revised_allocation", "disbursement_event",
                                      "cumulative_disbursement")
FIN_TYPES = (BUDGET, REVISED, EVENT, CUMULATIVE)

_DISBURSE = dict(_KIND_CUES)[DISBURSEMENT]
_REVISED = dict(_KIND_CUES)[REVISED_ALLOCATION]
_CUMULATIVE = re.compile(r"\b(?:so far|till date|to date|until now|till now|cumulative(?:ly)?|in total|in all|"
                         r"altogether|a total of|overall)\b|अब तक|अभी तक|आज तक|कुल मिलाकर|अब कुल|आतापर्यंत|"
                         r"இதுவரை|ಇಲ್ಲಿಯವರೆಗೆ", re.I)
_PRIOR = re.compile(r"\b(?:had already|has already|previously|earlier|before this)\b|इसके पूर्व|इससे पहले|"
                    r"इसके पहले|पहले ही|जा चुकी|जा चुके|जा चुका", re.I)

_THE_SCHEME = re.compile(r"\b(?:the|this|that) (?:scheme|yojana)\b|इस योजना|उक्त योजना|योजना के तहत|योजनेअंतर्गत", re.I)

_MONTHS = {}
for _i, _names in enumerate([
        ("january", "jan", "जनवरी"), ("february", "feb", "फरवरी", "फ़रवरी"), ("march", "mar", "मार्च"),
        ("april", "apr", "अप्रैल", "अप्रेल"), ("may", "मई"), ("june", "jun", "जून"), ("july", "jul", "जुलाई"),
        ("august", "aug", "अगस्त"), ("september", "sept", "sep", "सितंबर", "सितम्बर"),
        ("october", "oct", "अक्टूबर", "अक्तूबर"), ("november", "nov", "नवंबर", "नवम्बर"),
        ("december", "dec", "दिसंबर", "दिसम्बर")], 1):
    for _n in _names:
        _MONTHS[_n] = _i
_MONTH_RE = "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True))
_DAY_MONTH = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_RE + r")\.?(?:,?\s+(\d{4}))?(?![\wऀ-ॿ])",
                        re.I)
_MONTH_DAY = re.compile(r"\b(" + _MONTH_RE + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", re.I)
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})(?!\d)")
_YEAR = re.compile(r"(?<!\d)(20\d\d)(?!\d)")


def _iso(y: int, m: int, d: int) -> str | None:
    if 1 <= m <= 12 and 1 <= d <= 31 and 2000 <= y <= 2100:
        return f"{y:04d}-{m:02d}-{d:02d}"
    return None


def find_dates(text: str, default_year: int | None = None) -> list[tuple[int, str]]:
    """(offset, ISO date) for 'D Month [YYYY]', 'Month D[, YYYY]' and 'DD-MM-YY(YY)' dates. A date written without
    a year takes the next year written within 60 characters ('26 सितम्बर से 28 नवम्बर 2025'), else `default_year`."""
    out = []
    for rx, day_first in ((_DAY_MONTH, True), (_MONTH_DAY, False)):
        for m in rx.finditer(text):
            d, mon = (m.group(1), m.group(2)) if day_first else (m.group(2), m.group(1))
            year = m.group(3)
            if not year:
                nxt = _YEAR.search(text, m.end(), m.end() + 60)
                year = nxt.group(1) if nxt else default_year
            iso = _iso(int(year), _MONTHS[mon.lower()], int(d)) if year else None
            if iso:
                out.append((m.start(), iso))
    for m in _NUMERIC.finditer(text):
        y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        iso = _iso(y, int(m.group(2)), int(m.group(1)))
        if iso:
            out.append((m.start(), iso))
    return sorted(set(out))


def page_dates(text: str, published: str = "") -> tuple[str, str]:
    """(published, as_of). The page's own date is the first date in its opening; the dateline (event date) is the
    first written-out date near the start, which is what 'today' in a press release refers to."""
    head = text[:700]
    dates = find_dates(head)
    pub = published or (dates[0][1] if dates else "")
    worded = [iso for pos, iso in find_dates(head) if not _NUMERIC.match(head, pos)]
    return pub, (worded[0] if worded else pub)


def claim_type(before: str, after: str) -> tuple[str | None, bool]:
    """(type, prior) of a figure from the text before it (back to the previous figure) and after it (up to the
    next). Cumulative cues win over a transfer verb; an explicit earlier total is marked `prior`."""
    around = before + " " + after
    if _DISBURSE.search(around):
        if _PRIOR.search(around) and not _CUMULATIVE.search(before[-80:]):
            return CUMULATIVE, True
        if _CUMULATIVE.search(before[-120:]) or _CUMULATIVE.search(after[:60]):
            return CUMULATIVE, False
        return EVENT, False
    if _REVISED.search(around):
        return REVISED, False
    if ALLOC_CUES.search(around):
        return BUDGET, False
    return None, False


def _group_key(name: str) -> tuple:
    from .store import name_core
    return tuple(name_core(name)[1])


def article_scheme(text: str, title: str = "") -> str | None:
    """The scheme an article is about: the one its headline names, else the most mentioned one when it has at
    least two mentions and twice as many as any other."""
    from .store import name_score
    if title:
        named = find_schemes(title)
        if len({_group_key(n) for _, n in named}) == 1:
            return named[0][1]
    counts: dict[tuple, list[str]] = {}
    for _, n in find_schemes(text):
        key = next((k for k, ns in counts.items() if name_score(ns[0], n) >= 88), None)
        counts.setdefault(key or _group_key(n), []).append(n)
    if not counts:
        return None
    ranked = sorted(counts.values(), key=len, reverse=True)
    top = ranked[0]
    if len(ranked) == 1 or (len(top) >= 2 and len(top) >= 2 * len(ranked[1])):
        return max(top, key=len)  # the fullest wording ('मुख्यमंत्री महिला रोजगार योजना' over 'महिला रोजगार योजना')
    return None


def extract_claims(text: str, *, title: str = "", published: str = "", state: str | None = None,
                   source: dict | None = None) -> list[dict]:
    """Every typed ₹ crore figure in the text, with its scheme, beneficiaries, dates and FY."""
    text = re.sub(r"\s+", " ", text)
    pub, as_of = page_dates(text, published)
    main = article_scheme(text, title)
    year = int((as_of or pub or "2000")[:4])
    starts = [iso for _, iso in find_dates(text, default_year=year) if not as_of or iso <= as_of]
    out = []
    last = None  # the scheme named last, which "the scheme" / "इस योजना" refers to
    for sent in SENTENCE_SPLIT.split(text):
        amounts = find_amounts(sent)
        schemes = find_schemes(sent)
        referred = last if _THE_SCHEME.search(sent) else None
        if schemes:
            last = schemes[-1][1]
        for k, (pos, amount) in enumerate(amounts):
            lo = amounts[k - 1][0] + 25 if k else 0
            hi = amounts[k + 1][0] if k + 1 < len(amounts) else len(sent)
            before, after = sent[lo:pos], sent[pos:hi]
            typ, prior = claim_type(before, after)
            if typ is None:
                continue
            near = [n for p, n in schemes if p < pos] or [n for p, n in schemes]
            scheme = near[-1] if near else (referred or main)
            if not scheme:
                continue
            claim = {"type": typ, "scheme": scheme, "state": state, "amount_cr": amount,
                     "beneficiaries": find_beneficiaries(before[-160:]) or find_beneficiaries(after[:120]),
                     "published": pub or None, "quote": sent[max(0, lo):hi][:400].strip(), "extractor": "rules"}
            if typ in (EVENT, CUMULATIVE):
                claim["as_of"] = None if prior else (as_of or None)
            else:
                claim["as_of"] = pub or None
            if typ == CUMULATIVE and not prior and starts:
                claim["period_start"] = min(starts)
            named_fy = FY_RE.findall(claim["quote"])
            claim["fy"] = ((fy_of(claim["as_of"]) if claim["as_of"] else None) if typ in (EVENT, CUMULATIVE)
                           else named_fy[0] if len(set(named_fy)) == 1
                           else fy_of(pub, budget=True) if pub else None)
            if source:
                claim["source"] = source
            out.append(claim)
    return out

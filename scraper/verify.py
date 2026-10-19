"""Check a news candidate against the text of government documents."""
from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from .extract import find_amounts


@dataclass
class GovtDoc:
    url: str
    text: str


@dataclass
class Verdict:
    outcome: str            # verified | mismatch | review | not_found
    govt_amount: float | None = None
    govt_url: str | None = None
    score: float = 0.0
    snippet: str = ""


def _amount_matches(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= max(tol * max(a, b), 0.5)


def _best_name_hit(name: str, text: str, step: int = 120, width: int = 220) -> tuple[float, int]:
    """Slide a window over the document and return (best fuzzy score, offset)."""
    needle = name.lower()
    hay = text.lower()
    exact = hay.find(needle)
    if exact >= 0:
        return 100.0, exact
    best, best_pos = 0.0, -1
    for i in range(0, max(len(hay) - width, 1), step):
        sc = fuzz.partial_ratio(needle, hay[i:i + width])
        if sc > best:
            best, best_pos = sc, i
    return best, best_pos


def verify(name: str, amount: float, docs: list[GovtDoc], *, verified_min: float = 90,
           review_min: float = 72, tol: float = 0.005, window: int = 400) -> Verdict:
    best = Verdict("not_found")
    for doc in docs:
        if not doc.text:
            continue
        score, pos = _best_name_hit(name, doc.text)
        if score < review_min or pos < 0:
            continue
        around = doc.text[max(0, pos - window // 2): pos + len(name) + window]
        amounts = [a for _, a in find_amounts(around)]
        snippet = " ".join(around.split())[:300]
        match = next((a for a in amounts if _amount_matches(a, amount, tol)), None)
        if score >= verified_min and match is not None:
            return Verdict("verified", match, doc.url, score, snippet)
        if score >= verified_min and len(set(amounts)) == 1:
            # Scheme clearly present with one, different figure: the government number wins.
            cand = Verdict("mismatch", amounts[0], doc.url, score, snippet)
        else:
            cand = Verdict("review", match or (max(amounts) if amounts else None), doc.url, score, snippet)
        rank = {"mismatch": 2, "review": 1, "not_found": 0}
        if rank[cand.outcome] > rank.get(best.outcome, 0) or (
                cand.outcome == best.outcome and cand.score > best.score):
            best = cand
    return best


def find_allocation(names: list[str], docs: list[GovtDoc], *, window: int = 400) -> Verdict:
    """Look for a discovered scheme (no reported figure to compare with) in government documents.

    Stricter than `verify`: one of the scheme's names must appear verbatim (case and spacing aside), and
    exactly one distinct amount may stand near it. The result is never published directly – outcome
    'review' sends it to a person, who confirms the figure is this scheme's allocation for the year."""
    needles = [" ".join(n.lower().split()) for n in names if len(n.split()) >= 2]
    for doc in docs:
        flat = " ".join(doc.text.split())
        low = flat.lower()
        for needle in needles:
            pos = low.find(needle)
            if pos < 0:
                continue
            around = flat[max(0, pos - window // 2): pos + len(needle) + window]
            amounts = {a for _, a in find_amounts(around)}
            if len(amounts) == 1:
                return Verdict("review", amounts.pop(), doc.url, 100.0, around[:300])
    return Verdict("not_found")

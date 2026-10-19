"""Read/write the canonical data files under data/."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from rapidfuzz import fuzz

from .extract import slugify

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SCHEMES = DATA / "schemes.json"
REVIEW = DATA / "review_queue.json"
REJECTED = DATA / "rejected.json"
SEED = DATA / "seed.json"
CATALOG = DATA / "catalog.json"

# verified   amount confirmed in a government document (or an official release)
# secondary  amount from PRS's analysis of the official budget
# reported   seen in the news / discovered only – "Reported — awaiting official verification"; no amount shown
STATUSES = ("verified", "secondary", "reported")


def today() -> str:
    return dt.date.today().isoformat()


def load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- scheme names
_GENERIC = r"the|scheme|yojana|yojna|abhiyan|abhiyaan|programme|program"
_INDIC_GENERIC = {"योजना", "अभियान", "திட்டம்", "ಯೋಜನೆ"}


def name_core(name: str) -> tuple[str | None, list[str]]:
    """(prefix, distinctive words): 'Mukhyamantri Majhi Ladki Bahin Yojana' -> ('cm', ['majhi', 'ladki', 'bahin']).

    The prefix keeps a Prime Minister's scheme ('pm') apart from a Chief Minister's ('cm') of the same name –
    often the centrally sponsored scheme and the state's own one."""
    t = re.sub(r"\(.*?\)", " ", name.lower())
    t = re.sub(r"\b(?:pradhan ?mantri|prime minister'?s?|pm)\b", " pm ", t)
    t = re.sub(r"\b(?:mukhya ?mantri|chief minister'?s?|cm)\b|मुख्यमंत्री|முதலமைச்சர்|ಮುಖ್ಯಮಂತ್ರಿ", " cm ", t)
    t = re.sub(r"\b(?:" + _GENERIC + r")\b", " ", t)
    words = [w for w in re.split(r"[^a-z0-9ऀ-ॿ஀-௿ಀ-೿]+", t) if w and w not in _INDIC_GENERIC]
    prefixes = {w for w in words if w in ("pm", "cm")}
    core = [w for w in words if w not in ("pm", "cm")]
    return (prefixes.pop() if len(prefixes) == 1 else None), core


def name_score(a: str, b: str) -> float:
    """0–100 similarity of two scheme names, after generic words are removed. A one-word name only
    matches the same word, so 'Pension Scheme' never swallows 'Destitute Women Pension Scheme'."""
    (pa, ca), (pb, cb) = name_core(a), name_core(b)
    if not ca or not cb or (pa and pb and pa != pb):
        return 0.0
    if ca == cb:
        return 100.0
    ja, jb = " ".join(ca), " ".join(cb)
    if min(len(ca), len(cb)) < 2:
        return float(fuzz.ratio(ja, jb))
    return float(fuzz.token_set_ratio(ja, jb))


def find_existing(schemes: list[dict], state: str, name: str, fy: str, min_score: float = 88) -> dict | None:
    best, best_sc = None, 0.0
    for s in schemes:
        if s["state"] != state or s["fy"] != fy:
            continue
        sc = max([name_score(n, name) for n in [s["name"], *s.get("aliases", [])]] +
                 [100.0 if slugify(s["name"]) == slugify(name) and name_core(s["name"])[0] == name_core(name)[0] else 0.0])
        if sc > best_sc:
            best, best_sc = s, sc
    return best if best_sc >= min_score else None


# ---------------------------------------------------------------- scheme records
def add_source(rec: dict, src: dict) -> None:
    if not any(x["url"] == src["url"] for x in rec["sources"]):
        rec["sources"].append(src)


def add_report(rec: dict, report: dict) -> None:
    """One entry per article; a second sentence from the same article only fills gaps."""
    reports = rec.setdefault("reports", [])
    same = next((r for r in reports if r["url"] == report["url"]), None)
    if same is None:
        reports.append(report)
        reports.sort(key=lambda r: r.get("published") or "", reverse=True)
        return
    for k, v in report.items():
        if same.get(k) in (None, "", "existing_scheme") and v not in (None, ""):
            same[k] = v


def upsert(schemes: list[dict], *, state: str, name: str, fy: str, amount: float | None, status: str,
           source: dict, govt_source: dict | None = None, note: str | None = None) -> dict:
    """`announced` only ever holds an amount confirmed by an official source (status verified / secondary).
    A news figure is kept as `reported_amount` and never shown or counted."""
    assert status in STATUSES, status
    rec = find_existing(schemes, state, name, fy)
    if rec is None:
        rec = {"id": f"{state}-{slugify(name)}", "state": state, "fy": fy, "name": name,
               "announced": None, "reported_amount": None, "despatched": None, "status": "reported",
               "sources": [], "first_seen": today(), "last_checked": today()}
        schemes.append(rec)
    else:
        rec["last_checked"] = today()
        if name != rec["name"] and name not in rec.setdefault("aliases", []):
            rec["aliases"].append(name)
    if status == "reported":
        if amount is not None:
            rec["reported_amount"] = amount
    # Never downgrade: a verified figure is only replaced by another verified one.
    elif status == "verified" or rec["status"] != "verified":
        rec["announced"] = amount
        rec["status"] = status
    add_source(rec, source)
    if govt_source:
        add_source(rec, govt_source)
    if note:
        rec["note"] = note
    return rec

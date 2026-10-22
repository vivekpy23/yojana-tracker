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


def _fold(word: str) -> str:
    """One spelling for common romanisation variants: Lakshmi / Laxmi, Kaushal / Kaushaal, Shree / Shri."""
    for a, b in (("ksh", "x"), ("aa", "a"), ("ee", "i"), ("oo", "u")):
        word = word.replace(a, b)
    return word


def name_core(name: str) -> tuple[str | None, list[str]]:
    """(prefix, distinctive words): 'Mukhyamantri Majhi Ladki Bahin Yojana' -> ('cm', ['majhi', 'ladki', 'bahin']).

    The prefix keeps a Prime Minister's scheme ('pm') apart from a Chief Minister's ('cm') of the same name –
    often the centrally sponsored scheme and the state's own one."""
    t = re.sub(r"\(.*?\)", " ", name.lower())
    t = re.sub(r"\b(?:pradhan ?mantri|prime minister'?s?|pm)\b", " pm ", t)
    t = re.sub(r"\b(?:mukhya ?mantri|chief minister'?s?|cm)\b|मुख्यमंत्री|முதலமைச்சர்|ಮುಖ್ಯಮಂತ್ರಿ", " cm ", t)
    t = re.sub(r"\b(?:" + _GENERIC + r")\b", " ", t)
    words = [w for w in re.split(r"[^a-z0-9ऀ-ॿ஀-௿ಀ-೿]+", t) if w and w not in _INDIC_GENERIC]
    words = [_fold(w) for w in words]
    prefixes = {w for w in words if w in ("pm", "cm")}
    core = [w for w in words if w not in ("pm", "cm")]
    return (prefixes.pop() if len(prefixes) == 1 else None), core


# Consonant skeletons compare a Devanagari name with its romanisation: 'महिला रोजगार' and 'Mahila Rojgar'
# (or 'Rozgar') are both 'mhl rjgr'. Vowels are dropped because romanisation spells them inconsistently.
_DEVA = dict(zip("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहळ",
                 ["k", "kh", "g", "gh", "n", "ch", "chh", "j", "jh", "n", "t", "th", "d", "dh", "n", "t", "th", "d",
                  "dh", "n", "p", "ph", "b", "bh", "m", "y", "r", "l", "v", "sh", "sh", "s", "h", "l"]))
_DEVA.update({"ं": "n", "ृ": "r", "ड़": "r", "ढ़": "rh", "ज़": "j", "फ़": "ph",
              "क़": "k", "ख़": "kh", "ग़": "g"})


def skeleton(word: str) -> str:
    if any("ऀ" <= ch <= "ॿ" for ch in word):
        word = "".join(_DEVA.get(ch, "") for ch in word)
    else:
        word = word.lower().replace("z", "j").replace("w", "v").replace("f", "ph").replace("x", "ksh")
        word = re.sub(r"[aeiou]", "", word)
    word = word.replace("chh", "ch").replace("sh", "s")
    return re.sub(r"(.)\1+", r"\1", word.replace("h", "") if len(word) > 2 else word)


def _latin(name: str) -> bool:
    return not re.search(r"[ऀ-෿]", name)


def better_name(current: str, new: str) -> bool:
    """Whether `new` should become a scheme's name: a romanised name over a regional-script one (the site is in
    English), and a name whose distinctive words are a strict subset of the current one's ('Mukhyamantri Mahila
    Rojgar Yojana' over 'Bihar Mahila Rojgar Yojana' or 'Mint View Market … Mahila Rojgar Yojana')."""
    if _latin(new) != _latin(current):
        return _latin(new)
    (pc, cc), (pn, cn) = name_core(current), name_core(new)
    return bool(cn) and set(cn) < set(cc) and (pn == pc or pn is not None)


def name_score(a: str, b: str) -> float:
    """0–100 similarity of two scheme names, after generic words are removed. A one-word name only
    matches the same word, so 'Pension Scheme' never swallows 'Destitute Women Pension Scheme'.
    A Devanagari name and a romanised one are compared by consonant skeleton and need two distinctive words."""
    (pa, ca), (pb, cb) = name_core(a), name_core(b)
    if not ca or not cb or (pa and pb and pa != pb):
        return 0.0
    if ca == cb:
        return 100.0
    if _latin(" ".join(ca)) != _latin(" ".join(cb)):
        sa, sb = [skeleton(w) for w in ca], [skeleton(w) for w in cb]
        small, big = sorted((sa, sb), key=len)
        if sa == sb:  # one distinctive word is enough only when both carry the same CM / PM prefix
            return 100.0 if len(sa) >= 2 or (pa and pa == pb and len(sa[0]) >= 3) else 0.0
        if len(small) < 2:
            return 0.0
        if set(small) <= set(big):  # 'मुख्यमंत्री महिला रोजगार योजना' within 'Bihar Mahila Rojgar Yojana'
            return 92.0
        return float(fuzz.ratio(" ".join(sa), " ".join(sb))) * 0.95
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
            if better_name(rec["name"], name):  # romanised / cleaner name wins; the old one stays an alias
                rec["aliases"].append(rec["name"])
                rec["name"] = name
            else:
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


# ---------------------------------------------------------------- financial claims (scheme records' `financials`)
# One entry per distinct figure: the same figure reported by several sources is one entry with several sources.
# A cumulative total is never added to transfers or to another total; see disbursement_summary.
def _same_figure(a: float, b: float, tol: float = 0.005) -> bool:
    return abs(a - b) <= max(tol * max(a, b), 0.5)


def _days_apart(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None if (a or b) else 0
    return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days)


def add_financial(rec: dict, claim: dict) -> bool:
    """Add a typed claim (claims.py / llm.py) to the record; True when it is a new figure. Two claims are the
    same figure when type and amount match and their dates are the same (a transfer reported a day later by
    another outlet is the same transfer: within 2 days)."""
    fin = rec.setdefault("financials", [])
    src = claim.get("source") or {}
    for f in fin:
        gap = _days_apart(f.get("as_of"), claim.get("as_of"))
        if f["type"] == claim["type"] and _same_figure(f["amount_cr"], claim["amount_cr"]) and \
                gap is not None and gap <= (2 if claim["type"] == "disbursement_event" else 0):
            if src.get("url") and not any(s.get("url") == src["url"] for s in f["sources"]):
                f["sources"].append(src)
            for k in ("beneficiaries", "period_start", "published"):
                if f.get(k) is None and claim.get(k) is not None:
                    f[k] = claim[k]
            if claim.get("official"):
                f["official"] = True
            return False
    entry = {k: claim.get(k) for k in ("type", "amount_cr", "beneficiaries", "fy", "as_of", "period_start",
                                       "published", "quote", "extractor")}
    entry.update(sources=[src] if src.get("url") else [], official=bool(claim.get("official")), verified=False,
                 verified_against=None, first_seen=today())
    fin.append(entry)
    return True


def _fy_start(fy: str) -> str:
    return f"{fy[:4]}-04-01"


def _fy_end(fy: str) -> str:
    return f"{int(fy[:4]) + 1}-03-31"


def disbursement_summary(rec: dict, fy: str) -> dict | None:
    """The scheme's benefits disbursed in `fy`, from verified figures only, never double-counted:
      1. the latest verified cumulative total dated inside the FY, when the total started inside the FY
         (the scheme was launched, or the first transfer the source mentions was made, on/after 1 April);
      2. otherwise the sum of verified one-day transfers inside the FY (each transfer once).
    Returns None when neither exists."""
    lo, hi = _fy_start(fy), _fy_end(fy)
    fin = [f for f in rec.get("financials", []) if f.get("verified") and f.get("as_of") and lo <= f["as_of"] <= hi]
    # Conflicting figures – two different amounts of one type, for one date, verified in the same official
    # document (CM Bihar, 10 Aug 2025: '124734 करोड़' in the headline, '1247.34 करोड़' in the text) – are left out
    # and listed in rec['financial_conflicts'] for a person to resolve; they are never summed or guessed between.
    groups: dict[tuple, list[dict]] = {}
    for f in fin:
        if f["type"] in ("disbursement_event", "cumulative_disbursement"):
            groups.setdefault((f["type"], f["as_of"], f.get("verified_against")), []).append(f)
    clash = [g for g in groups.values() if len({round(f["amount_cr"], 2) for f in g}) > 1]
    rec["financial_conflicts"] = [{"type": g[0]["type"], "as_of": g[0]["as_of"], "source": g[0]["verified_against"],
                                   "amounts_cr": sorted({f["amount_cr"] for f in g})} for g in clash]
    if not rec["financial_conflicts"]:
        del rec["financial_conflicts"]
    fin = [f for f in fin if not any(f in g for g in clash)]
    totals = [f for f in fin if f["type"] == "cumulative_disbursement"
              and (f.get("period_start") or rec.get("launched") or "") >= lo]
    if totals:
        best = max(totals, key=lambda f: (f["as_of"], f["amount_cr"]))
        start = best.get("period_start") or rec.get("launched")
        src = next((s for s in best["sources"] if s.get("url") == best.get("verified_against")), best["sources"][0])
        return {"amount": best["amount_cr"], "basis": "actuals", "kind": "cumulative", "as_of": best["as_of"],
                "period": f"{start} to {best['as_of']} (cumulative)", "beneficiaries": best.get("beneficiaries"),
                "url": src["url"], "title": src.get("title", "")}
    events = sorted((f for f in fin if f["type"] == "disbursement_event"), key=lambda f: f["as_of"])
    if events:
        src = events[-1]["sources"][0]
        return {"amount": round(sum(f["amount_cr"] for f in events), 2), "basis": "actuals", "kind": "transfers",
                "as_of": events[-1]["as_of"], "period": f"{events[0]['as_of']} to {events[-1]['as_of']} "
                f"(sum of {len(events)} transfer{'s' if len(events) > 1 else ''})",
                "beneficiaries": None, "url": src["url"], "title": src.get("title", "")}
    return None

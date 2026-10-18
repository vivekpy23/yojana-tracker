"""Read/write the canonical data files under data/."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from rapidfuzz import fuzz

from .extract import slugify

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SCHEMES = DATA / "schemes.json"
REVIEW = DATA / "review_queue.json"
REJECTED = DATA / "rejected.json"
SEED = DATA / "seed.json"


def today() -> str:
    return dt.date.today().isoformat()


def load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def find_existing(schemes: list[dict], state: str, name: str, fy: str, min_score: float = 88) -> dict | None:
    best, best_sc = None, 0.0
    for s in schemes:
        if s["state"] != state or s["fy"] != fy:
            continue
        sc = max(fuzz.token_set_ratio(s["name"].lower(), name.lower()),
                 100.0 if slugify(s["name"]) == slugify(name) else 0.0)
        if sc > best_sc:
            best, best_sc = s, sc
    return best if best_sc >= min_score else None


def add_source(rec: dict, src: dict) -> None:
    if not any(x["url"] == src["url"] for x in rec["sources"]):
        rec["sources"].append(src)


def upsert(schemes: list[dict], *, state: str, name: str, fy: str, amount: float, status: str,
           source: dict, govt_source: dict | None = None, note: str | None = None) -> dict:
    rec = find_existing(schemes, state, name, fy)
    if rec is None:
        rec = {"id": f"{state}-{slugify(name)}", "state": state, "fy": fy, "name": name,
               "announced": amount, "despatched": None, "status": status, "sources": [],
               "first_seen": today(), "last_checked": today()}
        schemes.append(rec)
    else:
        rec["last_checked"] = today()
        # Never downgrade: a verified figure is only replaced by another verified one.
        if status == "verified" or rec["status"] != "verified":
            rec["announced"] = amount
            rec["status"] = status
    add_source(rec, source)
    if govt_source:
        add_source(rec, govt_source)
    if note:
        rec["note"] = note
    return rec

"""Scheme catalog (data/catalog.json): every scheme discovered on myScheme (primary source), the National
Portal of India (secondary) and in the news, one entry per scheme however many names it appears under.

An entry holds identity and description only – name, aliases, state, department, description, funding type,
official source URLs. Amounts never come from here: an allocation reaches data/schemes.json only after it is
found in a government document and confirmed (see run.py). Backend only; the site shows catalog details just
for the schemes it already lists.

Funding types (`funding`, with the reason in `funding_evidence`)
  state                the state's own scheme – myScheme level "State", or named as a Chief Minister's scheme
  centrally_sponsored  Centre and state share the cost – the name matches the Union Budget's list of
                       Centrally Sponsored Schemes (`funding.centrally_sponsored` in config.yaml)
  central              a Central Government scheme not on that list (myScheme level "Central")
  unknown              no evidence either way – never guessed
A hand-set value in `funding.overrides` wins over all of these.
"""
from __future__ import annotations

import re

from .extract import slugify
from .store import name_core, name_score, today

FUNDING_TYPES = ("state", "centrally_sponsored", "central", "unknown")
MATCH_MIN = 90
# Which source's department / description wins when two disagree.
PRECEDENCE = {"myscheme": 3, "national_portal": 2, "prs": 2, "config": 1, "news": 0}


def find(catalog: list[dict], state: str, name: str, min_score: float = MATCH_MIN) -> dict | None:
    best, best_sc = None, 0.0
    for rec in catalog:
        if rec["state"] != state:
            continue
        sc = max(name_score(n, name) for n in [rec["name"], *rec.get("aliases", [])])
        if sc > best_sc:
            best, best_sc = rec, sc
    return best if best_sc >= min_score else None


def classify_funding(names: list[str], level: str | None, funding_cfg: dict) -> tuple[str, str]:
    for pattern in funding_cfg.get("centrally_sponsored", []):
        if any(re.search(pattern, n, re.I) for n in names):
            return "centrally_sponsored", f"name matches the Union Budget's Centrally Sponsored Scheme list ({pattern})"
    if level == "state":
        return "state", "myScheme lists it as a State scheme"
    if any(name_core(n)[0] == "cm" for n in names):
        return "state", "named as a Chief Minister's scheme"
    if level == "central":
        return "central", "myScheme lists it as a Central scheme (not on the Centrally Sponsored list)"
    return "unknown", ""


def _apply_funding(rec: dict, funding_cfg: dict) -> None:
    override = funding_cfg.get("overrides", {}).get(rec["id"])
    if override:
        rec["funding"], rec["funding_evidence"] = override["funding"], override["evidence"]
    else:
        rec["funding"], rec["funding_evidence"] = classify_funding([rec["name"], *rec.get("aliases", [])],
                                                                   rec.get("level"), funding_cfg)


def merge(catalog: list[dict], entry: dict, funding_cfg: dict) -> tuple[dict, bool]:
    """Add a discovery to the catalog. `entry`: state, name, via (myscheme | national_portal | news | config),
    and optionally aliases, department, description, level (state | central), source {title, url}.
    Returns (catalog entry, created)."""
    names = [entry["name"], *entry.get("aliases", [])]
    rec = next((r for n in names if (r := find(catalog, entry["state"], n))), None)
    created = rec is None
    if created:
        rid, n = f"{entry['state']}-{slugify(entry['name'])}", 2
        while any(r["id"] == rid for r in catalog):
            rid, n = f"{entry['state']}-{slugify(entry['name'])}-{n}", n + 1
        rec = {"id": rid, "state": entry["state"], "name": entry["name"], "aliases": [], "department": None,
               "description": None, "level": None, "official_url": None, "sources": [], "discovered_via": [],
               "meta_from": None, "first_seen": today()}
        catalog.append(rec)
    via = entry["via"]
    # A better-placed source renames the entry; the old name stays as an alias.
    if not created and PRECEDENCE[via] > PRECEDENCE.get(rec["meta_from"] or "news", 0) and entry["name"] != rec["name"]:
        rec["aliases"].append(rec["name"])
        rec["name"] = entry["name"]
    for n in names:
        if n != rec["name"] and n not in rec["aliases"]:
            rec["aliases"].append(n)
    if PRECEDENCE[via] >= PRECEDENCE.get(rec["meta_from"] or "news", 0) or not rec["meta_from"]:
        for k in ("department", "description", "level"):
            if entry.get(k):
                rec[k] = entry[k]
        rec["meta_from"] = via
    else:  # a lower-placed source may still fill a gap
        for k in ("department", "description", "level"):
            if entry.get(k) and not rec.get(k):
                rec[k] = entry[k]
    src = entry.get("source")
    if src and src.get("url"):
        if not any(s["url"] == src["url"] for s in rec["sources"]):
            rec["sources"].append({"kind": via, **src})
        if via in ("myscheme", "national_portal") and (
                not rec["official_url"] or via == "myscheme" and "myscheme.gov.in" not in rec["official_url"]):
            rec["official_url"] = src["url"]
    if via not in rec["discovered_via"]:
        rec["discovered_via"].append(via)
    rec["last_seen"] = today()
    _apply_funding(rec, funding_cfg)
    return rec, created


def seed_aliases(catalog: list[dict], aliases_cfg: dict, funding_cfg: dict) -> None:
    """Known alternative names (config.yaml → scheme_aliases), so differently-named reports deduplicate."""
    for state, schemes in (aliases_cfg or {}).items():
        for name, alts in schemes.items():
            merge(catalog, {"state": state, "name": name, "aliases": list(alts), "via": "config"}, funding_cfg)


def annotate(scheme: dict, entry: dict | None, funding_cfg: dict) -> None:
    """Copy catalog details onto a data/schemes.json record (or classify it by name when it has no entry)."""
    if entry:
        scheme["catalog_id"] = entry["id"]
        for k in ("department", "description", "official_url"):
            if entry.get(k):
                scheme[k] = entry[k]
        scheme["funding"], scheme["funding_evidence"] = entry["funding"], entry["funding_evidence"]
        return
    override = funding_cfg.get("overrides", {}).get(scheme["id"])
    if override:
        scheme["funding"], scheme["funding_evidence"] = override["funding"], override["evidence"]
    else:
        scheme["funding"], scheme["funding_evidence"] = classify_funding(
            [scheme["name"], *scheme.get("aliases", [])], None, funding_cfg)

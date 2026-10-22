"""Daily pipeline.

    python -m scraper.run                 # full run: discover -> verify -> merge -> build
    python -m scraper.run --build-only    # just rebuild site/data/site.json from data/
    python -m scraper.run --no-news       # skip news discovery, only re-verify what we have
    python -m scraper.run --no-discovery  # skip myScheme / National Portal discovery

Order of work:
 1. apply decisions you made in data/review_queue.json (approve / reject)
 2. scheme discovery → data/catalog.json: myScheme for every State/UT (primary), National Portal of India
    (secondary), known aliases from config.yaml; one entry per scheme however many names it has, with
    department, description, official link and funding type (state / centrally sponsored / central)
 3. news discovery: PIB, national and regional-language newspapers, state press-release pages. Each article
    becomes a report on its scheme (type: new scheme, existing scheme, revised allocation, disbursement update)
 4. load government documents for each jurisdiction that has sources configured
 5. verify every allocation reported for the site's FY, and every still-reported scheme
       verified  -> status verified (news figure confirmed)
       mismatch  -> status verified with the GOVERNMENT figure, note added
       review    -> queued for a human (fuzzy name match / several numbers nearby)
       not_found -> kept as 'reported' – "Reported — awaiting official verification", no amount shown
    Disbursement updates and other-FY stories are kept as reports only; their amounts are never an allocation.
 6. catalog schemes found verbatim in a government document next to a single amount -> review queue
 7. try to confirm headline seed figures against their govt_check documents
 8. PRS key-figure tables (reference BE/RE rows) and CAG Monthly Key Indicators (March report ->
    provisional actuals per state) – scraper/prs.py, scraper/cag.py
 9. write data/*.json and site/data/site.json; data-integrity rules (scraper/validate.py) decide
    which comparisons are valid and write data/validation.json
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import time
import logging
import os
import re
from pathlib import Path

import yaml

from . import build as build_mod
from . import cag, llm, myscheme, newsroom, portal, prs, search
from .checkpoint import Budget, Checkpoint
from .newsroom import main_text
from .registry import Registry
from . import catalog as catalog_mod
from .claims import extract_claims
from .extract import DISBURSEMENT, STATES, Candidate, _dominant_state, category, detect_states, extract_candidates, fy_of
from .fetch import Fetcher, doc_links, is_govt, to_text
from .news import RELEVANT, canonical_url, discover
from .store import (CATALOG, DATA, REJECTED, REVIEW, SCHEMES, SEED, add_financial, add_report, disbursement_summary,
                    find_existing, load, save, today, upsert)
from .store import add_source as store_add_source
from .verify import GovtDoc, find_allocation, verify, verify_claim

COVERAGE = DATA / "coverage.json"

log = logging.getLogger("scraper")
CONFIG = Path(__file__).with_name("config.yaml")


def indian_format(n: float) -> list[str]:
    """40,77,772 / 4077772 / 4,077,772 – the ways a crore figure is printed."""
    n = int(round(n))
    s = str(n)
    last3, rest = s[-3:], s[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    indian = ",".join(groups + [last3]) if groups else last3
    return [indian, s, f"{n:,}"]


# ---------------------------------------------------------------- review queue
def rejection_key(state, name, amount) -> str:
    return f"{state}|{name.lower()}|{round(float(amount or 0))}"


def apply_review_decisions(schemes: list[dict], fy: str) -> list[dict]:
    queue = load(REVIEW, [])
    rejected = load(REJECTED, [])
    remaining = []
    for item in queue:
        decision = (item.get("decision") or "").lower()
        amount = item.get("use_amount") or item.get("govt_amount") or item.get("news_amount")
        if decision == "approve" and amount is not None:
            govt = {"kind": "govt", "title": "Government document (approved in review)",
                    "url": item["govt_url"]} if item.get("govt_url") else None
            upsert(schemes, state=item["state"], name=item["name"], fy=item.get("fy", fy), amount=float(amount),
                   status="verified" if govt else "reported", source=item["news_source"], govt_source=govt,
                   note=f"Approved in review on {today()}")
        elif decision == "reject":
            rejected.append(rejection_key(item["state"], item["name"], item.get("news_amount") or item.get("govt_amount")))
        else:
            remaining.append(item)
    save(REJECTED, sorted(set(rejected)))
    return remaining


# ---------------------------------------------------------------- govt corpus
def load_govt_docs(fetcher: Fetcher, cfg: dict, code: str) -> list[GovtDoc]:
    src = cfg["govt_sources"].get(code, {})
    urls = list(src.get("direct_docs", []))
    for page in src.get("index_pages", []) + src.get("notification_pages", []):
        got = fetcher.get(page, max_age_h=24)
        if got:
            urls += doc_links(got[0], page, cfg["doc_keywords"], cfg["http"]["max_docs_per_source"])
    docs = []
    for url in dict.fromkeys(urls):
        if not is_govt(url, cfg["govt_domains"]):
            continue
        got = fetcher.get(url, max_age_h=24 * 7)
        if got:
            text = to_text(*got, url=url)
            if text.strip():
                docs.append(GovtDoc(url, text))
    if docs:
        log.info("%s: %d government documents loaded", code, len(docs))
    return docs


# ---------------------------------------------------------------- discovery
def myscheme_states(cfg: dict, seed: dict) -> dict[str, str]:
    names = {st["code"]: st["name"] for st in seed.get("states", [])}
    names.update(cfg.get("myscheme", {}).get("state_names") or {})
    return names


def discover_schemes(fetcher: Fetcher, cfg: dict, seed: dict, catalog: list[dict]) -> dict:
    funding = cfg.get("funding", {})
    ms_entries, skipped = myscheme.discover(fetcher, cfg.get("myscheme", {}), myscheme_states(cfg, seed))
    np_entries = portal.discover(fetcher, cfg.get("national_portal", {}))
    created = 0
    for e in ms_entries + np_entries:
        created += catalog_mod.merge(catalog, e, funding)[1]
    return {"myscheme": len(ms_entries), "myscheme_skipped": skipped, "national_portal": len(np_entries),
            "new": created}


# ---------------------------------------------------------------- main steps
def process_candidate(c: Candidate, schemes, queue, docs, cfg, fy, rejected, catalog) -> str:
    if c.confidence < 0.6:
        if c.amount is None:
            return "skipped-low-confidence"
        # The figure can't be tied to this scheme; the scheme itself is still kept, without an amount.
        c = dataclasses.replace(c, amount=None)
    funding = cfg.get("funding", {})
    recheck = c.extra.get("recheck", False)
    entry = catalog_mod.find(catalog, c.state, c.name)
    if entry is None and not recheck:
        entry = catalog_mod.merge(catalog, {"state": c.state, "name": c.name, "department": c.department or None,
                                            "via": "news"}, funding)[0]
    name = entry["name"] if entry else c.name
    if c.amount is not None and rejection_key(c.state, name, c.amount) in rejected:
        return "rejected-before"
    news_src = {"kind": "govt" if c.extra.get("govt_feed") else "news",
                "title": c.source_title or c.extra.get("feed", "News"), "url": c.source_url}
    rec_fy = c.fy or fy
    # Only an amount the article presents as an allocation, for the site's FY, can become `announced`.
    allocation = c.amount is not None and c.kind != DISBURSEMENT

    def keep(status="reported", amount=None, govt=None, note=None):
        rec = upsert(schemes, state=c.state, name=name, fy=rec_fy, amount=amount, status=status,
                     source=news_src, govt_source=govt, note=note)
        if c.source_url and not recheck:
            add_report(rec, c.report())
        if c.department and not rec.get("department"):
            rec["department"] = c.department
        rec.setdefault("category", category(f"{name}. {c.sentence}"))  # PRS's reading wins when it has one
        catalog_mod.annotate(rec, entry, funding)
        return rec

    if not allocation or rec_fy != fy:
        keep(amount=c.amount if allocation else None)
        return "reported-disbursement" if c.kind == DISBURSEMENT else "reported"
    if c.extra.get("govt_feed") and c.confidence >= 0.9:
        keep("verified", c.amount)
        return "verified"
    m = cfg["match"]
    v = verify(name, c.amount, docs, verified_min=m["verified_min_score"], review_min=m["review_min_score"],
               tol=m["amount_tolerance"], window=m["window_chars"])
    govt_src = {"kind": "govt", "title": "Government document", "url": v.govt_url} if v.govt_url else None
    if v.outcome == "verified":
        keep("verified", v.govt_amount, govt_src)
    elif v.outcome == "mismatch":
        keep("verified", v.govt_amount, govt_src,
             note=f"News reported ₹{c.amount:,.0f} Cr; government document shows ₹{v.govt_amount:,.0f} Cr (used).")
    elif v.outcome == "review":
        if not any(q["state"] == c.state and q["name"] == name for q in queue):
            queue.append({"kind": "news", "state": c.state, "fy": fy, "name": name, "news_amount": c.amount,
                          "news_source": news_src, "govt_amount": v.govt_amount, "govt_url": v.govt_url,
                          "match_score": round(v.score), "govt_snippet": v.snippet,
                          "news_sentence": c.sentence[:300], "queued_on": today(), "decision": None,
                          "use_amount": None})
        keep(amount=c.amount)
    else:
        keep(amount=c.amount)
    return v.outcome


# ---------------------------------------------------------------- web discovery: search + official newsrooms
def article_state(text: str, title: str, hint: str | None) -> str | None:
    """The state an article is about: the one its headline names, else the most mentioned one, else the state
    it was searched for – but only when the text mentions that state (a Bihar query can return an MP story)."""
    named = detect_states(title)
    if len(named) == 1:
        return named[0]
    top = _dominant_state(text)
    if len(top) == 1:
        return top[0]
    return hint if hint and any(n in text for n in STATES.get(hint, [])) else None


def record_for(schemes, catalog, funding, state: str, name: str, fy: str, src: dict, aliases=()) -> dict:
    """The scheme's record for `fy`, created (status reported, no amount) when the scheme is new: a scheme is
    kept whether or not any allocation is known."""
    entry = catalog_mod.find(catalog, state, name)
    for alias in aliases:
        entry = entry or catalog_mod.find(catalog, state, alias)
    if entry is None:
        entry = catalog_mod.merge(catalog, {"state": state, "name": name, "via": "news"}, funding)[0]
    rec = find_existing(schemes, state, entry["name"], fy) or next(
        (r for a in [name, *aliases] for r in [find_existing(schemes, state, a, fy)] if r), None)
    if rec is None:
        rec = upsert(schemes, state=state, name=entry["name"], fy=fy, amount=None, status="reported", source=src)
    else:
        store_add_source(rec, src)
    for n in [name, *aliases]:
        if n == rec["name"] or n in rec.setdefault("aliases", []):
            continue
        if find_existing(schemes, state, n, fy) is rec:  # upsert files it (a romanised name becomes the name)
            upsert(schemes, state=state, name=n, fy=fy, amount=None, status="reported", source=src)
        else:  # another wording of the same scheme in the same source (AI extraction)
            rec["aliases"].append(n)
    rec.setdefault("category", category(f"{rec['name']}. {rec.get('description') or ''}"))
    catalog_mod.annotate(rec, entry, funding)
    return rec


def ingest(text: str, *, url: str, title: str, published: str, state_hint: str | None, lang: str, official: bool,
           outlet: str, ctx: dict) -> dict:
    """One article / press release → scheme reports (extract_candidates → process_candidate) and typed financial
    claims (claims.py rules + optional AI extraction) on the scheme records. Returns what was found."""
    fy, schemes, catalog, funding = ctx["fy"], ctx["schemes"], ctx["catalog"], ctx["cfg"].get("funding", {})
    state = article_state(text, title, state_hint)
    found = {"url": url, "title": title[:160], "state": state, "official": official, "schemes": [], "ids": [],
             "claims": 0}
    if state is None or state not in ctx["states"]:
        return found
    src = {"kind": "govt" if official else "news", "title": title or outlet, "url": url}
    typed = extract_claims(text, title=title, published=published, state=state, source=src)
    budget = [c["amount_cr"] for c in typed if c["type"] == "budget_allocation"]
    flows = [c["amount_cr"] for c in typed if c["type"] in ("disbursement_event", "cumulative_disbursement")]
    for c in extract_candidates(text, url=url, title=title, published=published, fy=fy, state_hint=state, lang=lang,
                                as_of=today()):
        if c.state != state:
            continue
        # On a web page an amount is an allocation only when the claims extractor reads it as one; a transfer,
        # a total disbursed or a project cost ('769 करोड़ की योजनाओं का शिलान्यास') is never `announced`.
        if c.amount is not None and not any(abs(c.amount - b) <= 0.5 for b in budget):
            c = dataclasses.replace(c, amount=None, kind=DISBURSEMENT if any(abs(c.amount - f) <= 0.5 for f in flows)
                                    else c.kind)
        c.extra.update(feed=outlet, govt_feed=official)
        outcome = process_candidate(c, schemes, ctx["queue"], ctx["docs"].get(state, []), ctx["cfg"], fy,
                                    ctx["rejected"], catalog)
        ctx["tally"][outcome] = ctx["tally"].get(outcome, 0) + 1
        found["schemes"].append(c.name)
        rec = find_existing(schemes, state, c.name, c.fy or fy)
        if rec:
            found["ids"].append(rec["id"])
    ai_schemes, ai_claims = ctx["extractor"].extract(text, src)
    for s in ai_schemes:
        if (s.get("state") or state) == state:
            rec = record_for(schemes, catalog, funding, state, s["name"], fy, src, s["aliases"])
            if s.get("launched") and not rec.get("launched"):
                rec["launched"] = s["launched"]
    for cl in typed + ai_claims:
        if (cl.get("state") or state) != state:
            continue
        if not cl.get("fy"):
            # An undated earlier total ('इसके पूर्व … की जा चुकी है') is kept on a record that already exists,
            # never shown, and never the reason a record (or a record in another FY) is created.
            rec = next((r for f in (fy, fy_of(cl["published"]) if cl.get("published") else fy)
                        for r in [find_existing(schemes, state, cl["scheme"], f)] if r), None)
            if rec is None:
                continue
        else:
            rec = record_for(schemes, catalog, funding, state, cl["scheme"], cl["fy"], src)
        add_financial(rec, {**cl, "official": official})
        found["claims"] += 1
        found["schemes"].append(rec["name"])
        found["ids"].append(rec["id"])
    found["schemes"] = list(dict.fromkeys(found["schemes"]))
    found["ids"] = list(dict.fromkeys(found["ids"]))
    return found


def _jurisdiction_cov() -> dict:
    return {"queries": [], "pages_read": 0, "official_pages": 0, "failures": {}, "untrusted_skipped": 0,
            "already_seen": 0, "schemes_touched": [], "new_schemes": [], "claims_added": 0, "newsrooms": {},
            "verification_searches": [], "not_reached": False}


def discover_web(fetcher: Fetcher, ctx: dict, cp: Checkpoint, reg: Registry, deadline: float) -> dict:
    """Daily, incremental, search-first discovery for every jurisdiction in ctx['states']:
      1. registered official newsrooms (outside the search quota): new items, and backfill toward the FY start;
      2. one broad discovery query per jurisdiction per provider per day (search.daily_query), within each
         provider's budget; results already searched recently, pages already read, and pages that are neither
         official nor from a credible outlet are skipped;
      3. targeted verification searches (separate budget) for the largest unverified figures.
    Each page goes through ingest(). The checkpoint is saved after every jurisdiction, and the loop stops at
    `deadline` (what wasn't reached is logged), so a run always ends cleanly and the next one continues.
    Returns coverage per jurisdiction."""
    cfg, fy, schemes = ctx["cfg"], ctx["fy"], ctx["schemes"]
    scfg = cfg.get("search", {})
    provs, skipped = search.providers(scfg)
    budgets = {p.name: Budget(next(pc for pc in scfg["providers"] if pc["name"] == p.name), cp) for p in provs}
    cov = {"run": today(), "fy": fy, "providers": [p.name for p in provs], "providers_skipped": skipped,
           "ai_extraction": ctx["extractor"].disabled or ctx["extractor"].model, "jurisdictions": {}}
    day = dt.date.fromisoformat(cp.today)

    def read(url, title, published, code, lang, outlet, c, provider):
        got = fetcher.get(url, max_age_h=24 * 30)
        if not got:
            reason = fetcher.last_error or "not fetched"
            c["failures"][reason] = c["failures"].get(reason, 0) + 1
            return None
        cp.mark_seen(url, code)
        text = main_text(*got, url=url)
        official = is_govt(url, cfg["govt_domains"])
        home = (reg.jurisdiction_of(url) or code) if official else code
        if official:
            ctx["docs"].setdefault(home, []).append(GovtDoc(url, text))
            c["official_pages"] += 1
        c["pages_read"] += 1
        return ingest(text, url=canonical_url(url), title=title, published=published, state_hint=home, lang=lang,
                      official=official, outlet=outlet, ctx=ctx)

    def touched(c, found, code, via_search):
        ids = found.get("ids", [])
        c["schemes_touched"] = sorted(set(c["schemes_touched"]) | set(ids))
        c["claims_added"] += found.get("claims", 0)
        if via_search:
            cp.add_surfaced(found.get("state") or code, ids)

    for code in sorted(ctx["states"], key=lambda c: (c != "CENTRE", c)):
        c = cov["jurisdictions"][code] = _jurisdiction_cov()
        if time.time() > deadline:
            c["not_reached"] = True
            continue
        before = {s["id"] for s in schemes if s["state"] == code}
        # 1. official newsrooms – not counted against any search budget
        for room in reg.newsrooms(code):
            room_cov = c["newsrooms"].setdefault(room.get("name", room["listing"]), {})
            for it in newsroom.crawl(fetcher, room, fy, cp.newsroom(room["listing"]), room_cov):
                if cp.seen(it.url):
                    continue
                cp.mark_seen(it.url, code)
                ctx["docs"].setdefault(code, []).append(GovtDoc(it.url, it.text))
                c["official_pages"] += 1
                if RELEVANT.search(it.text):
                    touched(c, ingest(it.text, url=it.url, title=it.title, published=it.published, state_hint=code,
                                      lang=it.lang, official=True, outlet=room.get("name", ""), ctx=ctx), code, False)
        # 2. one discovery query per provider (budgeted)
        pages_left = scfg.get("max_pages_per_jurisdiction", 15)
        for p in provs:
            b = budgets[p.name]
            query, lang, theme = search.daily_query(scfg, code, day)
            entry = {"provider": p.name, "query": query, "lang": lang, "theme": theme}
            key = f"{p.name}|{query}"
            if not b.allow_discovery(code):
                c["queries"].append({**entry, "skipped": "budget (daily per jurisdiction or monthly cap)"})
                continue
            if cp.query_ran_recently(key, scfg.get("query_cache_days", 7)):
                c["queries"].append({**entry, "skipped": "same query ran recently"})
                continue
            results = p.search(fetcher, query, lang, scfg.get("results_per_query", 10), fresh=True)
            b.spend("discovery", code)
            if results is None:
                c["queries"].append({**entry, "failed": fetcher.last_error or "no response"})
                continue
            cp.mark_query(key)
            trusted = [r for r in results if is_govt(r["url"], cfg["govt_domains"]) or reg.is_credible(r["url"])]
            new = [r for r in trusted if not cp.seen(canonical_url(r["url"])) and not cp.seen(r["url"])]
            c["untrusted_skipped"] += len(results) - len(trusted)
            c["already_seen"] += len(trusted) - len(new)
            c["queries"].append({**entry, "results": len(results), "trusted": len(trusted), "new": len(new)})
            for r in new:
                if pages_left <= 0:
                    break
                if not is_govt(r["url"], cfg["govt_domains"]) and not RELEVANT.search(f"{r['title']} {r['snippet']}"):
                    continue
                pages_left -= 1
                found = read(r["url"], r["title"], r["published"], code, lang, urlparse_host(r["url"]), c, p.name)
                if found:
                    touched(c, found, code, True)
        c["new_schemes"] = sorted({s["id"] for s in schemes if s["state"] == code} - before)
        cp.save()  # checkpoint after every jurisdiction

    # 3. verification searches – separate budget, only providers with site:. First the free check against the
    # official pages already read, so a search is spent only on figures still unverified; one search per scheme
    # (it covers all the scheme's figures).
    verify_financials(schemes, ctx["docs"], cfg)
    vcfg = scfg.get("verification", {})
    targets = []
    for rec in schemes:
        domains = reg.domains(rec["state"])
        if rec["state"] not in ctx["states"] or rec["fy"] != fy or not domains:
            continue
        for f in rec.get("financials", []):
            if f["verified"] or not f.get("as_of") or f["amount_cr"] < vcfg.get("min_amount_cr", 50):
                continue
            k = f"{rec['id']}|{f['type']}|{f['amount_cr']}|{f['as_of']}"
            if cp.verification_due(k, vcfg.get("retry_days", 14)):
                targets.append((f["amount_cr"], k, rec, domains))
    searched: set[str] = set()
    for _, k, rec, domains in sorted(targets, key=lambda t: -t[0]):
        if rec["id"] in searched:
            cp.mark_verification(k)  # covered by this run's search for the scheme
            continue
        searched.add(rec["id"])
        p = next((p for p in provs if p.site_operator and budgets[p.name].allow_verification()), None)
        if p is None or time.time() > deadline:
            break
        c = cov["jurisdictions"][rec["state"]]
        query = search.verification_query(scfg, [rec["name"], *rec.get("aliases", [])], rec["state"], domains[0])
        if not query:
            continue
        results = p.search(fetcher, query, "en", scfg.get("results_per_query", 10))
        budgets[p.name].spend("verification")
        cp.mark_verification(k)
        official = [r for r in (results or []) if is_govt(r["url"], cfg["govt_domains"])]
        c["verification_searches"].append({"provider": p.name, "query": query, "for": k,
                                           "results": None if results is None else len(results),
                                           "official": len(official)})
        for r in official[:3]:
            if not cp.seen(r["url"]):
                found = read(r["url"], r["title"], r["published"], rec["state"], "hi", urlparse_host(r["url"]), c, p.name)
                if found:
                    touched(c, found, rec["state"], True)
    cov["budget_used"] = {p.name: {"today": {k: cp.used(p.name, k) for k in ("discovery", "verification")},
                                   "month": cp.used(p.name, "", month=True)} for p in provs}
    return cov


def coverage_report(cov: dict, schemes: list[dict], cp: Checkpoint, fy: str) -> dict:
    """Per jurisdiction: what was searched and read, failures, and – for evaluating the search budget – the
    welfare schemes on record for the FY that no search result has ever surfaced ('missed by search')."""
    for code, c in cov["jurisdictions"].items():
        mine = [s for s in schemes if s["state"] == code and s["fy"] == fy and s.get("category", "welfare") == "welfare"
                and s.get("kind", "scheme") == "scheme"]
        surfaced = cp.surfaced(code)
        missed = [s for s in mine if s["id"] not in surfaced]
        c["schemes_on_record"] = len(mine)
        c["surfaced_by_search"] = len(mine) - len(missed)
        c["missed_by_search"] = {"count": len(missed), "examples": [
            {"id": s["id"], "name": s["name"], "found_via": sorted({x.get("kind", "") for x in s.get("sources", [])})}
            for s in missed[:15]]}
        c["verified_figures"] = sum(f["verified"] for s in mine for f in s.get("financials", []))
        c["disbursed_shown"] = sum(1 for s in mine if s.get("despatched") is not None)
    hist = load(COVERAGE, {}).get("history", [])
    hist.append({"run": cov["run"], **{code: {k: c[k] for k in ("pages_read", "official_pages", "claims_added",
                                                               "surfaced_by_search", "schemes_on_record")}
                                       | {"new_schemes": len(c["new_schemes"]), "queries": len(c["queries"])}
                                       for code, c in cov["jurisdictions"].items()}})
    cov["history"] = hist[-60:]
    return cov


def _web_line(web: dict) -> str:
    if "skipped" in web:
        return web["skipped"]
    js = web["jurisdictions"]
    q = sum(1 for c in js.values() for x in c["queries"] if "results" in x)
    skipped_q = sum(1 for c in js.values() for x in c["queries"] if "skipped" in x or "failed" in x)
    rooms = sum(len(c["newsrooms"]) for c in js.values())
    prov_skipped = ", ".join(f"{k} – {v}" for k, v in web["providers_skipped"].items())
    return (f"providers {', '.join(web['providers']) or 'none'}"
            f"{' (skipped: ' + prov_skipped + ')' if prov_skipped else ''}; {q} searches run, {skipped_q} skipped or "
            f"failed; {sum(c['pages_read'] for c in js.values())} pages read, "
            f"{sum(c['official_pages'] for c in js.values())} official; {rooms} newsrooms; "
            f"{sum(len(c['new_schemes']) for c in js.values())} new scheme records; "
            f"{sum(len(c['verification_searches']) for c in js.values())} verification searches; "
            f"{sum(c['not_reached'] for c in js.values())} jurisdictions not reached; budget used {web['budget_used']}; "
            f"AI extraction: {web['ai_extraction']} (data/coverage.json)")


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc.removeprefix("www.")


def verify_financials(schemes: list[dict], docs: dict[str, list[GovtDoc]], cfg: dict) -> dict:
    """Check every financial claim against the official documents read this run (its state's newsroom items,
    government pages found by search, budget documents). A claim is verified only when an official document
    prints the same figure next to the scheme's name (any of its names, any script). Then set each scheme's
    disbursed figure from verified figures only (store.disbursement_summary)."""
    out = {"claims": 0, "verified": 0, "disbursed_set": 0}
    tol = cfg["match"]["amount_tolerance"]
    for rec in schemes:
        fins = rec.get("financials", [])
        names = [rec["name"], *rec.get("aliases", [])]
        for f in fins:
            out["claims"] += 1
            if not f["verified"] and f.get("as_of"):
                doc = verify_claim(f["amount_cr"], names, docs.get(rec["state"], []), tol=tol)
                if doc:
                    f.update(verified=True, verified_against=doc.url)
                    if not any(s.get("url") == doc.url for s in f["sources"]):
                        f["sources"].append({"kind": "govt", "title": "Government source", "url": doc.url})
            out["verified"] += f["verified"]
        summary = disbursement_summary(rec, rec["fy"]) if fins else None
        if summary:
            rec["despatched"] = summary["amount"]
            rec["despatched_source"] = {k: summary[k] for k in ("url", "title", "period", "as_of", "basis", "kind",
                                                                "beneficiaries")}
            out["disbursed_set"] += 1
        elif (rec.get("despatched_source") or {}).get("kind") in ("cumulative", "transfers"):
            rec["despatched"] = rec["despatched_source"] = None  # set by an earlier run, no longer supported
    return out


def match_catalog(code: str, catalog, schemes, queue, docs, fy, rejected, limit: int) -> int:
    """Catalog schemes without a confirmed allocation, looked up verbatim in the government documents.
    A single amount next to the name goes to the review queue – never straight to the site."""
    queued = 0
    for entry in (e for e in catalog if e["state"] == code):
        if queued >= limit:
            break
        rec = find_existing(schemes, code, entry["name"], fy)
        if rec and rec["status"] in ("verified", "secondary"):
            continue
        if any(q.get("catalog_id") == entry["id"] or (q["state"] == code and q["name"] == entry["name"]) for q in queue):
            continue
        v = find_allocation([entry["name"], *entry.get("aliases", [])], docs)
        if v.outcome != "review" or rejection_key(code, entry["name"], v.govt_amount) in rejected:
            continue
        official = entry.get("official_url")
        queue.append({"kind": "allocation", "state": code, "fy": fy, "name": entry["name"], "catalog_id": entry["id"],
                      "news_amount": None,
                      "news_source": {"kind": "govt", "title": f"myScheme – {entry['name']}" if official and
                                      "myscheme" in official else "Government document", "url": official or v.govt_url},
                      "govt_amount": v.govt_amount, "govt_url": v.govt_url, "match_score": round(v.score),
                      "govt_snippet": v.snippet, "news_sentence": "", "queued_on": today(), "decision": None,
                      "use_amount": None})
        queued += 1
    return queued


def save_prs_schemes(items: list[dict], schemes: list[dict], catalog: list[dict], funding: dict) -> dict:
    """PRS scheme items → data/schemes.json, one record per scheme (matched by name, as news reports are).
    A scheme is saved with or without an allocation: status 'secondary' (PRS's reading of the budget), with
    `announced` set only when PRS ties an amount to it. An existing amount is never cleared, and a verified
    record is never downgraded (store.upsert)."""
    out = {"new": 0, "updated": 0}
    for it in items:
        entry = catalog_mod.merge(catalog, {"state": it["state"], "name": it["name"], "via": "prs",
                                            "description": it["benefit"], "source": it["source"]}, funding)[0]
        rec = find_existing(schemes, it["state"], entry["name"], it["fy"])
        out["updated" if rec else "new"] += 1
        if rec and rec.get("announced") is not None and it["allocation"] is None:
            store_add_source(rec, it["source"])  # keep the amount already on record
        else:
            rec = upsert(schemes, state=it["state"], name=entry["name"], fy=it["fy"], amount=it["allocation"],
                         status="secondary", source=it["source"])
        if it["allocation_source"] and it["allocation_source"] != it["source"]["url"]:
            store_add_source(rec, {"kind": "prs", "title": it["source"]["title"] + " (PDF)",
                                   "url": it["allocation_source"]})
        rec.update(category=it["category"], kind=it["kind"], benefit=it["benefit"], prs_section=it["section"])
        catalog_mod.annotate(rec, entry, funding)
    return out


def confirm_seed(fetcher: Fetcher, cfg: dict) -> int:
    """Upgrade seed headline figures to 'verified' when the number appears in their govt_check document."""
    seed = load(SEED, {})
    upgraded = 0
    for key, fig in seed.get("central", {}).items():
        if not isinstance(fig, dict) or "govt_check" not in fig or fig.get("status") == "verified":
            continue
        got = fetcher.get(fig["govt_check"], max_age_h=24 * 7)
        if not got:
            continue
        text = re.sub(r"\s+", " ", to_text(*got, url=fig["govt_check"]))
        if any(f in text for f in indian_format(fig["value"])):
            fig["status"] = "verified"
            fig["verified_on"] = today()
            fig["source"] = {"title": "Union Budget document (indiabudget.gov.in)", "url": fig["govt_check"]}
            upgraded += 1
    if upgraded:
        save(SEED, seed)
    return upgraded


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-discovery", action="store_true")
    ap.add_argument("--no-prs", action="store_true")
    ap.add_argument("--no-search", action="store_true", help="skip web search and official newsrooms")
    ap.add_argument("--states", help="comma-separated state codes to process (default: all), e.g. BR")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.build_only:
        build_mod.build()
        return 0

    started = time.time()
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    fy = cfg["fy"]
    h = cfg["http"]
    fetcher = Fetcher(h["user_agent"], h["timeout_s"], h["delay_s"], max_per_host=h.get("max_requests_per_host"),
                      retries=h.get("retries", 2), backoff=h.get("retry_backoff_s", 2.0))
    funding = cfg.get("funding", {})
    schemes = load(SCHEMES, [])
    queue = apply_review_decisions(schemes, fy)
    rejected = set(load(REJECTED, []))

    catalog = load(CATALOG, [])
    catalog_mod.seed_aliases(catalog, cfg.get("scheme_aliases"), funding)
    disc = ({"myscheme": 0, "myscheme_skipped": "--no-discovery", "national_portal": 0, "new": 0}
            if args.no_discovery else discover_schemes(fetcher, cfg, load(SEED, {}), catalog))

    # PRS budget analyses of the FY: named schemes and welfare announcements for every state PRS covers.
    # News (next) adds what PRS doesn't name; both land on the same records.
    seed_states = [st["code"] for st in load(SEED, {}).get("states", [])]
    states = set(args.states.upper().split(",")) if args.states else set(seed_states) | {"CENTRE"}
    prs_items, prs_stats = ([], {"skipped": "--no-prs"}) if args.no_prs else prs.scheme_items(fetcher, cfg, fy)
    prs_items = [i for i in prs_items if i["state"] in states]
    prs_saved = save_prs_schemes(prs_items, schemes, catalog, funding)

    candidates = [] if args.no_news else discover(
        fetcher, cfg["news_feeds"], fy, announcement_pages=cfg.get("announcement_pages", []),
        announcement_keywords=cfg.get("announcement_keywords", []), govt_domains=cfg["govt_domains"],
        max_articles_per_source=h.get("max_articles_per_source", 40))
    # Re-check everything still reported with an amount, so news figures get confirmed when documents appear.
    for s in schemes:
        if s["fy"] == fy and s["status"] == "reported" and s.get("reported_amount") is not None:
            news = next((x for x in s["sources"] if x["kind"] == "news"), s["sources"][0])
            candidates.append(Candidate(s["state"], s["name"], s["reported_amount"], "", 0.9,
                                        news["url"], news["title"], fy=fy, extra={"recheck": True}))

    candidates = [c for c in candidates if c.state in states]
    tally: dict[str, int] = {}
    allocation_queued = 0
    with_sources = set(cfg["govt_sources"]) & states
    docs_by_state: dict[str, list[GovtDoc]] = {}
    for code in dict.fromkeys([c.state for c in candidates] + sorted(with_sources)):
        mine = [c for c in candidates if c.state == code]
        docs = docs_by_state[code] = load_govt_docs(fetcher, cfg, code) if code in with_sources else []
        for c in mine:
            outcome = process_candidate(c, schemes, queue, docs, cfg, fy, rejected, catalog)
            tally[outcome] = tally.get(outcome, 0) + 1
        if docs:
            allocation_queued += match_catalog(code, catalog, schemes, queue, docs, fy, rejected,
                                               cfg.get("discovery", {}).get("max_allocation_reviews_per_state", 10))
    # Web search (configurable providers) and official newsrooms; then every financial claim is checked
    # against the official documents read, and disbursed figures are set from verified claims only.
    extractor = llm.Extractor(cfg.get("ai_extraction", {}))
    ctx = {"cfg": cfg, "fy": fy, "states": states, "schemes": schemes, "catalog": catalog, "queue": queue,
           "rejected": rejected, "docs": docs_by_state, "tally": tally, "extractor": extractor}
    cp = Checkpoint.load()
    deadline = started + cfg.get("run", {}).get("time_budget_min", 35) * 60
    web = {"skipped": "--no-search"} if args.no_search else discover_web(fetcher, ctx, cp, Registry.load(), deadline)
    fin = verify_financials(schemes, docs_by_state, cfg)
    if not args.no_search:
        web["ai_extraction_stats"] = extractor.stats
        web["verification"] = fin
        save(COVERAGE, coverage_report(web, schemes, cp, fy))
        cp.save()
    for s in schemes:  # department, description, official link and funding type from the catalog
        catalog_mod.annotate(s, catalog_mod.find(catalog, s["state"], s["name"]), funding)
        s.setdefault("category", category(f"{s['name']}. {s.get('description') or ''}"))

    upgraded = confirm_seed(fetcher, cfg)
    seed = load(SEED, {})
    prs_problems = prs.update_reference(fetcher, seed, fy, today(), cfg)
    actuals = cag.update_actuals(fetcher, cfg, seed, fy, today())
    save(SEED, seed)
    for code, why in prs_problems.items():
        log.warning("PRS %s: %s", code, "; ".join(why))
    for code, why in actuals["unreadable"].items():
        log.info("CAG %s: %s", code, why)
    save(CATALOG, sorted(catalog, key=lambda e: (e["state"], e["name"])))
    save(SCHEMES, schemes)
    save(REVIEW, queue)
    site = build_mod.build()

    funding_counts: dict[str, int] = {}
    for e in catalog:
        funding_counts[e["funding"]] = funding_counts.get(e["funding"], 0) + 1
    summary = (f"### Yojana Tracker run {today()}\n\n"
               f"- Scheme discovery: myScheme {disc['myscheme']} listings"
               f"{' (' + disc['myscheme_skipped'] + ')' if disc['myscheme_skipped'] else ''}, "
               f"National Portal {disc['national_portal']}; catalog {len(catalog)} schemes "
               f"({disc['new']} new) by funding {funding_counts}\n"
               f"- PRS budget analyses ({fy}): {prs_stats.get('discovered', 0)} discovered, "
               f"{prs_stats.get('fetched', 0)} fetched ({prs_stats.get('pdf_read', 0)} PDFs read); "
               f"{prs_stats.get('extracted', 0)} schemes/announcements extracted "
               f"({prs_stats.get('with_allocation', 0)} with an allocation) by category "
               f"{prs_stats.get('by_category', {})}; saved {prs_saved['new']} new, {prs_saved['updated']} updated; "
               f"failed: {', '.join(prs_stats.get('failed', [])) or 'none'}"
               f"{'; ' + prs_stats['skipped'] if prs_stats.get('skipped') else ''}\n"
               f"- News candidates processed: {sum(tally.values())} {tally}\n"
               f"- Web discovery ({', '.join(sorted(states)) if args.states else 'all states'}): "
               f"{_web_line(web)}\n"
               f"- Financial claims: {fin['claims']} on record, {fin['verified']} verified against official sources; "
               f"disbursed figure set for {fin['disbursed_set']} schemes\n"
               f"- Catalog allocations found in govt documents, sent to review: {allocation_queued}\n"
               f"- Headline figures confirmed against govt documents: {upgraded}\n"
               f"- CAG provisional actuals ({fy}, March report): {len(actuals['filled'])} states/UTs filled; "
               f"no March report: {', '.join(actuals['no_report']) or 'none'}; "
               f"left Pending (failed cross-check / unreadable): {', '.join(actuals['unreadable']) or 'none'}\n"
               f"- PRS reference tables: {'problems in ' + ', '.join(prs_problems) if prs_problems else 'all read and consistent'}\n"
               f"- Validation (data/validation.json): {site['validation']['error']} errors, "
               f"{site['validation']['blocked']} comparisons shown as N/A, {site['validation']['missing']} missing\n"
               f"- Waiting for your review: {len(queue)}\n"
               f"- Schemes by status: {site['counts']}\n")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            # Data-integrity errors (not mere N/A comparisons) also need a human before publishing.
            f.write(f"needs_review={'true' if queue or site['validation']['error'] else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

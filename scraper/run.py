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
import logging
import os
import re
from pathlib import Path

import yaml

from . import build as build_mod
from . import cag, myscheme, portal, prs
from . import catalog as catalog_mod
from .extract import DISBURSEMENT, Candidate
from .fetch import Fetcher, doc_links, is_govt, to_text
from .news import discover
from .store import (CATALOG, REJECTED, REVIEW, SCHEMES, SEED, add_report, find_existing, load, save, today,
                    upsert)
from .verify import GovtDoc, find_allocation, verify

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
        return "skipped-low-confidence"
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
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.build_only:
        build_mod.build()
        return 0

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    fy = cfg["fy"]
    h = cfg["http"]
    fetcher = Fetcher(h["user_agent"], h["timeout_s"], h["delay_s"], max_per_host=h.get("max_requests_per_host"))
    funding = cfg.get("funding", {})
    schemes = load(SCHEMES, [])
    queue = apply_review_decisions(schemes, fy)
    rejected = set(load(REJECTED, []))

    catalog = load(CATALOG, [])
    catalog_mod.seed_aliases(catalog, cfg.get("scheme_aliases"), funding)
    disc = ({"myscheme": 0, "myscheme_skipped": "--no-discovery", "national_portal": 0, "new": 0}
            if args.no_discovery else discover_schemes(fetcher, cfg, load(SEED, {}), catalog))

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

    tally: dict[str, int] = {}
    allocation_queued = 0
    with_sources = set(cfg["govt_sources"])
    for code in dict.fromkeys([c.state for c in candidates] + sorted(with_sources)):
        mine = [c for c in candidates if c.state == code]
        docs = load_govt_docs(fetcher, cfg, code) if code in with_sources else []
        for c in mine:
            outcome = process_candidate(c, schemes, queue, docs, cfg, fy, rejected, catalog)
            tally[outcome] = tally.get(outcome, 0) + 1
        if docs:
            allocation_queued += match_catalog(code, catalog, schemes, queue, docs, fy, rejected,
                                               cfg.get("discovery", {}).get("max_allocation_reviews_per_state", 10))
    for s in schemes:  # department, description, official link and funding type from the catalog
        catalog_mod.annotate(s, catalog_mod.find(catalog, s["state"], s["name"]), funding)

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
               f"- News candidates processed: {sum(tally.values())} {tally}\n"
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

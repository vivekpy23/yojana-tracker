"""Daily pipeline.

    python -m scraper.run              # full run: news -> verify -> merge -> build
    python -m scraper.run --build-only # just rebuild site/data/site.json from data/
    python -m scraper.run --no-news    # skip discovery, only re-verify what we have

Order of work:
 1. apply decisions you made in data/review_queue.json (approve / reject)
 2. discover candidates in news feeds (PIB + national outlets)
 3. load government documents for each pilot jurisdiction
 4. verify every new candidate and every still-unverified scheme
       verified  -> status verified (news figure confirmed)
       mismatch  -> status verified with the GOVERNMENT figure, note added
       review    -> queued for a human (fuzzy name match / several numbers nearby)
       not_found -> kept as 'unverified' (shown with a badge, excluded from totals)
 5. try to confirm headline seed figures against their govt_check documents
 6. PRS key-figure tables (reference BE/RE rows) and CAG Monthly Key Indicators (March report ->
    provisional actuals per state) – scraper/prs.py, scraper/cag.py
 7. write data/*.json and site/data/site.json; data-integrity rules (scraper/validate.py) decide
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
from . import cag, prs
from .extract import Candidate
from .fetch import Fetcher, doc_links, is_govt, to_text
from .news import discover
from .store import REJECTED, REVIEW, SCHEMES, SEED, load, save, today, upsert
from .verify import GovtDoc, verify

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
        if decision == "approve":
            amount = item.get("use_amount") or item.get("govt_amount") or item["news_amount"]
            govt = {"kind": "govt", "title": "Government document (approved in review)",
                    "url": item["govt_url"]} if item.get("govt_url") else None
            upsert(schemes, state=item["state"], name=item["name"], fy=fy, amount=float(amount),
                   status="verified" if govt else "unverified", source=item["news_source"], govt_source=govt,
                   note=f"Approved in review on {today()}")
        elif decision == "reject":
            rejected.append(rejection_key(item["state"], item["name"], item["news_amount"]))
        else:
            remaining.append(item)
    save(REJECTED, sorted(set(rejected)))
    return remaining


# ---------------------------------------------------------------- govt corpus
def load_govt_docs(fetcher: Fetcher, cfg: dict, code: str) -> list[GovtDoc]:
    src = cfg["govt_sources"].get(code, {})
    urls = list(src.get("direct_docs", []))
    for page in src.get("index_pages", []):
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
    log.info("%s: %d government documents loaded", code, len(docs))
    return docs


# ---------------------------------------------------------------- main steps
def process_candidate(c: Candidate, schemes, queue, docs, cfg, fy, rejected) -> str:
    if rejection_key(c.state, c.name, c.amount) in rejected:
        return "rejected-before"
    news_src = {"kind": "govt" if c.extra.get("govt_feed") else "news",
                "title": c.source_title or c.extra.get("feed", "News"), "url": c.source_url}
    if c.extra.get("govt_feed") and c.confidence >= 0.9:
        upsert(schemes, state=c.state, name=c.name, fy=fy, amount=c.amount, status="verified", source=news_src)
        return "verified"
    if c.confidence < 0.6:
        return "skipped-low-confidence"
    m = cfg["match"]
    v = verify(c.name, c.amount, docs, verified_min=m["verified_min_score"], review_min=m["review_min_score"],
               tol=m["amount_tolerance"], window=m["window_chars"])
    govt_src = {"kind": "govt", "title": "Government document", "url": v.govt_url} if v.govt_url else None
    if v.outcome == "verified":
        upsert(schemes, state=c.state, name=c.name, fy=fy, amount=v.govt_amount, status="verified",
               source=news_src, govt_source=govt_src)
    elif v.outcome == "mismatch":
        upsert(schemes, state=c.state, name=c.name, fy=fy, amount=v.govt_amount, status="verified",
               source=news_src, govt_source=govt_src,
               note=f"News reported ₹{c.amount:,.0f} Cr; government document shows ₹{v.govt_amount:,.0f} Cr (used).")
    elif v.outcome == "review":
        if not any(q["state"] == c.state and q["name"] == c.name for q in queue):
            queue.append({"state": c.state, "name": c.name, "news_amount": c.amount, "news_source": news_src,
                          "govt_amount": v.govt_amount, "govt_url": v.govt_url, "match_score": round(v.score),
                          "govt_snippet": v.snippet, "news_sentence": c.sentence[:300],
                          "queued_on": today(), "decision": None, "use_amount": None})
        upsert(schemes, state=c.state, name=c.name, fy=fy, amount=c.amount, status="unverified", source=news_src)
    else:
        upsert(schemes, state=c.state, name=c.name, fy=fy, amount=c.amount, status="unverified", source=news_src)
    return v.outcome


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
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.build_only:
        build_mod.build()
        return 0

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    fy = cfg["fy"]
    h = cfg["http"]
    fetcher = Fetcher(h["user_agent"], h["timeout_s"], h["delay_s"])
    schemes = load(SCHEMES, [])
    queue = apply_review_decisions(schemes, fy)
    rejected = set(load(REJECTED, []))

    candidates = [] if args.no_news else discover(fetcher, cfg["news_feeds"], cfg["news_window"], fy)
    jurisdictions = cfg["pilot_states"] + ["CENTRE"]
    candidates = [c for c in candidates if c.state in jurisdictions]
    # Re-check everything still unverified, so seeded news figures get confirmed when documents appear.
    for s in schemes:
        if s["fy"] == fy and s["status"] == "unverified" and s["state"] in jurisdictions:
            news = next((x for x in s["sources"] if x["kind"] == "news"), s["sources"][0])
            candidates.append(Candidate(s["state"], s["name"], s["announced"], "", 0.9,
                                        news["url"], news["title"]))

    tally: dict[str, int] = {}
    for code in jurisdictions:
        mine = [c for c in candidates if c.state == code]
        if not mine:
            continue
        docs = load_govt_docs(fetcher, cfg, code)
        for c in mine:
            outcome = process_candidate(c, schemes, queue, docs, cfg, fy, rejected)
            tally[outcome] = tally.get(outcome, 0) + 1

    upgraded = confirm_seed(fetcher, cfg)
    seed = load(SEED, {})
    prs_problems = prs.update_reference(fetcher, seed, fy, today(), cfg)
    actuals = cag.update_actuals(fetcher, cfg, seed, fy, today())
    save(SEED, seed)
    for code, why in prs_problems.items():
        log.warning("PRS %s: %s", code, "; ".join(why))
    for code, why in actuals["unreadable"].items():
        log.info("CAG %s: %s", code, why)
    save(SCHEMES, schemes)
    save(REVIEW, queue)
    site = build_mod.build()

    summary = (f"### Yojana Tracker run {today()}\n\n"
               f"- Candidates processed: {sum(tally.values())} {tally}\n"
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
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            # Data-integrity errors (not mere N/A comparisons) also need a human before publishing.
            f.write(f"needs_review={'true' if queue or site['validation']['error'] else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

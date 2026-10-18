"""Discovery: read RSS feeds and pull scheme candidates out of relevant articles."""
from __future__ import annotations

import datetime as dt
import logging
import re

import feedparser

from .extract import Candidate, extract_candidates
from .fetch import Fetcher, to_text

log = logging.getLogger(__name__)
RELEVANT = re.compile(r"yojana|yojna|scheme|budget|crore|allocat|welfare|beneficiar|DBT|pension|subsid", re.I)


def _published(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.date(t.tm_year, t.tm_mon, t.tm_mday).isoformat()
    return ""


def discover(fetcher: Fetcher, feeds: list[dict], window: dict, fy: str) -> list[Candidate]:
    out: list[Candidate] = []
    for feed in feeds:
        got = fetcher.get(feed["url"], max_age_h=3)
        if not got:
            continue
        parsed = feedparser.parse(got[0])
        log.info("%s: %d items", feed["name"], len(parsed.entries))
        for e in parsed.entries:
            title, summary = e.get("title", ""), e.get("summary", "")
            pub = _published(e)
            if pub and not (window["from"] <= pub <= window["to"]):
                continue
            if not RELEVANT.search(title + " " + summary):
                continue
            art = fetcher.get(e.get("link", ""), max_age_h=24 * 30)
            if not art:
                continue
            text = to_text(*art, url=e.get("link", ""))
            cands = extract_candidates(text, url=e.get("link", ""), title=title, published=pub, fy=fy)
            for c in cands:
                c.extra["feed"] = feed["name"]
                c.extra["govt_feed"] = bool(feed.get("govt"))
            out.extend(cands)
    return out

"""Discovery from the news: PIB, national and regional-language newspapers (RSS), and state government
press-release pages.

What is kept from an article is the link, headline, outlet, date and the facts extracted from it (scheme,
state, article type, amount, benefit, beneficiaries, department) – never the article text. A feed marked
`fetch_article: false` is read from its RSS headline and summary only, for outlets whose terms don't allow
fetching the full page. Every request goes through Fetcher (robots.txt, Crawl-delay, per-host delay and cap).

Google News RSS is not used: news.google.com/robots.txt disallows /rss for all crawlers.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
from bs4 import BeautifulSoup

from .extract import Candidate, extract_candidates
from .fetch import Fetcher, doc_links, is_govt, to_text

log = logging.getLogger(__name__)
RELEVANT = re.compile(r"yojana|yojna|scheme|budget|crore|allocat|welfare|beneficiar|DBT|pension|subsid|"
                      r"योजना|योजने|करोड़|करोड|कोटी|लाभार्थी|திட்டம்|திட்ட|கோடி|ಯೋಜನೆ|ಕೋಟಿ", re.I)
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref$|ref_|cmp|from$)", re.I)


def canonical_url(url: str) -> str:
    """The same article reached via AMP, tracking parameters or a fragment is one article."""
    p = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k) and k != "amp"])
    path = re.sub(r"/amp/?$|/amp(?=/)|\.amp$", "", p.path) or "/"
    host = p.netloc.lower().removeprefix("amp.")
    return urlunparse((p.scheme or "https", host, path, "", query, ""))


def _published(entry) -> str:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.date(t.tm_year, t.tm_mon, t.tm_mday).isoformat()
    return ""


def _tag(cands: list[Candidate], source: dict) -> list[Candidate]:
    for c in cands:
        c.extra["feed"] = source["name"]
        c.extra["govt_feed"] = bool(source.get("govt"))
    return cands


def discover(fetcher: Fetcher, feeds: list[dict], fy: str, *, announcement_pages: list[dict] = (),
             announcement_keywords: list[str] = (), govt_domains: list[str] = (),
             max_articles_per_source: int = 40) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    read_on = dt.date.today().isoformat()
    for feed in feeds:
        if feed.get("disabled"):
            continue
        got = fetcher.get(feed["url"], max_age_h=3)
        if not got:
            continue
        parsed = feedparser.parse(got[0])
        log.info("%s: %d items", feed["name"], len(parsed.entries))
        used = 0
        for e in parsed.entries:
            title = (e.get("title") or "").strip()
            summary = BeautifulSoup(e.get("summary") or "", "html.parser").get_text(" ", strip=True)
            link = canonical_url(e.get("link") or "")
            if not link or link in seen or not RELEVANT.search(title + " " + summary):
                continue
            seen.add(link)
            if used >= max_articles_per_source:
                break
            used += 1
            text = f"{title}. {summary}"
            if feed.get("fetch_article", True):
                art = fetcher.get(e.get("link", ""), max_age_h=24 * 30)
                if art:
                    text = to_text(*art, url=link)
            out += _tag(extract_candidates(text, url=link, title=title, published=_published(e), fy=fy,
                                           state_hint=feed.get("state"), lang=feed.get("lang", "en"),
                                           as_of=read_on), feed)
    # State government announcements: press-release index pages, one level deep.
    for page in announcement_pages:
        got = fetcher.get(page["url"], max_age_h=6)
        if not got:
            continue
        links = [u for u in doc_links(got[0], page["url"], list(announcement_keywords), max_articles_per_source)
                 if is_govt(u, list(govt_domains)) and canonical_url(u) not in seen]
        log.info("%s: %d announcement links", page["name"], len(links))
        for url in links:
            seen.add(canonical_url(url))
            art = fetcher.get(url, max_age_h=24 * 30)
            if not art:
                continue
            out += _tag(extract_candidates(to_text(*art, url=url), url=canonical_url(url), title=page["name"],
                                           fy=fy, state_hint=page.get("state"), lang=page.get("lang", "en"),
                                           as_of=read_on),
                        {**page, "govt": True})
    return out

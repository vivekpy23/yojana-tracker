"""Official newsrooms from the shared registry (data/registry.yaml → jurisdictions.*.newsrooms), monitored every
run independently of the search quota.

Generic by default – a registry entry needs only the listing URL:
  - item URLs: the most common link pattern on the listing with one numeric part (viewnews.aspx?id=4454 →
    viewnews.aspx?id={id}), inferred once and kept in the checkpoint;
  - text: the page's main content (main_text): navigation, headers, footers, sidebars and widgets removed.
Adapter keys, only for a site where the generic way fails: item_url ('…{id}…'), content_selector, backfill, lang.

Incremental (state in the checkpoint, so it doesn't depend on the fetch cache):
  - new items: ids above the highest seen, from the listing;
  - backfill: listings often show only recent items (Bihar's CM site: the current CM's, from 15 Apr 2026) while
    older items stay online, linked from nowhere. The ids below the oldest listed are walked down to the FY start,
    at most `max_backfill_requests` per run, resuming where the last run stopped; `max_misses` empty ids in a row
    or the first item dated before the FY start end it for good.
Every request goes through Fetcher (robots.txt, per-host delay, retries, per-run cap).
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .claims import page_dates
from .fetch import Fetcher, to_text

log = logging.getLogger(__name__)
ITEM_CACHE_H = 24 * 365
_BOILERPLATE = re.compile(r"sidebar|widget|menu|navbar|nav-|breadcrumb|footer|header|recent|related|share|social|"
                          r"marquee|carousel|slider|slide|banner|cookie|subscribe|advert|ticker", re.I)


@dataclass
class Item:
    url: str
    title: str
    text: str
    published: str
    as_of: str
    newsroom: str
    lang: str


def main_text(body: bytes, ctype: str, url: str, selector: str | None = None) -> str:
    """The page's own content: `selector` when an adapter gives one, else the page without navigation, headers,
    footers, sidebars and widgets (by tag, and by class / id names)."""
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return to_text(body, ctype, url=url)
    soup = BeautifulSoup(body, "html.parser")
    if selector:
        node = soup.select_one(selector)
        if node is not None:
            return node.get_text(" ", strip=True)
    for t in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
        t.decompose()
    for t in soup.find_all(True):
        if t.attrs is None or t.name in ("html", "body", "form"):
            continue
        names = " ".join([t.get("id") or "", *(t.get("class") or [])])
        if names and _BOILERPLATE.search(names):
            t.decompose()
    return soup.get_text(" ", strip=True)


def infer_template(html: bytes, listing: str) -> tuple[str | None, list[int]]:
    """(item URL template with {id}, listed ids) from the listing's links: the most common same-site pattern with
    one run of digits, seen at least 5 times."""
    host = urlparse(listing).netloc
    found: dict[str, set[int]] = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(listing, a["href"]).split("#")[0]
        if urlparse(url).netloc != host:
            continue
        nums = list(re.finditer(r"\d{2,}", url.split("://", 1)[1]))
        if len(nums) != 1:
            continue
        m = nums[0]
        start = len(url.split("://", 1)[0]) + 3
        tpl = url[: start + m.start()] + "{id}" + url[start + m.end():]
        found.setdefault(tpl, set()).add(int(m.group()))
    best = max(found.items(), key=lambda kv: len(kv[1]), default=(None, set()))
    return (best[0], sorted(best[1])) if len(best[1]) >= 5 else (None, [])


def _item(fetcher: Fetcher, room: dict, url: str) -> Item | None:
    got = fetcher.get(url, max_age_h=ITEM_CACHE_H)
    if not got:
        return None
    text = main_text(*got, url=url, selector=room.get("content_selector"))
    published, as_of = page_dates(text)
    if not published:
        return None  # no item at this id
    title = re.split(r"(?<=[।.!?])\s", text[:300])[0]
    return Item(url, title, text, published, as_of, room.get("name", urlparse(url).netloc), room.get("lang", "en"))


def crawl(fetcher: Fetcher, room: dict, fy: str, state: dict, audit: dict) -> list[Item]:
    """New items since the last run, plus (backfill) older unlisted ones; `state` is this newsroom's checkpoint."""
    fy_start = f"{fy[:4]}-04-01"
    got = fetcher.get(room["listing"], max_age_h=room.get("listing_cache_h", 6))
    if not got:
        audit.update(listing="could not be fetched")
        return []
    inferred, ids = infer_template(got[0], room["listing"])
    tpl = room.get("item_url") or inferred or state.get("template")
    if not tpl or not ids:
        audit.update(listing="no item links recognised – add an item_url adapter to the registry entry")
        return []
    state["template"] = tpl
    prev_max = state.get("max_id", 0)
    new_ids = [i for i in ids if i > prev_max]
    audit.update(template=tpl, listing_ids=f"{ids[0]}–{ids[-1]}", new_listed=len(new_ids))
    items = [it for i in new_ids for it in [_item(fetcher, room, tpl.format(id=i))] if it]
    state["max_id"] = max([prev_max, *ids])

    if room.get("backfill", True) and not state.get("backfill_done"):
        i = state.get("backfill_next") or ids[0] - 1
        requests_made = misses = 0
        stop = None
        while i > 0:
            if requests_made >= room.get("max_backfill_requests", 150):
                stop = f"per-run cap of {requests_made} requests (resumes at id {i} next run)"
                break
            it = _item(fetcher, room, tpl.format(id=i))
            requests_made += 0 if fetcher.last_from_cache else 1
            if it is None:
                misses += 1
                if misses >= room.get("max_misses", 25):
                    stop, state["backfill_done"] = f"{misses} ids in a row without an item", True
                    break
            else:
                misses = 0
                if it.published < fy_start:
                    stop, state["backfill_done"] = f"id {i} is dated {it.published}, before the FY start", True
                    break
                items.append(it)
            i -= 1
        state["backfill_next"] = i
        audit.update(backfill_requests=requests_made, backfill_stop=stop or "reached id 0")
    keep = [it for it in items if it.published >= fy_start]
    audit.update(items_read=len(keep))
    return keep

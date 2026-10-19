"""National Portal of India (india.gov.in) – secondary discovery source.

Each configured listing page (config.yaml → national_portal.pages, per State/UT or CENTRE) is read for links
whose text is a scheme name. A scheme found here but not on myScheme is still catalogued; where both list it,
myScheme's name, department and description win. Like myScheme, the portal supplies no amounts.

india.gov.in answered "Access Denied" to this project's crawler in October 2026; a blocked page is logged and
skipped, and the run carries on.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .extract import SCHEME_SUFFIX, clean_scheme
from .fetch import Fetcher

log = logging.getLogger(__name__)
_NAME = re.compile(r"\b(?:" + SCHEME_SUFFIX + r")\b", re.I)


def scheme_links(body: bytes, base_url: str) -> list[tuple[str, str]]:
    """(scheme name, absolute URL) for every link on the page whose text looks like a scheme name."""
    soup = BeautifulSoup(body, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = clean_scheme(a.get_text(" ", strip=True))
        if not (2 <= len(text.split()) <= 14) or not _NAME.search(text):
            continue
        url = urljoin(base_url, a["href"])
        if url.startswith("http") and text.lower() not in seen:
            seen.add(text.lower())
            out.append((text, url))
    return out


def discover(fetcher: Fetcher, cfg: dict) -> list[dict]:
    entries = []
    for code, pages in (cfg.get("pages") or {}).items():
        for page in pages:
            got = fetcher.get(page, max_age_h=cfg.get("cache_h", 24 * 6))
            if not got:
                log.info("National Portal %s: %s not available", code, page)
                continue
            links = scheme_links(got[0], page)
            log.info("National Portal %s: %d schemes on %s", code, len(links), page)
            entries += [{"state": code, "name": name, "via": "national_portal",
                         "source": {"title": f"National Portal of India – {name}", "url": url}}
                        for name, url in links]
    return entries

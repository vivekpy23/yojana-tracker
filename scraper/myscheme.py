"""myScheme (myscheme.gov.in) – the primary discovery source: every scheme the portal lists for a State/UT,
with its nodal department, description and whether it is a State or a Central scheme.

The search page (myscheme.gov.in/search, filter "State") is drawn in the browser from myScheme's search
API, which needs an API key. The run calls that API only with authorised access: set MYSCHEME_API_KEY to a
key issued to this project (API Setu / the myScheme team). The key bundled in the portal's own JavaScript is
not used. Without a key this step is skipped and logged; nothing else depends on it.

myScheme carries no budget figures, so it never supplies an amount – only names, departments and links.
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import urlencode

from .fetch import Fetcher

log = logging.getLogger(__name__)
SCHEME_URL = "https://www.myscheme.gov.in/schemes/{slug}"


def _first(v):
    return v[0] if isinstance(v, list) and v else (v if not isinstance(v, list) else None)


def parse_search(payload: dict) -> tuple[list[dict], int]:
    """(schemes, total hits) from one page of the search API's JSON."""
    hits = ((payload.get("data") or {}).get("hits") or {})
    total = (hits.get("page") or {}).get("total") or 0
    out = []
    for item in hits.get("items") or []:
        f = item.get("fields") or {}
        name, slug = (f.get("schemeName") or "").strip(), f.get("slug")
        if not name or not slug:
            continue
        level = (f.get("level") or "").strip().lower()
        out.append({
            "name": name,
            "short_title": f.get("schemeShortTitle"),
            "url": SCHEME_URL.format(slug=slug),
            "description": (f.get("briefDescription") or "").strip() or None,
            "department": _first(f.get("nodalDepartmentName")) or _first(f.get("nodalMinistryName")),
            "level": level if level in ("state", "central") else None,
            "states": f.get("beneficiaryState") or [],
            "categories": f.get("schemeCategory") or [],
        })
    return out, int(total)


def search_state(fetcher: Fetcher, cfg: dict, state_name: str, api_key: str) -> list[dict]:
    """All schemes myScheme lists for one State/UT, page by page (cached for `cache_h`)."""
    q = json.dumps([{"identifier": "beneficiaryState", "value": state_name}])
    size, out, start = cfg.get("page_size", 50), [], 0
    while start < cfg.get("max_per_state", 1000):
        url = cfg["search_api"] + "?" + urlencode({"lang": "en", "q": q, "keyword": "", "sort": "",
                                                   "from": start, "size": size})
        got = fetcher.get(url, max_age_h=cfg.get("cache_h", 24 * 6), headers={"x-api-key": api_key})
        if not got:
            break
        try:
            items, total = parse_search(json.loads(got[0]))
        except (ValueError, AttributeError) as e:
            log.warning("myScheme %s: unreadable response (%s)", state_name, e)
            break
        out += items
        start += size
        if not items or start >= total:
            break
    return out


def discover(fetcher: Fetcher, cfg: dict, states: dict[str, str]) -> tuple[list[dict], str | None]:
    """Catalog entries for every State/UT in `states` ({code: name as myScheme spells it}).
    Returns (entries, reason skipped or None)."""
    key = os.environ.get(cfg.get("api_key_env", "MYSCHEME_API_KEY"))
    if not key:
        return [], f"skipped – no authorised API access ({cfg.get('api_key_env', 'MYSCHEME_API_KEY')} not set)"
    entries = []
    for code, name in states.items():
        found = search_state(fetcher, cfg, name, key)
        log.info("myScheme %s: %d schemes", code, len(found))
        for s in found:
            # A Central scheme listed for the state is catalogued once, under the Centre.
            entries.append({"state": code if s["level"] != "central" else "CENTRE", "name": s["name"],
                            "aliases": [a for a in [s["short_title"]] if a and len(a.split()) > 1],
                            "department": s["department"], "description": s["description"], "level": s["level"],
                            "source": {"title": f"myScheme – {s['name']}", "url": s["url"]}, "via": "myscheme"})
    return entries, None

"""Search-first discovery: configurable web search providers (config.yaml → search.providers) and query planning.

Providers, tried in order; each is skipped (and logged) when unavailable:
  brave       Brave Search API – the primary provider: web index, `site:`, freshness filter. Key in the env var
              named by `api_key_env`. Not cached on disk (the key stays out of .cache/); the checkpoint stops
              repeats.
  bing_news   Bing News RSS – keyless supplement for recent news (bing.com/robots.txt allows /news/search); no
              `site:` support, so never used for verification.
  google_cse  Google Programmable Search – only for an existing key: closed to new customers, shuts down 1 Jan 2027.
Google News RSS (robots.txt disallows /rss) and DuckDuckGo's HTML endpoint (bot challenge) are not used.

Budget (per provider): `discovery_per_jurisdiction` searches per jurisdiction per day (default 1),
`verification_per_day` targeted searches, `monthly_cap` for both together – see checkpoint.Budget.

Queries are generated, never written per scheme:
  daily        ONE broad query per jurisdiction per day from `search.daily.templates`: theme (new schemes /
               announcements / disbursements) × language, rotated by date and staggered across jurisdictions, so
               each jurisdiction cycles through every theme in every one of its languages. {name} is the
               jurisdiction's name in that language (Bihar / बिहार; Union government / केंद्र सरकार).
  verification `"{scheme}" site:{domain}` on the jurisdiction's registry domains, for an unverified figure.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import feedparser

from .extract import STATES

log = logging.getLogger(__name__)
_SCRIPT = {"hi": r"[ऀ-ॿ]", "mr": r"[ऀ-ॿ]", "ta": r"[஀-௿]", "kn": r"[ಀ-೿]"}


@dataclass
class Hit:
    url: str
    title: str
    published: str
    snippet: str
    provider: str
    query: str
    lang: str
    purpose: str


class Provider:
    name = ""
    site_operator = False

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def unavailable(self) -> str:
        """Why the provider can't run (missing key), or '' when it can."""
        return ""

    def search(self, fetcher, query: str, lang: str, n: int, fresh: bool = False) -> list[dict] | None:
        """Results as dicts (url, title, snippet, published); None when the request failed."""
        raise NotImplementedError


class BingNews(Provider):
    name = "bing_news"

    def search(self, fetcher, query, lang, n, fresh=False):
        url = "https://www.bing.com/news/search?" + urlencode({"q": query, "format": "rss", "setlang": lang, "cc": "IN"})
        got = fetcher.get(url, max_age_h=self.cfg.get("cache_h", 12))
        if not got:
            return None
        out = []
        for e in feedparser.parse(got[0]).entries[:n]:
            link = e.get("link") or ""
            real = parse_qs(urlparse(link).query).get("url", [link])[0]  # the article, not Bing's click-through
            t = e.get("published_parsed")
            out.append({"url": real, "title": (e.get("title") or "").strip(), "snippet": e.get("summary") or "",
                        "published": dt.date(t.tm_year, t.tm_mon, t.tm_mday).isoformat() if t else ""})
        return out


class BraveWeb(Provider):
    name = "brave"
    site_operator = True

    def _key(self):
        return os.environ.get(self.cfg.get("api_key_env", "BRAVE_API_KEY"))

    def unavailable(self):
        return "" if self._key() else f"no key in ${self.cfg.get('api_key_env', 'BRAVE_API_KEY')}"

    def search(self, fetcher, query, lang, n, fresh=False):
        params = {"q": query, "count": min(n, 20), "country": "IN"}
        if fresh and self.cfg.get("freshness"):
            params["freshness"] = self.cfg["freshness"]  # pd | pw | pm – only recent pages, for daily discovery
        data = fetcher.get_api("https://api.search.brave.com/res/v1/web/search", params=params,
                               headers={"Accept": "application/json", "X-Subscription-Token": self._key()})
        if data is None:
            return None
        return [{"url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("description", ""),
                 "published": (r.get("page_age") or "")[:10]} for r in (data.get("web") or {}).get("results", [])]


class GoogleCSE(Provider):
    name = "google_cse"
    site_operator = True

    def unavailable(self):
        missing = [v for v in (self.cfg.get("api_key_env", "GOOGLE_CSE_KEY"), self.cfg.get("cx_env", "GOOGLE_CSE_CX"))
                   if not os.environ.get(v)]
        return f"no value in ${', $'.join(missing)}" if missing else ""

    def search(self, fetcher, query, lang, n, fresh=False):
        params = {"key": os.environ[self.cfg.get("api_key_env", "GOOGLE_CSE_KEY")],
                  "cx": os.environ[self.cfg.get("cx_env", "GOOGLE_CSE_CX")], "q": query, "num": min(n, 10), "gl": "in"}
        if fresh:
            params["dateRestrict"] = "w1"
        data = fetcher.get_api("https://www.googleapis.com/customsearch/v1", params=params)
        if data is None:
            return None
        return [{"url": i.get("link", ""), "title": i.get("title", ""), "snippet": i.get("snippet", ""), "published": ""}
                for i in data.get("items", [])]


PROVIDERS = {p.name: p for p in (BraveWeb, BingNews, GoogleCSE)}


def providers(scfg: dict) -> tuple[list[Provider], dict[str, str]]:
    """Enabled providers that can run, in config order, and {name: reason} for the ones skipped."""
    ready, skipped = [], {}
    for pc in scfg.get("providers", []):
        cls = PROVIDERS.get(pc.get("name"))
        if cls is None:
            skipped[pc.get("name", "?")] = "unknown provider"
            continue
        if not pc.get("enabled", True):
            skipped[cls.name] = "disabled in config"
            continue
        p = cls(pc)
        why = p.unavailable()
        if why:
            skipped[cls.name] = why
        else:
            ready.append(p)
    return ready, skipped


def jurisdiction_name(scfg: dict, code: str, lang: str) -> str:
    own = (scfg.get("jurisdiction_names") or {}).get(code, {})
    if own:
        return own.get(lang) or own.get("en") or code
    names = STATES.get(code, [code])
    if lang in _SCRIPT:
        local = [n for n in names if re.search(_SCRIPT[lang], n)]
        if local:
            return local[0]
    return names[0]


def languages(scfg: dict, code: str) -> list[str]:
    return scfg.get("languages", {}).get(code) or scfg.get("languages", {}).get("default", ["en", "hi"])


def daily_query(scfg: dict, code: str, day: dt.date) -> tuple[str, str, str]:
    """(query, language, theme) for this jurisdiction today. The (theme, language) pairs are walked one per day,
    offset per jurisdiction, so on any one day the jurisdictions spread over all themes."""
    templates = scfg["daily"]["templates"]
    pairs = [(theme, lang) for theme in templates for lang in languages(scfg, code) if templates[theme].get(lang)]
    offset = sum(map(ord, code))
    theme, lang = pairs[(day.toordinal() + offset) % len(pairs)]
    return templates[theme][lang].format(name=jurisdiction_name(scfg, code, lang)), lang, theme


def scheme_name_for(lang: str, names: list[str]) -> str | None:
    """The scheme's name in the language's script (English: the romanised name)."""
    if lang in _SCRIPT:
        return next((n for n in names if re.search(_SCRIPT[lang], n)), None)
    return next((n for n in names if n.isascii()), None)


def verification_query(scfg: dict, names: list[str], code: str, domain: str) -> str | None:
    """`"<scheme>" site:<domain>` with the name in the jurisdiction's regional script when known (official
    releases are often in Hindi, Marathi, …), else the romanised name."""
    for lang in [l for l in languages(scfg, code) if l != "en"] + ["en"]:
        name = scheme_name_for(lang, names)
        if name:
            return scfg["verification"]["template"].format(scheme=name, domain=domain)
    return None

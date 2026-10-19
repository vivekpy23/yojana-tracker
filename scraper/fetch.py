"""Polite HTTP fetching with robots.txt checks, per-host delays and an on-disk cache."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"


class Fetcher:
    """`max_per_host` caps network requests to one host in a run (cache hits don't count); a host that
    answers 429 / 503 is left alone for the rest of the run. robots.txt Crawl-delay is honoured when it is
    longer than `delay`."""

    def __init__(self, user_agent: str, timeout: int = 30, delay: float = 2.0, cache_dir: Path = CACHE_DIR,
                 max_per_host: int | None = None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.ua = user_agent
        self.timeout = timeout
        self.delay = delay
        self.max_per_host = max_per_host
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_hit: dict[str, float] = {}
        self._hits: dict[str, int] = {}
        self._backed_off: set[str] = set()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # ---------- politeness ----------
    def allowed(self, url: str) -> bool:
        host = urlparse(url).scheme + "://" + urlparse(url).netloc
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(host + "/robots.txt", timeout=self.timeout)
                rp.parse(r.text.splitlines() if r.ok else [])
                self._robots[host] = rp
            except requests.RequestException:
                self._robots[host] = None  # unreachable robots.txt -> treat as allowed
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(self.ua, url)

    def _crawl_delay(self, url: str) -> float:
        rp = self._robots.get(urlparse(url).scheme + "://" + urlparse(url).netloc)
        try:
            return float((rp.crawl_delay(self.ua) if rp else None) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _wait(self, url: str):
        host = urlparse(url).netloc
        gap = time.time() - self._last_hit.get(host, 0)
        delay = max(self.delay, self._crawl_delay(url))
        if gap < delay:
            time.sleep(delay - gap)
        self._last_hit[host] = time.time()

    # ---------- fetching ----------
    def get(self, url: str, *, max_age_h: float = 20, headers: dict | None = None) -> tuple[bytes, str] | None:
        """Return (body, content_type) or None. Cached for `max_age_h` hours (headers are not cached)."""
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        body_p, meta_p = self.cache_dir / key, self.cache_dir / f"{key}.json"
        if body_p.exists() and meta_p.exists():
            meta = json.loads(meta_p.read_text())
            if time.time() - meta["at"] < max_age_h * 3600:
                return body_p.read_bytes(), meta["ctype"]
        host = urlparse(url).netloc
        if host in self._backed_off:
            return None
        if self.max_per_host is not None and self._hits.get(host, 0) >= self.max_per_host:
            if self._hits[host] == self.max_per_host:
                log.info("per-run request cap (%d) reached for %s", self.max_per_host, host)
                self._hits[host] += 1
            return None
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        self._wait(url)
        self._hits[host] = self._hits.get(host, 0) + 1
        try:
            r = self.s.get(url, timeout=self.timeout, headers=headers)
            if r.status_code in (429, 503):
                self._backed_off.add(host)
                log.warning("%s answered %d – no more requests to it this run", host, r.status_code)
                return None
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("fetch failed %s: %s", url, e)
            return None
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        body_p.write_bytes(r.content)
        meta_p.write_text(json.dumps({"url": url, "ctype": ctype, "at": time.time()}))
        return r.content, ctype


def to_text(body: bytes, ctype: str, url: str = "") -> str:
    """Plain text from an HTML page or a PDF (text layer only; scanned PDFs return little)."""
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        import pdfplumber
        try:
            with pdfplumber.open(io.BytesIO(body)) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception as e:  # malformed PDFs are common on department sites
            log.warning("pdf parse failed %s: %s", url, e)
            return ""
    soup = BeautifulSoup(body, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header"]):
        t.decompose()
    return soup.get_text(" ", strip=True)


def doc_links(body: bytes, base_url: str, keywords: list[str], limit: int = 25) -> list[str]:
    """Links on an index page that look like budget documents."""
    soup = BeautifulSoup(body, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        label = (a.get_text(" ", strip=True) + " " + href).lower()
        if any(k in label for k in keywords) and href not in out and href.startswith("http"):
            out.append(href)
        if len(out) >= limit:
            break
    return out


def is_govt(url: str, domains: list[str]) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host.endswith(d) for d in domains)

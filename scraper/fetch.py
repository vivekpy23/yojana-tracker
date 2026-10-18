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
    def __init__(self, user_agent: str, timeout: int = 30, delay: float = 2.0, cache_dir: Path = CACHE_DIR):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.ua = user_agent
        self.timeout = timeout
        self.delay = delay
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_hit: dict[str, float] = {}
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

    def _wait(self, url: str):
        host = urlparse(url).netloc
        gap = time.time() - self._last_hit.get(host, 0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_hit[host] = time.time()

    # ---------- fetching ----------
    def get(self, url: str, *, max_age_h: float = 20) -> tuple[bytes, str] | None:
        """Return (body, content_type) or None. Cached for `max_age_h` hours."""
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        body_p, meta_p = self.cache_dir / key, self.cache_dir / f"{key}.json"
        if body_p.exists() and meta_p.exists():
            meta = json.loads(meta_p.read_text())
            if time.time() - meta["at"] < max_age_h * 3600:
                return body_p.read_bytes(), meta["ctype"]
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        self._wait(url)
        try:
            r = self.s.get(url, timeout=self.timeout)
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

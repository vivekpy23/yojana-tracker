"""AI-assisted extraction (config.yaml → ai_extraction): Claude reads an unstructured article or press release in
any language and returns each scheme it reports on, with typed financial claims, as JSON (structured outputs).

Off unless `ai_extraction.enabled` is true and the `anthropic` package can find credentials (ANTHROPIC_API_KEY,
or an `ant auth login` profile). The rule-based extractor (claims.py) always runs as well; both feed the same
records, and add_financial merges a figure found by both.

Claude's answer is never trusted on its own:
  - every claim must quote the sentence it comes from, and the quote must be in the text (whitespace aside);
  - the amount must be one the deterministic parser (extract.find_amounts) reads in that quote;
  - a date must be a valid ISO date. Anything else is dropped and counted in `rejected`.
Results are cached per text (sha256) in .cache/llm/, so a page is sent once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from .claims import FIN_TYPES
from .extract import find_amounts, fy_of
from .fetch import CACHE_DIR

log = logging.getLogger(__name__)

SYSTEM = (
    "You extract facts about Indian government welfare schemes (yojanas) from news articles and government press "
    "releases, which may be in English, Hindi or another Indian language. Report only what the text states; never "
    "infer or compute figures. For each scheme the text reports on, give its name exactly as written, its usual "
    "English/romanised name (e.g. 'Mukhyamantri Mahila Rojgar Yojana' for 'मुख्यमंत्री महिला रोजगार योजना'), the "
    "two-letter Indian state code (e.g. BR for Bihar, or CENTRE for a central scheme), and the launch date if stated. "
    "For each rupee figure in crore tied to the scheme, classify it: budget_allocation (money set aside in a "
    "budget), revised_allocation (an allocation changed later, e.g. supplementary or revised estimates), "
    "disbursement_event (money transferred on one occasion), cumulative_disbursement (total transferred so far). "
    "Give amount_crore as a number in crore (18 हजार 100 करोड़ = 18100), the number of beneficiaries it covers if "
    "stated (1 करोड़ 81 लाख = 18100000), as_of (the date the figure is current to, YYYY-MM-DD; for 'today' in a press "
    "release use its dateline; null for an earlier total whose date the text doesn't give), period_start for a "
    "cumulative total if stated, and quote: the exact sentence the figure appears in, copied verbatim from the text."
)

_DATE = {"type": ["string", "null"], "description": "YYYY-MM-DD or null"}
SCHEMA = {
    "type": "object",
    "properties": {
        "published": _DATE,
        "schemes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name_as_written": {"type": "string"},
                "name_english": {"type": "string"},
                "state": {"type": "string"},
                "launch_date": _DATE,
                "claims": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(FIN_TYPES)},
                        "amount_crore": {"type": "number"},
                        "beneficiaries": {"type": ["integer", "null"]},
                        "as_of": _DATE,
                        "period_start": _DATE,
                        "quote": {"type": "string"},
                    },
                    "required": ["type", "amount_crore", "beneficiaries", "as_of", "period_start", "quote"],
                    "additionalProperties": False,
                }},
            },
            "required": ["name_as_written", "name_english", "state", "launch_date", "claims"],
            "additionalProperties": False,
        }},
    },
    "required": ["published", "schemes"],
    "additionalProperties": False,
}


def _flat(s: str) -> str:
    return " ".join(s.split())


def _date(v) -> str | None:
    return v if isinstance(v, str) and re.fullmatch(r"20\d\d-\d\d-\d\d", v) else None


def validate(data: dict, text: str, source: dict | None = None) -> tuple[list[dict], list[dict], int]:
    """(schemes, claims, rejected) from Claude's JSON, keeping only claims grounded in the text."""
    flat = _flat(text)
    schemes, claims, rejected = [], [], 0
    pub = _date(data.get("published"))
    for s in data.get("schemes", []):
        names = [n for n in (s.get("name_english"), s.get("name_as_written")) if n]
        if not names:
            continue
        schemes.append({"name": names[0], "aliases": names[1:], "state": s.get("state"),
                        "launched": _date(s.get("launch_date"))})
        for c in s.get("claims", []):
            quote = _flat(c.get("quote") or "")
            amounts = [a for _, a in find_amounts(quote)]
            amt = c.get("amount_crore")
            if not quote or quote not in flat or not isinstance(amt, (int, float)) or \
                    not any(abs(a - amt) <= max(0.005 * a, 0.5) for a in amounts):
                rejected += 1
                continue
            as_of = _date(c.get("as_of"))
            claim = {"type": c["type"], "scheme": names[0], "state": s.get("state"), "amount_cr": float(amt),
                     "beneficiaries": c.get("beneficiaries"), "as_of": as_of, "period_start": _date(c.get("period_start")),
                     "published": pub, "quote": quote[:400], "extractor": "llm",
                     "fy": fy_of(as_of) if as_of else None}
            if source:
                claim["source"] = source
            claims.append(claim)
    return schemes, claims, rejected


class Extractor:
    def __init__(self, cfg: dict, client=None):
        self.cfg = cfg or {}
        self.model = self.cfg.get("model", "claude-opus-5-5")
        self.client = client
        self.disabled = ""
        self.stats = {"calls": 0, "cached": 0, "claims": 0, "rejected": 0, "errors": 0}
        if client is None:
            if not self.cfg.get("enabled"):
                self.disabled = "ai_extraction.enabled is false"
                return
            if os.environ.get("ANTHROPIC_API_KEY") == "":  # an unset CI secret arrives as ''
                del os.environ["ANTHROPIC_API_KEY"]
            try:
                import anthropic
                self.client = anthropic.Anthropic()
            except Exception as e:  # package missing or no credentials
                self.disabled = f"Claude client unavailable ({type(e).__name__}: {e})"
        self.cache = CACHE_DIR / "llm"
        self.cache.mkdir(parents=True, exist_ok=True)

    def _call(self, text: str) -> dict | None:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": self.cfg.get("effort", "medium"),
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": f"<document>\n{text}\n</document>"}],
        )
        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning("Claude stopped with %s; page left to the rule-based extractor", resp.stop_reason)
            return None
        body = next((b.text for b in resp.content if b.type == "text"), None)
        return json.loads(body) if body else None

    def extract(self, text: str, source: dict | None = None) -> tuple[list[dict], list[dict]]:
        if self.disabled or not text.strip():
            return [], []
        if len(text) > self.cfg.get("max_chars", 100_000):
            log.info("page too long for AI extraction (%d chars) – rule-based only", len(text))
            return [], []
        key = hashlib.sha256((self.model + text).encode()).hexdigest()[:32]
        path = self.cache / f"{key}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.stats["cached"] += 1
        else:
            try:
                data = self._call(text)
            except Exception as e:  # one failed page must not stop the run
                self.stats["errors"] += 1
                log.warning("AI extraction failed: %s", e)
                if type(e).__name__ in ("AuthenticationError", "PermissionDeniedError"):
                    self.disabled = f"Claude credentials rejected ({type(e).__name__})"
                return [], []
            self.stats["calls"] += 1
            if data is None:
                return [], []
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        schemes, claims, rejected = validate(data, text, source)
        self.stats["claims"] += len(claims)
        self.stats["rejected"] += rejected
        return schemes, claims

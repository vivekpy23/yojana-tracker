"""Run-to-run state for incremental discovery (data/discovery_state.json), committed to main by every daily run
(also when the rest of the update waits in a review PR), so each run continues where the last one stopped:

  seen_urls     url -> [first seen, jurisdiction]: a page is read once (kept `seen_url_days`)
  queries       provider|query -> date it last ran: the same search isn't repeated within `query_cache_days`
  usage         provider -> date -> {discovery, verification}: daily budgets and the monthly cap
  newsrooms     listing URL -> {template, max_id, backfill_next, backfill_done}: newsroom progress
  verification  claim key -> date last searched: a figure is searched for at most every `retry_days`
  surfaced      jurisdiction -> record ids that search results have surfaced (for "missed by search")
"""
from __future__ import annotations

import datetime as dt

from .store import DATA, load, save

STATE = DATA / "discovery_state.json"


class Checkpoint:
    def __init__(self, data: dict | None = None, today: str | None = None, path=STATE):
        self.path = path
        self.d = {"version": 1, "seen_urls": {}, "queries": {}, "usage": {}, "by_jurisdiction": {}, "newsrooms": {},
                  "verification": {}, "surfaced": {}}
        self.d.update(data or {})
        self.today = today or dt.date.today().isoformat()

    @classmethod
    def load(cls, path=STATE, today: str | None = None) -> "Checkpoint":
        return cls(load(path, {}), today, path)

    def save(self, *, seen_url_days: int = 90, query_days: int = 30) -> None:
        def keep(date: str, days: int) -> bool:
            return (dt.date.fromisoformat(self.today) - dt.date.fromisoformat(date)).days <= days
        self.d["seen_urls"] = {u: v for u, v in self.d["seen_urls"].items() if keep(v[0], seen_url_days)}
        self.d["queries"] = {q: v for q, v in self.d["queries"].items() if keep(v, query_days)}
        self.d["verification"] = {k: v for k, v in self.d["verification"].items() if keep(v, query_days)}
        for k in ("usage", "by_jurisdiction"):
            self.d[k] = {p: {day: n for day, n in days.items() if keep(day, 40)} for p, days in self.d[k].items()}
        self.d["saved"] = self.today
        save(self.path, self.d)

    # ---- pages
    def seen(self, url: str) -> bool:
        return url in self.d["seen_urls"]

    def mark_seen(self, url: str, code: str) -> None:
        self.d["seen_urls"].setdefault(url, [self.today, code])

    # ---- searches
    def query_ran_recently(self, key: str, days: int) -> bool:
        last = self.d["queries"].get(key)
        return bool(last) and (dt.date.fromisoformat(self.today) - dt.date.fromisoformat(last)).days < days

    def mark_query(self, key: str) -> None:
        self.d["queries"][key] = self.today

    def used(self, provider: str, purpose: str, *, month: bool = False) -> int:
        days = self.d["usage"].get(provider, {})
        if month:
            return sum(sum(v.values()) for day, v in days.items() if day[:7] == self.today[:7])
        return days.get(self.today, {}).get(purpose, 0)

    def used_for(self, provider: str, code: str) -> int:
        """Discovery searches already made today for this jurisdiction (earlier runs included)."""
        return self.d["by_jurisdiction"].get(provider, {}).get(self.today, {}).get(code, 0)

    def spend(self, provider: str, purpose: str, code: str | None = None) -> None:
        day = self.d["usage"].setdefault(provider, {}).setdefault(self.today, {})
        day[purpose] = day.get(purpose, 0) + 1
        if code:
            by = self.d["by_jurisdiction"].setdefault(provider, {}).setdefault(self.today, {})
            by[code] = by.get(code, 0) + 1

    # ---- verification searches
    def verification_due(self, key: str, retry_days: int) -> bool:
        last = self.d["verification"].get(key)
        return not last or (dt.date.fromisoformat(self.today) - dt.date.fromisoformat(last)).days >= retry_days

    def mark_verification(self, key: str) -> None:
        self.d["verification"][key] = self.today

    # ---- coverage
    def surfaced(self, code: str) -> set[str]:
        return set(self.d["surfaced"].get(code, []))

    def add_surfaced(self, code: str, rec_ids) -> None:
        self.d["surfaced"][code] = sorted(self.surfaced(code) | set(rec_ids))

    def newsroom(self, listing: str) -> dict:
        return self.d["newsrooms"].setdefault(listing, {})


class Budget:
    """Search requests allowed this run, per provider and purpose, from config.yaml → search.providers:
    `discovery_per_jurisdiction` (per jurisdiction per day), `verification_per_day`, and `monthly_cap` (both
    purposes together). Usage is read from and written to the checkpoint, so a second run on the same day
    doesn't search again."""

    def __init__(self, provider_cfg: dict, cp: Checkpoint):
        self.cfg, self.cp, self.name = provider_cfg, cp, provider_cfg["name"]

    def _under_cap(self) -> bool:
        cap = self.cfg.get("monthly_cap")
        return cap is None or self.cp.used(self.name, "", month=True) < cap

    def allow_discovery(self, code: str) -> bool:
        return self.cp.used_for(self.name, code) < self.cfg.get("discovery_per_jurisdiction", 1) and self._under_cap()

    def allow_verification(self) -> bool:
        return self.cp.used(self.name, "verification") < self.cfg.get("verification_per_day", 0) and self._under_cap()

    def spend(self, purpose: str, code: str | None = None) -> None:
        self.cp.spend(self.name, purpose, code)

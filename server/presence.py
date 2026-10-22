"""Live visitor count ("X people online now") – sessions with a heartbeat in the last 5 minutes.

The browser makes a random, anonymous session id (sessionStorage: it survives a refresh, ends with the tab) and
sends it with a heartbeat every ~45 s while the page is visible. The store keeps only {session id: last heartbeat
time}, nothing else – no IP address, user agent or history – and forgets a session 5 minutes after its last
heartbeat.

Stores:
  RedisStore   for production and for several server instances: one sorted set (member = session id,
               score = heartbeat time from Redis's own clock, so instance clocks don't matter). Each heartbeat runs
               ZADD + ZREMRANGEBYSCORE + ZCARD + EXPIRE in one transaction. Set REDIS_URL.
  MemoryStore  one process only (local development, tests); not shared between instances.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

WINDOW_S = 300                       # a session counts as online for 5 minutes after its last heartbeat
SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
KEY = "yojana:presence"


def valid_session(sid) -> bool:
    return isinstance(sid, str) and bool(SESSION_ID.match(sid))


class MemoryStore:
    def __init__(self, window: float = WINDOW_S, clock=time.time):
        self.window, self.clock = window, clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for sid in [s for s, t in self._seen.items() if now - t > self.window]:
            del self._seen[sid]

    def heartbeat(self, sid: str) -> int:
        with self._lock:
            now = self.clock()
            self._seen[sid] = now
            self._prune(now)
            return len(self._seen)

    def count(self) -> int:
        with self._lock:
            self._prune(self.clock())
            return len(self._seen)


class RedisStore:
    def __init__(self, client, window: float = WINDOW_S, key: str = KEY):
        self.r, self.window, self.key = client, window, key

    @classmethod
    def from_url(cls, url: str) -> "RedisStore":
        import redis  # server/requirements.txt
        return cls(redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2))

    def _now(self) -> float:
        sec, usec = self.r.time()  # one clock for every instance
        return sec + usec / 1e6

    def heartbeat(self, sid: str) -> int:
        now = self._now()
        p = self.r.pipeline(transaction=True)
        p.zadd(self.key, {sid: now})
        p.zremrangebyscore(self.key, "-inf", now - self.window)
        p.zcard(self.key)
        p.expire(self.key, int(self.window * 2))  # the whole set disappears once nobody is online
        return int(p.execute()[2])

    def count(self) -> int:
        now = self._now()
        p = self.r.pipeline(transaction=True)
        p.zremrangebyscore(self.key, "-inf", now - self.window)
        p.zcard(self.key)
        return int(p.execute()[1])


def from_env():
    """RedisStore when REDIS_URL is set; otherwise a MemoryStore, with a warning that it isn't shared."""
    url = os.environ.get("REDIS_URL")
    if url:
        return RedisStore.from_url(url)
    log.warning("REDIS_URL not set: the online count is kept in this process only (not shared between instances)")
    return MemoryStore()

"""Feedback endpoint for the Yojana Tracker homepage – Python standard library only.

    python -m server.feedback_server                 # http://127.0.0.1:8000  (site + POST /api/feedback)
    PORT=8080 HOST=0.0.0.0 python -m server.feedback_server

Serves site/ and accepts POST /api/feedback with JSON {"message": "..."}. Each submission is appended to
data/feedback.json (outside site/, so never published) as {"id", "message", "timestamp"}.

Settings (environment):
  FEEDBACK_FILE          where submissions go (default data/feedback.json)
  FEEDBACK_ALLOW_ORIGIN  the site's origin when it is served from elsewhere (e.g. https://<user>.github.io),
                         so the browser may post here; unset = same origin only
  TRUST_PROXY=1          take the client address from X-Forwarded-For (only behind your own reverse proxy)

Safety: body ≤ 4 KB, JSON only; message 3–1000 characters after trimming, control characters removed;
5 submissions per client per 10 minutes; writes are serialised (a lock in this process plus a lock file
for other processes) and atomic (temp file + rename), so concurrent posts never lose or corrupt entries.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
FEEDBACK_FILE = Path(os.environ.get("FEEDBACK_FILE", ROOT / "data" / "feedback.json"))

MAX_BODY = 4096
MIN_LEN, MAX_LEN = 3, 1000
RATE_LIMIT, RATE_WINDOW_S = 5, 600
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ---------------------------------------------------------------- validation
def clean_message(raw) -> tuple[str | None, str | None]:
    """(message, None) when acceptable, else (None, reason)."""
    if not isinstance(raw, str):
        return None, "Please enter a message."
    msg = _CONTROL.sub("", raw).strip()
    if len(msg) < MIN_LEN:
        return None, "Please enter a message."
    if len(msg) > MAX_LEN:
        return None, f"Please keep your message under {MAX_LEN} characters."
    return msg, None


# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """At most `limit` hits per key in a sliding `window` of seconds."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW_S):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


# ---------------------------------------------------------------- storage
_write_lock = threading.Lock()


@contextmanager
def _file_lock(path: Path):
    """An exclusive lock on <path>.lock, for writers in other processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(path.suffix + ".lock"), "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:  # LK_LOCK gives up after ~10 s; keep waiting
                    continue
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def append_feedback(message: str, path: Path | None = None) -> dict:
    """Append one entry and return it. Existing entries are read and written back unchanged."""
    path = path or FEEDBACK_FILE
    entry = {"id": uuid.uuid4().hex, "message": message,
             "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    with _write_lock, _file_lock(path):
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() and path.stat().st_size else []
        entries.append(entry)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)  # atomic: readers see the old file or the new one, never half of it
    return entry


# ---------------------------------------------------------------- HTTP
class Handler(SimpleHTTPRequestHandler):
    limiter = RateLimiter()
    feedback_file: Path | None = None
    allow_origin = os.environ.get("FEEDBACK_ALLOW_ORIGIN", "")
    trust_proxy = os.environ.get("TRUST_PROXY") == "1"

    def _client(self) -> str:
        if self.trust_proxy and self.headers.get("X-Forwarded-For"):
            return self.headers["X-Forwarded-For"].split(",")[0].strip()
        return self.client_address[0]

    def _cors(self):
        if self.allow_origin and self.headers.get("Origin") == self.allow_origin:
            self.send_header("Access-Control-Allow-Origin", self.allow_origin)
            self.send_header("Vary", "Origin")

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):  # CORS preflight
        if self.path != "/api/feedback":
            return self._json(404, {"error": "Not found."})
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/feedback":
            return self._json(404, {"error": "Not found."})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(415, {"error": "Send JSON."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            return self._json(413 if length > MAX_BODY else 400, {"error": "Message too long or empty."})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"error": "Send JSON."})
        msg, why = clean_message(payload.get("message") if isinstance(payload, dict) else None)
        if why:
            return self._json(400, {"error": why})
        if not self.limiter.allow(self._client()):
            return self._json(429, {"error": "Too many messages. Please try again in a few minutes."})
        entry = append_feedback(msg, self.feedback_file)
        self._json(201, {"ok": True, "id": entry["id"]})

    def log_message(self, fmt, *args):  # no message text in logs
        if not self.path.startswith("/api/"):
            super().log_message(fmt, *args)


def serve(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), partial(Handler, directory=str(SITE)))


if __name__ == "__main__":
    srv = serve(os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000")))
    print(f"Serving site/ and POST /api/feedback on http://{srv.server_address[0]}:{srv.server_address[1]}"
          f", saving to {FEEDBACK_FILE}")
    srv.serve_forever()

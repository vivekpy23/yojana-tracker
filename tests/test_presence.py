"""Live visitor count: 5-minute window, one count per session (refreshes don't add), shared across instances
through the store, nothing personal stored, and an error (never a made-up number) when the store is down."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from server import feedback_server as fs
from server import presence


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeRedis:
    """The few Redis commands RedisStore uses, on one shared sorted set per key, with the server's own clock."""

    def __init__(self, clock):
        self.clock, self.sets, self.ttl = clock, {}, {}

    def time(self):
        return int(self.clock()), int((self.clock() % 1) * 1e6)

    def pipeline(self, transaction=True):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def zadd(self, key, mapping):
        self.ops.append(lambda: self.r.sets.setdefault(key, {}).update(mapping) or len(mapping))

    def zremrangebyscore(self, key, lo, hi):
        def op():
            z = self.r.sets.get(key, {})
            gone = [m for m, s in z.items() if s <= hi]
            for m in gone:
                del z[m]
            return len(gone)
        self.ops.append(op)

    def zcard(self, key):
        self.ops.append(lambda: len(self.r.sets.get(key, {})))

    def expire(self, key, seconds):
        self.ops.append(lambda: self.r.ttl.__setitem__(key, seconds) or True)

    def execute(self):
        return [op() for op in self.ops]


SID_A, SID_B, SID_C = "a" * 32, "b" * 32, "c" * 32


@pytest.mark.parametrize("make", ["memory", "redis"])
def test_counts_sessions_active_in_the_last_five_minutes(make):
    clock = Clock()
    store = presence.MemoryStore(clock=clock) if make == "memory" else presence.RedisStore(FakeRedis(clock))
    assert store.heartbeat(SID_A) == 1
    assert store.heartbeat(SID_A) == 1                     # a refresh: same session, still one person
    clock.t += 60
    assert store.heartbeat(SID_B) == 2
    clock.t += 250                                         # A last seen 310 s ago, B 250 s ago
    assert store.count() == 1
    clock.t += 60
    assert store.count() == 0


def test_two_instances_share_one_count_through_redis():
    clock = Clock()
    backend = FakeRedis(clock)                              # one Redis, two app instances
    one, two = presence.RedisStore(backend), presence.RedisStore(backend)
    one.heartbeat(SID_A)
    two.heartbeat(SID_B)
    assert one.count() == two.count() == 2
    assert backend.ttl[presence.KEY] == 600                 # the key itself expires when nobody is online
    assert set(backend.sets[presence.KEY]) == {SID_A, SID_B}   # only random ids and times are stored


def test_session_ids_must_be_random_hex_tokens():
    assert presence.valid_session("0123456789abcdef0123456789abcdef")
    for bad in ("192.168.1.1", "x" * 32, "a" * 31, None, 42, "A" * 32):
        assert not presence.valid_session(bad)


@pytest.fixture
def server():
    clock = Clock()
    store = presence.RedisStore(FakeRedis(clock))
    fs.Handler.presence_limiter = fs.RateLimiter(limit=1000, window=60)
    srv = fs.serve("127.0.0.1", 0, store=store)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, store, clock
    srv.shutdown()
    srv.server_close()


def call(srv, method="GET", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/presence", method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), r.headers.get("Cache-Control")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), None


def test_heartbeat_and_count_over_http(server):
    srv, store, clock = server
    assert call(srv, "POST", {"sid": SID_A})[:2] == (200, {"online": 1})
    assert call(srv, "POST", {"sid": SID_A})[:2] == (200, {"online": 1})    # refresh
    assert call(srv, "POST", {"sid": SID_B})[:2] == (200, {"online": 2})
    status, body, cache = call(srv)
    assert (status, body, cache) == (200, {"online": 2}, "no-store")
    assert set(store.r.sets[presence.KEY]) == {SID_A, SID_B}                # no address, no user agent
    clock.t += 301
    assert call(srv)[1] == {"online": 0}                                    # real activity only
    assert store.r.sets[presence.KEY] == {}                                 # expired sessions are deleted
    assert call(srv, "POST", {"sid": "not-a-session"})[0] == 400


def test_store_down_means_an_error_not_a_number(server):
    srv, store, _ = server

    def down(*a, **k):
        raise ConnectionError("redis unreachable")
    store.r.time = down
    assert call(srv, "POST", {"sid": SID_C})[:2] == (503, {"error": "Count unavailable."})
    assert call(srv)[0] == 503


def test_one_address_cannot_invent_unlimited_sessions(server):
    srv, _, _ = server
    fs.Handler.presence_limiter = fs.RateLimiter(limit=3, window=60)
    codes = [call(srv, "POST", {"sid": f"{i:032x}"})[0] for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]

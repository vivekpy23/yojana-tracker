"""Feedback endpoint: validation, rate limiting, append-only storage, concurrent writes, nothing under site/."""
import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import feedback_server as fs


@pytest.fixture
def server(tmp_path):
    fs.Handler.feedback_file = tmp_path / "feedback.json"
    fs.Handler.limiter = fs.RateLimiter(limit=1000, window=600)
    srv = fs.serve("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, tmp_path / "feedback.json"
    srv.shutdown()
    srv.server_close()
    fs.Handler.feedback_file = None


def post(srv, body, ctype="application/json"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/feedback", data=data,
                                 headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_saves_id_message_timestamp_and_appends(server):
    srv, path = server
    assert post(srv, {"message": "  Goa is missing a scheme  "})[0] == 201
    assert post(srv, {"message": "Second one"})[0] == 201
    entries = json.loads(path.read_text(encoding="utf-8"))
    assert [e["message"] for e in entries] == ["Goa is missing a scheme", "Second one"]
    assert all(set(e) == {"id", "message", "timestamp"} for e in entries)
    assert entries[0]["id"] != entries[1]["id"] and entries[0]["timestamp"].endswith("+00:00")


@pytest.mark.parametrize("body,ctype,code", [
    ({"message": ""}, "application/json", 400),
    ({"message": "   "}, "application/json", 400),
    ({"message": "x" * 1001}, "application/json", 400),
    ({"message": 42}, "application/json", 400),
    (["not", "an", "object"], "application/json", 400),
    (b"not json", "application/json", 400),
    ({"message": "hello"}, "text/plain", 415),
    ({"message": "x" * 5000}, "application/json", 413),
])
def test_rejects_bad_input_and_saves_nothing(server, body, ctype, code):
    srv, path = server
    assert post(srv, body, ctype)[0] == code
    assert not path.exists()


def test_control_characters_removed():
    assert fs.clean_message("hi\x00 there\x07") == ("hi there", None)


def test_rate_limit_per_client(server):
    srv, path = server
    fs.Handler.limiter = fs.RateLimiter(limit=2, window=600)
    codes = [post(srv, {"message": f"message {i}"})[0] for i in range(3)]
    assert codes == [201, 201, 429]
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 2


def test_rate_limit_window_slides():
    rl = fs.RateLimiter(limit=1, window=10)
    assert rl.allow("a", now=0) and not rl.allow("a", now=5) and rl.allow("a", now=10) and rl.allow("b", now=5)


def test_concurrent_submissions_are_all_kept(server):
    srv, path = server
    with ThreadPoolExecutor(max_workers=20) as pool:
        codes = list(pool.map(lambda i: post(srv, {"message": f"concurrent {i}"})[0], range(40)))
    assert codes == [201] * 40
    entries = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(e["message"] for e in entries) == sorted(f"concurrent {i}" for i in range(40))
    assert len({e["id"] for e in entries}) == 40


def test_feedback_file_is_outside_the_published_site():
    assert fs.SITE not in fs.FEEDBACK_FILE.resolve().parents


def test_feedback_file_not_served(server):
    srv, _ = server
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/../data/feedback.json")
    assert e.value.code == 404

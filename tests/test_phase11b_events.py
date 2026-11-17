"""Event publisher: wire format, ordering, loss accounting, and a hot path that never blocks.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11b_events.py -q
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

from core.events import BET_FIELDS, EventPublisher  # noqa: E402

_FIXTURES = os.path.join(_HERE, "fixtures", "events_v1")
_BET = (168, 0.00000002, 2.0, "over", 49.5, 75.31, "win", 0.00000002,
        2.52702731, 0.00000005, 4, 1, 38.4)


class _FakePipe:
    def __init__(self, client):
        self._client = client
        self._ops = []

    def xadd(self, stream, fields, maxlen=None, approximate=False):
        self._ops.append((stream, fields["data"], maxlen, approximate))

    def execute(self):
        self._client.calls += 1
        if self._client.gate is not None:
            self._client.blocked.set()
            self._client.gate.wait(10)
        if self._client.fail:
            raise ConnectionError("redis down")
        self._client.entries.extend(self._ops)


class _FakeRedis:
    def __init__(self, fail=False, gate=None):
        self.fail = fail
        self.gate = gate
        self.blocked = threading.Event()
        self.calls = 0
        self.entries = []

    def pipeline(self, transaction=True):
        assert transaction is False
        return _FakePipe(self)


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def publishers():
    started = []

    def make(client, **kw):
        pub = EventPublisher("redis://unused", client=client, **kw)
        started.append((pub, client))
        return pub
    yield make
    for pub, client in started:
        if client.gate is not None:
            client.gate.set()
        pub.stop(timeout=2.0)


def test_events_arrive_in_order_with_envelope(publishers):
    client = _FakeRedis()
    pub = publishers(client, lane=3)
    assert pub.start()
    for i in range(100):
        pub.emit_bet(_BET)
        pub.emit_state({"total_bets": i})
        pub.emit_lifecycle("cycle", {"n": i})
    pub.stop()

    assert len(client.entries) == 300
    events = [json.loads(data) for _, data, _, _ in client.entries]
    assert [e["seq"] for e in events] == list(range(300))
    assert [s for s, _, _, _ in client.entries[:3]] == [
        "wolfbet:bets", "wolfbet:state", "wolfbet:lifecycle"]
    assert {(s, m, a) for s, _, m, a in client.entries} == {
        ("wolfbet:bets", 200_000, True), ("wolfbet:state", 20_000, True),
        ("wolfbet:lifecycle", 20_000, True)}
    first = events[0]
    assert set(first) == {"v", "svc", "lane", "pid", "seq", "ts_ms", "type", "payload"}
    assert (first["v"], first["svc"], first["lane"], first["pid"]) == (1, "wolfbet", 3, os.getpid())
    assert list(first["payload"]) == list(BET_FIELDS)
    assert events[2]["payload"] == {"kind": "cycle", "data": {"n": 0}}
    assert pub.stats()["sent"] == 300 and pub.stats()["connected"] is True


@pytest.mark.parametrize("name, item", [
    ("bet", ("bet", 17, 1794945626123, _BET)),
    ("state", ("state", 18, 1794945626200, {
        "pid": 4242, "session_id": 168, "running": True, "paused": False,
        "currency": "usdt", "strategy": "Martingale", "total_bets": 4,
        "profit": 0.00000005, "current_balance": 2.52702731, "bets_per_second": 16.6})),
    ("lifecycle", ("lifecycle", 19, 1794945626300, ("session_close", {
        "session_id": 168, "total_bets": 4, "profit": 0.00000005,
        "reason": "Profit target reached"}))),
])
def test_encoder_matches_contract_fixtures(name, item):
    pub = EventPublisher("redis://unused", lane=2)
    pub.pid = 4242
    stream, text = pub.encode(item)
    with open(os.path.join(_FIXTURES, f"{name}.json")) as f:
        assert json.loads(text) == json.load(f)
    assert stream == {"bet": "wolfbet:bets", "state": "wolfbet:state",
                      "lifecycle": "wolfbet:lifecycle"}[name]


def test_fixture_copies_match_contract_checksums():
    with open(os.path.join(_FIXTURES, "SHA256SUMS")) as f:
        sums = dict(reversed(line.split()) for line in f if line.strip())
    assert set(sums) == {"bet.json", "state.json", "lifecycle.json"}
    for name, digest in sums.items():
        with open(os.path.join(_FIXTURES, name), "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == digest, name


def test_full_queue_drops_oldest_and_counts(publishers):
    client = _FakeRedis(gate=threading.Event())
    pub = publishers(client, queue_max=10)
    pub.start()
    pub.emit_bet(_BET)
    assert client.blocked.wait(2)
    for _ in range(25):
        pub.emit_bet(_BET)
    assert pub.stats()["overflow"] == 15
    assert pub.stats()["queued"] == 26
    assert [item[1] for item in pub._queue] == list(range(16, 26))


def test_redis_errors_never_reach_the_caller_and_recover(publishers, caplog):
    client = _FakeRedis(fail=True)
    pub = publishers(client)
    pub.start()
    with caplog.at_level(logging.WARNING, logger="wolfbet.events"):
        for _ in range(5):
            pub.emit_bet(_BET)
        assert _wait(lambda: pub.stats()["dropped"] == 5)
        stats = pub.stats()
        assert stats["connected"] is False
        assert stats["last_error"] == "ConnectionError"
        assert "redis://" not in caplog.text

        client.fail = False
        for _ in range(3):
            pub.emit_bet(_BET)
        assert _wait(lambda: pub.stats()["sent"] == 3)
    assert pub.stats()["connected"] is True
    assert pub._backoff == 0.0
    assert len([r for r in caplog.records if r.name == "wolfbet.events"]) == 1


def test_emit_never_blocks_while_sender_is_stuck(publishers):
    client = _FakeRedis(gate=threading.Event())
    pub = publishers(client)
    pub.start()
    pub.emit_bet(_BET)
    assert client.blocked.wait(2)

    t0 = time.perf_counter()
    for _ in range(100_000):
        pub.emit_bet(_BET)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.2, f"100k emits took {elapsed * 1000:.0f} ms"
    assert pub.stats()["overflow"] == 100_000 - 50_000


def test_bad_event_is_dropped_and_the_rest_of_the_batch_is_sent(publishers):
    client = _FakeRedis()
    pub = publishers(client)
    pub.start()
    nan_bet = _BET[:7] + (float("nan"),) + _BET[8:]
    pub.emit_bet(nan_bet)
    pub.emit_bet(_BET[:5])
    pub.emit_bet(_BET)
    assert _wait(lambda: pub.stats()["sent"] == 1)
    assert pub.stats()["dropped"] == 2
    assert json.loads(client.entries[0][1])["seq"] == 2


def test_emits_before_start_are_ignored():
    pub = EventPublisher("redis://unused", client=_FakeRedis())
    pub.emit_bet(_BET)
    assert pub.stats()["queued"] == 0
    assert len(pub._queue) == 0


def test_missing_redis_package_disables_publisher(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "redis", None)
    pub = EventPublisher("redis://unused")
    with caplog.at_level(logging.WARNING, logger="wolfbet.events"):
        assert pub.start() is False
    assert "redis package is not installed" in caplog.text
    pub.emit_bet(_BET)
    assert pub.stats()["queued"] == 0
    assert not any(t.name == "EventPublisher" for t in threading.enumerate())


def test_redis_is_not_imported_unless_started():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from core.events import EventPublisher;"
        "p = EventPublisher('redis://unused');"
        "p.emit_bet(tuple(range(13))); p.stats();"
        "assert 'redis' not in sys.modules, 'redis imported'"
    )
    result = subprocess.run([sys.executable, "-c", code, _WOLFBET_DIR],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

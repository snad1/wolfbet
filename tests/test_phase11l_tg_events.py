"""TG event publisher: one scoped publisher per bot process, per-user streams, a bet path that never waits.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11l_tg_events.py -q
Bench: add `-m bench -s` to print per-bet cost with events off, on, and on with a stuck Redis.
"""

import asyncio
import hashlib
import json
import logging
import os
import statistics
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
from tg import engine as tg_engine  # noqa: E402

_BET = (1, 0.0001, 2.0, "over", 49.5, 75.0, "win", 0.0001, 1.0001, 0.0001, 1, 1, 30.0)


class _Redis:
    """Pipeline-shaped fake that keeps every XADD as (stream, decoded event)."""

    def __init__(self):
        self.entries = []
        self._pending = []

    def pipeline(self, transaction=True):
        return self

    def xadd(self, stream, fields, maxlen=None, approximate=None):
        self._pending.append((stream, json.loads(fields["data"])))

    def execute(self):
        self.entries.extend(self._pending)
        self._pending = []


class _StuckRedis(_Redis):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.blocked = threading.Event()

    def execute(self):
        self.blocked.set()
        self.release.wait(10)


def _scoped(client, **kw):
    pub = EventPublisher("redis://unused", stream_prefix="wolfbet-tg", scoped=True,
                         client=client, **kw)
    assert pub.start()
    return pub


def _result(state="win", profit=0.0001, balance=1.0001):
    return {
        "bet": {"state": state, "amount": "0.0001", "multiplier": "2.0", "bet_value": "49.5",
                "result_value": "75.0", "profit": str(profit)},
        "userBalance": {"amount": str(balance)},
    }


@pytest.fixture
def engine(tmp_path):
    e = tg_engine.BettingEngine(user_id=42, db_path=str(tmp_path / "tg.db"), api_key="k" * 40,
                                config={"game": "dice", "base_bet": 0.0001})
    e.session_id = 9
    e.running = True
    return e


@pytest.fixture
def published(monkeypatch):
    """The engine module's shared publisher, backed by a recording fake."""
    client = _Redis()
    pub = _scoped(client)
    monkeypatch.setattr(tg_engine, "_events", pub)
    yield client, pub
    pub.stop()


# ── scoped publisher ───────────────────────────────────────
_FIXTURES = os.path.join(_HERE, "fixtures", "tg_events_v1")
_FIXTURE_BET = (3, 0.00000002, 2.0, "over", 49.5, 75.31, "win", 0.00000002, 2.52702731,
                0.00000005, 4, 1, 31.0)


@pytest.mark.parametrize("name, item", [
    ("bet", ("bet", 17, 1795200000123, _FIXTURE_BET, 42)),
    ("state", ("state", 18, 1795200000200, {
        "session_id": 3, "running": True, "paused": False, "currency": "usdt",
        "strategy": "Martingale", "total_bets": 4, "profit": 0.00000005,
        "current_balance": 2.52702731, "bets_per_second": 9.5}, 42)),
    ("lifecycle", ("lifecycle", 19, 1795200000300, ("session_close", {
        "session_id": 3, "total_bets": 4, "wins": 3, "losses": 1, "profit": 0.00000005,
        "wagered": 0.00000008, "end_balance": 2.52702731}), 42)),
])
def test_scoped_encoder_matches_contract_fixtures(name, item):
    pub = EventPublisher("redis://unused", stream_prefix="wolfbet-tg", scoped=True)
    pub.pid = 4242
    stream, text = pub.encode(item)
    with open(os.path.join(_FIXTURES, f"{name}.json")) as f:
        assert json.loads(text) == json.load(f)
    assert stream == {"bet": "wolfbet-tg:42:bets", "state": "wolfbet-tg:42:state",
                      "lifecycle": "wolfbet-tg:42:lifecycle"}[name]


def test_tg_fixture_copies_match_contract_checksums():
    with open(os.path.join(_FIXTURES, "SHA256SUMS")) as f:
        sums = dict(reversed(line.split()) for line in f if line.strip())
    assert set(sums) == {"bet.json", "state.json", "lifecycle.json"}
    for name, digest in sums.items():
        with open(os.path.join(_FIXTURES, name), "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == digest, name


def test_scoped_events_go_to_the_users_streams_with_uid_in_the_payload():
    client = _Redis()
    pub = _scoped(client)
    pub.emit_bet(_BET, uid=7)
    pub.emit_state({"profit": 1.5}, uid=8)
    pub.emit_lifecycle("pause", {}, uid=7)
    pub.stop()

    streams = [stream for stream, _ in client.entries]
    assert streams == ["wolfbet-tg:7:bets", "wolfbet-tg:8:state", "wolfbet-tg:7:lifecycle"]
    bet, state, life = (event for _, event in client.entries)
    assert bet["svc"] == "wolfbet-tg"
    assert bet["payload"] == dict(zip(BET_FIELDS, _BET), uid=7)
    assert state["payload"] == {"profit": 1.5, "uid": 8}
    assert life["payload"] == {"kind": "pause", "data": {}, "uid": 7}


def test_seq_counts_per_user_so_a_gap_means_that_user_lost_events():
    client = _Redis()
    pub = _scoped(client)
    for uid in (1, 2, 1, 1, 2):
        pub.emit_bet(_BET, uid=uid)
    pub.stop()

    seqs = {}
    for stream, event in client.entries:
        seqs.setdefault(stream, []).append(event["seq"])
    assert seqs == {"wolfbet-tg:1:bets": [0, 1, 2], "wolfbet-tg:2:bets": [0, 1]}


def test_a_full_queue_uses_up_the_dropped_users_seq_only():
    client = _StuckRedis()
    pub = _scoped(client, queue_max=1)
    try:
        pub.emit_bet(_BET, uid=1)
        assert client.blocked.wait(2)
        pub.emit_bet(_BET, uid=1)
        assert pub.reserve(uid=1) is False
        pub.emit_bet(_BET, uid=1)
        assert pub.stats()["overflow"] == 2
    finally:
        client.release.set()
        pub.stop()
    assert next(pub._seqs[1]) == 4
    assert 2 not in pub._seqs


def test_unscoped_publisher_keeps_the_cli_stream_names():
    client = _Redis()
    pub = EventPublisher("redis://unused", client=client)
    assert pub.start()
    pub.emit_bet(_BET)
    pub.stop()
    (stream, event), = client.entries
    assert stream == "wolfbet:bets"
    assert "uid" not in event["payload"]


# ── engine events ──────────────────────────────────────────
def test_a_processed_bet_is_published_in_cli_field_order(engine, published):
    client, pub = published
    engine._last_api_ms = 31.0
    engine._process_bet_result(_result())
    pub.stop()

    (stream, event), = client.entries
    assert stream == "wolfbet-tg:42:bets"
    assert event["payload"] == {
        "session_id": 9, "amount": 0.0001, "multiplier": 2.0, "rule": "over",
        "bet_value": 49.5, "result_value": 75.0, "state": "win", "profit": 0.0001,
        "balance_after": 1.0001, "session_profit": 0.0001, "total_bets": 1,
        "current_streak": 1, "api_ms": 31.0, "uid": 42,
    }


def test_lifecycle_follows_pause_resume_reload_and_the_end(engine, published):
    client, pub = published
    engine.pause()
    engine.pause()
    engine.resume()
    engine.resume()
    asyncio.run(engine.mutate({"win_mult": 1.5, "api_key": "do-not-publish-me"}))
    engine._insufficient_balance = True
    engine.stop_reason = "Insufficient balance for bet 0.00010000"
    engine._emit_session_end()
    pub.stop()

    kinds = [event["payload"]["kind"] for _, event in client.entries]
    assert kinds == ["pause", "resume", "reload", "error", "session_close", "stop"]
    reload = client.entries[2][1]["payload"]["data"]
    assert reload == {"keys": ["api_key", "win_mult"]}
    assert "do-not-publish-me" not in json.dumps(client.entries)
    assert client.entries[-1][1]["payload"]["data"] == {
        "reason": "Insufficient balance for bet 0.00010000"}


def test_start_announces_the_open_session(engine, published):
    client, pub = published
    engine._emit_start(resumed=True)
    pub.stop()

    (_, start), (_, opened) = client.entries
    assert start["payload"]["kind"] == "start"
    assert start["payload"]["data"]["resumed"] is True
    assert opened["payload"]["kind"] == "session_open"
    assert opened["payload"]["data"]["session_id"] == 9


def test_state_is_sent_at_most_every_two_seconds(engine, published):
    client, pub = published
    engine._events_tick()
    engine._events_tick()
    engine._events_next_state = 0.0
    engine._events_tick()
    pub.stop()

    states = [event["payload"] for stream, event in client.entries if stream.endswith(":state")]
    assert len(states) == 2
    assert states[0]["uid"] == 42
    assert states[0]["low_bps"] == 0
    assert states[0]["lowest_balance"] == engine.current_balance


def test_no_publisher_means_no_events_and_no_errors(engine, monkeypatch):
    monkeypatch.setattr(tg_engine, "_events", None)
    assert engine._process_bet_result(_result()) == "win"
    engine.pause()
    engine._events_tick()
    engine._emit_session_end()


def test_bet_emits_stay_under_budget_while_redis_is_stuck(engine, monkeypatch):
    client = _StuckRedis()
    pub = _scoped(client)
    monkeypatch.setattr(tg_engine, "_events", pub)
    bet = _result()["bet"]
    try:
        engine._emit_bet(bet, 0.0001, 1.0)
        assert client.blocked.wait(2)
        t0 = time.perf_counter()
        for _ in range(10_000):
            engine._emit_bet(bet, 0.0001, 1.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    finally:
        client.release.set()
        pub.stop(timeout=2)
    assert elapsed_ms < 100, f"10k bet emits took {elapsed_ms:.0f} ms"


# ── switching events on ────────────────────────────────────
def test_events_off_starts_nothing_and_never_imports_redis():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from tg import engine;"
        "assert engine.start_events(enabled=False, url='redis://localhost:6379') is False;"
        "assert engine._events is None;"
        "assert 'redis' not in sys.modules, 'redis imported'"
    )
    env = dict(os.environ, APP_ENV="testing")
    env.pop("WOLFBET_TG_EVENTS", None)
    done = subprocess.run([sys.executable, "-c", code, _WOLFBET_DIR], env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("url", ["", "http://localhost:6379", "redis://"])
def test_a_bad_url_leaves_events_off_with_one_warning(url, monkeypatch, caplog):
    monkeypatch.setattr(tg_engine, "_events", None)
    with caplog.at_level(logging.WARNING):
        assert tg_engine.start_events(enabled=True, url=url, client=_Redis()) is False
    assert tg_engine._events is None
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "WOLFBET_TG_EVENTS is on but WOLFBET_EVENTS_URL is missing or invalid: events stay off"]


def test_start_and_stop_events_share_one_publisher(monkeypatch):
    monkeypatch.setattr(tg_engine, "_events", None)
    client = _Redis()
    assert tg_engine.start_events(enabled=True, url="redis://localhost:6379", client=client)
    first = tg_engine._events
    assert tg_engine.start_events(enabled=True, url="redis://localhost:6379", client=client)
    assert tg_engine._events is first
    tg_engine.stop_events()
    assert tg_engine._events is None


# ── bench ──────────────────────────────────────────────────
@pytest.mark.bench
def test_bench_per_bet_cost_with_events_off_on_and_stuck(tmp_path, monkeypatch):
    rounds, bets = 5, 20_000
    variants = ("off", "on, fast", "on, stuck")
    p50s = {v: [] for v in variants}
    p99s = {v: [] for v in variants}
    result = _result()

    def run(variant):
        client = {"off": None, "on, fast": _Redis(), "on, stuck": _StuckRedis()}[variant]
        pub = _scoped(client) if client is not None else None
        monkeypatch.setattr(tg_engine, "_events", pub)
        e = tg_engine.BettingEngine(user_id=7, db_path=str(tmp_path / "b.db"),
                                    api_key="k" * 40, config={"game": "dice"})
        e.session_id = 1
        times = []
        clock = time.perf_counter_ns
        for _ in range(bets):
            t0 = clock()
            e._process_bet_result(result)
            times.append(clock() - t0)
        if isinstance(client, _StuckRedis):
            client.release.set()
        if pub is not None:
            pub.stop(timeout=2)
        times.sort()
        p50s[variant].append(times[len(times) // 2] / 1000)
        p99s[variant].append(times[int(len(times) * 0.99)] / 1000)

    for _ in range(rounds):
        for variant in variants:
            run(variant)

    off = statistics.median(p99s["off"])
    print(f"\nTG per-bet cost, median of {rounds} interleaved rounds x {bets} bets")
    print("| Variant | per-bet µs p50 | per-bet µs p99 | p99 delta vs off | off p99 spread |")
    print("|---|---|---|---|---|")
    spread = (max(p99s["off"]) - min(p99s["off"])) / off * 100
    for v in variants:
        p99 = statistics.median(p99s[v])
        print(f"| {v} | {statistics.median(p50s[v]):.1f} | {p99:.1f} | "
              f"{(p99 - off) / off * 100:+.1f} % | {spread:.1f} % |")

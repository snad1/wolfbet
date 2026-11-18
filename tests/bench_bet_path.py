"""Bet-path benchmark: what the bot itself costs per bet, with the network taken out.

Drives the real betting_loop against a transport that answers instantly, then
times _db_save_bet_dispatch on its own. Runs with the event publisher off, on
with a fast Redis, on with Redis refusing connections and on with a Redis that
takes 1 s per call (plus a real Redis when REDIS_URL is set). Async variants run
five interleaved rounds and report the median p99; the spread of the off rounds
is the noise floor the gates are read against.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/bench_bet_path.py -m bench -s
"""

import importlib
import logging
import os
import platform
import sqlite3
import statistics
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

pytestmark = pytest.mark.bench

LOOP_BETS = 20_000
DISPATCH_CALLS = 100_000
ROUNDS = 5
DISPATCH_P99_GATE_US = 5.0
PER_BET_P99_GATE_PCT = 2.0
_PATH_ATTRS = ("DB_PATH", "CONFIG_PATH", "STATE_PATH", "PID_PATH", "RUN_PATH",
               "PRESET_PATH", "LOG_DIR")


class _Pipe:
    def __init__(self, client):
        self._client = client

    def xadd(self, *args, **kwargs):
        pass

    def execute(self):
        self._client.call()


class _FakeRedis:
    """fast: returns at once; unreachable: refuses; slow: 1 s per call until released."""

    def __init__(self, kind):
        self.kind = kind
        self.release = threading.Event()

    def pipeline(self, transaction=True):
        return _Pipe(self)

    def call(self):
        if self.kind == "unreachable":
            raise ConnectionRefusedError("connection refused")
        if self.kind == "slow":
            self.release.wait(1.0)


def _real_client():
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis
    except ImportError:
        return None
    return redis.Redis.from_url(url, socket_connect_timeout=0.1, socket_timeout=0.1)


@pytest.fixture(scope="module")
def wb(tmp_path_factory):
    """Import wolfbet with every file path inside a temp dir."""
    home = tmp_path_factory.mktemp("wolfbet_home")
    with pytest.MonkeyPatch.context() as mp:
        for attr in _PATH_ATTRS:
            mp.setenv(f"WOLFBET_{attr}", str(home / attr.lower()))
        module = importlib.import_module("wolfbet")
    return module


@pytest.fixture
def bench(wb, tmp_path, monkeypatch):
    """Quiet log, temp paths, bot state and publisher restored afterwards."""
    for attr in _PATH_ATTRS:
        monkeypatch.setattr(wb, attr, str(tmp_path / attr.lower()))
    handler = logging.FileHandler(tmp_path / "wolfbet.log")
    monkeypatch.setattr(wb.logger, "handlers", [handler])
    monkeypatch.setattr(wb, "_event_publisher", None)
    saved = {k: v for k, v in vars(wb.state).items() if k != "lock"}
    yield wb, tmp_path, monkeypatch
    _reset_writer(wb)
    handler.close()
    s = wb.state
    for key in [k for k in vars(s) if k != "lock" and k not in saved]:
        delattr(s, key)
    for key, value in saved.items():
        setattr(s, key, value)


def _reset_writer(wb):
    wb._stop_bet_writer(timeout=10.0)
    with wb._bet_write_queue.mutex:
        wb._bet_write_queue.queue.clear()
    for key in wb._bet_writer_stats:
        wb._bet_writer_stats[key] = 0


def _fresh_bot(wb, db_path, mode):
    s = wb.state
    wb.DB_PATH = db_path
    _reset_writer(wb)
    wb.init_db()
    for key, value in {
        "game": "dice", "strategy_key": "1", "strategy": "Flat", "rule": "over",
        "multiplier": 2.0, "bet_value": 49.5, "currency": "usdt",
        "base_bet": 0.0001, "current_bet": 0.0001,
        "start_balance": 1000.0, "current_balance": 1000.0, "profit": 0.0,
        "total_bets": 0, "wins": 0, "losses": 0, "current_streak": 0,
        "max_bets": LOOP_BETS, "max_profit": None, "max_loss": None, "max_wins": None,
        "stop_on_balance": None, "streak_reset_losses": 0, "recurring": False,
        "profit_increment": 0, "profit_threshold": 0, "basebet_pct": 0,
        "bet_delay": 0, "lane_share": None, "custom_rules": [],
        "rate_wait": False, "paused": False, "running": True,
        "_insufficient_balance": False, "session_start": time.time(),
        "db_write_mode": mode,
    }.items():
        setattr(s, key, value)
    s.session_id = wb._db_start_session()
    if mode == "async":
        wb._start_bet_writer()


class _InstantResponse:
    status_code = 200
    headers = {"x-ratelimit-limit": "1000000", "x-ratelimit-remaining": "1000000"}
    text = ""

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _instant_transport(wb, stamps):
    """Answer every bet at once, alternating win and loss, and stamp each call."""
    s = wb.state

    def post(url, headers=None, json=None, timeout=None):
        stamps.append(time.perf_counter_ns())
        amount = float(json["amount"])
        win = len(stamps) % 2 == 0
        profit = amount if win else -amount
        return _InstantResponse({
            "bet": {"state": "win" if win else "loss", "amount": amount,
                    "multiplier": 2.0, "bet_value": 49.5,
                    "result_value": 75.0 if win else 25.0, "profit": profit},
            "userBalance": {"amount": s.current_balance + profit},
        })
    return post


def _start_publisher(wb, client):
    """Start a publisher on `client`, recording its sender thread's CPU time after each batch."""
    pub = wb.EventPublisher("redis://bench", stream_prefix="wolfbet-bench", client=client)
    cpu = {"s": 0.0}
    send = pub._send_batch

    def timed_send():
        try:
            return send()
        finally:
            cpu["s"] = time.thread_time()
    pub._send_batch = timed_send
    pub.start()
    wb._event_publisher = pub
    return pub, cpu


def _us(samples_ns, pct, wb):
    return wb._percentile(sorted(samples_ns), pct) / 1000


def _measure(wb, tmp_path, monkeypatch, mode, client, tag):
    """One loop run plus one dispatch run; returns the metrics dict."""
    _fresh_bot(wb, str(tmp_path / f"{tag}.db"), mode)
    pub = cpu = None
    if client is not None:
        pub, cpu = _start_publisher(wb, client)
    stamps = []
    monkeypatch.setattr(wb._http_session, "post", _instant_transport(wb, stamps))

    t0 = time.perf_counter_ns()
    wb.betting_loop()
    loop_s = (time.perf_counter_ns() - t0) / 1e9
    assert wb.state.total_bets == LOOP_BETS and len(stamps) == LOOP_BETS
    per_bet = [b - a for a, b in zip(stamps[1:], stamps[2:])]

    bet = {"amount": 0.0001, "multiplier": 2.0, "bet_value": 49.5,
           "result_value": 75.0, "state": "win"}
    dispatch = []
    d0 = time.perf_counter_ns()
    for _ in range(DISPATCH_CALLS):
        a = time.perf_counter_ns()
        wb._db_save_bet_dispatch(bet, 0.0001, 1000.0)
        dispatch.append(time.perf_counter_ns() - a)
    wall_s = loop_s + (time.perf_counter_ns() - d0) / 1e9

    stats = {}
    if pub is not None:
        stats = pub.stats()
        wb._event_publisher = None
        pub._queue.clear()
        client_release = getattr(client, "release", None)
        if client_release is not None:
            client_release.set()
        pub.stop(timeout=10)
        assert not any(t.name == "EventPublisher" for t in threading.enumerate()), \
            "a publisher thread outlived its measurement"
    _reset_writer(wb)
    with sqlite3.connect(wb.DB_PATH) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    assert rows == LOOP_BETS + DISPATCH_CALLS
    return {
        "dispatch_p50": _us(dispatch, 0.50, wb), "dispatch_p99": _us(dispatch, 0.99, wb),
        "bet_p50": _us(per_bet, 0.50, wb), "bet_p99": _us(per_bet, 0.99, wb),
        "bet_p999": _us(per_bet, 0.999, wb), "bps": LOOP_BETS / loop_s,
        "cpu_pct": (cpu["s"] / wall_s * 100) if cpu else None,
        "sent": stats.get("sent"), "lost": (stats.get("dropped", 0) + stats.get("overflow", 0))
        if stats else None,
    }


def test_bet_path_with_and_without_publisher(bench):
    wb, tmp_path, monkeypatch = bench
    variants = {
        "off": lambda: None,
        "on, fast": lambda: _FakeRedis("fast"),
        "on, unreachable": lambda: _FakeRedis("unreachable"),
        "on, slow (1 s/call)": lambda: _FakeRedis("slow"),
    }
    if _real_client() is not None:
        variants["on, real redis"] = _real_client

    rounds = {name: [] for name in variants}
    for r in range(ROUNDS):
        for name, make in variants.items():
            rounds[name].append(_measure(wb, tmp_path, monkeypatch, "async", make(),
                                         f"async-{r}-{len(rounds[name])}-{name[:6]}"))
    sync = {name: _measure(wb, tmp_path, monkeypatch, "sync", variants[name](), f"sync-{name[:6]}")
            for name in ("off", "on, fast")}
    _print_report(wb, rounds, sync)


def _median(runs, key):
    vals = [r[key] for r in runs if r[key] is not None]
    return statistics.median(vals) if vals else None


def _print_report(wb, rounds, sync):
    from rich.console import Console
    from rich.table import Table

    off = rounds["off"]
    off_d99 = _median(off, "dispatch_p99")
    off_b99 = _median(off, "bet_p99")
    b99s = [r["bet_p99"] for r in off]
    noise_pct = (max(b99s) - min(b99s)) / off_b99 * 100

    tbl = Table(title=f"Async, median of {ROUNDS} interleaved rounds "
                      f"({LOOP_BETS:,} loop bets + {DISPATCH_CALLS:,} dispatch calls each)")
    for col in ("Variant", "dispatch us p50/p99", "per-bet us p50/p99/p99.9", "fake bps",
                "publisher CPU %", "events sent / lost", "dispatch p99 gate", "per-bet p99 gate"):
        tbl.add_column(col, justify="right" if col != "Variant" else "left")
    for name, runs in rounds.items():
        d99, b99 = _median(runs, "dispatch_p99"), _median(runs, "bet_p99")
        cpu = _median(runs, "cpu_pct")
        sent, lost = _median(runs, "sent"), _median(runs, "lost")
        if name == "off":
            gates = ("-", f"noise {noise_pct:.1f}%")
        else:
            dd, db = d99 - off_d99, (b99 - off_b99) / off_b99 * 100
            gates = (f"{dd:+.2f} us {'PASS' if dd <= DISPATCH_P99_GATE_US else 'FAIL'}",
                     f"{db:+.1f}% {'PASS' if db <= PER_BET_P99_GATE_PCT else 'FAIL'}")
        tbl.add_row(name,
                    f"{_median(runs, 'dispatch_p50'):.1f} / {d99:.1f}",
                    f"{_median(runs, 'bet_p50'):.1f} / {b99:.1f} / {_median(runs, 'bet_p999'):.1f}",
                    f"{_median(runs, 'bps'):,.0f}",
                    "-" if cpu is None else f"{cpu:.2f}",
                    "-" if sent is None else f"{sent:,.0f} / {lost:,.0f}",
                    *gates)
    console = Console(width=200)
    console.print()
    console.print(tbl)
    console.print("p99 per round (us), dispatch / per-bet:")
    for name, runs in rounds.items():
        console.print(f"  {name:22s} " + "  ".join(
            f"{r['dispatch_p99']:.1f}/{r['bet_p99']:.1f}" for r in runs))
    for name, r in sync.items():
        console.print(f"sync {name:10s} dispatch p50/p99 {r['dispatch_p50']:.1f}/{r['dispatch_p99']:.1f} us"
                      f"  per-bet p50/p99/p99.9 {r['bet_p50']:.1f}/{r['bet_p99']:.1f}/{r['bet_p999']:.1f} us"
                      f"  bps {r['bps']:,.0f}")
    console.print(f"Python {platform.python_version()} on {platform.platform()}, {platform.machine()}")

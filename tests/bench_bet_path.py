"""Bet-path benchmark: what the bot itself costs per bet, with the network taken out.

Drives the real betting_loop against a transport that answers instantly, in
both db_write_mode values, then times _db_save_bet_dispatch on its own. Rerun
it before and after any change near the bet path and compare the tables.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/bench_bet_path.py -m bench -s
"""

import importlib
import logging
import os
import platform
import sqlite3
import sys
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
_PATH_ATTRS = ("DB_PATH", "CONFIG_PATH", "STATE_PATH", "PID_PATH", "RUN_PATH",
               "PRESET_PATH", "LOG_DIR")
_RESULTS = {}


@pytest.fixture(scope="module")
def wb(tmp_path_factory):
    """Import wolfbet with every file path inside a temp dir, then print the table."""
    home = tmp_path_factory.mktemp("wolfbet_home")
    with pytest.MonkeyPatch.context() as mp:
        for attr in _PATH_ATTRS:
            mp.setenv(f"WOLFBET_{attr}", str(home / attr.lower()))
        module = importlib.import_module("wolfbet")
    yield module
    _print_table()


@pytest.fixture
def bench(wb, tmp_path, monkeypatch):
    """Fresh temp DB, quiet log, Flat dice config; restores the bot state afterwards."""
    for attr in _PATH_ATTRS:
        monkeypatch.setattr(wb, attr, str(tmp_path / attr.lower()))
    handler = logging.FileHandler(tmp_path / "wolfbet.log")
    monkeypatch.setattr(wb.logger, "handlers", [handler])

    s = wb.state
    saved = {k: v for k, v in vars(s).items() if k != "lock"}
    _reset_writer(wb)
    wb.init_db()
    for key, value in {
        "game": "dice", "strategy_key": "1", "strategy": "Flat", "rule": "over",
        "multiplier": 2.0, "bet_value": 49.5, "currency": "usdt",
        "base_bet": 0.0001, "current_bet": 0.0001,
        "start_balance": 1000.0, "current_balance": 1000.0,
        "max_bets": LOOP_BETS, "max_profit": None, "max_loss": None, "max_wins": None,
        "stop_on_balance": None, "streak_reset_losses": 0, "recurring": False,
        "profit_increment": 0, "profit_threshold": 0, "basebet_pct": 0,
        "bet_delay": 0, "lane_share": None, "custom_rules": [],
        "rate_wait": False, "paused": False, "running": True,
        "_insufficient_balance": False, "session_start": time.time(),
    }.items():
        setattr(s, key, value)
    s.session_id = wb._db_start_session()
    yield wb
    _reset_writer(wb)
    handler.close()
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


def _us_percentiles(wb, samples_ns, pcts):
    ordered = sorted(samples_ns)
    return [wb._percentile(ordered, p) / 1000 for p in pcts]


def _bet_rows(wb):
    conn = sqlite3.connect(wb.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["async", "sync"])
def test_bet_path(bench, monkeypatch, mode):
    wb = bench
    s = wb.state
    for attr in _PATH_ATTRS:
        assert getattr(wb, attr).startswith(str(os.path.dirname(wb.DB_PATH)))

    s.db_write_mode = mode
    if mode == "async":
        wb._start_bet_writer()
    stamps = []
    monkeypatch.setattr(wb._http_session, "post", _instant_transport(wb, stamps))

    t0 = time.perf_counter_ns()
    wb.betting_loop()
    loop_ns = time.perf_counter_ns() - t0
    _reset_writer(wb)

    assert s.total_bets == LOOP_BETS
    assert len(stamps) == LOOP_BETS
    assert _bet_rows(wb) == LOOP_BETS

    per_bet = [b - a for a, b in zip(stamps[1:], stamps[2:])]
    bet_p50, bet_p99, bet_p999 = _us_percentiles(wb, per_bet, (0.50, 0.99, 0.999))

    if mode == "async":
        wb._start_bet_writer()
    bet = {"amount": 0.0001, "multiplier": 2.0, "bet_value": 49.5,
           "result_value": 75.0, "state": "win"}
    dispatch = []
    for _ in range(DISPATCH_CALLS):
        a = time.perf_counter_ns()
        wb._db_save_bet_dispatch(bet, 0.0001, 1000.0)
        dispatch.append(time.perf_counter_ns() - a)
    stats = dict(wb._bet_writer_stats)
    _reset_writer(wb)
    d_p50, d_p99 = _us_percentiles(wb, dispatch, (0.50, 0.99))

    assert _bet_rows(wb) == LOOP_BETS + DISPATCH_CALLS
    assert bet_p50 > 0 and d_p50 > 0

    _RESULTS[mode] = {
        "dispatch": f"{d_p50:.1f} / {d_p99:.1f}",
        "fallback": f"{stats['fallback_sync']} / {stats['queue_high_watermark']}",
        "per_bet": f"{bet_p50:.1f} / {bet_p99:.1f} / {bet_p999:.1f}",
        "bps": f"{LOOP_BETS / (loop_ns / 1e9):,.0f}",
    }


def _print_table():
    if not _RESULTS:
        return
    from rich.console import Console
    from rich.table import Table

    tbl = Table(title=f"Bet path ({LOOP_BETS:,} loop bets, {DISPATCH_CALLS:,} dispatch calls)")
    tbl.add_column("Measure")
    modes = [m for m in ("async", "sync") if m in _RESULTS]
    for mode in modes:
        tbl.add_column(mode, justify="right")
    for key, label in (("dispatch", "dispatch us p50 / p99"),
                       ("fallback", "async fallback_sync / queue high-water"),
                       ("per_bet", "per-bet us p50 / p99 / p99.9"),
                       ("bps", "fake bps")):
        tbl.add_row(label, *[_RESULTS[m][key] for m in modes])
    console = Console()
    console.print()
    console.print(tbl)
    console.print(f"Python {platform.python_version()} on {platform.platform()}, "
                  f"{platform.machine()}")

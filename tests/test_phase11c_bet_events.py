"""Bet events: every saved bet goes to the event publisher when events are on, and nothing changes when off.

Run: APP_ENV=testing .venv-test/bin/python -m pytest wolfbet/tests/test_phase11c_bet_events.py -q
"""

import json
import logging
import os
import sqlite3
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from core.events import BET_FIELDS, EventPublisher  # noqa: E402

BETS = 100
_SECRET_URL = "redis://publisher:s3cret-pass@127.0.0.1:6379/0"


class _FakePipe:
    def __init__(self, client):
        self._client = client
        self._ops = []

    def xadd(self, stream, fields, maxlen=None, approximate=False):
        self._ops.append((stream, fields["data"]))

    def execute(self):
        if self._client.fail:
            raise ConnectionError("redis down")
        self._client.entries.extend(self._ops)


class _FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.entries = []

    def pipeline(self, transaction=True):
        return _FakePipe(self)


class _Resp:
    status_code = 200
    headers = {"x-ratelimit-limit": "1000000", "x-ratelimit-remaining": "1000000"}
    text = ""

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """Flat dice bot on a temp DB, answering every bet instantly; state restored after."""
    monkeypatch.setattr(wb, "DB_PATH", str(tmp_path / "bets.db"))
    monkeypatch.setattr(wb, "_event_publisher", None)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    s = wb.state
    saved = {k: v for k, v in vars(s).items() if k != "lock"}
    wb.init_db()
    for key, value in {
        "game": "dice", "strategy_key": "1", "strategy": "Flat", "rule": "over",
        "multiplier": 2.0, "bet_value": 49.5, "currency": "usdt",
        "base_bet": 0.0001, "current_bet": 0.0001,
        "start_balance": 1000.0, "current_balance": 1000.0, "profit": 0.0,
        "total_bets": 0, "wins": 0, "losses": 0, "current_streak": 0,
        "max_bets": BETS, "max_profit": None, "max_loss": None, "max_wins": None,
        "stop_on_balance": None, "streak_reset_losses": 0, "recurring": False,
        "profit_increment": 0, "profit_threshold": 0, "basebet_pct": 0,
        "bet_delay": 0, "lane_share": None, "custom_rules": [], "lane": 1,
        "rate_wait": False, "paused": False, "running": True,
        "_insufficient_balance": False, "session_start": time.time(),
        "db_write_mode": "sync", "events_enabled": False, "events_url": None,
    }.items():
        setattr(s, key, value)
    s.session_id = wb._db_start_session()

    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        amount = float(json["amount"])
        win = len(calls) % 3 != 0
        profit = amount if win else -amount
        return _Resp({
            "bet": {"state": "win" if win else "loss", "amount": amount, "multiplier": 2.0,
                    "bet_value": 49.5, "result_value": 75.0 if win else 25.0, "profit": profit},
            "userBalance": {"amount": s.current_balance + profit},
        })
    monkeypatch.setattr(wb._http_session, "post", post)
    yield wb
    wb._stop_bet_writer(timeout=5.0)
    for key in [k for k in vars(s) if k != "lock" and k not in saved]:
        delattr(s, key)
    for key, value in saved.items():
        setattr(s, key, value)


def _rows(bot):
    conn = sqlite3.connect(bot.DB_PATH)
    try:
        return conn.execute(
            "SELECT session_id, amount, multiplier, rule, bet_value, result_value, state, "
            "profit, balance_after FROM bets ORDER BY id").fetchall()
    finally:
        conn.close()


def _with_publisher(bot, client, lane=1):
    pub = EventPublisher("redis://unused", lane=lane, client=client)
    assert pub.start()
    bot._event_publisher = pub
    return pub


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _expected_rows():
    """Rows the scripted transport produces without events: win, win, loss, repeated."""
    rows, balance = [], 1000.0
    for n in range(1, BETS + 1):
        win = n % 3 != 0
        profit = 0.0001 if win else -0.0001
        balance = balance + profit
        rows.append((0.0001, 2.0, "over", 49.5, 75.0 if win else 25.0,
                     "win" if win else "loss", profit, balance))
    return rows


def _rows_without_session(bot):
    return [r[1:] for r in _rows(bot)]


def test_flag_off_starts_nothing_and_writes_the_usual_rows(bot):
    bot._start_events()
    bot.betting_loop()

    assert bot.state.total_bets == BETS
    assert bot._event_publisher is None
    assert not any(t.name == "EventPublisher" for t in threading.enumerate())
    assert "redis" not in sys.modules
    assert _rows_without_session(bot) == _expected_rows()


def test_flag_on_sends_one_event_per_bet_matching_the_table(bot):
    client = _FakeRedis()
    pub = _with_publisher(bot, client, lane=3)
    bot.betting_loop()
    pub.stop()

    rows = _rows(bot)
    assert len(rows) == BETS and len(client.entries) == BETS
    events = [json.loads(data) for _, data in client.entries]
    assert {stream for stream, _ in client.entries} == {"wolfbet:bets"}
    assert [e["seq"] for e in events] == list(range(BETS))
    assert all(e["lane"] == 3 and e["type"] == "bet" for e in events)
    for i, (event, row) in enumerate(zip(events, rows), 1):
        p = event["payload"]
        assert list(p) == list(BET_FIELDS)
        assert (p["session_id"], p["amount"], p["multiplier"], p["rule"], p["bet_value"],
                p["result_value"], p["state"], p["profit"], p["balance_after"]) == row
        assert p["total_bets"] == i
    assert events[-1]["payload"]["session_profit"] == pytest.approx(bot.state.profit)
    assert events[-1]["payload"]["api_ms"] == pytest.approx(bot.state._last_api_ms)
    assert [r[1:] for r in rows] == _expected_rows()


def test_redis_failing_never_slows_or_changes_betting(bot):
    pub = _with_publisher(bot, _FakeRedis(fail=True))
    bot.betting_loop()

    assert bot.state.total_bets == BETS
    assert len(_rows(bot)) == BETS
    assert _wait(lambda: pub.stats()["dropped"] + pub.stats()["overflow"] == BETS)
    assert pub.stats()["sent"] == 0
    assert _rows_without_session(bot) == _expected_rows()
    pub.stop(timeout=0.5)


def test_async_write_mode_also_emits(bot):
    bot.state.db_write_mode = "async"
    bot._start_bet_writer()
    client = _FakeRedis()
    pub = _with_publisher(bot, client)
    bot.betting_loop()
    bot._stop_bet_writer(timeout=5.0)
    pub.stop()

    assert len(client.entries) == BETS
    assert len(_rows(bot)) == BETS


def test_bad_api_value_keeps_the_save_path(bot):
    client = _FakeRedis()
    pub = _with_publisher(bot, client)
    bot._db_save_bet_dispatch({"amount": "abc", "state": "win"}, 0.0, 1.0)
    bot._db_save_bet_dispatch({"amount": 0.0001, "multiplier": 2.0, "bet_value": 49.5,
                               "result_value": 75.0, "state": "win"}, 0.0001, 1000.0001)
    pub.stop()
    assert len(client.entries) == 1


def test_start_events_needs_flag_and_url(bot, monkeypatch, caplog):
    built = []
    monkeypatch.setattr(bot, "EventPublisher",
                        lambda *a, **k: built.append(1) or EventPublisher(*a, **k))

    bot._start_events()
    assert built == [] and bot._event_publisher is None

    bot.state.events_enabled = True
    with caplog.at_level(logging.WARNING, logger="wolfbet"):
        bot._start_events()
    assert built == [] and bot._event_publisher is None
    assert "no events_url" in caplog.text

    bot.state.events_url = _SECRET_URL
    monkeypatch.setitem(sys.modules, "redis", None)
    with caplog.at_level(logging.INFO, logger="wolfbet"):
        bot._start_events()
    assert built == [1] and bot._event_publisher is None
    assert "s3cret-pass" not in caplog.text
    bot.betting_loop()
    assert bot.state.total_bets == BETS


def test_stop_events_flushes_and_clears(bot):
    client = _FakeRedis()
    _with_publisher(bot, client)
    bot._db_save_bet_dispatch({"amount": 0.0001, "multiplier": 2.0, "bet_value": 49.5,
                               "result_value": 75.0, "state": "win"}, 0.0001, 1000.0001)
    bot._stop_events()
    assert bot._event_publisher is None
    assert len(client.entries) == 1

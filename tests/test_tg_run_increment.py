"""TG v1.21.0 — run-wide profit increment (profit_increment_scope=run).

Mirrors CLI v2.37.0 with one run total per user: every slot and session adds
to one total, bumps are shared through <user dir>/run.json, and none / 0
turns the increment fully off (no background run).

Run: APP_ENV=testing pytest wolfbet/tests/test_tg_run_increment.py -q
"""

import asyncio
import json
import os
import sqlite3
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

from tg import engine as tg_engine  # noqa: E402
from tg import handlers as tg_handlers  # noqa: E402
from tg import formatter as tg_formatter  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "wolfbet.db")
    from core.database import init_db, migrate_db
    init_db(path)
    migrate_db(path)
    return path


def _seed(db, rows):
    conn = sqlite3.connect(db)
    for sid, profit in rows:
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, "
            "multiplier, total_bets, wins, losses, profit, wagered) "
            "VALUES (?, '2026-11-14T10:00:00', 'usdt', 'dice', 'Martingale', 0.0001, 2.0, "
            "4, 2, 2, ?, 0.001)", (sid, profit))
    conn.commit()
    conn.close()


def _eng(db, scope="run", inc=0.0001, thr=0.01, **extra):
    cfg = {"base_bet": 0.0001, "strategy": "Martingale", "strategy_key": "2",
           "profit_increment": inc, "profit_threshold": thr,
           "profit_increment_scope": scope, **extra}
    return tg_engine.BettingEngine(user_id=1, db_path=db, api_key="x", config=cfg)


def _run_file(db):
    return os.path.join(os.path.dirname(db), "run.json")


def _write_run(db, first, bumps):
    json.dump({"first_session": first, "started_at": "x", "bumps": bumps}, open(_run_file(db), "w"))


# ── session scope unchanged ───────────────────────────────
def test_session_scope_bumps_on_session_profit(db):
    e = _eng(db, scope="session")
    e._init_run()
    e.profit = 0.01
    assert e._maybe_profit_increment()
    assert e.base_bet == pytest.approx(0.0002)
    assert e.next_profit_milestone == pytest.approx(0.02)
    assert not os.path.exists(_run_file(db))


# ── run scope ─────────────────────────────────────────────
def test_new_run_created_and_bump_saved(db):
    e = _eng(db)
    e._init_run()
    e.session_id = 5
    e._attach_run(5)
    assert json.load(open(_run_file(db)))["first_session"] == 5
    e.profit = 0.01
    assert e._maybe_profit_increment()
    assert e.base_bet == pytest.approx(0.0002)
    assert json.load(open(_run_file(db)))["bumps"] == 1
    assert e.next_profit_milestone == pytest.approx(0.02)


def test_run_total_counts_other_sessions(db):
    _seed(db, [(1, 0.5), (3, 0.004), (4, 0.003)])
    _write_run(db, 3, 0)
    e = _eng(db)
    e._init_run()
    assert e.run_first_session == 3
    assert e._run_others == pytest.approx(0.007)       # session 1 is before the run
    e.profit = 0.002
    assert not e._maybe_profit_increment()
    e.profit = 0.003
    assert e._maybe_profit_increment()


def test_other_slot_picks_up_shared_bump(db):
    _seed(db, [(1, 0.0)])
    _write_run(db, 1, 0)
    a, b = _eng(db), _eng(db)
    for e, sid in ((a, 2), (b, 3)):
        e._init_run()
        e.session_id = sid
        e._attach_run(sid)
    a.profit = 0.01
    assert a._maybe_profit_increment()
    assert b.base_bet == pytest.approx(0.0001)
    assert b._run_sync() is True                       # bump made by slot a
    assert b.base_bet == pytest.approx(0.0002)
    assert b.next_profit_milestone == pytest.approx(0.02)
    assert b._maybe_profit_increment() is True          # loop restarts auto-bet
    assert b._maybe_profit_increment() is False


def test_recurring_restart_does_not_double_bumps(db):
    _seed(db, [(1, 0.02)])
    _write_run(db, 1, 2)
    # recurring carries base_bet (already bumped) + the origin
    e = _eng(db, base_bet=0.0003, _run_base_origin=0.0001)
    e._init_run()
    assert e.base_bet == pytest.approx(0.0003)
    assert e.next_profit_milestone == pytest.approx(0.03)


def test_fresh_bet_applies_bumps_on_config_base(db):
    _seed(db, [(1, 0.02)])
    _write_run(db, 1, 2)
    e = _eng(db)
    e._init_run()
    assert e.base_bet == pytest.approx(0.0003)


def test_resume_without_origin_subtracts_bumps(db):
    _seed(db, [(1, 0.02), (2, 0.0)])
    _write_run(db, 1, 2)
    e = _eng(db, base_bet=0.0003)                      # pre-1.21 snapshot: no origin
    e.session_id = 2
    e._init_run(resumed=True)
    assert e._run_base_origin == pytest.approx(0.0001)
    assert e.base_bet == pytest.approx(0.0003)


def test_snapshot_carries_scope_and_origin(db):
    e = _eng(db)
    e._init_run()
    cfg = e.snapshot_state()["config"]
    assert cfg["profit_increment_scope"] == "run"
    assert cfg["_run_base_origin"] == pytest.approx(0.0001)


def test_stale_run_file_starts_new_run(db):
    _seed(db, [(1, 0.1)])
    _write_run(db, 900, 4)
    e = _eng(db)
    e._init_run()
    assert e.run_first_session is None and e.run_bumps == 0
    assert e.base_bet == pytest.approx(0.0001)


# ── none / 0 = fully off ──────────────────────────────────
@pytest.mark.parametrize("inc,thr,scope", [(None, 0.01, "run"), (0.0, 0.01, "run"),
                                           (0.0001, None, "run"), (0.0001, 0.01, "session")])
def test_off_at_start_removes_run(db, inc, thr, scope):
    _seed(db, [(1, 0.05)])
    _write_run(db, 1, 3)
    e = _eng(db, scope=scope, inc=inc, thr=thr)
    e._init_run()
    e._attach_run(2)
    assert not os.path.exists(_run_file(db))
    assert e.base_bet == pytest.approx(0.0001)


def test_zero_increment_never_bumps(db):
    e = _eng(db, inc=0.0)
    e._init_run()
    e.run_first_session, e.profit = 1, 5.0
    assert not e._maybe_profit_increment()


def test_mutate_off_ends_run_and_on_starts_one(db):
    e = _eng(db)
    e._init_run()
    e.session_id = 7
    e._attach_run(7)
    msg = asyncio.run(e.mutate({"profit_increment": 0.0}))
    assert e.profit_increment is None and "ended" in msg
    assert not os.path.exists(_run_file(db))
    msg = asyncio.run(e.mutate({"profit_increment": 0.0002}))
    assert e.run_first_session == 7 and "started" in msg
    assert json.load(open(_run_file(db)))["first_session"] == 7


def test_run_sync_drops_run_after_reset(db):
    e = _eng(db)
    e._init_run()
    e.session_id = 3
    e._attach_run(3)
    os.remove(_run_file(db))
    assert e._run_sync() is False
    assert e.run_first_session is None


# ── display + handlers ────────────────────────────────────
def test_formatter_shows_run_line():
    lines = tg_formatter.format_full_config({
        "profit_increment": 0.0001, "profit_threshold": 0.01, "next_profit_milestone": 0.02,
        "profit_increment_scope": "run", "run_first_session": 4, "run_profit_total": 0.015,
        "run_bumps": 1})
    text = "\n".join(lines)
    assert "run profit" in text and "since #4, 1 bump(s)" in text


def _update(user_id=1):
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)
    msg = types.SimpleNamespace(reply_text=reply_text)
    upd = types.SimpleNamespace(effective_user=types.SimpleNamespace(id=user_id),
                                message=msg, effective_message=msg,
                                effective_chat=types.SimpleNamespace(id=1))
    return upd, sent


def test_set_piscope(monkeypatch):
    store = {}
    monkeypatch.setattr(tg_handlers, "load_user_config", lambda uid: dict(store))
    monkeypatch.setattr(tg_handlers, "save_user_config", lambda uid, c: store.update(c))
    upd, sent = _update()
    ctx = types.SimpleNamespace(args=["piscope", "run"])
    asyncio.run(tg_handlers.cmd_set(upd, ctx))
    assert store.get("profit_increment_scope") == "run"
    ctx = types.SimpleNamespace(args=["piscope", "forever"])
    asyncio.run(tg_handlers.cmd_set(upd, ctx))
    assert store["profit_increment_scope"] == "run" and "session" in sent[-1]
    asyncio.run(tg_handlers.cmd_set(upd, types.SimpleNamespace(args=["pi", "0"])))
    assert store["profit_increment"] is None


def test_runreset_refuses_while_running_and_deletes_when_idle(monkeypatch, tmp_path):
    db = str(tmp_path / "wolfbet.db")
    monkeypatch.setattr(tg_handlers, "user_db_path", lambda uid: db)
    run = os.path.join(str(tmp_path), "run.json")
    open(run, "w").write("{}")
    upd, sent = _update(user_id=4242)
    monkeypatch.setattr(tg_handlers, "_get_running", lambda uid: {1: object()})
    asyncio.run(tg_handlers.cmd_runreset(upd, types.SimpleNamespace(args=[])))
    assert os.path.exists(run) and "Stop" in sent[-1]
    monkeypatch.setattr(tg_handlers, "_get_running", lambda uid: {})
    asyncio.run(tg_handlers.cmd_runreset(upd, types.SimpleNamespace(args=[])))
    assert not os.path.exists(run) and "reset" in sent[-1]


def test_recurring_carry_and_bot_wiring():
    src = open(os.path.join(_WOLFBET_DIR, "tg", "handlers.py")).read()
    assert 'config["_run_base_origin"] = getattr(engine, "_run_base_origin", None)' in src
    bot = open(os.path.join(_WOLFBET_DIR, "tg", "bot.py")).read()
    assert 'CommandHandler("runreset", cmd_runreset)' in bot
    from tg.config import CONFIG_KEYS
    assert "profit_increment_scope" in CONFIG_KEYS

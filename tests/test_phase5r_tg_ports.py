"""Phase 5r — TG ports from CLI (v1.18.0): /diagnose, /analytics, uptime
pause bookkeeping, SQLite PRAGMA tuning.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5r_tg_ports.py -q
"""

import inspect
import os
import sqlite3
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

from tg import engine as tg_engine  # noqa: E402
from tg import handlers as tg_handlers  # noqa: E402
from tg import bot as tg_bot  # noqa: E402


def _mk_engine(db_path):
    return tg_engine.BettingEngine(
        user_id=1, db_path=db_path, api_key="x",
        config={"base_bet": 0.0001, "strategy": "Flat Bet", "strategy_key": "1"},
    )


def _mk_db():
    fd, path = tempfile.mkstemp(prefix="wb_tg_", suffix=".db")
    os.close(fd)
    os.remove(path)
    from core.database import init_db, migrate_db
    init_db(path)
    migrate_db(path)
    return path


# ── uptime pause fields ──────────────────────────────────
def test_fresh_engine_has_no_frozen_timestamps():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        assert e.session_bet_ended_at is None
        assert e.session_pause_started_at is None
    finally:
        os.remove(db)


def test_pause_freezes_timestamps():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time() - 300
        e.pause()
        assert e.session_bet_ended_at is not None
        assert e.session_pause_started_at is not None
    finally:
        os.remove(db)


def test_resume_clears_timestamps():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time() - 300
        e.pause()
        e.resume()
        assert e.session_bet_ended_at is None
        assert e.session_pause_started_at is None
    finally:
        os.remove(db)


def test_pause_is_idempotent():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time() - 300
        e.pause()
        first = e.session_bet_ended_at
        time.sleep(0.01)
        e.pause()
        assert e.session_bet_ended_at == first
    finally:
        os.remove(db)


# ── get_status uptime freeze ──────────────────────────────
def test_get_status_uses_now_when_active():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time() - 300
        e._status_cache = None
        st = e.get_status()
        assert 299 <= st["uptime_sec"] <= 301
        assert st["pause_remaining_sec"] == 0
    finally:
        os.remove(db)


def test_get_status_freezes_when_paused():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time() - 600
        e.session_bet_ended_at = time.time() - 300  # bet ended 5 min ago
        e._status_cache = None
        st = e.get_status()
        assert 299 <= st["uptime_sec"] <= 301
    finally:
        os.remove(db)


def test_status_dict_always_has_new_keys():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        e.session_start = time.time()
        e._status_cache = None
        st = e.get_status()
        assert "uptime_sec" in st
        assert "pause_remaining_sec" in st
    finally:
        os.remove(db)


# ── SQLite PRAGMA tuning ─────────────────────────────────
def test_get_conn_applies_all_pragmas():
    db = _mk_db()
    try:
        e = _mk_engine(db)
        conn = e._get_conn()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        cs = conn.execute("PRAGMA cache_size").fetchone()[0]
        assert cs == -32000
        ts = conn.execute("PRAGMA temp_store").fetchone()[0]
        assert ts == 2  # MEMORY
    finally:
        e._db_connection = None
        os.remove(db)


def test_source_get_conn_has_all_pragmas():
    src = inspect.getsource(tg_engine.BettingEngine._get_conn)
    assert "synchronous = NORMAL" in src
    assert "busy_timeout = 5000" in src
    assert "cache_size = -32000" in src
    assert "temp_store = MEMORY" in src


# ── DB ended_at freeze ────────────────────────────────────
def test_db_save_session_uses_frozen_ended_at():
    db = _mk_db()
    try:
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO sessions (id, started_at, currency, game, strategy, "
                     "base_bet, multiplier, start_balance) VALUES (?,?,?,?,?,?,?,?)",
                     (1, "2026-07-19T09:00:00", "usdt", "dice", "Flat", 0.0001, 2.0, 1.0))
        conn.commit()
        conn.close()
        e = _mk_engine(db)
        e.session_id = 1
        e.session_bet_ended_at = time.time() - 3500
        e._db_save_session(final=True)
        conn = sqlite3.connect(db)
        ended_at_str = conn.execute("SELECT ended_at FROM sessions WHERE id=1").fetchone()[0]
        conn.close()
        e._db_connection = None
        from datetime import datetime as _dt
        delta = time.time() - _dt.fromisoformat(ended_at_str).timestamp()
        assert 3400 < delta < 3600
    finally:
        os.remove(db)


def test_source_db_save_session_freezes_ended_at():
    src = inspect.getsource(tg_engine.BettingEngine._db_save_session)
    assert "session_bet_ended_at" in src


# ── /diagnose ─────────────────────────────────────────────
def test_cmd_diagnose_exists():
    assert callable(tg_handlers.cmd_diagnose)


def test_cmd_diagnose_registered_in_bot():
    src = inspect.getsource(tg_bot)
    assert 'CommandHandler("diagnose"' in src


def test_cmd_diagnose_source_has_top_blocker_and_advice():
    src = inspect.getsource(tg_handlers.cmd_diagnose)
    assert "Top blocker" in src
    assert "Advice" in src
    assert "Network / API latency" in src


# ── /analytics ────────────────────────────────────────────
def test_cmd_analytics_exists():
    assert callable(tg_handlers.cmd_analytics)


def test_cmd_analytics_registered_in_bot():
    src = inspect.getsource(tg_bot)
    assert 'CommandHandler("analytics"' in src


def test_cmd_analytics_source_has_all_sections():
    src = inspect.getsource(tg_handlers.cmd_analytics)
    assert "Lifetime" in src
    assert "By game" in src
    assert "By strategy" in src
    assert "Top 5 best" in src
    assert "Top 5 worst" in src


# ── /help ─────────────────────────────────────────────────
def test_help_mentions_new_commands():
    src = inspect.getsource(tg_handlers.cmd_help)
    assert "/diagnose" in src
    assert "/analytics" in src


# ── version ───────────────────────────────────────────────
def test_tg_version_bumped():
    import tg
    assert tg.VERSION == "1.20.0"

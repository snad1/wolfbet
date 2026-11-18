"""Phase 5q tests - session-uptime freezes during recurring pause (v2.28.4).

Bug: `wolfctl status` showed a session's `Uptime` as ~1 h 5 min when only
5 min of that was actual betting - the rest was the `recurring_delay=3600`
sleep. `state.session_start` was set once at session start and time.time()
kept ticking during the sleep, so uptime and DB `ended_at` both drifted.

Fix: freeze `state.session_bet_ended_at` right before the sleep in
`_reset_session_for_recurring`; clear it just before setting the new
`session_start`. `_save_state_file` and `_db_save_session` use the frozen
value when set.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5q_uptime_pause.py -q
"""

import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


# ── Defaults ──────────────────────────────────────────────
def test_default_bet_ended_at_is_none():
    """Fresh state must have no frozen bet-end - session is actively betting."""
    s = wb.BotState()
    assert s.session_bet_ended_at is None
    assert s.session_pause_started_at is None


# ── _reset_session_for_recurring freezes then clears ─────
def test_reset_recurring_freezes_before_sleep_and_clears_after(monkeypatch):
    """During the sleep window, session_bet_ended_at must be set. After the
    sleep + new session_start, it must be None again."""
    seen_during_sleep = {}

    def fake_sleep(_secs):
        # Snapshot the frozen values while we're "in" the pause.
        seen_during_sleep["bet_ended_at"] = wb.state.session_bet_ended_at
        seen_during_sleep["pause_started_at"] = wb.state.session_pause_started_at

    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(wb, "_db_start_session", lambda: 999)

    wb.state.recurring_delay = 60.0
    wb.state.session_bet_ended_at = None
    wb.state.session_pause_started_at = None

    wb._reset_session_for_recurring()

    # During sleep: both frozen timestamps were set.
    assert seen_during_sleep["bet_ended_at"] is not None
    assert seen_during_sleep["pause_started_at"] is not None
    # After reset: both cleared, new session_start bumped forward.
    assert wb.state.session_bet_ended_at is None
    assert wb.state.session_pause_started_at is None


def test_reset_recurring_skips_freeze_when_delay_zero(monkeypatch):
    """recurring_delay=0 means back-to-back cycles; freezing still runs
    (it's cheap + consistent) but pause-remaining is 0 in state file."""
    monkeypatch.setattr(wb, "_db_start_session", lambda: 999)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    wb.state.recurring_delay = 0.0
    wb._reset_session_for_recurring()
    # Still cleared after.
    assert wb.state.session_bet_ended_at is None


# ── _save_state_file: uptime + pause_remaining_sec ────────
def _write_state_and_read(monkeypatch, tmp_path,
                          session_start_offset, bet_ended_offset=None,
                          pause_started_offset=None, recurring_delay=60.0):
    """Set state fields relative to now, save state file, return the JSON dict."""
    state_path = str(tmp_path / "wb_state.json")
    monkeypatch.setattr(wb, "STATE_PATH", state_path)
    now = time.time()
    wb.state.session_start = now - session_start_offset
    wb.state.session_bet_ended_at = (
        now - bet_ended_offset if bet_ended_offset is not None else None
    )
    wb.state.session_pause_started_at = (
        now - pause_started_offset if pause_started_offset is not None else None
    )
    wb.state.recurring_delay = recurring_delay
    # v2.32.0 - the pause in progress records its own length (streak reset
    # can use a different delay than recurring_delay).
    wb.state.session_pause_total = recurring_delay
    wb._save_state_file()
    with open(state_path) as f:
        return json.load(f)


def test_uptime_uses_now_when_actively_betting(monkeypatch, tmp_path):
    d = _write_state_and_read(monkeypatch, tmp_path,
                              session_start_offset=300)  # 5 min ago
    assert 299 <= d["uptime_sec"] <= 301
    assert d["pause_remaining_sec"] == 0


def test_uptime_freezes_at_bet_end_during_pause(monkeypatch, tmp_path):
    # Bet ran for 5 min, then paused 2 min ago. recurring_delay=1h.
    d = _write_state_and_read(monkeypatch, tmp_path,
                              session_start_offset=420,   # 7 min ago
                              bet_ended_offset=120,       # bet ended 2 min ago
                              pause_started_offset=120,
                              recurring_delay=3600.0)
    # Uptime = 7 min - 2 min = 5 min (the bet phase, not incl. pause).
    assert 299 <= d["uptime_sec"] <= 301
    # Pause remaining = 3600 - 120 = 3480.
    assert 3479 <= d["pause_remaining_sec"] <= 3481
    assert d["recurring_delay_sec"] == 3600


def test_pause_remaining_clamps_to_zero_when_expired(monkeypatch, tmp_path):
    d = _write_state_and_read(monkeypatch, tmp_path,
                              session_start_offset=4000,
                              bet_ended_offset=3900,
                              pause_started_offset=3900,   # paused > recurring_delay
                              recurring_delay=60.0)
    assert d["pause_remaining_sec"] == 0


# ── _db_save_session freezes ended_at during pause ────────
def test_db_save_session_uses_frozen_ended_at(monkeypatch, tmp_path):
    """When session_bet_ended_at is set, DB row's ended_at must be that
    timestamp, not now(). Prevents 1-hour pauses lumped into session runtime."""
    fd, db_path = tempfile.mkstemp(prefix="wb_endedat_", suffix=".db")
    os.close(fd)
    os.remove(db_path)
    monkeypatch.setattr(wb, "DB_PATH", db_path)
    wb.init_db()
    # Seed sessions row so UPDATE has something to hit.
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO sessions (id, started_at, currency, game, strategy, "
                 "base_bet, multiplier, start_balance) VALUES (?,?,?,?,?,?,?,?)",
                 (1, "2026-07-19T09:00:00Z", "usdt", "dice", "Flat", 0.0001, 2.0, 1.0))
    conn.commit()
    conn.close()
    try:
        wb.state.session_id = 1
        # Bet ended at a known past timestamp.
        wb.state.session_bet_ended_at = time.time() - 3500  # ~58 min ago
        wb.state.lowest_balance = 1.0
        wb.state.current_balance = 1.001
        wb.state.total_bets = 100
        wb.state.wins = 10; wb.state.losses = 90
        wb.state.profit = 0.001; wb.state.wagered = 0.01
        wb.state.max_win_streak = 3; wb.state.max_loss_streak = 30
        wb.state.highest_balance = 1.002
        wb.state.highest_win = 0.001; wb.state.biggest_loss = 0.0005
        wb.state.bets_per_second = 20; wb.state.bets_per_minute = 1200
        wb.state.rate_limit = 4000
        wb.state.peak_bps = 30; wb.state.low_bps = 5
        wb.state.peak_bpm = 1500; wb.state.low_bpm = 400
        wb._db_save_session()
        # Read back ended_at.
        conn = sqlite3.connect(db_path)
        ended_at_str = conn.execute("SELECT ended_at FROM sessions WHERE id=1").fetchone()[0]
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

    # ended_at must be the frozen bet-end, NOT "now" - 58 min in the past.
    from datetime import datetime as _dt
    ended_at = _dt.fromisoformat(ended_at_str)
    delta = time.time() - ended_at.timestamp()
    assert 3400 < delta < 3600, (
        f"ended_at should be ~58 min in the past (frozen), got delta={delta:.1f}s"
    )


def test_db_save_session_uses_now_when_no_freeze(monkeypatch, tmp_path):
    """When session_bet_ended_at is None (active betting), ended_at = now."""
    fd, db_path = tempfile.mkstemp(prefix="wb_now_", suffix=".db")
    os.close(fd)
    os.remove(db_path)
    monkeypatch.setattr(wb, "DB_PATH", db_path)
    wb.init_db()
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO sessions (id, started_at, currency, game, strategy, "
                 "base_bet, multiplier, start_balance) VALUES (?,?,?,?,?,?,?,?)",
                 (2, "2026-07-19T10:00:00Z", "usdt", "dice", "Flat", 0.0001, 2.0, 1.0))
    conn.commit()
    conn.close()
    try:
        wb.state.session_id = 2
        wb.state.session_bet_ended_at = None
        wb.state.lowest_balance = 1.0
        wb._db_save_session()
        conn = sqlite3.connect(db_path)
        ended_at_str = conn.execute("SELECT ended_at FROM sessions WHERE id=2").fetchone()[0]
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)

    from datetime import datetime as _dt
    ended_at = _dt.fromisoformat(ended_at_str)
    delta = time.time() - ended_at.timestamp()
    assert delta < 5, f"ended_at should be ~now, got delta={delta:.1f}s"


# ── Monitor header shows pause indicator ──────────────────
def test_monitor_header_shows_paused_indicator():
    """When state file has pause_remaining_sec > 0, header must show
    'PAUSED HH:MM:SS/HH:MM:SS' instead of '● LIVE'."""
    import inspect
    src = inspect.getsource(wb._build_monitor_screen)
    assert "PAUSED" in src
    assert "pause_remaining_sec" in src


# ── cmd_status renders pause remaining ────────────────────
def test_cmd_status_uses_pause_remaining():
    import inspect
    src = inspect.getsource(wb.cmd_status)
    assert "pause_remaining_sec" in src
    assert "Recurring pause" in src


# ── State-file field is always present ────────────────────
def test_state_file_always_has_pause_fields():
    """Even when not paused, the keys exist so downstream renderers can
    key off them without KeyError."""
    import inspect
    src = inspect.getsource(wb._state_snapshot)
    assert '"pause_remaining_sec"' in src
    assert '"recurring_delay_sec"' in src

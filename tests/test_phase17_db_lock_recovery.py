"""A briefly locked database delays a session start; a crash stops the lane with its reason."""

import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


@pytest.fixture
def lane(monkeypatch, tmp_path):
    """A lane with its own database and live file in tmp."""
    db = str(tmp_path / "bot.db")
    monkeypatch.setattr(wb, "DB_PATH", db)
    monkeypatch.setattr(wb, "STATE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr(wb, "_BASE_STATE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr(wb, "state", wb.BotState())
    monkeypatch.setattr(wb, "_emit_lifecycle", lambda *a, **k: None)
    monkeypatch.setattr(wb.time, "sleep", lambda s: None)
    from core.database import init_db, migrate_db
    init_db(db)
    migrate_db(db)
    wb.state.lane = 3
    return db


def test_a_locked_database_is_waited_out_and_the_session_starts(lane, monkeypatch):
    real = wb._insert_session_row
    calls = []

    def locked_twice():
        calls.append(wb.state.status)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real()

    monkeypatch.setattr(wb, "_insert_session_row", locked_twice)
    sid = wb._db_start_session()
    assert len(calls) == 3
    assert calls[-1] == wb.DB_LOCKED_STATUS
    with sqlite3.connect(lane) as conn:
        assert conn.execute("SELECT lane FROM sessions WHERE id = ?", (sid,)).fetchone() == (3,)


def test_a_database_locked_too_long_raises(lane, monkeypatch):
    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(wb, "_insert_session_row", always_locked)
    with pytest.raises(sqlite3.OperationalError):
        wb._db_start_session()


def test_other_database_errors_are_not_retried(lane, monkeypatch):
    calls = []

    def broken():
        calls.append(1)
        raise sqlite3.OperationalError("no such table: sessions")

    monkeypatch.setattr(wb, "_insert_session_row", broken)
    with pytest.raises(sqlite3.OperationalError):
        wb._db_start_session()
    assert calls == [1]


def test_a_crash_in_the_loop_stops_the_lane_with_the_reason(lane, monkeypatch):
    def crash():
        raise sqlite3.OperationalError("database is locked")

    saved = []
    monkeypatch.setattr(wb, "_run_betting_loop", crash)
    monkeypatch.setattr(wb, "_db_save_session", lambda: saved.append("session"))
    monkeypatch.setattr(wb, "_save_state_file", lambda: saved.append("live"))
    monkeypatch.setattr(wb, "api_auto_stop", lambda: saved.append("auto_stop"))
    wb.state.game = "dice-auto"
    wb.state.running = True
    wb._guarded_betting_loop()
    assert wb.state.running is False
    assert wb.state.status == "STOPPED: internal error: OperationalError: database is locked"
    assert wb.state.last_error == "internal error: OperationalError: database is locked"
    assert saved == ["auto_stop", "session", "live"]


def test_only_lane_one_purges_bets(lane, monkeypatch):
    purged = []
    monkeypatch.setattr(wb, "purge_live_bets", lambda *a, **k: purged.append(a) or 0)
    wb.state.running = True
    wb._retention_worker()
    assert purged == []


def test_lane_one_purges_in_batches_without_vacuum(lane, monkeypatch):
    calls = []

    def purge(path, **kwargs):
        calls.append((path, kwargs))
        wb.state.running = False
        return 0

    monkeypatch.setattr(wb, "purge_live_bets", purge)
    wb.state.lane, wb.state.purge_days, wb.state.running = 1, 1, True
    wb._retention_worker()
    assert calls == [(lane, {"bet_age_days": 1})]

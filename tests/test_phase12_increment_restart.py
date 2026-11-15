"""Deferred auto-cycle restart on a profit increment, and the exit reason rows.

A run-scope increment can land on a losing bet. Clearing the auto UUID there made
the next loop tick hit the mid-chain guard, which stops the daemon.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase12_increment_restart.py -q
"""

import inspect
import io
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from rich.console import Console  # noqa: E402

_ATTRS = ("game", "base_bet", "current_bet", "profit", "current_streak",
          "profit_increment", "profit_threshold", "next_profit_milestone",
          "profit_increment_scope", "run_first_session", "run_bumps",
          "_auto_uuid", "_auto_restart_pending", "status", "session_id",
          "total_bets", "wins", "losses", "wagered", "start_balance",
          "current_balance", "highest_balance", "lowest_balance",
          "max_win_streak", "max_loss_streak", "highest_win", "biggest_loss",
          "currency", "bets_per_second", "bets_per_minute", "peak_bps",
          "low_bps", "peak_bpm", "low_bpm", "rate_limit", "_bets_this_sec",
          "_bets_this_min")


@pytest.fixture(autouse=True)
def _isolate():
    saved = {a: getattr(wb.state, a, None) for a in _ATTRS}
    s = wb.state
    s.game = "dice-auto"
    s.base_bet = s.current_bet = 0.0001
    s.profit = 0.0
    s.current_streak = 0
    s.profit_increment = 0.00000001
    s.profit_threshold = 0.0005
    s.next_profit_milestone = 0.0005
    s.profit_increment_scope = "session"
    s.run_first_session = None          # keeps _save_run a no-op
    s._auto_uuid = "uuid-old"
    s._auto_restart_pending = False
    s.status = ""
    yield
    for a, v in saved.items():
        setattr(wb.state, a, v)


def _fire_increment():
    wb.state.profit = 0.0005
    wb._check_profit_increment()


# ── increment defers instead of clearing ─────────────────
def test_increment_on_losing_chain_keeps_uuid(monkeypatch):
    monkeypatch.setattr(wb, "run_profit_total", lambda: 0.01)
    wb.state.profit_increment_scope = "run"
    wb.state.current_streak = -1
    wb.state.profit = -0.00005
    wb._check_profit_increment()
    assert wb.state._auto_uuid == "uuid-old"
    assert wb.state._auto_restart_pending is True
    assert wb.state.base_bet == pytest.approx(0.00010001)


def test_increment_on_closed_chain_still_defers_to_loop():
    _fire_increment()
    assert wb.state._auto_uuid == "uuid-old"
    assert wb.state._auto_restart_pending is True


def test_increment_off_touches_nothing():
    wb.state.profit_increment = 0.0
    _fire_increment()
    assert wb.state._auto_uuid == "uuid-old"
    assert wb.state._auto_restart_pending is False


# ── the loop-side helper decides when the swap happens ───
def test_pending_restart_waits_out_a_loss_chain():
    wb.state._auto_restart_pending = True
    wb.state.current_streak = -1
    wb.state.profit = -0.00005
    wb._maybe_apply_pending_auto_restart()
    assert wb.state._auto_uuid == "uuid-old"
    assert wb.state._auto_restart_pending is True


def test_pending_restart_applies_once_profit_recovers():
    wb.state._auto_restart_pending = True
    wb.state.current_streak = 1
    wb.state.profit = 0.00002
    wb._maybe_apply_pending_auto_restart()
    assert wb.state._auto_uuid is None
    assert wb.state._auto_restart_pending is False


def test_pending_restart_waits_while_only_streak_is_negative():
    wb.state._auto_restart_pending = True
    wb.state.current_streak = -2
    wb.state.profit = 0.0
    wb._maybe_apply_pending_auto_restart()
    assert wb.state._auto_uuid == "uuid-old"


def test_pending_restart_ignores_manual_games():
    wb.state.game = "dice"
    wb.state._auto_restart_pending = True
    wb._maybe_apply_pending_auto_restart()
    assert wb.state._auto_uuid == "uuid-old"


# ── the exit reason is recorded ──────────────────────────
def test_summary_prints_stop_reason(monkeypatch):
    wb.state.status = "STOPPED: Max wins reached: 5"
    buf = io.StringIO()
    monkeypatch.setattr(wb, "console",
                        Console(file=buf, force_terminal=False, width=200))
    wb._print_summary()
    out = buf.getvalue()
    assert "Stop reason" in out
    assert "Max wins reached: 5" in out


def test_daemon_exit_warning_exists():
    src = inspect.getsource(wb.main)
    assert "DAEMON EXIT" in src and "reason=%s" in src

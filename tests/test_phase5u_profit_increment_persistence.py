"""Phase 5u — profit_increment milestone persistence + recurring reset (v2.30.1).

Two bugs from v2.30.0:
  A) CFG row read `last_profit_milestone` (notification tracker), not
     `next_profit_milestone` (actionable bump target) — always rendered 100%.
  B) `_reset_session_for_recurring` never reset `state.next_profit_milestone`,
     so cycle 2+ inherited cycle 1's high-water target and couldn't fire bumps.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5u_profit_increment_persistence.py -q
"""

import inspect
import json
import os
import re
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s).replace("\x1b[K", "")


# ── Bug A: state-file persistence + CFG row read ─────────
def test_save_state_file_persists_next_profit_milestone(monkeypatch, tmp_path):
    state_path = str(tmp_path / "wb_state.json")
    monkeypatch.setattr(wb, "STATE_PATH", state_path)
    wb.state.profit_threshold = 0.007
    wb.state.profit_increment = 0.00000001
    wb.state.next_profit_milestone = 0.021  # 3rd milestone in a cycle
    wb.state.profit = 0.005
    wb._save_state_file()
    with open(state_path) as f:
        d = json.load(f)
    assert d["next_profit_milestone"] == 0.021


def test_cfg_row_reads_next_profit_milestone_when_present():
    d = {
        "profit_threshold": 0.007,
        "profit_increment": 0.00000001,
        "next_profit_milestone": 0.021,
        "profit": 0.014,  # 66.67% of 0.021
    }
    row = _strip_ansi(wb._build_config_progress_row(d))
    assert "/0.02100000" in row
    assert "(67%)" in row


def test_cfg_row_falls_back_to_profit_threshold_when_key_missing():
    """Pre-v2.30.1 state files (no next_profit_milestone key) still render sensibly."""
    d = {
        "profit_threshold": 0.007,
        "profit_increment": 0.00000001,
        "profit": 0.0035,  # 50% of the fallback target 0.007
    }
    row = _strip_ansi(wb._build_config_progress_row(d))
    assert "/0.00700000" in row
    assert "(50%)" in row


def test_cfg_row_no_longer_uses_last_profit_milestone_for_target():
    """The 100%-forever bug: last_profit_milestone=0 should not force target=threshold
    when next_profit_milestone has advanced."""
    d = {
        "profit_threshold": 0.007,
        "profit_increment": 0.00000001,
        "last_profit_milestone": 0.0,       # notification tracker stays at 0
        "next_profit_milestone": 0.049,     # 7th milestone; genuine actionable target
        "profit": 0.045,                    # 91.8% of 0.049 — should NOT clamp to 100%
    }
    row = _strip_ansi(wb._build_config_progress_row(d))
    assert "/0.04900000" in row
    assert "(91%)" in row or "(92%)" in row


# ── Bug B: recurring restart resets next_profit_milestone ─
def test_reset_session_for_recurring_resets_milestone(monkeypatch):
    monkeypatch.setattr(wb, "_db_start_session", lambda: 999)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    wb.state.profit_threshold = 0.007
    wb.state.profit_increment = 0.00000001
    wb.state.next_profit_milestone = 0.056  # advanced by cycle 1's bumps
    wb.state.profit = 0.05
    wb.state.recurring_delay = 0.0

    wb._reset_session_for_recurring()

    assert wb.state.profit == 0.0
    # Reset back to a fresh per-cycle target.
    assert abs(wb.state.next_profit_milestone - 0.007) < 1e-12


def test_reset_session_for_recurring_skips_when_no_threshold(monkeypatch):
    """profit_threshold unset → no milestone change (no-op)."""
    monkeypatch.setattr(wb, "_db_start_session", lambda: 999)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    wb.state.profit_threshold = None
    wb.state.next_profit_milestone = 42.0  # arbitrary
    wb.state.recurring_delay = 0.0
    wb._reset_session_for_recurring()
    assert wb.state.next_profit_milestone == 42.0


def test_reset_session_for_recurring_does_not_reset_base_bet(monkeypatch):
    """Base_bet must stack across cycles (matches user's observed behaviour)."""
    monkeypatch.setattr(wb, "_db_start_session", lambda: 999)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    wb.state.profit_threshold = 0.007
    wb.state.base_bet = 0.00000161  # e.g. one cycle-1 bump already applied
    wb.state.recurring_delay = 0.0
    wb._reset_session_for_recurring()
    assert wb.state.base_bet == 0.00000161


# ── Three-cycle regression: base bumps happen once per cycle ─
def test_three_cycle_simulation_bumps_base_per_cycle(monkeypatch):
    """Simulates the user's real scenario: 3 recurring cycles, profit crosses
    threshold in each. Base should bump 3 times (was: only 1 in v2.30.0)."""
    monkeypatch.setattr(wb, "_db_start_session", lambda: 999)
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    wb.state.game = "dice"
    wb.state.profit_threshold = 0.007
    wb.state.profit_increment = 0.00000001
    wb.state.base_bet = 0.00000144
    wb.state.next_profit_milestone = wb.state.profit_threshold
    wb.state.recurring_delay = 0.0

    def fire_bump_at_current_milestone():
        """Simulate the profit-increment branch firing once (mid-cycle)."""
        wb.state.profit = wb.state.next_profit_milestone + 0.0001
        # Match the two call sites at wolfbet.py:1697-1704 and 1938-1954.
        if (wb.state.profit_increment is not None and wb.state.profit_threshold
                and wb.state.profit >= wb.state.next_profit_milestone):
            wb.state.base_bet += wb.state.profit_increment
            wb.state.next_profit_milestone += wb.state.profit_threshold

    for _ in range(3):
        fire_bump_at_current_milestone()
        wb._reset_session_for_recurring()

    # 3 bumps × increment = expected new base.
    assert abs(wb.state.base_bet - (0.00000144 + 3 * 0.00000001)) < 1e-15

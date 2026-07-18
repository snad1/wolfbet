"""Phase 5h tests — purge_days + --cleanup (TG↔CLI parity).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5h_purge_days.py -q
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _reset_state():
    s = wb.state
    s.game = "dice"
    s.strategy_key = "1"
    s.strategy = "Flat Bet"
    s.current_balance = 1.0
    s.purge_days = 3


# ── Defaults + membership ─────────────────────────────────
def test_default_purge_days_3():
    _reset_state()
    assert wb.state.purge_days == 3


def test_purge_days_in_config_keys():
    assert "purge_days" in wb._CONFIG_KEYS


def test_purge_days_in_preset_keys():
    assert "purge_days" in wb._PRESET_KEYS


def test_purge_days_in_tweak_params():
    assert "purge_days" in wb._TWEAK_PARAMS


def test_purge_days_in_show_fields():
    assert any(f[0] == "purge_days" for f in wb._CONFIG_SHOW_FIELDS)


def test_alias_purgedays():
    assert wb._TWEAK_ALIAS.get("purgedays") == "purge_days"


# ── _apply_mutation clamps ────────────────────────────────
def test_apply_mutation_sets_value():
    _reset_state()
    wb._apply_mutation({"purge_days": "7"})
    assert wb.state.purge_days == 7


def test_apply_mutation_clamps_low():
    _reset_state()
    wb._apply_mutation({"purge_days": "0"})
    assert wb.state.purge_days == 1


def test_apply_mutation_clamps_high():
    _reset_state()
    wb._apply_mutation({"purge_days": "999"})
    assert wb.state.purge_days == 30


def test_apply_mutation_off_resets_to_default():
    _reset_state()
    wb.state.purge_days = 15
    wb._apply_mutation({"purge_days": "off"})
    assert wb.state.purge_days == 3


def test_apply_mutation_bad_raises():
    _reset_state()
    try:
        wb._apply_mutation({"purge_days": "abc"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ── cmd_cleanup delegates to cleanup_old_bets ─────────────
def test_cmd_cleanup_uses_state_default(monkeypatch):
    _reset_state()
    wb.state.purge_days = 5
    calls = []
    monkeypatch.setattr(wb, "cleanup_old_bets",
                        lambda path, bet_age_days: calls.append((path, bet_age_days)) or 0)
    monkeypatch.setattr(wb, "load_config", lambda: True)  # avoid disk touch
    wb.cmd_cleanup(None)
    assert calls, "cleanup_old_bets was not called"
    assert calls[0][1] == 5


def test_cmd_cleanup_explicit_days(monkeypatch):
    _reset_state()
    calls = []
    monkeypatch.setattr(wb, "cleanup_old_bets",
                        lambda path, bet_age_days: calls.append((path, bet_age_days)) or 42)
    monkeypatch.setattr(wb, "load_config", lambda: True)
    wb.cmd_cleanup(10)
    assert calls[0][1] == 10


def test_cmd_cleanup_clamps_to_range(monkeypatch):
    _reset_state()
    calls = []
    monkeypatch.setattr(wb, "cleanup_old_bets",
                        lambda path, bet_age_days: calls.append((path, bet_age_days)) or 0)
    monkeypatch.setattr(wb, "load_config", lambda: True)
    wb.cmd_cleanup(0)
    assert calls[0][1] == 1
    wb.cmd_cleanup(9999)
    assert calls[1][1] == 30


# ── _retention_worker exists and is callable ─────────────
def test_retention_worker_exists():
    assert callable(wb._retention_worker)

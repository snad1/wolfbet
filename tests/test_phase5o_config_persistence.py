"""Phase 5o tests — config persistence round-trip (v2.28.2 fix).

Bug: `wolfctl config-set recurring=on` printed the diff and "Saved to …"
but the value didn't persist because `recurring` wasn't in _CONFIG_KEYS.
Same silent failure for basebet_pct, all four milestone_* fields, and
the four streak-tuple fields. Present in wolfctl config-show + preset
+ mutation grammar since Phase 2 (v2.16.0), but never written to
~/.wolfbet_autobot.json.

This test locks the round-trip contract per field.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5o_config_persistence.py -q
"""

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _fresh_config_path(monkeypatch):
    """Swap CONFIG_PATH to a tmpfile; return path (caller ignores; monkeypatch cleans up)."""
    fd, path = tempfile.mkstemp(prefix="wb_cfg_", suffix=".json")
    os.close(fd)
    os.remove(path)   # save_config will re-create
    monkeypatch.setattr(wb, "CONFIG_PATH", path)
    return path


def _round_trip(monkeypatch, mutations: dict):
    """Set fields via _apply_mutation, save, clear, load, return final state."""
    path = _fresh_config_path(monkeypatch)
    # Set defaults BEFORE mutation so _apply_mutation is exercised end-to-end.
    wb._apply_mutation(mutations)
    wb.save_config()
    # Wipe the target attributes to prove they come back from disk.
    for k in mutations:
        canonical = wb._TWEAK_ALIAS.get(k, k)
        if canonical == "recurring":
            wb.state.recurring = False
        elif canonical == "basebet_pct":
            wb.state.basebet_pct = 0.0
        elif canonical == "milestone_bets":
            wb.state.milestone_bets = 100
        elif canonical == "milestone_wins":
            wb.state.milestone_wins = 0
        elif canonical == "milestone_losses":
            wb.state.milestone_losses = 0
        elif canonical == "milestone_profit":
            wb.state.milestone_profit = 0.0
    wb.load_config()
    return path


# ── Config-key completeness ───────────────────────────────
def test_recurring_in_config_keys():
    assert "recurring" in wb._CONFIG_KEYS


def test_basebet_pct_in_config_keys():
    assert "basebet_pct" in wb._CONFIG_KEYS


def test_milestone_bets_in_config_keys():
    assert "milestone_bets" in wb._CONFIG_KEYS


def test_all_four_milestones_in_config_keys():
    for k in ("milestone_bets", "milestone_wins",
              "milestone_losses", "milestone_profit"):
        assert k in wb._CONFIG_KEYS, f"{k!r} missing from _CONFIG_KEYS"


# ── Round-trip: recurring=on stays on ────────────────────
def test_recurring_persists_on(monkeypatch):
    _round_trip(monkeypatch, {"recurring": "on"})
    assert wb.state.recurring is True


def test_recurring_persists_off(monkeypatch):
    wb.state.recurring = True   # start dirty
    _round_trip(monkeypatch, {"recurring": "off"})
    assert wb.state.recurring is False


# ── Round-trip: basebet_pct + milestones ──────────────────
def test_basebet_pct_persists(monkeypatch):
    _round_trip(monkeypatch, {"basebet_pct": "0.001"})
    assert abs(wb.state.basebet_pct - 0.001) < 1e-9


def test_milestone_bets_persists(monkeypatch):
    _round_trip(monkeypatch, {"milestone_bets": "250"})
    assert wb.state.milestone_bets == 250


def test_milestone_profit_persists(monkeypatch):
    _round_trip(monkeypatch, {"milestone_profit": "0.005"})
    assert abs(wb.state.milestone_profit - 0.005) < 1e-9


# ── Streak-tuple round-trip through the "N:X" grammar ────
def test_streak_delay_loss_persists_as_string(monkeypatch):
    path = _fresh_config_path(monkeypatch)
    wb._apply_mutation({"streak_delay_loss": "3:0.5"})
    wb.save_config()
    with open(path) as f:
        raw = json.load(f)
    assert raw["streak_delay_loss"] == "3:0.5"
    # Now zero out + reload → tuple restored.
    wb.state.streak_delay_loss = (0, 0.0)
    wb.load_config()
    assert wb.state.streak_delay_loss == (3, 0.5)


def test_streak_delay_win_disabled_persists_as_null(monkeypatch):
    path = _fresh_config_path(monkeypatch)
    wb.state.streak_delay_win = (0, 0.0)   # disabled
    wb.save_config()
    with open(path) as f:
        raw = json.load(f)
    assert raw["streak_delay_win"] is None


def test_streakbet_loss_persists(monkeypatch):
    _fresh_config_path(monkeypatch)
    wb._apply_mutation({"streakbet_loss": "5:0.8"})
    wb.save_config()
    wb.state.streakbet_loss = (0, 0.0)
    wb.load_config()
    assert wb.state.streakbet_loss == (5, 0.8)


def test_streak_delay_bets_persists(monkeypatch):
    _fresh_config_path(monkeypatch)
    wb._apply_mutation({"streak_delay_bets": "10:1.5"})
    wb.save_config()
    wb.state.streak_delay_bets = (0, 0.0)
    wb.load_config()
    assert wb.state.streak_delay_bets == (10, 1.5)


# ── Backward compat: old config file without new keys still loads ──
def test_load_config_ignores_missing_new_keys(monkeypatch):
    path = _fresh_config_path(monkeypatch)
    # Write a minimal legacy config file — no `recurring`, no streak_*.
    with open(path, "w") as f:
        json.dump({
            "api_key": "abc",
            "currency": "usdt",
            "game": "dice",
            "rule": "over",
            "multiplier": 2.0,
            "bet_value": 50.49,
            "base_bet": 0.0001,
            "strategy": "Flat Bet",
            "strategy_key": "1",
            "win_mult": 1.0,
            "loss_mult": 2.0,
        }, f)
    # Force known defaults BEFORE load so we detect any silent overwrite.
    wb.state.recurring = False
    wb.state.milestone_bets = 100
    wb.state.streak_delay_loss = (0, 0.0)
    assert wb.load_config() is True
    # Missing keys stay at their pre-load defaults — no KeyError, no crash.
    assert wb.state.recurring is False
    assert wb.state.milestone_bets == 100
    assert wb.state.streak_delay_loss == (0, 0.0)

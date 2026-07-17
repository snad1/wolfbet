"""Phase 4a tests — preset save/load round-trip for Phase 2 config fields.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase4a_presets.py -q
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


@contextmanager
def _tmp_preset_path():
    """Swap wolfbet.PRESET_PATH for a temp file for the duration of the block."""
    original = wb.PRESET_PATH
    fd, path = tempfile.mkstemp(prefix="wb_test_presets_", suffix=".json")
    os.close(fd)
    os.remove(path)  # save_preset expects the file to NOT exist yet on first save
    wb.PRESET_PATH = path
    try:
        yield path
    finally:
        wb.PRESET_PATH = original
        if os.path.exists(path):
            os.remove(path)


def _reset_state_for_test():
    """Reset the fields Phase 2/4a helpers touch."""
    s = wb.state
    s.currency = "usdt"
    s.game = "dice"
    s.rule = "over"
    s.multiplier = 2.01
    s.bet_value = 49.5
    s.base_bet = 0.0001
    s.strategy = "Martingale"
    s.strategy_key = "2"
    s.win_mult = 1.0
    s.loss_mult = 2.0
    s.bet_delay = 0.0
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.custom_rules_text = ""
    s.delay_martin_threshold = 3
    s.profit_increment = None
    s.profit_threshold = None
    # Phase 2 fields (defaults):
    s.recurring = False
    s.basebet_pct = 0.0
    s.streak_delay_loss = (0, 0.0)
    s.streak_delay_win = (0, 0.0)
    s.streak_delay_bets = (0, 0.0)
    s.streakbet_loss = (0, 1.0)
    s.milestone_bets = 100
    s.milestone_wins = 0
    s.milestone_losses = 0
    s.milestone_profit = 0.0


# ── Round-trip covers every Phase 2 field ──────────────────────
def test_round_trip_all_phase2_fields():
    _reset_state_for_test()
    wb.state.recurring = True
    wb.state.basebet_pct = 0.001
    wb.state.streak_delay_loss = (5, 1.5)
    wb.state.streak_delay_win = (3, 0.5)
    wb.state.streak_delay_bets = (100, 0.2)
    wb.state.streakbet_loss = (10, 0.5)
    wb.state.milestone_bets = 250
    wb.state.milestone_wins = 20
    wb.state.milestone_losses = 15
    wb.state.milestone_profit = 0.01
    with _tmp_preset_path():
        wb.save_preset("phase4a_round_trip")
        _reset_state_for_test()  # scrub state before load
        assert wb.load_preset("phase4a_round_trip") is True
        assert wb.state.recurring is True
        assert wb.state.basebet_pct == 0.001
        assert wb.state.streak_delay_loss == (5, 1.5)
        assert wb.state.streak_delay_win == (3, 0.5)
        assert wb.state.streak_delay_bets == (100, 0.2)
        assert wb.state.streakbet_loss == (10, 0.5)
        assert wb.state.milestone_bets == 250
        assert wb.state.milestone_wins == 20
        assert wb.state.milestone_losses == 15
        assert wb.state.milestone_profit == 0.01


# ── Empty streak encodes to null; loads to (0, 0.0) ────────────
def test_empty_streak_encodes_as_null():
    _reset_state_for_test()
    with _tmp_preset_path() as path:
        wb.save_preset("empty_streak")
        with open(path) as f:
            payload = json.load(f)
        assert payload["empty_streak"]["streak_delay_loss"] is None
        assert payload["empty_streak"]["streakbet_loss"] is None


def test_empty_streak_loads_as_zero_tuple():
    _reset_state_for_test()
    with _tmp_preset_path():
        wb.save_preset("empty_streak")
        wb.state.streak_delay_loss = (99, 99.0)  # mutate before load
        assert wb.load_preset("empty_streak") is True
        assert wb.state.streak_delay_loss == (0, 0.0)


# ── Populated streak encodes as "N:X" string ───────────────────
def test_populated_streak_encodes_as_string():
    _reset_state_for_test()
    wb.state.streak_delay_loss = (5, 1.5)
    wb.state.streakbet_loss = (10, 0.5)
    with _tmp_preset_path() as path:
        wb.save_preset("populated_streak")
        with open(path) as f:
            payload = json.load(f)
        assert payload["populated_streak"]["streak_delay_loss"] == "5:1.5"
        assert payload["populated_streak"]["streakbet_loss"] == "10:0.5"


# ── Backward-compat: preset without new keys ───────────────────
def test_legacy_preset_loads_without_error():
    """A v2.17.0-era preset lacks all Phase 4a keys. Defaults must hold; no KeyError."""
    _reset_state_for_test()
    with _tmp_preset_path() as path:
        # Write a legacy-style preset (only the old keys)
        legacy = {
            "legacy": {
                "currency": "btc",
                "game": "dice",
                "rule": "under",
                "multiplier": 3.0,
                "bet_value": 33.0,
                "base_bet": 0.001,
                "strategy": "Flat",
                "strategy_key": "1",
                "win_mult": 1.0,
                "loss_mult": 1.0,
                "bet_delay": 0.5,
                "max_profit": None,
                "max_loss": None,
                "max_bets": None,
                "max_wins": None,
                "stop_on_balance": None,
                "custom_rules_text": "",
                "delay_martin_threshold": 3,
                "profit_increment": None,
                "profit_threshold": None,
            }
        }
        with open(path, "w") as f:
            json.dump(legacy, f)
        # Mutate state to non-defaults, then load
        wb.state.recurring = True
        wb.state.basebet_pct = 0.5
        wb.state.streak_delay_loss = (5, 1.5)
        assert wb.load_preset("legacy") is True
        # Old keys should have loaded:
        assert wb.state.currency == "btc"
        assert wb.state.rule == "under"
        assert wb.state.bet_delay == 0.5
        # New keys should NOT have been touched (no key in the preset):
        assert wb.state.recurring is True
        assert wb.state.basebet_pct == 0.5
        assert wb.state.streak_delay_loss == (5, 1.5)


# ── Malformed streak on load falls back to (0, 0.0) ────────────
def test_malformed_streak_value_falls_back_to_zero():
    """Hand-edited preset with a malformed streak field must not crash."""
    _reset_state_for_test()
    with _tmp_preset_path() as path:
        malformed = {
            "malformed": {
                "streak_delay_loss": "not-a-tuple",  # invalid
                "streak_delay_win": [5, 1.5],         # list instead of "N:X" — _parse_streak_delay treats as truthy but split fails
                "streakbet_loss": "5",                # missing X — split has 1 part
                "streak_delay_bets": None,
            }
        }
        with open(path, "w") as f:
            json.dump(malformed, f)
        # Mutate state
        wb.state.streak_delay_loss = (99, 99.0)
        wb.state.streak_delay_win = (99, 99.0)
        wb.state.streakbet_loss = (99, 99.0)
        wb.state.streak_delay_bets = (99, 99.0)
        assert wb.load_preset("malformed") is True
        assert wb.state.streak_delay_loss == (0, 0.0)
        assert wb.state.streak_delay_win == (0, 0.0)
        assert wb.state.streakbet_loss == (0, 0.0)
        assert wb.state.streak_delay_bets == (0, 0.0)


# ── save_preset stores JSON-native types for plain fields ──────
def test_recurring_bool_serialised_as_json_bool():
    _reset_state_for_test()
    wb.state.recurring = True
    with _tmp_preset_path() as path:
        wb.save_preset("bool_check")
        with open(path) as f:
            payload = json.load(f)
        assert payload["bool_check"]["recurring"] is True


def test_basebet_pct_zero_serialised_as_zero_not_none():
    """basebet_pct=0 is legitimate (disabled). Serialise as 0.0, not None."""
    _reset_state_for_test()
    wb.state.basebet_pct = 0.0
    with _tmp_preset_path() as path:
        wb.save_preset("zero_pct")
        with open(path) as f:
            payload = json.load(f)
        assert payload["zero_pct"]["basebet_pct"] == 0.0


# ── Preset name isolation ──────────────────────────────────────
def test_multiple_presets_do_not_leak():
    _reset_state_for_test()
    wb.state.recurring = True
    with _tmp_preset_path():
        wb.save_preset("preset_a")
        wb.state.recurring = False
        wb.save_preset("preset_b")
        # Load preset_a should restore recurring=True
        wb.state.recurring = False
        assert wb.load_preset("preset_a") is True
        assert wb.state.recurring is True
        # Load preset_b should restore recurring=False
        assert wb.load_preset("preset_b") is True
        assert wb.state.recurring is False

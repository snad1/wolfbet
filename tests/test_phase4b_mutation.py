"""Phase 4b tests for _apply_mutation — CLI runtime tweak grammar.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase4b_mutation.py -q
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import pytest  # noqa: E402
import wolfbet as wb  # noqa: E402


def _reset_state_for_test():
    """Reset the fields _apply_mutation touches."""
    s = wb.state
    s.game = "dice"
    s.bet_delay = 0.0
    s.base_bet = 0.0001
    s.current_bet = 0.0001
    s.current_balance = 10.0
    s.multiplier = 2.01
    s.loss_mult = 2.0
    s.win_mult = 1.0
    s.strategy = "Martingale"
    s.strategy_key = "2"
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.milestone_bets = 100
    s.milestone_wins = 0
    s.milestone_losses = 0
    s.milestone_profit = 0.0
    s.profit_increment = None
    s.profit_threshold = None
    s.next_profit_milestone = 0.0
    s.recurring = False
    s.basebet_pct = 0.0
    s.streak_delay_loss = (0, 0.0)
    s.streak_delay_win = (0, 0.0)
    s.streak_delay_bets = (0, 0.0)
    s.streakbet_loss = (0, 1.0)
    s._auto_uuid = None


# ── Instant params ─────────────────────────────────────────
def test_bet_delay_mutation():
    _reset_state_for_test()
    msgs = wb._apply_mutation({"bet_delay": "0.5"})
    assert wb.state.bet_delay == 0.5
    assert any("Delay" in m for m in msgs)


def test_basebet_pct_mutation():
    _reset_state_for_test()
    wb.state.current_balance = 10.0
    msgs = wb._apply_mutation({"basebet_pct": "0.001"})
    assert wb.state.basebet_pct == 0.001
    # basebet_pct * balance = 0.01
    assert abs(wb.state.base_bet - 0.01) < 1e-9
    assert any("Basebet" in m for m in msgs)


def test_basebet_pct_off():
    _reset_state_for_test()
    wb.state.basebet_pct = 0.5
    msgs = wb._apply_mutation({"basebet_pct": "off"})
    assert wb.state.basebet_pct == 0.0
    assert any("off" in m.lower() for m in msgs)


# ── Streak delays ──────────────────────────────────────────
def test_streak_delay_loss_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"streak_delay_loss": "3:0.5"})
    assert wb.state.streak_delay_loss == (3, 0.5)


def test_streak_delay_loss_off():
    _reset_state_for_test()
    wb.state.streak_delay_loss = (5, 1.0)
    wb._apply_mutation({"streak_delay_loss": "off"})
    assert wb.state.streak_delay_loss == (0, 0.0)


def test_streakbet_loss_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"streakbet_loss": "10:0.5"})
    assert wb.state.streakbet_loss == (10, 0.5)


# ── Stop conditions ────────────────────────────────────────
def test_max_profit_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"max_profit": "0.001"})
    assert wb.state.max_profit == 0.001


def test_max_profit_off():
    _reset_state_for_test()
    wb.state.max_profit = 0.005
    wb._apply_mutation({"max_profit": "off"})
    assert wb.state.max_profit is None


def test_max_wins_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"max_wins": "5000"})
    assert wb.state.max_wins == 5000


def test_max_bets_off_disables():
    _reset_state_for_test()
    wb.state.max_bets = 100
    wb._apply_mutation({"max_bets": "off"})
    assert wb.state.max_bets is None


def test_stop_on_balance_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"stop_on_balance": "1.0"})
    assert wb.state.stop_on_balance == 1.0


# ── Milestones ─────────────────────────────────────────────
def test_milestone_bets_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"milestone_bets": "50"})
    assert wb.state.milestone_bets == 50


def test_milestone_profit_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"milestone_profit": "0.001"})
    assert wb.state.milestone_profit == 0.001


def test_milestone_wins_off():
    _reset_state_for_test()
    wb.state.milestone_wins = 100
    wb._apply_mutation({"milestone_wins": "off"})
    assert wb.state.milestone_wins == 0


# ── Recurring ──────────────────────────────────────────────
@pytest.mark.parametrize("v,expected", [
    ("on", True), ("true", True), ("yes", True), ("y", True), ("1", True),
    ("off", False), ("false", False), ("no", False), ("n", False), ("0", False),
])
def test_recurring_toggle(v, expected):
    _reset_state_for_test()
    wb._apply_mutation({"recurring": v})
    assert wb.state.recurring is expected


# ── Profit increment / threshold ───────────────────────────
def test_profit_increment_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"profit_increment": "0.00000001"})
    assert wb.state.profit_increment == 0.00000001


def test_profit_threshold_sets_next_milestone():
    _reset_state_for_test()
    wb._apply_mutation({"profit_threshold": "0.001"})
    assert wb.state.profit_threshold == 0.001
    assert wb.state.next_profit_milestone == 0.001


# ── Strategy touches — auto vs manual ──────────────────────
def test_multiplier_on_manual_game_no_uuid_clear():
    _reset_state_for_test()
    wb.state.game = "dice"          # manual game
    wb.state._auto_uuid = None      # nothing to clear anyway
    wb._apply_mutation({"multiplier": "3.0"})
    assert wb.state.multiplier == 3.0
    assert wb.state._auto_uuid is None


def test_multiplier_on_auto_game_clears_uuid():
    _reset_state_for_test()
    wb.state.game = "dice-auto"
    wb.state._auto_uuid = "abc-123"
    msgs = wb._apply_mutation({"multiplier": "3.0"})
    assert wb.state.multiplier == 3.0
    assert wb.state._auto_uuid is None
    assert any("UUID cleared" in m for m in msgs)


def test_base_bet_updates_current_bet():
    _reset_state_for_test()
    wb._apply_mutation({"base_bet": "0.005"})
    assert wb.state.base_bet == 0.005
    assert wb.state.current_bet == 0.005


def test_loss_mult_win_mult_mutation():
    _reset_state_for_test()
    wb._apply_mutation({"loss_mult": "2.5", "win_mult": "1.5"})
    assert wb.state.loss_mult == 2.5
    assert wb.state.win_mult == 1.5


# ── Type validation ────────────────────────────────────────
def test_bet_delay_invalid_raises_valueerror():
    _reset_state_for_test()
    with pytest.raises(ValueError):
        wb._apply_mutation({"bet_delay": "notanumber"})


def test_max_bets_invalid_raises_valueerror():
    _reset_state_for_test()
    with pytest.raises(ValueError):
        wb._apply_mutation({"max_bets": "notaninteger"})


# ── Aliases resolve to canonical names ─────────────────────
def test_alias_delay_maps_to_bet_delay():
    assert wb._TWEAK_ALIAS["delay"] == "bet_delay"
    assert wb._TWEAK_ALIAS["sdloss"] == "streak_delay_loss"
    assert wb._TWEAK_ALIAS["maxprofit"] == "max_profit"
    assert wb._TWEAK_ALIAS["msbets"] == "milestone_bets"


def test_tweak_params_covers_all_canonical_fields():
    """All alias targets must be in _TWEAK_PARAMS."""
    for alias, canonical in wb._TWEAK_ALIAS.items():
        assert canonical in wb._TWEAK_PARAMS, f"Alias '{alias}' → '{canonical}' not in _TWEAK_PARAMS"

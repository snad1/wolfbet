"""Phase 2 pure-function tests for wolfbet.py helpers.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase2_helpers.py -q

These are pure-function tests that mutate the shared `wolfbet.state` singleton
per-test. Each test resets the fields it touches so tests can run in any order.
"""

import os
import sys

# Make wolfbet.py importable when running from the wolfbet/ dir or repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

# Force testing env before importing wolfbet (avoids production log-path side effects).
os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _reset_state_for_test():
    """Reset the fields Phase 2 helpers touch. Called at the top of each test."""
    s = wb.state
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.wagered = 0.0
    s.current_streak = 0
    s.max_win_streak = 0
    s.max_loss_streak = 0
    s.current_bet = s.base_bet = 0.0001
    s.current_balance = 10.0
    s.streak_delay_loss = (0, 0.0)
    s.streak_delay_win = (0, 0.0)
    s.streak_delay_bets = (0, 0.0)
    s.streakbet_loss = (0, 1.0)
    s.basebet_pct = 0.0
    s.milestone_bets = 100
    s.milestone_wins = 0
    s.milestone_losses = 0
    s.milestone_profit = 0.0
    s._last_profit_milestone = 0.0
    s.recurring = False


# ── _parse_streak_delay ─────────────────────────────────────
def test_parse_streak_delay_none_returns_zero():
    assert wb._parse_streak_delay(None) == (0, 0.0)


def test_parse_streak_delay_empty_returns_zero():
    assert wb._parse_streak_delay("") == (0, 0.0)


def test_parse_streak_delay_valid_format():
    assert wb._parse_streak_delay("5:1.0") == (5, 1.0)


def test_parse_streak_delay_alt_format():
    assert wb._parse_streak_delay("3:0.5") == (3, 0.5)


def test_parse_streak_delay_malformed_returns_zero():
    assert wb._parse_streak_delay("not-a-number") == (0, 0.0)
    assert wb._parse_streak_delay("5") == (0, 0.0)  # missing seconds
    assert wb._parse_streak_delay("abc:def") == (0, 0.0)


# ── _get_streak_delay ───────────────────────────────────────
def test_get_streak_delay_loss_streak_triggers():
    _reset_state_for_test()
    wb.state.streak_delay_loss = (3, 0.5)
    wb.state.current_streak = -3
    assert wb._get_streak_delay() == 0.5


def test_get_streak_delay_win_streak_triggers():
    _reset_state_for_test()
    wb.state.streak_delay_win = (5, 1.0)
    wb.state.current_streak = 5
    assert wb._get_streak_delay() == 1.0


def test_get_streak_delay_every_n_bets_triggers():
    _reset_state_for_test()
    wb.state.streak_delay_bets = (10, 0.2)
    wb.state.total_bets = 20
    assert wb._get_streak_delay() == 0.2


def test_get_streak_delay_no_streak_returns_zero():
    _reset_state_for_test()
    wb.state.streak_delay_loss = (5, 1.0)
    wb.state.current_streak = -2  # not yet at threshold
    assert wb._get_streak_delay() == 0.0


def test_get_streak_delay_max_of_multiple_applicable():
    _reset_state_for_test()
    wb.state.streak_delay_loss = (3, 0.5)
    wb.state.streak_delay_bets = (3, 1.5)
    wb.state.current_streak = -3
    wb.state.total_bets = 3
    # Both apply; expect max
    assert wb._get_streak_delay() == 1.5


# ── _get_streakbet_loss_factor ──────────────────────────────
def test_streakbet_factor_disabled_returns_one():
    _reset_state_for_test()
    wb.state.streakbet_loss = (0, 0.5)  # count=0 disables
    wb.state.current_streak = -5
    assert wb._get_streakbet_loss_factor() == 1.0


def test_streakbet_factor_on_loss_streak_returns_multiplier():
    _reset_state_for_test()
    wb.state.streakbet_loss = (3, 0.5)
    wb.state.current_streak = -3
    assert wb._get_streakbet_loss_factor() == 0.5


def test_streakbet_factor_win_streak_returns_one():
    _reset_state_for_test()
    wb.state.streakbet_loss = (3, 0.5)
    wb.state.current_streak = 3  # win streak — no reduction
    assert wb._get_streakbet_loss_factor() == 1.0


# ── _apply_basebet_pct ──────────────────────────────────────
def test_basebet_pct_disabled_returns_false():
    _reset_state_for_test()
    wb.state.basebet_pct = 0.0
    assert wb._apply_basebet_pct() is False


def test_basebet_pct_updates_base_bet():
    _reset_state_for_test()
    wb.state.basebet_pct = 0.001
    wb.state.current_balance = 10.0
    wb.state.base_bet = 0.0001
    assert wb._apply_basebet_pct() is True
    assert wb.state.base_bet == 0.01
    assert wb.state.current_bet == 0.01


def test_basebet_pct_skips_when_no_change():
    _reset_state_for_test()
    wb.state.basebet_pct = 0.001
    wb.state.current_balance = 10.0
    wb.state.base_bet = 0.01  # already correct
    assert wb._apply_basebet_pct() is False


# ── _check_milestone ────────────────────────────────────────
def test_check_milestone_bets_matches():
    _reset_state_for_test()
    wb.state.milestone_bets = 10
    wb.state.total_bets = 10
    assert wb._check_milestone("win") == "10 bets"


def test_check_milestone_wins_matches():
    _reset_state_for_test()
    wb.state.milestone_bets = 0
    wb.state.milestone_wins = 5
    wb.state.wins = 5
    wb.state.total_bets = 7  # not a bets milestone
    assert wb._check_milestone("win") == "5 wins"


def test_check_milestone_losses_matches():
    _reset_state_for_test()
    wb.state.milestone_bets = 0
    wb.state.milestone_losses = 3
    wb.state.losses = 3
    wb.state.total_bets = 4
    assert wb._check_milestone("loss") == "3 losses"


def test_check_milestone_profit_crosses_threshold():
    _reset_state_for_test()
    wb.state.milestone_bets = 0
    wb.state.milestone_profit = 0.001
    wb.state.profit = 0.0025  # crosses second multiple of 0.001
    wb.state.total_bets = 5
    result = wb._check_milestone("win")
    assert result is not None
    assert "profit" in result


def test_check_milestone_none_when_no_match():
    _reset_state_for_test()
    wb.state.milestone_bets = 100  # not yet at 100
    wb.state.total_bets = 7
    assert wb._check_milestone("win") is None


# ── _should_recurring_restart ───────────────────────────────
def test_recurring_disabled_returns_false():
    _reset_state_for_test()
    wb.state.recurring = False
    assert wb._should_recurring_restart("Profit target reached: +0.001") is False


def test_recurring_max_loss_never_restarts():
    _reset_state_for_test()
    wb.state.recurring = True
    wb.state.profit = -0.005
    assert wb._should_recurring_restart("Max loss hit: -0.005") is False


def test_recurring_insufficient_balance_never_restarts():
    _reset_state_for_test()
    wb.state.recurring = True
    wb.state.profit = 0.0
    assert wb._should_recurring_restart("STOPPED: Insufficient balance") is False


def test_recurring_profit_target_with_positive_profit_restarts():
    _reset_state_for_test()
    wb.state.recurring = True
    wb.state.profit = 0.001
    assert wb._should_recurring_restart("Profit target reached: +0.001") is True


def test_recurring_max_wins_restarts():
    _reset_state_for_test()
    wb.state.recurring = True
    wb.state.profit = 0.5
    assert wb._should_recurring_restart("Max wins reached: 100") is True


def test_recurring_max_bets_restarts():
    _reset_state_for_test()
    wb.state.recurring = True
    wb.state.profit = 0.001
    assert wb._should_recurring_restart("Max bets reached: 500") is True


def test_recurring_balance_floor_never_restarts():
    _reset_state_for_test()
    wb.state.recurring = True
    wb.state.profit = 0.0
    assert wb._should_recurring_restart("Balance floor hit: 5.00000000") is False


def test_recurring_negative_profit_never_restarts():
    _reset_state_for_test()
    wb.state.recurring = True
    wb.state.profit = -0.001
    # Even a "safe" reason won't restart if we're at a loss.
    assert wb._should_recurring_restart("Max bets reached: 500") is False

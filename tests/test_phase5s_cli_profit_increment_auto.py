"""Phase 5s — profit_increment mid-session auto-bet UUID restart (v2.29.0).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5s_cli_profit_increment_auto.py -q
"""

import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _envelope(profit=0.0005, amount=0.0001):
    return {
        "result": {
            "bet": {"state": "win", "result_value": 50, "amount": amount, "profit": profit},
            "userBalance": {"amount": 1.0},
        },
        "payload": {"amount": amount},
    }


def _reset(game="dice"):
    wb.state.game = game
    wb.state.base_bet = 0.0001
    wb.state.profit = 0.0
    wb.state.profit_increment = 0.00000001
    wb.state.profit_threshold = 0.0005
    wb.state.next_profit_milestone = 0.0005
    wb.state.session_id = 42
    wb.state.session_start = 1000.0
    wb.state.total_bets = 100
    wb.state.wins = 50
    wb.state.losses = 50
    wb.state.consecutive_errors = 0
    wb.state.backoff_delay = 1.0
    wb.state.consecutive_recovery_fails = 0
    wb.state.wagered = 0.0
    wb.state.current_balance = 1.0
    wb.state.current_streak = 0
    wb.state.max_win_streak = 0
    wb.state.max_loss_streak = 0
    wb.state.highest_bet = 0.0
    wb.state.highest_balance = 1.0
    wb.state.lowest_balance = 1.0
    wb.state.highest_win = 0.0
    wb.state.biggest_loss = 0.0
    wb.state._auto_uuid = "uuid-old"
    wb.state._auto_restart_pending = False


def test_source_deferral_wired_into_loop():
    src = inspect.getsource(wb)
    assert "_auto_restart_pending = True" in inspect.getsource(wb._check_profit_increment)
    assert "_maybe_apply_pending_auto_restart()" in inspect.getsource(wb.betting_loop)
    assert "_auto_restart_pending" in src


def test_source_guards_on_is_auto_game():
    # v2.37.0 — both outcome paths share _check_profit_increment.
    assert "is_auto_game(state.game)" in inspect.getsource(wb._check_profit_increment)
    assert "_check_profit_increment()" in inspect.getsource(wb.betting_loop)
    assert "_check_profit_increment()" in inspect.getsource(wb._apply_outcome_locked)


def test_non_auto_game_leaves_uuid_intact():
    _reset(game="dice")
    wb._apply_outcome_locked(_envelope(profit=0.0005))
    assert wb.state._auto_uuid == "uuid-old"


def test_limbo_auto_defers_uuid_clear_on_increment():
    _reset(game="limbo-auto")
    wb._apply_outcome_locked(_envelope(profit=0.0005))
    assert wb.state._auto_uuid == "uuid-old"
    assert wb.state._auto_restart_pending is True


def test_dice_auto_defers_uuid_clear_on_increment():
    _reset(game="dice-auto")
    wb._apply_outcome_locked(_envelope(profit=0.0005))
    assert wb.state._auto_uuid == "uuid-old"
    assert wb.state._auto_restart_pending is True


def test_session_id_unchanged_after_increment():
    _reset(game="limbo-auto")
    before = wb.state.session_id
    wb._apply_outcome_locked(_envelope(profit=0.0005))
    assert wb.state.session_id == before


def test_base_bet_bumps_by_increment():
    _reset(game="limbo-auto")
    before = wb.state.base_bet
    wb._apply_outcome_locked(_envelope(profit=0.0005))
    assert abs(wb.state.base_bet - (before + 0.00000001)) < 1e-15

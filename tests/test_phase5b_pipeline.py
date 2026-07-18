"""Phase 5b tests — pipelined betting dispatcher + guardrails + apply-outcome.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5b_pipeline.py -q
"""

import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _reset_state():
    s = wb.state
    s.strategy_key = "1"                # Flat by default
    s.strategy = "Flat Bet"
    s.game = "dice"
    s.rule = "over"
    s.multiplier = 2.01
    s.base_bet = 0.0001
    s.current_bet = 0.0001
    s.win_mult = 1.0
    s.loss_mult = 2.0
    s.current_balance = 1.0
    s.start_balance = 1.0
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.recurring = False
    s.basebet_pct = 0.0
    s.custom_rules = []
    s.pipeline_depth = 1
    s.pipeline_effective = 1
    s.pipeline_in_flight = 0
    s.pipeline_dispatched = 0
    s.pipeline_reason = ""
    # counters reset
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.wagered = 0.0
    s.session_start = time.time()
    s.current_streak = 0
    s.max_win_streak = 0
    s.max_loss_streak = 0
    s.consecutive_errors = 0
    s.backoff_delay = 1.0
    s._insufficient_balance = False
    s._auto_uuid = None


# ── _effective_pipeline_depth — guardrail matrix ──────────
def test_depth_1_stays_1():
    _reset_state()
    wb.state.pipeline_depth = 1
    assert wb._effective_pipeline_depth() == 1


def test_flat_dice_honors_requested_depth():
    _reset_state()
    wb.state.pipeline_depth = 4
    assert wb._effective_pipeline_depth() == 4


def test_flat_dice_clamps_to_max_8():
    _reset_state()
    wb.state.pipeline_depth = 100
    assert wb._effective_pipeline_depth() == 8


def test_martingale_forced_to_1():
    _reset_state()
    wb.state.pipeline_depth = 4
    wb.state.strategy_key = "2"
    wb.state.strategy = "Martingale"
    assert wb._effective_pipeline_depth() == 1
    assert "Martingale" in wb.state.pipeline_reason


def test_dalembert_forced_to_1():
    _reset_state()
    wb.state.pipeline_depth = 4
    wb.state.strategy_key = "4"
    wb.state.strategy = "D'Alembert"
    assert wb._effective_pipeline_depth() == 1


def test_rulebased_forced_to_1():
    _reset_state()
    wb.state.pipeline_depth = 4
    wb.state.strategy_key = "8"
    wb.state.strategy = "Rule-Based"
    assert wb._effective_pipeline_depth() == 1


def test_dice_auto_forced_to_1():
    _reset_state()
    wb.state.pipeline_depth = 4
    wb.state.game = "dice-auto"
    assert wb._effective_pipeline_depth() == 1
    assert "auto-bet" in wb.state.pipeline_reason


def test_limbo_auto_forced_to_1():
    _reset_state()
    wb.state.pipeline_depth = 4
    wb.state.game = "limbo-auto"
    assert wb._effective_pipeline_depth() == 1


# ── _prepare_next_bet_flat — payload snapshot ────────────
def test_prepare_next_bet_snapshots_amount():
    _reset_state()
    wb.state.current_bet = 0.0007
    payload = wb._prepare_next_bet_flat()
    assert payload["amount"] == 0.0007
    assert payload["bet_no"] == 1


def test_prepare_next_bet_increments_dispatched():
    _reset_state()
    wb._prepare_next_bet_flat()
    wb._prepare_next_bet_flat()
    p3 = wb._prepare_next_bet_flat()
    assert p3["bet_no"] == 3
    assert wb.state.pipeline_dispatched == 3


# ── _apply_outcome_locked — counter integrity ─────────────
def _fake_win_envelope(amount=0.0001, profit=0.0001, balance=1.0001):
    return {
        "payload": {"amount": amount, "bet_no": 1},
        "result": {
            "bet": {"state": "win", "result_value": 50.0, "amount": amount, "profit": profit},
            "userBalance": {"amount": balance},
        },
    }


def _fake_loss_envelope(amount=0.0001, profit=-0.0001, balance=0.9999):
    return {
        "payload": {"amount": amount, "bet_no": 1},
        "result": {
            "bet": {"state": "loss", "result_value": 20.0, "amount": amount, "profit": profit},
            "userBalance": {"amount": balance},
        },
    }


def test_apply_outcome_win_updates_counters():
    _reset_state()
    wb._apply_outcome_locked(_fake_win_envelope())
    assert wb.state.total_bets == 1
    assert wb.state.wins == 1
    assert wb.state.losses == 0
    assert wb.state.profit == 0.0001


def test_apply_outcome_loss_updates_streak():
    _reset_state()
    wb._apply_outcome_locked(_fake_loss_envelope())
    wb._apply_outcome_locked(_fake_loss_envelope())
    assert wb.state.losses == 2
    assert wb.state.current_streak == -2
    assert wb.state.max_loss_streak == 2


def test_apply_outcome_none_result_bumps_consecutive_errors():
    _reset_state()
    wb._apply_outcome_locked({"payload": {"amount": 0.0001, "bet_no": 1}, "result": None})
    assert wb.state.consecutive_errors == 1
    assert wb.state.total_bets == 0
    assert wb.state.backoff_delay == 2.0   # doubled from 1.0


# ── Concurrent counter integrity — many workers hit apply ──
def test_concurrent_apply_outcome_no_counter_drift():
    _reset_state()
    N = 500
    threads = []

    def worker():
        wb._apply_outcome_locked(_fake_win_envelope())

    for _ in range(N):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    assert wb.state.total_bets == N
    assert wb.state.wins == N
    # wagered = N * 0.0001, within float epsilon
    assert abs(wb.state.wagered - N * 0.0001) < 1e-9


# ── Tweak grammar — pipeline alias ────────────────────────
def test_tweak_alias_pipeline_maps_to_pipeline_depth():
    assert wb._TWEAK_ALIAS.get("pipeline") == "pipeline_depth"
    assert "pipeline_depth" in wb._TWEAK_PARAMS


def test_apply_mutation_sets_pipeline_depth():
    _reset_state()
    msgs = wb._apply_mutation({"pipeline_depth": 4})
    assert wb.state.pipeline_depth == 4
    assert any("Pipeline depth" in m for m in msgs)


def test_apply_mutation_pipeline_clamps_to_max():
    _reset_state()
    wb._apply_mutation({"pipeline_depth": 999})
    assert wb.state.pipeline_depth == 8


def test_apply_mutation_pipeline_clamps_to_min():
    _reset_state()
    wb._apply_mutation({"pipeline_depth": -5})
    assert wb.state.pipeline_depth == 1


def test_apply_mutation_pipeline_off_resets_to_1():
    _reset_state()
    wb.state.pipeline_depth = 4
    wb._apply_mutation({"pipeline_depth": "off"})
    assert wb.state.pipeline_depth == 1


def test_apply_mutation_pipeline_bad_int_raises():
    _reset_state()
    try:
        wb._apply_mutation({"pipeline_depth": "abc"})
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-integer pipeline_depth")


# ── Config + preset round-trip ────────────────────────────
def test_pipeline_depth_in_config_keys():
    assert "pipeline_depth" in wb._CONFIG_KEYS


def test_pipeline_depth_in_preset_keys():
    assert "pipeline_depth" in wb._PRESET_KEYS


# ── Risky-config warning ──────────────────────────────────
def test_risky_config_warns_on_pipeline_with_martingale():
    _reset_state()
    wb.state.pipeline_depth = 4
    wb.state.strategy_key = "2"
    wb.state.strategy = "Martingale"
    wb.state.max_loss = 0.005          # silence other Martingale warnings
    warnings = wb._check_risky_config()
    assert any("pipeline_depth" in w and "Flat" in w for w in warnings)


def test_risky_config_warns_on_pipeline_with_auto_game():
    _reset_state()
    wb.state.pipeline_depth = 3
    wb.state.game = "dice-auto"
    warnings = wb._check_risky_config()
    assert any("pipeline_depth" in w and "auto-bet" in w for w in warnings)


def test_risky_config_no_pipeline_warning_when_flat_dice():
    _reset_state()
    wb.state.pipeline_depth = 4
    warnings = wb._check_risky_config()
    assert not any("pipeline_depth" in w for w in warnings)

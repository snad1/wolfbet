"""v2.22.1 polish tests — _check_risky_config heuristics.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase7_polish.py -q
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
    s.game = "dice"                # not dice-auto (shared state across tests)
    s.strategy_key = "1"  # Flat by default
    s.multiplier = 2.0
    s.loss_mult = 2.0
    s.win_mult = 1.0
    s.base_bet = 0.0001
    s.current_balance = 1.0
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.recurring = False
    s.basebet_pct = 0.0
    s.custom_rules = []
    s.pipeline_depth = 1           # avoid stale pipeline warnings from prior tests


# ── Clean config → no warnings ────────────────────────────
def test_flat_strategy_clean_config_emits_no_warnings():
    _reset_state()
    assert wb._check_risky_config() == []


# ── Aggressive Martingale ────────────────────────────────
def test_martingale_loss_mult_25_warns():
    _reset_state()
    wb.state.strategy_key = "2"
    wb.state.loss_mult = 2.5
    wb.state.max_loss = 0.005  # set so we don't trigger the other warning
    warnings = wb._check_risky_config()
    assert any("Aggressive Martingale" in w for w in warnings)


def test_martingale_loss_mult_20_does_not_warn():
    _reset_state()
    wb.state.strategy_key = "2"
    wb.state.loss_mult = 2.0
    wb.state.max_loss = 0.005
    warnings = wb._check_risky_config()
    assert not any("Aggressive Martingale" in w for w in warnings)


# ── Doubling without max_loss ─────────────────────────────
def test_martingale_no_max_loss_warns():
    _reset_state()
    wb.state.strategy_key = "2"
    wb.state.max_loss = None
    warnings = wb._check_risky_config()
    assert any("max_loss" in w for w in warnings)


def test_delay_martingale_no_max_loss_warns():
    _reset_state()
    wb.state.strategy_key = "7"
    wb.state.max_loss = None
    warnings = wb._check_risky_config()
    assert any("max_loss" in w for w in warnings)


def test_flat_no_max_loss_does_not_trigger_martingale_warning():
    _reset_state()
    wb.state.strategy_key = "1"
    wb.state.max_loss = None
    warnings = wb._check_risky_config()
    assert not any("max_loss" in w for w in warnings)


# ── Anti-Martingale without profit lock ──────────────────
def test_anti_martingale_no_max_profit_warns():
    _reset_state()
    wb.state.strategy_key = "3"
    wb.state.max_profit = None
    warnings = wb._check_risky_config()
    assert any("Anti-Martingale" in w for w in warnings)


def test_anti_martingale_with_max_profit_no_warning():
    _reset_state()
    wb.state.strategy_key = "3"
    wb.state.max_profit = 0.001
    warnings = wb._check_risky_config()
    assert not any("Anti-Martingale" in w for w in warnings)


# ── Base bet vs balance ──────────────────────────────────
def test_base_bet_over_1pct_of_balance_warns():
    _reset_state()
    wb.state.current_balance = 1.0
    wb.state.base_bet = 0.02  # 2% of balance
    warnings = wb._check_risky_config()
    assert any("Base bet" in w and "balance" in w for w in warnings)


def test_base_bet_under_1pct_of_balance_no_warning():
    _reset_state()
    wb.state.current_balance = 1.0
    wb.state.base_bet = 0.0001  # 0.01%
    warnings = wb._check_risky_config()
    assert not any("Base bet" in w and "balance" in w for w in warnings)


# ── High multiplier ──────────────────────────────────────
def test_multiplier_over_10_warns():
    _reset_state()
    wb.state.multiplier = 15.0
    warnings = wb._check_risky_config()
    assert any("Multiplier" in w and "win chance" in w for w in warnings)


def test_multiplier_at_10_or_under_no_warning():
    _reset_state()
    wb.state.multiplier = 10.0
    warnings = wb._check_risky_config()
    assert not any("Multiplier" in w and "win chance" in w for w in warnings)


# ── Recurring runaway ────────────────────────────────────
def test_recurring_with_no_stop_conditions_warns():
    _reset_state()
    wb.state.recurring = True
    wb.state.max_profit = None
    wb.state.max_bets = None
    warnings = wb._check_risky_config()
    assert any("Recurring restart" in w for w in warnings)


def test_recurring_with_max_profit_no_runaway_warning():
    _reset_state()
    wb.state.recurring = True
    wb.state.max_profit = 0.005
    warnings = wb._check_risky_config()
    assert not any("Recurring restart" in w for w in warnings)


def test_recurring_with_max_bets_no_runaway_warning():
    _reset_state()
    wb.state.recurring = True
    wb.state.max_bets = 1000
    warnings = wb._check_risky_config()
    assert not any("Recurring restart" in w for w in warnings)


# ── Custom rules without max_bets ────────────────────────
def test_rule_based_no_max_bets_warns():
    _reset_state()
    wb.state.strategy_key = "8"
    wb.state.max_bets = None
    warnings = wb._check_risky_config()
    assert any("Custom rules" in w for w in warnings)


def test_rule_based_with_max_bets_no_warning():
    _reset_state()
    wb.state.strategy_key = "8"
    wb.state.max_bets = 500
    warnings = wb._check_risky_config()
    assert not any("Custom rules" in w for w in warnings)


# ── Aggressive basebet_pct ───────────────────────────────
def test_basebet_pct_at_5pct_warns():
    _reset_state()
    wb.state.basebet_pct = 0.05
    warnings = wb._check_risky_config()
    assert any("basebet_pct" in w for w in warnings)


def test_basebet_pct_under_5pct_no_warning():
    _reset_state()
    wb.state.basebet_pct = 0.04
    warnings = wb._check_risky_config()
    assert not any("basebet_pct" in w for w in warnings)


# ── Combined config triggers multiple warnings ────────────
def test_multiple_warnings_emitted_together():
    """Aggressive Martingale + no max_loss + high multiplier + basebet_pct → 4 warnings."""
    _reset_state()
    wb.state.strategy_key = "2"    # Martingale
    wb.state.loss_mult = 3.0        # aggressive
    wb.state.max_loss = None        # no floor
    wb.state.multiplier = 15.0      # high multiplier
    wb.state.basebet_pct = 0.10     # aggressive dynamic base
    warnings = wb._check_risky_config()
    # Expect all four:
    assert any("Aggressive Martingale" in w for w in warnings)
    assert any("max_loss" in w for w in warnings)
    assert any("Multiplier" in w for w in warnings)
    assert any("basebet_pct" in w for w in warnings)
    assert len(warnings) >= 4

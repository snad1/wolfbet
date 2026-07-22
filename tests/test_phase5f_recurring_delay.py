"""Phase 5f tests — recurring_delay (TG↔CLI parity).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5f_recurring_delay.py -q
"""

import os
import sys
import time

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
    s.currency = "usdt"
    s.rule = "over"
    s.multiplier = 2.01
    s.base_bet = 0.0001
    s.current_bet = 0.0001
    s.win_mult = 1.0
    s.loss_mult = 2.0
    s.current_balance = 1.0
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.recurring = False
    s.recurring_delay = 60.0
    s.basebet_pct = 0.0
    s.pipeline_depth = 1
    s.custom_rules = []
    s.consecutive_recovery_fails = 0
    s.last_stop_reason = ""
    # counters
    s.session_id = 42
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.wagered = 0.0
    s._auto_uuid = None


# ── Default + membership ──────────────────────────────────
def test_default_recurring_delay_60():
    _reset_state()
    assert wb.state.recurring_delay == 60.0


def test_recurring_delay_in_config_keys():
    assert "recurring_delay" in wb._CONFIG_KEYS


def test_recurring_delay_in_preset_keys():
    assert "recurring_delay" in wb._PRESET_KEYS


def test_recurring_delay_in_tweak_params():
    assert "recurring_delay" in wb._TWEAK_PARAMS


def test_alias_recdelay():
    assert wb._TWEAK_ALIAS.get("recdelay") == "recurring_delay"


def test_alias_recurringdelay():
    assert wb._TWEAK_ALIAS.get("recurringdelay") == "recurring_delay"


def test_recurring_delay_in_show_fields():
    fields = {f[0] for f in wb._CONFIG_SHOW_FIELDS}
    assert "recurring_delay" in fields


# ── _apply_mutation ───────────────────────────────────────
def test_apply_mutation_sets_recurring_delay():
    _reset_state()
    msgs = wb._apply_mutation({"recurring_delay": "90"})
    assert wb.state.recurring_delay == 90.0
    assert any("Recurring delay" in m and "90" in m for m in msgs)


def test_apply_mutation_off_zeroes_delay():
    _reset_state()
    wb.state.recurring_delay = 60.0
    wb._apply_mutation({"recurring_delay": "off"})
    assert wb.state.recurring_delay == 0.0


def test_apply_mutation_zero_is_off():
    _reset_state()
    wb._apply_mutation({"recurring_delay": "0"})
    assert wb.state.recurring_delay == 0.0


def test_apply_mutation_clamps_high():
    """v2.30.6 — cap raised from 3600 to 86400 (24 h); 99999 → 86400."""
    _reset_state()
    wb._apply_mutation({"recurring_delay": "99999"})
    assert wb.state.recurring_delay == 86400.0


def test_apply_mutation_clamps_low():
    _reset_state()
    wb._apply_mutation({"recurring_delay": "-50"})
    assert wb.state.recurring_delay == 0.0


def test_apply_mutation_bad_value_raises():
    _reset_state()
    try:
        wb._apply_mutation({"recurring_delay": "abc"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_apply_mutation_via_alias_recdelay(monkeypatch):
    """Simulate the CLI --config-set path: alias resolved by _parse_config_set_pairs."""
    _reset_state()
    parsed = wb._parse_config_set_pairs(["recdelay=45"])
    assert parsed == {"recurring_delay": "45"}
    wb._apply_mutation(parsed)
    assert wb.state.recurring_delay == 45.0


# ── _reset_session_for_recurring — sleeps + preserves session_id ──
def test_reset_sleeps_when_delay_positive(monkeypatch):
    _reset_state()
    wb.state.recurring_delay = 30.0
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    # _db_start_session touches the DB; monkeypatch to a stub.
    monkeypatch.setattr(wb, "_db_start_session", lambda: 999)
    wb._reset_session_for_recurring()
    assert 30.0 in calls, f"expected 30.0 in sleep calls, got {calls}"


def test_reset_does_not_sleep_when_delay_zero(monkeypatch):
    _reset_state()
    wb.state.recurring_delay = 0.0
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    monkeypatch.setattr(wb, "_db_start_session", lambda: 999)
    wb._reset_session_for_recurring()
    # 0 or absent — we allow the case where 0 was NOT called
    assert 0.0 not in calls and all(c != 0.0 for c in calls if c)


# ── _check_risky_config warning wording ───────────────────
def test_risky_recurring_runaway_no_delay(monkeypatch):
    _reset_state()
    wb.state.recurring = True
    wb.state.recurring_delay = 0.0
    wb.state.max_profit = None
    wb.state.max_bets = None
    warnings = wb._check_risky_config()
    assert any("Recurring restart" in w and "back-to-back" in w for w in warnings)


def test_risky_recurring_runaway_with_delay(monkeypatch):
    _reset_state()
    wb.state.recurring = True
    wb.state.recurring_delay = 60.0
    wb.state.max_profit = None
    wb.state.max_bets = None
    warnings = wb._check_risky_config()
    assert any("Recurring restart" in w and "60s" in w for w in warnings)


def test_risky_recurring_ok_with_cap():
    _reset_state()
    wb.state.recurring = True
    wb.state.recurring_delay = 0.0
    wb.state.max_bets = 1000
    warnings = wb._check_risky_config()
    assert not any("Recurring restart" in w for w in warnings)


# ── Config-show api_key SET/NOT SET indicator (string presence) ──
def test_config_show_marks_api_key_state():
    import inspect
    src = inspect.getsource(wb.cmd_config_show)
    assert "(SET)" in src
    assert "(NOT SET)" in src

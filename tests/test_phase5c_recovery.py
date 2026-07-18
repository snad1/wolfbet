"""Phase 5c tests — auto-bet recovery on 'Max nonce achieved' + session expired.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5c_recovery.py -q
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
    s.game = "dice-auto"
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
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.wagered = 0.0
    s.session_id = 42
    s._auto_uuid = "seed-uuid-123"
    s.last_stop_reason = ""
    s.consecutive_recovery_fails = 0
    s.consecutive_errors = 0
    s.backoff_delay = 1.0
    s.rate_backoff = 2.0
    s.running = True
    s.paused = False
    s._insufficient_balance = False


# ── _classify_auto_stop_reason ────────────────────────────
def test_classify_max_nonce():
    assert wb._classify_auto_stop_reason("Max nonce achieved. Change your server seed.") == "max_nonce"


def test_classify_max_nonce_short():
    assert wb._classify_auto_stop_reason("nonce exhausted") == "max_nonce"


def test_classify_session_expired():
    assert wb._classify_auto_stop_reason("Session has ended") == "expired"


def test_classify_expired_generic():
    assert wb._classify_auto_stop_reason("Bet session expired") == "expired"


def test_classify_not_exists():
    assert wb._classify_auto_stop_reason("betsession not exists") == "expired"


def test_classify_max_profit_returns_empty():
    """Server-side max_profit is NOT a recovery reason — it's terminal for the WolfBet cycle."""
    assert wb._classify_auto_stop_reason("Max profit reached") == ""


def test_classify_max_loss_returns_empty():
    assert wb._classify_auto_stop_reason("Max loss reached") == ""


def test_classify_empty_returns_empty():
    assert wb._classify_auto_stop_reason("") == ""
    assert wb._classify_auto_stop_reason(None) == ""


def test_classify_is_case_insensitive():
    assert wb._classify_auto_stop_reason("MAX NONCE ACHIEVED") == "max_nonce"


# ── _recover_expired_session — success + failure paths ────
def test_recover_success_clears_flags(monkeypatch):
    _reset_state()
    wb.state.last_stop_reason = "max_nonce"
    wb.state.consecutive_recovery_fails = 2

    monkeypatch.setattr(wb, "_rotate_server_seed", lambda: True)
    monkeypatch.setattr(wb, "api_auto_stop", lambda: None)
    monkeypatch.setattr(wb, "api_auto_start", lambda: (setattr(wb.state, "_auto_uuid", "new-uuid"), True)[1])
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    assert wb._recover_expired_session() is True
    assert wb.state.last_stop_reason == ""
    assert wb.state.consecutive_recovery_fails == 0


def test_recover_failure_increments_fail_counter(monkeypatch):
    _reset_state()
    wb.state.last_stop_reason = "max_nonce"

    monkeypatch.setattr(wb, "_rotate_server_seed", lambda: False)
    monkeypatch.setattr(wb, "api_auto_stop", lambda: None)
    monkeypatch.setattr(wb, "api_auto_start", lambda: False)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    assert wb._recover_expired_session() is False
    assert wb.state.consecutive_recovery_fails == 1
    assert wb.state.last_stop_reason == "max_nonce"  # unchanged — recovery didn't succeed


def test_recover_three_failures_reaches_circuit_breaker_threshold(monkeypatch):
    _reset_state()
    wb.state.last_stop_reason = "max_nonce"

    monkeypatch.setattr(wb, "_rotate_server_seed", lambda: False)
    monkeypatch.setattr(wb, "api_auto_stop", lambda: None)
    monkeypatch.setattr(wb, "api_auto_start", lambda: False)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    for i in range(3):
        wb._recover_expired_session()
    assert wb.state.consecutive_recovery_fails == 3


# ── Same-CLI-session guarantee ────────────────────────────
def test_recovery_does_not_touch_session_id(monkeypatch):
    _reset_state()
    wb.state.session_id = 42
    wb.state.total_bets = 100
    wb.state.profit = 0.5
    wb.state.last_stop_reason = "max_nonce"

    monkeypatch.setattr(wb, "_rotate_server_seed", lambda: True)
    monkeypatch.setattr(wb, "api_auto_stop", lambda: None)
    monkeypatch.setattr(wb, "api_auto_start", lambda: True)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    wb._recover_expired_session()

    assert wb.state.session_id == 42
    assert wb.state.total_bets == 100
    assert wb.state.profit == 0.5


# ── Successful bet resets consecutive_recovery_fails ──────
def test_apply_outcome_resets_recovery_counter():
    _reset_state()
    wb.state.consecutive_recovery_fails = 2
    envelope = {
        "payload": {"amount": 0.0001, "bet_no": 1},
        "result": {
            "bet": {"state": "win", "result_value": 50.0, "amount": 0.0001, "profit": 0.0001},
            "userBalance": {"amount": 1.0001},
        },
    }
    wb._apply_outcome_locked(envelope)
    assert wb.state.consecutive_recovery_fails == 0


# ── Dashboard renders without exceptions (smoke) ──────────
def test_dashboard_renders_dice():
    _reset_state()
    wb.state.game = "dice"
    screen = wb.build_dashboard_screen()
    assert isinstance(screen, str)
    assert "ROI" in screen
    assert "p50" in screen
    assert "p99" in screen


def test_dashboard_renders_dice_auto():
    _reset_state()
    wb.state.game = "dice-auto"
    wb.state._auto_start_remaining = 10
    screen = wb.build_dashboard_screen()
    assert "auto-start" in screen


def test_dashboard_renders_limbo_auto():
    _reset_state()
    wb.state.game = "limbo-auto"
    wb.state._auto_start_remaining = 2  # low quota → red tint
    screen = wb.build_dashboard_screen()
    assert "auto-start" in screen


# ── ROI %, p50/p99 helpers ────────────────────────────────
def test_p99_empty_returns_zero():
    _reset_state()
    wb.state._api_samples.clear()
    assert wb._compute_api_p99() == 0.0


def test_p99_returns_a_sample():
    _reset_state()
    wb.state._api_samples.clear()
    for x in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        wb.state._api_samples.append(x)
    p99 = wb._compute_api_p99()
    assert p99 >= 90.0  # last decile

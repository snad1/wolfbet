"""Phase 9b — v2.32.1: insufficient balance on dice-auto must stop, never "recover".

A long Martingale loss chain left too little balance for the next doubled bet.
WolfBet answered HTTP 400 "You have insufficient balance…", the CLI labelled it
"expired", restarted a fresh auto-cycle at base bet with the session in the red,
and the streak-reset trigger (profit ≥ 0) could never fire again. The session
kept betting until the balance was gone.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase9b_insufficient_400.py -q
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

INSUFFICIENT_BODY = (
    '{"error":{"message":"You have insufficient balance, to place {{amount}} '
    '{{currency}} bet","values":{"currency":"usdt","amount":"1.00000000"}}}'
)


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text
        self.headers = {}

    def json(self):
        import json
        return json.loads(self.text)


def _reset_state():
    s = wb.state
    s.game = "dice-auto"
    s.strategy_key = "2"
    s.strategy = "Martingale"
    s.currency = "usdt"
    s.rule = "over"
    s.multiplier = 2.0
    s.bet_value = 50.49
    s.base_bet = 0.0001
    s.current_bet = 0.0001
    s.loss_mult = 2.0
    s.win_mult = 1.0
    s.current_balance = 0.05
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.wagered = 0.0
    s.current_streak = 0
    s.session_id = 42
    s._auto_uuid = "uuid-test"
    s.last_stop_reason = ""
    s.last_error = ""
    s.consecutive_recovery_fails = 0
    s.consecutive_errors = 0
    s.backoff_delay = 1.0
    s.rate_backoff = 2.0
    s.rate_wait = False
    s.running = True
    s.paused = False
    s._insufficient_balance = False
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.streak_reset_losses = 3
    s.basebet_pct = 0
    s.bet_delay = 0


def _no_side_effects(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(wb, "_db_save_session", lambda: None)
    monkeypatch.setattr(wb, "_get_streak_delay", lambda: 0)


# ── api_place_bet: 400 insufficient is terminal, not "expired" ──
def test_bet_400_insufficient_sets_flag_not_expired(monkeypatch):
    _reset_state()
    _no_side_effects(monkeypatch)
    monkeypatch.setattr(wb._http_session, "post",
                        lambda *a, **k: _Resp(400, INSUFFICIENT_BODY))
    assert wb.api_place_bet(1.0) is None
    s = wb.state
    assert s._insufficient_balance is True
    assert s.last_stop_reason != "expired"
    assert s.last_error == "Insufficient balance"
    assert s._auto_uuid is None


def test_bet_400_session_expired_still_classified_expired(monkeypatch):
    _reset_state()
    _no_side_effects(monkeypatch)
    monkeypatch.setattr(wb._http_session, "post",
                        lambda *a, **k: _Resp(400, '{"error":{"message":"Session has ended"}}'))
    assert wb.api_place_bet(0.0001) is None
    assert wb.state.last_stop_reason == "expired"
    assert wb.state._insufficient_balance is False


# ── api_auto_start: 400 insufficient is one attempt + flag ──
def test_auto_start_400_insufficient_sets_flag(monkeypatch):
    _reset_state()
    _no_side_effects(monkeypatch)
    wb.state._auto_uuid = None
    wb.state._auto_start_last_time = -1e9
    wb.state._auto_start_remaining = None
    calls = []

    def _post(*a, **k):
        calls.append(1)
        return _Resp(400, INSUFFICIENT_BODY)

    monkeypatch.setattr(wb._http_session, "post", _post)
    assert wb.api_auto_start() is False
    assert wb.state._insufficient_balance is True
    assert len(calls) == 1


# ── betting_loop: never restart a server cycle after insufficient / mid-chain ──
def _forbid_restart(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("auto-cycle must not restart")
    monkeypatch.setattr(wb, "_recover_expired_session", _boom)
    monkeypatch.setattr(wb, "api_auto_start", _boom)


def test_loop_stops_on_insufficient_without_restart(monkeypatch):
    """Replay of the incident: the bet after a long loss chain gets 400 insufficient."""
    _reset_state()
    _no_side_effects(monkeypatch)
    _forbid_restart(monkeypatch)
    wb.state.current_streak = -10
    wb.state.profit = -0.5
    monkeypatch.setattr(wb._http_session, "post",
                        lambda *a, **k: _Resp(400, INSUFFICIENT_BODY))
    wb.betting_loop()
    assert wb.state.running is False
    assert wb.state.status == "STOPPED: Insufficient balance"


def test_loop_stops_midchain_expired(monkeypatch):
    _reset_state()
    _no_side_effects(monkeypatch)
    _forbid_restart(monkeypatch)
    wb.state._auto_uuid = None
    wb.state.last_stop_reason = "expired"
    wb.state.current_streak = -4
    wb.betting_loop()
    assert wb.state.running is False
    assert wb.state.status.startswith("STOPPED: Auto-cycle lost mid-chain")


def test_loop_stops_in_red_after_win(monkeypatch):
    _reset_state()
    _no_side_effects(monkeypatch)
    _forbid_restart(monkeypatch)
    wb.state._auto_uuid = None
    wb.state.last_stop_reason = "max_nonce"
    wb.state.current_streak = 2
    wb.state.profit = -0.01
    wb.betting_loop()
    assert wb.state.status.startswith("STOPPED: Auto-cycle lost mid-chain")


def test_loop_still_recovers_when_safe(monkeypatch):
    _reset_state()
    _no_side_effects(monkeypatch)
    wb.state._auto_uuid = None
    wb.state.last_stop_reason = "expired"
    wb.state.current_streak = 1
    wb.state.profit = 0.0001
    recovered = []

    def _recover():
        recovered.append(1)
        wb.state._auto_uuid = "fresh"
        return True

    def _bet(_amt):
        wb.state.running = False  # exit after the first attempt
        return None

    monkeypatch.setattr(wb, "_recover_expired_session", _recover)
    monkeypatch.setattr(wb, "api_place_bet", _bet)
    wb.betting_loop()
    assert recovered == [1]


def test_loop_fresh_start_unaffected(monkeypatch):
    _reset_state()
    _no_side_effects(monkeypatch)
    wb.state._auto_uuid = None
    started = []

    def _start():
        started.append(1)
        wb.state._auto_uuid = "fresh"
        return True

    def _bet(_amt):
        wb.state.running = False
        return None

    monkeypatch.setattr(wb, "api_auto_start", _start)
    monkeypatch.setattr(wb, "api_place_bet", _bet)
    wb.betting_loop()
    assert started == [1]


# ── chain depth + config warning ──
def test_chain_depth_basic():
    assert wb.martingale_chain_depth(1.0, 0.001, 2.0) == 9


def test_chain_depth_degenerate():
    assert wb.martingale_chain_depth(0, 0.001, 2.0) == 0
    assert wb.martingale_chain_depth(1.0, 0, 2.0) == 0
    assert wb.martingale_chain_depth(1.0, 0.001, 1.0) == 0


def test_risky_config_warns_chain_depth():
    _reset_state()
    wb.state.current_balance = 1.0
    wb.state.base_bet = 0.001
    wb.state.loss_mult = 2.0
    warns = wb._check_risky_config()
    assert any("covers a 9-loss chain" in w for w in warns)
    wb.state.max_loss = 0.2
    warns = wb._check_risky_config()
    assert not any("covers a" in w for w in warns)

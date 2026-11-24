"""Phase 14: session rules (pre-bet loss cap, loss restart, balance goal, phases)
and the multi-session backtest.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase14_session_box.py -q
"""

import argparse
import copy
import json
import os
import random
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import pytest  # noqa: E402

import wolfbet as wb  # noqa: E402
from core import session_rules as sr  # noqa: E402

SAT = 1e-8
TARGET = 100 * SAT          # one session aims for 100 sats
CAP = 1500 * SAT            # four Martingale losses in a row: 100 + 200 + 400 + 800


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Start every test from a known state and put the shared state back afterwards."""
    s = wb.state
    saved = {k: copy.copy(v) for k, v in vars(s).items() if k != "lock"}
    s.game = "dice"
    s.currency = "usdt"
    s.rule = "over"
    s.multiplier = 2.0
    s.bet_value = 50.49
    s.strategy = "Martingale"
    s.strategy_key = "2"
    s.base_bet = TARGET
    s.current_bet = TARGET
    s.loss_mult = 2.0
    s.win_mult = 2.0
    s.current_balance = 1.0
    s.start_balance = 1.0
    s.highest_balance = 1.0
    s.lowest_balance = 1.0
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.wagered = 0.0
    s.current_streak = 0
    s.last_broken_loss_streak = 0
    s.dalembert_unit = 0
    s.paroli_count = 0
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.recurring = False
    s.recurring_delay = 0.0
    s.streak_reset_losses = None
    s.profit_increment = None
    s.profit_threshold = None
    s.basebet_pct = 0.0
    s.bet_delay = 0
    s.soft_pace_delay = 0.0
    s.streak_delay_loss = (0, 0.0)
    s.streak_delay_win = (0, 0.0)
    s.streak_delay_bets = (0, 0.0)
    s.streakbet_loss = (0, 1.0)
    s.milestone_bets = 0
    s.milestone_wins = 0
    s.milestone_losses = 0
    s.milestone_profit = 0.0
    s.pipeline_depth = 1
    s.lane_share = None
    s.custom_rules = []
    s.custom_rules_text = ""
    s.hard_cap = False
    s.restart_after_loss = False
    s.balance_goal = None
    s.hybrid_recover_at = None
    s.hybrid_recover_strategy = "2"
    s.hybrid_recover_bet = 1.0
    s.hybrid_press_at = None
    s.hybrid_press_floor = 0.0
    s.hybrid_press_strategy = "5"
    s.hybrid_press_bet = 1.0
    s.hybrid_phase = "base"
    s.hybrid_switches = 0
    s.session_id = 1
    s._auto_uuid = None
    s._auto_restart_pending = False
    s._insufficient_balance = False
    s.last_stop_reason = ""
    s.last_error = ""
    s.consecutive_errors = 0
    s.consecutive_recovery_fails = 0
    s.backoff_delay = 1.0
    s.rate_wait = False
    s.running = True
    s.paused = False
    yield
    for key in [k for k in vars(s) if k != "lock" and k not in saved]:
        delattr(s, key)
    for key, value in saved.items():
        setattr(s, key, value)


def _quiet_loop(monkeypatch):
    """Take the sleeps and the database out of betting_loop."""
    sessions = iter(range(2, 10_000))
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(wb, "_db_save_session", lambda: None)
    monkeypatch.setattr(wb, "_db_save_bet_dispatch", lambda *_: None)
    monkeypatch.setattr(wb, "_db_start_session", lambda: next(sessions))
    monkeypatch.setattr(wb, "_get_streak_delay", lambda: 0)


def _script_bets(monkeypatch, outcomes):
    """Answer each bet from `outcomes`; the loop ends when the script runs out."""
    placed = []
    script = iter(outcomes)

    def _bet(amount):
        outcome = next(script, None)
        if outcome is None:
            wb.state.running = False
            return None
        amount = round(amount, 8)
        placed.append(amount)
        profit = amount if outcome == "win" else -amount
        return {"bet": {"state": outcome, "result_value": 60.0 if outcome == "win" else 40.0,
                        "amount": amount, "profit": profit},
                "userBalance": {"amount": wb.state.current_balance + profit}}

    monkeypatch.setattr(wb, "api_place_bet", _bet)
    return placed


def _scripted_rolls(outcomes, seen=None):
    """Backtest source that sizes each roll from the bot's current bet."""
    for outcome in outcomes:
        amount = wb.state.current_bet
        if seen is not None:
            seen.append((wb.state.hybrid_phase, round(amount, 8)))
        profit = amount * (wb.state.multiplier - 1) if outcome == "win" else -amount
        yield {"bet": {"state": outcome, "result_value": 0.0, "amount": amount, "profit": profit},
               "userBalance": {"amount": wb.state.current_balance + profit}}


def _seeded_rolls(seed):
    rng = random.Random(seed)
    while True:
        yield wb._simulate_bet_outcome(wb.state.multiplier, wb.state.rule, wb.state.current_bet,
                                       wb.state.current_balance, rng)


def _bold_setup():
    """Martingale whose first bet is the target, under a cap that pays for four losses."""
    s = wb.state
    s.max_profit = TARGET
    s.max_loss = CAP
    s.hard_cap = True


# ── next_phase ────────────────────────────────────────────
@pytest.mark.parametrize("phase, profit, expected", [
    ("base", 0.0, "base"),
    ("base", -0.5, "recover"),
    ("base", -0.5 + SAT, "base"),
    ("recover", -SAT, "recover"),
    ("recover", 0.0, "base"),
    ("recover", 0.1, "base"),
    ("recover", 0.2, "press"),
    ("base", 0.2, "press"),
    ("press", 0.01, "press"),
    ("press", 0.0, "base"),
    ("press", -0.5, "recover"),
])
def test_next_phase_follows_session_profit(phase, profit, expected):
    assert sr.next_phase(phase, profit, 0.5, 0.2, 0.0) == expected


def test_next_phase_ignores_float_dust():
    assert sr.next_phase("recover", -1e-12, 0.5, None) == "base"
    assert sr.next_phase("base", 0.2 - 1e-12, None, 0.2) == "press"
    assert sr.next_phase("base", -0.5 + 1e-12, 0.5, None) == "recover"


def test_next_phase_stays_base_with_no_thresholds():
    for phase in ("base", "recover", "press"):
        assert sr.next_phase(phase, -9.0, None, None) == "base"
        assert sr.next_phase(phase, 9.0, None, None) == "base"


def test_press_holds_above_its_floor_only():
    assert sr.next_phase("press", 0.11, None, 0.2, 0.1) == "press"
    assert sr.next_phase("press", 0.1, None, 0.2, 0.1) == "base"


# ── room for the next bet ─────────────────────────────────
def test_loss_room_is_what_is_left_before_the_cap():
    assert sr.loss_room(-7.0, 15.0) == pytest.approx(8.0)
    assert sr.loss_room(2.0, 15.0) == pytest.approx(17.0)
    assert sr.loss_room(-20.0, 15.0) == 0.0
    assert sr.loss_room(-7.0, -15.0) == pytest.approx(8.0)


def test_loss_room_is_zero_without_a_usable_cap():
    assert sr.loss_room(0.0, None) == 0.0
    assert sr.loss_room(0.0, float("nan")) == 0.0
    assert sr.loss_room(0.0, float("inf")) == 0.0


def test_floor_room():
    assert sr.floor_room(2.0, 1.5) == pytest.approx(0.5)
    assert sr.floor_room(1.0, 1.5) == 0.0
    assert sr.floor_room(2.0, None) == float("inf")
    assert sr.floor_room(2.0, float("nan")) == 0.0


# ── value parsing ─────────────────────────────────────────
@pytest.mark.parametrize("raw", [None, "", "off", "none", "0", "OFF", "false"])
def test_parse_amount_off_values(raw):
    assert sr.parse_amount("x", raw) is None


def test_parse_amount_reads_numbers():
    assert sr.parse_amount("x", "0.000015") == pytest.approx(0.000015)
    assert sr.parse_amount("x", 2) == 2.0


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-1", "abc", "1e999"])
def test_parse_amount_refuses_bad_numbers(raw):
    with pytest.raises(ValueError) as err:
        sr.parse_amount("max_thing", raw)
    assert "max_thing" in str(err.value)


def test_parse_flag():
    assert sr.parse_flag("x", "on") is True
    assert sr.parse_flag("x", "YES") is True
    assert sr.parse_flag("x", True) is True
    assert sr.parse_flag("x", "off") is False
    assert sr.parse_flag("x", None) is False
    with pytest.raises(ValueError):
        sr.parse_flag("hard_cap", "maybe")


def test_parse_multiple():
    assert sr.parse_multiple("x", "25") == 25.0
    assert sr.parse_multiple("x", 1) == 1.0
    for raw in ("0.5", "0", "nan", "inf", "x"):
        with pytest.raises(ValueError):
            sr.parse_multiple("hybrid_press_bet", raw)


def test_parse_strategy_key_accepts_one_to_seven():
    assert sr.parse_strategy_key("x", "2") == "2"
    assert sr.parse_strategy_key("x", 5) == "5"
    for raw in ("8", "0", "paroli", ""):
        with pytest.raises(ValueError):
            sr.parse_strategy_key("hybrid_press_strategy", raw)


# ── config_errors ─────────────────────────────────────────
def _cfg(**over):
    cfg = dict(base_bet=TARGET, loss_mult=2.0, win_mult=2.0, max_profit=TARGET, max_loss=CAP,
               stop_on_balance=None, recurring=True, hard_cap=False, restart_after_loss=False,
               balance_goal=None, hybrid_recover_at=None, hybrid_recover_strategy="2",
               hybrid_recover_bet=1.0, hybrid_press_at=None, hybrid_press_floor=0.0,
               hybrid_press_strategy="5", hybrid_press_bet=1.0)
    cfg.update(over)
    return cfg


def test_config_errors_empty_when_nothing_is_switched_on():
    assert sr.config_errors(_cfg(max_loss=None, max_profit=None)) == []


@pytest.mark.parametrize("over", [
    dict(hard_cap=True),
    dict(hard_cap=True, restart_after_loss=True, stop_on_balance=0.9),
    dict(balance_goal=1.1, stop_on_balance=0.9),
    dict(base_bet=TARGET / 4, max_loss=1575 * SAT, hybrid_press_at=TARGET / 4,
         hybrid_press_strategy="3"),
    dict(base_bet=SAT, max_loss=1000 * SAT, max_profit=50 * SAT, hybrid_recover_at=100 * SAT,
         hybrid_recover_bet=25.0, hybrid_press_at=20 * SAT, hybrid_press_bet=10.0),
])
def test_config_errors_accepts_usable_setups(over):
    assert sr.config_errors(_cfg(**over)) == []


@pytest.mark.parametrize("over, names", [
    (dict(hard_cap=True, max_loss=None), ["hard_cap needs max_loss"]),
    (dict(hard_cap=True, max_loss=float("nan")), ["hard_cap needs max_loss"]),
    (dict(hybrid_press_at=TARGET / 4, base_bet=TARGET / 4, max_loss=None),
     ["hybrid phases need max_loss"]),
    (dict(hard_cap=True, base_bet=CAP * 2), ["base_bet", "first bet does not fit"]),
    (dict(hard_cap=True, restart_after_loss=True, stop_on_balance=0.9, recurring=False),
     ["restart_after_loss needs recurring=on"]),
    (dict(restart_after_loss=True, stop_on_balance=0.9), ["restart_after_loss needs hard_cap=on"]),
    (dict(hard_cap=True, restart_after_loss=True), ["restart_after_loss needs stop_on_balance"]),
    (dict(balance_goal=0.8, stop_on_balance=0.9), ["balance_goal", "above stop_on_balance"]),
    (dict(balance_goal=float("inf")), ["balance_goal must be a positive number"]),
    (dict(hybrid_recover_at=CAP), ["hybrid_recover_at", "below max_loss"]),
    (dict(hybrid_recover_at=CAP / 2, hybrid_recover_bet=100.0),
     ["hybrid_recover_bet", "does not fit"]),
    (dict(hybrid_recover_at=CAP / 2, hybrid_recover_strategy="8"),
     ["hybrid_recover_strategy must be a strategy key 1-7"]),
    (dict(hybrid_recover_at=CAP / 2, loss_mult=1.0),
     ["hybrid_recover_strategy", "needs loss_mult above 1"]),
    (dict(hybrid_recover_at=CAP / 2, hybrid_recover_bet=0.5), ["hybrid_recover_bet must be 1 or more"]),
    (dict(base_bet=SAT, hybrid_press_at=20 * SAT, hybrid_press_floor=20 * SAT),
     ["hybrid_press_floor", "below hybrid_press_at"]),
    (dict(base_bet=SAT, hybrid_press_at=TARGET), ["hybrid_press_at", "below max_profit"]),
    (dict(base_bet=SAT, hybrid_press_at=20 * SAT, win_mult=1.0),
     ["hybrid_press_strategy", "needs win_mult above 1"]),
    (dict(base_bet=SAT, hybrid_press_at=20 * SAT, hybrid_press_strategy="9"),
     ["hybrid_press_strategy must be a strategy key 1-7"]),
    (dict(base_bet=SAT, hybrid_press_at=20 * SAT, hybrid_press_floor="x"),
     ["hybrid_press_floor must be zero or a positive number"]),
])
def test_config_errors_name_the_setting(over, names):
    text = " | ".join(sr.config_errors(_cfg(**over)))
    for name in names:
        assert name in text


# ── phase helpers on the bot state ────────────────────────
def test_active_strategy_and_base_follow_the_phase():
    s = wb.state
    s.strategy_key = "1"
    s.base_bet = SAT
    s.hybrid_recover_at = 100 * SAT
    s.hybrid_recover_bet = 25.0
    s.hybrid_press_at = 20 * SAT
    s.hybrid_press_strategy = "3"
    s.hybrid_press_bet = 10.0
    assert (wb._active_strategy_key(), wb._active_base_bet()) == ("1", SAT)
    s.hybrid_phase = "recover"
    assert wb._active_strategy_key() == "2"
    assert wb._active_base_bet() == pytest.approx(25 * SAT)
    s.hybrid_phase = "press"
    assert wb._active_strategy_key() == "3"
    assert wb._active_base_bet() == pytest.approx(10 * SAT)


def test_phases_off_use_the_configured_strategy_even_with_a_stale_phase():
    s = wb.state
    s.hybrid_phase = "press"
    assert wb._hybrid_on() is False
    assert wb._active_strategy_key() == "2"
    assert wb._active_base_bet() == TARGET
    assert wb._hard_cap_on() is False


def test_phases_imply_the_pre_bet_cap():
    wb.state.hybrid_press_at = TARGET
    assert wb._hard_cap_on() is True


def test_hybrid_step_switches_and_restarts_the_strategy(monkeypatch):
    s = wb.state
    stops = []
    monkeypatch.setattr(wb, "api_auto_stop", lambda: stops.append(1))
    s.base_bet = SAT
    s.hybrid_recover_at = 100 * SAT
    s.hybrid_recover_bet = 25.0
    s.profit = -100 * SAT
    s.current_bet = SAT
    s.dalembert_unit = 4
    s.paroli_count = 2
    assert wb._hybrid_step() is True
    assert s.hybrid_phase == "recover"
    assert s.current_bet == pytest.approx(25 * SAT)
    assert (s.dalembert_unit, s.paroli_count, s.hybrid_switches) == (0, 0, 1)
    assert wb._hybrid_step() is False
    assert stops == []


def test_hybrid_step_ends_the_server_cycle_on_an_auto_game(monkeypatch):
    s = wb.state
    stops = []
    monkeypatch.setattr(wb, "api_auto_stop", lambda: stops.append(1))
    s.game = "dice-auto"
    s.hybrid_press_at = 20 * SAT
    s.profit = 20 * SAT
    assert wb._hybrid_step() is True
    assert stops == [1]


def test_hybrid_step_does_nothing_with_phases_off(monkeypatch):
    monkeypatch.setattr(wb, "api_auto_stop", lambda: pytest.fail("no cycle to end"))
    wb.state.profit = -1.0
    assert wb._hybrid_step() is False
    assert wb.state.hybrid_phase == "base"


def test_hybrid_step_returns_to_base_when_phases_are_switched_off(monkeypatch):
    s = wb.state
    monkeypatch.setattr(wb, "api_auto_stop", lambda: None)
    s.hybrid_phase = "recover"
    s.current_bet = 25 * SAT
    assert wb._hybrid_step() is True
    assert s.hybrid_phase == "base"
    assert s.current_bet == TARGET


def test_next_bet_is_sized_by_the_phase_strategy():
    s = wb.state
    s.strategy_key = "1"
    s.base_bet = SAT
    s.hybrid_recover_at = 100 * SAT
    s.hybrid_recover_bet = 25.0
    s.hybrid_press_at = 20 * SAT
    s.hybrid_press_bet = 10.0
    s.hybrid_phase = "recover"
    s.current_bet = 25 * SAT
    assert wb.compute_next_bet("loss") == pytest.approx(50 * SAT)
    assert wb.compute_next_bet("win") == pytest.approx(25 * SAT)
    s.hybrid_phase = "press"
    s.current_bet = 10 * SAT
    assert wb.compute_next_bet("win") == pytest.approx(20 * SAT)
    s.current_bet = 20 * SAT
    assert wb.compute_next_bet("win") == pytest.approx(40 * SAT)
    s.current_bet = 40 * SAT
    assert wb.compute_next_bet("win") == pytest.approx(10 * SAT)


# ── exact targets and float dust ──────────────────────────
def test_profit_target_is_reached_despite_float_dust():
    s = wb.state
    s.max_profit = TARGET
    s.profit = -1e-6 - 2e-6 + 4e-6
    assert s.profit < TARGET
    stop, reason = wb.should_stop()
    assert stop is True
    assert reason.startswith("Profit target reached")


def test_max_loss_is_reached_despite_float_dust():
    s = wb.state
    s.max_loss = 3 * SAT
    s.profit = -1e-8 - 1e-8 - 1e-8 + 1e-17
    assert s.profit > -s.max_loss
    stop, reason = wb.should_stop()
    assert stop is True
    assert reason.startswith("Max loss hit")


def test_one_satoshi_short_of_the_target_is_not_reached():
    s = wb.state
    s.max_profit = TARGET
    s.profit = TARGET - SAT
    assert wb.should_stop() == (False, "")
    s.max_profit = None
    s.max_loss = TARGET
    s.profit = -TARGET + SAT
    assert wb.should_stop() == (False, "")


def test_a_target_below_one_satoshi_is_not_reached_at_zero_profit():
    s = wb.state
    s.max_profit = 1e-9
    assert wb.should_stop() == (False, "")
    s.profit = 1e-9
    assert wb.should_stop()[0] is True


def test_half_a_satoshi_short_is_not_reached():
    s = wb.state
    s.max_profit = SAT
    s.profit = SAT / 2
    assert wb.should_stop() == (False, "")


def test_flat_one_satoshi_session_ends_on_the_first_net_win():
    """Summed one-satoshi results drift off the exact value; the target must still fire."""
    s = wb.state
    s.strategy_key = "1"
    s.base_bet = SAT
    s.current_bet = SAT
    s.max_profit = SAT
    reason = wb.backtest_loop(_scripted_rolls(["loss", "loss", "win", "win", "win", "win"]),
                              progress_every=0)
    assert reason.startswith("Profit target reached")
    assert s.total_bets == 5


# ── pre-bet loss cap ──────────────────────────────────────
def test_cap_allows_a_bet_that_fits_exactly():
    s = wb.state
    _bold_setup()
    s.profit = -700 * SAT
    s.current_bet = 800 * SAT
    assert wb.should_stop() == (False, "")


def test_cap_stops_before_a_bet_that_would_pass_max_loss():
    s = wb.state
    _bold_setup()
    s.max_loss = 2000 * SAT
    s.profit = -1500 * SAT
    s.current_bet = 1600 * SAT
    stop, reason = wb.should_stop()
    assert stop is True
    assert reason.startswith("Loss cap: next bet 0.00001600 does not fit")


def test_without_the_cap_the_same_bet_is_allowed():
    s = wb.state
    _bold_setup()
    s.hard_cap = False
    s.max_loss = 2000 * SAT
    s.profit = -1500 * SAT
    s.current_bet = 1600 * SAT
    assert wb.should_stop() == (False, "")


def test_cap_also_keeps_the_balance_above_the_floor():
    s = wb.state
    _bold_setup()
    s.current_balance = 1.0
    s.stop_on_balance = 1.0 - 50 * SAT
    stop, reason = wb.should_stop()
    assert stop is True
    assert reason.startswith("Balance floor: next bet 0.00000100 does not fit")


def test_cap_without_max_loss_stops_and_says_why():
    s = wb.state
    s.hard_cap = True
    stop, reason = wb.should_stop()
    assert stop is True
    assert reason == "Loss cap: hard_cap needs max_loss"


def test_cap_forces_one_bet_at_a_time():
    s = wb.state
    s.strategy_key = "1"
    s.strategy = "Flat Bet"
    s.pipeline_depth = 4
    assert wb._effective_pipeline_depth() == 4
    s.hard_cap = True
    s.max_loss = CAP
    assert wb._effective_pipeline_depth() == 1
    assert "loss cap" in s.pipeline_reason


def test_loop_never_places_the_bet_that_does_not_fit(monkeypatch):
    s = wb.state
    _quiet_loop(monkeypatch)
    _bold_setup()
    s.max_loss = 2000 * SAT
    placed = _script_bets(monkeypatch, ["loss"] * 4 + ["win"])
    wb.betting_loop()
    assert placed == pytest.approx([100 * SAT, 200 * SAT, 400 * SAT, 800 * SAT])
    assert s.running is False
    assert s.status.startswith("STOPPED: Loss cap")
    assert s.profit == pytest.approx(-1500 * SAT)


def test_loop_without_the_cap_places_the_oversized_bet(monkeypatch):
    s = wb.state
    _quiet_loop(monkeypatch)
    _bold_setup()
    s.hard_cap = False
    s.max_loss = 2000 * SAT
    placed = _script_bets(monkeypatch, ["loss"] * 5)
    wb.betting_loop()
    assert placed[-1] == pytest.approx(1600 * SAT)
    assert s.profit == pytest.approx(-3100 * SAT)


# ── restart after a capped loss ───────────────────────────
def _loss_restart_setup():
    s = wb.state
    _bold_setup()
    s.recurring = True
    s.restart_after_loss = True
    s.stop_on_balance = 0.9
    s.profit = -CAP


@pytest.mark.parametrize("reason", ["Max loss hit: -0.00001500",
                                    "Loss cap: next bet 0.00001600 does not fit (left 0.00000500)"])
def test_capped_loss_restarts_with_every_guard_in_place(reason):
    _loss_restart_setup()
    assert wb._should_recurring_restart(reason) is True


@pytest.mark.parametrize("field, value", [
    ("restart_after_loss", False),
    ("recurring", False),
    ("hard_cap", False),
    ("max_loss", None),
    ("stop_on_balance", None),
    ("current_balance", 0.9),
])
def test_capped_loss_does_not_restart_with_a_guard_missing(field, value):
    _loss_restart_setup()
    setattr(wb.state, field, value)
    assert wb._should_recurring_restart("Max loss hit: -0.00001500") is False


@pytest.mark.parametrize("reason", [
    "Balance floor hit: 0.90000000",
    "Balance floor: next bet 0.00000100 does not fit above 0.90000000",
    "Insufficient balance",
    "Rule stop: Every 1 loss(s)",
    "Lane budget exceeded (next bet 0.00000100, lane equity 0.00000000)",
    "Auto-cycle lost mid-chain (streak -4, profit -0.00001500)",
    "Balance goal reached: 1.10000000",
])
def test_other_loss_stops_never_restart(reason):
    _loss_restart_setup()
    assert wb._should_recurring_restart(reason) is False


def test_a_bet_that_never_fits_cannot_restart_forever():
    _loss_restart_setup()
    wb.state.profit = 0.0
    assert wb._should_recurring_restart("Loss cap: next bet 0.00002000 does not fit") is False


def test_restarts_in_profit_are_unchanged():
    s = wb.state
    s.recurring = True
    s.profit = TARGET
    assert wb._should_recurring_restart("Profit target reached: +0.00000100") is True
    s.recurring = False
    assert wb._should_recurring_restart("Profit target reached: +0.00000100") is False


def test_loop_restarts_after_a_capped_loss_then_stops_at_the_goal(monkeypatch):
    s = wb.state
    _quiet_loop(monkeypatch)
    _loss_restart_setup()
    s.profit = 0.0
    s.stop_on_balance = 1.0 - 5000 * SAT
    s.balance_goal = 1.0 + 150 * SAT
    placed = _script_bets(monkeypatch, ["loss"] * 4 + ["win"] * 17 + ["loss"])
    wb.betting_loop()
    assert placed == pytest.approx([100 * SAT, 200 * SAT, 400 * SAT, 800 * SAT] + [100 * SAT] * 17)
    assert s.status.startswith("STOPPED: Balance goal reached")
    assert s.current_balance == pytest.approx(1.0 + 200 * SAT)
    assert s.session_id == 18


def test_new_session_starts_every_strategy_from_its_first_step(monkeypatch):
    s = wb.state
    _quiet_loop(monkeypatch)
    s.hybrid_phase = "press"
    s.dalembert_unit = 5
    s.paroli_count = 2
    s.current_bet = 600 * SAT
    wb._reset_session_for_recurring(0)
    assert s.hybrid_phase == "base"
    assert (s.dalembert_unit, s.paroli_count) == (0, 0)
    assert s.current_bet == TARGET


# ── balance goal ──────────────────────────────────────────
def test_balance_goal_stops_and_does_not_restart():
    s = wb.state
    s.recurring = True
    s.max_profit = TARGET
    s.profit = TARGET
    s.balance_goal = 1.0
    stop, reason = wb.should_stop()
    assert stop is True
    assert reason.startswith("Balance goal reached")
    assert wb._should_recurring_restart(reason) is False


def test_balance_goal_off_changes_nothing():
    assert wb.should_stop() == (False, "")


# ── auto games ────────────────────────────────────────────
def test_auto_config_and_payload_follow_the_phase():
    s = wb.state
    s.game = "dice-auto"
    s.strategy_key = "1"
    s.base_bet = SAT
    s.max_profit = 50 * SAT
    s.max_loss = 1000 * SAT
    s.hybrid_recover_at = 100 * SAT
    s.hybrid_recover_bet = 25.0
    s.hybrid_phase = "recover"
    s.profit = -100 * SAT
    payload = wb._build_auto_start_payload()
    assert payload["amount"] == "0.00000025"
    commands = [(r["command"][0]["name"], r["when"][0]["name"]) for r in payload["config"]]
    assert ("increaseAmountPercent", "lose") in commands
    stops = {r["when"][0]["name"]: r["when"][0]["value"]
             for r in payload["config"] if r["command"][0]["name"] == "stop"}
    assert stops["profit"] == pytest.approx(150 * SAT)
    assert stops["loss"] == pytest.approx(900 * SAT)


def test_auto_stop_rules_are_unchanged_without_phases():
    s = wb.state
    s.game = "dice-auto"
    s.max_profit = TARGET
    s.max_loss = CAP
    s.profit = -300 * SAT
    assert wb._auto_stop_conditions() == {"max_profit": TARGET, "max_loss": CAP,
                                          "max_bets": None, "stop_on_balance": None}


def test_auto_cycle_restarts_in_the_red_when_a_phase_owns_recovery(monkeypatch):
    s = wb.state
    _quiet_loop(monkeypatch)
    s.game = "dice-auto"
    s.strategy_key = "1"
    s.base_bet = SAT
    s.max_loss = 1000 * SAT
    s.hybrid_recover_at = 100 * SAT
    s.hybrid_recover_bet = 25.0
    s.profit = -200 * SAT
    s.current_streak = -4
    s.last_stop_reason = "expired"
    recovered = []

    def _recover():
        recovered.append(1)
        s._auto_uuid = "fresh"
        return True

    def _bet(_amount):
        s.running = False
        return None

    monkeypatch.setattr(wb, "_recover_expired_session", _recover)
    monkeypatch.setattr(wb, "api_place_bet", _bet)
    wb.betting_loop()
    assert recovered == [1]
    assert s.hybrid_phase == "recover"
    assert s.current_bet == pytest.approx(25 * SAT)


def test_auto_cycle_lost_in_the_red_still_stops_without_phases(monkeypatch):
    s = wb.state
    _quiet_loop(monkeypatch)
    s.game = "dice-auto"
    s.profit = -200 * SAT
    s.current_streak = -4
    s.last_stop_reason = "expired"
    monkeypatch.setattr(wb, "_recover_expired_session", lambda: pytest.fail("must not restart"))
    monkeypatch.setattr(wb, "api_auto_start", lambda: pytest.fail("must not restart"))
    wb.betting_loop()
    assert s.status.startswith("STOPPED: Auto-cycle lost mid-chain")


# ── labels and live state ─────────────────────────────────
def test_phase_shows_beside_the_strategy():
    assert wb._with_phase("Martingale", "press") == "Martingale  phase press"
    assert wb._with_phase("Martingale", None) == "Martingale"


def test_session_row_marks_sessions_that_ran_with_phases(monkeypatch, tmp_path):
    s = wb.state
    monkeypatch.setattr(wb, "DB_PATH", str(tmp_path / "bot.db"))
    wb.init_db()
    plain = wb._db_start_session()
    s.hybrid_press_at = TARGET
    hybrid = wb._db_start_session()
    conn = sqlite3.connect(wb.DB_PATH)
    rows = dict(conn.execute("SELECT id, strategy FROM sessions").fetchall())
    conn.close()
    assert rows[plain] == "Martingale"
    assert rows[hybrid] == "Hybrid: Martingale"


def test_live_state_carries_the_phase_only_when_phases_are_on(monkeypatch, tmp_path):
    s = wb.state
    monkeypatch.setattr(wb, "STATE_PATH", str(tmp_path / "live.json"))
    wb._save_state_file()
    assert json.load(open(wb.STATE_PATH))["hybrid_phase"] is None
    s.hybrid_press_at = TARGET
    s.hybrid_phase = "press"
    wb._save_state_file()
    assert json.load(open(wb.STATE_PATH))["hybrid_phase"] == "press"


# ── config commands ───────────────────────────────────────
def test_mutation_sets_every_session_rule():
    s = wb.state
    wb._apply_mutation({
        "max_loss": "0.00001575", "max_profit": "0.000001", "hard_cap": "on",
        "restart_after_loss": "on", "balance_goal": "1.5", "hybrid_recover_at": "0.000001",
        "hybrid_recover_strategy": "4", "hybrid_recover_bet": "3", "hybrid_press_at": "0.00000025",
        "hybrid_press_floor": "0.0000001", "hybrid_press_strategy": "3", "hybrid_press_bet": "2",
    })
    assert (s.hard_cap, s.restart_after_loss) == (True, True)
    assert s.balance_goal == 1.5
    assert s.hybrid_recover_at == pytest.approx(100 * SAT)
    assert (s.hybrid_recover_strategy, s.hybrid_recover_bet) == ("4", 3.0)
    assert s.hybrid_press_at == pytest.approx(25 * SAT)
    assert s.hybrid_press_floor == pytest.approx(10 * SAT)
    assert (s.hybrid_press_strategy, s.hybrid_press_bet) == ("3", 2.0)


def test_mutation_switches_rules_off_again():
    s = wb.state
    s.hard_cap = True
    s.balance_goal = 1.5
    s.hybrid_press_at = TARGET
    s.hybrid_press_floor = SAT
    wb._apply_mutation({"hard_cap": "off", "balance_goal": "off", "hybrid_press_at": "off",
                        "hybrid_press_floor": "off"})
    assert (s.hard_cap, s.balance_goal, s.hybrid_press_at, s.hybrid_press_floor) == (False, None, None, 0.0)


@pytest.mark.parametrize("change", [
    {"hybrid_press_strategy": "8"},
    {"hybrid_recover_bet": "0.5"},
    {"balance_goal": "nan"},
    {"hybrid_recover_at": "-1"},
    {"hard_cap": "maybe"},
])
def test_mutation_refuses_bad_values(change):
    before = {key: getattr(wb.state, key) for key in change}
    with pytest.raises(ValueError):
        wb._apply_mutation(change)
    assert {key: getattr(wb.state, key) for key in change} == before


def test_mutation_warns_when_the_rules_cannot_work():
    msgs = wb._apply_mutation({"hard_cap": "on"})
    assert any(m.startswith("⚠") and "max_loss" in m for m in msgs)
    wb.state.max_loss = CAP
    msgs = wb._apply_mutation({"max_loss": "off"})
    assert any(m.startswith("⚠") and "max_loss" in m for m in msgs)


def test_mutation_adds_no_warning_when_nothing_is_switched_on():
    assert wb._apply_mutation({"max_loss": "0.5"}) == ["Max loss: 0.5"]


def test_changing_a_phase_setting_ends_the_server_cycle():
    s = wb.state
    s.game = "dice-auto"
    s._auto_uuid = "live"
    s.max_loss = CAP
    wb._apply_mutation({"hybrid_press_at": "0.00000025"})
    assert s._auto_uuid is None
    s._auto_uuid = "live"
    wb._apply_mutation({"hard_cap": "on"})
    assert s._auto_uuid == "live"


def test_session_rules_survive_a_config_round_trip(monkeypatch, tmp_path):
    s = wb.state
    monkeypatch.setattr(wb, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    s.proxy = None
    s.hard_cap = True
    s.restart_after_loss = True
    s.balance_goal = 1.5
    s.hybrid_recover_at = 100 * SAT
    s.hybrid_recover_strategy = "4"
    s.hybrid_recover_bet = 3.0
    s.hybrid_press_at = 25 * SAT
    s.hybrid_press_floor = 10 * SAT
    s.hybrid_press_strategy = "3"
    s.hybrid_press_bet = 2.0
    wanted = {key: getattr(s, key) for key in wb._SESSION_RULE_KEYS}
    wb.save_config()
    for key in wb._SESSION_RULE_KEYS:
        setattr(s, key, None)
    assert wb.load_config() is True
    assert {key: getattr(s, key) for key in wb._SESSION_RULE_KEYS} == wanted


def test_presets_and_config_show_carry_the_session_rules():
    shown = [field for field, _ in wb._CONFIG_SHOW_FIELDS]
    for key in wb._SESSION_RULE_KEYS:
        assert key in wb._PRESET_KEYS
        assert key in wb._TWEAK_PARAMS
        assert key in shown


def test_config_set_prints_the_warning(monkeypatch, tmp_path, capsys):
    s = wb.state
    monkeypatch.setattr(wb, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(wb, "PID_PATH", str(tmp_path / "bot.pid"))
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.setattr(wb, "_signal_lane_daemons", lambda *_: 0)
    s.proxy = None
    s.hard_cap = True
    s.max_loss = CAP
    wb.save_config()
    wb.cmd_config_set(["max_loss=off"])
    out = capsys.readouterr().out
    assert "hard_cap needs max_loss" in out
    assert "Restart every lane to apply" not in out
    wb.cmd_config_set(["hard_cap=off"])
    assert "Restart every lane to apply: hard_cap" in capsys.readouterr().out


# ── start-up check ────────────────────────────────────────
def test_start_is_refused_with_rules_that_cannot_work(capsys):
    wb.state.hard_cap = True
    with pytest.raises(SystemExit) as stop:
        wb._exit_on_session_rule_errors()
    assert stop.value.code == 2
    out = capsys.readouterr().out
    assert "Cannot start" in out
    assert "hard_cap needs max_loss" in out


def test_start_goes_ahead_with_usable_rules():
    _bold_setup()
    wb._exit_on_session_rule_errors()


def _saved_config_that_cannot_work(monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.setattr(wb, "api_get_balances", lambda: pytest.fail("no call before the check"))
    wb.state.proxy = None
    wb.state.hard_cap = True
    wb.save_config()


def test_service_start_refusal_exits_cleanly_so_the_unit_does_not_retry(monkeypatch, tmp_path, capsys):
    _saved_config_that_cannot_work(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as stop:
        wb._load_and_connect(daemon=True)
    assert stop.value.code == 0
    assert "hard_cap needs max_loss" in capsys.readouterr().out


def test_interactive_resume_refusal_is_an_error_exit(monkeypatch, tmp_path):
    _saved_config_that_cannot_work(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as stop:
        wb._load_and_connect()
    assert stop.value.code == 2


# ── backtest ──────────────────────────────────────────────
def test_backtest_loop_reports_why_it_stopped():
    s = wb.state
    s.max_bets = 2
    assert wb.backtest_loop(_scripted_rolls(["win"] * 5), progress_every=0).startswith("Max bets reached")
    assert s.total_bets == 2


def test_backtest_loop_returns_empty_when_the_rolls_run_out():
    assert wb.backtest_loop(_scripted_rolls(["loss", "win"]), progress_every=0) == ""
    assert wb.state.total_bets == 2


def test_no_roll_is_lost_between_backtest_sessions():
    s = wb.state
    s.strategy_key = "1"
    s.max_bets = 2
    rolls = _scripted_rolls(["win", "loss", "loss", "win", "win"])
    wb.backtest_loop(rolls, progress_every=0)
    wb._backtest_new_session()
    wb.backtest_loop(rolls, progress_every=0)
    assert s.total_bets == 2
    assert s.profit == pytest.approx(0.0)
    assert next(rolls)["bet"]["state"] == "win"
    assert next(rolls, None) is None


def test_backtest_walks_base_recover_base():
    s = wb.state
    s.strategy_key = "1"
    s.base_bet = TARGET
    s.current_bet = TARGET
    s.max_profit = TARGET
    s.max_loss = 2000 * SAT
    s.hybrid_recover_at = 200 * SAT
    s.hybrid_recover_bet = 2.0
    seen = []
    reason = wb.backtest_loop(_scripted_rolls(["loss", "loss", "win", "win", "win"], seen),
                              progress_every=0)
    assert seen == [("base", pytest.approx(100 * SAT)), ("base", pytest.approx(100 * SAT)),
                    ("recover", pytest.approx(200 * SAT)), ("base", pytest.approx(100 * SAT))]
    assert reason.startswith("Profit target reached")
    assert s.hybrid_switches == 2


def test_backtest_sessions_add_up_and_repeat_with_the_same_seed():
    _bold_setup()
    first = wb._run_backtest_sessions(_seeded_rolls(7), 300, 1, 1.0)
    assert first["sessions"] == 300
    assert first["runs"] == {"played every session": 1}
    assert 0 < first["green"] < 300
    assert first["worst"] == pytest.approx(-CAP)
    assert first["won"] == pytest.approx(first["green"] * TARGET)
    assert first["net"] == pytest.approx(first["won"] + first["lost"])
    assert first["finals"][0] == pytest.approx(1.0 + first["net"])
    assert first["switches"] == 0
    second = wb._run_backtest_sessions(_seeded_rolls(7), 300, 1, 1.0)
    assert second == first


def test_backtest_keeps_going_after_a_loss_whatever_the_live_restart_settings():
    _bold_setup()
    wb.state.recurring = False
    totals = wb._run_backtest_sessions(_scripted_rolls(["loss"] * 4 + ["win"]), 2, 1, 1.0)
    assert totals["sessions"] == 2
    assert totals["green"] == 1


def test_backtest_runs_end_at_the_goal_or_the_floor():
    s = wb.state
    _bold_setup()
    s.balance_goal = 1.0 + 20 * TARGET
    s.stop_on_balance = 1.0 - 4 * CAP
    totals = wb._run_backtest_sessions(_seeded_rolls(11), None, 40, 1.0)
    assert sum(totals["runs"].values()) == 40
    assert set(totals["runs"]) <= {"reached the balance goal", "at the balance floor"}
    assert len(totals["finals"]) == 40
    assert totals["runs"].get("reached the balance goal", 0) > 0


def test_backtest_run_ends_when_the_next_bet_cannot_be_paid():
    s = wb.state
    s.max_profit = TARGET
    totals = wb._run_backtest_sessions(_scripted_rolls(["loss"] * 9), None, 1, 400 * SAT)
    assert totals["runs"] == {"balance too low for the next bet": 1}
    assert totals["bets"] == 2
    assert s.current_balance == pytest.approx(100 * SAT)


def test_backtest_stops_when_the_rolls_run_out():
    _bold_setup()
    totals = wb._run_backtest_sessions(_scripted_rolls(["win", "win", "loss"]), None, 3, 1.0)
    assert totals["runs"] == {"the rolls ran out": 1}
    assert totals["sessions"] == 2
    assert totals["bets"] == 3


def test_backtest_table_prints_the_numbers(capsys):
    _bold_setup()
    totals = wb._run_backtest_sessions(_seeded_rolls(3), 200, 1, 1.0)
    wb._print_backtest_table(totals, 1.0)
    out = capsys.readouterr().out
    assert "Green sessions" in out
    assert "per 100 wagered" in out
    assert f"{-0.01 * totals['wagered']:+.8f}  (-1.00 per 100 wagered)" in out
    assert "Runs ended: played every session" in out
    assert "recurring is off" in out


def _backtest_args(**over):
    args = dict(preset=None, replay_session=None, simulate=None, seed=1,
                sessions=None, runs=None, balance=None)
    args.update(over)
    return argparse.Namespace(**args)


def _saved_bold_config(monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(wb, "DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.setattr(wb.console, "clear", lambda *_a, **_k: None)
    wb.state.proxy = None
    _bold_setup()
    wb.save_config()


def test_backtest_command_plays_sessions_from_the_saved_config(monkeypatch, tmp_path, capsys):
    _saved_bold_config(monkeypatch, tmp_path)
    wb._run_backtest(_backtest_args(sessions=150))
    out = capsys.readouterr().out
    assert "150 in 1 run(s)" in out
    assert "Green sessions" in out


def test_backtest_command_without_the_new_flags_is_one_session(monkeypatch, tmp_path, capsys):
    _saved_bold_config(monkeypatch, tmp_path)
    wb._run_backtest(_backtest_args(simulate=50))
    out = capsys.readouterr().out
    assert "Backtest complete" in out
    assert "Green sessions" not in out


def test_backtest_command_replays_real_rolls_across_sessions(monkeypatch, tmp_path, capsys):
    _saved_bold_config(monkeypatch, tmp_path)
    wb.init_db()
    conn = sqlite3.connect(wb.DB_PATH)
    conn.execute("INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, "
                 "multiplier, start_balance) VALUES (1, '2026-01-01T00:00:00', 'usdt', 'dice', "
                 "'Flat Bet', 0.00000001, 2.0, 1.0)")
    for i, outcome in enumerate(["win", "loss", "win", "loss", "loss", "win"], 1):
        conn.execute("INSERT INTO bets (id, session_id, timestamp, amount, multiplier, rule, bet_value, "
                     "result_value, state, profit, balance_after) VALUES (?, 1, ?, 0.00000001, 2.0, "
                     "'over', 50.49, 50.0, ?, 0.0, 1.0)", (i, f"2026-01-01T00:00:{i:02d}Z", outcome))
    conn.commit()
    conn.close()
    wb._run_backtest(_backtest_args(replay_session=1, sessions=10, runs=3))
    out = capsys.readouterr().out
    assert "running it once" in out
    assert "3 in 1 run(s)" in out
    assert "Runs ended: the rolls ran out" in out
    assert wb.state.total_bets == 0


@pytest.mark.parametrize("over", [dict(sessions=0), dict(runs=0), dict(sessions=5, balance=0.0),
                                  dict(sessions=5, balance=float("nan"))])
def test_backtest_command_refuses_bad_flags(monkeypatch, tmp_path, over):
    _saved_bold_config(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as stop:
        wb._run_backtest(_backtest_args(**over))
    assert stop.value.code == 2


def test_backtest_command_refuses_rules_that_cannot_work(monkeypatch, tmp_path, capsys):
    _saved_bold_config(monkeypatch, tmp_path)
    wb.state.max_loss = None
    wb.save_config()
    with pytest.raises(SystemExit) as stop:
        wb._run_backtest(_backtest_args(sessions=5))
    assert stop.value.code == 2
    assert "hard_cap needs max_loss" in capsys.readouterr().out


def test_version():
    assert wb.VERSION == "2.56.0"


# ── the switching is visible in the live log ──────────────
def test_phase_switch_is_logged_where_the_owner_can_see_it(monkeypatch, caplog):
    s = wb.state
    monkeypatch.setattr(wb, "api_auto_stop", lambda: None)
    s.strategy_key = "1"
    s.base_bet = SAT
    s.session_id = 152
    s.hybrid_recover_at = 100 * SAT
    s.hybrid_recover_bet = 25.0
    s.profit = -100 * SAT
    with caplog.at_level("WARNING", logger="wolfbet"):
        assert wb._hybrid_step() is True
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("PHASE")]
    assert lines == ["PHASE session 152: base (Flat Bet) -> recover (Martingale) "
                     "at profit -0.00000100, next bet 0.00000025"]


def test_every_session_end_is_logged_before_the_restart(monkeypatch, caplog):
    s = wb.state
    _quiet_loop(monkeypatch)
    _loss_restart_setup()
    s.profit = 0.0
    s.stop_on_balance = 1.0 - 5000 * SAT
    s.balance_goal = 1.0 + 150 * SAT
    _script_bets(monkeypatch, ["loss"] * 4 + ["win"] * 17 + ["loss"])
    with caplog.at_level("WARNING", logger="wolfbet"):
        wb.betting_loop()
    ends = [r.getMessage() for r in caplog.records if r.getMessage().startswith("SESSION END")]
    assert len(ends) == 18
    assert ends[0].startswith("SESSION END 1: Max loss hit: -0.00001500 after 4 bets, 0 phase switches")
    assert all(", 0 phase switches," in line for line in ends)
    assert ends[1].startswith("SESSION END 2: Profit target reached")
    assert ends[-1].startswith("SESSION END 18: Balance goal reached")


def test_backtest_writes_nothing_to_the_live_log(monkeypatch, tmp_path, caplog):
    _saved_bold_config(monkeypatch, tmp_path)
    wb.state.hybrid_press_at = TARGET / 4
    wb.state.base_bet = TARGET / 4
    wb.state.max_loss = 1575 * SAT
    wb.state.win_mult = 2.0
    wb.state.hybrid_press_strategy = "3"
    wb.save_config()
    level = wb.logger.level
    with caplog.at_level("WARNING", logger="wolfbet"):
        wb._run_backtest(_backtest_args(sessions=200))
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert wb.logger.level == level


def test_switch_count_starts_again_in_every_session(monkeypatch):
    s = wb.state
    _quiet_loop(monkeypatch)
    s.hybrid_switches = 23
    wb._reset_session_for_recurring(0)
    assert s.hybrid_switches == 0


def test_phase_line_never_prints_minus_zero(monkeypatch, caplog):
    s = wb.state
    monkeypatch.setattr(wb, "api_auto_stop", lambda: None)
    s.hybrid_recover_at = 100 * SAT
    s.hybrid_phase = "recover"
    s.profit = -1e-17
    with caplog.at_level("WARNING", logger="wolfbet"):
        wb._hybrid_step()
    assert "at profit +0.00000000" in caplog.records[-1].getMessage()

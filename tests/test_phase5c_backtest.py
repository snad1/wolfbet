"""Phase 5c tests — backtest / replay pure-function helpers.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5c_backtest.py -q
"""

import os
import random
import sqlite3
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import pytest  # noqa: E402
import wolfbet as wb  # noqa: E402


def _reset_state():
    s = wb.state
    s.currency = "usdt"
    s.game = "dice"
    s.rule = "over"
    s.multiplier = 2.0
    s.bet_value = 49.5
    s.base_bet = 0.0001
    s.current_bet = 0.0001
    s.current_balance = 1.0
    s.start_balance = 1.0
    s.highest_balance = 1.0
    s.lowest_balance = 1.0
    s.strategy = "Martingale"
    s.strategy_key = "2"
    s.loss_mult = 2.0
    s.win_mult = 1.0
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.wagered = 0.0
    s.current_streak = 0
    s.max_win_streak = 0
    s.max_loss_streak = 0
    s.highest_bet = 0.0
    s.highest_win = 0.0
    s.biggest_loss = 0.0
    s.recurring = False
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.stop_on_balance = None
    s.milestone_bets = 100
    s.milestone_wins = 0
    s.milestone_losses = 0
    s.milestone_profit = 0.0
    s.streak_delay_loss = (0, 0.0)
    s.streak_delay_win = (0, 0.0)
    s.streak_delay_bets = (0, 0.0)
    s.streakbet_loss = (0, 1.0)
    s.basebet_pct = 0.0
    s.custom_rules = []
    s.custom_rules_text = ""
    s.running = True
    s.paused = False


@contextmanager
def _tmp_db_path_with_bets():
    """Set up a temp DB with a session and 3 bets (2 wins, 1 loss)."""
    original = wb.DB_PATH
    fd, path = tempfile.mkstemp(prefix="wb_bt_", suffix=".db")
    os.close(fd)
    os.remove(path)
    wb.DB_PATH = path
    try:
        wb.init_db()
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, multiplier, start_balance) "
            "VALUES (1, '2026-07-17T10:00:00', 'usdt', 'dice', 'Flat', 0.0001, 2.0, 1.0)"
        )
        for i, (state_val, roll) in enumerate([("win", 60.0), ("loss", 40.0), ("win", 55.0)], 1):
            ts = f"2026-07-17T10:00:{i:02d}Z"
            conn.execute(
                "INSERT INTO bets (id, session_id, timestamp, amount, multiplier, rule, "
                "bet_value, result_value, state, profit, balance_after) "
                "VALUES (?, 1, ?, 0.0001, 2.0, 'over', 49.5, ?, ?, 0.0, 1.0)",
                (i, ts, roll, state_val),
            )
        conn.commit()
        conn.close()
        yield path
    finally:
        wb.DB_PATH = original
        if os.path.exists(path):
            os.remove(path)


# ── _simulate_bet_outcome ─────────────────────────────────
def test_simulate_returns_bet_shape():
    _reset_state()
    rng = random.Random(42)
    result = wb._simulate_bet_outcome(2.0, "over", 0.001, 1.0, rng)
    assert "bet" in result and "userBalance" in result
    assert result["bet"]["state"] in ("win", "loss")
    assert "amount" in result["bet"] and "profit" in result["bet"]
    assert 0.0 <= result["bet"]["result_value"] < 100.0


def test_simulate_deterministic_with_seed():
    _reset_state()
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    r1 = wb._simulate_bet_outcome(2.0, "over", 0.001, 1.0, rng1)
    r2 = wb._simulate_bet_outcome(2.0, "over", 0.001, 1.0, rng2)
    assert r1["bet"]["state"] == r2["bet"]["state"]
    assert r1["bet"]["result_value"] == r2["bet"]["result_value"]


def test_simulate_win_probability_over_many_bets():
    """For multiplier 2.0 (over → win_chance ≈ 49.5%), 10_000 simulated bets should
    land in a ~48-51% win range."""
    _reset_state()
    rng = random.Random(42)
    wins = sum(1 for _ in range(10_000)
               if wb._simulate_bet_outcome(2.0, "over", 0.001, 1.0, rng)["bet"]["state"] == "win")
    assert 4700 <= wins <= 5200  # ~49.5% ± 3%


def test_simulate_higher_multiplier_lower_win_rate():
    """multiplier 10.0 → win_chance ≈ 9.9% → far fewer wins."""
    _reset_state()
    rng = random.Random(42)
    wins = sum(1 for _ in range(5000)
               if wb._simulate_bet_outcome(10.0, "over", 0.001, 1.0, rng)["bet"]["state"] == "win")
    assert 400 <= wins <= 600  # ~9.9% × 5000 = ~495


def test_simulate_profit_matches_bet_size():
    _reset_state()
    rng = random.Random(999)
    result = wb._simulate_bet_outcome(2.0, "over", 0.001, 1.0, rng)
    if result["bet"]["state"] == "win":
        assert abs(result["bet"]["profit"] - 0.001) < 1e-9  # bet * (mult - 1) = 0.001
    else:
        assert result["bet"]["profit"] == -0.001


# ── _replay_bets_from_db ──────────────────────────────────
def test_replay_yields_all_bets():
    _reset_state()
    with _tmp_db_path_with_bets():
        results = list(wb._replay_bets_from_db(1, lambda: 0.0001))
        assert len(results) == 3
        assert results[0]["bet"]["state"] == "win"
        assert results[1]["bet"]["state"] == "loss"
        assert results[2]["bet"]["state"] == "win"


def test_replay_missing_session_raises():
    _reset_state()
    with _tmp_db_path_with_bets():
        with pytest.raises(ValueError):
            list(wb._replay_bets_from_db(9999, lambda: 0.0001))


def test_replay_uses_current_bet_size():
    _reset_state()
    with _tmp_db_path_with_bets():
        # bet_amount_fn returns 0.005, not the historical 0.0001
        results = list(wb._replay_bets_from_db(1, lambda: 0.005))
        for r in results:
            assert r["bet"]["amount"] == 0.005


# ── _apply_backtest_result ────────────────────────────────
def test_apply_win_result():
    _reset_state()
    result = {"bet": {"state": "win", "result_value": 60.0, "amount": 0.001, "profit": 0.001},
              "userBalance": {"amount": 1.001}}
    bet_state = wb._apply_backtest_result(result)
    assert bet_state == "win"
    assert wb.state.total_bets == 1
    assert wb.state.wins == 1
    assert wb.state.losses == 0
    assert wb.state.profit == 0.001
    assert wb.state.current_streak == 1


def test_apply_loss_result():
    _reset_state()
    result = {"bet": {"state": "loss", "result_value": 40.0, "amount": 0.001, "profit": -0.001},
              "userBalance": {"amount": 0.999}}
    wb._apply_backtest_result(result)
    assert wb.state.losses == 1
    assert wb.state.profit == -0.001
    assert wb.state.current_streak == -1


def test_apply_mixed_sequence_tracks_streaks():
    _reset_state()
    sequence = [
        ("win",  0.001,  0.001, 1.001),
        ("win",  0.001,  0.001, 1.002),
        ("win",  0.001,  0.001, 1.003),
        ("loss", 0.001, -0.001, 1.002),
        ("loss", 0.001, -0.001, 1.001),
    ]
    for st, amt, pr, bal in sequence:
        wb._apply_backtest_result({
            "bet": {"state": st, "result_value": 50.0, "amount": amt, "profit": pr},
            "userBalance": {"amount": bal},
        })
    assert wb.state.wins == 3
    assert wb.state.losses == 2
    assert wb.state.max_win_streak == 3
    assert wb.state.max_loss_streak == 2
    assert wb.state.current_streak == -2


# ── backtest_loop end-to-end (integration-style) ──────────
def test_backtest_loop_runs_all_bets():
    _reset_state()
    rng = random.Random(42)
    source = (wb._simulate_bet_outcome(2.0, "over", 0.0001, 1.0, rng) for _ in range(50))
    wb.backtest_loop(source, progress_every=0)  # 0 disables progress prints
    assert wb.state.total_bets == 50
    assert wb.state.wins + wb.state.losses == 50


def test_backtest_loop_respects_max_bets_stop():
    _reset_state()
    wb.state.max_bets = 10  # stop condition
    rng = random.Random(42)
    source = (wb._simulate_bet_outcome(2.0, "over", 0.0001, 1.0, rng) for _ in range(100))
    wb.backtest_loop(source, progress_every=0)
    assert wb.state.total_bets == 10  # should_stop fires at 10


def test_backtest_deterministic_with_seed():
    """Two seeded backtests over the same config must produce identical final state."""
    _reset_state()
    rng1 = random.Random(42)
    source1 = (wb._simulate_bet_outcome(2.0, "over", 0.0001, 1.0, rng1) for _ in range(100))
    wb.backtest_loop(source1, progress_every=0)
    final1 = (wb.state.total_bets, wb.state.wins, wb.state.losses, round(wb.state.profit, 12))

    _reset_state()
    rng2 = random.Random(42)
    source2 = (wb._simulate_bet_outcome(2.0, "over", 0.0001, 1.0, rng2) for _ in range(100))
    wb.backtest_loop(source2, progress_every=0)
    final2 = (wb.state.total_bets, wb.state.wins, wb.state.losses, round(wb.state.profit, 12))

    assert final1 == final2

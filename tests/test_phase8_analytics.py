"""Phase 8 tests — lifetime analytics helpers (--analytics command).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase8_analytics.py -q
"""

import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


@contextmanager
def _tmp_db_path(populate: bool = True):
    """Swap wolfbet.DB_PATH for a fresh tempfile DB.

    Populates with three sessions across two games and two strategies when
    populate=True; leaves empty when populate=False.

    Sessions seeded:
      #1  dice        Martingale  bets=100  wins=55  losses=45  profit= 0.010  wagered=0.10
      #2  dice        Flat        bets= 50  wins=25  losses=25  profit=-0.005  wagered=0.05
      #3  limbo-auto  Martingale  bets=200  wins=45  losses=155 profit=-0.020  wagered=0.20
    """
    original = wb.DB_PATH
    fd, path = tempfile.mkstemp(prefix="wb_analytics_", suffix=".db")
    os.close(fd)
    os.remove(path)
    wb.DB_PATH = path
    try:
        wb.init_db()
        if populate:
            conn = sqlite3.connect(path)
            rows = [
                (1, "2026-07-17T10:00:00Z", "usdt", "dice",       "Martingale", 0.0001, 2.01, 1.0, 100,  55, 45,  0.010, 0.10),
                (2, "2026-07-17T11:00:00Z", "usdt", "dice",       "Flat",       0.0001, 2.0,  1.0,  50,  25, 25, -0.005, 0.05),
                (3, "2026-07-17T12:00:00Z", "usdt", "limbo-auto", "Martingale", 0.0001, 3.0,  1.0, 200,  45, 155, -0.020, 0.20),
            ]
            for r in rows:
                conn.execute(
                    "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, multiplier, "
                    "start_balance, total_bets, wins, losses, profit, wagered) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", r
                )
            conn.commit()
            conn.close()
        yield path
    finally:
        wb.DB_PATH = original
        if os.path.exists(path):
            os.remove(path)


# ── _analytics_lifetime ────────────────────────────────────
def test_lifetime_empty_db_returns_zeros():
    with _tmp_db_path(populate=False):
        d = wb._analytics_lifetime()
        assert d["sessions_total"] == 0
        assert d["bets_total"] == 0
        assert d["profit_total"] == 0
        assert d["win_rate_pct"] == 0.0
        assert d["sessions_in_profit"] == 0
        assert d["sessions_in_loss"] == 0


def test_lifetime_totals_match_hand_math():
    with _tmp_db_path():
        d = wb._analytics_lifetime()
        assert d["sessions_total"] == 3
        assert d["bets_total"] == 350           # 100 + 50 + 200
        assert d["wins_total"] == 125           #  55 + 25 +  45
        assert d["losses_total"] == 225         #  45 + 25 + 155
        assert abs(d["wagered_total"] - 0.35) < 1e-9
        assert abs(d["profit_total"] - (-0.015)) < 1e-9  # 0.010 -0.005 -0.020


def test_lifetime_win_rate_pct():
    with _tmp_db_path():
        d = wb._analytics_lifetime()
        # 125 / 350 = 35.714…%
        assert abs(d["win_rate_pct"] - 35.7142857) < 1e-3


def test_lifetime_sessions_in_profit_and_loss():
    with _tmp_db_path():
        d = wb._analytics_lifetime()
        assert d["sessions_in_profit"] == 1     # #1
        assert d["sessions_in_loss"] == 2       # #2, #3


def test_lifetime_best_and_worst_profit():
    with _tmp_db_path():
        d = wb._analytics_lifetime()
        assert abs(d["best_profit"]  - 0.010) < 1e-9
        assert abs(d["worst_profit"] - (-0.020)) < 1e-9


# ── _analytics_by_game ─────────────────────────────────────
def test_by_game_two_groups():
    with _tmp_db_path():
        rows = wb._analytics_by_game()
        games = {r["game"] for r in rows}
        assert games == {"dice", "limbo-auto"}


def test_by_game_ordered_by_profit_desc():
    with _tmp_db_path():
        rows = wb._analytics_by_game()
        # dice aggregate: profit = 0.010 + -0.005 = 0.005 (positive)
        # limbo-auto: profit = -0.020 (negative)
        assert rows[0]["game"] == "dice"
        assert rows[1]["game"] == "limbo-auto"


def test_by_game_aggregates_sessions_and_bets():
    with _tmp_db_path():
        rows = {r["game"]: r for r in wb._analytics_by_game()}
        assert rows["dice"]["sessions"] == 2
        assert rows["dice"]["bets"] == 150
        assert rows["limbo-auto"]["sessions"] == 1
        assert rows["limbo-auto"]["bets"] == 200


# ── _analytics_by_strategy ─────────────────────────────────
def test_by_strategy_two_groups():
    with _tmp_db_path():
        rows = wb._analytics_by_strategy()
        strategies = {r["strategy"] for r in rows}
        assert strategies == {"Martingale", "Flat"}


def test_by_strategy_martingale_aggregates_two_sessions():
    with _tmp_db_path():
        rows = {r["strategy"]: r for r in wb._analytics_by_strategy()}
        assert rows["Martingale"]["sessions"] == 2       # #1 + #3
        assert rows["Martingale"]["bets"] == 300         # 100 + 200
        assert abs(rows["Martingale"]["profit"] - (-0.010)) < 1e-9  # 0.010 + -0.020


# ── _analytics_top_sessions ────────────────────────────────
def test_top_sessions_profit_desc_first_is_best():
    with _tmp_db_path():
        rows = wb._analytics_top_sessions("profit_desc", 5)
        assert rows[0]["id"] == 1
        assert abs(rows[0]["profit"] - 0.010) < 1e-9


def test_top_sessions_profit_asc_first_is_worst():
    with _tmp_db_path():
        rows = wb._analytics_top_sessions("profit_asc", 5)
        assert rows[0]["id"] == 3
        assert abs(rows[0]["profit"] - (-0.020)) < 1e-9


def test_top_sessions_bets_desc_first_has_most_bets():
    with _tmp_db_path():
        rows = wb._analytics_top_sessions("bets_desc", 5)
        assert rows[0]["id"] == 3
        assert rows[0]["bets"] == 200


def test_top_sessions_limit_respected():
    with _tmp_db_path():
        rows = wb._analytics_top_sessions("profit_desc", 2)
        assert len(rows) == 2


def test_top_sessions_unknown_order_defaults_to_profit_desc():
    with _tmp_db_path():
        rows = wb._analytics_top_sessions("bogus", 5)
        assert rows[0]["id"] == 1  # matches profit_desc


def test_top_sessions_empty_db_returns_empty_list():
    with _tmp_db_path(populate=False):
        rows = wb._analytics_top_sessions("profit_desc", 5)
        assert rows == []


# ── cmd_analytics smoke — no crash paths ───────────────────
def test_cmd_analytics_empty_db_does_not_crash(capsys):
    with _tmp_db_path(populate=False):
        wb.cmd_analytics()  # should print the "no sessions" line and return cleanly


def test_cmd_analytics_populated_does_not_crash(capsys):
    with _tmp_db_path():
        wb.cmd_analytics()  # should render the full report

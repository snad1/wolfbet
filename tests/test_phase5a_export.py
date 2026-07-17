"""Phase 5a tests — session read + CSV/JSON export + summary renderer.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5a_export.py -q
"""

import csv
import json
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

import pytest  # noqa: E402
import wolfbet as wb  # noqa: E402


@contextmanager
def _tmp_db_path():
    """Swap wolfbet.DB_PATH for a fresh tempfile DB. Seeds a session + 3 bets."""
    original = wb.DB_PATH
    fd, path = tempfile.mkstemp(prefix="wb_test_", suffix=".db")
    os.close(fd)
    os.remove(path)
    wb.DB_PATH = path
    try:
        wb.init_db()
        # Seed one session + three bets
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, multiplier, start_balance) "
            "VALUES (1, '2026-07-17T10:00:00', 'usdt', 'dice', 'Martingale', 0.0001, 2.01, 10.0)"
        )
        for i, (state_val, profit) in enumerate([("win", 0.0001), ("loss", -0.0001), ("win", 0.0002)], 1):
            ts = f"2026-07-17T10:00:{i:02d}Z"
            conn.execute(
                "INSERT INTO bets (id, session_id, timestamp, amount, multiplier, rule, "
                "bet_value, result_value, state, profit, balance_after) "
                "VALUES (?, 1, ?, 0.0001, 2.01, 'over', 49.5, ?, ?, ?, 10.0)",
                (i, ts, 50.0 + i, state_val, profit),
            )
        conn.commit()
        conn.close()
        yield path
    finally:
        wb.DB_PATH = original
        if os.path.exists(path):
            os.remove(path)


@contextmanager
def _tmp_out_path(suffix=".csv"):
    fd, path = tempfile.mkstemp(prefix="wb_out_", suffix=suffix)
    os.close(fd)
    os.remove(path)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


# ── _read_session / _read_bets ────────────────────────────
def test_read_session_returns_dict():
    with _tmp_db_path():
        session = wb._read_session(1)
        assert session["id"] == 1
        assert session["currency"] == "usdt"
        assert session["strategy"] == "Martingale"


def test_read_session_missing_returns_empty():
    with _tmp_db_path():
        assert wb._read_session(999) == {}


def test_read_bets_returns_ordered_list():
    with _tmp_db_path():
        bets = wb._read_bets(1)
        assert len(bets) == 3
        assert bets[0]["id"] == 1
        assert bets[2]["id"] == 3
        assert bets[0]["state"] == "win"
        assert bets[1]["state"] == "loss"


def test_read_bets_missing_session_returns_empty_list():
    with _tmp_db_path():
        assert wb._read_bets(999) == []


def test_latest_session_id():
    with _tmp_db_path():
        assert wb._latest_session_id() == 1


# ── Format detection ──────────────────────────────────────
def test_export_csv_lowercase():
    with _tmp_db_path(), _tmp_out_path(".csv") as out:
        n, fmt = wb._export_session(1, out)
        assert n == 3
        assert fmt == "csv"


def test_export_csv_uppercase():
    with _tmp_db_path(), _tmp_out_path(".CSV") as out:
        _, fmt = wb._export_session(1, out)
        assert fmt == "csv"


def test_export_json_extension():
    with _tmp_db_path(), _tmp_out_path(".json") as out:
        n, fmt = wb._export_session(1, out)
        assert n == 3
        assert fmt == "json"


def test_export_unknown_extension_defaults_to_csv():
    with _tmp_db_path(), _tmp_out_path(".dat") as out:
        _, fmt = wb._export_session(1, out)
        assert fmt == "csv"


# ── Export payload shape ─────────────────────────────────
def test_export_csv_payload():
    with _tmp_db_path(), _tmp_out_path(".csv") as out:
        wb._export_session(1, out)
        with open(out) as f:
            content = f.read()
        # Session metadata as `#`-comment rows
        assert "# id: 1" in content
        assert "# currency: usdt" in content
        assert "# strategy: Martingale" in content
        # Bet rows should include the three ids
        assert content.count("\n") >= 3  # at least the three bet rows
        # Re-parse: skip comment rows and confirm CSV integrity
        rows = [line for line in content.splitlines() if not line.startswith("#") and line.strip()]
        # rows[0] is header
        reader = csv.reader(rows)
        header = next(reader)
        assert "id" in header
        assert "state" in header
        data = list(reader)
        assert len(data) == 3


def test_export_json_payload():
    with _tmp_db_path(), _tmp_out_path(".json") as out:
        wb._export_session(1, out)
        with open(out) as f:
            payload = json.load(f)
        assert "session" in payload and "bets" in payload
        assert payload["session"]["id"] == 1
        assert payload["session"]["currency"] == "usdt"
        assert len(payload["bets"]) == 3
        assert payload["bets"][0]["state"] == "win"


def test_export_file_perms_are_restrictive():
    with _tmp_db_path(), _tmp_out_path(".csv") as out:
        wb._export_session(1, out)
        mode = os.stat(out).st_mode & 0o777
        assert mode == 0o600


# ── Missing session ────────────────────────────────────────
def test_export_missing_session_raises():
    with _tmp_db_path(), _tmp_out_path(".csv") as out:
        with pytest.raises(ValueError):
            wb._export_session(999, out)


# ── format_session_summary — no-crash + shape ─────────────
def _reset_state_for_summary():
    s = wb.state
    s.session_id = 42
    s.session_start = 0
    s.currency = "usdt"
    s.game = "dice"
    s.rule = "over"
    s.multiplier = 2.01
    s.strategy = "Flat"
    s.strategy_key = "1"
    s.base_bet = 0.0001
    s.bet_delay = 0.0
    s.recurring = False
    s.custom_rules = []
    s.total_bets = 0
    s.wins = 0
    s.losses = 0
    s.profit = 0.0
    s.wagered = 0.0
    s.current_balance = 10.0
    s.start_balance = 10.0
    s.highest_balance = 10.0
    s.lowest_balance = 10.0
    s.highest_bet = 0.0
    s.highest_win = 0.0
    s.biggest_loss = 0.0
    s.current_streak = 0
    s.max_win_streak = 0
    s.max_loss_streak = 0
    s.bets_per_second = 0.0
    s.bets_per_minute = 0.0


def test_summary_zero_bets_no_crash():
    _reset_state_for_summary()
    g = wb.format_session_summary("Session ended")
    # Should return a Group without raising a divide-by-zero
    assert g is not None


def test_summary_with_activity_no_crash():
    _reset_state_for_summary()
    wb.state.total_bets = 100
    wb.state.wins = 55
    wb.state.losses = 45
    wb.state.profit = 0.005
    wb.state.wagered = 0.02
    wb.state.current_balance = 10.005
    wb.state.highest_balance = 10.007
    wb.state.lowest_balance = 9.99
    wb.state.highest_win = 0.0003
    wb.state.biggest_loss = 0.0002
    wb.state.max_win_streak = 8
    wb.state.max_loss_streak = 5
    g = wb.format_session_summary("Profit target reached: +0.005")
    assert g is not None

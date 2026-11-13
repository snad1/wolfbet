"""Phase 9c — v2.33.0: --since/--until with a time of day + avg win / avg loss per bet.

started_at is stored as ISO with a 'T'. A bound typed with a space
("2026-11-13 14:00") compared wrong as a string (' ' < 'T'), so the window
matched every session of that day. Stats now also show the average win per
winning bet and average loss per losing bet instead of Best Win / Worst Loss.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase9c_stats_window_avgs.py -q
"""

import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


# ── window bound normalisation ────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("2026-11-13", "2026-11-13"),
    ("2026-11-13 14:00", "2026-11-13T14:00"),
    ("2026-11-13 14:00:05", "2026-11-13T14:00:05"),
    ("  2026-11-13 14:00:05.5 ", "2026-11-13T14:00:05.5"),
    ("2026-11-13T14:00", "2026-11-13T14:00"),
])
def test_normalize_bound(raw, expected):
    assert wb._normalize_window_bound(raw) == expected


@pytest.mark.parametrize("raw", ["yesterday", "2026-13-01", "14:00", "2026-11-13 25:00"])
def test_normalize_bound_rejects_garbage(raw):
    with pytest.raises(ValueError):
        wb._normalize_window_bound(raw)


def test_window_params_use_t_form():
    _, params, label = wb._stats_date_window(since="2026-11-13 14:00",
                                             until="2026-11-13 15:30:10")
    assert params == ["2026-11-13T14:00", "2026-11-13T15:30:10"]
    assert "2026-11-13 14:00" in label


# ── seeded DB ─────────────────────────────────────────────
def _seed(monkeypatch, tmp_path, sessions, bets=()):
    db = str(tmp_path / "stats.db")
    monkeypatch.setattr(wb, "DB_PATH", db)
    from core.database import init_db, migrate_db
    init_db(db)
    migrate_db(db)
    conn = sqlite3.connect(db)
    for s in sessions:
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, "
            "multiplier, total_bets, wins, losses, profit, wagered, win_total, loss_total) "
            "VALUES (:id,:started,'usdt','dice-auto','Martingale',0.0001,2.0,"
            ":bets,:wins,:losses,:profit,:wagered,:wt,:lt)", s)
    for b in bets:
        conn.execute(
            "INSERT INTO bets (session_id, timestamp, amount, state, profit) VALUES (?,?,?,?,?)", b)
    conn.commit()
    conn.close()
    return db


def _sess(i, started, wins=2, losses=3, wt=0.0003, lt=0.0007):
    return {"id": i, "started": started, "bets": wins + losses, "wins": wins,
            "losses": losses, "profit": wt - lt, "wagered": wt + lt, "wt": wt, "lt": lt}


def test_stats_time_window_excludes_earlier_same_day(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, [
        _sess(1, "2026-11-13T11:35:20.752951"),
        _sess(2, "2026-11-13T14:00:05.000001"),
        _sess(3, "2026-11-13T23:59:59.500000"),
    ])
    wb.cmd_stats(since="2026-11-13 14:00")
    out = capsys.readouterr().out
    assert "Session #1" not in out
    assert "Session #2" in out and "Session #3" in out

    wb.cmd_stats(since="2026-11-13 14:00:06")
    out = capsys.readouterr().out
    assert "Session #2" not in out and "Session #3" in out

    wb.cmd_stats(until="2026-11-13")  # bare date → whole day
    out = capsys.readouterr().out
    assert "Session #3" in out


def test_stats_invalid_window_prints_error(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, [_sess(1, "2026-11-13T11:35:20")])
    wb.cmd_stats(since="tomorrow")
    out = capsys.readouterr().out
    assert "Invalid --since/--until" in out
    assert "Session #1" not in out


def test_analytics_invalid_window_prints_error(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, [_sess(1, "2026-11-13T11:35:20")])
    wb.cmd_analytics(since="2026-11-13 99:00")
    assert "Invalid --since/--until" in capsys.readouterr().out


# ── avg win / avg loss ────────────────────────────────────
def test_stats_shows_avg_win_loss(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, [
        _sess(1, "2026-11-13T10:00:00", wins=2, losses=3, wt=0.0004, lt=0.0009),
        _sess(2, "2026-11-13T11:00:00", wins=2, losses=1, wt=0.0002, lt=0.0001),
    ])
    wb.cmd_stats()
    out = capsys.readouterr().out
    assert "Best Win" not in out and "Worst Loss" not in out
    assert "+0.00020000" in out and "-0.00030000" in out   # session 1: 0.0004/2, 0.0009/3
    assert "+0.00010000" in out and "-0.00010000" in out   # session 2
    # totals: (0.0004+0.0002)/4 = 0.00015 ; (0.0009+0.0001)/4 = 0.00025
    assert "+0.00015000" in out and "-0.00025000" in out


def test_avg_falls_back_to_bet_rows(monkeypatch, tmp_path):
    db = _seed(monkeypatch, tmp_path,
               [_sess(7, "2026-11-13T10:00:00", wins=2, losses=1, wt=0, lt=0),
                _sess(8, "2026-11-13T11:00:00", wins=1, losses=1, wt=0, lt=0)],
               bets=[(7, "t", 0.0001, "win", 0.0001), (7, "t", 0.0003, "win", 0.0003),
                     (7, "t", 0.0002, "loss", -0.0002)])
    conn = sqlite3.connect(db)
    wl = wb._win_loss_totals(conn)
    conn.close()
    assert wl[7] == pytest.approx((0.0004, 0.0002, 2, 1))
    assert wl[8] is None  # bet rows purged → unknown
    assert wb._fmt_avg_wl(None, 1, True) == "—"


def test_session_view_shows_avg(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, [_sess(5, "2026-11-13T10:00:00", wins=4, losses=2,
                                        wt=0.0008, lt=0.0006)])
    wb.cmd_session_bets(5)
    out = capsys.readouterr().out
    assert "Avg Win:" in out and "+0.00020000" in out
    assert "Avg Loss:" in out and "-0.00030000" in out
    assert "Best Win" not in out


# ── accumulation in the betting loop ──────────────────────
def test_serial_loop_accumulates_totals(monkeypatch):
    s = wb.state
    s.game = "dice"
    s.strategy_key = "1"
    s.base_bet = s.current_bet = 0.0001
    s.total_bets = s.wins = s.losses = 0
    s.profit = s.wagered = 0.0
    s.win_total = s.loss_total = 0.0
    s.current_streak = 0
    s.running = True
    s.paused = False
    s.rate_wait = False
    s._insufficient_balance = False
    for attr in ("max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
                 "streak_reset_losses"):
        setattr(s, attr, None)
    s.basebet_pct = 0
    s.bet_delay = 0
    seq = [("win", 0.0001, 0.0001), ("loss", 0.0002, -0.0002), ("loss", 0.0004, -0.0004),
           ("win", 0.0008, 0.0008)]

    def _bet(_amt):
        if not seq:
            s.running = False
            return None
        st, amt, pnl = seq.pop(0)
        return {"bet": {"state": st, "amount": amt, "profit": pnl, "result_value": 50},
                "userBalance": {"amount": 1.0}}

    monkeypatch.setattr(wb, "api_place_bet", _bet)
    monkeypatch.setattr(wb, "_db_save_bet_dispatch", lambda *a, **k: None)
    monkeypatch.setattr(wb, "_db_save_session", lambda: None)
    monkeypatch.setattr(wb, "_get_streak_delay", lambda: 0)
    monkeypatch.setattr(wb.time, "sleep", lambda *_: None)
    wb.betting_loop()
    assert s.win_total == pytest.approx(0.0009)
    assert s.loss_total == pytest.approx(0.0006)

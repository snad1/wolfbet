"""Phase 9c — v2.33.x: --since/--until with a time of day + avg wins / losses per session.

started_at is stored as ISO with a 'T'. A bound typed with a space
("2026-11-13 14:00") compared wrong as a string (' ' < 'T'), so the window
matched every session of that day. Stats totals now also show the average
number of wins and losses per session instead of Best Win / Worst Loss.

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
    from core.database import init_db
    init_db(db)
    conn = sqlite3.connect(db)
    for s in sessions:
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, "
            "multiplier, total_bets, wins, losses, profit, wagered) "
            "VALUES (:id,:started,'usdt','dice-auto','Martingale',0.0001,2.0,"
            ":bets,:wins,:losses,:profit,:wagered)", s)
    for b in bets:
        conn.execute(
            "INSERT INTO bets (session_id, timestamp, amount, state, profit) VALUES (?,?,?,?,?)", b)
    conn.commit()
    conn.close()
    return db


def _sess(i, started, wins=2, losses=3):
    return {"id": i, "started": started, "bets": wins + losses, "wins": wins,
            "losses": losses, "profit": 0.0, "wagered": 0.001}


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


# ── avg wins / losses per session ─────────────────────────
def test_avg_wins_one_each(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, [_sess(i, f"2026-11-13T1{i}:00:00", wins=1, losses=2)
                                  for i in range(1, 4)])
    wb.cmd_stats()
    out = capsys.readouterr().out
    assert "Wins/Session:  Max 1  Avg 1.0" in out
    assert "Losses/Session: Max 2  Avg 2.0" in out


def test_avg_wins_losses_mixed_and_windowed(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, [
        _sess(1, "2026-11-13T09:00:00", wins=9, losses=9),
        _sess(2, "2026-11-13T14:00:00", wins=2, losses=3),
        _sess(3, "2026-11-13T15:00:00", wins=4, losses=1),
    ])
    wb.cmd_stats(since="2026-11-13 14:00")
    out = capsys.readouterr().out
    assert "Wins/Session:  Max 4  Avg 3.0" in out
    assert "Losses/Session: Max 3  Avg 2.0" in out
    for gone in ("Avg Win", "Avg Loss", "Best Win", "Worst Loss"):
        assert gone not in out


def test_session_view_has_no_amount_lines(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, [_sess(5, "2026-11-13T10:00:00", wins=4, losses=2)])
    wb.cmd_session_bets(5)
    out = capsys.readouterr().out
    assert "Session #5" in out
    for gone in ("Avg Win", "Avg Loss", "Best Win", "Worst Loss"):
        assert gone not in out

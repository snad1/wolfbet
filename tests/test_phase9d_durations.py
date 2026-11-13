"""Phase 9d — v2.34.0: range time span in stats/analytics + total run time in monitor.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase9d_durations.py -q
"""

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@pytest.mark.parametrize("sec,expected", [
    (0, "0s"),
    (45, "45s"),
    (62, "1m 2s"),
    (180, "3m 0s"),
    (3600, "1h 0m 0s"),
    (86405, "1d 0h 0m 5s"),
    (50 * 86400 + 7383, "1mo 20d 2h 3m 3s"),
    (None, "0s"),
    (-5, "0s"),
])
def test_fmt_duration(sec, expected):
    assert wb._fmt_duration(sec) == expected


def _seed(monkeypatch, tmp_path, rows):
    db = str(tmp_path / "span.db")
    monkeypatch.setattr(wb, "DB_PATH", db)
    from core.database import init_db
    init_db(db)
    conn = sqlite3.connect(db)
    for i, (started, ended) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at, currency, game, strategy, "
            "base_bet, multiplier, total_bets, wins, losses, profit, wagered) "
            "VALUES (?,?,?,'usdt','dice-auto','Martingale',0.0001,2.0,4,2,2,0.0,0.001)",
            (i, started, ended))
    conn.commit()
    conn.close()
    return db


ROWS = [
    ("2026-11-13T10:00:00.000000", "2026-11-13T10:00:30.000000"),  # 30s
    ("2026-11-13T11:00:00.000000", "2026-11-13T11:01:00.000000"),  # 60s
    ("2026-11-13T12:00:00.000000", "2026-11-13T12:02:05.000000"),  # 125s
]


def test_span_helper(monkeypatch, tmp_path):
    db = _seed(monkeypatch, tmp_path, ROWS)
    conn = sqlite3.connect(db)
    first, last, span, bet = wb._sessions_time_span(conn)
    conn.close()
    assert first == datetime(2026, 11, 13, 10, 0, 0)
    assert last == datetime(2026, 11, 13, 12, 2, 5)
    assert span == 2 * 3600 + 125
    assert bet == 215


def test_span_helper_window_and_running(monkeypatch, tmp_path):
    started = (datetime.now() - timedelta(seconds=100)).isoformat()
    db = _seed(monkeypatch, tmp_path, ROWS + [(started, None)])
    conn = sqlite3.connect(db)
    where, params, _ = wb._stats_date_window(since="2026-11-13 11:00")
    first, last, span, bet = wb._sessions_time_span(conn, where, params)
    conn.close()
    assert first == datetime(2026, 11, 13, 11, 0, 0)
    assert 285 - 1 <= bet <= 285 + 5      # 60 + 125 + ~100 running
    assert last >= datetime.now() - timedelta(seconds=5)


def test_span_helper_empty(monkeypatch, tmp_path):
    db = _seed(monkeypatch, tmp_path, [])
    conn = sqlite3.connect(db)
    assert wb._sessions_time_span(conn) == (None, None, 0, 0)
    conn.close()


def test_stats_totals_show_span(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, ROWS)
    wb.cmd_stats()
    out = capsys.readouterr().out
    assert "(2026-11-13 10:00:00 → 2026-11-13 12:02:05)" in out
    assert "Time Span:" in out and "2h 2m 5s" in out
    assert "Bet Time:" in out and "3m 35s" in out


def test_analytics_shows_span(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path, ROWS)
    wb.cmd_analytics()
    out = capsys.readouterr().out
    assert "Time span:" in out and "2h 2m 5s" in out
    assert "Bet time:" in out and "3m 35s" in out
    assert "Time range:" in out


# ── monitor / status run time ─────────────────────────────
def _monitor_dict(**extra):
    d = {"session_id": 7, "uptime_sec": 1, "currency": "usdt", "strategy": "Martingale"}
    d.update(extra)
    return d


def test_monitor_header_shows_run_time():
    screen = _ANSI.sub("", wb._build_monitor_screen(_monitor_dict(run_uptime_sec=7385), [], True))
    header = next(line for line in screen.splitlines() if "WolfBet v" in line)
    assert "#7  0:00:01  Run 2h 3m 5s" in header


def test_monitor_header_without_key_has_no_run():
    screen = _ANSI.sub("", wb._build_monitor_screen(_monitor_dict(), [], True))
    assert "  Run " not in screen


def test_state_dict_has_run_uptime():
    src = open(os.path.join(_WOLFBET_DIR, "wolfbet.py")).read()
    assert '"run_uptime_sec": int(time.time() - state.run_started_at)' in src
    assert "state.run_started_at = state.session_start" in src


def test_run_started_at_survives_recurring_reset():
    src = open(os.path.join(_WOLFBET_DIR, "wolfbet.py")).read()
    body = src[src.index("def _reset_session_for_recurring"):src.index("def _should_recurring_restart")]
    assert "run_started_at" not in body

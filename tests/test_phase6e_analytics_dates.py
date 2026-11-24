"""Phase 6e: date-range analytics: CLI --analytics --since/--until + TG /analytics dates (v2.31.0 / TG v1.19.0).

v2.30.9 shipped --stats --since/--until. This wave extends the same window to
analytics (CLI helpers + TG handler). Sibling bots get CLI stats + TG analytics
dates in their own repos.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase6e_analytics_dates.py -q
"""

import inspect
import os
import sqlite3
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from tg import handlers as tg_handlers, engine as tg_engine, bot as tg_bot  # noqa: E402
import tg  # noqa: E402


# ── VERSION ──────────────────────────────────────────────
def test_wolfbet_version():
    assert wb.VERSION == "2.61.1"


def test_tg_version():
    assert tg.VERSION == "1.26.1"


# ── Seeded temp DB ───────────────────────────────────────
def _seed_db(monkeypatch):
    fd, db = tempfile.mkstemp(prefix="wb_an_", suffix=".db")
    os.close(fd); os.remove(db)
    monkeypatch.setattr(wb, "DB_PATH", db)
    from core.database import init_db
    init_db(db)
    conn = sqlite3.connect(db)
    # 3 sessions: Aug 1 (dice, Flat, +0.10), Aug 10 (limbo, Martingale, -0.20),
    # Aug 15 (dice, Flat, +0.30).
    for i, (started, game, strat, profit) in enumerate([
        ("2026-08-01T10:00:00", "dice",  "Flat",      0.10),
        ("2026-08-10T12:00:00", "limbo", "Martingale", -0.20),
        ("2026-08-15T20:00:00", "dice",  "Flat",      0.30),
    ], 1):
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, "
            "base_bet, multiplier, total_bets, wins, losses, profit, wagered) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, started, "usdt", game, strat, 0.0001, 2.0,
             100 * i, 50 * i, 50 * i, profit, 0.01 * i))
    conn.commit(); conn.close()
    return db


# ── Helper-level: analytics honor the window ────────────
def test_lifetime_filtered(monkeypatch):
    db = _seed_db(monkeypatch)
    try:
        where, params, _ = wb._stats_date_window(since="2026-08-10")
        d = wb._analytics_lifetime(where, params)
        assert d["sessions_total"] == 2          # Aug 10 + Aug 15
        assert abs(d["profit_total"] - 0.10) < 1e-9   # -0.20 + 0.30
    finally:
        os.remove(db)


def test_by_game_filtered(monkeypatch):
    db = _seed_db(monkeypatch)
    try:
        where, params, _ = wb._stats_date_window(since="2026-08-02",
                                                 until="2026-08-15")
        rows = wb._analytics_by_game(where, params)
        # Window contains only the Aug 10 limbo + Aug 15 dice sessions.
        games = {r["game"] for r in rows}
        assert games == {"limbo", "dice"}
        dice = next(r for r in rows if r["game"] == "dice")
        assert dice["sessions"] == 1              # Aug 1 dice excluded
    finally:
        os.remove(db)


def test_by_strategy_filtered(monkeypatch):
    db = _seed_db(monkeypatch)
    try:
        where, params, _ = wb._stats_date_window(since="2026-08-11")
        rows = wb._analytics_by_strategy(where, params)
        # Only Aug 15 (Flat) remains.
        assert [r["strategy"] for r in rows] == ["Flat"]
    finally:
        os.remove(db)


def test_top_sessions_filtered(monkeypatch):
    db = _seed_db(monkeypatch)
    try:
        where, params, _ = wb._stats_date_window(since="2026-08-02")
        rows = wb._analytics_top_sessions("profit_desc", 5, where, params)
        # Aug 1 (+0.10) excluded; best remaining is Aug 15 (+0.30).
        assert rows[0]["id"] == 3 and abs(rows[0]["profit"] - 0.30) < 1e-9
        assert all(r["id"] != 1 for r in rows)
    finally:
        os.remove(db)


# ── CLI surface ─────────────────────────────────────────
def test_cmd_analytics_accepts_and_threads_dates(monkeypatch, tmp_path, capsys):
    db = _seed_db(monkeypatch)
    try:
        wb.cmd_analytics(since="2026-08-10")
        out = capsys.readouterr().out
        assert "Range analytics" in out
        assert "2 sessions" in out.replace(",", "")   # "2" not "3"
        assert "2026-08-10" in out
    finally:
        os.remove(db)


def test_cmd_analytics_no_filter_lifetime_label(monkeypatch, capsys):
    db = _seed_db(monkeypatch)
    try:
        wb.cmd_analytics()
        out = capsys.readouterr().out
        assert "Lifetime analytics" in out
        assert "3 sessions" in out.replace(",", "")
    finally:
        os.remove(db)


def test_dispatch_threads_args():
    src = inspect.getsource(wb)
    assert "cmd_analytics(since=args.since, until=args.until, lane=args.lane, as_json=args.json)" in src


def test_analytics_combines_with_since_functional():
    """Regression for the exclusive-group trap: --analytics + --since parses."""
    import subprocess
    env = dict(os.environ, APP_ENV="testing")
    env["HOME"] = tempfile.mkdtemp(prefix="wb_an_args_")
    p = subprocess.run(
        [sys.executable, os.path.join(_WOLFBET_DIR, "wolfbet.py"),
         "--analytics", "--since", "2026-08-14"],
        capture_output=True, text=True, timeout=30, env=env)
    combined = p.stdout + p.stderr
    assert "not allowed with argument" not in combined


# ── TG surface ──────────────────────────────────────────
def test_tg_analytics_parses_args_and_threads_where():
    src = inspect.getsource(tg_handlers.cmd_analytics)
    assert "context.args" in src
    assert "started_at >= ?" in src and "started_at <= ?" in src
    # All five queries share the window.
    assert src.count("{where}") >= 5
    # Bare until extended to end-of-day.
    assert 'T23:59:59' in src
    # Header labels the window.
    assert "Range" in src and "window_label" in src


def test_tg_analytics_usage_hint_on_three_args():
    src = inspect.getsource(tg_handlers.cmd_analytics)
    assert "Usage: `/analytics" in src


def test_tg_help_mentions_dates():
    src = inspect.getsource(tg_handlers.cmd_help)
    assert "/analytics [SINCE] [UNTIL]" in src


# ── wolfctl passthrough ─────────────────────────────────
def test_wolfctl_analytics_passes_args():
    with open(os.path.join(_WOLFBET_DIR, "wolfctl")) as f:
        src = f.read()
    assert '--analytics "$@"' in src

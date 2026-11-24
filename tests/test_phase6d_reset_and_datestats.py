"""Phase 6d: `wolfctl reset` (archive + fresh stats) + date-range `--stats` (v2.30.9 / wolfctl v3.9).

User asked for a hard statistics reset: clear all sessions/bets, keep config,
archive old data, and/or evaluate history by date range.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase6d_reset_and_datestats.py -q
"""

import inspect
import os
import re
import sqlite3
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402

_WOLFCTL_PATH = os.path.join(_WOLFBET_DIR, "wolfctl")


def _wolfctl_src() -> str:
    with open(_WOLFCTL_PATH) as f:
        return f.read()


# ── VERSION ──────────────────────────────────────────────
def test_wolfbet_version():
    assert wb.VERSION == "2.55.0"


def test_wolfctl_version_constant_3_9():
    src = _wolfctl_src()
    assert re.search(r"^WOLFCTL_VERSION=3\.24\b", src, re.MULTILINE)
    assert "Management Command v3.24" in src


# ── wolfctl reset: archive-first design ─────────────────
def test_cmd_reset_defined_and_dispatched():
    src = _wolfctl_src()
    assert re.search(r"^cmd_reset\(\)\s*\{", src, re.MULTILINE)
    assert re.search(r"^\s+reset\)\s+shift;\s*cmd_reset", src, re.MULTILINE)


def test_reset_advertised_in_usage():
    src = _wolfctl_src()
    assert "wolfctl reset" in src


def test_reset_archives_never_deletes():
    """mv (archive), never rm on the DB itself."""
    src = _wolfctl_src()
    reset_body = re.search(r"cmd_reset\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE).group(0)
    assert "mv \"$db\" \"$archive\"" in reset_body
    # rm only allowed for the stale live-state / run files, never the DB.
    rm_lines = [l for l in reset_body.splitlines() if re.search(r"\brm ", l)]
    assert all("wolfbet_autobot_live.json" in l or "wolfbet_run.json" in l for l in rm_lines), \
        f"rm must only target stale live-state, got: {rm_lines}"


def test_reset_clears_stale_live_state():
    src = _wolfctl_src()
    reset_body = re.search(r"cmd_reset\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE).group(0)
    assert "wolfbet_autobot_live.json" in reset_body


def test_reset_confirmation_with_counts_and_yes_bypass():
    src = _wolfctl_src()
    reset_body = re.search(r"cmd_reset\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE).group(0)
    assert 'confirm "Proceed?"' in reset_body and "[y/N]" in src
    assert "--yes" in reset_body
    assert "SELECT COUNT(*) FROM sessions" in reset_body


def test_reset_stops_daemon_if_running():
    src = _wolfctl_src()
    reset_body = re.search(r"cmd_reset\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE).group(0)
    assert "is-active" in reset_body
    assert 'systemctl --user stop "$SERVICE"' in reset_body


def test_reset_archive_name_is_timestamped():
    src = _wolfctl_src()
    reset_body = re.search(r"cmd_reset\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE).group(0)
    assert "date +%Y%m%d-%H%M%S" in reset_body


# ── wolfctl archives ────────────────────────────────────
def test_cmd_archives_defined_and_dispatched():
    src = _wolfctl_src()
    assert re.search(r"^cmd_archives\(\)\s*\{", src, re.MULTILINE)
    assert re.search(r"^\s+archives\)\s+cmd_archives", src, re.MULTILINE)


def test_archives_lists_glob_and_reports_span():
    src = _wolfctl_src()
    body = re.search(r"cmd_archives\(\)\s*\{.*?^\}", src, re.DOTALL | re.MULTILINE).group(0)
    assert ".wolfbet_autobot-*.db" in body
    assert "MIN(started_at)" in body and "MAX(started_at)" in body


# ── Python: date-window helper ──────────────────────────
def test_date_window_empty_when_no_args():
    clause, params, label = wb._stats_date_window()
    assert clause == "" and params == [] and label == ""


def test_date_window_since_only():
    clause, params, label = wb._stats_date_window(since="2026-08-01")
    assert "started_at >= ?" in clause
    assert params == ["2026-08-01"]
    assert "2026-08-01" in label


def test_date_window_bare_until_extends_end_of_day():
    _, params, _ = wb._stats_date_window(until="2026-08-15")
    assert params == ["2026-08-15T23:59:59.999999"]


def test_date_window_full_ts_until_untouched():
    _, params, _ = wb._stats_date_window(until="2026-08-15T06:00:00")
    assert params == ["2026-08-15T06:00:00"]


# ── Functional: filtered query against a seeded temp DB ─
def _seed_db(monkeypatch, tmp_path):
    fd, db = tempfile.mkstemp(prefix="wb_stats_", suffix=".db")
    os.close(fd); os.remove(db)
    monkeypatch.setattr(wb, "DB_PATH", db)
    from core.database import init_db
    init_db(db)
    conn = sqlite3.connect(db)
    for i, started in enumerate(
        ["2026-08-01T10:00:00", "2026-08-10T12:00:00", "2026-08-15T20:00:00"], 1
    ):
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, "
            "base_bet, multiplier, total_bets, wins, losses, profit, wagered) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, started, "usdt", "dice", "Flat", 0.0001, 2.0,
             100 * i, 50 * i, 50 * i, 0.001 * i, 0.01 * i))
    conn.commit(); conn.close()
    return db


def test_stats_since_filters_listing_and_totals(monkeypatch, tmp_path, capsys):
    db = _seed_db(monkeypatch, tmp_path)
    try:
        wb.cmd_stats(since="2026-08-10")
        out = capsys.readouterr().out
        # Sessions 2 + 3 are in window; session 1 (Aug 1) excluded.
        assert "Session #3" in out and "Session #2" in out
        assert "Session #1" not in out
        assert "Range Totals" in out
        assert "2026-08-10" in out
    finally:
        os.remove(db)


def test_stats_until_bare_date_inclusive_end_of_day(monkeypatch, tmp_path, capsys):
    """Session 3 started 2026-08-15T20:00: a bare --until 2026-08-15 must
    still include it (end-of-day extension)."""
    db = _seed_db(monkeypatch, tmp_path)
    try:
        wb.cmd_stats(until="2026-08-15")
        out = capsys.readouterr().out
        assert "Session #3" in out and "Session #1" in out
    finally:
        os.remove(db)


def test_stats_window_combined(monkeypatch, tmp_path, capsys):
    db = _seed_db(monkeypatch, tmp_path)
    try:
        wb.cmd_stats(since="2026-08-02", until="2026-08-15")
        out = capsys.readouterr().out
        assert "Session #2" in out and "Session #3" in out
        assert "Session #1" not in out
    finally:
        os.remove(db)


def test_stats_no_filter_unchanged(monkeypatch, tmp_path, capsys):
    """Regression: default path shows all sessions + All-Time label."""
    db = _seed_db(monkeypatch, tmp_path)
    try:
        wb.cmd_stats()
        out = capsys.readouterr().out
        assert "Session #1" in out and "Session #2" in out and "Session #3" in out
        assert "All-Time Totals" in out
        assert "last 20" in out
    finally:
        os.remove(db)


def test_stats_filtered_limit_bumps_to_50():
    src = inspect.getsource(wb._stats_data)
    assert "limit = 50" in src


# ── argparse surface ────────────────────────────────────
def test_argparse_has_since_until():
    src = inspect.getsource(wb)
    assert '"--since"' in src and '"--until"' in src
    assert "cmd_stats(since=args.since, until=args.until, lane=args.lane, as_json=args.json)" in src


def test_since_until_are_modifiers_not_commands():
    """v2.30.9 hotfix: `--stats --since DATE` was rejected because --since
    sat inside the mutually-exclusive command group. Modifiers must be added
    via parser.add_argument, not group.add_argument."""
    import re as _re
    src = inspect.getsource(wb)
    assert _re.search(r'parser\.add_argument\("--since"', src), \
        "--since must be on the standalone parser (was in the exclusive group)"
    assert _re.search(r'parser\.add_argument\("--until"', src)
    assert not _re.search(r'group\.add_argument\("--since"', src)
    assert not _re.search(r'group\.add_argument\("--until"', src)


def test_stats_with_since_parses_functional():
    """Subprocess smoke: the real CLI accepts --stats + --since together.
    (Parser is inline in main(); run the module with a stub HOME so it can
    never touch a real DB: we only care that argparse doesn't reject.)"""
    import subprocess
    env = dict(os.environ, APP_ENV="testing")
    env["HOME"] = tempfile.mkdtemp(prefix="wb_argparse_")
    p = subprocess.run(
        [sys.executable, os.path.join(_WOLFBET_DIR, "wolfbet.py"),
         "--stats", "--since", "2026-08-14"],
        capture_output=True, text=True, timeout=30, env=env)
    combined = p.stdout + p.stderr
    assert "not allowed with argument" not in combined, \
        f"argparse still rejects --stats + --since: {combined[:400]}"

"""--export and --backtest with --json: a verifiable export file and a backtest
result document, plus JSON progress lines on stderr for long backtests.

Snapshots in fixtures/cli_v1/snapshots were captured from the Rich output
before the change; without --json the output must stay the same.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase11j_export_backtest_json.py -q
"""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys

import pytest
from rich.console import Console

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402

_FIXTURES = os.path.join(_HERE, "fixtures", "cli_v1")
_SNAPSHOTS = os.path.join(_FIXTURES, "snapshots")

_CONFIG = {
    "currency": "usdt", "game": "dice", "rule": "over", "multiplier": 2.0, "bet_value": 50.49,
    "strategy": "Martingale", "strategy_key": "2", "base_bet": 0.00000001, "loss_mult": 2.0,
    "win_mult": 1.0, "max_profit": 0.0000001, "max_loss": 0.000002, "recurring": True,
}
_REPLAY = ["win", "loss", "loss", "win", "loss", "win", "win", "loss", "loss", "loss", "win"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Saved config, a DB with one session of bets, fresh state and a fixed-width console."""
    monkeypatch.chdir(tmp_path)
    with open("config.json", "w") as f:
        json.dump(_CONFIG, f)
    db = str(tmp_path / "wb.db")
    from core.database import init_db, migrate_db
    init_db(db)
    migrate_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sessions (id, started_at, ended_at, currency, game, strategy, base_bet, "
                 "multiplier, total_bets, wins, losses, profit, wagered, start_balance, end_balance) "
                 "VALUES (1, '2026-11-10T10:00:00', '2026-11-10T10:05:00', 'usdt', 'dice', "
                 "'Martingale', 0.00000001, 2.0, 11, 5, 6, 0.0, 0.0000003, 1.0, 1.0)")
    for i, outcome in enumerate(_REPLAY, 1):
        conn.execute("INSERT INTO bets (id, session_id, timestamp, amount, multiplier, rule, "
                     "bet_value, result_value, state, profit, balance_after) "
                     "VALUES (?, 1, ?, 0.00000001, 2.0, 'over', 50.49, ?, ?, 0.0, 1.0)",
                     (i, f"2026-11-10T10:00:{i:02d}", 60.0 if outcome == "win" else 20.0, outcome))
    conn.commit()
    conn.close()
    monkeypatch.setattr(wb, "CONFIG_PATH", "config.json")
    monkeypatch.setattr(wb, "DB_PATH", db)
    monkeypatch.setattr(wb, "state", wb.BotState())
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.setattr(wb.logger, "level", wb.logger.level)
    monkeypatch.delenv("WOLFBET_API_KEY", raising=False)
    buf = io.StringIO()
    monkeypatch.setattr(wb, "console", Console(file=buf, width=140, color_system=None,
                                               force_terminal=False, legacy_windows=False))
    return {"buf": buf, "tmp": str(tmp_path), "db": db}


def _bt_args(**over):
    args = dict(preset=None, replay_session=None, simulate=None, seed=1,
                sessions=None, runs=None, balance=None, json=False)
    args.update(over)
    return argparse.Namespace(**args)


_BACKTEST_CASES = {
    "backtest_single":   dict(simulate=400),
    "backtest_sessions": dict(simulate=20000, sessions=40),
    "backtest_runs":     dict(simulate=20000, sessions=10, runs=6, balance=0.00001),
    "backtest_replay":   dict(replay_session=1, sessions=3),
}


def _rich_backtest(env, case) -> str:
    code = 0
    try:
        wb._run_backtest(_bt_args(**_BACKTEST_CASES[case]))
    except SystemExit as e:
        code = e.code
    return env["buf"].getvalue() + f"[exit {code}]\n"


@pytest.mark.parametrize("case", sorted(_BACKTEST_CASES))
def test_rich_backtest_output_unchanged(env, case):
    with open(os.path.join(_SNAPSHOTS, f"{case}.txt"), encoding="utf-8") as f:
        assert _rich_backtest(env, case) == f.read()


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["wolfbet.py", *argv])
    try:
        wb.main()
    except SystemExit as e:
        return e.code
    return 0


def test_rich_export_line_unchanged(env, monkeypatch):
    code = _main(monkeypatch, "--export", "out.csv")
    with open(os.path.join(_SNAPSHOTS, "export_line.txt"), encoding="utf-8") as f:
        assert env["buf"].getvalue() + f"[exit {code}]\n" == f.read()


# ── helpers ──
def _validate(doc: dict, name: str) -> None:
    import jsonschema
    with open(os.path.join(_FIXTURES, f"wolfbet-cli-{name}-v1.json")) as f:
        jsonschema.Draft202012Validator(json.load(f)).validate(doc)


def _one_doc(capsys) -> tuple:
    """(stdout JSON document, stderr lines) after a --json run."""
    cap = capsys.readouterr()
    assert cap.out.count("\n") == 1, "exactly one JSON document on stdout"
    return json.loads(cap.out), [ln for ln in cap.err.splitlines() if ln]


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ── --export --json ──
def test_export_json_reports_a_verifiable_file(env, monkeypatch, capsys):
    assert _main(monkeypatch, "--export", "out.csv", "--json") == 0
    doc, _ = _one_doc(capsys)
    _validate(doc, "export")
    path = os.path.join(env["tmp"], "out.csv")
    assert doc == {"v": 1, "session_id": 1, "path": path, "format": "csv", "rows": len(_REPLAY),
                   "bytes": os.path.getsize(path), "sha256": _sha(path)}
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert env["buf"].getvalue() == ""


def test_export_into_a_directory_names_the_file(env, monkeypatch, capsys):
    os.mkdir("handoff")
    assert _main(monkeypatch, "--export", "handoff/", "--export-session-id", "1", "--json") == 0
    doc, _ = _one_doc(capsys)
    assert doc["path"] == os.path.join(env["tmp"], "handoff", "wolfbet-session-1.csv")
    assert doc["sha256"] == _sha(doc["path"])
    assert _main(monkeypatch, "--export", "handoff/s.json", "--json") == 0
    doc, _ = _one_doc(capsys)
    assert doc["format"] == "json" and json.load(open(doc["path"]))["session"]["id"] == 1


@pytest.mark.parametrize("make,target", [
    (lambda: (open("victim.txt", "w").write("keep"), os.symlink("victim.txt", "link.csv")), "link.csv"),
    (lambda: None, "missing-dir/out.csv"),
    (lambda: os.mkfifo("pipe.csv"), "pipe.csv"),
    (lambda: (os.mkdir("real"), os.symlink("real", "dirlink")), "dirlink"),
])
def test_unsafe_export_paths_are_refused(env, monkeypatch, capsys, make, target):
    make()
    before = sorted(os.listdir("."))
    assert _main(monkeypatch, "--export", target) == 1
    assert "Export path must be a regular file in an existing directory (not a symlink)" in \
        env["buf"].getvalue()
    assert _main(monkeypatch, "--export", target, "--json") == 1
    doc, _ = _one_doc(capsys)
    _validate(doc, "export")
    assert doc["error"]["code"] == "invalid_path"
    assert sorted(os.listdir(".")) == before
    if os.path.exists("victim.txt"):
        assert open("victim.txt").read() == "keep"


def test_export_error_docs(env, monkeypatch, capsys):
    assert _main(monkeypatch, "--export", "out.csv", "--export-session-id", "99", "--json") == 1
    doc, _ = _one_doc(capsys)
    _validate(doc, "export")
    assert doc["error"] == {"code": "session_not_found", "message": "Session #99 not found"}
    conn = sqlite3.connect(env["db"])
    conn.execute("DELETE FROM bets")
    conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()
    assert _main(monkeypatch, "--export", "out.csv", "--json") == 1
    doc, _ = _one_doc(capsys)
    assert doc["error"]["code"] == "no_sessions"


def test_export_write_failure_hides_details_in_production(env, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise PermissionError(13, "Permission denied", "/secret/path")
    monkeypatch.setattr(wb.os, "fdopen", boom)
    monkeypatch.setattr(wb, "APP_ENV", "production")
    assert _main(monkeypatch, "--export", "out.csv", "--json") == 1
    doc, _ = _one_doc(capsys)
    assert doc["error"] == {"code": "write_failed", "message": "Could not write the export file"}


# ── --backtest --json ──
def _bt_json(env, capsys, **over):
    code = 0
    try:
        wb._run_backtest(_bt_args(json=True, **over))
    except SystemExit as e:
        code = e.code
    doc, err = _one_doc(capsys)
    _validate(doc, "backtest")
    for line in err:
        _validate(json.loads(line), "progress")
    assert env["buf"].getvalue() == "", "the console stays silent under --json"
    return doc, [json.loads(line)["progress"] for line in err], code


def test_sessions_json_has_the_table_figures(env, capsys):
    doc, progress, code = _bt_json(env, capsys, **_BACKTEST_CASES["backtest_runs"])
    assert code == 0 and doc["mode"] == "sessions"
    r = doc["results"]
    with open(os.path.join(_SNAPSHOTS, "backtest_runs.txt"), encoding="utf-8") as f:
        table = f.read()
    assert f"{r['sessions']:,} in {r['runs']:,} run(s)" in table
    assert f"{r['green_pct']:.2f}%  ({r['green']:,} of {r['sessions']:,})" in table
    assert f"{r['avg_win']:+.8f}" in table and f"{r['avg_loss']:+.8f}" in table
    assert f"{r['net']:+.8f}  ({r['net_per_100']:+.2f} per 100 wagered)" in table
    assert f"typical {r['final_balance_typical']:.8f}" in table
    assert r["runs_ended"] == {"played every session": 6}
    assert doc["source"] == {"type": "simulate", "bets": 20000, "seed": 1}
    assert doc["strategy"]["name"] == "Martingale"
    assert progress == sorted(progress) and progress[-1] == 1.0


def test_same_seed_gives_the_same_document(env, capsys, monkeypatch):
    first, _, _ = _bt_json(env, capsys, simulate=5000, sessions=20)
    monkeypatch.setattr(wb, "state", wb.BotState())
    second, _, _ = _bt_json(env, capsys, simulate=5000, sessions=20)
    assert first == second


def test_single_session_json(env, capsys):
    doc, progress, code = _bt_json(env, capsys, simulate=400)
    assert code == 0 and doc["mode"] == "session"
    with open(os.path.join(_SNAPSHOTS, "backtest_single.txt"), encoding="utf-8") as f:
        summary = f.read()
    s = doc["summary"]
    assert f"Backtest complete: {s['bets']} bets" in summary
    assert f"{s['end_balance']:.8f}" in summary and f"W+{s['max_win_streak']}" in summary
    assert doc["stop_reason"].startswith("Profit target")
    assert progress[-1] == 1.0


def test_replay_json(env, capsys):
    doc, _, code = _bt_json(env, capsys, **_BACKTEST_CASES["backtest_replay"])
    assert code == 0 and doc["source"] == {"type": "replay", "session_id": 1}
    assert doc["results"]["runs_ended"] == {"the rolls ran out": 1}


def test_progress_is_throttled_to_two_lines_a_second(env, capsys, monkeypatch):
    clock = iter(i * 0.1 for i in range(10_000_000))       # 10 calls per simulated second
    monkeypatch.setattr(wb.time, "monotonic", lambda: next(clock))
    report = wb._json_progress()
    for i in range(100):
        report(i / 100)
    report(1.0, final=True)
    lines = [json.loads(ln)["progress"] for ln in capsys.readouterr().err.splitlines()]
    assert len(lines) == 21 and lines[-1] == 1.0       # one per 0.5 s over 10 s, plus the final


@pytest.mark.parametrize("over,code,name", [
    (dict(sessions=0), 2, "invalid_argument"),
    (dict(sessions=5, balance=float("nan")), 2, "invalid_argument"),
    (dict(preset="nope"), 1, "preset_not_found"),
    (dict(replay_session=7), 1, "replay_empty"),
])
def test_backtest_error_docs(env, capsys, over, code, name):
    doc, _, got = _bt_json(env, capsys, **over)
    assert got == code and doc["error"]["code"] == name


def test_empty_replay_is_refused_before_the_run(env, capsys):
    wb._run_backtest(_bt_args(replay_session=1, simulate=None))     # session 1 has bets: runs
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        wb._run_backtest(_bt_args(replay_session=7))
    assert e.value.code == 1
    assert "✗ Session #7 has no bets to replay" in env["buf"].getvalue()


def test_backtest_rule_errors_give_invalid_config(env, capsys):
    cfg = {**_CONFIG, "hard_cap": True, "max_loss": None}
    with open("config.json", "w") as f:
        json.dump(cfg, f)
    doc, _, code = _bt_json(env, capsys, sessions=5)
    assert code == 2 and doc["error"]["code"] == "invalid_config"
    assert "hard_cap needs max_loss" in doc["error"]["message"]


def test_cli_backtest_json_streams(env):
    import subprocess
    run_env = {**os.environ, "APP_ENV": "testing", "HOME": env["tmp"],
               "WOLFBET_CONFIG_PATH": os.path.join(env["tmp"], "config.json"),
               "WOLFBET_DB_PATH": env["db"]}
    r = subprocess.run([sys.executable, os.path.join(_WOLFBET_DIR, "wolfbet.py"), "--backtest",
                        "--simulate", "3000", "--sessions", "10", "--seed", "4", "--json"],
                       capture_output=True, text=True, env=run_env, timeout=120)
    assert r.returncode == 0, r.stderr
    _validate(json.loads(r.stdout), "backtest")
    lines = [json.loads(ln) for ln in r.stderr.splitlines() if ln.strip()]
    assert lines and lines[-1] == {"progress": 1.0}

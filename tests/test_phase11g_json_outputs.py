"""--json on the read commands: one JSON document on stdout, Rich view unchanged.

Each command is split into a data function and the Rich renderer that reads it,
so the dashboard agent and the terminal see the same numbers. Snapshots in
fixtures/cli_v1/snapshots were captured from the renderer before the split.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase11g_json_outputs.py -q
"""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
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

API_KEY = "wb_live_0123456789abcdefSECRET9876"
PROXY = "http://proxyuser:proxypass@10.0.0.1:8080"
EVENTS_URL = "redis://dash:redispass@127.0.0.1:6379/0"
SECRETS = ("0123456789abcdefSECRET", "proxypass", "proxyuser", "redispass")

_SESSIONS = [
    # id, start, end, cur, game, strategy, base, mult, bets, wins, losses, profit, wagered,
    # start_bal, end_bal, mws, mls, hi_bal, lo_bal, hi_win, big_loss, bpm, bps,
    # peak_bps, low_bps, peak_bpm, low_bpm, lane
    (1, "2026-11-10T10:00:00", "2026-11-10T10:30:00", "usdt", "dice", "Martingale",
     0.0001, 2.0, 6, 3, 3, 0.0004, 0.0012, 1.0, 1.0004, 2, 3, 1.0006, 0.9997,
     0.0002, 0.0004, 12.0, 0.2, 5, 1, 300, 60, 1),
    (2, "2026-11-11T09:00:00", "2026-11-11T09:20:00", "usdt", "limbo", "Flat",
     0.0002, 1.5, 3, 1, 2, -0.0002, 0.0006, 1.0004, 1.0002, 1, 2, 1.0005, 1.0001,
     0.0001, 0.0002, 9.0, 0.15, 3, 1, 180, 60, 2),
    (3, "2026-11-12T08:00:00", "2026-11-12T08:05:00", "usdt", "dice", "Flat",
     0.0001, 2.0, 0, 0, 0, 0.0, 0.0, 1.0002, 1.0002, 0, 0, 1.0002, 1.0002,
     0.0, 0.0, 0.0, 0.0, 0, 0, 0, 0, 3),
    (4, "2026-11-12T12:00:00", "2026-11-12T13:00:00", "usdt", "dice", "Martingale",
     0.0001, 2.0, 10, 6, 4, 0.0009, 0.002, 1.0002, 1.0011, 3, 2, 1.0012, 1.0,
     0.0003, 0.0002, 10.0, 0.17, 4, 1, 240, 60, None),
]

_BETS = [
    # session_id, timestamp, amount, roll, state, profit, balance_after
    (1, "2026-11-10T10:00:01", 0.0001, 60.12, "win", 0.0001, 1.0001),
    (1, "2026-11-10T10:00:02", 0.0001, 70.50, "win", 0.0001, 1.0002),
    (1, "2026-11-10T10:00:03", 0.0001, 20.00, "loss", -0.0001, 1.0001),
    (1, "2026-11-10T10:00:04", 0.0002, 30.00, "loss", -0.0002, 0.9999),
    (1, "2026-11-10T10:00:05", 0.0004, 10.00, "loss", -0.0004, 0.9997),
    (1, "2026-11-10T10:00:06", 0.0008, 99.00, "win", 0.0009, 1.0004),
    (2, "2026-11-11T09:00:01", 0.0002, 1.20, "loss", -0.0002, 1.0002),
    (2, "2026-11-11T09:00:02", 0.0002, 2.00, "win", 0.0001, 1.0003),
    (2, "2026-11-11T09:00:03", 0.0002, 1.10, "loss", -0.0001, 1.0002),
]

_STATE = {
    "pid": None, "session_id": 4, "uptime_sec": 3600, "run_uptime_sec": 7200,
    "currency": "usdt", "strategy": "Martingale", "strategy_key": "2", "multiplier": 2.0,
    "rule": "over", "loss_mult": 2.0, "base_bet": 0.0002, "current_bet": 0.0004,
    "highest_bet": 0.0008, "start_balance": 1.0002, "current_balance": 1.0011,
    "highest_balance": 1.0012, "lowest_balance": 1.0, "profit": 0.0009, "wagered": 0.002,
    "total_bets": 10, "wins": 6, "losses": 4, "current_streak": -2, "max_win_streak": 3,
    "max_loss_streak": 2, "highest_win": 0.0003, "biggest_loss": 0.0002,
    "bets_per_minute": 10.0, "bets_per_second": 0.17, "peak_bps": 4, "low_bps": 1,
    "peak_bpm": 240, "low_bpm": 60, "rate_remaining": 900, "rate_limit": 1000,
    "max_profit": 0.01, "max_loss": 0.005, "stop_on_balance": None,
    "profit_increment": 0.0001, "profit_threshold": 0.001, "profit_increment_scope": "run",
    "next_profit_milestone": 0.002, "run_profit_total": 0.0015, "run_first_session": 1,
    "run_bumps": 1, "api_p50_ms": 40.0, "api_p99_ms": 120.0, "api_peak_ms": 300.0,
    "cycle_ms": 55.0, "overhead_ms": 5.0, "theoretical_max_bps": 18.18, "game": "dice",
    "efficiency_pct": 92.0, "pipeline_effective": 1,
    "events": {"enabled": True, "running": True, "connected": True, "sent": 120,
               "dropped": 1, "overflow": 0, "last_error": None},
    "status": "Betting", "updated_at": "2026-11-12T13:00:00", "rule_count": 0,
}

_CONFIG = {
    "api_key": API_KEY, "currency": "usdt", "game": "dice", "rule": "over",
    "multiplier": 2.0, "bet_value": 49.5, "strategy": "Martingale", "strategy_key": "2",
    "base_bet": 0.0001, "loss_mult": 2.0, "max_profit": 0.01, "max_loss": 0.005,
    "recurring": True, "recurring_delay": 60, "streak_delay_loss": "3:5",
    "proxy": PROXY, "events_enabled": True, "events_url": EVENTS_URL,
    "lanes": {"count": 3, "split": "bet",
              "overrides": {"2": {"multiplier": 3.0, "streak_delay_loss": "2:4"},
                            "3": {"max_loss": 0.002}}},
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fixture DB, state and config files, a fresh BotState and a fixed-width console."""
    db = str(tmp_path / "wb.db")
    from core.database import init_db, migrate_db
    init_db(db)
    migrate_db(db)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO sessions (id, started_at, ended_at, currency, game, strategy, base_bet, "
        "multiplier, total_bets, wins, losses, profit, wagered, start_balance, end_balance, "
        "max_win_streak, max_loss_streak, highest_balance, lowest_balance, highest_win, "
        "biggest_loss, bets_per_minute, bets_per_second, peak_bps, low_bps, peak_bpm, "
        "low_bpm, lane) VALUES (" + ",".join("?" * 28) + ")", _SESSIONS)
    conn.executemany(
        "INSERT INTO bets (session_id, timestamp, amount, result_value, state, profit, "
        "balance_after) VALUES (?,?,?,?,?,?,?)", _BETS)
    conn.commit()
    conn.close()

    base_state = str(tmp_path / "live.json")
    for k, extra in ((1, {}), (2, {"session_id": 2, "profit": -0.0002, "lane_equity": 0.5,
                                   "bets_per_second": 20.0, "soft_pace_delay": 0.05,
                                   "pause_remaining_sec": 30})):
        with open(wb._lane_path(base_state, k), "w") as f:
            json.dump({**_STATE, "pid": os.getpid(), **extra}, f)
    with open(wb._lane_path(base_state, 3), "w") as f:
        f.write("{not json")
    monkeypatch.chdir(tmp_path)
    config = "config.json"
    with open(config, "w") as f:
        json.dump(_CONFIG, f)

    monkeypatch.setattr(wb, "DB_PATH", db)
    monkeypatch.setattr(wb, "_BASE_STATE_PATH", base_state)
    monkeypatch.setattr(wb, "STATE_PATH", base_state)
    monkeypatch.setattr(wb, "CONFIG_PATH", config)
    monkeypatch.setattr(wb, "state", wb.BotState())
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.delenv("WOLFBET_API_KEY", raising=False)
    monkeypatch.delenv("WOLFBET_EVENTS_URL", raising=False)
    buf = io.StringIO()
    monkeypatch.setattr(wb, "console", Console(file=buf, width=160, color_system=None,
                                               force_terminal=False, legacy_windows=False))
    return {"buf": buf, "tmp": str(tmp_path)}


_RICH_CASES = {
    "status":            lambda: wb.cmd_status(),
    "lanes_status":      lambda: wb.cmd_lanes_status(),
    "config_show":       lambda: wb.cmd_config_show(),
    "stats":             lambda: wb.cmd_stats(),
    "stats_window":      lambda: wb.cmd_stats(since="2026-11-11", until="2026-11-12", lane=2),
    "analytics":         lambda: wb.cmd_analytics(),
    "analytics_window":  lambda: wb.cmd_analytics(since="2026-11-11 00:00", lane=1),
    "last_bets":         lambda: wb.cmd_last_bets(5),
    "session_bets":      lambda: wb.cmd_session_bets(1),
}


def _rich_text(env, case: str) -> str:
    _RICH_CASES[case]()
    return env["buf"].getvalue()


@pytest.mark.parametrize("case", sorted(_RICH_CASES))
def test_rich_output_unchanged(env, case):
    with open(os.path.join(_SNAPSHOTS, f"{case}.txt"), encoding="utf-8") as f:
        assert _rich_text(env, case) == f.read()


# ── --json: one document per command, valid against its contract schema ──
def _schema(name: str) -> dict:
    with open(os.path.join(_FIXTURES, f"wolfbet-cli-{name}-v1.json")) as f:
        return json.load(f)


def _validate(doc: dict, name: str) -> None:
    import jsonschema
    jsonschema.Draft202012Validator(_schema(name)).validate(doc)


_JSON_CASES = {
    "status":            ("status", lambda: wb.cmd_status(as_json=True)),
    "lanes_status":      ("lanes-status", lambda: wb.cmd_lanes_status(as_json=True)),
    "config_show":       ("config-show", lambda: wb.cmd_config_show(as_json=True)),
    "stats":             ("stats", lambda: wb.cmd_stats(as_json=True)),
    "stats_window":      ("stats", lambda: wb.cmd_stats(since="2026-11-11", until="2026-11-12",
                                                        lane=2, as_json=True)),
    "analytics":         ("analytics", lambda: wb.cmd_analytics(as_json=True)),
    "analytics_window":  ("analytics", lambda: wb.cmd_analytics(since="2026-11-11 00:00", lane=1,
                                                                as_json=True)),
    "last_bets":         ("last-bets", lambda: wb.cmd_last_bets(5, as_json=True)),
    "session_bets":      ("session-bets", lambda: wb.cmd_session_bets(1, as_json=True)),
}


def _json_doc(capsys, case: str) -> dict:
    schema, run = _JSON_CASES[case]
    run()
    out = capsys.readouterr().out
    assert out.count("\n") == 1, "exactly one JSON document on stdout"
    doc = json.loads(out)
    _validate(doc, schema)
    return doc


@pytest.mark.parametrize("case", sorted(_JSON_CASES))
def test_json_document_matches_schema_and_prints_nothing_else(env, capsys, case):
    doc = _json_doc(capsys, case)
    assert doc["v"] == 1 and "error" not in doc
    assert env["buf"].getvalue() == "", "no Rich output with --json"


def test_analytics_json_has_the_numbers_the_rich_view_prints(env, capsys):
    doc = _json_doc(capsys, "analytics")
    rich = _rich_text(env, "analytics")
    life = doc["lifetime"]
    assert life["sessions_total"] == 4 and life["bets_total"] == 19
    assert f"{life['profit_total']:.8f}" in rich and f"{life['wagered_total']:.8f}" in rich
    assert f"{life['win_rate_pct']:.1f}%" in rich
    assert [r["lane"] for r in doc["by_lane"]] == ["Lane 1", "Lane 2", "Lane 3"]
    for r in doc["by_lane"] + doc["by_game"] + doc["by_strategy"]:
        assert f"{r['profit']:+.8f}".lstrip("+") in rich and f"{r['wagered']:.8f}" in rich
    assert [s["id"] for s in doc["top_best"]] == [4, 1, 3, 2]
    assert doc["span"] == {"first": "2026-11-10T10:00:00", "last": "2026-11-12T13:00:00",
                           "span_sec": 183600.0, "bet_sec": 6900.0}


def test_analytics_window_and_lane_filter_the_json_like_the_rich_view(env, capsys):
    doc = _json_doc(capsys, "analytics_window")
    rich = _rich_text(env, "analytics_window")
    assert doc["window"] == {"since": "2026-11-11T00:00", "until": None, "lane": 1}
    assert doc["lifetime"]["sessions_total"] == 1          # session 4: lane NULL counts as lane 1
    assert [s["id"] for s in doc["top_best"]] == [4]
    assert f"{doc['lifetime']['profit_total']:.8f}" in rich and "lane 1" in rich


def test_stats_json_filters_and_totals(env, capsys):
    doc = _json_doc(capsys, "stats_window")
    assert doc["limit"] == 50
    assert [s["id"] for s in doc["sessions"]] == [2]
    assert doc["sessions"][0]["lane"] == 2 and doc["sessions"][0]["currency"] == "USDT"
    assert doc["totals"]["sessions"] == 1 and doc["totals"]["profit"] == pytest.approx(-0.0002)
    doc = _json_doc(capsys, "stats")
    assert doc["limit"] == 20 and [s["id"] for s in doc["sessions"]] == [4, 3, 2, 1]
    assert doc["totals"]["bets"] == 19 and doc["totals"]["best_win_streak"] == 3


def test_session_bets_json(env, capsys):
    doc = _json_doc(capsys, "session_bets")
    assert doc["session"]["id"] == 1 and doc["uptime_sec"] == 1800
    assert doc["streaks"] == {"win": {"2": 1, "1": 1}, "loss": {"3": 1}}
    assert [b["id"] for b in doc["bets"]] == [6, 5, 4, 3, 2, 1]
    assert doc["bets"][0]["time"] == "10:00:06"


def test_lanes_and_status_json(env, capsys):
    doc = _json_doc(capsys, "lanes_status")
    assert [(r["lane"], r["alive"], r["paused"]) for r in doc["lanes"]] == \
        [(1, True, False), (2, True, True), (3, False, False)]
    assert doc["lanes"][2]["state"] is None
    assert doc["totals"]["live"] == 2 and doc["totals"]["count"] == 3
    assert doc["totals"]["profit"] == pytest.approx(0.0007)
    doc = _json_doc(capsys, "status")
    assert doc["running"] is True and doc["state"]["session_id"] == 4


def test_config_show_json_masks_every_secret(env, capsys):
    _JSON_CASES["config_show"][1]()
    out = capsys.readouterr().out
    for secret in SECRETS:
        assert secret not in out
    doc = json.loads(out)
    _validate(doc, "config-show")
    assert doc["api_key"] == {"set": True, "masked": "wb_live_...9876"}
    assert doc["config"]["proxy"] == "http://10.0.0.1:8080"
    assert doc["config"]["events_url"] == "redis://127.0.0.1:6379"
    assert doc["config"]["streak_delay_loss"] == [3, 5.0]
    assert doc["lane_overrides"] == {"2": {"multiplier": 3.0, "streak_delay_loss": [2, 4.0]},
                                     "3": {"max_loss": 0.002}}
    assert doc["live_base_bet"] == 0.0002 and doc["run_bumps"] == 1
    assert doc["lanes"] == 3 and doc["lane_split"] == "bet"


# ── error documents: same exit codes as the Rich commands ──
def _error_doc(capsys, schema: str, run, exit_code=None) -> dict:
    if exit_code is None:
        run()
    else:
        with pytest.raises(SystemExit) as e:
            run()
        assert e.value.code == exit_code
    doc = json.loads(capsys.readouterr().out)
    _validate(doc, schema)
    return doc["error"]


@pytest.mark.parametrize("schema,run", [
    ("stats", lambda: wb.cmd_stats(as_json=True)),
    ("analytics", lambda: wb.cmd_analytics(as_json=True)),
    ("last-bets", lambda: wb.cmd_last_bets(5, as_json=True)),
    ("session-bets", lambda: wb.cmd_session_bets(1, as_json=True)),
])
def test_missing_database_gives_error_doc(env, capsys, monkeypatch, tmp_path, schema, run):
    monkeypatch.setattr(wb, "DB_PATH", str(tmp_path / "missing.db"))
    assert _error_doc(capsys, schema, run)["code"] == "no_database"


def test_error_docs_for_bad_input_and_missing_files(env, capsys, monkeypatch, tmp_path):
    err = _error_doc(capsys, "stats", lambda: wb.cmd_stats(since="yesterday", as_json=True))
    assert err["code"] == "invalid_window" and "yesterday" in err["message"]
    err = _error_doc(capsys, "analytics", lambda: wb.cmd_analytics(until="13/11", as_json=True))
    assert err["code"] == "invalid_window"
    err = _error_doc(capsys, "session-bets", lambda: wb.cmd_session_bets(999, as_json=True))
    assert err == {"code": "session_not_found", "message": "Session #999 not found."}

    with open(wb.STATE_PATH, "w") as f:
        f.write("{broken")
    assert _error_doc(capsys, "status", lambda: wb.cmd_status(as_json=True),
                      exit_code=1)["code"] == "state_unreadable"
    monkeypatch.setattr(wb, "STATE_PATH", str(tmp_path / "none.json"))
    assert _error_doc(capsys, "status", lambda: wb.cmd_status(as_json=True),
                      exit_code=0)["code"] == "no_session"
    monkeypatch.setattr(wb, "_BASE_STATE_PATH", str(tmp_path / "none.json"))
    assert _error_doc(capsys, "lanes-status",
                      lambda: wb.cmd_lanes_status(as_json=True))["code"] == "no_session"
    monkeypatch.setattr(wb, "CONFIG_PATH", str(tmp_path / "none-config.json"))
    assert _error_doc(capsys, "config-show", lambda: wb.cmd_config_show(as_json=True),
                      exit_code=1)["code"] == "no_config"
    assert env["buf"].getvalue() == ""


def test_rich_error_messages_unchanged(env, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "DB_PATH", str(tmp_path / "missing.db"))
    wb.cmd_analytics()
    wb.cmd_stats()
    monkeypatch.setattr(wb, "CONFIG_PATH", str(tmp_path / "none-config.json"))
    with pytest.raises(SystemExit) as e:
        wb.cmd_config_show()
    assert e.value.code == 1
    assert env["buf"].getvalue() == (
        "No database found: run a session first.\nNo database found.\n"
        "No config saved yet: run the wizard first.\n  Try: python3 wolfbet.py --setup-only\n")
    assert capsys.readouterr().out == ""


# ── the real CLI entry point ──
def _cli(env, *argv):
    tmp = env["tmp"]
    run_env = {**os.environ, "APP_ENV": "testing",
               "WOLFBET_DB_PATH": os.path.join(tmp, "wb.db"),
               "WOLFBET_CONFIG_PATH": os.path.join(tmp, "config.json"),
               "WOLFBET_STATE_PATH": os.path.join(tmp, "live.json"),
               "HOME": tmp}
    run_env.pop("WOLFBET_API_KEY", None)
    return subprocess.run([sys.executable, os.path.join(_WOLFBET_DIR, "wolfbet.py"), *argv],
                          capture_output=True, text=True, env=run_env, timeout=60)


def test_cli_json_stdout_is_one_document(env):
    r = _cli(env, "--analytics", "--lane", "2", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    _validate(doc, "analytics")
    assert doc["window"]["lane"] == 2 and doc["lifetime"]["sessions_total"] == 1
    r = _cli(env, "--config-show", "--json")
    assert r.returncode == 0, r.stderr
    _validate(json.loads(r.stdout), "config-show")
    for secret in SECRETS:
        assert secret not in r.stdout and secret not in r.stderr


def test_cli_json_with_an_unsupported_command_exits_2(env):
    r = _cli(env, "--list-presets", "--json")
    assert r.returncode == 2 and r.stdout == ""
    assert "--json works with --status" in r.stderr


def test_cli_without_json_is_unchanged(env):
    r = _cli(env, "--last-bets", "2")
    assert r.returncode == 0 and "Last 2 Bets" in r.stdout
    with pytest.raises(ValueError):
        json.loads(r.stdout)


# ── contract copies ──
def test_schema_copies_match_checksums():
    with open(os.path.join(_FIXTURES, "SHA256SUMS")) as f:
        sums = dict(reversed(line.split()) for line in f if line.strip())
    assert len(sums) == 9
    for name, digest in sums.items():
        with open(os.path.join(_FIXTURES, name), "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == digest, name

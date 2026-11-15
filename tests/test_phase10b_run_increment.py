"""Phase 10b — v2.37.0: run-wide profit increment (profit_increment_scope=run).

The per-session increment only sees the current session's profit and its
milestone resets each recurring cycle. Scope "run" measures the total since
the run started (DB sum of the run's sessions + current), keeps the milestone
across sessions and persists bumps in a per-lane run file until run-reset.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase10b_run_increment.py -q
"""

import json
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

_ATTRS = ("lane", "profit", "base_bet", "current_bet", "profit_increment",
          "profit_threshold", "next_profit_milestone", "profit_increment_scope",
          "run_first_session", "run_since", "run_bumps", "run_profit_done",
          "game", "_auto_uuid", "session_id")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    saved = {a: getattr(wb.state, a, None) for a in _ATTRS}
    db = str(tmp_path / "run.db")
    monkeypatch.setattr(wb, "DB_PATH", db)
    monkeypatch.setattr(wb, "RUN_PATH", str(tmp_path / "run.json"))
    from core.database import init_db, migrate_db
    init_db(db)
    migrate_db(db)
    wb.state.lane = 1
    wb.state.game = "dice"
    wb.state._auto_uuid = None
    yield db
    for a, v in saved.items():
        setattr(wb.state, a, v)


def _seed(db, rows):
    """rows: [(id, profit, lane)]"""
    conn = sqlite3.connect(db)
    for sid, profit, lane in rows:
        conn.execute(
            "INSERT INTO sessions (id, started_at, currency, game, strategy, base_bet, "
            "multiplier, total_bets, wins, losses, profit, wagered, lane) "
            "VALUES (?, '2026-11-14T10:00:00', 'usdt', 'dice', 'Martingale', 0.0001, 2.0, "
            "4, 2, 2, ?, 0.001, ?)", (sid, profit, lane))
    conn.commit()
    conn.close()


def _cfg(scope, base=0.0001, inc=0.0001, thr=0.01):
    s = wb.state
    s.profit_increment_scope, s.base_bet, s.current_bet = scope, base, base
    s.profit_increment, s.profit_threshold = inc, thr
    s.next_profit_milestone = thr
    s.profit = 0.0


# ── session scope unchanged ───────────────────────────────
def test_session_scope_uses_session_profit():
    _cfg("session")
    wb.state.run_profit_done = 5.0         # ignored in session scope
    wb.state.profit = 0.005
    wb._check_profit_increment()
    assert wb.state.base_bet == pytest.approx(0.0001)
    wb.state.profit = 0.01
    wb._check_profit_increment()
    assert wb.state.base_bet == pytest.approx(0.0002)
    assert wb.state.next_profit_milestone == pytest.approx(0.02)


def test_init_run_noop_in_session_scope(_env):
    _cfg("session")
    wb._init_run()
    wb._attach_run(7)
    assert wb.state.run_first_session is None
    assert not os.path.exists(wb.RUN_PATH)


# ── run scope ─────────────────────────────────────────────
def test_new_run_created_at_first_session(_env):
    _cfg("run")
    wb._init_run()
    assert wb.state.run_first_session is None and wb.state.run_bumps == 0
    wb._attach_run(12)
    data = json.load(open(wb.RUN_PATH))
    assert data["first_session"] == 12 and data["bumps"] == 0 and data["started_at"]


def test_run_profit_sums_sessions_and_bumps_persist(_env):
    _seed(_env, [(1, 0.5, 1), (5, 0.004, 1), (6, 0.003, 1), (7, 0.9, 2)])
    json.dump({"first_session": 5, "started_at": "x", "bumps": 0}, open(wb.RUN_PATH, "w"))
    _cfg("run")
    wb._init_run()
    assert wb.state.run_profit_done == pytest.approx(0.007)   # #1 before run, #7 other lane
    wb.state.profit = 0.002
    wb._check_profit_increment()
    assert wb.state.base_bet == pytest.approx(0.0001)          # 0.009 < 0.01
    wb.state.profit = 0.003                                    # run total 0.010
    wb._check_profit_increment()
    assert wb.state.base_bet == pytest.approx(0.0002)
    assert wb.state.next_profit_milestone == pytest.approx(0.02)
    assert json.load(open(wb.RUN_PATH))["bumps"] == 1


def test_resume_restores_bumps_on_base_bet(_env):
    _seed(_env, [(3, 0.025, 1)])
    json.dump({"first_session": 3, "started_at": "x", "bumps": 2}, open(wb.RUN_PATH, "w"))
    _cfg("run")
    wb._init_run()
    assert wb.state.base_bet == pytest.approx(0.0003)
    assert wb.state.current_bet == pytest.approx(0.0003)
    assert wb.state.next_profit_milestone == pytest.approx(0.03)
    wb._attach_run(4)                       # existing run keeps its start
    assert json.load(open(wb.RUN_PATH))["first_session"] == 3


def test_stale_run_file_after_db_reset_starts_new_run(_env):
    _seed(_env, [(1, 0.1, 1)])
    json.dump({"first_session": 5000, "started_at": "x", "bumps": 4}, open(wb.RUN_PATH, "w"))
    _cfg("run")
    wb._init_run()
    assert wb.state.run_first_session is None and wb.state.run_bumps == 0
    assert wb.state.base_bet == pytest.approx(0.0001)


def test_lane_run_file_and_filter(_env):
    _seed(_env, [(2, 0.01, 1), (3, 0.02, 2)])
    wb.state.lane = 2
    json.dump({"first_session": 2, "started_at": "x", "bumps": 0},
              open(wb._lane_path(wb.RUN_PATH, 2), "w"))
    _cfg("run")
    wb._init_run()
    assert wb.state.run_first_session == 2
    assert wb.state.run_profit_done == pytest.approx(0.02)


def test_recurring_reset_keeps_run_milestone_and_total():
    src = open(os.path.join(_WOLFBET_DIR, "wolfbet.py")).read()
    body = src[src.index("def _reset_session_for_recurring"):src.index("def _should_recurring_restart")]
    assert "state.run_profit_done += state.profit" in body
    assert 'state.profit_increment_scope != "run"' in body


def test_auto_game_bump_defers_uuid_clear(_env):
    _cfg("run")
    wb.state.game, wb.state._auto_uuid = "dice-auto", "u"
    wb.state.run_first_session = 1
    wb.state.run_profit_done, wb.state.profit = 0.02, 0.0
    wb._check_profit_increment()
    assert wb.state._auto_uuid == "u"
    assert wb.state._auto_restart_pending is True and wb.state.run_bumps == 1


# ── config / commands ─────────────────────────────────────
def test_config_set_scope(monkeypatch, tmp_path):
    path = str(tmp_path / "cfg.json")
    json.dump({"api_key": "k", "currency": "usdt", "game": "dice", "rule": "over",
               "multiplier": 2.0, "bet_value": 50.49, "base_bet": 0.0001}, open(path, "w"))
    monkeypatch.setattr(wb, "CONFIG_PATH", path)
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.setattr(wb, "_signal_lane_daemons", lambda *a, **k: 0)
    wb.cmd_config_set(["piscope=run"])
    assert json.load(open(path))["profit_increment_scope"] == "run"
    with pytest.raises(SystemExit) as e:
        wb.cmd_config_set(["profit_increment_scope=forever"])
    assert e.value.code == 2


def test_run_reset_removes_files(_env):
    for k in (1, 3):
        open(wb._lane_path(wb.RUN_PATH, k), "w").write("{}")
    assert wb.cmd_run_reset() == 2
    assert not os.path.exists(wb.RUN_PATH)
    open(wb._lane_path(wb.RUN_PATH, 2), "w").write("{}")
    assert wb.cmd_run_reset(2) == 1


def test_progress_row_uses_run_total():
    row = wb._build_config_progress_row({
        "profit_threshold": 0.01, "profit_increment": 0.0001, "next_profit_milestone": 0.02,
        "profit": 0.0, "profit_increment_scope": "run", "run_profit_total": 0.015})
    assert "RunMs" in row and "50%" in row


def test_wolfctl_run_reset_wiring():
    s = open(os.path.join(_WOLFBET_DIR, "wolfctl")).read()
    assert 'run-reset)    shift; cmd_run_reset "$@" ;;' in s
    assert '"$PYTHON" "$BOT" --run-reset' in s
    assert 'rm -f "$HOME/.wolfbet_run.json"' in s


# ── none / 0 turns it fully off (no background run) ───────
def test_zero_increment_never_bumps_or_counts(_env):
    _cfg("run", inc=0.0)
    wb.state.run_first_session = 1
    wb.state.run_profit_done = 1.0
    wb._check_profit_increment()
    assert wb.state.run_bumps == 0 and wb.state.base_bet == pytest.approx(0.0001)


@pytest.mark.parametrize("key,val", [("profit_increment", "0.0"), ("profit_increment", "-1"),
                                     ("profit_threshold", "0.0"), ("profit_threshold", "none")])
def test_mutation_zero_means_off(key, val):
    _cfg("run")
    wb._apply_mutation({key: val})
    assert getattr(wb.state, key) is None
    assert not wb._increment_on()


@pytest.mark.parametrize("inc,thr,scope", [(None, 0.01, "run"), (0.0001, None, "run"),
                                           (0.0001, 0.01, "session")])
def test_init_run_off_removes_run_file(_env, inc, thr, scope):
    _seed(_env, [(3, 0.05, 1)])
    json.dump({"first_session": 3, "started_at": "x", "bumps": 2}, open(wb.RUN_PATH, "w"))
    _cfg(scope, inc=inc, thr=thr)
    wb._init_run()
    wb._attach_run(4)
    assert not os.path.exists(wb.RUN_PATH)
    assert wb.state.run_first_session is None and wb.state.run_bumps == 0
    assert wb.state.base_bet == pytest.approx(0.0001)       # old bumps not re-applied


def _live_cfg(monkeypatch, tmp_path, **vals):
    path = str(tmp_path / "cfg.json")
    json.dump({"base_bet": 0.0001, "profit_increment_scope": "run", **vals}, open(path, "w"))
    monkeypatch.setattr(wb, "CONFIG_PATH", path)


def test_live_reload_increment_off_ends_run(_env, monkeypatch, tmp_path):
    _cfg("run")
    wb._attach_run(5)
    assert os.path.exists(wb.RUN_PATH)
    _live_cfg(monkeypatch, tmp_path, profit_increment=None, profit_threshold=0.01)
    wb._reload_live_config()
    assert not os.path.exists(wb.RUN_PATH) and wb.state.run_first_session is None


def test_live_reload_increment_on_starts_run(_env, monkeypatch, tmp_path):
    _cfg("run", inc=None)
    wb.state.session_id = 8
    _live_cfg(monkeypatch, tmp_path, profit_increment=0.0001, profit_threshold=0.01)
    wb._reload_live_config()
    assert wb.state.run_first_session == 8
    assert json.load(open(wb.RUN_PATH))["first_session"] == 8

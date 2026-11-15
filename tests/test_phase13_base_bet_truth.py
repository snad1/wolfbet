"""Base bet is one truth: live reload, origin-keyed bumps, config-show provenance.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase13_base_bet_truth.py -q
"""

import inspect
import io
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402
from rich.console import Console  # noqa: E402

_INC = 0.00000001
_THR = 0.00035

_ATTRS = ("game", "base_bet", "current_bet", "profit", "current_streak",
          "profit_increment", "profit_threshold", "next_profit_milestone",
          "profit_increment_scope", "run_first_session", "run_since",
          "run_bumps", "run_profit_done", "run_base_origin", "_auto_uuid",
          "_auto_restart_pending", "api_key", "lane")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    saved = {a: getattr(wb.state, a, None) for a in _ATTRS}
    monkeypatch.setattr(wb, "RUN_PATH", str(tmp_path / "run.json"))
    monkeypatch.setattr(wb, "CONFIG_PATH", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(wb, "_run_profit_done_from_db", lambda *a, **k: 0.25)
    monkeypatch.setattr(wb, "_db_max_session_id", lambda: 10)
    s = wb.state
    s.lane = 1
    s.game = "dice"
    s.base_bet = s.current_bet = 0.00005805
    s.profit = 0.0
    s.current_streak = 0
    s.profit_increment = _INC
    s.profit_threshold = _THR
    s.next_profit_milestone = _THR
    s.profit_increment_scope = "run"
    s.run_first_session = None
    s.run_since = None
    s.run_bumps = 0
    s.run_profit_done = 0.0
    s.run_base_origin = None
    s._auto_uuid = None
    s._auto_restart_pending = False
    s.api_key = "k"
    yield
    for a, v in saved.items():
        setattr(wb.state, a, v)


def _write_run(bumps, base, first_session=5):
    payload = {"first_session": first_session, "started_at": "2026-11-15T00:00:00",
               "bumps": bumps}
    if base is not None:
        payload["base"] = base
    with open(wb.RUN_PATH, "w") as f:
        json.dump(payload, f)


def _read_run():
    with open(wb.RUN_PATH) as f:
        return json.load(f)


# ── explicit base change re-baselines the run ────────────
def test_rebase_zeroes_bumps_and_reanchors():
    s = wb.state
    s.run_first_session = 5
    s.run_bumps = 729
    s.run_profit_done = 0.25
    s.base_bet = s.current_bet = 0.00006534
    _write_run(729, 0.00005805)

    wb._rebase_run_for_base_change(0.00005805)

    assert s.current_bet == pytest.approx(0.00005805)
    assert s.run_bumps == 0
    assert s.run_base_origin == pytest.approx(0.00005805)
    assert s.next_profit_milestone == pytest.approx((714 + 1) * _THR)
    assert _read_run()["base"] == pytest.approx(0.00005805)
    assert _read_run()["bumps"] == 0


def test_rebase_marks_auto_cycle_pending():
    wb.state.game = "dice-auto"
    wb.state.run_first_session = 5
    wb._rebase_run_for_base_change(0.00005805)
    assert wb.state._auto_restart_pending is True


def test_rebase_leaves_manual_game_unpending():
    wb._rebase_run_for_base_change(0.00005805)
    assert wb.state._auto_restart_pending is False


def test_rebase_without_run_only_sets_the_bet():
    wb.state.run_first_session = None
    wb._rebase_run_for_base_change(0.00009)
    assert wb.state.current_bet == pytest.approx(0.00009)
    assert wb.state.run_bumps == 0


# ── bumps belong to the base they were earned on ─────────
def test_resume_drops_bumps_after_a_base_change():
    _write_run(729, 0.00006534)
    wb.state.run_bumps = 0

    wb._init_run()

    assert wb.state.run_bumps == 0
    assert wb.state.base_bet == pytest.approx(0.00005805)
    assert wb.state.next_profit_milestone == pytest.approx((714 + 1) * _THR)


def test_resume_applies_bumps_on_the_same_base():
    wb.state.next_profit_milestone = _THR
    wb.state.run_profit_done = 0.0
    _write_run(3, 0.00005805)
    wb_orig = wb._run_profit_done_from_db
    wb._run_profit_done_from_db = lambda *a, **k: 3 * _THR
    try:
        wb._init_run()
    finally:
        wb._run_profit_done_from_db = wb_orig

    assert wb.state.run_bumps == 3
    assert wb.state.base_bet == pytest.approx(round(0.00005805 + 3 * _INC, 8))
    assert wb.state.next_profit_milestone == pytest.approx(4 * _THR)


def test_legacy_run_file_keeps_bumps_and_adopts_base():
    _write_run(3, None)
    wb.state.run_profit_done = 0.0
    wb._run_profit_done_from_db = lambda *a, **k: 3 * _THR

    wb._init_run()

    assert wb.state.run_bumps == 3
    assert wb.state.run_base_origin == pytest.approx(0.00005805)


# ── the live reload is what the incident needed ──────────
def test_live_reload_applies_base_bet():
    wb.state.game = "dice-auto"
    wb.state.base_bet = wb.state.current_bet = 0.00006534
    wb.state.run_first_session = 5
    wb.state.run_bumps = 729
    wb.state.run_profit_done = 0.25
    with open(wb.CONFIG_PATH, "w") as f:
        json.dump({"base_bet": 0.00005805}, f)

    applied = wb._reload_live_config()

    assert "base_bet" in applied
    assert wb.state.base_bet == pytest.approx(0.00005805)
    assert wb.state.current_bet == pytest.approx(0.00005805)
    assert wb.state.run_bumps == 0
    assert wb.state._auto_restart_pending is True


# ── the screen shows the live value ──────────────────────
def test_config_show_reports_live_base(monkeypatch, tmp_path):
    with open(wb.CONFIG_PATH, "w") as f:
        json.dump({}, f)
    monkeypatch.setattr(wb, "load_config", lambda *a, **k: True)
    monkeypatch.setattr(wb, "_monitor_read_state", lambda: {
        "pid": 4242, "base_bet": 0.00006534, "run_bumps": 729})
    monkeypatch.setattr(wb, "_monitor_get_pid", lambda d: 4242)
    wb.state.base_bet = 0.00005805
    buf = io.StringIO()
    monkeypatch.setattr(wb, "console",
                        Console(file=buf, force_terminal=False, width=200))

    wb.cmd_config_show()

    out = buf.getvalue()
    assert "live 0.00006534" in out
    assert "+729 bumps" in out


def test_wolfctl_start_reports_an_active_unit():
    src = open(os.path.join(_WOLFBET_DIR, "wolfctl")).read()
    start = src[src.index("cmd_start()"):src.index("cmd_stop()")]
    assert "was_active" in start
    assert "already running" in start
    assert "wolfctl restart" in start


def test_monitor_reports_a_stop_only_when_it_ran():
    src = inspect.getsource(wb.cmd_monitor)
    assert "stopped = True" in src
    assert "if stopped:" in src

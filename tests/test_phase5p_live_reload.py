"""Phase 5p tests — SIGHUP-driven live config reload (v2.28.3).

Fixes the "wolfctl config-set recurring=on prints Saved but the daemon
never picks it up" bug. wolfctl config-set now signals the daemon after
save; the daemon re-reads config for a bounded allowlist of safe fields.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5p_live_reload.py -q
"""

import json
import os
import signal
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _fresh_config(monkeypatch, payload: dict):
    fd, path = tempfile.mkstemp(prefix="wb_reload_", suffix=".json")
    os.close(fd)
    with open(path, "w") as f:
        json.dump(payload, f)
    monkeypatch.setattr(wb, "CONFIG_PATH", path)
    return path


# ── Allowlist membership ─────────────────────────────────
def test_live_reload_includes_recurring():
    assert "recurring" in wb._LIVE_RELOAD_KEYS


def test_live_reload_includes_all_stop_conditions():
    for k in ("max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance"):
        assert k in wb._LIVE_RELOAD_KEYS


def test_live_reload_includes_streak_tuples():
    for k in ("streak_delay_loss", "streak_delay_win",
              "streak_delay_bets", "streakbet_loss"):
        assert k in wb._LIVE_RELOAD_KEYS


def test_live_reload_excludes_unsafe_fields():
    """Fields whose mid-session change would corrupt session identity or the
    Martingale ladder MUST require a full restart."""
    for k in ("api_key", "currency", "game", "rule", "multiplier",
              "bet_value", "strategy", "strategy_key", "base_bet",
              "loss_mult", "win_mult", "delay_martin_threshold"):
        assert k not in wb._LIVE_RELOAD_KEYS, f"{k!r} must NOT be live-reloadable"


# ── _reload_live_config — round-trip ─────────────────────
def test_reload_picks_up_recurring_on(monkeypatch):
    _fresh_config(monkeypatch, {"recurring": True})
    wb.state.recurring = False
    applied = wb._reload_live_config()
    assert wb.state.recurring is True
    assert "recurring" in applied
    assert applied["recurring"] == (False, True)


def test_reload_picks_up_max_profit(monkeypatch):
    _fresh_config(monkeypatch, {"max_profit": 0.005})
    wb.state.max_profit = None
    applied = wb._reload_live_config()
    assert wb.state.max_profit == 0.005
    assert "max_profit" in applied


def test_reload_decodes_streak_tuple(monkeypatch):
    _fresh_config(monkeypatch, {"streak_delay_loss": "3:0.5"})
    wb.state.streak_delay_loss = (0, 0.0)
    applied = wb._reload_live_config()
    assert wb.state.streak_delay_loss == (3, 0.5)
    assert "streak_delay_loss" in applied


def test_reload_decodes_null_streak_tuple_as_zero(monkeypatch):
    _fresh_config(monkeypatch, {"streak_delay_win": None})
    wb.state.streak_delay_win = (5, 1.0)
    wb._reload_live_config()
    assert wb.state.streak_delay_win == (0, 0.0)


def test_reload_picks_up_pipeline_depth(monkeypatch):
    _fresh_config(monkeypatch, {"pipeline_depth": 3})
    wb.state.pipeline_depth = 1
    applied = wb._reload_live_config()
    assert wb.state.pipeline_depth == 3
    assert "pipeline_depth" in applied


def test_reload_picks_up_db_write_mode(monkeypatch):
    _fresh_config(monkeypatch, {"db_write_mode": "sync"})
    wb.state.db_write_mode = "async"
    applied = wb._reload_live_config()
    assert wb.state.db_write_mode == "sync"
    assert "db_write_mode" in applied


def test_reload_no_change_returns_empty(monkeypatch):
    _fresh_config(monkeypatch, {"recurring": True})
    wb.state.recurring = True   # already matches
    applied = wb._reload_live_config()
    assert applied == {}   # no delta → nothing reported


# ── Unsafe key skip semantics ────────────────────────────
def test_reload_skips_unsafe_game_field(monkeypatch, caplog):
    """Changing `game` on disk must NOT flip live state — logs a warn instead."""
    _fresh_config(monkeypatch, {"game": "dice"})
    wb.state.game = "limbo-auto"
    caplog.set_level("WARNING", logger=wb.logger.name)
    applied = wb._reload_live_config()
    assert wb.state.game == "limbo-auto"   # unchanged
    assert "game" not in applied
    # Warning must mention the skip
    assert any("unsafe" in rec.message.lower() and "game" in rec.message
               for rec in caplog.records)


def test_reload_skips_unsafe_base_bet(monkeypatch):
    _fresh_config(monkeypatch, {"base_bet": 0.0009})
    wb.state.base_bet = 0.0001
    applied = wb._reload_live_config()
    assert wb.state.base_bet == 0.0001
    assert "base_bet" not in applied


# ── Missing file / bad JSON safety ────────────────────────
def test_reload_missing_config_returns_empty(monkeypatch):
    monkeypatch.setattr(wb, "CONFIG_PATH", "/tmp/nonexistent-wolfbet-config.json")
    assert wb._reload_live_config() == {}


def test_reload_bad_json_logs_warn_returns_empty(monkeypatch, caplog):
    fd, path = tempfile.mkstemp(prefix="wb_bad_", suffix=".json")
    os.close(fd)
    with open(path, "w") as f:
        f.write("{ this is not JSON")
    monkeypatch.setattr(wb, "CONFIG_PATH", path)
    caplog.set_level("WARNING", logger=wb.logger.name)
    assert wb._reload_live_config() == {}
    assert any("failed to parse" in rec.message for rec in caplog.records)


# ── _sig_reload never raises ─────────────────────────────
def test_sig_reload_swallows_exceptions(monkeypatch):
    def _boom():
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(wb, "_reload_live_config", _boom)
    # If this raises, the test fails.
    wb._sig_reload(1, None)


def test_sig_reload_calls_reload(monkeypatch):
    calls = []
    monkeypatch.setattr(wb, "_reload_live_config",
                        lambda: (calls.append(1), {"recurring": (False, True)})[1])
    wb._sig_reload(1, None)
    assert calls == [1]


# ── SIGHUP handler is registered in main() ───────────────
def test_main_registers_sighup_handler():
    import inspect
    src = inspect.getsource(wb.main)
    assert "signal.SIGHUP" in src
    assert "_sig_reload" in src


# ── cmd_config_set signals the daemon on PID_PATH present ──
def test_cmd_config_set_sends_sighup_when_pid_alive(monkeypatch, tmp_path):
    # Point CONFIG_PATH + PID_PATH at tmp files.
    cfg = tmp_path / "wolfbet.json"
    with open(cfg, "w") as f:
        json.dump({
            "api_key": "eyJ0eXAi", "currency": "usdt", "game": "dice",
            "rule": "over", "multiplier": 2.0, "bet_value": 50.49,
            "base_bet": 0.0001, "strategy": "Flat Bet", "strategy_key": "1",
            "win_mult": 1.0, "loss_mult": 2.0,
        }, f)
    pid_path = tmp_path / "wolfbet.pid"
    pid_path.write_text("12345\n")
    monkeypatch.setattr(wb, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(wb, "PID_PATH", str(pid_path))

    calls = []
    def fake_kill(pid, sig):
        calls.append((pid, sig))
    monkeypatch.setattr(os, "kill", fake_kill)

    wb.cmd_config_set(["max_profit=0.005"])
    assert calls == [(12345, signal.SIGHUP)]


def test_cmd_config_set_skips_sighup_when_pid_missing(monkeypatch, tmp_path):
    cfg = tmp_path / "wolfbet.json"
    with open(cfg, "w") as f:
        json.dump({
            "api_key": "abc", "currency": "usdt", "game": "dice",
            "rule": "over", "multiplier": 2.0, "bet_value": 50.49,
            "base_bet": 0.0001, "strategy": "Flat Bet", "strategy_key": "1",
            "win_mult": 1.0, "loss_mult": 2.0,
        }, f)
    monkeypatch.setattr(wb, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(wb, "PID_PATH", str(tmp_path / "no_such.pid"))
    calls = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append((pid, sig)))

    wb.cmd_config_set(["max_profit=0.005"])
    assert calls == []   # no signal when daemon isn't running

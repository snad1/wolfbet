"""Phase 10 - v2.35.0: parallel lanes (N independent sessions side by side).

v2.36.0: how lanes share money is configurable (lanes.split, default none).

Each lane is its own bot process (`--lane K`) with its own state/pid files,
session, Martingale chain, stops and pauses. Lanes share the DB and config,
may override settings per lane, and (split budget/both) may only risk balance ÷ lanes.

Run: APP_ENV=testing pytest wolfbet/tests/test_phase10_lanes.py -q
"""

import json
import os
import sqlite3
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402

_LANE_ATTRS = ("lane", "lanes_count", "lane_share", "lane_run_profit", "profit",
               "current_bet", "current_balance", "base_bet", "max_wins",
               "streak_reset_losses", "recurring", "currency", "loss_mult",
               "multiplier", "bet_value", "rule", "streak_delay_loss",
               "soft_pace_delay", "lane_split", "base_bet_disk", "base_bet_split",
               "basebet_pct", "max_loss", "strategy_key")


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    saved = {a: getattr(wb.state, a, None) for a in _LANE_ATTRS}
    monkeypatch.setattr(wb, "STATE_PATH", wb.STATE_PATH)
    monkeypatch.setattr(wb, "PID_PATH", wb.PID_PATH)
    fmt = wb._log_handler.formatter
    yield
    for a, v in saved.items():
        setattr(wb.state, a, v)
    wb._log_handler.setFormatter(fmt)


def _config(monkeypatch, tmp_path, data):
    path = str(tmp_path / "cfg.json")
    with open(path, "w") as f:
        json.dump(data, f)
    monkeypatch.setattr(wb, "CONFIG_PATH", path)
    return path


BASE = {"api_key": "k", "currency": "usdt", "game": "dice-auto", "rule": "over",
        "multiplier": 2.0, "bet_value": 50.49, "base_bet": 0.0001,
        "strategy": "Martingale", "strategy_key": "2", "loss_mult": 2.0,
        "max_wins": 5, "recurring": True, "streak_reset_losses": 2}


# ── identity + paths ──────────────────────────────────────
def test_lane_path():
    assert wb._lane_path("/h/.wolfbet_autobot_live.json", 1) == "/h/.wolfbet_autobot_live.json"
    assert wb._lane_path("/h/.wolfbet_autobot_live.json", 3) == "/h/.wolfbet_autobot_live.lane3.json"
    assert wb._lane_path("/h/.wolfbet_autobot.pid", 2) == "/h/.wolfbet_autobot.lane2.pid"


def test_apply_lane_switches_paths_and_log_prefix():
    wb._apply_lane(4)
    assert wb.state.lane == 4
    assert wb.STATE_PATH.endswith(".lane4.json")
    assert wb.PID_PATH.endswith(".lane4.pid")
    assert "[L4]" in wb._log_handler.formatter._fmt


def test_apply_lane_one_keeps_classic_paths():
    wb._apply_lane(1)
    assert wb.STATE_PATH == wb._BASE_STATE_PATH and wb.PID_PATH == wb._BASE_PID_PATH


@pytest.mark.parametrize("bad", [0, 11, -1])
def test_apply_lane_range(bad):
    with pytest.raises(ValueError):
        wb._apply_lane(bad)


# ── config: count + overrides ─────────────────────────────
def test_load_config_applies_lane_overrides(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path, {**BASE, "lanes": {"count": 3, "overrides": {
        "2": {"base_bet": 0.0005, "max_wins": 2, "currency": "btc",
              "streak_delay_loss": "3:1.5"}}}})
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    wb.state.lane = 2
    assert wb.load_config()
    assert wb.state.lanes_count == 3
    assert wb.state.base_bet == 0.0005 and wb.state.current_bet == 0.0005
    assert wb.state.max_wins == 2
    assert wb.state.currency == "usdt"           # forbidden per lane → base kept
    assert wb.state.streak_delay_loss == (3, 1.5)
    wb.state.lane = 1
    assert wb.load_config()
    assert wb.state.base_bet == 0.0001 and wb.state.max_wins == 5


def test_save_config_keeps_lanes_block_and_lane_k_never_writes(monkeypatch, tmp_path):
    path = _config(monkeypatch, tmp_path, {**BASE, "lanes": {"count": 2, "overrides": {"2": {"max_wins": 1}}}})
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    wb.state.lane = 1
    wb.load_config()
    wb.state.max_wins = 9
    wb.save_config()
    data = json.load(open(path))
    assert data["max_wins"] == 9
    assert data["lanes"] == {"count": 2, "overrides": {"2": {"max_wins": 1}}}
    wb.state.lane = 2
    wb.state.max_wins = 77
    wb.save_config()
    assert json.load(open(path))["max_wins"] == 9


def test_live_reload_merges_lane_overrides(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path, {**BASE, "max_wins": 5,
                                    "lanes": {"count": 2, "overrides": {"2": {"max_wins": 3}}}})
    wb.state.lane = 2
    wb.state.max_wins = 5
    wb._reload_live_config()
    assert wb.state.max_wins == 3


def test_split_lane_pairs():
    base, per_lane, count, split = wb._split_lane_pairs(
        ["max_wins=4", "lanes=3", "lane2.base_bet=0.001", "LANE3.maxwins=1", "lane_split=Both"])
    assert base == ["max_wins=4"] and count == 3 and split == "both"
    assert per_lane == {2: ["base_bet=0.001"], 3: ["maxwins=1"]}


@pytest.mark.parametrize("pairs", [["lanes=0"], ["lanes=11"], ["lanes=x"], ["lane1.base_bet=1"],
                                   ["lane11.base_bet=1"], ["lane_split=half"],
                                   ["lane2.lane_split=bet"]])
def test_split_lane_pairs_rejects(pairs):
    with pytest.raises(ValueError):
        wb._split_lane_pairs(pairs)


def test_config_set_lanes_and_overrides(monkeypatch, tmp_path, capsys):
    path = _config(monkeypatch, tmp_path, dict(BASE))
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.setattr(wb, "_signal_lane_daemons", lambda *a, **k: 0)
    wb.state.lane = 1
    wb.cmd_config_set(["lanes=3", "lane2.base_bet=0.0003", "lane2.max_wins=2"])
    data = json.load(open(path))
    assert data["lanes"]["count"] == 3
    assert data["lanes"]["overrides"]["2"]["base_bet"] == pytest.approx(0.0003)
    assert data["lanes"]["overrides"]["2"]["max_wins"] == 2
    assert data["base_bet"] == 0.0001                    # base untouched
    wb.cmd_config_set(["lane2.max_wins=default"])
    data = json.load(open(path))
    assert "max_wins" not in data["lanes"]["overrides"]["2"]
    assert "override removed" in capsys.readouterr().out


def test_config_set_forbidden_lane_key_exits(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path, dict(BASE))
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    wb.state.lane = 1
    with pytest.raises(SystemExit) as e:
        wb.cmd_config_set(["lane2.currency=btc"])
    assert e.value.code == 2


# ── split budget ──────────────────────────────────────────
def _budget_state(share, run_profit=0.0, profit=0.0, bet=0.0001):
    s = wb.state
    for a in ("max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
              "streak_reset_losses"):
        setattr(s, a, None)
    s.lane_share, s.lane_run_profit, s.profit, s.current_bet = share, run_profit, profit, bet
    s.total_bets = s.wins = 0
    s.current_streak = -3


def test_budget_stop_when_next_bet_exceeds_equity():
    _budget_state(share=0.01, run_profit=0.002, profit=-0.005, bet=0.0080)
    stop, reason = wb.should_stop()
    assert stop and reason.startswith(wb.LANE_BUDGET_REASON)
    wb.state.profit = 0.0
    assert not wb._should_recurring_restart(reason)


def test_budget_ok_within_equity():
    _budget_state(share=0.01, run_profit=0.0, profit=-0.005, bet=0.004)
    assert wb.should_stop() == (False, "")


def test_no_budget_without_lanes():
    _budget_state(share=None, bet=99.0)
    assert wb.should_stop() == (False, "")


def test_init_lane_budget():
    wb.state.lanes_count, wb.state.current_balance = 4, 1.0
    wb.state.lane_split = "none"                 # v2.36.0 default: no lane budget
    wb._init_lane_budget()
    assert wb.state.lane_share is None
    for mode in ("budget", "both"):
        wb.state.lane_split = mode
        wb._init_lane_budget()
        assert wb.state.lane_share == pytest.approx(0.25)
    wb.state.lane_split = "bet"
    wb._init_lane_budget()
    assert wb.state.lane_share is None
    wb.state.lane_split = "budget"
    wb._init_lane_budget()
    wb.state.lanes_count = 1
    wb._init_lane_budget()
    assert wb.state.lane_share is None


def test_risky_config_uses_lane_share():
    wb.state.strategy_key, wb.state.streak_reset_losses, wb.state.max_loss = "2", 2, None
    wb.state.base_bet, wb.state.loss_mult = 0.001, 2.0
    wb.state.current_balance, wb.state.lanes_count = 4.0, 4   # share 1.0 → 9-loss chain
    wb.state.lane_split = "budget"
    warns = wb._check_risky_config()
    assert any("lane share covers a 9-loss chain" in w for w in warns)
    wb.state.lane_split = "none"                              # full balance 4.0 → 11
    warns = wb._check_risky_config()
    assert any("balance covers a 11-loss chain" in w for w in warns)


# ── throttle protection ───────────────────────────────────
def test_soft_pace_curve():
    assert wb.soft_pace_delay(4000, 4000) == 0.0
    assert wb.soft_pace_delay(400, 4000) == 0.0          # at the low-water mark
    mid = wb.soft_pace_delay(200, 4000)
    assert 0 < mid < wb.SOFT_PACE_MAX_DELAY
    assert wb.soft_pace_delay(0, 4000) == wb.SOFT_PACE_MAX_DELAY
    assert wb.soft_pace_delay("x", 4000) == 0.0


def test_update_rate_limit_sets_soft_pace():
    class R:
        headers = {"x-ratelimit-limit": "4000", "x-ratelimit-remaining": "100"}
    wb._update_rate_limit(R())
    assert wb.state.soft_pace_delay > 0
    R.headers = {"x-ratelimit-limit": "4000", "x-ratelimit-remaining": "3900"}
    wb._update_rate_limit(R())
    assert wb.state.soft_pace_delay == 0.0


def test_autostart_global_pace_spaces_lanes(monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "AUTOSTART_LOCK_PATH", str(tmp_path / "as.lock"))
    monkeypatch.setattr(wb, "AUTO_START_MIN_INTERVAL", 0.15)
    wb.state.lanes_count = 3
    stamps = []
    lock = threading.Lock()

    def lane():
        wb._autostart_global_pace()
        with lock:
            stamps.append(time.time())

    threads = [threading.Thread(target=lane) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.12 for g in gaps), gaps


def test_autostart_pace_noop_single_lane(monkeypatch, tmp_path):
    monkeypatch.setattr(wb, "AUTOSTART_LOCK_PATH", str(tmp_path / "as.lock"))
    wb.state.lanes_count = 1
    t0 = time.time()
    for _ in range(5):
        wb._autostart_global_pace()
    assert time.time() - t0 < 0.05
    assert not os.path.exists(tmp_path / "as.lock")


def test_recommend_lanes():
    rows = {2: {"rps": 50, "s429": 0}, 3: {"rps": 70, "s429": 0}, 4: {"rps": 75, "s429": 0}}
    assert wb.recommend_lanes(28, rows) == 3
    assert wb.recommend_lanes(28, {2: {"rps": 50, "s429": 3}}) == 1
    assert wb.recommend_lanes(28, {2: {"rps": 30, "s429": 0}}) == 1


# ── DB lane tag, stats + analytics ────────────────────────
def _seed(monkeypatch, tmp_path):
    db = str(tmp_path / "lanes.db")
    monkeypatch.setattr(wb, "DB_PATH", db)
    from core.database import init_db, migrate_db
    init_db(db)
    migrate_db(db)
    conn = sqlite3.connect(db)
    for i, lane in ((1, 1), (2, 2), (3, 2), (4, None)):
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at, currency, game, strategy, base_bet, "
            "multiplier, total_bets, wins, losses, profit, wagered, lane) "
            "VALUES (?,?,?,'usdt','dice-auto','Martingale',0.0001,2.0,4,2,2,0.0001,0.001,?)",
            (i, f"2026-11-13T1{i}:00:00", f"2026-11-13T1{i}:00:10", lane))
    conn.commit()
    conn.close()
    return db


def test_stats_lane_filter_and_tag(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path)
    wb.cmd_stats(lane=2)
    out = capsys.readouterr().out
    assert "Session #2" in out and "Session #3" in out
    assert "Session #1" not in out and "Session #4" not in out
    assert "[L2]" in out and "lane 2" in out
    wb.cmd_stats(lane=1)                       # NULL lane counts as lane 1
    out = capsys.readouterr().out
    assert "Session #1" in out and "Session #4" in out and "Session #2" not in out


def test_analytics_by_lane(monkeypatch, tmp_path, capsys):
    _seed(monkeypatch, tmp_path)
    wb.cmd_analytics()
    out = capsys.readouterr().out
    assert "By lane" in out and "Lane 2" in out


def test_db_start_session_tags_lane(monkeypatch, tmp_path):
    db = _seed(monkeypatch, tmp_path)
    wb.state.lane = 3
    sid = wb._db_start_session()
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT lane FROM sessions WHERE id=?", (sid,)).fetchone()[0] == 3
    conn.close()


def test_lanes_status_table(monkeypatch, tmp_path, capsys):
    base = str(tmp_path / "live.json")
    monkeypatch.setattr(wb, "_BASE_STATE_PATH", base)
    for k, p in ((1, 0.001), (2, -0.0005)):
        with open(wb._lane_path(base, k), "w") as f:
            json.dump({"pid": os.getpid(), "session_id": 10 + k, "uptime_sec": 5,
                       "run_uptime_sec": 3600, "profit": p, "lane_equity": 0.5,
                       "bets_per_second": 20.0, "soft_pace_delay": 0.0}, f)
    wb.cmd_lanes_status()
    out = capsys.readouterr().out
    assert "#11" in out and "#12" in out
    assert "40.0" in out and "lanes live 2/2" in out


# ── wolfctl + systemd wiring ──────────────────────────────
def _src(name):
    return open(os.path.join(_WOLFBET_DIR, name)).read()


def test_wolfctl_lane_commands():
    s = _src("wolfctl")
    assert 'cmd_lanes "$@"' in s
    assert "--lane %i" in s
    assert '"$PYTHON" "$BOT" --monitor --lane "$k"' in s
    assert "--lanes-status" in s
    assert "stop_all_lanes" in s and "sync_lanes" in s
    assert "WOLFCTL_VERSION=3.20" in s


def test_install_writes_lane_template():
    s = _src("install.sh")
    assert '${SERVICE_NAME}-lane@.service' in s
    assert "wolfbet.py --resume --daemon --lane %i" in s


# ── v2.36.0: configurable lane split ──────────────────────
def _load_lane(monkeypatch, tmp_path, lane, lanes_block, **extra):
    path = _config(monkeypatch, tmp_path, {**BASE, **extra, "lanes": lanes_block})
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    wb.state.lane = lane
    assert wb.load_config()
    return path


def test_split_default_none_uses_full_bet(monkeypatch, tmp_path):
    _load_lane(monkeypatch, tmp_path, 2, {"count": 2})
    assert wb.state.lane_split == "none"
    assert wb.state.base_bet == 0.0001 and wb.state.current_bet == 0.0001


def test_split_invalid_mode_falls_back_to_none(monkeypatch, tmp_path):
    _load_lane(monkeypatch, tmp_path, 2, {"count": 2, "split": "half"})
    assert wb.state.lane_split == "none" and wb.state.base_bet == 0.0001


@pytest.mark.parametrize("mode,expected", [("budget", 0.0001), ("bet", 0.00005), ("both", 0.00005)])
def test_split_modes_base_bet(monkeypatch, tmp_path, mode, expected):
    for lane in (1, 2):
        _load_lane(monkeypatch, tmp_path, lane, {"count": 2, "split": mode})
        assert wb.state.base_bet == pytest.approx(expected)
        assert wb.state.current_bet == pytest.approx(expected)


def test_split_bet_ignored_with_one_lane(monkeypatch, tmp_path):
    _load_lane(monkeypatch, tmp_path, 1, {"count": 1, "split": "both"})
    assert wb.state.base_bet == 0.0001


def test_split_bet_keeps_lane_base_bet_override(monkeypatch, tmp_path):
    _load_lane(monkeypatch, tmp_path, 2, {"count": 2, "split": "bet",
                                          "overrides": {"2": {"base_bet": 0.0003}}})
    assert wb.state.base_bet == pytest.approx(0.0003)


def test_split_bet_rounding_to_zero_keeps_full(monkeypatch, tmp_path):
    _load_lane(monkeypatch, tmp_path, 2, {"count": 3, "split": "bet"}, base_bet=0.00000001)
    assert wb.state.base_bet == 0.00000001


def test_split_bet_save_config_writes_full_bet(monkeypatch, tmp_path):
    path = _load_lane(monkeypatch, tmp_path, 1, {"count": 2, "split": "bet"})
    assert wb.state.base_bet == pytest.approx(0.00005)
    wb.state.max_wins = 9
    wb.save_config()                                  # untouched split bet → full on disk
    data = json.load(open(path))
    assert data["base_bet"] == 0.0001 and data["max_wins"] == 9
    assert data["lanes"]["split"] == "bet"
    wb.load_config()
    assert wb.state.base_bet == pytest.approx(0.00005)  # never halves twice
    wb.state.base_bet = 0.0004                        # user changed it → saved as-is
    wb.save_config()
    assert json.load(open(path))["base_bet"] == 0.0004


def test_split_basebet_pct_divided(monkeypatch):
    wb.state.lanes_count, wb.state.lane_split = 2, "bet"
    wb.state.current_balance, wb.state.basebet_pct, wb.state.base_bet = 10.0, 0.001, 0.0
    assert wb._apply_basebet_pct()
    assert wb.state.base_bet == pytest.approx(0.005)
    wb.state.lane_split = "none"
    assert wb._apply_basebet_pct()
    assert wb.state.base_bet == pytest.approx(0.01)


def test_split_live_reload_no_spurious_base_bet_warning(monkeypatch, tmp_path, caplog):
    _load_lane(monkeypatch, tmp_path, 2, {"count": 2, "split": "bet"})
    with caplog.at_level("WARNING", logger=wb.logger.name):
        wb._reload_live_config()
    assert "base_bet" not in caplog.text
    assert wb.state.base_bet == pytest.approx(0.00005)


def test_config_set_lane_split(monkeypatch, tmp_path, capsys):
    path = _config(monkeypatch, tmp_path, dict(BASE))
    monkeypatch.setattr(wb, "_apply_proxy_to_session", lambda: None)
    monkeypatch.setattr(wb, "_signal_lane_daemons", lambda *a, **k: 0)
    wb.state.lane = 1
    wb.cmd_config_set(["lanes=2", "lane_split=both"])
    data = json.load(open(path))
    assert data["lanes"] == {"count": 2, "overrides": {}, "split": "both"}
    assert data["base_bet"] == 0.0001
    out = capsys.readouterr().out
    assert "lane_split" in out and "both" in out
    with pytest.raises(SystemExit) as e:
        wb.cmd_config_set(["lane_split=half"])
    assert e.value.code == 2


def test_config_show_prints_split(monkeypatch, tmp_path, capsys):
    _load_lane(monkeypatch, tmp_path, 1, {"count": 2, "split": "budget"})
    wb.cmd_config_show()
    out = capsys.readouterr().out
    assert "split: budget" in out and "balance ÷ lanes" in out


def test_wolfctl_lanes_split_option():
    s = open(os.path.join(_WOLFBET_DIR, "wolfctl")).read()
    assert "--split" in s and '"lane_split=$split"' in s
    assert 'lanes)        shift; cmd_lanes "$@" ;;' in s

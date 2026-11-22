"""Phase 5d tests - non-interactive config CLI (--config-show / --config-set).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5d_config_cli.py -q
"""

import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


# ── _parse_config_set_pairs ───────────────────────────────
def test_parse_kv_canonical():
    out = wb._parse_config_set_pairs(["max_profit=0.005"])
    assert out == {"max_profit": "0.005"}


def test_parse_kv_alias_resolved():
    out = wb._parse_config_set_pairs(["maxprofit=0.005", "delay=0.35", "pipeline=3"])
    assert out == {"max_profit": "0.005", "bet_delay": "0.35", "pipeline_depth": "3"}


def test_parse_kv_case_insensitive_keys():
    out = wb._parse_config_set_pairs(["MaxProfit=0.005"])
    assert "max_profit" in out


def test_parse_kv_malformed_raises():
    for bad in ["nokey", "=nova", "malformed:pair"]:
        try:
            wb._parse_config_set_pairs([bad])
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_parse_kv_unknown_key_raises():
    try:
        wb._parse_config_set_pairs(["never_a_real_field=1"])
    except ValueError as e:
        assert "Unknown config field" in str(e)
        return
    raise AssertionError("expected ValueError for unknown key")


def test_parse_kv_multiple_pairs():
    out = wb._parse_config_set_pairs(["max_profit=0.005", "max_loss=0.001", "base_bet=0.0002"])
    assert len(out) == 3
    assert out["max_profit"] == "0.005"
    assert out["max_loss"] == "0.001"


# ── _fmt_config_value ─────────────────────────────────────
def test_fmt_value_none_is_off():
    assert "off" in wb._fmt_config_value("max_profit", None)


def test_fmt_value_bool_true():
    assert "on" in wb._fmt_config_value("recurring", True)


def test_fmt_value_bool_false():
    assert "off" in wb._fmt_config_value("recurring", False)


def test_fmt_value_streak_tuple():
    assert wb._fmt_config_value("streak_delay_loss", (3, 0.5)) == "3:0.5"


def test_fmt_value_streak_tuple_zero_is_off():
    assert "off" in wb._fmt_config_value("streak_delay_loss", (0, 0.0))


def test_fmt_value_float_precision():
    assert wb._fmt_config_value("base_bet", 0.0001) == "0.00010000"


# ── End-to-end via subprocess ─────────────────────────────
def _run_cli(*args, env=None):
    """Run wolfbet.py CLI with the given args, return (rc, stdout, stderr)."""
    cmd = [sys.executable, os.path.join(_WOLFBET_DIR, "wolfbet.py"), *args]
    e = os.environ.copy()
    e["APP_ENV"] = "testing"
    if env:
        e.update(env)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=e)
    return proc.returncode, proc.stdout, proc.stderr


def _seed_config_file(path: str, extra: dict = None) -> None:
    data = {
        "api_key": "test-key-abcdefghijkl",
        "currency": "usdt",
        "game": "dice",
        "rule": "over",
        "multiplier": 2.01,
        "bet_value": 50.49,
        "base_bet": 0.0001,
        "strategy": "Flat Bet",
        "strategy_key": "1",
        "win_mult": 1.0,
        "loss_mult": 2.0,
        "bet_delay": 0.0,
    }
    if extra:
        data.update(extra)
    with open(path, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)


def test_config_show_masks_api_key():
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "wolfbet.json")
        _seed_config_file(cfg)
        rc, out, err = _run_cli("--config-show", env={"WOLFBET_CONFIG_PATH": cfg,
                                                        "WOLFBET_DB_PATH": os.path.join(td, "db.sqlite")})
        assert rc == 0, err
        # Full raw key must NOT appear.
        assert "test-key-abcdefghijkl" not in out
        # Masked form should appear (first 8 + last 4).
        assert "test-key" in out and "ijkl" in out


def test_config_show_no_config_exits_nonzero():
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "wolfbet.json")   # does not exist
        rc, out, err = _run_cli("--config-show", env={"WOLFBET_CONFIG_PATH": cfg,
                                                        "WOLFBET_DB_PATH": os.path.join(td, "db.sqlite")})
        assert rc != 0
        assert "No config saved" in out


def test_config_set_writes_to_disk():
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "wolfbet.json")
        _seed_config_file(cfg)
        rc, out, err = _run_cli("--config-set", "max_profit=0.005", "base_bet=0.0002",
                                env={"WOLFBET_CONFIG_PATH": cfg,
                                     "WOLFBET_DB_PATH": os.path.join(td, "db.sqlite")})
        assert rc == 0, err
        with open(cfg) as f:
            data = json.load(f)
        assert data["max_profit"] == 0.005
        assert abs(data["base_bet"] - 0.0002) < 1e-9


def test_config_set_api_key_refused():
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "wolfbet.json")
        _seed_config_file(cfg)
        rc, out, err = _run_cli("--config-set", "api_key=NEWKEY",
                                env={"WOLFBET_CONFIG_PATH": cfg,
                                     "WOLFBET_DB_PATH": os.path.join(td, "db.sqlite")})
        assert rc == 2
        assert "WOLFBET_API_KEY" in out or "WOLFBET_API_KEY" in err
        # Config file must NOT have been rewritten with the new key.
        with open(cfg) as f:
            data = json.load(f)
        assert data["api_key"] == "test-key-abcdefghijkl"


def test_config_set_unknown_key_refused():
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "wolfbet.json")
        _seed_config_file(cfg)
        rc, out, err = _run_cli("--config-set", "nonsense_field=1",
                                env={"WOLFBET_CONFIG_PATH": cfg,
                                     "WOLFBET_DB_PATH": os.path.join(td, "db.sqlite")})
        assert rc == 2
        assert "Unknown config field" in out or "Unknown config field" in err


def test_config_set_malformed_pair_refused():
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "wolfbet.json")
        _seed_config_file(cfg)
        rc, out, err = _run_cli("--config-set", "no_equals_sign",
                                env={"WOLFBET_CONFIG_PATH": cfg,
                                     "WOLFBET_DB_PATH": os.path.join(td, "db.sqlite")})
        assert rc == 2
        assert "Malformed" in out or "Malformed" in err


def test_help_epilog_contains_examples():
    rc, out, err = _run_cli("--help")
    assert rc == 0
    assert "Examples:" in out
    assert "--config-show" in out
    assert "--config-set" in out
    assert "--pipeline" in out
    assert "--backtest" in out


def test_version_flag():
    rc, out, err = _run_cli("--version")
    assert rc == 0
    assert wb.VERSION in out


# ── game is settable from config-set ──────────────────────
def _reset_mutated_state():
    """State every mutation test starts from; session rules off so no warnings fire."""
    s = wb.state
    s.game = "dice-auto"
    s.rule = "over"
    s.multiplier = 2.0
    s.bet_value = 50.49
    s._auto_uuid = None
    s.max_loss = None
    s.max_profit = None
    s.hard_cap = False
    s.restart_after_loss = False
    s.balance_goal = None
    s.hybrid_recover_at = None
    s.hybrid_press_at = None


def test_parse_kv_game_is_known():
    assert wb._parse_config_set_pairs(["game=dice"]) == {"game": "dice"}


def test_mutation_accepts_every_game():
    for game in sorted(wb.GAME_ENDPOINTS):
        _reset_mutated_state()
        msgs = wb._apply_mutation({"game": game})
        assert wb.state.game == game
        assert any("restart to apply" in m for m in msgs)


def test_mutation_refuses_an_unknown_game():
    _reset_mutated_state()
    try:
        wb._apply_mutation({"game": "slots"})
    except ValueError as e:
        assert "game must be one of" in str(e)
        assert "slots" in str(e)
    else:
        raise AssertionError("expected ValueError for an unknown game")
    assert wb.state.game == "dice-auto"


def test_mutation_clears_the_auto_bet_cycle_it_leaves_behind():
    _reset_mutated_state()
    wb.state.game = "dice-auto"
    wb.state._auto_uuid = "live"
    msgs = wb._apply_mutation({"game": "dice"})
    assert wb.state._auto_uuid is None
    assert any("Auto-bet UUID cleared" in m for m in msgs)


def test_mutation_leaves_the_cycle_alone_when_it_changes_nothing():
    _reset_mutated_state()
    wb.state.game = "dice"
    wb.state._auto_uuid = None
    msgs = wb._apply_mutation({"game": "dice"})
    assert wb.state.game == "dice"
    assert not any("Auto-bet UUID" in m for m in msgs)


def test_game_needs_a_restart_to_apply():
    assert "game" not in wb._LIVE_RELOAD_KEYS


def test_game_and_rule_may_differ_per_lane():
    assert "game" not in wb._LANE_FORBIDDEN_KEYS
    assert "rule" not in wb._LANE_FORBIDDEN_KEYS


# ── the opening strategy is settable from config-set ──────
def test_mutation_sets_the_opening_strategy():
    _reset_mutated_state()
    wb.state.strategy_key = "2"
    wb.state.strategy = "Martingale"
    wb.state.dalembert_unit = 3
    wb.state.paroli_count = 2
    msgs = wb._apply_mutation({"strategy_key": "1"})
    assert (wb.state.strategy_key, wb.state.strategy) == ("1", "Flat Bet")
    assert (wb.state.dalembert_unit, wb.state.paroli_count) == (0, 0)
    assert any("restart to apply" in m for m in msgs)


def test_strategy_alias_resolves_to_the_key():
    assert wb._parse_config_set_pairs(["strategy=4"]) == {"strategy_key": "4"}
    _reset_mutated_state()
    wb._apply_mutation({"strategy_key": "4"})
    assert wb.state.strategy == "D'Alembert"


def test_mutation_refuses_an_unknown_strategy():
    _reset_mutated_state()
    wb.state.strategy_key = "2"
    try:
        wb._apply_mutation({"strategy_key": "9"})
    except ValueError as e:
        assert "strategy_key must be one of" in str(e)
    else:
        raise AssertionError("expected ValueError for strategy_key 9")
    assert wb.state.strategy_key == "2"


def test_strategy_change_clears_a_live_auto_bet_cycle():
    _reset_mutated_state()
    wb.state._auto_uuid = "live"
    wb._apply_mutation({"strategy_key": "1"})
    assert wb.state._auto_uuid is None


def test_strategy_needs_a_restart_to_apply():
    assert "strategy_key" not in wb._LIVE_RELOAD_KEYS

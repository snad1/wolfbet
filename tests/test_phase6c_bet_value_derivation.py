"""Phase 6c: bet_value derivation + auto-correct on load / config-set (v2.30.8).

Real-world outage: `wolfctl start` hit `HTTP 422` and placed zero bets.
Root cause: on-disk config held `multiplier=10` + stale `bet_value=50.49`
(leftover from old `multiplier=2` configs). Server replied
"Incorrect win chance given". `load_config` + `wolfctl config-set multiplier`
never recomputed `bet_value`; auto games never hit `/bet` so the in-line
self-correction code path didn't help either.

Fixes:
  - New `_derive_bet_value(multiplier, rule)` helper: single source of truth.
  - `load_config()` re-derives on load when disk value mismatches.
  - `_apply_mutation({"multiplier": ...})` re-derives when no explicit
    `bet_value` in the same call.
  - `api_auto_start` includes the WolfBet response body in `state.last_error`
    so operators self-diagnose via `wolfctl status` instead of log-trawling.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase6c_bet_value_derivation.py -q
"""

import inspect
import json
import os
import re
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


# ── VERSION ──────────────────────────────────────────────
def test_version_bumped():
    assert wb.VERSION == "2.52.0"


# ── _derive_bet_value ───────────────────────────────────
def test_derive_bet_value_over_10x():
    assert wb._derive_bet_value(10, "over") == 90.09


def test_derive_bet_value_over_2x():
    assert wb._derive_bet_value(2, "over") == 50.49


def test_derive_bet_value_over_2_01x():
    # 99/2.01 = 49.2537...; over → 100 - 49.2537 - 0.01 = 50.7362 → 50.74.
    assert wb._derive_bet_value(2.01, "over") == 50.74


def test_derive_bet_value_under_10x():
    assert wb._derive_bet_value(10, "under") == 9.9


def test_derive_bet_value_zero_multiplier_safe_default():
    """No ZeroDivisionError: returns the wizard's safe-default."""
    assert wb._derive_bet_value(0, "over") == 50.74


# ── load_config auto-corrects stale bet_value ───────────
def _write_config(monkeypatch, tmp_path, **overrides):
    """Write a fake config file and point wolfbet at it."""
    cfg_path = str(tmp_path / "wb_cfg.json")
    monkeypatch.setattr(wb, "CONFIG_PATH", cfg_path)
    base = {
        "currency": "usdt", "game": "dice-auto", "rule": "over",
        "multiplier": 10, "bet_value": 50.49,          # ← STALE
        "base_bet": 0.0001, "api_key": "",
    }
    base.update(overrides)
    with open(cfg_path, "w") as f:
        json.dump(base, f)
    return cfg_path


def test_load_config_corrects_stale_bet_value(monkeypatch, tmp_path):
    """v2.30.8: disk says bet_value=50.49; multiplier=10 means it should
    be 90.09 after load."""
    _write_config(monkeypatch, tmp_path)
    assert wb.load_config() is True
    assert wb.state.multiplier == 10
    assert wb.state.bet_value == 90.09


def test_load_config_preserves_matching_bet_value(monkeypatch, tmp_path):
    """When disk value is already correct, load_config doesn't touch it."""
    _write_config(monkeypatch, tmp_path, bet_value=90.09)
    assert wb.load_config() is True
    assert wb.state.bet_value == 90.09


def test_load_config_re_derives_for_under_rule(monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, rule="under", bet_value=50.49)
    assert wb.load_config() is True
    assert wb.state.bet_value == 9.9


# ── _apply_mutation re-derives on multiplier change ─────
def test_apply_mutation_multiplier_re_derives_bet_value():
    """Going from multiplier=2 (bet_value=50.49) to multiplier=10 must
    re-derive to 90.09 automatically."""
    wb.state.multiplier = 2
    wb.state.rule = "over"
    wb.state.bet_value = 50.49
    msgs = wb._apply_mutation({"multiplier": 10})
    assert wb.state.multiplier == 10
    assert wb.state.bet_value == 90.09
    assert any("bet_value re-derived" in m for m in msgs)


def test_apply_mutation_explicit_bet_value_wins():
    """If the operator explicitly passes both multiplier AND bet_value in
    the same call, bet_value override wins (no silent re-derive)."""
    wb.state.multiplier = 2
    wb.state.rule = "over"
    wb.state.bet_value = 50.49
    msgs = wb._apply_mutation({"multiplier": 10, "bet_value": 12.34})
    assert wb.state.multiplier == 10
    assert wb.state.bet_value == 12.34
    assert not any("re-derived" in m for m in msgs)


def test_apply_mutation_multiplier_in_range_no_re_derive_msg():
    """When the new multiplier happens to produce the same bet_value (e.g.
    setting it to its current value), no spurious re-derive message."""
    wb.state.multiplier = 10
    wb.state.rule = "over"
    wb.state.bet_value = 90.09
    msgs = wb._apply_mutation({"multiplier": 10})
    assert wb.state.bet_value == 90.09
    assert not any("re-derived" in m for m in msgs)


# ── api_auto_start surfaces body in last_error ──────────
def test_api_auto_start_includes_body_snippet_in_error():
    """v2.30.8: grep-based source check: state.last_error must include a
    body snippet so operators self-diagnose from `wolfctl status`."""
    src = inspect.getsource(wb.api_auto_start)
    assert "body_snip" in src
    # Format: f"...: {body_snip}" appended conditionally.
    assert re.search(r'\+\s*\(f": \{body_snip\}"\s+if\s+body_snip', src)


# ── Source markers ──────────────────────────────────────
def test_derive_bet_value_helper_defined_at_module_scope():
    src = inspect.getsource(wb)
    assert re.search(r"^def _derive_bet_value\(multiplier:", src, re.MULTILINE)


def test_load_config_calls_derive_helper():
    src = inspect.getsource(wb.load_config)
    assert "_derive_bet_value" in src
    assert "mismatched derived value" in src or "derived_bv" in src


def test_apply_mutation_calls_derive_helper():
    src = inspect.getsource(wb._apply_mutation)
    assert "_derive_bet_value" in src
    assert "bet_value re-derived" in src

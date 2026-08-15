"""Phase 6a — silent-clamp visibility + raised recurring_delay cap (v2.30.6).

Real-world report: `wolfctl config-set recurring_delay=10800` panel showed
`recurring_delay: 3600 → 3600` — the 3600 s cap silently swallowed the
operator's input (10800 = 3 h). Investigation confirmed the cap was an
arbitrary TG-mirror, not a technical limit (time.sleep(86400) is fine).

Fixes:
  - Cap raised from 3600 to 86400 (24 h) in _apply_mutation + wizard + doc.
  - New _clamped() helper appends a ⚠ warning to msgs when clamp actually
    fires; wired into recurring_delay, purge_days, pipeline_depth.
  - cmd_config_set captures msgs and prints ⚠ lines alongside the diff.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase6a_clamp_visibility.py -q
"""

import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


# ── VERSION ──────────────────────────────────────────────
def test_version_bumped():
    assert wb.VERSION == "2.31.0"


# ── recurring_delay accepts values previously clamped ────
def test_recurring_delay_10800_survives_apply_mutation():
    """3-hour pause — the user's exact real-world scenario."""
    wb.state.recurring_delay = 60.0
    msgs = wb._apply_mutation({"recurring_delay": 10800})
    assert wb.state.recurring_delay == 10800.0
    assert not any(m.startswith("⚠") for m in msgs), \
        f"unexpected clamp warning: {msgs}"


def test_recurring_delay_86400_survives_apply_mutation():
    """24-hour pause — the new cap boundary."""
    wb.state.recurring_delay = 60.0
    msgs = wb._apply_mutation({"recurring_delay": 86400})
    assert wb.state.recurring_delay == 86400.0
    assert not any(m.startswith("⚠") for m in msgs)


def test_recurring_delay_over_cap_clamps_with_warning():
    """Over 86400 (24 h) → clamp to 86400 AND raise the warning."""
    wb.state.recurring_delay = 60.0
    msgs = wb._apply_mutation({"recurring_delay": 100000})
    assert wb.state.recurring_delay == 86400.0
    warnings = [m for m in msgs if m.startswith("⚠")]
    assert warnings, "no clamp warning surfaced"
    assert "100000" in warnings[0]
    assert "86400" in warnings[0]
    assert "recurring_delay" in warnings[0]


def test_recurring_delay_negative_clamps_to_zero_with_warning():
    wb.state.recurring_delay = 60.0
    msgs = wb._apply_mutation({"recurring_delay": -10})
    assert wb.state.recurring_delay == 0.0
    assert any(m.startswith("⚠") and "recurring_delay" in m for m in msgs)


# ── purge_days visibility ────────────────────────────────
def test_purge_days_50_clamps_to_30_with_warning():
    wb.state.purge_days = 3
    msgs = wb._apply_mutation({"purge_days": 50})
    assert wb.state.purge_days == 30
    warnings = [m for m in msgs if m.startswith("⚠")]
    assert warnings and "purge_days" in warnings[0]
    assert "50" in warnings[0] and "30" in warnings[0]


def test_purge_days_in_range_no_warning():
    wb.state.purge_days = 3
    msgs = wb._apply_mutation({"purge_days": 7})
    assert wb.state.purge_days == 7
    assert not any(m.startswith("⚠") for m in msgs)


# ── pipeline_depth visibility ────────────────────────────
def test_pipeline_depth_20_clamps_to_8_with_warning():
    wb.state.pipeline_depth = 1
    msgs = wb._apply_mutation({"pipeline_depth": 20})
    assert wb.state.pipeline_depth == 8
    warnings = [m for m in msgs if m.startswith("⚠")]
    assert warnings and "pipeline_depth" in warnings[0]
    assert "20" in warnings[0] and "8" in warnings[0]


def test_pipeline_depth_in_range_no_warning():
    wb.state.pipeline_depth = 1
    msgs = wb._apply_mutation({"pipeline_depth": 3})
    assert wb.state.pipeline_depth == 3
    assert not any(m.startswith("⚠") for m in msgs)


# ── Source markers: helper exists + cmd_config_set surfaces msgs ─
def test_clamped_helper_source_present():
    """v2.30.6 — the _clamped helper lives inside _apply_mutation and is the
    single source of truth for the three clamp-and-warn sites."""
    src = inspect.getsource(wb._apply_mutation)
    assert "def _clamped(" in src
    # Site calls may be line-split; check for the field names as _clamped's
    # first positional arg via a robust substring pattern.
    assert '"recurring_delay"' in src and "_clamped(" in src
    assert '_clamped("purge_days"' in src
    assert '_clamped("pipeline_depth"' in src


def test_cmd_config_set_captures_and_prints_warnings():
    src = inspect.getsource(wb.cmd_config_set)
    # mutation_msgs captured (was: discarded return value in v2.30.5).
    assert "mutation_msgs" in src
    # Warnings surfaced via console.print.
    assert 'm.startswith("⚠")' in src


# ── Doc comment updated ──────────────────────────────────
def test_recurring_delay_doc_reflects_new_cap():
    src = inspect.getsource(wb)
    assert "cap 86400" in src

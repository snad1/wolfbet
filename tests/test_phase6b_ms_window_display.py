"""Phase 6b: CFG-row Ms shows progress within the current threshold window (v2.30.7).

Real-world report: monitor showed `Ms +1.54943550/1.94295672 (80%)` with
profit_threshold=0.64765224. The 80% was cumulative (profit / next_ms) -
technically true but not what an operator asking "how close to the next
bump?" would expect. After 2 bumps, next_ms had climbed to 3×threshold,
so the cumulative pct doesn't reset per bump: confusing.

v2.30.7: denominator is now one profit_threshold unit; numerator is
progress within the current window (profit - (next_ms - threshold)).
Pct resets to 0% after each bump.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase6b_ms_window_display.py -q
"""

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s).replace("\x1b[K", "")


def _row(profit, profit_threshold, next_profit_milestone=None, **extra):
    d = {
        "profit": profit,
        "profit_threshold": profit_threshold,
        "profit_increment": 0.00000001,
    }
    if next_profit_milestone is not None:
        d["next_profit_milestone"] = next_profit_milestone
    d.update(extra)
    return _strip_ansi(wb._build_config_progress_row(d))


# ── User's exact real-world scenario ─────────────────────
def test_reproduces_user_scenario_39pct_not_80pct():
    """User's config: threshold=0.64765224, next_ms=1.94295672 (after 2 bumps),
    profit=1.54943550. Old display: (80%). New display: (39%)."""
    row = _row(profit=1.54943550,
               profit_threshold=0.64765224,
               next_profit_milestone=1.94295672)
    # Denominator = one threshold unit (was next_ms = 1.94295672 pre-v2.30.7).
    assert "/0.64765224" in row
    # window_start = 1.94295672 - 0.64765224 = 1.29530448
    # window_progress = 1.54943550 - 1.29530448 = 0.25413102
    # pct = 0.25413102 / 0.64765224 = 39.24%
    assert "(39%)" in row
    assert "+0.25413102" in row


# ── Fresh session (no bumps yet): no regression from phase5t ────
def test_fresh_session_no_bumps_still_shows_progress():
    row = _row(profit=0.0005, profit_threshold=0.001)  # no next_ms → defaults to threshold
    # window_start = 0.001 - 0.001 = 0; window_progress = 0.0005; pct = 50%.
    assert "(50%)" in row
    assert "/0.00100000" in row


# ── Just after a bump: pct near 0 ────────────────────────
def test_just_after_bump_shows_near_zero_pct():
    """Simulate: bump just fired, next_ms advanced to 2×threshold, profit
    only barely past the last milestone."""
    row = _row(profit=0.010001,
               profit_threshold=0.010,
               next_profit_milestone=0.020)  # next window starts at 0.010
    # window_progress = 0.010001 - 0.010 = 0.000001; pct ≈ 0%.
    assert "(0%)" in row


# ── Just before the next bump: pct near 100 ──────────────
def test_just_before_bump_shows_near_hundred_pct():
    row = _row(profit=0.01999,
               profit_threshold=0.010,
               next_profit_milestone=0.020)
    # window_start = 0.010; window_progress = 0.00999; pct = 99.9%.
    assert "(99%)" in row or "(100%)" in row


# ── Denominator is always profit_threshold (constant), not next_ms ──
def test_denominator_always_profit_threshold():
    for next_ms in [0.007, 0.014, 0.021, 0.049, 1.943]:
        row = _row(profit=0.001, profit_threshold=0.007,
                   next_profit_milestone=next_ms)
        assert "/0.00700000" in row, \
            f"denominator should always be threshold (0.00700000), got row: {row}"


# ── Backwards-compat: no next_profit_milestone key (pre-v2.30.1 state) ──
def test_pre_v2_30_1_state_file_still_renders():
    """Pre-v2.30.1 state files never persisted next_profit_milestone. The
    fallback pins next_ms to profit_threshold, so window_start = 0 and
    display collapses to `profit / threshold`: the natural first-window
    reading. Preserves phase5t/5u behaviour."""
    row = _row(profit=0.0035, profit_threshold=0.007)
    assert "/0.00700000" in row
    assert "(50%)" in row


# ── Segment elided when threshold not set ────────────────
def test_segment_elided_when_threshold_missing():
    d = {"profit": 0.5, "profit_increment": 0.00000001}
    row = _strip_ansi(wb._build_config_progress_row(d))
    assert "Ms " not in row


# ── VERSION ──────────────────────────────────────────────
def test_version_bumped():
    assert wb.VERSION == "2.41.0"

"""Phase 5w: metric correctness (v2.30.3).

Fixes three defects surfaced from live 18h/1M-bet server data:
  A) _record_cycle_ms mixed smoothed cycle EMA with UNSMOOTHED _last_api_ms,
     making overhead swing 0↔peak between consecutive display reads.
  B) cmd_diagnose printed "api_p50 + overhead + delay = cycle_ms" but the
     three components come from different windows; the '=' was a lie.
  C) cmd_status "BPS ceiling" = 1000/cycle_ms is wrong for auto-games (server
     batches many bets per outer iteration) and disagreed with cmd_diagnose's
     Theoretical max.
  D) _compute_theoretical_max_bps used all-time api_avg: 18h sessions biased
     by cold-start samples.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5w_metric_correctness.py -q
"""

import inspect
import os
import re
import sys
from collections import deque

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _reset_metric_state():
    """Fresh metric fields per test."""
    wb.state._api_samples = deque(maxlen=500)
    wb.state._api_ms_total = 0.0
    wb.state._api_ms_count = 0
    wb.state._last_api_ms = 0.0
    wb.state._cycle_ms = 0.0
    wb.state._cycle_overhead_ms = 0.0
    wb.state.bet_delay = 0.0


# ── Version ──────────────────────────────────────────────
def test_version_bumped():
    assert wb.VERSION == "2.65.1"


# ── Defect A: overhead stable across single-sample perturbation ─
def test_overhead_stable_when_single_sample_spikes():
    _reset_metric_state()
    # Warm the p50 window with 50 samples around 40 ms.
    for ms in [40.0] * 50:
        wb._record_api_ms(ms)
    # Prime the cycle EMA at ~65 ms (40 api + 25 overhead-ish).
    for _ in range(30):
        wb._record_cycle_ms(65.0)
    baseline_overhead = wb.state._cycle_overhead_ms
    assert 20 < baseline_overhead < 30, f"expected ~25 baseline, got {baseline_overhead}"

    # Now feed ONE unusually slow API sample (300 ms).
    wb._record_api_ms(300.0)
    wb._record_cycle_ms(65.0)
    perturbed = wb.state._cycle_overhead_ms
    # Overhead must NOT slam to 0 (previous bug). p50 barely moves; overhead
    # stays within a few ms of baseline.
    assert perturbed > 10, (
        f"overhead should stay near baseline after single slow sample, "
        f"got {perturbed} (baseline {baseline_overhead})")


def test_overhead_falls_back_to_last_api_ms_when_deque_empty():
    """No API samples yet → no p50 available → falls back to _last_api_ms.

    v2.30.4: also requires the cycle deque to be reset alongside so the
    first sample IS the median (test invariant unchanged from v2.30.3)."""
    _reset_metric_state()
    from collections import deque
    wb.state._cycle_samples = deque(maxlen=500)
    wb.state._last_api_ms = 50.0
    wb._record_cycle_ms(80.0)
    # overhead = cycle_p50 (80) - api_ref (50) - delay*1000 (0) = 30
    assert abs(wb.state._cycle_overhead_ms - 30.0) < 1e-9


def test_record_cycle_ms_source_uses_p50():
    src = inspect.getsource(wb._record_cycle_ms)
    assert "_compute_api_p50()" in src
    assert "state._api_samples" in src  # the fallback guard


# ── Defect B: diagnose uses ≈, not = ──────────────────────
def test_diagnose_uses_approx_not_equals():
    src = inspect.getsource(wb.cmd_diagnose)
    # New line uses ≈ and includes a "Measured cycle" companion.
    assert "≈" in src
    assert "Measured cycle" in src
    assert "decomposition" in src
    # v2.30.4: "outer-iteration EMA" is retired; label now reads "median of last N iterations".
    assert "median of last" in src
    # Old fake-equality line is gone.
    assert "= [magenta]{cycle_ms:.0f}ms[/]/bet" not in src


# ── Defect C: cmd_status uses theoretical_max_bps (unified) ─
def test_cmd_status_source_uses_theoretical_max_bps():
    src = inspect.getsource(wb.cmd_status)
    # The old wrong formula must be gone from the BPS-ceiling row.
    assert "1000.0 / cycle_ms" not in src
    # And the state field is read.
    assert 'd.get("theoretical_max_bps")' in src
    # Auto-game footnote.
    assert "batched" in src


def test_cmd_status_shows_auto_game_batching_footnote_source():
    src = inspect.getsource(wb.cmd_status)
    assert "is_auto_game" in src


def test_monitor_dashboard_uses_theoretical_max_bps():
    """The in-process live dashboard row also uses the unified formula."""
    src = inspect.getsource(wb)
    # The old cycle-based bps computation should be reduced to just cmd_diagnose's
    # decomposition path (one occurrence). All ceiling displays now use
    # _compute_theoretical_max_bps.
    # Look at build_dashboard_screen specifically.
    dash_src = inspect.getsource(wb.build_dashboard_screen)
    assert "_compute_theoretical_max_bps" in dash_src
    assert "1000.0 / cycle_ms" not in dash_src


# ── Defect D: rolling api_avg (not all-time) ─────────────
def test_theoretical_max_uses_rolling_window():
    """After 500 samples of ~100ms + 500 samples of ~50ms, the theoretical max
    reflects the CURRENT window (50ms → 20 bps), not the all-time mean (75ms)."""
    _reset_metric_state()
    # Old bias: 500 slow samples early in the session.
    for _ in range(500):
        wb._record_api_ms(100.0)
    # Current window: 500 fast samples.
    for _ in range(500):
        wb._record_api_ms(50.0)
    # The rolling deque (maxlen=500) now only contains the 50 ms samples.
    tmax = wb._compute_theoretical_max_bps()
    assert 19.0 < tmax < 21.0, f"expected ~20 bps from 50 ms rolling avg, got {tmax}"


def test_rolling_avg_helper_matches_deque_mean():
    _reset_metric_state()
    for ms in (30.0, 40.0, 50.0, 60.0):
        wb._record_api_ms(ms)
    expected = (30 + 40 + 50 + 60) / 4
    assert abs(wb._compute_api_rolling_avg() - expected) < 1e-9


# ── Regression: manual game keeps sensible ceiling ───────
def test_manual_game_ceiling_still_meaningful():
    """For dice/dice-manual, cycle_ms is per bet and matches api_avg. The
    unified theoretical_max_bps should equal 1/api_avg regardless."""
    _reset_metric_state()
    for _ in range(50):
        wb._record_api_ms(50.0)
    tmax = wb._compute_theoretical_max_bps()
    assert 19.5 < tmax < 20.5


# ── State-file writes new field consistently ─────────────
def test_save_state_file_still_writes_theoretical_max_bps():
    """Preserve backward compat: `theoretical_max_bps` state-file key survives."""
    src = inspect.getsource(wb._state_snapshot)
    assert '"theoretical_max_bps"' in src

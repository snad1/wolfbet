"""Phase 3 pure-function tests for wolfbet.py observability metrics.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase3_metrics.py -q
"""

import os
import sys
from collections import deque

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _reset_metrics():
    """Reset the fields Phase 3 helpers read/write."""
    s = wb.state
    s._last_api_ms = 0.0
    s._api_ms_total = 0.0
    s._api_ms_count = 0
    s.peak_api_ms = 0.0
    s._api_samples = deque(maxlen=500)
    s._on_target_count = 0
    s._cycle_ms = 0.0
    s._cycle_overhead_ms = 0.0
    s.bet_delay = 0.0
    s.bets_per_second = 0.0


# ── _compute_api_avg ──────────────────────────────────────
def test_api_avg_empty_returns_zero():
    _reset_metrics()
    assert wb._compute_api_avg() == 0.0


def test_api_avg_three_samples():
    _reset_metrics()
    wb.state._api_ms_total = 30.0 + 60.0 + 90.0
    wb.state._api_ms_count = 3
    assert wb._compute_api_avg() == 60.0


# ── _compute_api_p50 ──────────────────────────────────────
def test_api_p50_empty_returns_zero():
    _reset_metrics()
    assert wb._compute_api_p50() == 0.0


def test_api_p50_single_sample():
    _reset_metrics()
    wb.state._api_samples.append(42.0)
    assert wb._compute_api_p50() == 42.0


def test_api_p50_three_samples_returns_middle():
    _reset_metrics()
    for v in [10.0, 100.0, 50.0]:
        wb.state._api_samples.append(v)
    # sorted: [10, 50, 100]; index 1 → 50
    assert wb._compute_api_p50() == 50.0


def test_api_p50_500_sample_window_saturates():
    _reset_metrics()
    for i in range(600):  # exceeds maxlen=500
        wb.state._api_samples.append(float(i))
    # After 600 appends, deque holds samples 100..599 (indices), median at position 250 → value 350
    assert wb._compute_api_p50() == 350.0


# ── _compute_on_target_pct ────────────────────────────────
def test_on_target_pct_no_delay_returns_none():
    _reset_metrics()
    wb.state.bet_delay = 0.0
    wb.state._api_ms_count = 10
    wb.state._on_target_count = 5
    assert wb._compute_on_target_pct() is None


def test_on_target_pct_no_samples_returns_none():
    _reset_metrics()
    wb.state.bet_delay = 0.1
    assert wb._compute_on_target_pct() is None


def test_on_target_pct_computes_percentage():
    _reset_metrics()
    wb.state.bet_delay = 0.1
    wb.state._api_ms_count = 20
    wb.state._on_target_count = 15
    assert wb._compute_on_target_pct() == 75.0


# ── _compute_theoretical_max_bps ──────────────────────────
def test_theoretical_max_zero_when_no_data():
    _reset_metrics()
    assert wb._compute_theoretical_max_bps() == 0.0


def test_theoretical_max_bounded_by_delay():
    _reset_metrics()
    wb.state.bet_delay = 0.320   # 320ms
    wb.state._api_ms_total = 50.0
    wb.state._api_ms_count = 1   # api_avg 50ms → api_avg_s = 0.05
    # max(0.32, 0.05) = 0.32 → 1/0.32 ≈ 3.125
    assert abs(wb._compute_theoretical_max_bps() - 3.125) < 1e-6


def test_theoretical_max_bounded_by_api_when_no_delay():
    _reset_metrics()
    wb.state.bet_delay = 0.0
    # v2.30.3 — theoretical max now uses the rolling `_api_samples` window
    # (matches p50/p99 semantics) instead of the all-time _api_ms_total/count.
    wb.state._api_samples.append(100.0)  # rolling avg 100ms → 0.1s → 10 bps
    assert wb._compute_theoretical_max_bps() == 10.0


# ── _compute_efficiency_pct ───────────────────────────────
def test_efficiency_zero_when_no_ceiling():
    _reset_metrics()
    wb.state.bets_per_second = 5.0
    # No api samples + no delay → tmax=0 → efficiency=0
    assert wb._compute_efficiency_pct() == 0.0


def test_efficiency_computes_ratio():
    _reset_metrics()
    wb.state.bet_delay = 0.320
    wb.state._api_ms_total = 50.0
    wb.state._api_ms_count = 1
    wb.state.bets_per_second = 3.0    # tmax ≈ 3.125 → efficiency ≈ 96%
    eff = wb._compute_efficiency_pct()
    assert 95.0 <= eff <= 97.0


def test_efficiency_caps_at_100():
    _reset_metrics()
    wb.state.bet_delay = 0.320
    wb.state._api_ms_total = 50.0
    wb.state._api_ms_count = 1
    wb.state.bets_per_second = 100.0  # would compute >>100
    assert wb._compute_efficiency_pct() == 100.0


# ── _record_api_ms ────────────────────────────────────────
def test_record_api_ms_updates_all_counters():
    _reset_metrics()
    wb.state.bet_delay = 0.1  # 100ms — a 50ms sample is on-target
    wb._record_api_ms(50.0)
    assert wb.state._last_api_ms == 50.0
    assert wb.state._api_ms_total == 50.0
    assert wb.state._api_ms_count == 1
    assert wb.state.peak_api_ms == 50.0
    assert list(wb.state._api_samples) == [50.0]
    assert wb.state._on_target_count == 1


def test_record_api_ms_off_target_when_above_delay():
    _reset_metrics()
    wb.state.bet_delay = 0.1  # 100ms
    wb._record_api_ms(150.0)  # above delay → NOT on-target
    assert wb.state._on_target_count == 0


def test_record_api_ms_no_on_target_when_delay_zero():
    _reset_metrics()
    wb.state.bet_delay = 0.0
    wb._record_api_ms(50.0)
    assert wb.state._on_target_count == 0


# ── _record_cycle_ms ──────────────────────────────────────
def test_record_cycle_ms_first_sample():
    _reset_metrics()
    wb._record_cycle_ms(320.0)
    assert wb.state._cycle_ms == 320.0


def test_record_cycle_ms_ema_smooths():
    _reset_metrics()
    wb.state._cycle_ms = 300.0
    wb._record_cycle_ms(400.0)
    # 300 * 0.9 + 400 * 0.1 = 310
    assert abs(wb.state._cycle_ms - 310.0) < 1e-6


def test_record_cycle_ms_overhead_derived():
    _reset_metrics()
    wb.state._last_api_ms = 50.0
    wb.state.bet_delay = 0.1  # 100ms
    wb._record_cycle_ms(200.0)
    # overhead = 200 - 50 - 100 = 50
    assert wb.state._cycle_overhead_ms == 50.0


def test_record_cycle_ms_overhead_floors_at_zero():
    _reset_metrics()
    wb.state._last_api_ms = 500.0  # implausibly larger than cycle
    wb.state.bet_delay = 0.0
    wb._record_cycle_ms(100.0)
    assert wb.state._cycle_overhead_ms == 0.0  # never negative

"""Phase 5x — cycle-metric windowing + windowed efficiency + sharpened advice (v2.30.4).

v2.30.3 replaced a broken sum formula and added rolling api_avg, but the underlying
cycle_ms was still α=0.1 EMA — one 400 ms iteration held the display up for
5-10 samples, and diagnose could momentarily show `Actual > Theoretical`.
v2.30.4 mirrors the api_p50/p99 windowing pattern for cycle_ms and unifies the
windows so efficiency stays algebraically consistent.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5x_cycle_windowing.py -q
"""

import inspect
import io
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


def _reset_cycle_samples():
    wb.state._cycle_samples = deque(maxlen=500)
    wb.state._cycle_ms = 0.0
    wb.state._cycle_overhead_ms = 0.0


# ── Windowed cycle metrics ───────────────────────────────
def test_cycle_p50_stable_across_single_outlier():
    """One huge outlier must NOT drag the p50 (median) upward — was the EMA
    behaviour before v2.30.4."""
    _reset_cycle_samples()
    for ms in [80] * 20:
        wb._record_cycle_ms(ms)
    median_before = wb._compute_cycle_p50()
    wb._record_cycle_ms(2000)  # one 2-second iteration (retention worker / GC / /auto/start)
    median_after = wb._compute_cycle_p50()
    # Median should be unchanged (still 80) — outlier lives only in p99.
    assert abs(median_after - median_before) <= 5.0, \
        f"median moved {median_before}→{median_after}, expected ~stable"


def test_cycle_p99_catches_outliers():
    """p99 by definition is the 99th percentile — feed >1% outliers so they
    land in the tail (matches the existing api_p99 semantics)."""
    _reset_cycle_samples()
    for ms in [80] * 90:
        wb._record_cycle_ms(ms)
    for _ in range(2):
        wb._record_cycle_ms(2000)
    assert wb._compute_cycle_p99() >= 500, "p99 should reflect tail outliers"


def test_cycle_window_rolls_after_maxlen():
    _reset_cycle_samples()
    wb.state._cycle_samples = deque(maxlen=10)  # tiny window for the test
    for ms in [50] * 10:
        wb._record_cycle_ms(ms)
    assert wb._compute_cycle_p50() == 50
    for ms in [200] * 10:
        wb._record_cycle_ms(ms)
    # Old 50s have rolled off; median is now 200.
    assert wb._compute_cycle_p50() == 200


def test_cycle_ms_aliased_to_p50_for_backwards_compat():
    _reset_cycle_samples()
    for ms in [80] * 20:
        wb._record_cycle_ms(ms)
    assert wb.state._cycle_ms == wb._compute_cycle_p50()


def test_overhead_stable_across_single_outlier():
    """Overhead = max(0, cycle_p50 - api_p50 - delay). Since both p50s are
    windowed medians, one outlier on either can't slam overhead to 0."""
    _reset_cycle_samples()
    wb.state._api_samples = deque(maxlen=500)
    wb.state.bet_delay = 0.0
    # Populate baseline: cycle=80, api=40 → overhead=40
    for _ in range(50):
        wb.state._api_samples.append(40)
        wb._record_cycle_ms(80)
    overhead_before = wb.state._cycle_overhead_ms
    assert 35 <= overhead_before <= 45
    # Inject one very slow API sample.
    wb.state._api_samples.append(9999)
    wb._record_cycle_ms(80)
    overhead_after = wb.state._cycle_overhead_ms
    assert abs(overhead_after - overhead_before) <= 5, \
        f"overhead swung {overhead_before}→{overhead_after} from a single outlier"


# ── Windowed recent BPS + efficiency ─────────────────────
def test_recent_bps_matches_inverse_of_cycle_p50():
    _reset_cycle_samples()
    for _ in range(20):
        wb._record_cycle_ms(100)  # 10 BPS
    assert abs(wb._compute_recent_bps() - 10.0) < 0.01


def test_efficiency_uses_recent_not_session():
    """When recent_bps < session bps (recent slowdown), efficiency should reflect
    the RECENT reality, not the flattering session-wide average."""
    _reset_cycle_samples()
    wb.state._api_samples = deque(maxlen=500)
    wb.state.bet_delay = 0.0
    wb.state.bets_per_second = 25.0  # session-wide (would give >100% before)
    for _ in range(50):
        wb.state._api_samples.append(50)   # theoretical = 20 BPS
        wb._record_cycle_ms(100)           # recent = 10 BPS
    # efficiency = 10 / 20 * 100 = 50%, not 25/20 = 125%
    eff = wb._compute_efficiency_pct()
    assert 45 <= eff <= 55, f"efficiency should be ~50%, got {eff}"


def test_efficiency_falls_back_to_session_when_cycle_window_empty():
    _reset_cycle_samples()
    wb.state._api_samples = deque(maxlen=500)
    wb.state.bet_delay = 0.0
    for _ in range(10):
        wb.state._api_samples.append(50)
    wb.state.bets_per_second = 15.0  # session-wide
    # cycle deque still empty → recent=0 → fallback to session bps
    assert wb._compute_recent_bps() == 0.0
    eff = wb._compute_efficiency_pct()
    assert 70 <= eff <= 80  # 15/20 * 100 = 75%


# ── State-file emits new keys ────────────────────────────
def test_state_file_source_emits_new_keys():
    src = inspect.getsource(wb._save_state_file)
    assert '"cycle_p50_ms"' in src
    assert '"cycle_p99_ms"' in src
    assert '"cycle_sample_count"' in src
    assert '"bets_per_second_recent"' in src
    # Backwards-compat: old keys still emitted.
    assert '"cycle_ms"' in src
    assert '"bets_per_second"' in src


# ── cmd_diagnose display ────────────────────────────────
def test_cmd_diagnose_source_shows_recent_and_median():
    src = inspect.getsource(wb.cmd_diagnose)
    assert "Recent:" in src
    assert "Efficiency:" in src
    assert "median" in src
    assert "p99" in src


def test_cmd_diagnose_source_has_shared_tenancy_advice():
    """Sharpened advice: on healthy DB + healthy rate-limit, don't mislead the
    user into hunting a bug that isn't there — say shared-tenancy silicon."""
    src = inspect.getsource(wb.cmd_diagnose)
    assert "shared-tenancy CPU/disk jitter" in src


def test_cmd_diagnose_source_has_efficiency_over_100_flag():
    src = inspect.getsource(wb.cmd_diagnose)
    assert '">100%"' in src


# ── Version ──────────────────────────────────────────────
def test_version_bumped():
    assert wb.VERSION == "2.37.1"

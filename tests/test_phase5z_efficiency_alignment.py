"""Phase 5z: Recent + Theoretical use the same statistic (v2.30.5).

v2.30.4's Efficiency ratio was `Recent (cycle_p50) / Theoretical (api_mean)` -
median vs mean from different distributions. On right-skewed traffic (a few
slow API requests) median < mean, so Recent > Theoretical fired ">100%" as
the default reading. v2.30.5 switches Recent to cycle_mean so both sides
derive from mean(latency); Little's-Law-consistent (throughput ≈ 1/mean).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5z_efficiency_alignment.py -q
"""

import inspect
import os
import statistics
import sys
from collections import deque

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _reset():
    wb.state._cycle_samples = deque(maxlen=500)
    wb.state._api_samples = deque(maxlen=500)
    wb.state._cycle_ms = 0.0
    wb.state._cycle_overhead_ms = 0.0
    wb.state.bet_delay = 0.0
    wb.state.bets_per_second = 0.0


# ── VERSION ──────────────────────────────────────────────
def test_version_bumped():
    assert wb.VERSION == "2.62.0"


# ── cycle_mean helper ────────────────────────────────────
def test_cycle_mean_empty_returns_zero():
    _reset()
    assert wb._compute_cycle_mean() == 0.0


def test_cycle_mean_matches_python_mean():
    _reset()
    samples = [40, 50, 200, 45, 42]
    for s in samples:
        wb._record_cycle_ms(s)
    assert abs(wb._compute_cycle_mean() - statistics.mean(samples)) < 1e-9


# ── Recent BPS now derives from cycle_mean ───────────────
def test_recent_bps_uses_cycle_mean_not_p50():
    """The whole v2.30.5 point: Recent must use the same statistic as
    Theoretical. Feed a right-skewed cycle distribution and check that
    Recent = 1000/mean, NOT 1000/median (would give a much higher BPS)."""
    _reset()
    # 90 samples at 40 ms + 10 samples at 400 ms → median=40, mean=76.
    for _ in range(90):
        wb._record_cycle_ms(40)
    for _ in range(10):
        wb._record_cycle_ms(400)
    recent = wb._compute_recent_bps()
    # If we (wrongly) used median (40 ms) we'd get 25 BPS.
    # With mean (76 ms) we get ~13.16 BPS.
    assert 12.0 < recent < 14.0, f"Recent should be ~13 (from mean), got {recent}"


def test_recent_bps_source_uses_cycle_mean():
    src = inspect.getsource(wb._compute_recent_bps)
    assert "_compute_cycle_mean" in src
    assert "_compute_cycle_p50" not in src  # explicit swap-out


# ── Efficiency = 100% when both sides match ──────────────
def test_efficiency_100_pct_when_cycle_matches_api():
    """cycle_mean == api_mean + delay=0 → efficiency = 100%. No >100% flag."""
    _reset()
    for _ in range(50):
        wb.state._api_samples.append(50)
        wb._record_cycle_ms(50)
    eff = wb._compute_efficiency_pct()
    assert 99.0 <= eff <= 100.0, f"efficiency should be ~100, got {eff}"


def test_efficiency_stays_within_100_on_healthy_traffic():
    """After v2.30.5, healthy traffic (cycle_mean ~= api_mean) should never
    cross >100%. Feed the exact right-skewed distribution from the real
    user report: p50=37, mean=~77 for both api and cycle."""
    _reset()
    for _ in range(90):
        wb.state._api_samples.append(40)
        wb._record_cycle_ms(40)
    for _ in range(10):
        wb.state._api_samples.append(400)
        wb._record_cycle_ms(400)
    eff = wb._compute_efficiency_pct()
    assert eff <= 100.0, f"efficiency should stay ≤100 on matched distributions, got {eff}"


# ── State-file emission ──────────────────────────────────
def test_state_file_emits_cycle_mean_ms():
    src = inspect.getsource(wb._state_snapshot)
    assert '"cycle_mean_ms"' in src
    # Old keys still emitted for backwards compat.
    assert '"cycle_p50_ms"' in src
    assert '"cycle_p99_ms"' in src


# ── p50/p99 display unchanged (backwards compat) ─────────
def test_cycle_p50_still_available():
    _reset()
    for s in [40, 50, 200]:
        wb._record_cycle_ms(s)
    # p50 and mean now differ (mean pulled up by 200); both must be reachable.
    assert wb._compute_cycle_p50() != wb._compute_cycle_mean()

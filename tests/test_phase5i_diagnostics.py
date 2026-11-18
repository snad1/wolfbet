"""Phase 5i tests - BPS diagnostics (--benchmark, --diagnose, latency status rows).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5i_diagnostics.py -q
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


# ── _percentile ───────────────────────────────────────────
def test_percentile_p50_midpoint():
    xs = sorted([10.0, 20.0, 30.0, 40.0, 50.0])
    assert wb._percentile(xs, 0.50) == 30.0


def test_percentile_p99_last():
    xs = sorted(list(range(100)))
    assert wb._percentile(xs, 0.99) >= 98


def test_percentile_empty_zero():
    assert wb._percentile([], 0.5) == 0.0


def test_percentile_single_sample():
    assert wb._percentile([42.0], 0.5) == 42.0


# ── cmd_benchmark - n clamping ────────────────────────────
class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


@contextmanager
def _fake_get(status_codes=None, latencies_ms=None):
    """Patch _http_session.get so tests never touch the network."""
    original = wb._http_session.get
    idx = [0]
    def fake(url, **kw):
        i = idx[0]
        code = 200
        if status_codes:
            code = status_codes[i % len(status_codes)]
        idx[0] += 1
        return _FakeResponse(code)
    wb._http_session.get = fake
    try:
        yield idx
    finally:
        wb._http_session.get = original


def test_benchmark_default_n_is_20(monkeypatch, capsys):
    monkeypatch.setattr(wb, "load_config", lambda: True)
    with _fake_get() as counter:
        wb.cmd_benchmark(None)
    assert counter[0] == 20


def test_benchmark_clamps_high(monkeypatch):
    monkeypatch.setattr(wb, "load_config", lambda: True)
    with _fake_get() as counter:
        wb.cmd_benchmark(500)
    assert counter[0] == 200


def test_benchmark_clamps_low(monkeypatch):
    monkeypatch.setattr(wb, "load_config", lambda: True)
    with _fake_get() as counter:
        wb.cmd_benchmark(1)
    assert counter[0] == 3


def test_benchmark_exact_n(monkeypatch):
    monkeypatch.setattr(wb, "load_config", lambda: True)
    with _fake_get() as counter:
        wb.cmd_benchmark(5)
    assert counter[0] == 5


# ── Verdict tiers via captured output ─────────────────────
def test_benchmark_verdict_rate_limited(monkeypatch, capsys):
    monkeypatch.setattr(wb, "load_config", lambda: True)
    with _fake_get(status_codes=[429, 200, 429, 200, 429]) as _c:
        wb.cmd_benchmark(5)
    out = capsys.readouterr().out
    assert "Rate-limited" in out


def test_benchmark_verdict_ok_when_all_200(monkeypatch, capsys):
    monkeypatch.setattr(wb, "load_config", lambda: True)
    with _fake_get(status_codes=[200] * 10) as _c:
        wb.cmd_benchmark(10)
    out = capsys.readouterr().out
    # Any of the non-rate-limited verdicts qualifies (fast/OK/network bottleneck/tail).
    assert any(k in out for k in ("Network fast", "Network OK", "Network bottleneck", "Tail-latency"))


def test_benchmark_no_config_exits_nonzero(monkeypatch):
    monkeypatch.setattr(wb, "load_config", lambda: False)
    try:
        wb.cmd_benchmark(3)
    except SystemExit as e:
        assert e.code == 1
        return
    raise AssertionError("expected SystemExit(1) when no config")


# ── cmd_diagnose - missing state file ─────────────────────
def test_diagnose_missing_state_file(monkeypatch, capsys):
    tmp = tempfile.NamedTemporaryFile(delete=True)
    path = tmp.name
    tmp.close()  # ensure file does not exist
    monkeypatch.setattr(wb, "STATE_PATH", path)
    wb.cmd_diagnose()
    out = capsys.readouterr().out
    assert "No running daemon session" in out


# ── cmd_diagnose - top-blocker selection ──────────────────
def _write_state(tmpdir, **overrides):
    payload = {
        "api_p50_ms": 30,
        "api_p99_ms": 50,
        "overhead_ms": 5,
        "cycle_ms": 40,
        "bet_delay": 0.0,
        "bets_per_second": 25.0,
        "theoretical_max_bps": 30.0,
        "rate_remaining": 400,
        "rate_limit": 480,
        "auto_start_remaining": 15,
        "game": "dice",
        "strategy": "Flat Bet",
        "pipeline_effective": 1,
    }
    payload.update(overrides)
    path = os.path.join(tmpdir, "state.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


def test_diagnose_top_blocker_network(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as td:
        path = _write_state(td, api_p50_ms=200, overhead_ms=10, bet_delay=0)
        monkeypatch.setattr(wb, "STATE_PATH", path)
        wb.cmd_diagnose()
    out = capsys.readouterr().out
    assert "Network / API latency" in out
    assert "wolfctl benchmark" in out or "benchmark 20" in out


def test_diagnose_top_blocker_user_delay(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as td:
        path = _write_state(td, api_p50_ms=15, overhead_ms=5, bet_delay=0.5)
        monkeypatch.setattr(wb, "STATE_PATH", path)
        wb.cmd_diagnose()
    out = capsys.readouterr().out
    assert "User-configured delay" in out
    assert "delay=0" in out


def test_diagnose_pipeline_advice_for_flat_no_delay(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as td:
        path = _write_state(td, api_p50_ms=15, bet_delay=0.0, strategy="Flat Bet")
        monkeypatch.setattr(wb, "STATE_PATH", path)
        wb.cmd_diagnose()
    out = capsys.readouterr().out
    assert "pipeline" in out.lower()


# ── State file has all diagnostic fields ─────────────────
def test_state_file_layout_includes_diagnostic_fields():
    import inspect
    src = inspect.getsource(wb._state_snapshot)
    for field in ("api_p50_ms", "api_p99_ms", "cycle_ms", "overhead_ms",
                  "efficiency_pct", "theoretical_max_bps",
                  "pipeline_effective", "pipeline_dispatched"):
        assert f'"{field}"' in src, f"missing {field} in _save_state_file"

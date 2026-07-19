"""Phase 5n tests — monitor layout row-budget invariant (v2.28.2 fix).

Bug reproduced: v2.28.1 added a tweak/export hint footer row without
updating _build_monitor_screen's `bets_avail = term_h - 10` budget,
so total rendered lines exceeded term_h by 1 and the session-title
row scrolled off the top. This test locks the invariant.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5n_monitor_layout.py -q
"""

import collections
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


_TermSize = collections.namedtuple("_TermSize", ["columns", "lines"])


def _fixture_state():
    """Enough fields to satisfy every d.get() in _build_monitor_screen."""
    return {
        "session_id": 42,
        "uptime_sec": 90,
        "currency": "usdt",
        "strategy": "Martingale",
        "multiplier": 10.0,
        "rule": "over",
        "base_bet": 0.00000010,
        "current_bet": 0.00000012,
        "highest_bet": 0.00000100,
        "current_balance": 1.001,
        "start_balance": 1.0,
        "profit": 0.001,
        "wagered": 0.02,
        "total_bets": 500,
        "wins": 50,
        "losses": 450,
        "current_streak": -5,
        "max_win_streak": 3,
        "max_loss_streak": 25,
        "highest_balance": 1.002,
        "lowest_balance": 0.999,
        "highest_win": 0.001,
        "biggest_loss": 0.0002,
        "bets_per_second": 15.0,
        "bets_per_minute": 900,
        "peak_bps": 20,
        "low_bps": 5,
        "peak_bpm": 1000,
        "low_bpm": 400,
        "rate_remaining": 3500,
        "rate_limit": 4000,
        "paused": False,
        "status": "Placing bet #501…",
    }


def _fixture_bets(n):
    return [
        {"n": i, "time": "01:50:49", "amt": 0.0000001, "roll": 5.0,
         "pnl": -0.0000001, "bal": 1.0, "state": "loss"}
        for i in range(n)
    ]


def _render(term_h, term_w=180, monkeypatch=None):
    monkeypatch.setattr(os, "get_terminal_size",
                        lambda: _TermSize(term_w, term_h))
    d = _fixture_state()
    bets = _fixture_bets(200)  # more than any budget could ask for
    return wb._build_monitor_screen(d, bets, pid_alive=True)


# ── Invariant: total rendered lines == term_h ─────────────
def _line_count(rendered: str) -> int:
    # _build_monitor_screen joins with \r\n; splitting counts every produced row.
    return len(rendered.split("\r\n"))


def test_monitor_layout_fits_term_h_24(monkeypatch):
    rendered = _render(24, monkeypatch=monkeypatch)
    assert _line_count(rendered) == 24


def test_monitor_layout_fits_term_h_40(monkeypatch):
    rendered = _render(40, monkeypatch=monkeypatch)
    assert _line_count(rendered) == 40


def test_monitor_layout_fits_term_h_80(monkeypatch):
    rendered = _render(80, monkeypatch=monkeypatch)
    assert _line_count(rendered) == 80


# ── Row 1 present: session-title header (top-row regression guard) ──
def test_first_line_contains_session_title(monkeypatch):
    rendered = _render(40, monkeypatch=monkeypatch)
    first = rendered.split("\r\n")[0]
    assert "WolfBet v" in first, f"session-title row missing; got: {first[:80]}"


# ── Row N present: tweak/export hint (footer preservation) ────────
def test_last_line_contains_tweak_hint(monkeypatch):
    rendered = _render(40, monkeypatch=monkeypatch)
    last = rendered.split("\r\n")[-1]
    assert "wolfctl config-set" in last, f"footer missing; got: {last[:120]}"


# ── Constant value locked ────────────────────────────────
def test_monitor_fixed_rows_constant_is_11():
    assert wb._MONITOR_FIXED_ROWS == 11


# ── Legacy magic number removed ──────────────────────────
def test_magic_10_replaced_by_named_constant():
    import inspect
    src = inspect.getsource(wb._build_monitor_screen)
    assert "term_h - _MONITOR_FIXED_ROWS" in src
    assert "term_h - 10" not in src

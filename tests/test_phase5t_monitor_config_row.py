"""Phase 5t — Monitor Row 6 config-progress row (v2.30.0).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5t_monitor_config_row.py -q
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


def _base(**kw):
    d = {
        "profit": 0.0,
        "profit_threshold": None,
        "profit_increment": None,
        "last_profit_milestone": 0.0,
        "max_profit": None,
        "max_loss": None,
        "max_bets": None,
        "max_wins": None,
        "stop_on_balance": None,
        "recurring": False,
        "recurring_delay_sec": 0,
        "total_bets": 0,
        "wins": 0,
        "current_balance": 1.0,
    }
    d.update(kw)
    return d


def test_empty_config_renders_fallback():
    row = wb._build_config_progress_row(_base())
    plain = _strip_ansi(row)
    assert "CFG" in plain
    assert "no goals set" in plain


def test_row_is_single_line():
    row = wb._build_config_progress_row(_base(max_profit=0.01, max_bets=1000))
    assert "\n" not in row


def test_milestone_shown_only_when_both_set():
    row = _strip_ansi(wb._build_config_progress_row(
        _base(profit_threshold=0.001, profit_increment=0.00000001,
              last_profit_milestone=0.0, profit=0.0005)))
    assert "Ms " in row


def test_milestone_progress_50pct():
    d = _base(profit_threshold=0.001, profit_increment=0.00000001,
              last_profit_milestone=0.0, profit=0.0005)
    row = _strip_ansi(wb._build_config_progress_row(d))
    assert "(50%)" in row


def test_milestone_progress_over_100_clamps():
    d = _base(profit_threshold=0.001, profit_increment=0.00000001,
              last_profit_milestone=0.0, profit=0.005)
    row = _strip_ansi(wb._build_config_progress_row(d))
    assert "(100%)" in row


def test_max_profit_segment_and_color_yellow_at_85():
    d = _base(max_profit=0.01, profit=0.0085)
    row = wb._build_config_progress_row(d)
    plain = _strip_ansi(row)
    assert "MaxP" in plain
    assert "(85%)" in plain
    assert "\x1b[33m" in row  # yellow


def test_max_profit_bold_red_at_96pct():
    d = _base(max_profit=0.01, profit=0.0096)
    row = wb._build_config_progress_row(d)
    assert "\x1b[1;31m" in row  # bold_red


def test_max_profit_green_at_low_pct():
    d = _base(max_profit=0.01, profit=0.001)
    row = wb._build_config_progress_row(d)
    assert "\x1b[32m" in row  # green


def test_max_loss_uses_abs_only_when_negative():
    d = _base(max_loss=-0.005, profit=-0.001)
    plain = _strip_ansi(wb._build_config_progress_row(d))
    assert "MaxL" in plain
    assert "(20%)" in plain


def test_max_loss_dim_when_profit_positive():
    d = _base(max_loss=-0.005, profit=0.001)
    row = wb._build_config_progress_row(d)
    plain = _strip_ansi(row)
    assert "MaxL" in plain
    assert "(0%)" in plain


def test_max_bets_progress():
    d = _base(max_bets=10000, total_bets=4222)
    plain = _strip_ansi(wb._build_config_progress_row(d))
    assert "MaxB" in plain
    assert "4222/10000" in plain
    assert "(42%)" in plain


def test_max_wins_progress():
    d = _base(max_wins=1000, wins=397)
    plain = _strip_ansi(wb._build_config_progress_row(d))
    assert "MaxW" in plain
    assert "397/1000" in plain


def test_minbal_yellow_when_within_5pct():
    d = _base(stop_on_balance=1.0, current_balance=1.03)
    row = wb._build_config_progress_row(d)
    plain = _strip_ansi(row)
    assert "MinBal" in plain
    assert "\x1b[33m" in row  # yellow


def test_minbal_dim_when_far_above_floor():
    d = _base(stop_on_balance=1.0, current_balance=2.0)
    row = wb._build_config_progress_row(d)
    plain = _strip_ansi(row)
    assert "MinBal" in plain
    # yellow color should not appear on the MinBal segment
    assert "\x1b[33m" not in row


def test_recurring_on_shows_delay():
    d = _base(recurring=True, recurring_delay_sec=60)
    plain = _strip_ansi(wb._build_config_progress_row(d))
    assert "Rec" in plain
    assert "on(60s)" in plain


def test_recurring_off_skipped():
    d = _base(recurring=False, max_profit=0.01)
    plain = _strip_ansi(wb._build_config_progress_row(d))
    assert "Rec" not in plain


def test_monitor_fixed_rows_still_11():
    assert wb._MONITOR_FIXED_ROWS == 11


def test_row_6_uses_helper():
    import inspect
    src = inspect.getsource(wb._build_monitor_screen)
    assert "_build_config_progress_row" in src
    assert "P/L sparkline placeholder" not in src

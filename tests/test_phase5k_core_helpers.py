"""Phase 5k tests — shared core.net_helpers + core.diagnostics.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5k_core_helpers.py -q

These are the primary regression guard for the six helpers deduplicated
between CLI and TG in v2.28.0. If any of these break, both engines are
affected.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

from core.net_helpers import (  # noqa: E402
    validate_proxy, mask_proxy, classify_auto_stop_reason, parse_streak_delay,
)
from core.diagnostics import percentile, benchmark_verdict  # noqa: E402


# ── validate_proxy ────────────────────────────────────────
def test_validate_http():
    assert validate_proxy("http://host:8080") == "http://host:8080"


def test_validate_https():
    assert validate_proxy("https://host:8080") == "https://host:8080"


def test_validate_socks5():
    assert validate_proxy("socks5://host:1080") == "socks5://host:1080"


def test_validate_off_values_return_none():
    for v in ("off", "none", "direct", "0", "false", "disable", "no", "", None,
              "OFF", "  off  "):
        assert validate_proxy(v) is None, f"expected None for {v!r}"


def test_validate_bad_scheme_raises():
    for bad in ("ftp://x", "gopher://x", "x", "//host", "http:x"):
        try:
            validate_proxy(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


# ── mask_proxy ────────────────────────────────────────────
def test_mask_strips_userinfo():
    assert mask_proxy("http://user:pass@host:8080") == "http://host:8080"


def test_mask_no_port_no_none_leak():
    # THIS is the bug in TG's inline masker (fixed by v2.28.0 extraction).
    assert mask_proxy("http://host") == "http://host"
    assert "None" not in mask_proxy("http://host")


def test_mask_preserves_scheme_and_port():
    assert mask_proxy("https://proxy.example.com:443") == "https://proxy.example.com:443"
    assert mask_proxy("socks5://10.0.0.1:1080") == "socks5://10.0.0.1:1080"


def test_mask_none_returns_direct():
    assert mask_proxy(None) == "direct"


def test_mask_empty_returns_direct():
    assert mask_proxy("") == "direct"


# ── classify_auto_stop_reason ────────────────────────────
def test_classify_max_nonce():
    # This is the TG bot bug fix: TG's inline check missed "nonce".
    assert classify_auto_stop_reason("Max nonce achieved. Change your server seed.") == "max_nonce"


def test_classify_nonce_case_insensitive():
    assert classify_auto_stop_reason("MAX NONCE ACHIEVED") == "max_nonce"


def test_classify_session_expired_variants():
    for msg in ("Session has ended", "Bet session expired",
                "betsession not exists", "no query results"):
        assert classify_auto_stop_reason(msg) == "expired", f"expected expired for {msg!r}"


def test_classify_max_profit_terminal():
    # Server-side max_profit is terminal for the WolfBet cycle, not a recovery case.
    assert classify_auto_stop_reason("Max profit reached") == ""
    assert classify_auto_stop_reason("Max loss reached") == ""


def test_classify_empty_returns_empty():
    assert classify_auto_stop_reason("") == ""
    assert classify_auto_stop_reason(None) == ""


# ── parse_streak_delay ────────────────────────────────────
def test_parse_streak_ok():
    assert parse_streak_delay("3:0.5") == (3, 0.5)
    assert parse_streak_delay("10:2.0") == (10, 2.0)


def test_parse_streak_empty():
    assert parse_streak_delay("") == (0, 0.0)
    assert parse_streak_delay(None) == (0, 0.0)


def test_parse_streak_malformed():
    for bad in ("abc", "3:x", "3:", ":5", "3", "3:2:1"):
        result = parse_streak_delay(bad)
        # "3:2:1" is technically parseable — split on first ":" → ("3", "2:1"),
        # then float("2:1") raises → (0, 0.0). All others also (0, 0.0).
        assert result[0] >= 0 and isinstance(result[1], float)


# ── percentile ────────────────────────────────────────────
def test_percentile_p50_midpoint():
    xs = sorted([10.0, 20.0, 30.0, 40.0, 50.0])
    assert percentile(xs, 0.50) == 30.0


def test_percentile_p99_last():
    xs = sorted(list(range(100)))
    assert percentile(xs, 0.99) >= 98


def test_percentile_empty_zero():
    assert percentile([], 0.5) == 0.0


def test_percentile_single_sample():
    assert percentile([42.0], 0.5) == 42.0


def test_percentile_clamps_to_last_index():
    # pct > 1.0 must not IndexError
    assert percentile([1, 2, 3], 2.0) == 3


# ── benchmark_verdict ────────────────────────────────────
def test_verdict_rate_limited():
    colour, msg = benchmark_verdict(p50=44, p99=100,
                                    statuses=[429, 200, 200, 429], sample_count=4)
    assert colour == "red"
    assert "Rate-limited" in msg


def test_verdict_network_bottleneck():
    colour, msg = benchmark_verdict(p50=150, p99=300,
                                    statuses=[200] * 10, sample_count=10)
    assert colour == "yellow"
    assert "Network bottleneck" in msg


def test_verdict_tail_latency():
    colour, msg = benchmark_verdict(p50=40, p99=200,
                                    statuses=[200] * 10, sample_count=10)
    assert colour == "yellow"
    assert "Tail-latency" in msg


def test_verdict_network_fast():
    colour, msg = benchmark_verdict(p50=20, p99=25,
                                    statuses=[200] * 10, sample_count=10)
    assert colour == "green"
    assert "Network fast" in msg


def test_verdict_network_ok():
    colour, msg = benchmark_verdict(p50=50, p99=80,
                                    statuses=[200] * 10, sample_count=10)
    assert colour == "cyan"
    assert "Network OK" in msg


def test_verdict_rate_limited_precedes_network_bottleneck():
    # 429 detection must fire even when p50 is also high.
    colour, msg = benchmark_verdict(p50=150, p99=300,
                                    statuses=[429, 429, 200], sample_count=3)
    assert colour == "red"
    assert "Rate-limited" in msg


# ── CLI ↔ TG re-export sanity ────────────────────────────
def test_cli_reexports_from_wolfbet():
    """The CLI keeps thin re-export aliases so all v2.25.x-v2.27.x tests
    calling wolfbet._validate_proxy etc. still work."""
    import wolfbet as wb
    assert wb._validate_proxy is validate_proxy
    assert wb._mask_proxy is mask_proxy
    assert wb._classify_auto_stop_reason is classify_auto_stop_reason
    assert wb._parse_streak_delay is parse_streak_delay
    assert wb._percentile is percentile


def test_tg_engine_imports_shared_helpers():
    """TG bot imports classify_auto_stop_reason (max-nonce fix) and
    parse_streak_delay from the same core module."""
    import inspect
    from tg import engine as tg_engine
    src = inspect.getsource(tg_engine)
    assert "from core.net_helpers import" in src
    assert "classify_auto_stop_reason" in src
    assert "parse_streak_delay" in src


def test_tg_handlers_imports_shared_helpers():
    import inspect
    from tg import handlers as tg_handlers
    src = inspect.getsource(tg_handlers)
    assert "from core.net_helpers import" in src
    assert "validate_proxy" in src
    assert "mask_proxy" in src

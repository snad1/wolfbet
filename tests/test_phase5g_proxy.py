"""Phase 5g tests — proxy support (TG↔CLI parity).

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase5g_proxy.py -q
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import wolfbet as wb  # noqa: E402


def _reset_state():
    s = wb.state
    s.game = "dice"
    s.strategy_key = "1"
    s.strategy = "Flat Bet"
    s.currency = "usdt"
    s.multiplier = 2.01
    s.base_bet = 0.0001
    s.current_bet = 0.0001
    s.win_mult = 1.0
    s.loss_mult = 2.0
    s.current_balance = 1.0
    s.max_profit = None
    s.max_loss = None
    s.max_bets = None
    s.max_wins = None
    s.recurring = False
    s.recurring_delay = 60.0
    s.basebet_pct = 0.0
    s.pipeline_depth = 1
    s.custom_rules = []
    s.proxy = None
    s.purge_days = 3
    wb._apply_proxy_to_session()


# ── Defaults + membership ─────────────────────────────────
def test_default_proxy_none():
    _reset_state()
    assert wb.state.proxy is None


def test_proxy_in_config_keys():
    assert "proxy" in wb._CONFIG_KEYS


def test_proxy_in_preset_keys():
    assert "proxy" in wb._PRESET_KEYS


def test_proxy_in_tweak_params():
    assert "proxy" in wb._TWEAK_PARAMS


def test_proxy_in_show_fields():
    assert any(f[0] == "proxy" for f in wb._CONFIG_SHOW_FIELDS)


# ── _validate_proxy ───────────────────────────────────────
def test_validate_http():
    assert wb._validate_proxy("http://host:8080") == "http://host:8080"


def test_validate_https():
    assert wb._validate_proxy("https://host:8080") == "https://host:8080"


def test_validate_socks5():
    assert wb._validate_proxy("socks5://host:1080") == "socks5://host:1080"


def test_validate_off_values_return_none():
    for v in ("off", "none", "direct", "0", "false", "disable", "no", "", None):
        assert wb._validate_proxy(v) is None, f"expected None for {v!r}"


def test_validate_bad_scheme_raises():
    for bad in ("ftp://x", "gopher://x", "x", "//host"):
        try:
            wb._validate_proxy(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


# ── _mask_proxy — credentials stripped ────────────────────
def test_mask_strips_userinfo():
    assert wb._mask_proxy("http://user:pass@host:8080") == "http://host:8080"


def test_mask_preserves_port():
    assert wb._mask_proxy("https://proxy.example.com:443") == "https://proxy.example.com:443"


def test_mask_no_port_ok():
    assert wb._mask_proxy("http://host") == "http://host"


def test_mask_none_returns_direct():
    assert wb._mask_proxy(None) == "direct"


def test_mask_empty_returns_direct():
    assert wb._mask_proxy("") == "direct"


# ── _apply_mutation — proxy path applies to session ───────
def test_apply_mutation_sets_state_and_session():
    _reset_state()
    wb._apply_mutation({"proxy": "http://u:p@host:8080"})
    assert wb.state.proxy == "http://u:p@host:8080"
    assert wb._http_session.proxies == {
        "http": "http://u:p@host:8080",
        "https": "http://u:p@host:8080",
    }


def test_apply_mutation_off_clears_session():
    _reset_state()
    wb._apply_mutation({"proxy": "http://host"})
    assert wb._http_session.proxies
    wb._apply_mutation({"proxy": "off"})
    assert wb.state.proxy is None
    assert not wb._http_session.proxies


def test_apply_mutation_bad_scheme_raises():
    _reset_state()
    try:
        wb._apply_mutation({"proxy": "ftp://x"})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_apply_mutation_logs_masked_value():
    _reset_state()
    msgs = wb._apply_mutation({"proxy": "http://u:p@host:8080"})
    combined = " ".join(msgs)
    assert "http://host:8080" in combined
    assert "u:p" not in combined   # credentials must not leak into the message


# ── _fmt_config_value — proxy row is masked ───────────────
def test_fmt_config_value_direct_when_none():
    assert "direct" in wb._fmt_config_value("proxy", None)


def test_fmt_config_value_masks_credentials():
    rendered = wb._fmt_config_value("proxy", "http://user:pass@host:8080")
    assert "host:8080" in rendered
    assert "user:pass" not in rendered


# ── _check_risky_config — http:// proxy warning ───────────
def test_risky_warns_on_http_proxy():
    _reset_state()
    wb.state.proxy = "http://host:8080"
    warnings = wb._check_risky_config()
    assert any("proxy scheme is http://" in w for w in warnings)


def test_risky_no_warn_on_https_proxy():
    _reset_state()
    wb.state.proxy = "https://host:8080"
    warnings = wb._check_risky_config()
    assert not any("proxy scheme" in w for w in warnings)


def test_risky_no_warn_on_socks5_proxy():
    _reset_state()
    wb.state.proxy = "socks5://host:1080"
    warnings = wb._check_risky_config()
    assert not any("proxy scheme" in w for w in warnings)


def test_risky_no_warn_when_direct():
    _reset_state()
    wb.state.proxy = None
    warnings = wb._check_risky_config()
    assert not any("proxy scheme" in w for w in warnings)


# ── HTTP call sites all go through the shared session ────
def test_no_bare_requests_get_post_in_wolfbet_py():
    """Enforce that all wolfbet.com calls go through _http_session (proxy-aware)."""
    import re
    with open(os.path.join(_WOLFBET_DIR, "wolfbet.py"), encoding="utf-8") as f:
        src = f.read()
    matches = re.findall(r"\brequests\.(get|post)\s*\(", src)
    assert matches == [], f"bare requests.{{get,post}} calls found: {matches}"


def test_uses_http_session_at_5_or_more_sites():
    import re
    with open(os.path.join(_WOLFBET_DIR, "wolfbet.py"), encoding="utf-8") as f:
        src = f.read()
    matches = re.findall(r"_http_session\.(get|post)\s*\(", src)
    assert len(matches) >= 5, f"expected >=5 _http_session calls, got {len(matches)}"

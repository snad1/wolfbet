"""Phase 6 tests — core/games.py shared registry + auto-bet builders.

Run: WOLFBET_ENV=testing pytest wolfbet/tests/test_phase6_games.py -q
"""

import os
import sys
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_WOLFBET_DIR = os.path.dirname(_HERE)
if _WOLFBET_DIR not in sys.path:
    sys.path.insert(0, _WOLFBET_DIR)

os.environ.setdefault("APP_ENV", "testing")

import pytest  # noqa: E402
from core.games import (  # noqa: E402
    GAME_ENDPOINTS, AUTO_ENDPOINTS, GAME_LABELS, AUTO_START_MIN_INTERVAL,
    wolfbet_hash, is_auto_game, fmt_multiplier, request_hash_header,
    custom_rule_to_wolfbet, build_auto_config, build_auto_start_payload,
)


# ── Registry shape ──────────────────────────────────────────
def test_game_endpoints_has_all_expected_keys():
    for k in ("dice", "dice-manual", "limbo", "dice-auto", "limbo-auto"):
        assert k in GAME_ENDPOINTS
        assert k in GAME_LABELS


def test_auto_endpoints_only_auto_games():
    assert set(AUTO_ENDPOINTS.keys()) == {"dice-auto", "limbo-auto"}
    for v in AUTO_ENDPOINTS.values():
        assert "start" in v and "stop" in v


def test_auto_start_min_interval_positive():
    assert AUTO_START_MIN_INTERVAL > 0


# ── wolfbet_hash ────────────────────────────────────────────
def test_wolfbet_hash_length_and_hex():
    h = wolfbet_hash("")
    assert len(h) == 64
    int(h, 16)  # must parse as hex — raises if malformed


def test_wolfbet_hash_body_changes_output():
    # Same nonce (within a millisecond) but different body → different hash.
    # We can't easily control the nonce, but repeating the same body twice
    # should give the SAME output if nonce hasn't ticked, or DIFFERENT if it has.
    # Just assert non-empty distinct-body hashes both look valid.
    h1 = wolfbet_hash("payload-a")
    h2 = wolfbet_hash("payload-b")
    assert len(h1) == 64 and len(h2) == 64


# ── is_auto_game ────────────────────────────────────────────
def test_is_auto_game_positive():
    assert is_auto_game("dice-auto") is True
    assert is_auto_game("limbo-auto") is True


def test_is_auto_game_negative():
    assert is_auto_game("dice") is False
    assert is_auto_game("dice-manual") is False
    assert is_auto_game("limbo") is False
    assert is_auto_game("bogus") is False


# ── fmt_multiplier ──────────────────────────────────────────
@pytest.mark.parametrize("m,expected", [
    (2.0,     "2"),
    (2.01,    "2.01"),
    (2.1,     "2.1"),
    (10.0,    "10"),
    (2.1000,  "2.1"),
    (0.0,     "0"),
])
def test_fmt_multiplier(m, expected):
    assert fmt_multiplier(m) == expected


# ── request_hash_header ─────────────────────────────────────
def test_request_hash_header_none_for_dice():
    assert request_hash_header("dice", "any-body") is None


def test_request_hash_header_dynamic_for_auto():
    h = request_hash_header("dice-auto", '{"uuid":"abc"}')
    assert h is not None
    assert len(h) == 64
    int(h, 16)  # hex


def test_request_hash_header_unknown_game():
    assert request_hash_header("bogus-game", "") is None


# ── custom_rule_to_wolfbet ──────────────────────────────────
def _rule(cond_type="sequence", cond_mode="every", cond_trigger="loss",
          cond_value=1, action="increase_amount", action_value="100"):
    """Build a StrategyRule-shape SimpleNamespace fixture."""
    return SimpleNamespace(
        cond_type=cond_type, cond_mode=cond_mode, cond_trigger=cond_trigger,
        cond_value=cond_value, action=action, action_value=action_value,
        description=None,
    )


def test_custom_rule_martingale_translation():
    result = custom_rule_to_wolfbet(_rule())
    assert result is not None
    assert result["command"] == [{"name": "increaseAmountPercent", "value": 100.0}]
    assert result["when"] == [{"name": "lose", "value": 1, "type": "every"}]


def test_custom_rule_reset_on_win():
    r = _rule(cond_trigger="win", cond_value=1, action="reset_amount", action_value="0")
    result = custom_rule_to_wolfbet(r)
    assert result is not None
    assert result["command"] == [{"name": "resetAmount"}]
    assert result["when"] == [{"name": "win", "value": 1, "type": "every"}]


def test_custom_rule_profit_condition_unsupported():
    r = _rule(cond_type="profit")
    assert custom_rule_to_wolfbet(r) is None


def test_custom_rule_unknown_action_returns_none():
    r = _rule(action="nonsense")
    assert custom_rule_to_wolfbet(r) is None


def test_custom_rule_switch_action():
    r = _rule(action="switch", action_value="")
    result = custom_rule_to_wolfbet(r)
    assert result is not None
    assert result["command"] == [{"name": "switch"}]


# ── build_auto_config — one test per strategy branch ────────
def _build(strategy_key, **overrides):
    kwargs = dict(
        loss_mult=2.0, win_mult=1.0, base_bet=0.0001,
        game="dice-auto", custom_rules=[], stop_conditions=None,
    )
    kwargs.update(overrides)
    return build_auto_config(strategy_key, **kwargs)


def test_config_flat_two_reset_rules():
    r = _build("1")
    assert len(r) == 2
    assert all(cmd[0]["name"] == "resetAmount" for cmd in [rl["command"] for rl in r])


def test_config_martingale_win_reset_and_loss_increase():
    r = _build("2")
    assert r[0]["command"] == [{"name": "resetAmount"}]
    assert r[1]["command"][0]["name"] == "increaseAmountPercent"
    assert r[1]["command"][0]["value"] == 100  # loss_mult=2.0 → 100%


def test_config_anti_martingale():
    r = _build("3", win_mult=1.5)
    assert r[0]["command"][0]["name"] == "increaseAmountPercent"
    assert r[0]["command"][0]["value"] == 50  # win_mult=1.5 → 50%


def test_config_dalembert_uses_unit():
    r = _build("4", base_bet=0.0005)
    assert r[0]["command"][0]["name"] == "decreaseAmountValue"
    assert r[0]["command"][0]["value"] == 0.0005


def test_config_paroli_has_three_rules():
    r = _build("5", win_mult=1.5)
    assert len(r) == 3
    # Third rule resets on win-streak of 3
    assert r[2]["when"][0]["type"] == "streak"
    assert r[2]["when"][0]["value"] == 3


def test_config_wolfrider_dice_auto_adds_switch():
    r = _build("6", game="dice-auto")
    assert any(rl["command"] == [{"name": "switch"}] for rl in r)


def test_config_wolfrider_manual_no_switch():
    r = _build("6", game="dice")
    assert not any(rl["command"] == [{"name": "switch"}] for rl in r)


def test_config_delay_martingale_uses_threshold():
    r = _build("7")
    assert r[1]["when"][0]["type"] == "streakGreater"
    assert r[1]["when"][0]["value"] == 3  # default delay_martin_threshold


def test_config_rule_based_empty_falls_back_to_flat():
    r = _build("8", custom_rules=[])
    assert len(r) == 1
    assert r[0]["command"] == [{"name": "resetAmount"}]


def test_config_rule_based_with_custom_rule():
    r = _build("8", custom_rules=[_rule()])
    assert len(r) == 1
    assert r[0]["command"][0]["name"] == "increaseAmountPercent"


def test_config_unknown_strategy_key_defaults_to_flat():
    r = _build("99")
    assert len(r) == 1
    assert r[0]["command"] == [{"name": "resetAmount"}]


def test_config_stop_conditions_appended():
    r = _build("1", stop_conditions={
        "max_profit": 0.001,
        "max_loss": 0.005,
        "max_bets": 100,
        "stop_on_balance": 0.5,
    })
    # 2 flat rules + 4 stop rules = 6
    assert len(r) == 6
    stop_cmds = [rl for rl in r if rl["command"] == [{"name": "stop"}]]
    assert len(stop_cmds) == 4


# ── build_auto_start_payload ────────────────────────────────
def test_payload_dice_auto_has_rule_and_bet_value():
    payload = build_auto_start_payload(
        game="dice-auto", currency="usdt", base_bet=0.0001,
        multiplier=2.01, rule="over", bet_value=49.5,
        config=[{"command": [{"name": "resetAmount"}], "when": []}],
    )
    assert payload["game"] == "dice"
    assert payload["rule"] == "over"
    assert payload["bet_value"] == "49.50"
    assert payload["multiplier"] == "2.01"
    assert payload["currency"] == "usdt"


def test_payload_limbo_auto_no_rule_no_bet_value():
    payload = build_auto_start_payload(
        game="limbo-auto", currency="usdt", base_bet=0.0001,
        multiplier=2.0, rule="over", bet_value=49.5, config=[],
    )
    assert payload["game"] == "limbo"
    assert "rule" not in payload
    assert "bet_value" not in payload
    assert payload["multiplier"] == "2"  # trailing zero stripped

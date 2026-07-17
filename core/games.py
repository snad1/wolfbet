"""WolfBet game registry + auto-bet builders — shared by tg/engine.py and wolfbet.py.

Phase 6 refactor (2026-07-17): single source of truth for endpoints, labels,
dynamic x-hash-api, strategy translation. Both engines import from here.

Wolfbet-repo-local; NOT part of the shared casino-shared library because the
other 5 bots (stake / cryptogames / duckdice / shuffle / goated) don't hit
WolfBet endpoints. See audits/cli-vs-tg-parity.md Phase 6 for rationale.
"""

import hashlib
import time
from typing import Optional

from core.strategy import StrategyRule  # noqa: F401 (used as parameter type)


# ── Game endpoint registry ──────────────────────────────────
# game_key → (api_path, api_version, rate_limit_default, hash_spec)
#
# hash_spec semantics:
#   None         → no x-hash-api header required
#   "dynamic"    → compute per-request via wolfbet_hash(body_str)
#   <hex string> → send the exact string verbatim (legacy manual endpoints)
GAME_ENDPOINTS = {
    "dice":        ("/v1/bet/place",         1, 480,  None),
    "dice-manual": ("/v1/dice/manual/play",  1, 720,  "dynamic"),
    "limbo":       ("/v2/limbo/manual/play", 2, 500,  "dynamic"),
    "dice-auto":   ("/v2/dice/auto/play",    2, 4000, "dynamic"),
    "limbo-auto":  ("/v2/limbo/auto/play",   2, 4000, "dynamic"),
}

GAME_LABELS = {
    "dice":        "Dice (API v1)",
    "dice-manual": "Dice Manual (v1)",
    "limbo":       "Limbo (v2)",
    "dice-auto":   "Dice Auto (v2)",
    "limbo-auto":  "Limbo Auto (v2)",
}

AUTO_ENDPOINTS = {
    "dice-auto":  {"start": "/v2/dice/auto/start",  "stop": "/v2/dice/auto/stop"},
    "limbo-auto": {"start": "/v2/limbo/auto/start", "stop": "/v2/limbo/auto/stop"},
}

# /auto/start rate-limit window is undocumented (memory says ~20s, not 60s).
# Enforce a floor between requests; rely on 429 backoff to probe the actual window.
AUTO_START_MIN_INTERVAL = 1.0


# ── Pure helpers ────────────────────────────────────────────
def wolfbet_hash(body_str: str = "") -> str:
    """WolfBet x-hash-api: md5(body) interleaved char-by-char with md5(nonce)."""
    md5_body  = hashlib.md5(body_str.encode()).hexdigest()
    md5_nonce = hashlib.md5(str(int(time.time() * 1000)).encode()).hexdigest()
    return "".join(a + b for a, b in zip(md5_body, md5_nonce))


def is_auto_game(game_key: str) -> bool:
    """True if game_key uses the /auto/* endpoint surface."""
    return game_key in AUTO_ENDPOINTS


def fmt_multiplier(m: float) -> str:
    """Format multiplier as WolfBet expects: no trailing zeros ('2' not '2.0', '2.01' kept)."""
    s = f"{m:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def request_hash_header(game_key: str, body_str: str = "") -> Optional[str]:
    """Return the x-hash-api header value for a game_key, or None if not required.

    Resolves the hash_spec sentinel:
      None      → return None (no header)
      "dynamic" → return wolfbet_hash(body_str)
      <string>  → return the string verbatim (legacy manual endpoints)
    """
    ep = GAME_ENDPOINTS.get(game_key)
    if not ep or not ep[3]:
        return None
    spec = ep[3]
    return wolfbet_hash(body_str) if spec == "dynamic" else spec


def custom_rule_to_wolfbet(r) -> Optional[dict]:
    """Translate a client StrategyRule into a WolfBet auto-bet rule dict.

    Only sequence-mode conditions are supported by WolfBet auto-bet; profit /
    bet-threshold conditions return None (client-side enforcement only).
    """
    if r.cond_type != "sequence":
        return None
    trigger_map = {"win": "win", "loss": "lose", "bet": "bet"}
    type_map = {
        "every":        "every",
        "every_streak": "streak",
        "first_streak": "streak",
        "streak_above": "streakGreater",
    }
    wb_type    = type_map.get(r.cond_mode)
    wb_trigger = trigger_map.get(r.cond_trigger)
    if not (wb_type and wb_trigger):
        return None
    try:
        av = float(r.action_value) if r.action_value not in (None, "") else 0.0
    except (TypeError, ValueError):
        av = 0.0
    action_map = {
        "reset_amount":    [{"name": "resetAmount"}],
        "increase_amount": [{"name": "increaseAmountPercent", "value": av}],
        "decrease_amount": [{"name": "decreaseAmountPercent", "value": av}],
        "add_amount":      [{"name": "increaseAmountValue",   "value": av}],
        "deduct_amount":   [{"name": "decreaseAmountValue",   "value": av}],
        "set_amount":      [{"name": "setAmount",             "value": av}],
        "switch":          [{"name": "switch"}],
        "stop":            [{"name": "stop"}],
    }
    wb_command = action_map.get(r.action)
    if not wb_command:
        return None
    return {
        "command": wb_command,
        "when":    [{"name": wb_trigger, "value": int(r.cond_value), "type": wb_type}],
    }


def build_auto_config(strategy_key: str, loss_mult: float, win_mult: float,
                      base_bet: float, game: str, custom_rules: list,
                      stop_conditions: Optional[dict] = None,
                      delay_martin_threshold: int = 3,
                      logger=None, log_prefix: str = "") -> list:
    """Translate strategy_key + stop conditions into WolfBet auto-bet config.

    Args:
        strategy_key: '1'..'8' (Flat, Martingale, Anti-Martingale, D'Alembert,
            Paroli, WolfRider, Delay Martingale, Rule-Based).
        loss_mult / win_mult: strategy multipliers.
        base_bet: base bet (used as unit for D'Alembert step).
        game: game_key (WolfRider adds a switch action for 'dice-auto').
        custom_rules: list of StrategyRule (Strategy 8 only; ignored otherwise).
        stop_conditions: dict with any of 'max_profit', 'max_loss', 'max_bets',
            'stop_on_balance' (Optional).
        delay_martin_threshold: streak threshold for Delay Martingale.
        logger: optional logger for translation warnings.
        log_prefix: optional prefix for log lines (TG uses 'User N:' style).
    """
    rules = []
    loss_pct = round((loss_mult - 1) * 100)
    win_pct  = round((win_mult  - 1) * 100)

    if strategy_key == "1":  # Flat
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]})
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "lose", "value": 1, "type": "every"}]})
    elif strategy_key == "2":  # Martingale
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]})
        rules.append({"command": [{"name": "increaseAmountPercent", "value": loss_pct}], "when": [{"name": "lose", "value": 1, "type": "every"}]})
    elif strategy_key == "3":  # Anti-Martingale
        rules.append({"command": [{"name": "increaseAmountPercent", "value": win_pct}], "when": [{"name": "win", "value": 1, "type": "every"}]})
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "lose", "value": 1, "type": "every"}]})
    elif strategy_key == "4":  # D'Alembert
        unit = base_bet
        rules.append({"command": [{"name": "decreaseAmountValue", "value": unit}], "when": [{"name": "win", "value": 1, "type": "every"}]})
        rules.append({"command": [{"name": "increaseAmountValue", "value": unit}], "when": [{"name": "lose", "value": 1, "type": "every"}]})
    elif strategy_key == "5":  # Paroli
        rules.append({"command": [{"name": "increaseAmountPercent", "value": win_pct}], "when": [{"name": "win", "value": 1, "type": "every"}]})
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "lose", "value": 1, "type": "every"}]})
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 3, "type": "streak"}]})
    elif strategy_key == "6":  # WolfRider (dice only — switch direction)
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]})
        rules.append({"command": [{"name": "increaseAmountPercent", "value": loss_pct}], "when": [{"name": "lose", "value": 1, "type": "every"}]})
        if game == "dice-auto":
            rules.append({"command": [{"name": "switch"}], "when": [{"name": "bet", "value": 1, "type": "every"}]})
    elif strategy_key == "7":  # Delay Martingale
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]})
        rules.append({"command": [{"name": "increaseAmountPercent", "value": loss_pct}], "when": [{"name": "lose", "value": delay_martin_threshold, "type": "streakGreater"}]})
    elif strategy_key == "8":  # Rule-Based — translate user's custom rules
        for r in (custom_rules or []):
            wb_rule = custom_rule_to_wolfbet(r)
            if wb_rule:
                rules.append(wb_rule)
            elif logger is not None:
                logger.warning("%srule '%s' cannot be expressed in WolfBet auto-bet format — skipped",
                               log_prefix, getattr(r, "description", "") or getattr(r, "action", "?"))
        if not rules:
            # Fallback: no translatable rules → flat behaviour so the session can still start
            rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]})
    else:  # Default flat
        rules.append({"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]})

    # Stop conditions
    if stop_conditions:
        mp = stop_conditions.get("max_profit")
        ml = stop_conditions.get("max_loss")
        mb = stop_conditions.get("max_bets")
        sb = stop_conditions.get("stop_on_balance")
        if mp is not None:
            rules.append({"command": [{"name": "stop"}], "when": [{"name": "profit", "value": float(mp), "type": "gte"}]})
        if ml is not None:
            rules.append({"command": [{"name": "stop"}], "when": [{"name": "loss", "value": float(ml), "type": "gt"}]})
        if mb is not None:
            rules.append({"command": [{"name": "stop"}], "when": [{"name": "bet", "value": int(mb), "type": "every"}]})
        if sb is not None:
            rules.append({"command": [{"name": "stop"}], "when": [{"name": "balance", "value": float(sb), "type": "lt"}]})

    return rules


def build_auto_start_payload(game: str, currency: str, base_bet: float,
                             multiplier: float, rule: str, bet_value: float,
                             config: list) -> dict:
    """Build the /auto/start body — includes game key, strategy multiplier, config."""
    base = {
        "currency":   currency,
        "amount":     f"{base_bet:.8f}",
        "multiplier": fmt_multiplier(multiplier),
        "config":     config,
    }
    if game == "limbo-auto":
        base["game"] = "limbo"
    elif game == "dice-auto":
        base["game"] = "dice"
        base["rule"] = rule
        base["bet_value"] = f"{bet_value:.2f}"
    return base

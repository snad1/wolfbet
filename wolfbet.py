#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║   🐺  WolfBet AutoBot v2.22.0  — Live Dashboard         ║
║   Auto-betting engine with live TUI & session logging   ║
╚══════════════════════════════════════════════════════════╝
"""

import os, sys, json, time, sqlite3, threading, signal, random, string, argparse, logging, hashlib, csv  # noqa: E401
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, List
from logging.handlers import TimedRotatingFileHandler
try:
    import readline  # enables arrow keys, history, and line editing in input()
except ImportError:
    pass  # fallback — input() still works, just no line editing

try:
    import requests
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich.align import Align
    from rich.rule import Rule
    from rich import box
except ImportError:
    print("Missing dependencies. Run:  pip install requests rich")
    sys.exit(1)

# ── Shared core imports (single source of truth) ─────────
from core.strategy import (
    STRATEGIES, STRATEGY_NAMES, STRATEGY_BY_NAME,
    RULE_ACTIONS, RULE_ACTION_LABELS, SEQ_MODES,
    StrategyRule, describe_rule as _describe_rule,
    cmp as _cmp, load_rules_from_text as _load_rules_from_text,
)
from core.database import init_db as _core_init_db, migrate_db, db_connect, secure_path
from core.engine import (
    compute_next_bet as _core_compute_next_bet,
    wolfrider_switch as _core_wolfrider_switch,
    apply_action as _core_apply_action,
    evaluate_rules as _core_evaluate_rules,
)
from core.games import (
    GAME_ENDPOINTS, AUTO_ENDPOINTS, GAME_LABELS, AUTO_START_MIN_INTERVAL,
    wolfbet_hash, is_auto_game, fmt_multiplier, request_hash_header,
    custom_rule_to_wolfbet, build_auto_config, build_auto_start_payload,
)

# ══════════════════════════════════════════════════════════
#  CONSTANTS (configurable via environment variables)
# ══════════════════════════════════════════════════════════
API_BASE     = os.environ.get("WOLFBET_API_BASE",    "https://wolfbet.com/api")

# Phase 6: game registry, endpoint tables, hash + auto-bet builders live in
# wolfbet/core/games.py — imported at the top of this file. Legacy in-repo
# copies of _wolfbet_hash / _is_auto_game / GAME_ENDPOINTS etc. removed.
#
# NOTE: this CLI's legacy static-hash manual endpoints (dice-manual / limbo)
# now go through the shared "dynamic" hash_spec instead — the two hardcoded
# md5 strings that used to live here weren't valid without a matching nonce
# in any modern WolfBet session anyway (dead code). Both v1.15.7 (TG) and
# v2.15.0 (CLI) already committed to "dynamic" for those endpoints.


# ══════════════════════════════════════════════════════════
#  PHASE 2 helpers — session robustness (ported from tg/engine.py)
# ══════════════════════════════════════════════════════════
def _parse_streak_delay(value) -> tuple:
    """Parse 'N:seconds' (or 'N:multiplier') config. Returns (N, X) or (0, 0.0)."""
    if not value:
        return (0, 0.0)
    try:
        parts = str(value).split(":")
        return (int(parts[0]), float(parts[1]))
    except (IndexError, ValueError):
        return (0, 0.0)


def _apply_basebet_pct() -> bool:
    """Recompute base_bet from basebet_pct * current_balance. Returns True if changed."""
    if state.basebet_pct <= 0 or state.current_balance <= 0:
        return False
    new_base = round(state.current_balance * state.basebet_pct, 8)
    if new_base <= 0:
        return False
    if abs(new_base - state.base_bet) < 1e-12:
        return False
    with state.lock:
        state.base_bet    = new_base
        state.current_bet = new_base
    logger.info("BASEBET_PCT: base_bet → %.8f (balance %.8f × %.6f)",
                new_base, state.current_balance, state.basebet_pct)
    return True


def _get_streakbet_loss_factor() -> float:
    """Return current_bet multiplier when on a qualifying loss streak. 1.0 = no change."""
    count, mult = state.streakbet_loss
    streak = state.current_streak
    if count > 0 and streak < 0 and abs(streak) > 0 and abs(streak) % count == 0:
        return mult
    return 1.0


def _get_streak_delay() -> float:
    """Return max applicable streak delay in seconds, or 0.0."""
    streak = state.current_streak
    delay = 0.0
    if streak < 0 and state.streak_delay_loss[0] > 0:
        count, d = state.streak_delay_loss
        if abs(streak) > 0 and abs(streak) % count == 0:
            delay = max(delay, d)
    if streak > 0 and state.streak_delay_win[0] > 0:
        count, d = state.streak_delay_win
        if streak % count == 0:
            delay = max(delay, d)
    if state.streak_delay_bets[0] > 0 and state.total_bets > 0:
        count, d = state.streak_delay_bets
        if state.total_bets % count == 0:
            delay = max(delay, d)
    return delay


def _check_milestone(last_bet_state: str) -> Optional[str]:
    """Return a milestone reason string when a threshold is crossed, or None."""
    # Bets milestone
    if state.milestone_bets > 0 and state.total_bets > 0 and state.total_bets % state.milestone_bets == 0:
        return f"{state.total_bets} bets"
    # Wins milestone
    if state.milestone_wins > 0 and last_bet_state == "win" and state.wins > 0 and state.wins % state.milestone_wins == 0:
        return f"{state.wins} wins"
    # Losses milestone
    if state.milestone_losses > 0 and last_bet_state != "win" and state.losses > 0 and state.losses % state.milestone_losses == 0:
        return f"{state.losses} losses"
    # Profit milestone — every X profit units crossed
    if state.milestone_profit > 0 and state.profit > 0:
        threshold = state.milestone_profit
        # crossed a new multiple of threshold since last check?
        crossed = int(state.profit / threshold) * threshold
        if crossed > state._last_profit_milestone:
            with state.lock:
                state._last_profit_milestone = crossed
            return f"{crossed:.8f} profit"
    return None


def _reset_session_for_recurring() -> None:
    """Reset per-session counters while preserving config. Starts a new DB session row."""
    with state.lock:
        state.session_id     = _db_start_session()
        state.session_start  = time.time()
        state.total_bets     = 0
        state.wins           = 0
        state.losses         = 0
        state.profit         = 0.0
        state.wagered        = 0.0
        state.current_streak = 0
        state.max_win_streak = 0
        state.max_loss_streak = 0
        state.highest_bet    = 0.0
        state.highest_win    = 0.0
        state.biggest_loss   = 0.0
        state.recent_bets.clear()
        state.profit_history.clear()
        state.start_balance  = state.current_balance
        state.highest_balance = state.current_balance
        state.lowest_balance  = state.current_balance
        state.current_bet    = state.base_bet
        # Reset auto-bet UUID so betting_loop fires a fresh /auto/start next tick.
        state._auto_uuid     = None
        state._last_profit_milestone = 0.0
        state.consecutive_errors = 0
        state.backoff_delay  = 1.0
        state.last_error     = ""
        state.status         = f"Recurring restart — session #{state.session_id}"
    logger.info("RECURRING RESTART complete: new session #%d, base_bet=%.8f, balance=%.8f",
                state.session_id, state.base_bet, state.current_balance)


# ══════════════════════════════════════════════════════════
#  PHASE 4b — runtime tweak grammar (mirrors tg/handlers.cmd_tweak)
# ══════════════════════════════════════════════════════════
_TWEAK_ALIAS = {
    "delay":       "bet_delay",
    "sdloss":      "streak_delay_loss",
    "sdwin":       "streak_delay_win",
    "sdbets":      "streak_delay_bets",
    "sbloss":      "streakbet_loss",
    "bbpct":       "basebet_pct",
    "maxprofit":   "max_profit",
    "maxloss":     "max_loss",
    "maxbets":     "max_bets",
    "maxwins":     "max_wins",
    "minbalance":  "stop_on_balance",
    "msbets":      "milestone_bets",
    "mswins":      "milestone_wins",
    "mslosses":    "milestone_losses",
    "msprofit":    "milestone_profit",
    "pi":          "profit_increment",
    "pt":          "profit_threshold",
    "basebet":     "base_bet",
    "lossmult":    "loss_mult",
    "winmult":     "win_mult",
}
_TWEAK_PARAMS = {
    "bet_delay", "basebet_pct",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold", "recurring",
    "base_bet", "multiplier", "loss_mult", "win_mult",
}
_TWEAK_OFF_VALUES = (None, "", "off", "none", "0", "disable", "false", "no", "n")


def _apply_mutation(changes: dict) -> list:
    """Apply {param → value} mutations to state.* — returns human status list.

    Mirrors tg/engine.py:1274-1387 for CLI parity. Raises ValueError on malformed
    numeric input; caller is responsible for surfacing that cleanly."""
    msgs = []

    def _is_off(v):
        return isinstance(v, str) and v.lower() in _TWEAK_OFF_VALUES or v is None

    # ── Instant (no restart) ──
    if "bet_delay" in changes:
        state.bet_delay = float(changes["bet_delay"])
        msgs.append(f"Delay: {state.bet_delay:.3f}s")

    if "basebet_pct" in changes:
        v = changes["basebet_pct"]
        state.basebet_pct = 0.0 if _is_off(v) else float(v)
        if state.basebet_pct > 0 and state.current_balance > 0:
            _apply_basebet_pct()
            msgs.append(f"Basebet pct: {state.basebet_pct*100:.4f}% → base={state.base_bet:.8f}")
        else:
            msgs.append("Basebet pct: off")

    for attr, label in [("streak_delay_loss", "Streak delay loss"),
                        ("streak_delay_win",  "Streak delay win"),
                        ("streak_delay_bets", "Streak delay bets"),
                        ("streakbet_loss",    "Streakbet loss")]:
        if attr in changes:
            v = changes[attr]
            setattr(state, attr, _parse_streak_delay(None if _is_off(v) else v))
            tup = getattr(state, attr)
            if tup[0] > 0:
                msgs.append(f"{label}: every {tup[0]} → {tup[1]:.3f}")
            else:
                msgs.append(f"{label}: off")

    # Stop conditions — instant
    for attr, label, cast in [("max_profit",      "Max profit",  float),
                              ("max_loss",        "Max loss",    float),
                              ("max_bets",        "Max bets",    int),
                              ("max_wins",        "Max wins",    int),
                              ("stop_on_balance", "Min balance", float)]:
        if attr in changes:
            v = changes[attr]
            if _is_off(v):
                setattr(state, attr, None)
                msgs.append(f"{label}: off")
            else:
                setattr(state, attr, cast(v))
                msgs.append(f"{label}: {getattr(state, attr)}")

    # Milestone thresholds — instant
    for attr, label in [("milestone_bets",   "Milestone bets"),
                        ("milestone_wins",   "Milestone wins"),
                        ("milestone_losses", "Milestone losses")]:
        if attr in changes:
            v = changes[attr]
            setattr(state, attr, 0 if _is_off(v) else int(v))
            msgs.append(f"{label}: {getattr(state, attr) or 'off'}")
    if "milestone_profit" in changes:
        v = changes["milestone_profit"]
        state.milestone_profit = 0.0 if _is_off(v) else float(v)
        msgs.append(f"Milestone profit: {state.milestone_profit:.8f}" if state.milestone_profit else "Milestone profit: off")

    # Profit increment — instant
    if "profit_increment" in changes:
        v = changes["profit_increment"]
        state.profit_increment = None if _is_off(v) else float(v)
        msgs.append(f"Profit increment: {state.profit_increment:.8f}" if state.profit_increment else "Profit increment: off")
    if "profit_threshold" in changes:
        v = changes["profit_threshold"]
        state.profit_threshold = None if _is_off(v) else float(v)
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold
        msgs.append(f"Profit threshold: {state.profit_threshold:.8f}" if state.profit_threshold else "Profit threshold: off")

    # Recurring — instant boolean
    if "recurring" in changes:
        v = str(changes["recurring"]).lower()
        state.recurring = v in ("on", "true", "yes", "y", "1")
        msgs.append(f"Recurring: {'on' if state.recurring else 'off'}")

    # ── Strategy-config changes — auto games clear UUID for fresh restart ──
    strategy_touched = False
    if "base_bet" in changes:
        state.base_bet = float(changes["base_bet"])
        state.current_bet = state.base_bet
        msgs.append(f"Base bet: {state.base_bet:.8f}")
        strategy_touched = True
    if "multiplier" in changes:
        state.multiplier = float(changes["multiplier"])
        msgs.append(f"Multiplier: {state.multiplier:.2f}")
        strategy_touched = True
    if "loss_mult" in changes:
        state.loss_mult = float(changes["loss_mult"])
        msgs.append(f"Loss mult: {state.loss_mult:.2f}")
        strategy_touched = True
    if "win_mult" in changes:
        state.win_mult = float(changes["win_mult"])
        msgs.append(f"Win mult: {state.win_mult:.2f}")
        strategy_touched = True

    if strategy_touched and is_auto_game(state.game) and state._auto_uuid:
        with state.lock:
            state._auto_uuid = None
        msgs.append("Auto-bet UUID cleared — restarts on next bet")

    return msgs


def _should_recurring_restart(reason: str) -> bool:
    """Whitelist safe stop-reasons for recurring restart. Loss / insufficient-balance never restart."""
    if not state.recurring:
        return False
    if state.profit < 0:
        return False
    # Positive whitelist — only these three reasons trigger recurring:
    #   Profit target reached | Max bets reached | Max wins reached
    safe_prefixes = ("Profit target", "Max bets", "Max wins")
    return any(reason.startswith(p) for p in safe_prefixes)


# ══════════════════════════════════════════════════════════
#  PHASE 3 metrics — observability helpers (ported from tg/engine.py:1487-1508)
# ══════════════════════════════════════════════════════════
def _compute_api_avg() -> float:
    """Rolling average API time in ms (0 when no samples yet)."""
    if state._api_ms_count == 0:
        return 0.0
    return state._api_ms_total / state._api_ms_count


def _compute_api_p50() -> float:
    """Median (p50) API time in ms from the last-500-sample window (0 when empty)."""
    if not state._api_samples:
        return 0.0
    ordered = sorted(state._api_samples)
    return ordered[len(ordered) // 2]


def _compute_on_target_pct() -> Optional[float]:
    """Fraction of bets whose API ≤ bet_delay*1000; None when delay disabled / no samples."""
    if state.bet_delay <= 0 or state._api_ms_count == 0:
        return None
    return (state._on_target_count / state._api_ms_count) * 100


def _compute_theoretical_max_bps() -> float:
    """Ceiling bps = 1 / max(bet_delay, api_avg). 0 when both are 0 (fresh session)."""
    api_avg_s = _compute_api_avg() / 1000.0
    floor = max(state.bet_delay, api_avg_s)
    return (1.0 / floor) if floor > 0 else 0.0


def _compute_efficiency_pct() -> float:
    """Actual bps as % of theoretical max (capped at 100). 0 when no ceiling yet."""
    tmax = _compute_theoretical_max_bps()
    if tmax <= 0:
        return 0.0
    return min(100.0, state.bets_per_second / tmax * 100)


def _record_api_ms(dt_ms: float) -> None:
    """Update all API-timing counters from a single request's dt_ms. Locks around state mutation."""
    with state.lock:
        state._last_api_ms   = dt_ms
        state._api_ms_total += dt_ms
        state._api_ms_count += 1
        state._api_samples.append(dt_ms)
        if dt_ms > state.peak_api_ms:
            state.peak_api_ms = dt_ms
        if state.bet_delay > 0 and dt_ms <= state.bet_delay * 1000:
            state._on_target_count += 1


def _record_cycle_ms(cycle_observed_ms: float) -> None:
    """EMA-smooth the per-bet cycle time; derive overhead = cycle − api − delay*1000."""
    with state.lock:
        if state._cycle_ms > 0:
            state._cycle_ms = state._cycle_ms * 0.9 + cycle_observed_ms * 0.1
        else:
            state._cycle_ms = cycle_observed_ms
        state._cycle_overhead_ms = max(0.0, state._cycle_ms - state._last_api_ms - state.bet_delay * 1000)
DB_PATH      = os.environ.get("WOLFBET_DB_PATH",     os.path.expanduser("~/.wolfbet_autobot.db"))
CONFIG_PATH  = os.environ.get("WOLFBET_CONFIG_PATH",  os.path.expanduser("~/.wolfbet_autobot.json"))
STATE_PATH   = os.environ.get("WOLFBET_STATE_PATH",   os.path.expanduser("~/.wolfbet_autobot_live.json"))
PID_PATH     = os.environ.get("WOLFBET_PID_PATH",     os.path.expanduser("~/.wolfbet_autobot.pid"))
PRESET_PATH  = os.environ.get("WOLFBET_PRESET_PATH",  os.path.expanduser("~/.wolfbet_presets.json"))
LOG_DIR      = os.environ.get("WOLFBET_LOG_DIR",      os.path.expanduser("~/.wolfbet_logs"))
VERSION      = "2.22.0"
APP_ENV      = os.environ.get("APP_ENV", "production")

# ── Daily rotating logger ──
# Default: WARNING (production). Use --verbose for DEBUG.
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("wolfbet")
logger.setLevel(logging.WARNING)
_log_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, "wolfbet.log"),
    when="midnight", backupCount=30, utc=False,
)
_log_handler.suffix = "%Y-%m-%d"
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_log_handler)

CURRENCIES  = ["btc", "eth", "ltc", "doge", "trx", "bch", "xrp", "usdt", "bnb", "ada", "matic"]

# ══════════════════════════════════════════════════════════
#  DATABASE SETUP (delegates to core.database)
# ══════════════════════════════════════════════════════════
def init_db():
    _core_init_db(DB_PATH)
    migrate_db(DB_PATH)

# ══════════════════════════════════════════════════════════
#  CONFIG PERSISTENCE
# ══════════════════════════════════════════════════════════
_CONFIG_KEYS = [
    "api_key", "currency", "game", "rule", "multiplier", "bet_value",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "profit_increment", "profit_threshold",
]

def _mask_key(key: str) -> str:
    """Mask API key for safe logging: show first 8 + last 4 chars."""
    if not key or len(key) < 16:
        return "***"
    return f"{key[:8]}…{key[-4:]}"

def save_config():
    data = {k: getattr(state, k) for k in _CONFIG_KEYS}
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Config saved (key=%s)", _mask_key(data.get("api_key", "")))

def load_config() -> bool:
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        for k in _CONFIG_KEYS:
            if k in data:
                setattr(state, k, data[k])
        # Environment variable override for API key (ISO 27001: no hardcoded secrets)
        env_key = os.environ.get("WOLFBET_API_KEY")
        if env_key:
            state.api_key = env_key
        secure_path(CONFIG_PATH)
        state.current_bet = state.base_bet
        # init profit milestone
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold
        # parse rules if present
        if state.custom_rules_text:
            state.custom_rules = _load_rules_from_text(state.custom_rules_text)
        return True
    except Exception:
        return False

# ══════════════════════════════════════════════════════════
#  PRESETS
# ══════════════════════════════════════════════════════════
_PRESET_KEYS = [
    "currency", "game", "rule", "multiplier", "bet_value",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "profit_increment", "profit_threshold",
    # Phase 4a additions — JSON-native Phase 2 fields:
    "recurring", "basebet_pct",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
]

# Fields stored on state as (N, X) tuples but round-tripped through JSON as "N:X" strings.
# Decoded on load via _parse_streak_delay (Phase 2 helper).
_STREAK_TUPLE_KEYS = [
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets",
    "streakbet_loss",
]

def _load_all_presets() -> dict:
    if not os.path.exists(PRESET_PATH):
        return {}
    try:
        with open(PRESET_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_all_presets(presets: dict):
    fd = os.open(PRESET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(presets, f, indent=2)

def save_preset(name: str):
    presets = _load_all_presets()
    data = {k: getattr(state, k) for k in _PRESET_KEYS}
    # Encode streak-tuple fields as "N:X" (matching the snapshot format).
    for k in _STREAK_TUPLE_KEYS:
        n, x = getattr(state, k)
        data[k] = f"{n}:{x}" if n > 0 else None
    presets[name] = data
    _save_all_presets(presets)

def load_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    data = presets[name]
    for k in _PRESET_KEYS:
        if k in data:
            setattr(state, k, data[k])
    # Decode streak-tuple fields via _parse_streak_delay (returns (0, 0.0) on empty/malformed).
    for k in _STREAK_TUPLE_KEYS:
        if k in data:
            setattr(state, k, _parse_streak_delay(data[k]))
    state.current_bet = state.base_bet
    if state.profit_threshold:
        state.next_profit_milestone = state.profit_threshold
    if state.custom_rules_text:
        state.custom_rules = _load_rules_from_text(state.custom_rules_text)
    return True

def delete_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    del presets[name]
    _save_all_presets(presets)
    return True

def list_presets() -> dict:
    return _load_all_presets()

# ══════════════════════════════════════════════════════════
#  RULE-BASED STRATEGY ENGINE  (v2 — DarkDice-inspired)
# ══════════════════════════════════════════════════════════

# ── Condition types ──
COND_TYPES = {
    "1": "sequence",   # streak / count based
    "2": "profit",     # profit / loss / balance based
    "3": "bet",        # bet property based
}

# SEQ_MODES imported from core.strategy
SEQ_TRIGGERS = {"1": "win", "2": "loss", "3": "bet"}

# ── Profit sub-options ──
PROFIT_FIELDS = {"1": "profit", "2": "loss", "3": "balance"}

# ── Bet sub-options ──
BET_FIELDS = {"1": "amount", "2": "number", "3": "winchance", "4": "payout"}

# ── Comparison operators (for profit and bet conditions) ──
CMP_OPS = {
    "1": ("gte", ">="),
    "2": ("gt",  ">"),
    "3": ("lte", "<="),
    "4": ("lt",  "<"),
}

# RULE_ACTIONS, StrategyRule, _describe_rule, _cmp, _load_rules_from_text
# all imported from core.strategy (single source of truth)


def _apply_action(rule: StrategyRule):
    """Execute a rule's action on the bot state (delegates to core.engine)."""
    mutations = _core_apply_action(
        rule, state.current_bet, state.base_bet,
        state.rule, state.multiplier, state.current_streak,
        initial_multiplier=state.initial_multiplier,
    )
    for k, v in mutations.items():
        if k == "stop_reason":
            state.status = v
            logger.warning("RULE STOP triggered: %s", rule.description)
        elif k == "running":
            state.running = v
        else:
            setattr(state, k, v)
    if mutations:
        logger.debug("RULE FIRED: %s → %s", rule.description, list(mutations.keys()))


def apply_rules(bet_state: str):
    """Evaluate and apply all rule-based strategy rules (delegates to core.engine)."""
    triggered = _core_evaluate_rules(
        state.custom_rules, bet_state, state.wins, state.losses,
        state.total_bets, state.current_streak, state.profit,
        state.current_balance, state.current_bet, state.multiplier,
    )
    for rule in triggered:
        logger.debug("RULE FIRED: %s", rule.description)
        _apply_action(rule)


# ══════════════════════════════════════════════════════════
#  BOT STATE — single shared object
# ══════════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.lock = threading.Lock()

        # ── runtime flags ──
        self.running  = False
        self.paused   = False
        self.stopping = False

        # ── config ──
        self.api_key        = ""
        self.currency       = "usdt"
        self.game           = "dice"
        self.rule           = "over"
        self.multiplier     = 2.01
        self.initial_multiplier = 2.01
        self.bet_value      = 50.49
        self.base_bet       = 0.0001
        self.current_bet    = 0.0001
        self.strategy       = "Martingale"
        self.strategy_key   = "2"
        self.win_mult       = 1.0
        self.loss_mult      = 2.0

        # ── rule-based strategy ──
        self.custom_rules: List[StrategyRule] = []
        self.custom_rules_text: str = ""

        # ── stop conditions ──
        self.max_profit      : Optional[float] = None
        self.max_loss        : Optional[float] = None
        self.max_bets        : Optional[int]   = None
        self.max_wins        : Optional[int]   = None
        self.stop_on_balance : Optional[float] = None

        # ── session info ──
        self.session_id     = None
        self.session_start  = time.time()
        self.total_bets     = 0
        self.wins           = 0
        self.losses         = 0
        self.profit         = 0.0
        self.wagered        = 0.0
        self.start_balance  = 0.0
        self.current_balance= 0.0

        # ── streaks ──
        self.current_streak  = 0   # + = win streak, – = loss streak
        self.max_win_streak  = 0
        self.max_loss_streak = 0

        # ── extremes ──
        self.highest_bet     = 0.0
        self.highest_win     = 0.0
        self.biggest_loss    = 0.0
        self.highest_balance = 0.0
        self.lowest_balance  = float("inf")

        # ── bet pacing ──
        self.bet_delay       = 0       # seconds between bets (configurable)
        self.backoff_delay   = 1.0     # current backoff (grows on consecutive errors)
        self.max_backoff     = 30.0    # cap for exponential backoff
        self.consecutive_errors = 0
        self._insufficient_balance = False

        # ── rate limiting ──
        self.rate_limit      = 480
        self.rate_remaining  = 480
        self.rate_wait       = False
        self.rate_backoff    = 2.0     # current probe interval (exponential: 2→4→8→16→32)
        self.rate_reset_at   = 0.0     # legacy (kept for monitor compatibility)

        # ── auto-bet state (dice-auto / limbo-auto) ──
        self._auto_uuid: Optional[str] = None
        self._auto_start_last_time: float = 0.0
        self._auto_start_remaining: Optional[int] = None

        # ── Phase 3 observability metrics ──
        self._last_api_ms   : float = 0.0
        self._api_ms_total  : float = 0.0
        self._api_ms_count  : int   = 0
        self.peak_api_ms    : float = 0.0
        self._api_samples   : deque = deque(maxlen=500)
        self._on_target_count: int   = 0
        self._cycle_ms      : float = 0.0            # EMA-smoothed per-bet cycle
        self._cycle_overhead_ms: float = 0.0         # cycle − api − delay*1000
        self._last_cycle_start: float = 0.0

        # ── performance ──
        self.bets_per_minute = 0.0
        self.bets_per_second = 0.0
        self.peak_bps        = 0.0   # highest bets in a single second
        self.low_bps         = float("inf")  # lowest bets in a completed second
        self.peak_bpm        = 0.0   # highest bets in a single minute
        self.low_bpm         = float("inf")  # lowest bets in a completed minute
        self._bets_this_sec  = 0     # counter for current second
        self._bets_this_min  = 0     # counter for current minute
        self._current_sec    = 0     # which second we're in (int timestamp)
        self._current_min    = 0     # which minute we're in (int timestamp // 60)
        self.bet_timestamps  = deque(maxlen=120)

        # ── profit history for sparkline / graph ──
        self.profit_history  = deque(maxlen=80)
        self.profit_history.append(0.0)

        # ── recent bets ──
        self.recent_bets = deque(maxlen=10)

        # ── status messages ──
        self.status     = "Initializing…"
        self.last_error = ""

        # ── profit-based base bet increment ──
        self.profit_increment    : Optional[float] = None   # amount to add to base_bet each milestone
        self.profit_threshold    : Optional[float] = None   # profit interval that triggers increment
        self.next_profit_milestone: float = 0.0              # next profit level to trigger bump

        # ── strategy internal state ──
        self.dalembert_unit = 0
        self.paroli_count   = 0
        self.delay_martin_threshold = 3   # Delay Martingale: flat for this many consecutive losses

        # ── Phase 2 session robustness ──
        # Format: "N:seconds" — after every N consecutive losses/wins/bets, pause X seconds
        self.streak_delay_loss: tuple = (0, 0.0)
        self.streak_delay_win : tuple = (0, 0.0)
        self.streak_delay_bets: tuple = (0, 0.0)
        # Format: "N:multiplier" — after every N consecutive losses, multiply current_bet by X (0<X<1 reduces)
        self.streakbet_loss   : tuple = (0, 1.0)
        # Base bet as % of balance (0 disables); recomputed periodically from live balance
        self.basebet_pct      : float = 0.0
        # Milestone thresholds — 0 disables each
        self.milestone_bets   : int = 100
        self.milestone_wins   : int = 0
        self.milestone_losses : int = 0
        self.milestone_profit : float = 0.0
        self._last_profit_milestone: float = 0.0
        # Recurring: restart session after non-loss, non-insufficient-balance stop with profit≥0
        self.recurring        : bool = False


state   = BotState()
console = Console()

# ══════════════════════════════════════════════════════════
#  API HELPERS
# ══════════════════════════════════════════════════════════
def _headers(game_key: str = None, body_str: str = "") -> dict:
    """Build request headers. Hash resolution delegated to core.games.request_hash_header."""
    h = {
        "Authorization":    f"Bearer {state.api_key}",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type":     "application/json",
    }
    hash_val = request_hash_header(game_key or state.game, body_str)
    if hash_val is not None:
        h["x-hash-api"]    = hash_val
        h["x-client-type"] = "Web-Application"
    return h

def _update_rate_limit(resp):
    try:
        ep = GAME_ENDPOINTS.get(state.game, ("", 1, 480, None))
        default_limit = ep[2]
        limit     = int(resp.headers.get("x-ratelimit-limit",     default_limit))
        remaining = int(resp.headers.get("x-ratelimit-remaining", state.rate_remaining))
        with state.lock:
            state.rate_limit     = limit
            state.rate_remaining = remaining
            if remaining <= 2 and not state.rate_wait:
                state.rate_wait      = True
                state.rate_backoff   = 2.0   # start probing at 2s
    except Exception:
        pass

def api_get_balances():
    r = requests.get(f"{API_BASE}/v1/user/balances", headers=_headers("dice"), timeout=12)
    _update_rate_limit(r)
    r.raise_for_status()
    return r.json().get("balances", [])

def _build_bet_payload(amount: float) -> dict:
    gk = state.game
    if gk == "dice":
        return {
            "currency":   state.currency,
            "game":       "dice",
            "amount":     f"{amount:.8f}",
            "rule":       state.rule,
            "multiplier": f"{state.multiplier}",
            "bet_value":  f"{state.bet_value}",
        }
    elif gk == "dice-manual":
        return {
            "currency":   state.currency,
            "game":       "dice",
            "amount":     f"{amount:.8f}",
            "rule":       state.rule,
            "multiplier": f"{state.multiplier}",
            "bet_value":  f"{state.bet_value}",
            "auto":       1,
        }
    elif gk == "limbo":
        return {
            "currency":   state.currency,
            "game":       "limbo",
            "amount":     f"{amount:.8f}",
            "multiplier": f"{state.multiplier}",
            "auto":       1,
        }
    elif gk in ("dice-auto", "limbo-auto"):
        return {"uuid": state._auto_uuid}
    return {}


# ══════════════════════════════════════════════════════════
#  AUTO-BET  (dice-auto / limbo-auto)
#  Endpoints, hash, strategy translation → wolfbet/core/games.py (Phase 6).
#  Thin state.*-parameter shims below.
# ══════════════════════════════════════════════════════════
def _build_auto_config() -> list:
    """Adapter: state.* → shared build_auto_config()."""
    return build_auto_config(
        state.strategy_key, state.loss_mult, state.win_mult, state.base_bet,
        state.game, state.custom_rules,
        {"max_profit": state.max_profit, "max_loss": state.max_loss,
         "max_bets": state.max_bets, "stop_on_balance": state.stop_on_balance},
        state.delay_martin_threshold, logger,
    )


def _build_auto_start_payload() -> dict:
    """Adapter: state.* → shared build_auto_start_payload()."""
    return build_auto_start_payload(
        state.game, state.currency, state.base_bet, state.multiplier,
        state.rule, state.bet_value, _build_auto_config(),
    )


def _update_auto_start_remaining(headers) -> None:
    """Learn live remaining quota from x-ratelimit-remaining."""
    try:
        v = headers.get("x-ratelimit-remaining")
        if v is not None:
            state._auto_start_remaining = int(v)
    except (ValueError, TypeError):
        pass


def _rotate_server_seed() -> bool:
    """Rotate the WolfBet server seed via GET /v1/game/seed/refresh."""
    url = f"{API_BASE}/v1/game/seed/refresh"
    try:
        r = requests.get(url, headers=_headers("dice"), timeout=12)
        if r.status_code == 200:
            logger.info("Server seed rotated")
            return True
        logger.warning("seed/refresh HTTP %d: %s", r.status_code, (r.text or "")[:120])
    except Exception as e:
        logger.warning("seed/refresh exception: %s: %r", type(e).__name__, e)
    return False


def api_auto_start() -> bool:
    """Start a WolfBet auto-bet session; sets state._auto_uuid on success.

    Pacing: 1s floor + quota-scaled pre-wait (6s / 4s / 2s at remaining
    0 / 1 / 2). 429 retries with exponential backoff up to 60s (respects
    Retry-After header).
    """
    endpoints = AUTO_ENDPOINTS.get(state.game)
    if not endpoints:
        with state.lock:
            state.last_error = f"Auto-bet not available for {state.game}"
        return False
    wait = (state._auto_start_last_time + AUTO_START_MIN_INTERVAL) - time.monotonic()
    if state._auto_start_remaining is not None:
        quota_floor = {0: 6.0, 1: 4.0, 2: 2.0}.get(state._auto_start_remaining, 0.0)
        wait = max(wait, quota_floor)
    if wait > 0:
        logger.info("Pacing auto/start, waiting %.2fs (remaining=%s)",
                    wait, state._auto_start_remaining)
        time.sleep(wait)
    url = f"{API_BASE}{endpoints['start']}"
    payload = _build_auto_start_payload()
    body_str = json.dumps(payload, separators=(",", ":"))
    backoffs = [1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 25.0, 60.0]
    for attempt in range(len(backoffs) + 1):
        state._auto_start_last_time = time.monotonic()
        try:
            t0 = time.time()
            r = requests.post(url, headers=_headers(body_str=body_str),
                              json=payload, timeout=15)
            _record_api_ms((time.time() - t0) * 1000)
            _update_auto_start_remaining(r.headers)
            if r.status_code == 200:
                data = r.json()
                uuid = data.get("autoBet", {}).get("uuid")
                if uuid:
                    with state.lock:
                        state._auto_uuid = uuid
                    logger.info("Auto-bet started, uuid=%s (remaining=%s)",
                                uuid, state._auto_start_remaining)
                    return True
                with state.lock:
                    state.last_error = f"Auto-bet start: no UUID in response"
                logger.error("auto/start no UUID: %s", data)
                return False
            if r.status_code == 429 and attempt < len(backoffs):
                delay = float(r.headers.get("Retry-After", backoffs[attempt]))
                logger.warning("auto/start 429, retrying in %.1fs (attempt %d/%d)",
                               delay, attempt + 1, len(backoffs))
                time.sleep(delay)
                continue
            with state.lock:
                state.last_error = f"Auto-bet start failed: HTTP {r.status_code}"
            logger.error("auto/start HTTP %d: %s", r.status_code, r.text[:200])
            return False
        except requests.exceptions.RequestException as e:
            if attempt < len(backoffs):
                delay = backoffs[attempt]
                logger.warning("auto/start network error (%s), retrying in %.1fs",
                               type(e).__name__, delay)
                time.sleep(delay)
                continue
            with state.lock:
                state.last_error = f"Auto-bet start: {type(e).__name__}"
            return False
    return False


def api_auto_stop() -> None:
    """Best-effort stop of the auto-bet session; always clears state._auto_uuid."""
    endpoints = AUTO_ENDPOINTS.get(state.game)
    if not endpoints or not state._auto_uuid:
        return
    url = f"{API_BASE}{endpoints['stop']}"
    payload = {"uuid": state._auto_uuid}
    body_str = json.dumps(payload, separators=(",", ":"))
    try:
        requests.post(url, headers=_headers(body_str=body_str),
                      json=payload, timeout=12)
        logger.info("Auto-bet stopped, uuid=%s", state._auto_uuid)
    except Exception:
        pass
    with state.lock:
        state._auto_uuid = None


def _fmt_auto_stop_reason(sr) -> str:
    """Turn WolfBet's auto-stop payload into a human message."""
    if isinstance(sr, dict):
        tmpl = sr.get("message") or ""
        for k, v in (sr.get("values") or {}).items():
            try:
                v_fmt = f"{float(v):.8f}" if "." in str(v) or "e" in str(v).lower() else str(v)
            except (TypeError, ValueError):
                v_fmt = str(v)
            tmpl = tmpl.replace("{{" + str(k) + "}}", v_fmt)
        return tmpl.strip() or str(sr)
    return str(sr)


def _recover_expired_session() -> bool:
    """Recover from 'Session has ended' — rotate seed, stop dead UUID, start new."""
    logger.warning("Auto-bet session expired — rotating seed + restarting")
    _rotate_server_seed()
    api_auto_stop()
    return api_auto_start()


def api_place_bet(amount: float) -> Optional[dict]:
    payload = _build_bet_payload(amount)
    ep = GAME_ENDPOINTS.get(state.game, ("/v1/bet/place", 1, 480, None))
    url = f"{API_BASE}{ep[0]}"
    body_str = json.dumps(payload, separators=(",", ":"))
    logger.debug("BET payload=%s url=%s", body_str, url)
    try:
        t0 = time.time()
        r = requests.post(url, headers=_headers(body_str=body_str),
                          json=payload, timeout=15)
        _record_api_ms((time.time() - t0) * 1000)
        _update_rate_limit(r)

        if r.status_code == 200:
            data = r.json()
            if "error" in data:
                err_msg = data["error"].get("message", "") if isinstance(data["error"], dict) else str(data["error"])
                err_lower = err_msg.lower()
                if "insufficient" in err_lower or "balance" in err_lower:
                    logger.warning("INSUFFICIENT BALANCE")
                    with state.lock:
                        state.last_error = "Insufficient balance"
                        state._insufficient_balance = True
                    return None
                # Auto-bet UUID expired — clear it so next loop iteration will restart.
                if is_auto_game(state.game) and ("session" in err_lower or "expired" in err_lower or "not exists" in err_lower):
                    logger.warning("AUTO-BET SESSION EXPIRED: %s", err_msg[:120])
                    with state.lock:
                        state._auto_uuid = None
                        state.last_error = "Auto-bet session expired — restarting…"
                    return None
                with state.lock:
                    state.last_error = f"API error: {err_msg[:120]}"
                return None
            # Auto-bet: server may report a stop reason inline (max_profit/max_loss/etc.)
            if is_auto_game(state.game):
                stop_reason = data.get("stopReason") or data.get("stop_reason")
                if stop_reason:
                    reason_str = _fmt_auto_stop_reason(stop_reason)
                    logger.info("AUTO-BET STOP from server: %s", reason_str)
                    with state.lock:
                        state._auto_uuid = None
                        state.last_error = f"WolfBet auto-stop: {reason_str}"
                    # Let caller record this bet; loop's should_stop or next auto_start decides continuation.
            logger.debug("BET OK  id=%s state=%s",
                         data.get("bet", {}).get("id"),
                         data.get("bet", {}).get("state"))
            return data
        elif r.status_code == 429:
            # Respect Retry-After when provided (RFC 7231); else fall back to fixed probe.
            retry_after = r.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 2.0
            except (TypeError, ValueError):
                delay = 2.0
            logger.warning("RATE LIMIT 429 — backoff %.1fs (Retry-After=%s)", delay, retry_after)
            with state.lock:
                state.rate_wait    = True
                state.rate_backoff = max(2.0, delay)
                state.last_error   = f"Rate limit 429 — retrying in {delay:.0f}s…"
        elif r.status_code == 400 and is_auto_game(state.game):
            # UUID expired / invalid — clear it so next loop iteration restarts.
            body = r.text[:200]
            logger.warning("AUTO-BET 400 (likely UUID expired): %s", body)
            with state.lock:
                state._auto_uuid = None
                state.last_error = "Auto-bet session expired — restarting…"
        else:
            body = r.text[:500]
            logger.error("HTTP %d  response=%s  payload=%s",
                         r.status_code, body, body_str)
            with state.lock:
                state.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
    except requests.exceptions.Timeout:
        logger.warning("REQUEST TIMEOUT on bet #%d", state.total_bets + 1)
        with state.lock:
            state.last_error = "Request timeout — retrying…"
    except Exception as e:
        err_str = str(e).lower()
        if "insufficient" in err_str or "balance" in err_str:
            logger.warning("INSUFFICIENT BALANCE")
            with state.lock:
                state.last_error = "Insufficient balance"
                state._insufficient_balance = True
        else:
            logger.exception("UNEXPECTED ERROR placing bet: %s", e)
            with state.lock:
                state.last_error = str(e)[:120]
    return None

# ══════════════════════════════════════════════════════════
#  STRATEGY ENGINE
# ══════════════════════════════════════════════════════════
def compute_next_bet(last_result: str) -> float:
    """Compute next bet amount (delegates to core.engine)."""
    nxt, state.dalembert_unit, state.paroli_count = _core_compute_next_bet(
        state.strategy_key, state.base_bet, state.current_bet,
        state.loss_mult, state.win_mult, last_result,
        state.current_streak, state.delay_martin_threshold,
        state.dalembert_unit, state.paroli_count,
    )
    return nxt

def wolfrider_switch():
    """WolfRider post-bet: switch over/under (delegates to core.engine)."""
    new_rule, new_bv = _core_wolfrider_switch(state.total_bets, state.rule, state.multiplier)
    if new_rule is not None:
        state.rule = new_rule
        state.bet_value = new_bv
        logger.debug("WOLFRIDER SWITCH → %s  bet_value=%s  (bet #%d)", new_rule, new_bv, state.total_bets)

def should_stop() -> tuple:
    p = state.profit
    b = state.current_balance
    if state.max_profit is not None and p >= state.max_profit:
        return True, f"Profit target reached: {p:+.8f}"
    if state.max_loss is not None and p <= -abs(state.max_loss):
        return True, f"Max loss hit: {p:+.8f}"
    if state.max_bets is not None and state.total_bets >= state.max_bets:
        return True, f"Max bets reached: {state.total_bets}"
    if state.max_wins is not None and state.wins >= state.max_wins:
        return True, f"Max wins reached: {state.wins}"
    if state.stop_on_balance is not None and b <= state.stop_on_balance:
        return True, f"Balance floor hit: {b:.8f}"
    return False, ""

# ══════════════════════════════════════════════════════════
#  BETTING LOOP  (runs in background thread)
# ══════════════════════════════════════════════════════════
def betting_loop():
    last_result = "none"

    while state.running:
        loop_start = time.monotonic()
        # ── paused ──────────────────────────────────────
        if state.paused:
            state.status = "PAUSED — press [R] to resume"
            time.sleep(0.4)
            continue

        # ── rate-limit cooldown — wait then resume ──
        if state.rate_wait:
            backoff = state.rate_backoff
            state.status = f"Rate limit — resuming in {backoff:.0f}s…"
            time.sleep(backoff)
            with state.lock:
                state.rate_wait      = False
                state.rate_backoff   = 2.0
                state.rate_remaining = 50  # assume recovered; bet response will update
            logger.info("Rate limit cooldown done, resuming betting")
            continue

        # ── stop-condition check ─────────────────────────
        stop, reason = should_stop()
        if stop:
            if _should_recurring_restart(reason):
                logger.info("RECURRING RESTART after: %s", reason)
                # Best-effort: stop the old auto UUID before spinning up a new session.
                if is_auto_game(state.game):
                    api_auto_stop()
                _db_save_session()
                _reset_session_for_recurring()
                last_result = "none"
                continue
            state.running = False
            state.status  = f"STOPPED: {reason}"
            if is_auto_game(state.game):
                api_auto_stop()
            _db_save_session()
            break

        # ── streak delay (loss / win / every-N-bets) ──
        sd = _get_streak_delay()
        if sd > 0:
            state.status = f"Streak delay {sd:.2f}s (streak {state.current_streak})"
            time.sleep(sd)

        # ── periodic basebet_pct recompute (every 10 bets) ──
        if state.basebet_pct > 0 and state.total_bets > 0 and state.total_bets % 10 == 0:
            _apply_basebet_pct()

        # ── auto-bet: ensure we have a live UUID ──
        if is_auto_game(state.game) and not state._auto_uuid:
            state.status = "Starting auto-bet session…"
            if not api_auto_start():
                # api_auto_start set state.last_error; back off and retry
                with state.lock:
                    state.consecutive_errors += 1
                    state.backoff_delay = min(state.backoff_delay * 2, state.max_backoff)
                time.sleep(state.backoff_delay)
                continue
            with state.lock:
                state.consecutive_errors = 0
                state.backoff_delay = 1.0

        # ── bet pacing delay ───────────────────────────────
        if state.bet_delay > 0:
            time.sleep(state.bet_delay)

        # ── place bet ────────────────────────────────────
        state.status = f"Placing bet #{state.total_bets + 1}…"
        result = api_place_bet(state.current_bet)

        if result is None:
            if state._insufficient_balance:
                state.running = False
                state.status = "STOPPED: Insufficient balance"
                _db_save_session()
                break
            # exponential backoff on consecutive errors
            with state.lock:
                state.consecutive_errors += 1
                state.backoff_delay = min(
                    state.backoff_delay * 2,
                    state.max_backoff,
                )
            time.sleep(state.backoff_delay)
            continue

        bet_data     = result.get("bet", {})
        balance_data = result.get("userBalance", {})
        bet_state    = bet_data.get("state", "loss")
        result_value = float(bet_data.get("result_value", 0))
        amount_used  = float(bet_data.get("amount", state.current_bet))
        new_balance  = float(balance_data.get("amount", state.current_balance))

        # Use API's profit directly — it's positive on win, negative on loss
        raw_profit = float(bet_data.get("profit", 0))

        with state.lock:
            # reset backoff on success
            state.consecutive_errors = 0
            state.backoff_delay      = 1.0

            state.total_bets     += 1
            state.wagered        += amount_used
            state.profit         += raw_profit
            state.current_balance = new_balance

            if bet_state == "win":
                state.wins += 1
                if raw_profit > state.highest_win:
                    state.highest_win = raw_profit
                state.current_streak = max(state.current_streak, 0) + 1
                state.max_win_streak = max(state.max_win_streak, state.current_streak)
            else:
                state.losses += 1
                if amount_used > state.biggest_loss:
                    state.biggest_loss = amount_used
                state.current_streak = min(state.current_streak, 0) - 1
                state.max_loss_streak = max(state.max_loss_streak, abs(state.current_streak))

            if amount_used > state.highest_bet:
                state.highest_bet = amount_used
            if new_balance > state.highest_balance:
                state.highest_balance = new_balance
            if new_balance < state.lowest_balance:
                state.lowest_balance = new_balance

            # ── profit-based base bet increment ──
            if (state.profit_increment is not None and state.profit_threshold
                    and state.profit >= state.next_profit_milestone):
                state.base_bet += state.profit_increment
                state.next_profit_milestone += state.profit_threshold
                logger.info(
                    "PROFIT INCREMENT: base_bet → %.8f (next milestone at %.8f profit)",
                    state.base_bet, state.next_profit_milestone,
                )

            # profit graph snapshot (every 5 bets)
            if state.total_bets % 5 == 0:
                state.profit_history.append(state.profit)

            # recent bets log
            state.recent_bets.append({
                "n":     state.total_bets,
                "time":  datetime.now().isoformat(),
                "amt":   amount_used,
                "roll":  result_value,
                "state": bet_state,
                "pnl":   raw_profit,
                "bal":   new_balance,
            })

            # bets-per-second (session average) and bets-per-minute (actual)
            now = time.time()
            elapsed = now - state.session_start
            if elapsed > 0:
                state.bets_per_second = state.total_bets / elapsed
                state.bets_per_minute = state.bets_per_second * 60

            # track peak/low bets per individual second and minute
            sec_key = int(now)
            min_key = int(now) // 60
            if sec_key != state._current_sec:
                # new second — record the old second's count
                if state._current_sec > 0 and state._bets_this_sec > 0:
                    if state._bets_this_sec > state.peak_bps:
                        state.peak_bps = state._bets_this_sec
                    if state._bets_this_sec < state.low_bps:
                        state.low_bps = state._bets_this_sec
                state._current_sec = sec_key
                state._bets_this_sec = 1
            else:
                state._bets_this_sec += 1

            if min_key != state._current_min:
                # new minute — record the old minute's count
                if state._current_min > 0 and state._bets_this_min > 0:
                    if state._bets_this_min > state.peak_bpm:
                        state.peak_bpm = state._bets_this_min
                    if state._bets_this_min < state.low_bpm:
                        state.low_bpm = state._bets_this_min
                state._current_min = min_key
                state._bets_this_min = 1
            else:
                state._bets_this_min += 1

            sign  = "+" if raw_profit >= 0 else ""
            emoji = "W" if bet_state == "win" else "L"
            state.status = (
                f"{emoji} {'WIN' if bet_state=='win' else 'LOSS'} | "
                f"Roll: {result_value:.2f} | "
                f"Bet: {amount_used:.8f} | "
                f"P/L: {sign}{raw_profit:.8f} | "
                f"Bal: {new_balance:.8f}"
            )
            state.last_error = ""

        # persist to DB (non-blocking)
        _db_save_bet(bet_data, raw_profit, new_balance)

        # ── WolfRider: switch over/under after bet ──
        if state.strategy_key == "6":
            wolfrider_switch()

        # ── Rule-Based: apply custom rules after bet ──
        if state.strategy_key == "8" and state.custom_rules:
            apply_rules(bet_state)

        # ── milestone check (bets/wins/losses/profit) ──
        ms_reason = _check_milestone(bet_state)
        if ms_reason:
            logger.info("MILESTONE: %s (balance %.8f, profit %+.8f)",
                        ms_reason, state.current_balance, state.profit)
            with state.lock:
                state.status = f"Milestone: {ms_reason}"

        # calculate next bet — no cap, API rejects if insufficient balance
        raw_next  = compute_next_bet(bet_state)
        next_bet  = max(state.base_bet, raw_next)
        # ── streakbet_loss: reduce bet after N consecutive losses ──
        sb_factor = _get_streakbet_loss_factor()
        if sb_factor != 1.0:
            next_bet = max(state.base_bet, next_bet * sb_factor)
            logger.info("STREAKBET_LOSS: current_bet ×%.2f → %.8f (loss streak %d)",
                        sb_factor, next_bet, state.current_streak)
        state.current_bet = next_bet
        last_result = bet_state

        # ── cycle-time metric (EMA-smoothed, informs Efficiency + overhead) ──
        _record_cycle_ms((time.monotonic() - loop_start) * 1000)

# ══════════════════════════════════════════════════════════
#  DATABASE  I/O
# ══════════════════════════════════════════════════════════
def _db_conn():
    return db_connect(DB_PATH)


# ── Phase 5a — session read + export helpers ───────────────────────
def _read_session(session_id: int) -> dict:
    """Return the sessions row as a dict, or {} if not found."""
    conn = _db_conn()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def _read_bets(session_id: int) -> list:
    """Return all bets for a session (ordered by id) as a list of dicts."""
    conn = _db_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM bets WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _latest_session_id() -> int:
    """Return the highest session id in the DB, or 0 if none."""
    conn = _db_conn()
    try:
        row = conn.execute("SELECT MAX(id) FROM sessions").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] else 0


# ── Phase 5c — backtest / replay data sources ─────────────────────
def _simulate_bet_outcome(multiplier: float, rule: str, bet_amount: float,
                          current_balance: float, rng) -> dict:
    """Generate a synthetic bet result from a WolfBet-style RTP.

    99% RTP: win_chance% = 99 / multiplier. Roll uniformly in [0, 100).
    Returns the same {bet, userBalance} shape api_place_bet returns.
    """
    win_chance_pct = 99.0 / multiplier if multiplier > 0 else 50.0
    roll = rng.random() * 100.0
    if rule == "over":
        won = roll > (100.0 - win_chance_pct)
    else:  # "under"
        won = roll < win_chance_pct
    if won:
        profit = bet_amount * (multiplier - 1)
        state_val = "win"
    else:
        profit = -bet_amount
        state_val = "loss"
    new_bal = current_balance + profit
    return {
        "bet": {
            "state":        state_val,
            "result_value": roll,
            "amount":       bet_amount,
            "profit":       profit,
        },
        "userBalance": {"amount": new_bal},
    }


def _replay_bets_from_db(session_id: int, bet_amount_fn):
    """Yield bet-result dicts sourced from a historical session.

    Outcomes come from the historical rolls (win/loss + result_value); bet
    sizes come from the CURRENT strategy via `bet_amount_fn()`. Profit is
    recomputed from the current multiplier at the current bet size, so replay
    tests strategy behaviour against the same roll sequence.
    """
    saved_bets = _read_bets(session_id)
    if not saved_bets:
        raise ValueError(f"Session #{session_id} has no bets to replay")
    for hist in saved_bets:
        amount = bet_amount_fn()
        won = hist.get("state") == "win"
        multiplier = state.multiplier
        profit = amount * (multiplier - 1) if won else -amount
        yield {
            "bet": {
                "state":        hist.get("state", "loss"),
                "result_value": hist.get("result_value", 0.0),
                "amount":       amount,
                "profit":       profit,
            },
            "userBalance": {"amount": state.current_balance + profit},
        }


def _apply_backtest_result(result: dict) -> str:
    """Mutate state.* from a single backtest result. Returns the bet_state ('win'|'loss').

    Extracted so betting_loop and backtest_loop share the same state-update rules
    without needing an async/thread scheduler. Locking is preserved for parity.
    """
    bet_data     = result["bet"]
    balance_data = result["userBalance"]
    bet_state    = bet_data.get("state", "loss")
    amount_used  = float(bet_data.get("amount", 0.0))
    raw_profit   = float(bet_data.get("profit", 0.0))
    new_balance  = float(balance_data.get("amount", state.current_balance))

    with state.lock:
        state.total_bets  += 1
        state.wagered     += amount_used
        state.profit      += raw_profit
        state.current_balance = new_balance
        if bet_state == "win":
            state.wins += 1
            state.current_streak = max(state.current_streak, 0) + 1
            state.max_win_streak = max(state.max_win_streak, state.current_streak)
            if raw_profit > state.highest_win:
                state.highest_win = raw_profit
        else:
            state.losses += 1
            state.current_streak = min(state.current_streak, 0) - 1
            state.max_loss_streak = max(state.max_loss_streak, abs(state.current_streak))
            if amount_used > state.biggest_loss:
                state.biggest_loss = amount_used
        if amount_used > state.highest_bet:
            state.highest_bet = amount_used
        if new_balance > state.highest_balance:
            state.highest_balance = new_balance
        if new_balance < state.lowest_balance:
            state.lowest_balance = new_balance
    return bet_state


def backtest_loop(source, progress_every: int = 100):
    """Run the strategy over `source` (iterator of bet-result dicts) with no API calls.

    Honours should_stop() and state.running so both stop-condition config and the
    q hotkey work. Prints progress every `progress_every` bets so long simulations
    show heartbeat instead of feeling frozen.
    """
    for result in source:
        if not state.running:
            break
        stop, reason = should_stop()
        if stop:
            state.status = f"[BACKTEST] STOPPED: {reason}"
            break

        bet_state = _apply_backtest_result(result)

        # Strategy hooks — reuse from betting_loop path
        _check_milestone(bet_state)
        if state.strategy_key == "6":
            wolfrider_switch()
        if state.strategy_key == "8" and state.custom_rules:
            apply_rules(bet_state)

        # Next-bet computation with Phase 2 streakbet reduction
        raw_next = compute_next_bet(bet_state)
        next_bet = max(state.base_bet, raw_next)
        sb_factor = _get_streakbet_loss_factor()
        if sb_factor != 1.0:
            next_bet = max(state.base_bet, next_bet * sb_factor)
        state.current_bet = next_bet

        state.status = f"[BACKTEST] bet {state.total_bets}: {bet_state}  bal {state.current_balance:.8f}  p/l {state.profit:+.8f}"
        if progress_every > 0 and state.total_bets % progress_every == 0:
            console.print(f"  [dim][BACKTEST][/] {state.total_bets} bets  "
                          f"W:{state.wins} L:{state.losses}  "
                          f"P/L: [{'green' if state.profit >= 0 else 'red'}]"
                          f"{'+' if state.profit >= 0 else ''}{state.profit:.8f}[/]  "
                          f"bal: {state.current_balance:.8f}")


def _run_backtest(args) -> None:
    """Backtest entry point: load config → build source → run backtest_loop → summary.

    Config source is (in order): explicit --preset NAME, else the wizard's
    saved ~/.wolfbet_autobot.json, else sensible defaults. Never touches the
    DB except to read historical bets in replay mode. Prints progress + final
    summary via format_session_summary from v2.20.0.
    """
    console.clear()
    console.print("[bold cyan]🧪 BACKTEST MODE[/]  [dim]— no API calls, no real balance risk[/]\n")
    init_db()  # needed for _read_bets in replay; harmless otherwise

    # 1. Load config
    if args.preset:
        if not load_preset(args.preset):
            console.print(f"[red]Preset '{args.preset}' not found.[/]")
            sys.exit(1)
        console.print(f"  [dim]Loaded preset:[/] [cyan]{args.preset}[/]")
    elif load_config():
        console.print("  [dim]Loaded saved config from[/] [cyan]" + CONFIG_PATH + "[/]")
    else:
        console.print("  [dim]Using backtest defaults (Martingale, 2.01x, over 49.5).[/]")
        state.strategy       = "Martingale"
        state.strategy_key   = "2"
        state.multiplier     = 2.01
        state.rule           = "over"
        state.bet_value      = 49.5
        state.base_bet       = 0.0001
        state.loss_mult      = 2.0
        state.win_mult       = 1.0
        state.currency       = "usdt"
        state.game           = "dice"

    # 2. Initialise virtual session state (in-memory only)
    state.session_id       = -1
    state.session_start    = time.time()
    state.running          = True
    state.paused           = False
    state.total_bets       = 0
    state.wins             = 0
    state.losses           = 0
    state.profit           = 0.0
    state.wagered          = 0.0
    state.current_streak   = 0
    state.max_win_streak   = 0
    state.max_loss_streak  = 0
    state.highest_bet      = 0.0
    state.highest_win      = 0.0
    state.biggest_loss     = 0.0
    state.start_balance    = 1.0     # $1 sandbox default
    state.current_balance  = 1.0
    state.highest_balance  = 1.0
    state.lowest_balance   = 1.0
    state.current_bet      = state.base_bet
    if state.custom_rules_text:
        state.custom_rules = _load_rules_from_text(state.custom_rules_text)

    # 3. Build the outcome source
    if args.replay_session:
        try:
            source = _replay_bets_from_db(args.replay_session, lambda: state.current_bet)
            console.print(f"  [dim]Replaying session[/] [cyan]#{args.replay_session}[/]\n")
        except ValueError as e:
            console.print(f"[red]✗ {e}[/]")
            sys.exit(1)
    else:
        n = args.simulate or 1000
        rng = random.Random(args.seed) if args.seed is not None else random.Random()
        seed_str = f"seed={args.seed}" if args.seed is not None else "seed=system-entropy"
        console.print(f"  [dim]Simulating[/] [cyan]{n}[/] [dim]bets with[/] [cyan]{seed_str}[/]\n")

        def _gen():
            for _ in range(n):
                yield _simulate_bet_outcome(
                    state.multiplier, state.rule, state.current_bet,
                    state.current_balance, rng,
                )
        source = _gen()

    # 4. Run the loop (synchronous — fast enough for CLI, no thread noise)
    console.print(f"  [dim]Strategy:[/] [bold]{state.strategy}[/]  "
                  f"[dim]base_bet:[/] {state.base_bet:.8f}  "
                  f"[dim]multiplier:[/] {state.multiplier}x  "
                  f"[dim]rule:[/] {state.rule}\n")
    try:
        backtest_loop(source, progress_every=max(100, (args.simulate or 1000) // 20))
    except Exception as e:
        console.print(f"[red]✗ Backtest failed:[/] {e}")
        logger.exception("BACKTEST error")
        sys.exit(1)

    # 5. Summary
    console.print()
    console.print(format_session_summary(f"Backtest complete: {state.total_bets} bets"))
    console.print()


def _export_session(session_id: int, path: str) -> tuple:
    """Write a session + its bets to `path`. Format detected from extension.

    Returns (bet_count, format). Raises ValueError if the session is missing
    or if the file cannot be written.
    """
    path = os.path.expanduser(path)
    fmt = "json" if path.lower().endswith(".json") else "csv"
    session = _read_session(session_id)
    if not session:
        raise ValueError(f"Session #{session_id} not found")
    bets = _read_bets(session_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="") as f:
        if fmt == "json":
            json.dump({"session": session, "bets": bets}, f, indent=2, default=str)
        else:
            # CSV: session metadata as leading `#` comment rows, then bets table.
            for k, v in session.items():
                f.write(f"# {k}: {v}\n")
            if bets:
                w = csv.DictWriter(f, fieldnames=list(bets[0].keys()))
                w.writeheader()
                w.writerows(bets)
    return (len(bets), fmt)

def _db_start_session() -> int:
    conn = _db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions
            (started_at, currency, game, strategy, base_bet, multiplier, start_balance)
        VALUES (?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        state.currency, state.game, state.strategy,
        state.base_bet, state.multiplier, state.start_balance,
    ))
    conn.commit()
    sid = c.lastrowid
    conn.close()
    return sid

def _db_save_bet(bet_data: dict, profit: float, balance: float):
    try:
        conn = _db_conn()
        conn.execute("""
            INSERT INTO bets
                (session_id, timestamp, amount, multiplier, rule,
                 bet_value, result_value, state, profit, balance_after)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            state.session_id,
            datetime.now().isoformat(),
            float(bet_data.get("amount", 0)),
            float(bet_data.get("multiplier", 0)),
            state.rule,
            float(bet_data.get("bet_value", 0)),
            float(bet_data.get("result_value", 0)),
            bet_data.get("state", ""),
            profit,
            balance,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_bet failed: %s", e)

def _db_save_session():
    try:
        conn = _db_conn()
        lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
        conn.execute("""
            UPDATE sessions SET
                ended_at=?, total_bets=?, wins=?, losses=?,
                profit=?, wagered=?, end_balance=?,
                max_win_streak=?, max_loss_streak=?,
                highest_balance=?, lowest_balance=?,
                highest_win=?, biggest_loss=?,
                bets_per_minute=?, bets_per_second=?,
                peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?
            WHERE id=?
        """, (
            datetime.now().isoformat(),
            state.total_bets, state.wins, state.losses,
            state.profit, state.wagered, state.current_balance,
            state.max_win_streak, state.max_loss_streak,
            state.highest_balance, lo_bal,
            state.highest_win, state.biggest_loss,
            # Save session-average BPM for history
            min(state.bets_per_second * 60, state.rate_limit) if state.bets_per_second else state.bets_per_minute,
            state.bets_per_second,
            state.peak_bps,
            state.low_bps if state.low_bps != float("inf") else 0,
            state.peak_bpm,
            state.low_bpm if state.low_bpm != float("inf") else 0,
            state.session_id,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_session failed: %s", e)

def _db_get_history(limit: int = 12):
    conn = _db_conn()
    rows = conn.execute("""
        SELECT id, started_at, ended_at, currency, strategy,
               total_bets, profit, wagered, max_win_streak, max_loss_streak
        FROM sessions ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows

# ══════════════════════════════════════════════════════════
#  STATE FILE  (for --status / --daemon communication)
# ══════════════════════════════════════════════════════════
def _save_state_file():
    """Write live session state to JSON so --status can read it."""
    try:
        data = {
            "pid": os.getpid(),
            "session_id": state.session_id,
            "running": state.running,
            "paused": state.paused,
            "currency": state.currency,
            "strategy": state.strategy,
            "multiplier": state.multiplier,
            "base_bet": state.base_bet,
            "current_bet": state.current_bet,
            "total_bets": state.total_bets,
            "wins": state.wins,
            "losses": state.losses,
            "profit": state.profit,
            "wagered": state.wagered,
            "current_balance": state.current_balance,
            "start_balance": state.start_balance,
            "current_streak": state.current_streak,
            "max_win_streak": state.max_win_streak,
            "max_loss_streak": state.max_loss_streak,
            "highest_bet": state.highest_bet,
            "highest_win": state.highest_win,
            "biggest_loss": state.biggest_loss,
            "bets_per_minute": state.bets_per_minute,
            "bets_per_second": state.bets_per_second,
            "peak_bps": state.peak_bps,
            "low_bps": state.low_bps if state.low_bps != float("inf") else 0,
            "peak_bpm": state.peak_bpm,
            "low_bpm": state.low_bpm if state.low_bpm != float("inf") else 0,
            "highest_balance": state.highest_balance,
            "lowest_balance": state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance,
            "rate_remaining": state.rate_remaining,
            "rate_limit": state.rate_limit,
            # auto-bet state (dice-auto / limbo-auto)
            "auto_uuid": state._auto_uuid,
            "auto_start_remaining": state._auto_start_remaining,
            "game": state.game,
            # Phase 2 config (persisted for --status visibility + future --resume)
            "recurring": state.recurring,
            "streak_delay_loss": f"{state.streak_delay_loss[0]}:{state.streak_delay_loss[1]}" if state.streak_delay_loss[0] > 0 else None,
            "streak_delay_win":  f"{state.streak_delay_win[0]}:{state.streak_delay_win[1]}"  if state.streak_delay_win[0] > 0 else None,
            "streak_delay_bets": f"{state.streak_delay_bets[0]}:{state.streak_delay_bets[1]}" if state.streak_delay_bets[0] > 0 else None,
            "streakbet_loss":    f"{state.streakbet_loss[0]}:{state.streakbet_loss[1]}"      if state.streakbet_loss[0] > 0 else None,
            "basebet_pct":       state.basebet_pct or None,
            "milestone_bets":    state.milestone_bets,
            "milestone_wins":    state.milestone_wins or None,
            "milestone_losses":  state.milestone_losses or None,
            "milestone_profit":  state.milestone_profit or None,
            "last_profit_milestone": state._last_profit_milestone,
            # Phase 3 observability metrics
            "api_ms":              state._last_api_ms,
            "api_avg_ms":          _compute_api_avg(),
            "api_peak_ms":         state.peak_api_ms,
            "api_p50_ms":          _compute_api_p50(),
            "on_target_pct":       _compute_on_target_pct(),
            "cycle_ms":            state._cycle_ms,
            "overhead_ms":         state._cycle_overhead_ms,
            "efficiency_pct":      _compute_efficiency_pct(),
            "theoretical_max_bps": _compute_theoretical_max_bps(),
            "rule": state.rule,
            "bet_value": state.bet_value,
            "bet_delay": state.bet_delay,
            "loss_mult": state.loss_mult,
            "win_mult": state.win_mult,
            "strategy_key": state.strategy_key,
            # stop conditions
            "max_profit": state.max_profit,
            "max_loss": state.max_loss,
            "max_bets": state.max_bets,
            "max_wins": state.max_wins,
            "stop_on_balance": state.stop_on_balance,
            "profit_increment": state.profit_increment,
            "profit_threshold": state.profit_threshold,
            # rule-based info
            "rule_count": len(state.custom_rules) if state.strategy_key == "8" else 0,
            "rule_descriptions": [r.description for r in state.custom_rules] if state.strategy_key == "8" else [],
            "uptime_sec": int(time.time() - state.session_start),
            "status": state.status,
            "last_error": state.last_error,
            "updated_at": datetime.now().isoformat(),
        }
        fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _cleanup_state_file():
    for p in (STATE_PATH, PID_PATH):
        try:
            os.remove(p)
        except OSError:
            pass

# ══════════════════════════════════════════════════════════
#  TUI — BORDERLESS ULTRA-COMPACT DASHBOARD
# ══════════════════════════════════════════════════════════
SPARKLINE = "▁▂▃▄▅▆▇█"

def _sparkline(values) -> str:
    if not values or len(values) < 2:
        return "—"
    mn, mx = min(values), max(values)
    span   = mx - mn or 1
    return "".join(SPARKLINE[int((v - mn) / span * 7)] for v in values)

# ── ANSI color helpers (no Rich dependency for TUI) ─────
_RST  = "\033[0m"
_BOLD = "\033[1m"
_DIM  = "\033[2m"
_COLORS = {
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "white": "\033[37m",
    "bg_blue": "\033[44m",
    "bold_red": "\033[1;31m", "bold_green": "\033[1;32m",
    "bold_yellow": "\033[1;33m", "bold_cyan": "\033[1;36m",
    "bold_white": "\033[1;37m",
    "dim_blue": "\033[2;34m", "dim": "\033[2m",
}
_CLR_EOL = "\033[K"

def _a(color: str, text: str) -> str:
    """Apply ANSI color to text and reset."""
    code = _COLORS.get(color, "")
    return f"{code}{text}{_RST}" if code else text

def _fmt_speed() -> str:
    """Format peak/low BPS and BPM as a compact range string."""
    pbps = int(state.peak_bps) if state.peak_bps > 0 else 0
    lbps = int(state.low_bps) if state.low_bps != float("inf") else 0
    pbpm = int(state.peak_bpm) if state.peak_bpm > 0 else 0
    lbpm = int(state.low_bpm) if state.low_bpm != float("inf") else 0
    return f"{lbps}-{pbps}/s {lbpm}-{pbpm}/m"

def build_dashboard_screen() -> str:
    """Build full-screen dashboard using direct ANSI codes.

    Layout (always exactly term_h lines):
      Row 1:       Header bar
      Row 2:       Balance / PnL / Wagered / WR
      Row 3:       Bets / Wins / Losses / Streaks
      Row 4:       Bet / Strategy / Multiplier / BPM / Rate limit
      Row 5:       Peak / Low bal / Best win / Worst loss / Avg P/L
      Row 6:       Sparkline (P/L graph)
      Row 7:       Separator ───
      Row 8:       Bets table header
      Row 9..N-2:  Recent bets (flex — fills remaining, padded with blanks)
      Row N-1:     Separator ───
      Row N:       Status + controls
    """
    try:
        term_size = os.get_terminal_size()
        term_h = term_size.lines
        term_w = term_size.columns
    except OSError:
        term_h = 24
        term_w = 80

    lines = []  # list of raw ANSI strings, one per terminal row

    def add(s: str):
        lines.append(f"{s}{_CLR_EOL}")

    def add_blank():
        lines.append(_CLR_EOL)

    # ── Row 1: Header ─────────────────────────────────
    uptime = str(timedelta(seconds=int(time.time() - state.session_start)))
    if state.running and not state.paused:
        st_ind = _a("bold_green", "● LIVE")
    elif state.paused:
        st_ind = _a("bold_yellow", "● PAUSED")
    else:
        st_ind = _a("bold_red", "● STOPPED")

    hdr_text = f"WolfBet v{VERSION}  #{state.session_id}  {uptime}  "
    # visible chars used: 4 + len(hdr_text) + len("● STATUS") + 1
    hdr_vis = 4 + len(hdr_text) + len("● STOPPED") + 1
    fill = max(0, term_w - hdr_vis)
    add(f"{_a('dim_blue', '─── ')}{_a('bold_cyan', f'WolfBet v{VERSION}')}"
        f"{_a('white', f'  #{state.session_id}  {uptime}')}  {st_ind} "
        f"{_a('dim_blue', '─' * fill)}")

    # ── Row 2: Balance bar ────────────────────────────
    p = state.profit
    ps = "+" if p >= 0 else ""
    pc = "bold_green" if p >= 0 else "bold_red"
    wr = (state.wins / state.total_bets * 100) if state.total_bets else 0
    wr_c = "bold_green" if wr >= 50 else "bold_red"

    add(f"{_COLORS['bg_blue']}{_BOLD}{_COLORS['white']} BAL {_RST} "
        f"{_a('bold_white', f'{state.current_balance:.8f} {state.currency.upper()}')}"
        f"  {_a('dim', 'PnL')} {_a(pc, f'{ps}{p:.8f}')}"
        f"  {_a('dim', 'WAG')} {_a('yellow', f'{state.wagered:.8f}')}"
        f"  {_a('dim', 'WR')} {_a(wr_c, f'{wr:.1f}%')}")

    # ── Row 3: Stats line 1 ──────────────────────────
    cs = state.current_streak
    if cs > 0:
        sk = _a("green", f"W+{cs}")
    elif cs < 0:
        sk = _a("red", f"L{cs}")
    else:
        sk = _a("dim", "—")

    add(f" {_a('dim', 'Bets')} {_a('bold_white', str(state.total_bets))}"
        f"  {_a('green', 'W')} {_a('bold_green', str(state.wins))}"
        f"  {_a('red', 'L')} {_a('bold_red', str(state.losses))}"
        f"  {_a('dim', 'Str')} {sk}"
        f"  {_a('dim', f'Best W+{state.max_win_streak}/L-{state.max_loss_streak}')}")

    # ── Row 4: Bet / Strategy / Rate ──────────────────
    rl = state.rate_remaining
    rl_c = "green" if rl > 100 else ("yellow" if rl > 20 else "red")

    s4 = (f" {_a('dim', 'Bet')} {_a('yellow', f'{state.current_bet:.8f}')}")
    if state.profit_increment is not None and state.profit_threshold:
        s4 += f"  {_a('dim', 'Base')} {_a('bold_green', f'{state.base_bet:.8f}')}"
    s4 += (f"  {_a('dim', 'Hi')} {_a('white', f'{state.highest_bet:.8f}')}"
           f"  {_a('cyan', state.strategy)}"
           f"  {_a('dim', f'{state.multiplier}x')}"
           f"  {_a('dim', state.rule)}"
           f"  {_a('dim', 'BPS')} {_a('magenta', f'{state.bets_per_second:.1f}')}"
           f"  {_a('dim', 'BPM')} {_a('magenta', f'{state.bets_per_minute:.0f}')}"
           f"  {_a('dim', 'RL')} {_a(rl_c, f'{rl}/{state.rate_limit}')}"
           f"  {_a('dim', 'Speed')} {_a('magenta', _fmt_speed())}")
    add(s4)

    # ── Row 5: Extremes / Performance ────────────────
    hi_bal = state.highest_balance if state.highest_balance > 0 else state.current_balance
    lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
    avg_pnl = state.profit / state.total_bets if state.total_bets else 0.0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    bal_change = state.current_balance - state.start_balance
    bc_c = "bold_green" if bal_change >= 0 else "bold_red"
    bc_s = "+" if bal_change >= 0 else ""

    s5 = (f" {_a('dim', 'Peak')} {_a('green', f'{hi_bal:.8f}')}"
          f"  {_a('dim', 'Low')} {_a('red', f'{lo_bal:.8f}')}"
          f"  {_a('dim', 'BestW')} {_a('green', f'+{state.highest_win:.8f}')}"
          f"  {_a('dim', 'WorstL')} {_a('red', f'-{state.biggest_loss:.8f}')}"
          f"  {_a('dim', 'Avg')} {_a(avg_c, f'{avg_s}{avg_pnl:.8f}')}"
          f"  {_a('dim', 'Bal')} {_a(bc_c, f'{bc_s}{bal_change:.8f}')}")
    add(s5)

    # ── Row 6: API timing + rate limit + (delay-gated) API stable + Efficiency + Cycle ──
    api_avg = _compute_api_avg()
    api_p50 = _compute_api_p50()
    add(f" {_a('dim', 'API')} last {_a('cyan', f'{state._last_api_ms:.0f}ms')}"
        f"  avg {_a('cyan', f'{api_avg:.0f}ms')}"
        f"  peak {_a('cyan', f'{state.peak_api_ms:.0f}ms')}")

    rl_line = f" {_a('dim', 'Rate limit')} {_a('yellow', f'{state.rate_remaining}/{state.rate_limit}')} play"
    if state._auto_start_remaining is not None:
        rl_line += f"  · {_a('yellow', f'{state._auto_start_remaining}/20')} start"
    add(rl_line)

    if state.bet_delay > 0:
        ot = _compute_on_target_pct()
        ot_str = f"{ot:.0f}%" if ot is not None else "n/a"
        add(f" {_a('dim', 'API stable')} {_a('cyan', f'{api_p50:.0f}ms')} median"
            f"  · {_a('cyan', ot_str)} on-target  (API ≤ bet delay)")
        tmax = _compute_theoretical_max_bps()
        eff = _compute_efficiency_pct()
        eff_c = 'green' if eff >= 90 else 'yellow'
        add(f" {_a('dim', 'Efficiency')} {_a(eff_c, f'{eff:.0f}%')}"
            f"  (max {_a('cyan', f'{tmax:.2f}')} bps @ delay={state.bet_delay:.3f}s)")

    cycle_ms = state._cycle_ms
    bps_from_cycle = (1000.0 / cycle_ms) if cycle_ms > 0 else 0.0
    add(f" {_a('dim', 'Cycle')} {_a('magenta', f'{cycle_ms:.0f}ms')}/bet"
        f"  (overhead {_a('cyan', f'{state._cycle_overhead_ms:.0f}ms')})"
        f"  {_a('dim', '→')} {_a('magenta', f'{bps_from_cycle:.2f}')} bps")

    # ── Row 6: Sparkline ─────────────────────────────
    spark = _sparkline(list(state.profit_history))
    sp_c = "green" if p >= 0 else "red"
    add(f" {_a('dim', 'P/L')} {_a(sp_c, spark)}")

    # ── Row 7: Separator ──────────────────────────────
    add(_a("dim_blue", "─" * term_w))

    # ── Row 8: Bets table header ──────────────────────
    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Amount':>14}  {'Roll':>7}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    # ── Rows 9..N-2: Recent bet rows (flex area) ─────
    # top(8) + bottom(2) = 10 pinned lines
    bets_avail = max(0, term_h - 10)
    recent = list(state.recent_bets)[-bets_avail:] if bets_avail > 0 else []

    for b in reversed(recent):
        bwl_c = "bold_green" if b["state"] == "win" else "bold_red"
        bwl_t = " W " if b["state"] == "win" else " L "
        bpnl_c = "green" if b["pnl"] >= 0 else "red"
        bsign = "+" if b["pnl"] >= 0 else ""
        bn = b["n"]
        btm = b["time"]
        bamt = b["amt"]
        broll = b["roll"]
        bpnl = b["pnl"]
        bbal = b["bal"]
        add(f"{_a('dim', f'{bn:>6}')}"
            f"  {_a('dim', f'{btm:<9}')}"
            f" {_a('white', f'{bamt:>14.8f}')}"
            f"  {_a('white', f'{broll:>7.2f}')}"
            f"  {_a(bwl_c, bwl_t)}"
            f"  {_a(bpnl_c, f'{bsign}{bpnl:>14.8f}')}"
            f"  {_a('white', f'{bbal:>14.8f}')}")

    # Pad empty lines
    for _ in range(bets_avail - len(recent)):
        add_blank()

    # ── Row N-1: Bottom separator ─────────────────────
    add(_a("dim_blue", "─" * term_w))

    # ── Row N: Status + controls ──────────────────────
    status_txt = state.status[:60]
    keys = "[P]ause [R]esume [T]weak [E]xport [H]istory [Q]uit"
    add(f" {_a('white', status_txt)}  {_a('dim', keys)}")

    return "\r\n".join(lines)

# ══════════════════════════════════════════════════════════
#  HISTORY SCREEN
# ══════════════════════════════════════════════════════════
def show_history():
    was_paused  = state.paused
    state.paused = True
    time.sleep(0.15)

    rows = _db_get_history(15)
    console.clear()
    console.print(Rule("[bold cyan]Session History[/]"))

    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("ID",       width=5,  justify="center")
    t.add_column("Started",  width=17)
    t.add_column("Ended",    width=17)
    t.add_column("Cur",      width=6,  justify="center")
    t.add_column("Strategy", width=18)
    t.add_column("Bets",     width=7,  justify="right")
    t.add_column("Profit",   width=16, justify="right")
    t.add_column("Wagered",  width=16, justify="right")
    t.add_column("W/L Str",  width=10, justify="center")

    for row in rows:
        sid, started, ended, cur, strat, bets, profit, wagered, mws, mls = row
        pc  = "green" if (profit or 0) >= 0 else "red"
        ps  = "+" if (profit or 0) >= 0 else ""
        t.add_row(
            str(sid),
            (started or "").replace("T", " "),
            (ended   or "—").replace("T", " "),
            (cur or "").upper(),
            strat or "—",
            str(bets or 0),
            Text(f"{ps}{profit:.8f}" if profit else "0.00000000", style=pc),
            f"{wagered:.8f}" if wagered else "—",
            f"W{mws or 0}/L{mls or 0}",
        )

    console.print(t)
    console.print("\n[dim]  Press any key to return…[/]")
    _getch()
    state.paused = was_paused


def _print_tweak_help():
    """Print a compact reference of tweakable params + example values."""
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("Param",   width=20)
    t.add_column("Example", width=28)
    t.add_column("Notes",   width=32)
    rows = [
        ("delay <sec>",         "delay 0.1",         "bet_delay"),
        ("bbpct <0-1>",         "bbpct 0.001",       "basebet_pct (0 disables)"),
        ("sdloss / sdwin",      "sdloss 3:0.5",      "'N:sec' streak delay"),
        ("sdbets / sbloss",     "sdbets 100:0.5",    "streak-bet reduction"),
        ("maxprofit",           "maxprofit 0.01",    "'off' to disable"),
        ("maxloss / maxbets",   "maxbets 5000",      "instant stop condition"),
        ("maxwins / minbalance","maxwins 1000",      "instant stop condition"),
        ("msbets / mswins",     "msbets 100",        "milestone thresholds"),
        ("mslosses / msprofit", "msprofit 0.001",    "milestone thresholds"),
        ("pi / pt",             "pt 0.001",          "profit increment"),
        ("recurring on/off",    "recurring on",      "restart on non-loss stop"),
        ("basebet <amount>",    "basebet 0.00000002","auto games: UUID clears"),
        ("multiplier",          "multiplier 3.0",    "auto games: UUID clears"),
        ("lossmult / winmult",  "lossmult 2.01",     "auto games: UUID clears"),
    ]
    for p, ex, note in rows:
        t.add_row(f"[cyan]{p}[/]", f"[yellow]{ex}[/]", f"[dim]{note}[/]")
    console.print(t)


def format_session_summary(reason: str = "") -> Group:
    """Build a Rich Group with the end-of-session recap panels.

    Mirrors tg/formatter.format_stop content shape but rendered as terminal-native
    Rich (Panel/Table.grid) rather than Markdown. Green/red border on the balance
    panel from the profit sign — matches the ✅/🛑 emoji policy from v2.16.9.
    """
    p = state.profit
    ps = "+" if p >= 0 else ""
    pe = "✅" if p >= 0 else "🛑"
    bal_change = state.current_balance - state.start_balance
    bc_s = "+" if bal_change >= 0 else ""
    avg_pnl = p / state.total_bets if state.total_bets > 0 else 0.0
    avg_s = "+" if avg_pnl >= 0 else ""
    wr = (state.wins / state.total_bets * 100) if state.total_bets > 0 else 0.0

    header = Rule(f"[bold]{pe} Session #{state.session_id} ended[/]  [dim]— {reason or 'Session ended'}[/]")

    bal_c  = "green" if p >= 0 else "red"
    bc_c   = "green" if bal_change >= 0 else "red"
    avg_c  = "green" if avg_pnl >= 0 else "red"

    bal = Table.grid(padding=(0, 2))
    bal.add_row("Start balance:", f"{state.start_balance:.8f} {state.currency.upper()}")
    bal.add_row("End balance:",   f"{state.current_balance:.8f}  ([{bc_c}]{bc_s}{bal_change:.8f}[/])")
    bal.add_row("P/L:",           f"[{bal_c}]{ps}{p:.8f}[/]")
    bal.add_row("Wagered:",       f"{state.wagered:.8f}")

    stats = Table.grid(padding=(0, 2))
    stats.add_row("Bets:",     f"{state.total_bets:,}   W: {state.wins:,}   L: {state.losses:,}")
    stats.add_row("Win rate:", f"{wr:.1f}%")
    stats.add_row("Avg P/L:",  f"[{avg_c}]{avg_s}{avg_pnl:.8f}[/]")

    low_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
    extremes = Table.grid(padding=(0, 2))
    extremes.add_row("Peak balance:", f"{state.highest_balance:.8f}")
    extremes.add_row("Low balance:",  f"{low_bal:.8f}")
    extremes.add_row("Best win:",     f"+{state.highest_win:.8f}")
    extremes.add_row("Worst loss:",   f"-{state.biggest_loss:.8f}")
    extremes.add_row("Max streaks:",  f"W+{state.max_win_streak}  /  L-{state.max_loss_streak}")

    cfg = Table.grid(padding=(0, 2))
    cfg.add_row("Game:",     GAME_LABELS.get(state.game, state.game))
    cfg.add_row("Strategy:", f"{state.strategy}  ({state.multiplier}x, {state.rule})")
    cfg.add_row("Base bet:", f"{state.base_bet:.8f}")
    if state.bet_delay > 0:
        cfg.add_row("Bet delay:", f"{state.bet_delay:.3f}s")
    if state.strategy_key == "8" and state.custom_rules:
        cfg.add_row("Rules:", f"{len(state.custom_rules)} custom")
    if state.recurring:
        cfg.add_row("Recurring:", "on")

    uptime_s = int(time.time() - state.session_start) if state.session_start else 0
    h, rem = divmod(uptime_s, 3600)
    m, s = divmod(rem, 60)
    footer = Text.from_markup(
        f"[dim]Uptime[/] [bold]{h}h {m}m {s}s[/]   "
        f"[dim]Avg speed[/] [bold]{state.bets_per_second:.1f} bps / {state.bets_per_minute:.0f} bpm[/]"
    )

    return Group(
        header,
        Panel(bal,      title="[bold]💰 Balance[/]",  border_style=bal_c),
        Panel(stats,    title="[bold]📊 Stats[/]",    border_style="cyan"),
        Panel(extremes, title="[bold]📈 Extremes[/]", border_style="cyan"),
        Panel(cfg,      title="[bold]⚙️  Config[/]",  border_style="dim"),
        footer,
    )


def show_export():
    """Modal export prompt: mirrors show_tweak idiom."""
    was_paused = state.paused
    state.paused = True
    time.sleep(0.15)
    console.clear()
    console.print(Rule("[bold cyan]Export session[/]"))
    default_path = os.path.expanduser(f"~/wolfbet-session-{state.session_id}.csv")
    console.print(f"  [dim]Default[/] [cyan]{default_path}[/]")
    console.print("  [dim]Blank or 'back' cancels.  '.json' switches format.[/]\n")
    try:
        raw = input("  Path: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        state.paused = was_paused
        return
    if raw.lower() in ("back", "cancel", "q", "quit"):
        state.paused = was_paused
        return
    path = raw or default_path
    try:
        n, fmt = _export_session(state.session_id, path)
        console.print(f"  [green]✓[/] Exported [bold]{n}[/] bets to [cyan]{path}[/] ({fmt.upper()})")
        logger.info("EXPORT: session=%d format=%s bets=%d path=%s", state.session_id, fmt, n, path)
    except Exception as e:
        console.print(f"  [red]✗ {e}[/]")
        logger.warning("EXPORT FAILED: session=%d path=%s: %s", state.session_id, path, e)
    console.print("\n[dim]  Press any key to return…[/]")
    _getch()
    console.clear()
    state.paused = was_paused


def show_tweak():
    """Modal runtime tweak surface — parity with tg/handlers.cmd_tweak.

    Enters paused state, prompts for `param value`, applies via _apply_mutation,
    resumes. 'back' / empty / q cancels."""
    was_paused = state.paused
    state.paused = True
    time.sleep(0.15)

    console.clear()
    console.print(Rule("[bold cyan]Runtime tweak[/]"))
    _print_tweak_help()

    while True:
        console.print("\n[dim]Format: [/][cyan]param value[/]  [dim]|  'back' to cancel[/]")
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw or raw.lower() in ("back", "q", "quit"):
            break
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            console.print("  [red]Missing value.[/] Try: [cyan]delay 0.1[/]")
            continue
        param, value = parts[0].lower(), parts[1]
        param = _TWEAK_ALIAS.get(param, param)
        if param not in _TWEAK_PARAMS:
            console.print(f"  [red]Unknown param:[/] [cyan]{param}[/]")
            continue
        try:
            msgs = _apply_mutation({param: value})
        except (ValueError, TypeError) as e:
            console.print(f"  [red]Invalid value:[/] {e}")
            continue
        for m in msgs:
            console.print(f"  [green]✓[/] {m}")
            logger.info("TWEAK: %s", m)
        time.sleep(2.0)
        break

    console.clear()
    state.paused = was_paused

# ══════════════════════════════════════════════════════════
#  KEYBOARD INPUT  (runs in background thread)
# ══════════════════════════════════════════════════════════
def _getch() -> str:
    try:
        import termios, tty
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return ""

def input_handler():
    while state.running or state.paused:
        ch = _getch().lower()
        if not ch:
            time.sleep(0.1)
            continue
        if ch in ("q", "\x03", "\x1c"):  # Q, Ctrl+C, Ctrl+backslash
            state.running  = False
            state.paused   = False
            state.status   = "Stopping — saving session…"
            logger.warning("SESSION END (user quit)  bets=%d  profit=%+.8f  balance=%.8f",
                        state.total_bets, state.profit, state.current_balance)
            _db_save_session()
            # Phase 5a — session summary before exit
            try:
                console.clear()
                console.print(format_session_summary("Stopped by user"))
                console.print("\n[dim]  Press any key to close…[/]")
                _getch()
            except Exception as e:
                logger.warning("Could not render session summary: %s", e)
        elif ch == "p":
            state.paused = True
            state.status = "PAUSED"
        elif ch == "r":
            state.paused = False
            state.status = "Resumed"
        elif ch == "h":
            show_history()
        elif ch == "t":
            show_tweak()
        elif ch == "e":
            show_export()

# ══════════════════════════════════════════════════════════
#  SETUP WIZARD  (step-based, supports "back" and "none")
# ══════════════════════════════════════════════════════════
_BACK = object()   # sentinel

def _ask(prompt_text: str, default: str = "", choices: list = None) -> str:
    """input()-based prompt with Rich-colored label.
    Arrow keys / editing work normally.
    Type 'back' to go to the previous step."""
    hint = f" [bold yellow]{default}[/]" if default else ""
    console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
    while True:
        try:
            raw = input(": ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if raw.lower() == "back":
            return _BACK
        if not raw:
            if default:
                return default
            if choices:
                console.print(f"    [red]Choose from:[/] {', '.join(choices)}")
                console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
                continue
            return default
        if choices and raw.lower() not in [c.lower() for c in choices]:
            console.print(f"    [red]Choose from:[/] {', '.join(choices)}")
            console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
            continue
        return raw

def _ask_optional(prompt_text: str, current) -> str:
    """Like _ask but shows current value and accepts 'none'/'clear' to wipe it."""
    if current is not None:
        hint = f" [bold yellow]{current}[/]  [dim](type 'none' to clear)[/]"
    else:
        hint = " [dim]none[/]"
    console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
    try:
        raw = input(": ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if raw.lower() == "back":
        return _BACK
    if raw.lower() in ("none", "clear", "off", "disable", "0"):
        return ""
    if not raw:
        return str(current) if current is not None else ""
    return raw


def _build_one_rule():
    """Interactive builder for a single rule. Returns StrategyRule, _BACK, or None (done)."""
    console.print("  [bold cyan]── New Rule ──[/]")
    console.print("    [dim]Condition type:[/]")
    for k, name in COND_TYPES.items():
        label = {"sequence": "Sequence (streaks/counts)", "profit": "Profit / Loss / Balance",
                 "bet": "Bet properties"}.get(name, name)
        console.print(f"      [bold yellow][{k}][/] {label}")

    ct = _ask("  Condition type", choices=list(COND_TYPES.keys()))
    if ct is _BACK:
        return _BACK
    cond_type = COND_TYPES[ct]

    cond_field = ""
    cond_mode = ""
    cond_value = 0.0
    cond_trigger = ""

    if cond_type == "sequence":
        # Ask for mode
        console.print("    [dim]When should this fire?[/]")
        for k, (_, label) in SEQ_MODES.items():
            console.print(f"      [bold yellow][{k}][/] {label}")
        sm = _ask("  Sequence mode", choices=list(SEQ_MODES.keys()))
        if sm is _BACK:
            return _BACK
        cond_mode = SEQ_MODES[sm][0]

        # Ask for value N
        nv = _ask("  N value (number)", default="1")
        if nv is _BACK:
            return _BACK
        try:
            cond_value = float(nv)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

        # Ask for trigger (win/loss/bet)
        console.print("    [dim]On which event?[/]")
        for k, t in SEQ_TRIGGERS.items():
            console.print(f"      [bold yellow][{k}][/] {t}")
        st = _ask("  Event", choices=list(SEQ_TRIGGERS.keys()))
        if st is _BACK:
            return _BACK
        cond_trigger = SEQ_TRIGGERS[st]

    elif cond_type == "profit":
        # Ask for field
        console.print("    [dim]Check what?[/]")
        for k, name in PROFIT_FIELDS.items():
            console.print(f"      [bold yellow][{k}][/] {name}")
        pf = _ask("  Field", choices=list(PROFIT_FIELDS.keys()))
        if pf is _BACK:
            return _BACK
        cond_field = PROFIT_FIELDS[pf]

        # Ask for operator
        console.print("    [dim]Comparison:[/]")
        for k, (_, sym) in CMP_OPS.items():
            console.print(f"      [bold yellow][{k}][/] {sym}")
        op = _ask("  Operator", choices=list(CMP_OPS.keys()))
        if op is _BACK:
            return _BACK
        cond_mode = CMP_OPS[op][0]

        # Ask for value
        vl = _ask(f"  Value ({state.currency.upper()})", default="0")
        if vl is _BACK:
            return _BACK
        try:
            cond_value = float(vl)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    elif cond_type == "bet":
        # Ask for field
        console.print("    [dim]Check what?[/]")
        for k, name in BET_FIELDS.items():
            console.print(f"      [bold yellow][{k}][/] {name}")
        bf = _ask("  Field", choices=list(BET_FIELDS.keys()))
        if bf is _BACK:
            return _BACK
        cond_field = BET_FIELDS[bf]

        # Ask for operator
        console.print("    [dim]Comparison:[/]")
        for k, (_, sym) in CMP_OPS.items():
            console.print(f"      [bold yellow][{k}][/] {sym}")
        op = _ask("  Operator", choices=list(CMP_OPS.keys()))
        if op is _BACK:
            return _BACK
        cond_mode = CMP_OPS[op][0]

        # Ask for value
        unit = "%" if cond_field == "winchance" else ("x" if cond_field == "payout" else "")
        vl = _ask(f"  Value{(' (' + unit + ')') if unit else ''}", default="0")
        if vl is _BACK:
            return _BACK
        try:
            cond_value = float(vl)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    # ── Ask for action ──
    console.print("    [dim]Then do what?[/]")
    for k, (_, label) in RULE_ACTIONS.items():
        console.print(f"      [bold yellow][{k:>2}][/] {label}")
    ak = _ask("  Action", choices=list(RULE_ACTIONS.keys()))
    if ak is _BACK:
        return _BACK
    action = RULE_ACTIONS[ak][0]

    # Ask for action value if needed
    action_value = 0.0
    needs_value = action in ("increase_amount", "decrease_amount", "add_amount",
                             "deduct_amount", "set_amount", "set_winchance",
                             "increase_wc", "decrease_wc")
    if needs_value:
        if action in ("increase_amount", "decrease_amount", "increase_wc", "decrease_wc"):
            unit_hint = "%"
        elif action == "set_winchance":
            unit_hint = "% win chance"
        else:
            unit_hint = state.currency.upper()
        av = _ask(f"  Value ({unit_hint})", default="0")
        if av is _BACK:
            return _BACK
        try:
            action_value = float(av)
        except ValueError:
            console.print("    [red]Invalid number[/]")
            return None

    rule = StrategyRule(
        cond_type=cond_type, cond_field=cond_field, cond_mode=cond_mode,
        cond_value=cond_value, cond_trigger=cond_trigger,
        action=action, action_value=action_value,
    )
    rule.description = _describe_rule(rule)
    return rule


def _edit_one_rule(old: StrategyRule):
    """Edit an existing rule part-by-part. Returns updated StrategyRule, _BACK, or None."""
    # Work on a mutable copy
    r = StrategyRule.from_dict(old.to_dict())
    act_label = next((v[1] for v in RULE_ACTIONS.values() if v[0] == r.action), r.action)

    while True:
        # Show current rule parts as a numbered menu
        console.print()
        console.print(f"    [bold]{r.description}[/]")
        console.print()

        parts = []
        if r.cond_type == "sequence":
            mode_label = dict(every="Every N", every_streak="Every streak of N",
                              first_streak="First streak of N",
                              streak_above="Streak above N", streak_below="Streak below N").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Mode",       mode_label))
            parts.append(("N value",    str(int(r.cond_value))))
            parts.append(("Event",      r.cond_trigger))
        elif r.cond_type == "profit":
            op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Field",      r.cond_field))
            parts.append(("Operator",   op_sym))
            parts.append(("Value",      str(r.cond_value)))
        elif r.cond_type == "bet":
            op_sym = dict(gte=">=", gt=">", lte="<=", lt="<").get(r.cond_mode, r.cond_mode)
            parts.append(("Condition",  r.cond_type.title()))
            parts.append(("Field",      r.cond_field))
            parts.append(("Operator",   op_sym))
            parts.append(("Value",      str(r.cond_value)))

        act_label = next((v[1] for v in RULE_ACTIONS.values() if v[0] == r.action), r.action)
        parts.append(("Action",     act_label))
        if r.action_value:
            parts.append(("Action value", str(r.action_value)))

        for i, (label, val) in enumerate(parts, 1):
            console.print(f"      [bold yellow][{i}][/] {label:14s} [cyan]{val}[/]")
        save_n = len(parts) + 1
        replace_n = save_n + 1
        console.print(f"      [bold yellow][{save_n}][/] [green]Save changes[/]")
        console.print(f"      [bold yellow][{replace_n}][/] [yellow]Rebuild from scratch[/]")

        valid = [str(i) for i in range(1, replace_n + 1)]
        pick = _ask("    Edit which part?", default=str(save_n), choices=valid)
        if pick is _BACK:
            return _BACK
        pick = int(pick)

        if pick == save_n:
            r.description = _describe_rule(r)
            return r

        if pick == replace_n:
            console.print("    [dim]Building replacement rule…[/]")
            replacement = _build_one_rule()
            if replacement is _BACK:
                continue
            return replacement

        label = parts[pick - 1][0]

        # ── Edit the selected part ──
        if label == "Condition":
            console.print("      [dim]Change condition type:[/]")
            for k, name in COND_TYPES.items():
                tag = {"sequence": "Sequence", "profit": "Profit / Loss / Balance",
                       "bet": "Bet properties"}.get(name, name)
                console.print(f"        [bold yellow][{k}][/] {tag}")
            cur_key = next((k for k, v in COND_TYPES.items() if v == r.cond_type), "1")
            ct = _ask("      Type", default=cur_key, choices=list(COND_TYPES.keys()))
            if ct is _BACK:
                continue
            new_type = COND_TYPES[ct]
            if new_type != r.cond_type:
                # Type changed — reset related fields to defaults
                r.cond_type = new_type
                if new_type == "sequence":
                    r.cond_field = ""
                    r.cond_mode = "every"
                    r.cond_value = 1.0
                    r.cond_trigger = "win"
                elif new_type == "profit":
                    r.cond_field = "profit"
                    r.cond_mode = "gte"
                    r.cond_value = 0.0
                    r.cond_trigger = ""
                elif new_type == "bet":
                    r.cond_field = "amount"
                    r.cond_mode = "gte"
                    r.cond_value = 0.0
                    r.cond_trigger = ""

        elif label == "Mode":
            console.print("      [dim]Sequence mode:[/]")
            for k, (_, ml) in SEQ_MODES.items():
                console.print(f"        [bold yellow][{k}][/] {ml}")
            cur_key = next((k for k, v in SEQ_MODES.items() if v[0] == r.cond_mode), "1")
            sm = _ask("      Mode", default=cur_key, choices=list(SEQ_MODES.keys()))
            if sm is _BACK:
                continue
            r.cond_mode = SEQ_MODES[sm][0]

        elif label == "N value":
            nv = _ask("      N value", default=str(int(r.cond_value)))
            if nv is _BACK:
                continue
            try:
                r.cond_value = float(nv)
            except ValueError:
                console.print("      [red]Invalid number[/]")

        elif label == "Event":
            console.print("        [bold yellow][1][/] win  [bold yellow][2][/] loss  [bold yellow][3][/] bet")
            cur_key = {"win": "1", "loss": "2", "bet": "3"}.get(r.cond_trigger, "1")
            st = _ask("      Event", default=cur_key, choices=["1", "2", "3"])
            if st is _BACK:
                continue
            r.cond_trigger = SEQ_TRIGGERS[st]

        elif label == "Field":
            if r.cond_type == "profit":
                console.print("        [bold yellow][1][/] profit  [bold yellow][2][/] loss  [bold yellow][3][/] balance")
                cur_key = next((k for k, v in PROFIT_FIELDS.items() if v == r.cond_field), "1")
                pf = _ask("      Field", default=cur_key, choices=list(PROFIT_FIELDS.keys()))
                if pf is _BACK:
                    continue
                r.cond_field = PROFIT_FIELDS[pf]
            elif r.cond_type == "bet":
                for k, name in BET_FIELDS.items():
                    console.print(f"        [bold yellow][{k}][/] {name}")
                cur_key = next((k for k, v in BET_FIELDS.items() if v == r.cond_field), "1")
                bf = _ask("      Field", default=cur_key, choices=list(BET_FIELDS.keys()))
                if bf is _BACK:
                    continue
                r.cond_field = BET_FIELDS[bf]

        elif label == "Operator":
            for k, (_, sym) in CMP_OPS.items():
                console.print(f"        [bold yellow][{k}][/] {sym}")
            cur_key = next((k for k, v in CMP_OPS.items() if v[0] == r.cond_mode), "1")
            op = _ask("      Operator", default=cur_key, choices=list(CMP_OPS.keys()))
            if op is _BACK:
                continue
            r.cond_mode = CMP_OPS[op][0]

        elif label == "Value":
            vl = _ask("      Value", default=str(r.cond_value))
            if vl is _BACK:
                continue
            try:
                r.cond_value = float(vl)
            except ValueError:
                console.print("      [red]Invalid number[/]")

        elif label == "Action":
            console.print("      [dim]Change action:[/]")
            for k, (_, al) in RULE_ACTIONS.items():
                console.print(f"        [bold yellow][{k:>2}][/] {al}")
            cur_key = next((k for k, v in RULE_ACTIONS.items() if v[0] == r.action), "1")
            ak = _ask("      Action", default=cur_key, choices=list(RULE_ACTIONS.keys()))
            if ak is _BACK:
                continue
            new_action = RULE_ACTIONS[ak][0]
            if new_action != r.action:
                r.action = new_action
                r.action_value = 0.0  # reset value when action changes

        elif label == "Action value":
            av = _ask("      Action value", default=str(r.action_value))
            if av is _BACK:
                continue
            try:
                r.action_value = float(av)
            except ValueError:
                console.print("      [red]Invalid number[/]")


def setup_wizard():
    console.clear()
    console.print()
    console.print(Align.center(
        Panel(
            "[bold cyan]WolfBet AutoBot  v2.6[/]\n"
            "[dim]High-speed dice auto-bettor with live TUI dashboard[/]",
            box=box.DOUBLE, expand=False, padding=(1, 6), style="blue"
        )
    ))
    console.print()
    console.print("[dim]  Tips: arrow keys work  |  type 'back' for previous step  |  'none' to clear a value[/]")
    console.print()

    # ── Load saved config as defaults ─────────────────────
    has_saved = load_config()
    # Environment variable takes priority (ISO 27001: externalized secrets)
    env_key = os.environ.get("WOLFBET_API_KEY")
    if env_key:
        state.api_key = env_key
    saved_key = state.api_key
    balances  = None
    available_bal = {}

    if has_saved and saved_key:
        console.print("[green]  Found saved config from last session:[/]")
        console.print(f"    [dim]Currency:[/]   [yellow]{state.currency.upper()}[/]")
        console.print(f"    [dim]Strategy:[/]   [yellow]{state.strategy}[/]")
        console.print(f"    [dim]Multiplier:[/] [yellow]{state.multiplier}x[/]")
        console.print(f"    [dim]Base bet:[/]   [yellow]{state.base_bet:.8f}[/]")
        console.print(f"    [dim]API key:[/]    [yellow]{saved_key[:8]}…{saved_key[-4:]}[/]")
        console.print()

    # ── Step functions ────────────────────────────────────
    def step_api_key():
        nonlocal balances, saved_key
        console.print(Rule("[bold cyan]Step 1 / 7 — API Authentication[/]"))
        if saved_key:
            console.print(f"  Saved key: [yellow]{saved_key[:8]}…{saved_key[-4:]}[/]")
            v = _ask("Use saved API key? (y/n)", default="y", choices=["y", "n"])
            if v is _BACK:
                return False
            if v.lower() == "y":
                state.api_key = saved_key
            else:
                console.print("  [dim]Get your token from:[/] [underline]https://wolfbet.com/user/settings?tab=api[/]")
                v = _ask("Paste your WolfBet API key")
                if v is _BACK:
                    return False
                state.api_key = v
                saved_key = v
        else:
            console.print("  [dim]Get your token from:[/] [underline]https://wolfbet.com/user/settings?tab=api[/]")
            v = _ask("Paste your WolfBet API key")
            if v is _BACK:
                return False
            state.api_key = v
            saved_key = v

        console.print("  [dim]Testing connection…[/]")
        try:
            balances = api_get_balances()
            console.print("[green]  Connected![/]\n")
        except Exception as e:
            detail = f": {e}" if APP_ENV != "production" else ""
            console.print(f"[red]  Connection failed{detail}.[/]")
            console.print("[yellow]  Check your API key and try again.[/]\n")
            state.api_key = ""
            saved_key = ""
            return False
        return True

    def step_currency():
        nonlocal available_bal
        console.print(Rule("[bold cyan]Step 2 / 7 — Currency[/]"))
        if balances:
            console.print("  [dim]Your balances:[/]")
            for b in balances:
                amt = float(b["amount"])
                available_bal[b["currency"]] = amt
                if amt > 0:
                    console.print(f"    [yellow]{b['currency'].upper():8s}[/] {amt:.8f}")
            console.print()

        v = _ask("Select currency", default=state.currency, choices=CURRENCIES)
        if v is _BACK:
            return False
        state.currency = v.lower()
        bal = available_bal.get(state.currency, 0.0)
        state.start_balance   = bal
        state.current_balance = bal
        state.highest_balance = bal
        state.lowest_balance  = bal
        console.print(f"  [green]Balance: {bal:.8f} {state.currency.upper()}[/]\n")
        return True

    def step_bet_config():
        console.print(Rule("[bold cyan]Step 3 / 7 — Bet Configuration[/]"))
        console.print("  [dim]Available games:[/]")
        for gk, label in GAME_LABELS.items():
            ep = GAME_ENDPOINTS[gk]
            console.print(f"    [yellow]{gk:14s}[/] — {label}  (rate limit: {ep[2]}/min)")
        console.print()

        v = _ask("Select game", default=state.game, choices=list(GAME_LABELS.keys()))
        if v is _BACK:
            return False
        state.game = v.lower()
        console.print()

        if state.game in ("dice", "dice-manual"):
            v = _ask("Bet direction", default=state.rule, choices=["over", "under"])
            if v is _BACK:
                return False
            state.rule = v.lower()
        else:
            state.rule = "over"  # not used for limbo

        console.print()
        console.print("  [dim]Multiplier examples (99% RTP, 1% house edge):[/]")
        for mult, chance in [(1.5, 66.0), (1.98, 50.0), (2.0, 49.5), (3.0, 33.0), (9.9, 10.0)]:
            console.print(f"    [yellow]{mult:5.2f}x[/]  →  ~{chance:.0f}% win")
        console.print()

        while True:
            v = _ask("Multiplier", default=str(state.multiplier))
            if v is _BACK:
                return False
            try:
                m = float(v)
            except ValueError:
                console.print("    [red]Enter a valid number[/]")
                continue
            if m < 1.01:
                console.print("    [red]Multiplier must be at least 1.01[/]")
                continue
            state.multiplier = m
            break
        win_chance = 99.0 / state.multiplier
        if state.game in ("dice", "dice-manual"):
            if state.rule == "over":
                state.bet_value = round(100.0 - win_chance - 0.01, 2)
            else:
                state.bet_value = round(win_chance, 2)
            console.print(f"    [dim]→ Bet value:[/] [yellow]{state.bet_value}[/]  [dim]Win chance:[/] [yellow]{win_chance:.2f}%[/]\n")
        else:
            console.print(f"    [dim]→ Win chance:[/] [yellow]{win_chance:.2f}%[/]\n")

        v = _ask(f"Base bet amount ({state.currency.upper()})", default=str(state.base_bet))
        if v is _BACK:
            return False
        state.base_bet    = float(v)
        state.current_bet = state.base_bet
        return True

    def step_strategy():
        console.print(Rule("[bold cyan]Step 4 / 7 — Betting Strategy[/]"))
        for k, (name, desc) in STRATEGIES.items():
            console.print(f"    [bold yellow][{k}][/] [cyan]{name:20s}[/]  [dim]{desc}[/]")
        console.print()

        v = _ask("Select strategy", default=state.strategy_key, choices=list(STRATEGIES.keys()))
        if v is _BACK:
            return False
        state.strategy_key = v
        state.strategy     = STRATEGIES[v][0]

        # ── Strategy parameter editing ──
        if v in ("2", "3", "5", "6", "7"):
            console.print()
            # Set sensible defaults based on strategy if coming from a different one
            if v in ("2", "6", "7"):  # loss-based strategies
                console.print(f"  [dim]On loss: multiply bet by[/] [yellow]{state.loss_mult}x[/]")
                lm = _ask("  Loss multiplier", default=str(state.loss_mult))
                if lm is _BACK:
                    return False
                try:
                    state.loss_mult = float(lm)
                except ValueError:
                    pass
            if v in ("3", "5"):  # win-based strategies
                console.print(f"  [dim]On win: multiply bet by[/] [yellow]{state.win_mult}x[/]")
                wm = _ask("  Win multiplier", default=str(state.win_mult))
                if wm is _BACK:
                    return False
                try:
                    state.win_mult = float(wm)
                except ValueError:
                    pass
            console.print()

        if v == "6":
            console.print("  [bold cyan]WolfRider[/] — your dedicated strategy:")
            console.print(f"    [green]• On win:[/]  reset to base bet")
            console.print(f"    [green]• On loss:[/] multiply by {state.loss_mult}x")
            console.print("    [green]• Switch over/under[/] on every 2nd, 6th, and 9th bet")
            console.print()

        elif v == "7":
            console.print()
            console.print("  [bold cyan]Delay Martingale[/] — flat bet through the first N")
            console.print("  consecutive losses, then start doubling.")
            console.print()
            d = _ask("  Losses before doubling starts", default=str(state.delay_martin_threshold))
            if d is _BACK:
                return False
            state.delay_martin_threshold = int(d)
            console.print(f"    [green]→ Flat for {state.delay_martin_threshold} losses, then Martingale[/]\n")

        elif v == "8":
            console.print()
            console.print("  [bold cyan]Rule-Based Strategy[/] — build custom conditions & actions")
            console.print("  [dim]Each rule: IF <condition> THEN <action>[/]")
            console.print()

            # Rule management loop
            while True:
                if state.custom_rules:
                    console.print(f"  [yellow]Current rules ({len(state.custom_rules)}):[/]")
                    for i, r in enumerate(state.custom_rules, 1):
                        console.print(f"    [dim]{i}.[/] {r.description}")
                    console.print()
                    console.print("    [bold yellow][1][/] Add rule   [bold yellow][2][/] Edit rule   [bold yellow][3][/] Delete rule   [bold yellow][4][/] Clear all   [bold yellow][5][/] Continue")
                    v2 = _ask("  Select", default="5", choices=["1", "2", "3", "4", "5"])
                    if v2 is _BACK:
                        return False

                    if v2 == "5":
                        break

                    elif v2 == "1":
                        rule = _build_one_rule()
                        if rule is _BACK:
                            continue
                        if rule is not None:
                            state.custom_rules.append(rule)
                            console.print(f"    [green]✓ Rule {len(state.custom_rules)}:[/] {rule.description}\n")

                    elif v2 == "2":
                        if len(state.custom_rules) == 1:
                            idx = 0
                        else:
                            en = _ask(f"  Rule # to edit (1-{len(state.custom_rules)})")
                            if en is _BACK:
                                continue
                            try:
                                idx = int(en) - 1
                                if not (0 <= idx < len(state.custom_rules)):
                                    console.print("    [red]Invalid rule number[/]")
                                    continue
                            except ValueError:
                                console.print("    [red]Enter a number[/]")
                                continue
                        old = state.custom_rules[idx]
                        console.print(f"    [dim]Editing:[/] {old.description}")
                        replacement = _edit_one_rule(old)
                        if replacement is _BACK:
                            continue
                        if replacement is not None:
                            state.custom_rules[idx] = replacement
                            console.print(f"    [green]✓ Updated:[/] {replacement.description}\n")

                    elif v2 == "3":
                        if len(state.custom_rules) == 1:
                            idx = 0
                        else:
                            dn = _ask(f"  Rule # to delete (1-{len(state.custom_rules)})")
                            if dn is _BACK:
                                continue
                            try:
                                idx = int(dn) - 1
                                if not (0 <= idx < len(state.custom_rules)):
                                    console.print("    [red]Invalid rule number[/]")
                                    continue
                            except ValueError:
                                console.print("    [red]Enter a number[/]")
                                continue
                        removed = state.custom_rules.pop(idx)
                        console.print(f"    [red]Deleted:[/] {removed.description}\n")

                    elif v2 == "4":
                        state.custom_rules = []
                        console.print("    [yellow]All rules cleared.[/]\n")

                else:
                    # No existing rules — go straight to builder
                    console.print("  [dim]No rules yet. Let's build some.[/]\n")
                    rule = _build_one_rule()
                    if rule is _BACK:
                        return False
                    if rule is not None:
                        state.custom_rules.append(rule)
                        console.print(f"    [green]✓ Rule {len(state.custom_rules)}:[/] {rule.description}\n")
                    else:
                        break

            if state.custom_rules:
                state.custom_rules_text = json.dumps([r.to_dict() for r in state.custom_rules])
                console.print(f"  [bold green]→ {len(state.custom_rules)} rule(s) configured[/]\n")
            else:
                console.print("  [yellow]No rules configured — will use Flat bet.[/]\n")

        console.print()
        return True

    def step_stop_conditions():
        console.print(Rule("[bold cyan]Step 5 / 7 — Stop Conditions[/]"))
        console.print("  [dim]Enter to keep current  |  'none' to clear  |  'back' for previous step[/]\n")

        v = _ask_optional(f"Stop when profit reaches ({state.currency.upper()})", state.max_profit)
        if v is _BACK:
            return False
        state.max_profit = float(v) if v else None

        v = _ask_optional(f"Stop when loss exceeds ({state.currency.upper()})", state.max_loss)
        if v is _BACK:
            return False
        state.max_loss = float(v) if v else None

        v = _ask_optional("Stop after N bets", state.max_bets)
        if v is _BACK:
            return False
        state.max_bets = int(v) if v else None

        v = _ask_optional("Stop after N wins", state.max_wins)
        if v is _BACK:
            return False
        state.max_wins = int(v) if v else None

        v = _ask_optional(f"Stop if balance drops below ({state.currency.upper()})", state.stop_on_balance)
        if v is _BACK:
            return False
        state.stop_on_balance = float(v) if v else None

        console.print("\n  [bold]Profit-based bet increment[/] [dim]— auto-raise base bet as profit grows[/]")
        v = _ask_optional(f"Profit threshold to trigger increment ({state.currency.upper()})", state.profit_threshold)
        if v is _BACK:
            return False
        state.profit_threshold = float(v) if v else None

        v = _ask_optional(f"Increment amount to add to base bet ({state.currency.upper()})", state.profit_increment)
        if v is _BACK:
            return False
        state.profit_increment = float(v) if v else None

        # init milestone
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold

        console.print()
        return True

    def step_advanced_tuning():
        """Optional Phase 2 session tuning: recurring, streak delays, basebet_pct, milestones."""
        console.print(Rule("[bold cyan]Step 5b / 7 — Advanced session tuning (optional)[/]"))
        gate = _ask("Configure advanced tuning? (y/N)",
                    default="y" if (state.recurring or state.basebet_pct or state.streak_delay_loss[0] or
                                    state.streak_delay_win[0] or state.streak_delay_bets[0] or
                                    state.streakbet_loss[0]) else "n",
                    choices=["y", "n"])
        if gate.lower() != "y":
            console.print()
            return True

        console.print("  [dim]Enter to keep current  |  'none' to clear  |  'back' for previous step[/]\n")

        # Recurring — boolean gate; whitelisted stop-reasons drive the restart.
        rec = _ask("Recurring restart on non-loss stop? (y/N)",
                   default="y" if state.recurring else "n", choices=["y", "n"])
        state.recurring = (rec.lower() == "y")

        # basebet_pct — dynamic base bet as % of balance.
        v = _ask_optional("Base bet as % of balance (0-1, blank/none to disable)",
                          state.basebet_pct or None)
        if v is _BACK:
            return False
        try:
            state.basebet_pct = float(v) if v else 0.0
        except ValueError:
            state.basebet_pct = 0.0

        # Four streak-format fields — all use "N:X" grammar via _parse_streak_delay.
        def _ask_streak(prompt, current_tuple):
            current_str = f"{current_tuple[0]}:{current_tuple[1]}" if current_tuple[0] > 0 else None
            raw = _ask_optional(prompt, current_str)
            if raw is _BACK:
                return _BACK
            return _parse_streak_delay(raw)  # (0, 0.0) on empty / malformed

        for attr, prompt in [
            ("streak_delay_loss", "Streak delay on losses (format 'N:sec')"),
            ("streak_delay_win",  "Streak delay on wins (format 'N:sec')"),
            ("streak_delay_bets", "Streak delay every N bets (format 'N:sec')"),
            ("streakbet_loss",    "Streakbet loss reduction (format 'N:multiplier')"),
        ]:
            result = _ask_streak(prompt, getattr(state, attr))
            if result is _BACK:
                return False
            setattr(state, attr, result)

        # Milestone thresholds.
        v = _ask_optional("Milestone every N bets (default 100)", state.milestone_bets or 100)
        if v is _BACK:
            return False
        try:
            state.milestone_bets = int(v) if v else 100
        except ValueError:
            state.milestone_bets = 100

        for attr, prompt in [
            ("milestone_wins",   "Milestone every N wins"),
            ("milestone_losses", "Milestone every N losses"),
        ]:
            v = _ask_optional(prompt, getattr(state, attr) or None)
            if v is _BACK:
                return False
            try:
                setattr(state, attr, int(v) if v else 0)
            except ValueError:
                setattr(state, attr, 0)

        v = _ask_optional(f"Milestone every {state.currency.upper()} profit crossed",
                          state.milestone_profit or None)
        if v is _BACK:
            return False
        try:
            state.milestone_profit = float(v) if v else 0.0
        except ValueError:
            state.milestone_profit = 0.0

        console.print()
        return True

    def step_preset():
        console.print(Rule("[bold cyan]Step 6 / 7 — Presets[/]"))
        presets = list_presets()
        if presets:
            console.print("  [dim]Saved presets:[/]")
            for name, data in presets.items():
                console.print(f"    [yellow]{name}[/]  [dim]→ {data.get('strategy', '?')} on {data.get('currency', '?').upper()}[/]")
            console.print()

        v = _ask("Save current config as preset? (name or 'skip')", default="skip")
        if v is _BACK:
            return False
        if v.lower() != "skip":
            save_preset(v)
            console.print(f"  [green]Saved preset '{v}'[/]\n")
        else:
            console.print()
        return True

    def step_confirm():
        console.print(Rule("[bold green]Step 7 / 7 — Review & Confirm[/]"))
        rows = [
            ("Currency",        f"{state.currency.upper()}"),
            ("Balance",         f"{state.start_balance:.8f}"),
            ("Rule",            state.rule),
            ("Multiplier",      f"{state.multiplier}x  (win chance {99/state.multiplier:.2f}%)"),
            ("Bet value",       str(state.bet_value)),
            ("Base bet",        f"{state.base_bet:.8f} {state.currency.upper()}"),
            ("Strategy",        state.strategy),
        ]
        if state.strategy_key == "6":
            rows.append(("Switching",   "over/under on bet 2, 6, 9 multiples"))
        if state.strategy_key == "7":
            rows.append(("Delay",       f"flat for {state.delay_martin_threshold} losses"))
        if state.strategy_key == "8" and state.custom_rules:
            rows.append(("Rules",       f"{len(state.custom_rules)} custom rule(s)"))
        rows += [
            ("Max profit",      str(state.max_profit) if state.max_profit else "—"),
            ("Max loss",        str(state.max_loss)   if state.max_loss   else "—"),
            ("Max bets",        str(state.max_bets)   if state.max_bets   else "Unlimited"),
            ("Max wins",        str(state.max_wins)   if state.max_wins   else "Unlimited"),
            ("Min balance",     str(state.stop_on_balance) if state.stop_on_balance else "—"),
            ("Profit increment", f"+{state.profit_increment:.8f} every {state.profit_threshold} profit"
                if state.profit_increment is not None and state.profit_threshold else "—"),
        ]
        for k, val in rows:
            console.print(f"  [dim]{k:18s}[/] [yellow]{val}[/]")

        # Show rule details for strategy 8
        if state.strategy_key == "8" and state.custom_rules:
            console.print()
            for i, r in enumerate(state.custom_rules, 1):
                console.print(f"    [dim]{i}.[/] [green]{r.description}[/]")

        console.print()
        console.print("[bold red]  WARNING: Real money will be wagered. Bet responsibly.[/]")
        console.print()

        v = _ask("Start auto-betting? (y/n/back)", default="n", choices=["y", "n", "back"])
        if v is _BACK or v.lower() == "back":
            return False
        if v.lower() != "y":
            console.print("[yellow]Aborted.[/]")
            sys.exit(0)

        save_config()
        console.print(f"[dim]  Config saved to {CONFIG_PATH}[/]\n")
        return True

    # ── Run steps with back navigation ────────────────────
    steps = [step_api_key, step_currency, step_bet_config, step_strategy,
             step_stop_conditions, step_advanced_tuning, step_preset, step_confirm]
    i = 0
    while i < len(steps):
        ok = steps[i]()
        if ok:
            i += 1
        else:
            i = max(0, i - 1)

# ══════════════════════════════════════════════════════════
#  PRINT SESSION SUMMARY
# ══════════════════════════════════════════════════════════
def _print_summary():
    console.print()
    console.print(Rule("[bold cyan]Session Complete[/]"))
    console.print()
    # Finalize the current second/minute counters so peak/low are accurate
    if state._bets_this_sec > 0:
        if state._bets_this_sec > state.peak_bps:
            state.peak_bps = state._bets_this_sec
        if state._bets_this_sec < state.low_bps:
            state.low_bps = state._bets_this_sec
    if state._bets_this_min > 0:
        if state._bets_this_min > state.peak_bpm:
            state.peak_bpm = state._bets_this_min
        if state._bets_this_min < state.low_bpm:
            state.low_bpm = state._bets_this_min

    lo_bps = int(state.low_bps) if state.low_bps != float("inf") else 0
    lo_bpm = int(state.low_bpm) if state.low_bpm != float("inf") else 0

    pairs = [
        ("Session ID",      str(state.session_id)),
        ("Total Bets",      str(state.total_bets)),
        ("Wins / Losses",   f"{state.wins} / {state.losses}"),
        ("Win Rate",        f"{state.wins/state.total_bets*100:.1f}%" if state.total_bets else "—"),
        ("Profit",          f"{state.profit:+.8f} {state.currency.upper()}"),
        ("Start Balance",   f"{state.start_balance:.8f} {state.currency.upper()}"),
        ("Final Balance",   f"{state.current_balance:.8f} {state.currency.upper()}"),
        ("Streaks",         f"W+{state.max_win_streak}  L-{state.max_loss_streak}"),
        ("Best Win",        f"+{state.highest_win:.8f}"),
        ("Worst Loss",      f"-{state.biggest_loss:.8f}"),
        ("Speed",           f"{state.bets_per_second:.1f}/s  {state.bets_per_minute:.0f}/m  "
                            f"Range: {lo_bps}-{int(state.peak_bps)}/s  "
                            f"{lo_bpm}-{int(state.peak_bpm)}/m"),
    ]
    for k, v in pairs:
        sty = "green" if ("profit" in k.lower() and state.profit >= 0) else (
              "red"   if ("profit" in k.lower() and state.profit <  0) else "white")
        console.print(f"  [dim]{k:18s}[/] [{sty}]{v}[/]")
    console.print()

# ══════════════════════════════════════════════════════════
#  --monitor COMMAND  (attach live TUI to running daemon)
# ══════════════════════════════════════════════════════════
def _monitor_read_state():
    """Read daemon state file and return dict, or None if not running."""
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return None

def _monitor_get_pid(d: dict) -> int:
    """Get daemon PID and verify it's alive."""
    pid = d.get("pid", 0)
    if pid:
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            pass
    return 0

def _monitor_get_recent_bets(session_id: int, limit: int) -> list:
    """Fetch recent bets from DB for the monitor view."""
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = _db_conn()
        rows = conn.execute("""
            SELECT id, substr(timestamp,1,19), amount, result_value, state, profit, balance_after
            FROM bets WHERE session_id=? ORDER BY id DESC LIMIT ?
        """, (session_id, limit)).fetchall()
        conn.close()
        result = []
        for r in rows:
            result.append({
                "n": r[0], "time": (r[1] or "")[11:],
                "amt": r[2] or 0, "roll": r[3] or 0,
                "state": r[4] or "lose", "pnl": r[5] or 0,
                "bal": r[6] or 0,
            })
        return result  # newest first
    except Exception:
        return []

def _build_monitor_screen(d: dict, bets: list, pid_alive: bool) -> str:
    """Build full-screen ANSI dashboard from state dict + bet list."""
    try:
        term_size = os.get_terminal_size()
        term_h = term_size.lines
        term_w = term_size.columns
    except OSError:
        term_h, term_w = 24, 80

    lines = []
    def add(s: str):
        lines.append(f"{s}{_CLR_EOL}")
    def add_blank():
        lines.append(_CLR_EOL)

    # Extract fields with safe defaults
    sid       = d.get("session_id", "?")
    uptime    = str(timedelta(seconds=d.get("uptime_sec", 0)))
    currency  = d.get("currency", "?").upper()
    strategy  = d.get("strategy", "?")
    mult      = d.get("multiplier", 0)
    rule      = d.get("rule", "over")
    base_bet  = d.get("base_bet", 0)
    cur_bet   = d.get("current_bet", 0)
    hi_bet    = d.get("highest_bet", 0)
    bal       = d.get("current_balance", 0)
    start_bal = d.get("start_balance", 0)
    profit    = d.get("profit", 0)
    wagered   = d.get("wagered", 0)
    total     = d.get("total_bets", 0)
    wins      = d.get("wins", 0)
    losses    = d.get("losses", 0)
    cs        = d.get("current_streak", 0)
    mws       = d.get("max_win_streak", 0)
    mls       = d.get("max_loss_streak", 0)
    hi_bal    = d.get("highest_balance", 0) or bal
    lo_bal    = d.get("lowest_balance", 0) or bal
    hi_win    = d.get("highest_win", 0)
    big_loss  = d.get("biggest_loss", 0)
    bps       = d.get("bets_per_second", 0)
    bpm       = d.get("bets_per_minute", 0)
    pk_bps    = int(d.get("peak_bps", 0))
    lw_bps    = int(d.get("low_bps", 0))
    pk_bpm    = int(d.get("peak_bpm", 0))
    lw_bpm    = int(d.get("low_bpm", 0))
    rl        = d.get("rate_remaining", 0)
    rl_max    = d.get("rate_limit", 480)
    paused    = d.get("paused", False)
    status    = d.get("status", "")

    # ── Row 1: Header ─────────────────────────────────
    if pid_alive and not paused:
        st_ind = _a("bold_green", "● LIVE")
    elif pid_alive and paused:
        st_ind = _a("bold_yellow", "● PAUSED")
    else:
        st_ind = _a("bold_red", "● STOPPED")
    hdr_vis = 4 + len(f"WolfBet v{VERSION}  #{sid}  {uptime}  ") + len("● STOPPED") + 1
    fill = max(0, term_w - hdr_vis)
    add(f"{_a('dim_blue', '─── ')}{_a('bold_cyan', f'WolfBet v{VERSION}')}"
        f"{_a('white', f'  #{sid}  {uptime}')}  {st_ind} "
        f"{_a('dim_blue', '─' * fill)}")

    # ── Row 2: Balance bar ────────────────────────────
    ps = "+" if profit >= 0 else ""
    pc = "bold_green" if profit >= 0 else "bold_red"
    wr = (wins / total * 100) if total else 0
    wr_c = "bold_green" if wr >= 50 else "bold_red"
    add(f"{_COLORS['bg_blue']}{_BOLD}{_COLORS['white']} BAL {_RST} "
        f"{_a('bold_white', f'{bal:.8f} {currency}')}"
        f"  {_a('dim', 'PnL')} {_a(pc, f'{ps}{profit:.8f}')}"
        f"  {_a('dim', 'WAG')} {_a('yellow', f'{wagered:.8f}')}"
        f"  {_a('dim', 'WR')} {_a(wr_c, f'{wr:.1f}%')}")

    # ── Row 3: Stats line 1 ──────────────────────────
    if cs > 0:
        sk = _a("green", f"W+{cs}")
    elif cs < 0:
        sk = _a("red", f"L{cs}")
    else:
        sk = _a("dim", "—")
    add(f" {_a('dim', 'Bets')} {_a('bold_white', str(total))}"
        f"  {_a('green', 'W')} {_a('bold_green', str(wins))}"
        f"  {_a('red', 'L')} {_a('bold_red', str(losses))}"
        f"  {_a('dim', 'Str')} {sk}"
        f"  {_a('dim', f'Best W+{mws}/L-{mls}')}")

    # ── Row 4: Bet / Strategy / Rate ──────────────────
    rl_c = "green" if rl > 100 else ("yellow" if rl > 20 else "red")
    s4 = f" {_a('dim', 'Bet')} {_a('yellow', f'{cur_bet:.8f}')}"
    s4 += f"  {_a('dim', 'Base')} {_a('bold_green', f'{base_bet:.8f}')}"
    s4 += (f"  {_a('dim', 'Hi')} {_a('white', f'{hi_bet:.8f}')}"
           f"  {_a('cyan', strategy)}"
           f"  {_a('dim', f'{mult}x')}"
           f"  {_a('dim', rule)}"
           f"  {_a('dim', 'BPS')} {_a('magenta', f'{bps:.1f}')}"
           f"  {_a('dim', 'BPM')} {_a('magenta', f'{bpm:.0f}')}"
           f"  {_a('dim', 'RL')} {_a(rl_c, f'{rl}/{rl_max}')}"
           f"  {_a('dim', 'Speed')} {_a('magenta', f'{lw_bps}-{pk_bps}/s {lw_bpm}-{pk_bpm}/m')}")
    add(s4)

    # ── Row 5: Extremes / Performance ────────────────
    avg_pnl = profit / total if total else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    bal_change = bal - start_bal
    bc_c = "bold_green" if bal_change >= 0 else "bold_red"
    bc_s = "+" if bal_change >= 0 else ""
    s5 = (f" {_a('dim', 'Peak')} {_a('green', f'{hi_bal:.8f}')}"
          f"  {_a('dim', 'Low')} {_a('red', f'{lo_bal:.8f}')}"
          f"  {_a('dim', 'BestW')} {_a('green', f'+{hi_win:.8f}')}"
          f"  {_a('dim', 'WorstL')} {_a('red', f'-{big_loss:.8f}')}"
          f"  {_a('dim', 'Avg')} {_a(avg_c, f'{avg_s}{avg_pnl:.8f}')}"
          f"  {_a('dim', 'Bal')} {_a(bc_c, f'{bc_s}{bal_change:.8f}')}")
    add(s5)

    # ── Row 6: Sparkline placeholder (no history in state file) ──
    add(f" {_a('dim', 'P/L')} {_a('dim', '—')}")

    # ── Row 7: Separator ──────────────────────────────
    add(_a("dim_blue", "─" * term_w))

    # ── Row 8: Bets table header ──────────────────────
    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Amount':>14}  {'Roll':>7}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    # ── Rows 9..N-2: Recent bet rows ─────────────────
    bets_avail = max(0, term_h - 10)
    shown = bets[:bets_avail]  # already newest-first from DB

    for b in shown:
        bn = b["n"]
        btm = b["time"]
        bamt = b["amt"]
        broll = b["roll"]
        bpnl = b["pnl"]
        bbal = b["bal"]
        bwl_c = "bold_green" if b["state"] == "win" else "bold_red"
        bwl_t = " W " if b["state"] == "win" else " L "
        bpnl_c = "green" if bpnl >= 0 else "red"
        bsign = "+" if bpnl >= 0 else ""
        add(f"{_a('dim', f'{bn:>6}')}"
            f"  {_a('dim', f'{btm:<9}')}"
            f" {_a('white', f'{bamt:>14.8f}')}"
            f"  {_a('white', f'{broll:>7.2f}')}"
            f"  {_a(bwl_c, bwl_t)}"
            f"  {_a(bpnl_c, f'{bsign}{bpnl:>14.8f}')}"
            f"  {_a('white', f'{bbal:>14.8f}')}")

    for _ in range(bets_avail - len(shown)):
        add_blank()

    # ── Row N-1: Bottom separator ─────────────────────
    add(_a("dim_blue", "─" * term_w))

    # ── Row N: Status + controls ──────────────────────
    status_txt = status[:60] if status else "Monitoring…"
    keys = "[P]ause [R]esume [Q]uit/Detach [S]top daemon"
    add(f" {_a('bold_white', 'MONITOR')} {_a('white', status_txt)}  {_a('dim', keys)}")

    return "\r\n".join(lines)


def cmd_monitor():
    """Attach a live TUI viewer to a running daemon session."""
    d = _monitor_read_state()
    if not d:
        console.print("[yellow]No running session found. Start with: wolfctl start[/]")
        return
    pid = _monitor_get_pid(d)
    if not pid:
        console.print("[yellow]Daemon not running (stale state file).[/]")
        return

    sid = d.get("session_id", 0)
    console.print(f"[dim]Attaching to session #{sid} (PID {pid})… Press Q to detach.[/]")
    time.sleep(0.5)

    # Save terminal state
    _old_term = None
    try:
        import termios
        _old_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    # Enter alt screen
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()

    # Keyboard reader in background thread
    _monitor_running = [True]  # mutable so thread can see changes
    _monitor_action = [None]

    def _monitor_keys():
        while _monitor_running[0]:
            try:
                import termios as _t, tty as _tty
                fd = sys.stdin.fileno()
                old = _t.tcgetattr(fd)
                try:
                    _tty.setraw(fd)
                    ch = sys.stdin.read(1).lower()
                finally:
                    _t.tcsetattr(fd, _t.TCSADRAIN, old)
                if ch in ("q", "\x03", "\x1c"):  # Q, Ctrl+C, Ctrl+\
                    _monitor_action[0] = "quit"
                    _monitor_running[0] = False
                elif ch == "p":
                    _monitor_action[0] = "pause"
                elif ch == "r":
                    _monitor_action[0] = "resume"
                elif ch == "s":
                    _monitor_action[0] = "stop"
                    _monitor_running[0] = False
            except Exception:
                time.sleep(0.1)

    t_keys = threading.Thread(target=_monitor_keys, daemon=True)
    t_keys.start()

    try:
        while _monitor_running[0]:
            # Handle pending actions
            action = _monitor_action[0]
            _monitor_action[0] = None
            if action == "pause" and pid:
                try:
                    os.kill(pid, signal.SIGUSR1)
                except OSError:
                    pass
            elif action == "resume" and pid:
                try:
                    os.kill(pid, signal.SIGUSR2)
                except OSError:
                    pass

            # Read fresh state
            d = _monitor_read_state()
            if not d:
                break
            pid = _monitor_get_pid(d)

            # Fetch recent bets from DB
            bet_limit = max(0, (os.get_terminal_size().lines if sys.stdout.isatty() else 24) - 10)
            recent_bets = _monitor_get_recent_bets(sid, bet_limit)

            # Render
            sys.stdout.write("\033[H")
            sys.stdout.write(_build_monitor_screen(d, recent_bets, bool(pid)))
            sys.stdout.flush()
            time.sleep(0.5)

        # Handle stop action
        if _monitor_action[0] == "stop" or (locals().get("action") == "stop"):
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
    finally:
        _monitor_running[0] = False
        # Restore terminal
        sys.stdout.write("\033[?25h\033[?1049l\033[0m")
        sys.stdout.flush()
        if _old_term is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _old_term)
            except Exception:
                pass
        import subprocess
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
        except Exception:
            pass

    if _monitor_action[0] == "stop":
        console.print("[bold red]Daemon stopped.[/]")
    else:
        console.print("[dim]Detached from monitor. Daemon continues running.[/]")


# ══════════════════════════════════════════════════════════
#  --status COMMAND
# ══════════════════════════════════════════════════════════
def cmd_status():
    if not os.path.exists(STATE_PATH):
        console.print("[yellow]No running session found.[/]")
        sys.exit(0)
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception:
        console.print("[red]Could not read state file.[/]")
        sys.exit(1)

    pid = d.get("pid")
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass

    status = "[bold green]RUNNING[/]" if alive else "[bold red]STOPPED[/]"
    p = d.get("profit", 0)
    ps = "+" if p >= 0 else ""
    pc = "green" if p >= 0 else "red"
    wr = (d["wins"] / d["total_bets"] * 100) if d.get("total_bets") else 0
    uptime = str(timedelta(seconds=d.get("uptime_sec", 0)))

    # extract all fields
    bets = d.get("total_bets", 0)
    wins = d.get("wins", 0)
    losses = d.get("losses", 0)
    hi_bal = d.get("highest_balance", 0)
    lo_bal = d.get("lowest_balance", 0)
    hi_win = d.get("highest_win", 0)
    big_loss = d.get("biggest_loss", 0)
    bpm = d.get("bets_per_minute", 0)
    bps = d.get("bets_per_second", 0)
    pk_bps = int(d.get("peak_bps", 0))
    lw_bps = int(d.get("low_bps", 0))
    pk_bpm = int(d.get("peak_bpm", 0))
    lw_bpm = int(d.get("low_bpm", 0))
    start_bal = d.get("start_balance", 0)
    cur_bal = d.get("current_balance", 0)
    avg_pnl = p / bets if bets > 0 else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    cs = d.get("current_streak", 0)
    if cs > 0:
        sk_now = f"W+{cs}"
    elif cs < 0:
        sk_now = f"L{cs}"
    else:
        sk_now = "—"

    console.print(Rule(f"[bold cyan]WolfBet Session #{d.get('session_id', '?')}  {status}[/]"))

    # ── Session Info ──
    strat_str = f"{d.get('strategy', '?')}  {d.get('multiplier', '?')}x  {d.get('rule', '?')}"
    sk = d.get("strategy_key", "")
    if sk in ("2", "6", "7"):
        strat_str += f"  [dim](loss mult: {d.get('loss_mult', '?')}x)[/]"
    elif sk in ("3", "5"):
        strat_str += f"  [dim](win mult: {d.get('win_mult', '?')}x)[/]"

    rows = [
        ("Uptime",         uptime),
        ("Currency",       d.get("currency", "?").upper()),
        ("Strategy",       strat_str),
        ("Base / Current", f"{d.get('base_bet', 0):.8f} / {d.get('current_bet', 0):.8f}"),
        ("Highest Bet",    f"{d.get('highest_bet', 0):.8f}"),
    ]

    # Bet delay if set
    delay = d.get("bet_delay", 0)
    if delay:
        rows.append(("Bet Delay",     f"{delay}s"))

    rows += [
        ("Balance",        f"{start_bal:.8f} → {cur_bal:.8f}"),
        ("Peak / Low",     f"[green]{hi_bal:.8f}[/] / [red]{lo_bal:.8f}[/]"),
        ("Profit",         f"[{pc}]{ps}{p:.8f}[/]"),
        ("Wagered",        f"{d.get('wagered', 0):.8f}"),
        ("Avg P/L",        f"[{avg_c}]{avg_s}{avg_pnl:.8f}[/] per bet"),
        ("Bets",           str(bets)),
        ("Wins / Losses",  f"[green]{wins}[/] / [red]{losses}[/]"),
        ("Win Rate",       f"{wr:.1f}%"),
        ("Current Streak", sk_now),
        ("Best Streaks",   f"W+{d.get('max_win_streak', 0)}  L-{d.get('max_loss_streak', 0)}"),
        ("Best Win",       f"[green]+{hi_win:.8f}[/]"),
        ("Worst Loss",     f"[red]-{big_loss:.8f}[/]"),
        ("Speed",          f"{bps:.1f}/s  {bpm:.0f}/m  Range: {lw_bps}-{pk_bps}/s  {lw_bpm}-{pk_bpm}/m"),
        ("Rate Limit",     f"{d.get('rate_remaining', '?')}/{d.get('rate_limit', '?')}"),
    ]

    # ── Stop Conditions ──
    stop_parts = []
    mp = d.get("max_profit")
    if mp is not None:
        stop_parts.append(f"Profit ≥ {mp}")
    ml = d.get("max_loss")
    if ml is not None:
        stop_parts.append(f"Loss ≥ {ml}")
    mb = d.get("max_bets")
    if mb is not None:
        stop_parts.append(f"Bets ≥ {mb}")
    mw = d.get("max_wins")
    if mw is not None:
        stop_parts.append(f"Wins ≥ {mw}")
    sb = d.get("stop_on_balance")
    if sb is not None:
        stop_parts.append(f"Balance ≤ {sb}")
    if stop_parts:
        rows.append(("Stop Conditions", " | ".join(stop_parts)))
    else:
        rows.append(("Stop Conditions", "[dim]None set[/]"))

    # Profit increment
    pi = d.get("profit_increment")
    pt = d.get("profit_threshold")
    if pi and pt:
        rows.append(("Profit Bump",   f"+{pi:.8f} every {pt} profit"))

    rows += [
        ("Last Status",    d.get("status", "")[:80]),
        ("Updated",        d.get("updated_at", "?").replace("T", " ")),
    ]

    for k, v in rows:
        console.print(f"  [dim]{k:18s}[/] {v}")
    if d.get("last_error"):
        console.print(f"  [dim]{'Last Error':18s}[/] [red]{d['last_error'][:60]}[/]")

    # ── Rule-Based rules ──
    rc = d.get("rule_count", 0)
    if rc > 0:
        console.print()
        console.print(f"  [yellow]Rules ({rc}):[/]")
        for i, desc in enumerate(d.get("rule_descriptions", []), 1):
            console.print(f"    [dim]{i}.[/] {desc}")

    console.print()

# ══════════════════════════════════════════════════════════
#  --stop COMMAND
# ══════════════════════════════════════════════════════════
def cmd_stop():
    if not os.path.exists(PID_PATH):
        console.print("[yellow]No running session found (no PID file).[/]")
        sys.exit(0)
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent stop signal to PID {pid}.[/]")
    except ProcessLookupError:
        console.print("[yellow]Process already stopped.[/]")
        _cleanup_state_file()
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Could not stop{detail}.[/]")

# ══════════════════════════════════════════════════════════
#  --list-presets COMMAND
# ══════════════════════════════════════════════════════════
def cmd_list_presets():
    presets = list_presets()
    if not presets:
        console.print("[yellow]No saved presets.[/]")
        console.print(f"[dim]Presets are saved during the wizard or with --save-preset NAME[/]")
        return
    console.print(Rule("[bold cyan]Saved Presets[/]"))
    for name, data in presets.items():
        strat = data.get("strategy", "?")
        cur   = data.get("currency", "?").upper()
        mult  = data.get("multiplier", "?")
        base  = data.get("base_bet", 0)
        rules = data.get("custom_rules_text", "")
        rule_count = len(rules.strip().splitlines()) if rules else 0
        extra = f"  ({rule_count} rules)" if rule_count else ""
        console.print(f"  [bold yellow]{name:20s}[/]  [cyan]{strat}[/] on [yellow]{cur}[/]  "
                      f"{mult}x  base={base:.8f}{extra}")
    console.print()

# ══════════════════════════════════════════════════════════
#  DATABASE QUERY COMMANDS
# ══════════════════════════════════════════════════════════
def cmd_stats():
    """Show all-time session statistics."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()

    # session list — fetch all columns
    rows = conn.execute("""
        SELECT id, started_at AS started, ended_at AS ended,
               UPPER(currency) AS cur, strategy, multiplier, base_bet,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0)
        FROM sessions ORDER BY id DESC LIMIT 20
    """).fetchall()

    console.print(Rule("[bold cyan]Session History (last 20)[/]"))

    # Print each session as a detailed block
    for r in rows:
        sid, started, ended, cur, strat, mult, base, bets, wins, losses, \
            profit, wagered, start_bal, end_bal, mws, mls, \
            hi_bal, lo_bal, hi_win, big_loss, \
            bpm, bps, s_peak_bps, s_low_bps, s_peak_bpm, s_low_bpm = r
        bets = bets or 0
        wins = wins or 0
        losses = losses or 0
        profit = profit or 0
        wagered = wagered or 0
        start_bal = start_bal or 0
        end_bal = end_bal or 0
        mws = mws or 0
        mls = mls or 0
        mult = mult or 0
        base = base or 0
        hi_bal = hi_bal or 0
        lo_bal = lo_bal or 0
        hi_win = hi_win or 0
        big_loss = big_loss or 0
        bpm = bpm or 0
        bps = bps or 0
        s_peak_bps = int(s_peak_bps or 0)
        s_low_bps = int(s_low_bps or 0)
        s_peak_bpm = int(s_peak_bpm or 0)
        s_low_bpm = int(s_low_bpm or 0)
        pc = "green" if profit >= 0 else "red"
        ps = "+" if profit >= 0 else ""
        wr = f"{wins/bets*100:.1f}%" if bets > 0 else "—"
        avg_pnl = profit / bets if bets > 0 else 0
        avg_c = "green" if avg_pnl >= 0 else "red"
        avg_s = "+" if avg_pnl >= 0 else ""

        # compute uptime
        uptime_str = ""
        try:
            t0 = datetime.fromisoformat(started) if started else None
            t1 = datetime.fromisoformat(ended) if ended else datetime.now()
            if t0:
                delta = int((t1 - t0).total_seconds())
                h, rem = divmod(max(delta, 0), 3600)
                m, s = divmod(rem, 60)
                uptime_str = f"  [dim]Uptime:[/] {h}h {m}m {s}s"
        except Exception:
            pass

        console.print(f"  [bold cyan]Session #{sid}[/]  [dim]{(started or '?').replace('T', ' ')} → {(ended or 'running').replace('T', ' ')}[/]{uptime_str}")
        console.print(f"    [dim]Currency:[/] {cur or '?'}  [dim]Strategy:[/] {strat or '?'}  [dim]Mult:[/] {mult}x  [dim]Base:[/] {base:.8f}")
        console.print(f"    [dim]Bets:[/] {bets}  [dim]W:[/] [green]{wins}[/]  [dim]L:[/] [red]{losses}[/]  [dim]WR:[/] {wr}")
        console.print(f"    [dim]Speed:[/] {bps:.1f} avg/s  {bpm:.0f} avg/m  [dim]BPS:[/] {s_low_bps}-{s_peak_bps}/s  [dim]BPM:[/] {s_low_bpm}-{s_peak_bpm}/m")
        console.print(f"    [dim]Profit:[/] [{pc}]{ps}{profit:.8f}[/]  [dim]Wagered:[/] {wagered:.8f}  [dim]Avg P/L:[/] [{avg_c}]{avg_s}{avg_pnl:.8f}[/]")
        console.print(f"    [dim]Balance:[/] {start_bal:.8f} → {end_bal:.8f}  [dim]Peak:[/] [green]{hi_bal:.8f}[/]  [dim]Low:[/] [red]{lo_bal:.8f}[/]")
        console.print(f"    [dim]Streaks:[/] W+{mws}  L-{mls}  [dim]Best Win:[/] [green]+{hi_win:.8f}[/]  [dim]Worst Loss:[/] [red]-{big_loss:.8f}[/]")
        console.print()

    # totals
    totals = conn.execute("""
        SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
               COALESCE(SUM(losses),0), COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0),
               COALESCE(MAX(max_win_streak),0), COALESCE(MAX(max_loss_streak),0),
               COALESCE(MAX(profit),0), COALESCE(MIN(profit),0),
               COALESCE(MAX(total_bets),0), COALESCE(AVG(total_bets),0),
               COALESCE(MAX(highest_balance),0), COALESCE(MAX(highest_win),0),
               COALESCE(MAX(biggest_loss),0), COALESCE(AVG(bets_per_minute),0),
               COALESCE(MAX(peak_bps),0), COALESCE(MAX(peak_bpm),0),
               COALESCE(AVG(bets_per_second),0)
        FROM sessions
    """).fetchone()
    conn.close()

    sessions, tot_bets, tot_wins, tot_losses, tot_profit, tot_wagered, \
        best_ws, best_ls, best_profit, worst_profit, max_bets, avg_bets, \
        all_hi_bal, all_hi_win, all_big_loss, avg_bpm, \
        all_peak_bps, all_peak_bpm, avg_bps = totals
    pc = "green" if tot_profit >= 0 else "red"
    ps = "+" if tot_profit >= 0 else ""
    wr = f"{tot_wins/tot_bets*100:.1f}%" if tot_bets > 0 else "—"
    bp_c = "green" if best_profit >= 0 else "red"
    wp_c = "green" if worst_profit >= 0 else "red"
    avg_pnl = tot_profit / tot_bets if tot_bets > 0 else 0
    avg_pc = "green" if avg_pnl >= 0 else "red"
    avg_ps = "+" if avg_pnl >= 0 else ""

    console.print(Rule("[bold cyan]All-Time Totals[/]"))
    console.print(f"  [dim]Sessions:[/]      {sessions}")
    console.print(f"  [dim]Total Bets:[/]    {tot_bets}  (W {tot_wins} / L {tot_losses}  WR {wr})")
    console.print(f"  [dim]Wagered:[/]       {tot_wagered:.8f}")
    console.print(f"  [dim]Profit:[/]        [{pc}]{ps}{tot_profit:.8f}[/]")
    console.print(f"  [dim]Avg P/L:[/]       [{avg_pc}]{avg_ps}{avg_pnl:.8f}[/] per bet")
    console.print(f"  [dim]Best Session:[/]  [{bp_c}]+{best_profit:.8f}[/]")
    console.print(f"  [dim]Worst Session:[/] [{wp_c}]{worst_profit:.8f}[/]")
    console.print(f"  [dim]Peak Balance:[/]  {all_hi_bal:.8f}")
    console.print(f"  [dim]Best Win:[/]      [green]+{all_hi_win:.8f}[/]")
    console.print(f"  [dim]Worst Loss:[/]    [red]-{all_big_loss:.8f}[/]")
    console.print(f"  [dim]Best Streaks:[/]  W+{best_ws}  L-{best_ls}")
    console.print(f"  [dim]Speed:[/]         {avg_bps:.1f} avg/s  {avg_bpm:.0f} avg/m  [dim]Peak:[/] {int(all_peak_bps)}/s  {int(all_peak_bpm)}/m")
    console.print(f"  [dim]Bets/Session:[/]  Max {max_bets}  Avg {avg_bets:.0f}")
    console.print()

def cmd_last_bets(n: int):
    """Show last N bets from database."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()
    rows = conn.execute("""
        SELECT b.id, b.session_id, substr(b.timestamp,1,19) AS dt,
               b.amount, b.result_value, b.state, b.profit, b.balance_after
        FROM bets b ORDER BY b.id DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]No bets found.[/]")
        return

    console.print(Rule(f"[bold cyan]Last {len(rows)} Bets[/]"))
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("Bet",      width=7,  justify="right")
    t.add_column("Sess",     width=5,  justify="center")
    t.add_column("DateTime", width=19)
    t.add_column("Amount",   width=14, justify="right")
    t.add_column("Roll",     width=7,  justify="right")
    t.add_column("W/L",      width=4,  justify="center")
    t.add_column("P/L",      width=15, justify="right")
    t.add_column("Balance",  width=15, justify="right")

    for r in rows:
        bid, sid, dt, amt, roll, st, pnl, bal = r
        rc = "bold green" if st == "win" else "bold red"
        pc = "green" if (pnl or 0) >= 0 else "red"
        ps = "+" if (pnl or 0) >= 0 else ""
        t.add_row(
            str(bid), str(sid), dt or "?",
            f"{amt:.8f}" if amt else "?",
            f"{roll:.2f}" if roll else "?",
            Text("W" if st == "win" else "L", style=rc),
            Text(f"{ps}{pnl:.8f}" if pnl is not None else "?", style=pc),
            f"{bal:.8f}" if bal else "?"
        )
    console.print(t)
    console.print()

def _compute_streak_distribution(conn, session_id: int) -> dict:
    """Compute streak distribution from bets table.
    Returns {"win": {streak_len: count, ...}, "loss": {streak_len: count, ...}}"""
    rows = conn.execute(
        "SELECT state FROM bets WHERE session_id=? ORDER BY id", (session_id,)
    ).fetchall()
    dist = {"win": {}, "loss": {}}
    if not rows:
        return dist
    cur_type = None
    cur_len = 0
    for (st,) in rows:
        kind = "win" if st == "win" else "loss"
        if kind == cur_type:
            cur_len += 1
        else:
            # Record the streak that just ended
            if cur_type and cur_len > 0:
                dist[cur_type][cur_len] = dist[cur_type].get(cur_len, 0) + 1
            cur_type = kind
            cur_len = 1
    # Record the final streak
    if cur_type and cur_len > 0:
        dist[cur_type][cur_len] = dist[cur_type].get(cur_len, 0) + 1
    return dist


def cmd_session_bets(session_id: int):
    """Show full detailed stats and bets for a specific session."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()

    # session info — fetch all columns
    sess = conn.execute("""
        SELECT id, started_at, ended_at,
               UPPER(currency), strategy, base_bet, multiplier,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0)
        FROM sessions WHERE id = ?
    """, (session_id,)).fetchone()

    if not sess:
        console.print(f"[red]Session #{session_id} not found.[/]")
        conn.close()
        return

    sid, started, ended, cur, strat, base, mult, bets, wins, losses, \
        profit, wagered, start_bal, end_bal, mws, mls, \
        hi_bal, lo_bal, hi_win, big_loss, \
        bpm, bps, s_pk_bps, s_lw_bps, s_pk_bpm, s_lw_bpm = sess

    # safe defaults
    bets = bets or 0; wins = wins or 0; losses = losses or 0
    profit = profit or 0; wagered = wagered or 0
    start_bal = start_bal or 0; end_bal = end_bal or 0
    mws = mws or 0; mls = mls or 0; mult = mult or 0; base = base or 0
    hi_bal = hi_bal or 0; lo_bal = lo_bal or 0
    hi_win = hi_win or 0; big_loss = big_loss or 0
    bpm = bpm or 0; bps = bps or 0
    s_pk_bps = int(s_pk_bps or 0); s_lw_bps = int(s_lw_bps or 0)
    s_pk_bpm = int(s_pk_bpm or 0); s_lw_bpm = int(s_lw_bpm or 0)

    pc = "green" if profit >= 0 else "red"
    ps = "+" if profit >= 0 else ""
    wr = f"{wins/bets*100:.1f}%" if bets > 0 else "—"
    avg_pnl = profit / bets if bets > 0 else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""

    # compute uptime
    uptime_str = "—"
    try:
        t0 = datetime.fromisoformat(started) if started else None
        t1 = datetime.fromisoformat(ended) if ended else datetime.now()
        if t0:
            delta = int((t1 - t0).total_seconds())
            h, rem = divmod(max(delta, 0), 3600)
            m, s = divmod(rem, 60)
            uptime_str = f"{h}h {m}m {s}s"
    except Exception:
        pass

    console.print(Rule(f"[bold cyan]Session #{sid} — Full Stats[/]"))
    console.print(f"  [dim]Started:[/]       {(started or '?').replace('T', ' ')}")
    console.print(f"  [dim]Ended:[/]         {(ended or 'running').replace('T', ' ')}")
    console.print(f"  [dim]Uptime:[/]        {uptime_str}")
    console.print(f"  [dim]Currency:[/]      {cur}")
    console.print(f"  [dim]Strategy:[/]      {strat}  {mult}x  Base: {base:.8f}")
    console.print(f"  [dim]Bets:[/]          {bets}  [dim]W:[/] [green]{wins}[/]  [dim]L:[/] [red]{losses}[/]  [dim]WR:[/] {wr}")
    console.print(f"  [dim]Profit:[/]        [{pc}]{ps}{profit:.8f}[/]")
    console.print(f"  [dim]Wagered:[/]       {wagered:.8f}")
    console.print(f"  [dim]Avg P/L:[/]       [{avg_c}]{avg_s}{avg_pnl:.8f}[/] per bet")
    console.print(f"  [dim]Balance:[/]       {start_bal:.8f} → {end_bal:.8f}")
    console.print(f"  [dim]Peak / Low:[/]    [green]{hi_bal:.8f}[/] / [red]{lo_bal:.8f}[/]")
    console.print(f"  [dim]Best Streaks:[/]  W+{mws}  L-{mls}")
    console.print(f"  [dim]Best Win:[/]      [green]+{hi_win:.8f}[/]")
    console.print(f"  [dim]Worst Loss:[/]    [red]-{big_loss:.8f}[/]")
    console.print(f"  [dim]Speed:[/]         {bps:.1f} avg/s  {bpm:.0f} avg/m  Range: {s_lw_bps}-{s_pk_bps}/s  {s_lw_bpm}-{s_pk_bpm}/m")

    # ── Streak Distribution ──────────────────────────
    console.print()
    console.print(Rule("[bold cyan]Streak Distribution[/]"))

    dist = _compute_streak_distribution(conn, session_id)

    # Loss streaks
    loss_dist = dist["loss"]
    if loss_dist:
        console.print(f"  [bold red]Loss Streaks[/]  (how many times each streak length occurred)")
        t_loss = Table(box=box.SIMPLE, show_edge=False, padding=(0, 2))
        t_loss.add_column("Streak", style="bold red", justify="right")
        t_loss.add_column("Count", justify="right")
        t_loss.add_column("", width=30)  # visual bar
        max_count = max(loss_dist.values()) if loss_dist else 1
        for length in sorted(loss_dist.keys()):
            count = loss_dist[length]
            bar_w = int(count / max_count * 25)
            bar = "[red]" + "█" * bar_w + "[/]"
            t_loss.add_row(f"L-{length}", str(count), bar)
        console.print(t_loss)
    else:
        console.print("  [dim]No loss streaks recorded[/]")

    console.print()

    # Win streaks
    win_dist = dist["win"]
    if win_dist:
        console.print(f"  [bold green]Win Streaks[/]  (how many times each streak length occurred)")
        t_win = Table(box=box.SIMPLE, show_edge=False, padding=(0, 2))
        t_win.add_column("Streak", style="bold green", justify="right")
        t_win.add_column("Count", justify="right")
        t_win.add_column("", width=30)  # visual bar
        max_count = max(win_dist.values()) if win_dist else 1
        for length in sorted(win_dist.keys()):
            count = win_dist[length]
            bar_w = int(count / max_count * 25)
            bar = "[green]" + "█" * bar_w + "[/]"
            t_win.add_row(f"W+{length}", str(count), bar)
        console.print(t_win)
    else:
        console.print("  [dim]No win streaks recorded[/]")

    # ── Recent Bets ──────────────────────────────────
    rows = conn.execute("""
        SELECT id, substr(timestamp,12,8), amount, result_value, state, profit, balance_after
        FROM bets WHERE session_id = ? ORDER BY id DESC LIMIT 50
    """, (session_id,)).fetchall()
    conn.close()

    if rows:
        console.print()
        console.print(Rule(f"[bold cyan]Last {len(rows)} Bets[/]"))
        t = Table(box=box.SIMPLE, expand=True)
        t.add_column("Bet",     width=7, justify="right")
        t.add_column("Time",    width=9)
        t.add_column("Amount",  width=14, justify="right")
        t.add_column("Roll",    width=7, justify="right")
        t.add_column("W/L",     width=4, justify="center")
        t.add_column("P/L",     width=15, justify="right")
        t.add_column("Balance", width=15, justify="right")

        for r in rows:
            bid, tm, amt, roll, st, pnl, bal = r
            rc = "bold green" if st == "win" else "bold red"
            pcc = "green" if (pnl or 0) >= 0 else "red"
            pss = "+" if (pnl or 0) >= 0 else ""
            t.add_row(
                str(bid), tm or "?",
                f"{amt:.8f}" if amt else "?",
                f"{roll:.2f}" if roll else "?",
                Text("W" if st == "win" else "L", style=rc),
                Text(f"{pss}{pnl:.8f}" if pnl is not None else "?", style=pcc),
                f"{bal:.8f}" if bal else "?"
            )
        console.print(t)
    console.print()

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def _load_and_connect():
    """Load saved config and fetch fresh balance. Used by --resume and --daemon."""
    if not load_config():
        console.print("[red]No saved config found. Run without --resume first.[/]")
        sys.exit(1)
    console.print("[dim]Loading saved config…[/]")
    try:
        balances = api_get_balances()
        for b in balances:
            if b["currency"] == state.currency:
                bal = float(b["amount"])
                state.start_balance   = bal
                state.current_balance = bal
                state.highest_balance = bal
                state.lowest_balance  = bal
                break
        console.print(f"[green]Resumed — {state.strategy} on {state.currency.upper()} "
                      f"@ {state.base_bet:.8f} base bet[/]")
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Connection failed{detail}. Check your settings and try again.[/]")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="WolfBet AutoBot v" + VERSION)
    parser.add_argument("--resume",  action="store_true", help="Skip wizard, reuse saved config")
    parser.add_argument("--daemon",  action="store_true", help="Run in background (no TUI, implies --resume)")
    parser.add_argument("--setup-only", action="store_true", help="Run wizard, save config, don't start betting")
    parser.add_argument("--monitor", action="store_true", help="Attach live TUI to a running daemon session")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status",  action="store_true", help="Show status of running session")
    group.add_argument("--stop",    action="store_true", help="Stop a running daemon session")
    group.add_argument("--list-presets", action="store_true", help="List saved presets")
    group.add_argument("--stats",   action="store_true", help="Show all-time session statistics")
    group.add_argument("--last-bets", type=int, metavar="N", help="Show last N bets from database")
    group.add_argument("--session-bets", type=int, metavar="ID", help="Show bets for a specific session")
    parser.add_argument("--preset", type=str, metavar="NAME",
                        help="Load a named preset and start (skips wizard)")
    parser.add_argument("--export", type=str, metavar="PATH",
                        help="Export a session to CSV or JSON (format from extension) and exit")
    parser.add_argument("--export-session-id", type=int, metavar="ID",
                        help="Session ID for --export (default: latest)")
    parser.add_argument("--backtest", action="store_true",
                        help="Run strategy against saved rolls or random samples (no API calls)")
    parser.add_argument("--replay-session", type=int, metavar="ID",
                        help="Backtest data source: replay bets from session #ID")
    parser.add_argument("--simulate", type=int, metavar="N",
                        help="Backtest data source: simulate N synthetic bets (default 1000)")
    parser.add_argument("--seed", type=int, metavar="N",
                        help="RNG seed for --simulate (deterministic reproducibility)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging (dev mode)")

    args = parser.parse_args()

    # ── Verbose mode ──────────────────────────────────────
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # ── --daemon implies --resume ──────────────────────────
    if args.daemon:
        args.resume = True

    # ── Quick commands (no DB/state needed) ───────────────
    if args.export:
        init_db()
        sid = args.export_session_id or _latest_session_id()
        if sid <= 0:
            console.print("[red]No sessions in the DB yet.[/]")
            sys.exit(1)
        try:
            n, fmt = _export_session(sid, args.export)
            console.print(f"[green]✓[/] Exported [bold]{n}[/] bets from session #{sid} → "
                          f"[cyan]{os.path.expanduser(args.export)}[/] ({fmt.upper()})")
            sys.exit(0)
        except Exception as e:
            console.print(f"[red]✗ Export failed:[/] {e}")
            sys.exit(1)
    if args.backtest:
        _run_backtest(args)
        return
    if args.status:
        cmd_status()
        return
    if args.stop:
        cmd_stop()
        return
    if args.list_presets:
        cmd_list_presets()
        return
    if args.stats:
        cmd_stats()
        return
    if args.last_bets:
        cmd_last_bets(args.last_bets)
        return
    if args.session_bets is not None:
        cmd_session_bets(args.session_bets)
        return

    # ── Setup-only mode ───────────────────────────────────
    if args.setup_only:
        init_db()
        setup_wizard()
        console.print("[bold green]Config saved.[/] Start with: [cyan]wolfctl start[/] or [cyan]python3 wolfbet.py --daemon[/]")
        return

    # ── Monitor mode (attach to running daemon) ──────────
    if args.monitor:
        cmd_monitor()
        return

    init_db()

    # ── Load config based on mode ─────────────────────────
    if args.preset:
        if not load_preset(args.preset):
            console.print(f"[red]Preset '{args.preset}' not found.[/]")
            cmd_list_presets()
            sys.exit(1)
        # need API key from main config
        if not state.api_key:
            saved = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
            if saved.get("api_key"):
                state.api_key = saved["api_key"]
            else:
                console.print("[red]No API key found. Run wizard first.[/]")
                sys.exit(1)
        console.print(f"[green]Loaded preset '{args.preset}' — {state.strategy} on "
                      f"{state.currency.upper()} @ {state.base_bet:.8f}[/]")
        # fetch fresh balance
        try:
            balances = api_get_balances()
            for b in balances:
                if b["currency"] == state.currency:
                    bal = float(b["amount"])
                    state.start_balance   = bal
                    state.current_balance = bal
                    state.highest_balance = bal
                    state.lowest_balance  = bal
                    break
        except Exception as e:
            detail = f": {e}" if APP_ENV != "production" else ""
            console.print(f"[red]Connection failed{detail}. Check your settings and try again.[/]")
            sys.exit(1)

    elif args.resume or args.daemon:
        _load_and_connect()
    else:
        setup_wizard()

    state.session_id    = _db_start_session()
    state.session_start = time.time()
    state.initial_multiplier = state.multiplier
    state.running       = True

    # write PID file
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    logger.warning("SESSION START  id=%s  mode=%s  currency=%s  strategy=%s  "
                   "multiplier=%s  base_bet=%.8f  balance=%.8f",
                   state.session_id,
                   "daemon" if args.daemon else ("preset" if args.preset else "tui"),
                   state.currency, state.strategy,
                   state.multiplier, state.base_bet,
                   state.current_balance)

    # ── Signal handlers ───────────────────────────────────
    def _shutdown(sig, frame):
        state.running = False
        state.status  = "Interrupted — saving session…"
        logger.warning("SESSION END (signal)  bets=%d  profit=%+.8f  balance=%.8f",
                    state.total_bets, state.profit, state.current_balance)
        # Best-effort auto-bet cleanup so the server drops the UUID.
        if is_auto_game(state.game) and state._auto_uuid:
            try:
                api_auto_stop()
            except Exception:
                pass
        _db_save_session()
        _save_state_file()
        _cleanup_state_file()
        # Phase 5a — session summary before exit (best-effort; never blocks save path)
        try:
            console.clear()
            console.print(format_session_summary("Interrupted"))
            console.print("\n[dim]  Press any key to close…[/]")
            _getch()
        except Exception as e:
            logger.warning("Could not render session summary on shutdown: %s", e)
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Remote control signals (from monitor)
    def _sig_pause(sig, frame):
        state.paused = True
        state.status = "PAUSED (remote)"
    def _sig_resume(sig, frame):
        state.paused = False
        state.status = "Resumed (remote)"
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, _sig_pause)
    if hasattr(signal, "SIGUSR2"):
        signal.signal(signal.SIGUSR2, _sig_resume)

    # ── Betting thread ────────────────────────────────────
    t_bet = threading.Thread(target=betting_loop, daemon=True, name="BettingLoop")
    t_bet.start()

    # ── Periodic save (DB + state file) every 5s ─────────
    def periodic_save():
        while state.running or state.paused:
            time.sleep(5)
            _db_save_session()
            _save_state_file()
    t_save = threading.Thread(target=periodic_save, daemon=True, name="PeriodicSave")
    t_save.start()

    # ══════════════════════════════════════════════════════
    #  DAEMON MODE — no TUI, just bet in background
    # ══════════════════════════════════════════════════════
    if args.daemon:
        console.print(f"[bold green]Daemon started[/]  PID={os.getpid()}  Session #{state.session_id}")
        console.print(f"[dim]  Check status:  python3 wolfbet.py --status[/]")
        console.print(f"[dim]  Stop:          python3 wolfbet.py --stop[/]")
        console.print(f"[dim]  Logs:          tail -f ~/.wolfbet_logs/wolfbet.log[/]")
        try:
            while state.running:
                time.sleep(2)
                _save_state_file()
        except KeyboardInterrupt:
            pass
        state.running = False
        _db_save_session()
        _save_state_file()
        _cleanup_state_file()
        _print_summary()
        return

    # ══════════════════════════════════════════════════════
    #  TUI MODE — interactive dashboard (direct ANSI rendering)
    # ══════════════════════════════════════════════════════
    t_input = threading.Thread(target=input_handler, daemon=True, name="InputHandler")
    t_input.start()

    # Save terminal state and enter alternate screen buffer
    _old_term = None
    try:
        import termios
        _old_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    sys.stdout.write("\033[?1049h")  # enter alt screen
    sys.stdout.write("\033[?25l")    # hide cursor
    sys.stdout.flush()

    try:
        while state.running or state.paused:
            sys.stdout.write("\033[H")  # cursor home
            sys.stdout.write(build_dashboard_screen())
            sys.stdout.flush()
            time.sleep(0.5)

        # Final render
        sys.stdout.write("\033[H")
        sys.stdout.write(build_dashboard_screen())
        sys.stdout.flush()
        time.sleep(1.0)
    finally:
        # Restore terminal — order matters
        sys.stdout.write("\033[?25h")    # show cursor
        sys.stdout.write("\033[?1049l")  # leave alt screen
        sys.stdout.write("\033[0m")      # reset colors/attrs
        sys.stdout.flush()
        # Restore saved terminal settings first
        if _old_term is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _old_term)
            except Exception:
                pass
        # Always run stty sane to fix any remaining issues (raw mode, echo, etc.)
        import subprocess
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
        except Exception:
            pass
        sys.stdout.write("\033c")  # full terminal reset (RIS)
        sys.stdout.flush()

    _db_save_session()
    _cleanup_state_file()
    _print_summary()


if __name__ == "__main__":
    main()

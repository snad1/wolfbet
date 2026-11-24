#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║   🐺  WolfBet AutoBot v2.31.0 : Live Dashboard         ║
║   Auto-betting engine with live TUI & session logging   ║
╚══════════════════════════════════════════════════════════╝
"""

import os, sys, json, re, time, sqlite3, threading, signal, random, string, argparse, logging, hashlib, csv, queue, math  # noqa: E401
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, List
from logging.handlers import TimedRotatingFileHandler
try:
    import readline  # enables arrow keys, history, and line editing in input()
except ImportError:
    pass  # fallback: input() still works, just no line editing

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
    from rich.markup import escape
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
from core.database import cleanup_old_bets, cleanup_live_bets
# v2.28.0: shared helpers extracted for CLI ↔ TG parity + dedup.
from core.validation import finite_float
from core.net_helpers import (
    validate_proxy, mask_proxy, classify_auto_stop_reason, parse_streak_delay,
)
from core.diagnostics import percentile, benchmark_verdict
from core.events import EVENTS_URL_SCHEMES as _EVENTS_URL_SCHEMES
from core.events import EventPublisher
from core.events import validate_events_url as _validate_events_url
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
from core.session_rules import (
    PHASE_BASE, PHASE_RECOVER, PHASE_PRESS, HALF_SAT,
    next_phase, loss_room, floor_room, config_errors,
    parse_amount, parse_flag, parse_multiple, parse_strategy_key, PHASE_STRATEGY_KEYS,
)

# ══════════════════════════════════════════════════════════
#  CONSTANTS (configurable via environment variables)
# ══════════════════════════════════════════════════════════
API_BASE     = os.environ.get("WOLFBET_API_BASE",    "https://wolfbet.com/api")

# Phase 6: game registry, endpoint tables, hash + auto-bet builders live in
# wolfbet/core/games.py: imported at the top of this file. Legacy in-repo
# copies of _wolfbet_hash / _is_auto_game / GAME_ENDPOINTS etc. removed.
#
# NOTE: this CLI's legacy static-hash manual endpoints (dice-manual / limbo)
# now go through the shared "dynamic" hash_spec instead: the two hardcoded
# md5 strings that used to live here weren't valid without a matching nonce
# in any modern WolfBet session anyway (dead code). Both v1.15.7 (TG) and
# v2.15.0 (CLI) already committed to "dynamic" for those endpoints.


# ══════════════════════════════════════════════════════════
#  PHASE 2 helpers: session robustness (ported from tg/engine.py)
# ══════════════════════════════════════════════════════════
# _parse_streak_delay: extracted to core.net_helpers in v2.28.0; alias kept for
# call-site + test compatibility. Same behaviour (empty/malformed → (0, 0.0)).
_parse_streak_delay = parse_streak_delay


def _apply_basebet_pct() -> bool:
    """Recompute base_bet from basebet_pct * current_balance. Returns True if changed."""
    if state.basebet_pct <= 0 or state.current_balance <= 0:
        return False
    # v2.36.0: bet split divides the derived base bet too.
    new_base = round(state.current_balance * state.basebet_pct / _split_bet_divisor(), 8)
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
    # Profit milestone: every X profit units crossed
    if state.milestone_profit > 0 and state.profit > 0:
        threshold = state.milestone_profit
        # crossed a new multiple of threshold since last check?
        crossed = int(state.profit / threshold) * threshold
        if crossed > state._last_profit_milestone:
            with state.lock:
                state._last_profit_milestone = crossed
            return f"{crossed:.8f} profit"
    return None


def martingale_chain_depth(balance: float, base: float, mult: float) -> int:
    """Longest loss run a Martingale chain can pay for: max k with
    base·(mult^k − 1)/(mult − 1) <= balance. 0 when inputs can't form a chain."""
    if balance <= 0 or base <= 0 or mult <= 1:
        return 0
    k = int(math.log(1 + balance * (mult - 1) / base) / math.log(mult))
    # guard float rounding at the boundary
    while k > 0 and base * (mult ** k - 1) / (mult - 1) > balance:
        k -= 1
    return k


def _check_risky_config() -> list:
    """Inspect state.* for combinations known to drain balance quickly.

    Returns a list of human-readable heads-up strings. Never blocks: every
    warning is a hint the user can override. Empty list = clean config.
    Called by step_confirm at wizard exit.
    """
    warnings = []
    sk = state.strategy_key
    mult = state.multiplier
    loss_mult = state.loss_mult

    # Aggressive Martingale
    if sk == "2" and loss_mult >= 2.5:
        warnings.append(
            f"Aggressive Martingale (loss_mult={loss_mult}): a 5-loss streak needs "
            f"~{loss_mult**5:.0f}× base_bet in reserve."
        )
    # Doubling with no loss floor
    if sk in ("2", "7") and state.max_loss is None:
        warnings.append(
            "No `max_loss` set: Martingale-style strategies can drain balance "
            "on a bad streak. Consider `t maxloss <amount>` (or set it in the wizard) "
            "to establish a floor."
        )
    # Streak reset can't fire in the red: the balance caps the chain it can survive
    # v2.35.0: with a lane budget, each lane may only risk its share of the balance.
    _split = _split_budget_on()
    _bank = state.current_balance / state.split_lanes if _split else state.current_balance
    depth = martingale_chain_depth(_bank, state.base_bet, loss_mult)
    if sk in ("2", "7") and state.streak_reset_losses and state.max_loss is None and depth:
        warnings.append(
            f"Streak reset with no `max_loss`: "
            f"{'lane share' if _split else 'balance'} "
            f"covers a {depth}-loss chain. "
            f"Loss {depth + 1} in a row ends the session with insufficient balance."
        )
    # Anti-Martingale without profit lock-in
    if sk == "3" and state.max_profit is None:
        warnings.append(
            "Anti-Martingale grows bets on wins with no lock-in. "
            "Consider `t maxprofit <amount>` to bank a target."
        )
    # Base bet vs balance
    if state.current_balance > 0 and state.base_bet > 0.01 * state.current_balance:
        pct = state.base_bet / state.current_balance * 100
        warnings.append(
            f"Base bet is {pct:.1f}% of current balance ({state.base_bet:.8f} of "
            f"{state.current_balance:.8f}): small bankrolls drain in ~7 losses at this size."
        )
    # High multiplier
    if mult > 10:
        win_pct = 99.0 / mult
        warnings.append(
            f"Multiplier {mult}x → win chance ≈ {win_pct:.1f}%. "
            "Most sessions at this multiplier end in the red."
        )
    # Recurring runaway
    if state.recurring and state.max_profit is None and state.max_bets is None:
        rd = getattr(state, "recurring_delay", 0.0) or 0.0
        gap = f"delay {rd:.0f}s between cycles" if rd > 0 else "back-to-back (no delay)"
        warnings.append(
            "Recurring restart is on with no max_profit and no max_bets: "
            f"sessions could restart indefinitely ({gap}). "
            "Consider setting at least one cap."
        )
    # Rule-based without cap
    if sk == "8" and state.max_bets is None:
        warnings.append(
            "Custom rules (Strategy 8) with no `max_bets` cap can loop. "
            "Consider setting `max_bets` for safety."
        )
    # Aggressive basebet_pct
    if state.basebet_pct >= 0.05:
        warnings.append(
            f"basebet_pct = {state.basebet_pct*100:.1f}% of balance: "
            "the base bet grows quickly on winning streaks."
        )
    # Pipeline on a strategy that can't safely pipeline
    if state.pipeline_depth > 1 and sk != "1":
        warnings.append(
            f"pipeline_depth={state.pipeline_depth} but strategy is {state.strategy}: "
            "will be forced to 1 (Flat-only). Set strategy 1 (Flat) to actually pipeline."
        )
    if state.pipeline_depth > 1 and is_auto_game(state.game):
        warnings.append(
            f"pipeline_depth={state.pipeline_depth} but game is {state.game}: "
            "auto-bet is server-side single-flight, will be forced to 1."
        )
    # Proxy on cleartext channel: credentials on the wire (non-blocking)
    if state.proxy and state.proxy.startswith("http://"):
        warnings.append(
            f"proxy scheme is http:// ({_mask_proxy(state.proxy)}): "
            "credentials + traffic travel unencrypted between you and the proxy. "
            "Prefer https:// or socks5:// when possible."
        )
    return warnings


STREAK_RESET_REASON = "Loss streak reset"
LOSS_CAP_REASON = "Loss cap"
BALANCE_GOAL_REASON = "Balance goal reached"


def _restart_delay_for(reason: str) -> float:
    """Pause before the next cycle: streak reset has its own delay."""
    if reason.startswith(STREAK_RESET_REASON):
        return state.streak_reset_delay
    return state.recurring_delay


def _reset_session_for_recurring(delay: Optional[float] = None, reason: str = "") -> None:
    """Reset per-session counters while preserving config. Starts a new DB session row."""
    if delay is None:
        delay = state.recurring_delay
    _emit_lifecycle("session_close", _session_close_data())
    _emit_lifecycle("cycle", {"from_session_id": state.session_id, "reason": reason,
                              "delay_s": delay})
    # v2.28.4: freeze the "bet end" timestamp BEFORE the sleep so uptime +
    # DB ended_at reflect the real betting window (not "5 min bet + 1 h pause"
    # lumped together). Cleared just before we set the new session_start.
    _bet_end = time.time()
    with state.lock:
        state.session_bet_ended_at = _bet_end
        state.session_pause_started_at = _bet_end
        state.session_pause_total = delay
    # Recurring delay: pause BEFORE the next cycle so the user has time to
    # intervene, the server settles, and the dashboard shows why the bot is idle.
    if delay > 0:
        with state.lock:
            state.status = f"Recurring pause {delay:.0f}s before next cycle..."
        logger.info("RECURRING pause %.0fs before next cycle", delay)
        time.sleep(delay)
    with state.lock:
        # Clear frozen timestamps as we enter the new session.
        state.session_bet_ended_at = None
        state.session_pause_started_at = None
        state.session_id     = _db_start_session()
        state.session_start  = time.time()
        state.total_bets     = 0
        state.wins           = 0
        state.losses         = 0
        # v2.35.0: lane equity carries the lane's result across its sessions.
        state.lane_run_profit += state.profit
        # v2.37.0: the run total keeps the finished session's result.
        state.run_profit_done += state.profit
        state.profit         = 0.0
        state.wagered        = 0.0
        state.current_streak = 0
        state.max_win_streak = 0
        state.max_loss_streak = 0
        state.last_broken_loss_streak = 0
        state.highest_bet    = 0.0
        state.highest_win    = 0.0
        state.biggest_loss   = 0.0
        state.recent_bets.clear()
        state.profit_history.clear()
        state.start_balance  = state.current_balance
        state.highest_balance = state.current_balance
        state.lowest_balance  = state.current_balance
        state.current_bet    = state.base_bet
        state.hybrid_phase   = PHASE_BASE
        state.hybrid_switches = 0
        state.dalembert_unit = 0
        state.paroli_count   = 0
        # Reset auto-bet UUID so betting_loop fires a fresh /auto/start next tick.
        state._auto_uuid     = None
        state._auto_restart_pending = False
        state._last_profit_milestone = 0.0
        # v2.30.1: reset the profit-increment milestone so each cycle hunts
        # a fresh target. Base_bet is deliberately preserved above (bumps
        # stack across cycles). Without this, cycle 2+ would need to accumulate
        # past cycle 1's high-water mark before any bump could fire.
        # v2.37.0: run scope keeps its milestone across sessions.
        if state.profit_threshold and state.profit_increment_scope != "run":
            state.next_profit_milestone = state.profit_threshold
        state.consecutive_errors = 0
        state.backoff_delay  = 1.0
        state.last_error     = ""
        state.status         = f"Recurring restart: session #{state.session_id}"
    logger.info("RECURRING RESTART complete: new session #%d, base_bet=%.8f, balance=%.8f",
                state.session_id, state.base_bet, state.current_balance)


# ══════════════════════════════════════════════════════════
#  PHASE 4b: runtime tweak grammar (mirrors tg/handlers.cmd_tweak)
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
    "piscope":     "profit_increment_scope",
    "pscope":      "profit_increment_scope",
    "basebet":     "base_bet",
    "lossmult":    "loss_mult",
    "winmult":     "win_mult",
    "pipeline":    "pipeline_depth",
    "recdelay":    "recurring_delay",
    "recurringdelay": "recurring_delay",
    "srloss":      "streak_reset_losses",
    "srdelay":     "streak_reset_delay",
    "purgedays":   "purge_days",
    "dbmode":      "db_write_mode",
    "dbwritemode": "db_write_mode",
    "strategy":    "strategy_key",
}
_SESSION_RULE_KEYS = (
    "hard_cap", "restart_after_loss", "balance_goal",
    "hybrid_recover_at", "hybrid_recover_strategy", "hybrid_recover_bet",
    "hybrid_press_at", "hybrid_press_floor", "hybrid_press_strategy", "hybrid_press_bet",
)
_TWEAK_PARAMS = {
    "bet_delay", "basebet_pct",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold", "profit_increment_scope",
    "recurring", "recurring_delay",
    "streak_reset_losses", "streak_reset_delay",
    "base_bet", "multiplier", "loss_mult", "win_mult",
    "game", "strategy_key",
    "currency", "rule", "delay_martin_threshold", "custom_rules_text",
    "pipeline_depth",
    "proxy", "purge_days",
    "db_write_mode",
    "events_enabled", "events_url",
    "key_ref", "proxy_ref",
    *_SESSION_RULE_KEYS,
}
_TWEAK_OFF_VALUES = (None, "", "off", "none", "0", "disable", "false", "no", "n")


def _derive_bet_value(multiplier: float, rule: str) -> float:
    """Canonical WolfBet win-chance derivation. 99% RTP (1% house edge).

    v2.30.9: single source of truth used by the wizard, `load_config`,
    and `_apply_mutation`. Was inline in three places and silently stale
    in any config where `wolfctl config-set multiplier=...` ran without the
    wizard: server returned HTTP 422 "Incorrect win chance given" on
    /auto/start (top cause of the 2026-08-01 outage on vmi3142201).
    """
    if multiplier <= 0:
        return 50.74  # safe default (matches the wizard's fallback at mult=2.01)
    wc = 99.0 / multiplier
    if rule == "over":
        return round(100.0 - wc - 0.01, 2)
    return round(wc, 2)


def _finite_num(name: str, v, cast=float):
    """Parse a user-supplied limit, rejecting nan and inf before it reaches a stop check."""
    number = finite_float(name, v)
    if cast is int and not number.is_integer():
        raise ValueError(f"{name} must be a whole number")
    return cast(number)


def _apply_mutation(changes: dict) -> list:
    """Apply {param → value} mutations to state.*: returns human status list.

    Mirrors tg/engine.py:1274-1387 for CLI parity. Raises ValueError on malformed
    numeric input; caller is responsible for surfacing that cleanly."""
    msgs = []

    def _is_off(v):
        return isinstance(v, str) and v.lower() in _TWEAK_OFF_VALUES or v is None

    def _clamped(name: str, raw: float, lo: float, hi: float) -> float:
        """v2.30.6: clamp raw into [lo, hi]; append a warning to msgs when
        clamped. Was three copy-paste sites that silently swallowed
        out-of-range input; now the user sees exactly what happened."""
        clamped = max(lo, min(hi, raw))
        if clamped != raw:
            msgs.append(
                f"⚠ {name} clamped from {raw:g} to {clamped:g} "
                f"(allowed: {lo:g}..{hi:g})"
            )
        return clamped

    # ── Instant (no restart) ──
    if "bet_delay" in changes:
        state.bet_delay = float(changes["bet_delay"])
        msgs.append(f"Delay: {state.bet_delay:.3f}s")

    if "basebet_pct" in changes:
        v = changes["basebet_pct"]
        state.basebet_pct = 0.0 if _is_off(v) else _finite_num("Base bet %", v)
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

    # Stop conditions: instant
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
                setattr(state, attr, _finite_num(label, v, cast))
                msgs.append(f"{label}: {getattr(state, attr)}")

    # Milestone thresholds: instant
    for attr, label in [("milestone_bets",   "Milestone bets"),
                        ("milestone_wins",   "Milestone wins"),
                        ("milestone_losses", "Milestone losses")]:
        if attr in changes:
            v = changes[attr]
            setattr(state, attr, 0 if _is_off(v) else _finite_num(label, v, int))
            msgs.append(f"{label}: {getattr(state, attr) or 'off'}")
    if "milestone_profit" in changes:
        v = changes["milestone_profit"]
        state.milestone_profit = 0.0 if _is_off(v) else _finite_num("Milestone profit", v)
        msgs.append(f"Milestone profit: {state.milestone_profit:.8f}" if state.milestone_profit else "Milestone profit: off")

    # Profit increment: instant
    if "profit_increment" in changes:
        v = changes["profit_increment"]
        state.profit_increment = None if _is_off(v) else _finite_num("Profit increment", v)
        if state.profit_increment is not None and state.profit_increment <= 0:
            state.profit_increment = None     # v2.37.0: 0 / 0.0 means off
        msgs.append(f"Profit increment: {state.profit_increment:.8f}" if state.profit_increment else "Profit increment: off")
    if "profit_threshold" in changes:
        v = changes["profit_threshold"]
        state.profit_threshold = None if _is_off(v) else _finite_num("Profit threshold", v)
        if state.profit_threshold is not None and state.profit_threshold <= 0:
            state.profit_threshold = None     # v2.37.0: 0 / 0.0 means off
        if state.profit_threshold:
            # v2.37.0: run scope: next milestone after the bumps already made.
            _done = state.run_bumps if state.profit_increment_scope == "run" else 0
            state.next_profit_milestone = (_done + 1) * state.profit_threshold
        msgs.append(f"Profit threshold: {state.profit_threshold:.8f}" if state.profit_threshold else "Profit threshold: off")
    if "profit_increment_scope" in changes:
        v = str(changes["profit_increment_scope"] or "").strip().lower()
        if v not in PROFIT_INCREMENT_SCOPES:
            raise ValueError(f"profit_increment_scope must be session or run, got {v!r}")
        state.profit_increment_scope = v
        msgs.append(f"Profit increment scope: {v} (applies on next start)")

    # Recurring: instant boolean
    if "recurring" in changes:
        v = str(changes["recurring"]).lower()
        state.recurring = v in ("on", "true", "yes", "y", "1")
        msgs.append(f"Recurring: {'on' if state.recurring else 'off'}")

    # Recurring delay: pause between cycles in seconds (0 = back-to-back).
    if "recurring_delay" in changes:
        v = changes["recurring_delay"]
        if _is_off(v):
            state.recurring_delay = 0.0
        else:
            try:
                # v2.30.6: cap raised from 3600 (arbitrary TG mirror) to 86400
                # (24 h). Silent clamp swapped for visible warning via _clamped.
                state.recurring_delay = _clamped(
                    "recurring_delay", _finite_num("Recurring delay", v),
                    *_rule_bounds("recurring_delay"))
            except (TypeError, ValueError):
                raise ValueError(f"recurring_delay must be a number 0-86400, got {v!r}")
        msgs.append(f"Recurring delay: {state.recurring_delay:.0f}s"
                    if state.recurring_delay > 0 else "Recurring delay: off")

    # Streak reset: end + restart the session on the first win after
    # N+ consecutive losses (only when session profit >= 0).
    if "streak_reset_losses" in changes:
        v = changes["streak_reset_losses"]
        if _is_off(v):
            state.streak_reset_losses = None
        else:
            try:
                n = _finite_num("Streak reset", v, int)
            except (TypeError, ValueError):
                raise ValueError(f"streak_reset_losses must be an integer >= 1, got {v!r}")
            if n < 1:
                raise ValueError(f"streak_reset_losses must be an integer >= 1, got {v!r}")
            state.streak_reset_losses = n
        msgs.append(f"Streak reset: after {state.streak_reset_losses} losses"
                    if state.streak_reset_losses else "Streak reset: off")

    if "streak_reset_delay" in changes:
        v = changes["streak_reset_delay"]
        if _is_off(v):
            state.streak_reset_delay = 0.0
        else:
            try:
                state.streak_reset_delay = _clamped(
                    "streak_reset_delay", _finite_num("Streak reset delay", v),
                    *_rule_bounds("streak_reset_delay"))
            except (TypeError, ValueError):
                raise ValueError(f"streak_reset_delay must be a number 0-86400, got {v!r}")
        msgs.append(f"Streak reset delay: {state.streak_reset_delay:.0f}s"
                    if state.streak_reset_delay > 0 else "Streak reset delay: off")

    # Proxy: validated + applied to the shared HTTP session.
    if "proxy" in changes:
        try:
            state.proxy = _validate_proxy(changes["proxy"])
        except ValueError as e:
            raise ValueError(f"proxy: {e}")
        _apply_proxy_to_session()
        msgs.append(f"Proxy: {_mask_proxy(state.proxy)}")

    # Lane accounts: a vault key and a vault proxy, by id; each must exist and be of its kind.
    for ref_key, kind in (("key_ref", "key"), ("proxy_ref", "proxy")):
        if ref_key not in changes:
            continue
        ref = str(changes[ref_key] or "").strip().lower()
        if ref_key == "proxy_ref" and _is_off(ref):
            state.proxy_ref = None
            msgs.append("Proxy from the vault: off (restart to apply)")
            continue
        info = _vault_info(ref)
        if info is None or info.kind != kind:
            raise ValueError(f"no {kind} named {ref} in the vault")
        setattr(state, ref_key, ref)
        msgs.append(f"{'Key' if kind == 'key' else 'Proxy'}: {ref} (restart to apply)")

    # Purge days: bet-row retention window (1-30). "off" resets to default 3;
    # numeric values (including 0) clamp to [1, 30]: 0 days is nonsensical, so it maps to 1.
    if "purge_days" in changes:
        v = changes["purge_days"]
        if isinstance(v, str) and v.strip().lower() in ("off", "none", "false", "disable", "no"):
            state.purge_days = 3
        else:
            try:
                state.purge_days = int(_clamped("purge_days", _finite_num("Purge days", v),
                                                *_rule_bounds("purge_days")))
            except (TypeError, ValueError):
                raise ValueError(f"purge_days must be an integer 1-30, got {v!r}")
        msgs.append(f"Purge days: {state.purge_days}")

    # DB write mode: "async" (batched, tuned) or "sync" (legacy, per-bet fsync).
    if "db_write_mode" in changes:
        v = str(changes["db_write_mode"]).strip().lower()
        if v not in _CONFIG_VALUE_RULES["db_write_mode"]["choices"]:
            raise ValueError(f"db_write_mode must be 'async' or 'sync', got {v!r}")
        state.db_write_mode = v
        msgs.append(f"DB write mode: {state.db_write_mode}")

    if "events_enabled" in changes:
        v = str(changes["events_enabled"]).strip().lower()
        if v in ("on", "true", "yes", "y", "1"):
            state.events_enabled = True
        elif v in ("off", "false", "no", "n", "0"):
            state.events_enabled = False
        else:
            raise ValueError(f"events_enabled must be on or off, got {v!r}")
        msgs.append(f"Events: {'on' if state.events_enabled else 'off'}")

    if "events_url" in changes:
        v = changes["events_url"]
        state.events_url = None if _is_off(v) else _validate_events_url(str(v).strip())
        msgs.append(f"Events URL: {_redact_url(state.events_url)}")

    # Pipeline depth: instant int (dispatcher re-evaluates on next iteration).
    # Guardrail is enforced by _effective_pipeline_depth(); we just clamp to [1, 8].
    if "pipeline_depth" in changes:
        v = changes["pipeline_depth"]
        if _is_off(v):
            state.pipeline_depth = 1
        else:
            try:
                state.pipeline_depth = int(_clamped("pipeline_depth", _finite_num("Pipeline depth", v),
                                                    *_rule_bounds("pipeline_depth")))
            except (TypeError, ValueError):
                raise ValueError(f"pipeline_depth must be an integer 1-8, got {v!r}")
        msgs.append(f"Pipeline depth: {state.pipeline_depth}")

    for attr in ("hard_cap", "restart_after_loss"):
        if attr in changes:
            setattr(state, attr, parse_flag(attr, changes[attr]))
            msgs.append(f"{attr}: {'on' if getattr(state, attr) else 'off'}")
    for attr in ("balance_goal", "hybrid_recover_at", "hybrid_press_at"):
        if attr in changes:
            amount = parse_amount(attr, changes[attr])
            setattr(state, attr, amount)
            msgs.append(f"{attr}: {amount:.8f}" if amount else f"{attr}: off")
    if "hybrid_press_floor" in changes:
        state.hybrid_press_floor = parse_amount("hybrid_press_floor", changes["hybrid_press_floor"]) or 0.0
        msgs.append(f"hybrid_press_floor: {state.hybrid_press_floor:.8f}")
    for attr in ("hybrid_recover_bet", "hybrid_press_bet"):
        if attr in changes:
            setattr(state, attr, parse_multiple(attr, changes[attr]))
            msgs.append(f"{attr}: {getattr(state, attr):g}x base_bet")
    for attr in ("hybrid_recover_strategy", "hybrid_press_strategy"):
        if attr in changes:
            setattr(state, attr, parse_strategy_key(attr, changes[attr]))
            msgs.append(f"{attr}: {STRATEGY_NAMES[getattr(state, attr)]}")

    # ── Strategy-config changes: auto games clear UUID for fresh restart ──
    strategy_touched = False
    if "game" in changes:
        game = str(changes["game"]).strip().lower()
        if game not in GAME_ENDPOINTS:
            raise ValueError(f"game must be one of {', '.join(sorted(GAME_ENDPOINTS))}, "
                             f"got {game!r}")
        if is_auto_game(state.game) and state._auto_uuid:
            with state.lock:
                state._auto_uuid = None
            msgs.append("Auto-bet UUID cleared - restarts on next bet")
        state.game = game
        msgs.append(f"Game: {GAME_LABELS.get(game, game)} (restart to apply)")
    if "strategy_key" in changes:
        key = str(changes["strategy_key"]).strip()
        if key not in STRATEGY_NAMES:
            raise ValueError(f"strategy_key must be one of {', '.join(sorted(STRATEGY_NAMES))}, "
                             f"got {key!r}")
        state.strategy_key = key
        state.strategy = STRATEGY_NAMES[key]
        state.dalembert_unit = 0
        state.paroli_count = 0
        msgs.append(f"Strategy: {state.strategy} (restart to apply)")
        strategy_touched = True
    if "currency" in changes:
        currency = str(changes["currency"]).strip().lower()
        if currency not in CURRENCIES:
            raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}, got {currency!r}")
        state.currency = currency
        msgs.append(f"Currency: {currency.upper()} (restart to apply)")
        strategy_touched = True
    if "rule" in changes:
        rule = str(changes["rule"]).strip().lower()
        if rule not in DICE_RULES:
            raise ValueError(f"rule must be one of {', '.join(DICE_RULES)}, got {rule!r}")
        state.rule = rule
        msgs.append(f"Rule: {rule} (restart to apply)")
        if "multiplier" not in changes and "bet_value" not in changes:
            state.bet_value = _derive_bet_value(state.multiplier, rule)
            msgs.append(f"bet_value re-derived: {state.bet_value:.2f}")
        strategy_touched = True
    if "delay_martin_threshold" in changes:
        threshold = _finite_num("Delay Martingale threshold", changes["delay_martin_threshold"], int)
        if threshold < 1:
            raise ValueError("Delay Martingale threshold must be at least 1")
        state.delay_martin_threshold = threshold
        msgs.append(f"Delay Martingale threshold: {threshold}")
        strategy_touched = True
    if "custom_rules_text" in changes:
        raw = changes["custom_rules_text"]
        text = "" if _is_off(raw) else str(raw).strip()
        if len(text) > RULES_MAX_CHARS:
            raise ValueError(f"Custom rules must be at most {RULES_MAX_CHARS} characters")
        rules = _load_rules_from_text(text)
        if text and not rules:
            raise ValueError("Custom rules: no valid rule found")
        state.custom_rules_text, state.custom_rules = text, rules
        msgs.append(f"Custom rules: {len(rules)} rule(s)")
        strategy_touched = True
    if "base_bet" in changes:
        state.base_bet = float(changes["base_bet"])
        state.current_bet = state.base_bet
        msgs.append(f"Base bet: {state.base_bet:.8f}")
        strategy_touched = True
    if "multiplier" in changes:
        state.multiplier = float(changes["multiplier"])
        msgs.append(f"Multiplier: {state.multiplier:.2f}")
        # v2.30.9: re-derive bet_value to match new multiplier (was silently
        # stale, top cause of HTTP 422 "Incorrect win chance given" on
        # /auto/start). Explicit bet_value override in the same call wins.
        if "bet_value" not in changes and state.rule in ("over", "under"):
            old_bv = state.bet_value
            state.bet_value = _derive_bet_value(state.multiplier, state.rule)
            if abs(old_bv - state.bet_value) > 0.01:
                msgs.append(
                    f"bet_value re-derived: {state.bet_value:.2f} "
                    f"(was {old_bv:.2f}; from multiplier={state.multiplier} rule={state.rule})"
                )
        strategy_touched = True
    # v2.30.9: explicit bet_value override (advanced; normally derived from
    # multiplier+rule). Apply last so the operator's value wins over the
    # auto-derive above.
    if "bet_value" in changes:
        state.bet_value = float(changes["bet_value"])
        msgs.append(f"Bet value: {state.bet_value:.2f}")
        strategy_touched = True
    if "loss_mult" in changes:
        state.loss_mult = float(changes["loss_mult"])
        msgs.append(f"Loss mult: {state.loss_mult:.2f}")
        strategy_touched = True
    if "win_mult" in changes:
        state.win_mult = float(changes["win_mult"])
        msgs.append(f"Win mult: {state.win_mult:.2f}")
        strategy_touched = True
    if any(key.startswith("hybrid_") for key in changes):
        strategy_touched = True

    if strategy_touched and is_auto_game(state.game) and state._auto_uuid:
        with state.lock:
            state._auto_uuid = None
        msgs.append("Auto-bet UUID cleared: restarts on next bet")

    msgs.extend(f"⚠ {problem}" for problem in _session_rule_errors())
    return msgs


def _loss_restart_allowed(reason: str) -> bool:
    """True when a session that ended in the red may start a new one.

    Needs restart_after_loss with recurring, the pre-bet cap, max_loss and a
    balance floor, a balance still above that floor, and a session that ended
    at its loss cap. Every other loss stop ends the run.
    """
    if not (state.restart_after_loss and state.recurring and _hard_cap_on()):
        return False
    if state.max_loss is None or state.stop_on_balance is None:
        return False
    if state.current_balance <= state.stop_on_balance:
        return False
    return reason.startswith(("Max loss hit", LOSS_CAP_REASON))


def _should_recurring_restart(reason: str) -> bool:
    """Whether a stopped session starts a new one.

    In profit: streak reset, or recurring after a profit target, max bets or
    max wins. In the red: only the guarded loss restart. Insufficient balance
    and the balance floor never restart.
    """
    if state.profit < 0:
        return _loss_restart_allowed(reason)
    # Streak reset is its own restart rule: independent of the recurring flag.
    if reason.startswith(STREAK_RESET_REASON):
        return bool(state.streak_reset_losses)
    if not state.recurring:
        return False
    # Positive whitelist: only these three reasons trigger recurring:
    #   Profit target reached | Max bets reached | Max wins reached
    safe_prefixes = ("Profit target", "Max bets", "Max wins")
    return any(reason.startswith(p) for p in safe_prefixes)


# ══════════════════════════════════════════════════════════
#  PHASE 3 metrics: observability helpers (ported from tg/engine.py:1487-1508)
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


def _compute_api_p99() -> float:
    """99th percentile API time in ms from the last-500-sample window (0 when empty)."""
    if not state._api_samples:
        return 0.0
    ordered = sorted(state._api_samples)
    idx = max(0, min(len(ordered) - 1, int(0.99 * len(ordered))))
    return ordered[idx]


def _compute_on_target_pct() -> Optional[float]:
    """Fraction of bets whose API ≤ bet_delay*1000; None when delay disabled / no samples."""
    if state.bet_delay <= 0 or state._api_ms_count == 0:
        return None
    return (state._on_target_count / state._api_ms_count) * 100


def _compute_api_rolling_avg() -> float:
    """Mean API ms from the last-N-sample deque (0 when empty).

    v2.30.3: same window as p50/p99. Prevents cold-start samples from
    biasing the mean over long sessions.
    """
    if not state._api_samples:
        return 0.0
    return sum(state._api_samples) / len(state._api_samples)


def _compute_cycle_p50() -> float:
    """Median per-iteration cycle time in ms from the rolling window (0 when empty).

    v2.30.4: replaces the α=0.1 EMA. Same window semantics as api_p50, so
    overhead = cycle_p50 − api_p50 stays algebraically consistent and stable.
    """
    if not state._cycle_samples:
        return 0.0
    ordered = sorted(state._cycle_samples)
    return ordered[len(ordered) // 2]


def _compute_cycle_p99() -> float:
    """99th-percentile per-iteration cycle time in ms (0 when empty)."""
    if not state._cycle_samples:
        return 0.0
    ordered = sorted(state._cycle_samples)
    idx = max(0, min(len(ordered) - 1, int(0.99 * len(ordered))))
    return ordered[idx]


def _compute_cycle_mean() -> float:
    """Arithmetic mean per-iteration cycle time in ms (0 when empty).

    v2.30.5: symmetrical with _compute_api_rolling_avg so Recent BPS and
    Theoretical BPS both derive from mean(latency). Little's-Law-consistent:
    throughput ≈ 1 / mean_latency. Median (cycle_p50) stays available in the
    display for tail-jitter visibility but no longer drives the ratio.
    """
    if not state._cycle_samples:
        return 0.0
    return sum(state._cycle_samples) / len(state._cycle_samples)


def _compute_recent_bps() -> float:
    """Windowed BPS from cycle_mean: same statistic as theoretical_max_bps.

    v2.30.5: was cycle_p50 (median). On right-skewed cycle distributions
    (a few slow iterations from GC / retention / /auto/start handshakes)
    median < mean, so Recent > Theoretical fired ">100% efficiency" as the
    default reading rather than a rare drift signal. Both sides now use
    the mean; efficiency is a real ratio.
    """
    cycle_mean = _compute_cycle_mean()
    return 1000.0 / cycle_mean if cycle_mean > 0 else 0.0


def _compute_theoretical_max_bps() -> float:
    """Ceiling bps = 1 / max(bet_delay, api_rolling_avg). 0 when both are 0.

    v2.30.3: was all-time mean; now rolling to match p50/p99 window semantics.
    """
    api_avg_s = _compute_api_rolling_avg() / 1000.0
    floor = max(state.bet_delay, api_avg_s)
    return (1.0 / floor) if floor > 0 else 0.0


def _compute_efficiency_pct() -> float:
    """Recent bps as % of theoretical max (capped at 100). 0 when no ceiling yet.

    v2.30.4: uses _compute_recent_bps (windowed) instead of session-wide
    bets_per_second so the ratio stays algebraically consistent with the
    same window feeding theoretical_max_bps. Session-wide actual can exceed
    the windowed ceiling when api_avg drifts up momentarily: that used
    to render as impossible ">100% efficiency" or clamp-and-hide.
    """
    tmax = _compute_theoretical_max_bps()
    if tmax <= 0:
        return 0.0
    recent = _compute_recent_bps()
    if recent <= 0:
        # Cycle window hasn't populated yet: fall back to session-wide
        # so early-session status isn't blank.
        recent = state.bets_per_second
    return min(100.0, recent / tmax * 100)


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
    """Push one per-iteration cycle observation; expose p50 + p99 via helpers.

    v2.30.4: replaces the α=0.1 EMA that held onto outliers for 5-10 samples
    (a single 400 ms iteration would bump displayed cycle by ~40 ms and hold).
    Now uses a rolling deque, mirroring _api_samples. Median (cycle_p50) is
    the stable "typical" cycle; p99 shows tail jitter separately.
    """
    with state.lock:
        state._cycle_samples.append(cycle_observed_ms)
        # _cycle_ms aliased to cycle_p50 for backwards state-file compat -
        # existing readers see a stable median instead of a spiky EMA.
        state._cycle_ms = _compute_cycle_p50()
        api_ref = _compute_api_p50() if state._api_samples else state._last_api_ms
        state._cycle_overhead_ms = max(0.0, state._cycle_ms - api_ref - state.bet_delay * 1000)
DB_PATH      = os.environ.get("WOLFBET_DB_PATH",     os.path.expanduser("~/.wolfbet_autobot.db"))
CONFIG_PATH  = os.environ.get("WOLFBET_CONFIG_PATH",  os.path.expanduser("~/.wolfbet_autobot.json"))
STATE_PATH   = os.environ.get("WOLFBET_STATE_PATH",   os.path.expanduser("~/.wolfbet_autobot_live.json"))
PID_PATH     = os.environ.get("WOLFBET_PID_PATH",     os.path.expanduser("~/.wolfbet_autobot.pid"))
_BASE_STATE_PATH, _BASE_PID_PATH = STATE_PATH, PID_PATH  # v2.35.0: lane 1 paths
# v2.37.0: run-wide profit increment tracking (one file per lane)
RUN_PATH     = os.environ.get("WOLFBET_RUN_PATH",     os.path.expanduser("~/.wolfbet_run.json"))
PRESET_PATH  = os.environ.get("WOLFBET_PRESET_PATH",  os.path.expanduser("~/.wolfbet_presets.json"))
LOG_DIR      = os.environ.get("WOLFBET_LOG_DIR",      os.path.expanduser("~/.wolfbet_logs"))
VERSION      = "2.58.0"
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

# ══════════════════════════════════════════════════════════
#  PARALLEL LANES  (v2.35.0)
# ══════════════════════════════════════════════════════════
# A lane is one bot process with its own session, Martingale chain, counters,
# stops and pauses. Lane 1 = the classic single bot (paths unchanged). Lanes
# 2..N run side by side, share the DB + config file, and each may only use
# balance ÷ lanes (split budget).
LANE_ID_MAX = 99
LANES_MAX_DEFAULT = 10
AUTOSTART_LOCK_PATH = os.environ.get(
    "WOLFBET_AUTOSTART_LOCK", os.path.expanduser("~/.wolfbet_autostart.lock"))
# Every setting: how a change reaches running lanes, and whether one lane may override it.
#   live    - a SIGHUP re-reads it into each running lane; the session keeps going.
#   lane    - read at start: restart the lanes using the changed value.
#   service - shared by every lane and read at start: restart every lane.
# Shared settings (False) are the raw key and the storage and publishing settings; a lane
# picks its own account through key_ref, currency and proxy / proxy_ref.
_SETTINGS = {
    "api_key": ("service", False),
    "balance_goal": ("lane", True),
    "base_bet": ("live", True),
    "basebet_pct": ("live", True),
    "bet_delay": ("live", True),
    "currency": ("lane", True),
    "custom_rules_text": ("live", True),
    "db_write_mode": ("live", False),
    "delay_martin_threshold": ("service", False),
    "events_enabled": ("live", False),
    "events_url": ("live", False),
    "game": ("lane", True),
    "hard_cap": ("lane", True),
    "hybrid_press_at": ("lane", True),
    "hybrid_press_bet": ("lane", True),
    "hybrid_press_floor": ("lane", True),
    "hybrid_press_strategy": ("lane", True),
    "hybrid_recover_at": ("lane", True),
    "hybrid_recover_bet": ("lane", True),
    "hybrid_recover_strategy": ("lane", True),
    "key_ref": ("lane", True),
    "loss_mult": ("lane", True),
    "max_bets": ("live", True),
    "max_loss": ("live", True),
    "max_profit": ("live", True),
    "max_wins": ("live", True),
    "milestone_bets": ("live", True),
    "milestone_losses": ("live", True),
    "milestone_profit": ("live", True),
    "milestone_wins": ("live", True),
    "multiplier": ("lane", True),
    "pipeline_depth": ("live", False),
    "profit_increment": ("live", True),
    "profit_increment_scope": ("lane", True),
    "profit_threshold": ("live", True),
    "proxy": ("live", True),
    "proxy_ref": ("lane", True),
    "purge_days": ("live", False),
    "recurring": ("live", True),
    "recurring_delay": ("live", True),
    "restart_after_loss": ("lane", True),
    "rule": ("lane", True),
    "stop_on_balance": ("live", True),
    "strategy_key": ("lane", True),
    "streak_delay_bets": ("live", True),
    "streak_delay_loss": ("live", True),
    "streak_delay_win": ("live", True),
    "streak_reset_delay": ("live", True),
    "streak_reset_losses": ("live", True),
    "streakbet_loss": ("live", True),
    "win_mult": ("lane", True),
}
_LIVE_RELOAD_KEYS = frozenset(k for k, (apply, _) in _SETTINGS.items() if apply == "live")
_LANE_FORBIDDEN_KEYS = frozenset(k for k, (_, per_lane) in _SETTINGS.items() if not per_lane)


def _restart_only(keys) -> list:
    """The keys among `keys` that a running lane picks up only when it restarts."""
    return sorted(k for k in keys if k not in _LIVE_RELOAD_KEYS)


def _lane_path(path: str, lane: int) -> str:
    """'~/.wolfbet_autobot_live.json' + lane 2 → '~/.wolfbet_autobot_live.lane2.json'."""
    if lane <= 1:
        return path
    root, ext = os.path.splitext(path)
    return f"{root}.lane{lane}{ext}"


def _lane_file_ids(path: str) -> list:
    """Lane ids that have a file next to `path` (lane 1 = `path`, lane K = its .laneK twin)."""
    import glob
    root, ext = os.path.splitext(path)
    ids = [1] if os.path.exists(path) else []
    for f in glob.glob(f"{glob.escape(root)}.lane*{glob.escape(ext)}"):
        tag = f[len(root) + len(".lane"):len(f) - len(ext)]
        if tag.isdigit():
            ids.append(int(tag))
    return sorted(ids)


LANE_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,32}$")
LANE_NAME_HELP = "lane name must be 1-32 characters: letters, digits, space, _ or -"


def _default_lane_name(lane: int) -> str:
    return f"Lane {lane}"


def _lane_registry(data: dict) -> dict:
    """Every configured lane as {id: {"name", "enabled", "overrides"}}, ids 1..LANE_ID_MAX.

    Reads `lanes.items`; a config written before lanes had names (`lanes.count` plus
    `lanes.overrides`) reads as lanes 1..count enabled, and lanes that only have overrides
    above the count as disabled, so both shapes behave the same.
    """
    block = data.get("lanes") or {}
    items = block.get("items")
    reg = {}
    if isinstance(items, dict):
        for key, entry in items.items():
            try:
                lane = int(key)
            except (TypeError, ValueError):
                continue
            if not 1 <= lane <= LANE_ID_MAX or not isinstance(entry, dict):
                continue
            name = entry.get("name")
            reg[lane] = {
                "name": name if isinstance(name, str) and LANE_NAME_RE.match(name)
                else _default_lane_name(lane),
                "enabled": bool(entry.get("enabled", True)),
                "overrides": dict(entry.get("overrides") or {}),
            }
    else:
        try:
            count = max(1, min(LANES_MAX_DEFAULT, int(block.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        overrides = block.get("overrides") or {}
        ids = set(range(1, count + 1))
        for key in overrides:
            if str(key).isdigit() and 1 <= int(key) <= LANES_MAX_DEFAULT:
                ids.add(int(key))
        for lane in sorted(ids):
            reg[lane] = {"name": _default_lane_name(lane), "enabled": lane <= count,
                         "overrides": dict(overrides.get(str(lane)) or {})}
    reg.setdefault(1, {"name": _default_lane_name(1), "enabled": True, "overrides": {}})
    reg[1]["enabled"] = True
    return reg


def _lanes_max(data: dict) -> int:
    """How many lanes may be enabled at once: the owner-set `lanes.max`, default 10."""
    try:
        return max(1, min(LANE_ID_MAX, int((data.get("lanes") or {}).get("max")
                                           or LANES_MAX_DEFAULT)))
    except (TypeError, ValueError):
        return LANES_MAX_DEFAULT


def _lane_ids(data: dict) -> list:
    """Every registry lane id, enabled or not, in order."""
    return sorted(_lane_registry(data))


def _lane_name(data: dict, lane: int) -> str:
    entry = _lane_registry(data).get(lane)
    return entry["name"] if entry else _default_lane_name(lane)


def _set_lane_log_prefix(lane: int, name: str) -> None:
    """`[L3]`, or `[L3 friend-A]` once the lane has its own name; lane 1 has none until named."""
    named = name != _default_lane_name(lane)
    state.lane_name = name if named else None
    tag = "" if lane <= 1 and not named else f"[L{lane} {name}] " if named else f"[L{lane}] "
    _log_handler.setFormatter(logging.Formatter(
        f"%(asctime)s  %(levelname)-7s  {tag}%(message)s", datefmt="%Y-%m-%d %H:%M:%S"))


def _read_config_raw() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _apply_lane(lane: int) -> None:
    """Switch this process to lane `lane`: state/pid paths + log prefix."""
    global STATE_PATH, PID_PATH
    lane = 1 if lane is None else int(lane)
    if not 1 <= lane <= LANE_ID_MAX:
        raise ValueError(f"--lane must be 1-{LANE_ID_MAX}, got {lane}")
    state.lane = lane
    if lane > 1:
        STATE_PATH = _lane_path(_BASE_STATE_PATH, lane)
        PID_PATH = _lane_path(_BASE_PID_PATH, lane)
    _set_lane_log_prefix(lane, _lane_name(_read_config_raw(), lane))


def _lane_overrides(data: dict, lane: int) -> dict:
    """Per-lane override dict from a raw config dict (lane 1 has none)."""
    if lane <= 1:
        return {}
    ov = (_lane_registry(data).get(lane) or {}).get("overrides") or {}
    return {k: v for k, v in ov.items() if k not in _LANE_FORBIDDEN_KEYS}


def _lanes_count(data: dict) -> int:
    """How many lanes run: the enabled lanes of the registry."""
    return sum(1 for entry in _lane_registry(data).values() if entry["enabled"])


# v2.36.0: how lanes share the money. Default "none": every lane uses the
# full base bet and no budget guard (the user sizes the bets themselves).
#   budget → each lane may risk balance ÷ lanes (stop "Lane budget exceeded")
#   bet    → base bet (and basebet_pct) ÷ lanes
#   both   → budget + bet
LANE_SPLIT_MODES = ("none", "budget", "bet", "both")
_LANE_SPLIT_HELP = {
    "none":   "every lane uses the full base bet, no lane budget",
    "budget": "each lane may risk balance ÷ lanes",
    "bet":    "base bet ÷ lanes",
    "both":   "base bet ÷ lanes and each lane may risk balance ÷ lanes",
}


def _lane_split(data: dict) -> str:
    mode = str((data.get("lanes") or {}).get("split") or "none").strip().lower()
    return mode if mode in LANE_SPLIT_MODES else "none"


def _split_budget_on() -> bool:
    return (getattr(state, "split_lanes", 1) > 1
            and getattr(state, "lane_split", "none") in ("budget", "both"))


def _split_bet_divisor() -> int:
    """Lanes count when the bet split applies, else 1."""
    if (getattr(state, "split_lanes", 1) > 1
            and getattr(state, "lane_split", "none") in ("bet", "both")):
        return state.split_lanes
    return 1


def _lane_account(data: dict, lane: int) -> tuple:
    """(key_ref, currency, proxy, proxy_ref) lane `lane` bets with: base values, then its overrides."""
    merged = {**data, **_lane_overrides(data, lane)}
    return (merged.get("key_ref") or VAULT_KEY_ID, merged.get("currency"),
            merged.get("proxy"), merged.get("proxy_ref"))


def _split_lanes(data: dict, lane: int) -> int:
    """Enabled lanes on the same account (key and currency) as `lane`: a split never pools
    the balances of different accounts."""
    reg = _lane_registry(data)
    mine = _lane_account(data, lane)[:2]
    return sum(1 for k, entry in reg.items()
               if entry["enabled"] and _lane_account(data, k)[:2] == mine) or 1


def _key_lanes(data: dict, lane: int) -> int:
    """Enabled lanes on the same key as `lane`: they share one rate limit, whatever the currency."""
    reg = _lane_registry(data)
    mine = _lane_account(data, lane)[0]
    return sum(1 for k, entry in reg.items()
               if entry["enabled"] and _lane_account(data, k)[0] == mine) or 1


def _lane_config(data: dict, lane: int) -> dict:
    """Base config + this lane's overrides + the bet split (v2.36.0).

    A lane with its own base_bet override keeps that exact value: the user
    already sized it. A bet that would round to 0 stays full (logged).
    """
    ov = _lane_overrides(data, lane)
    merged = {**data, **ov}
    n = _split_lanes(data, lane)
    if (n > 1 and _lane_split(data) in ("bet", "both") and "base_bet" not in ov
            and isinstance(merged.get("base_bet"), (int, float))):
        split = round(merged["base_bet"] / n, 8)
        if split > 0:
            merged["base_bet"] = split
        else:
            logger.warning("lane split: base_bet %.8f ÷ %d rounds to 0: using full bet",
                           merged["base_bet"], n)
    return merged


DICE_RULES = ("over", "under")
RULES_MAX_CHARS = 4000
CURRENCIES  =["btc", "eth", "ltc", "doge", "trx", "bch", "xrp", "usdt", "bnb", "ada", "matic"]

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
    "profit_increment", "profit_threshold", "profit_increment_scope",
    "pipeline_depth", "recurring_delay",
    "proxy", "purge_days",
    "db_write_mode",
    "key_ref", "proxy_ref",
    # v2.28.2: previously missing from _CONFIG_KEYS despite living in
    # _PRESET_KEYS / _CONFIG_SHOW_FIELDS / _TWEAK_PARAMS. Every wolfctl
    # config-set on these fields silently reverted on the next process
    # launch since Phase 2 (v2.16.0). Now honestly persisted.
    "recurring", "basebet_pct",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "streak_reset_losses", "streak_reset_delay",
    "events_enabled", "events_url",
    *_SESSION_RULE_KEYS,
]

def _mask_key(key: str) -> str:
    """Mask API key for safe logging: show first 8 + last 4 chars."""
    if not key or len(key) < 16:
        return "***"
    return f"{key[:8]}...{key[-4:]}"

VAULT_SERVICE = "wolfbet"
VAULT_KEY_ID = "default"
TG_KEY_ID = "telegram"
TG_TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,64}$")


def _vault():
    """This bot's key vault, or None when casino-shared has no vault yet (logged)."""
    try:
        from shared.core.vault import Vault
    except ImportError:
        logger.warning("Key vault unavailable: casino-shared has no vault module (run wolfctl update)")
        return None
    return Vault(VAULT_SERVICE)


def _vault_secret(key_id: str = VAULT_KEY_ID, kind: str = "key") -> Optional[str]:
    """Vault entry `key_id` of `kind`, or None when absent, of another kind or unreadable."""
    vault = _vault()
    if vault is None:
        return None
    from shared.core.vault import VaultError
    try:
        info = vault.info(key_id)
        return vault.secret(key_id) if info is not None and info.kind == kind else None
    except (VaultError, OSError) as e:
        logger.warning("Key vault unavailable: %s", e)
        return None


def _vault_info(key_id: str):
    """What the vault knows about `key_id` (no secret), or None when it is absent or unusable."""
    from shared.core.vault import KEY_ID_RE, VaultError
    vault = _vault()
    if vault is None or not KEY_ID_RE.match(key_id or ""):
        return None
    try:
        return vault.info(key_id)
    except (VaultError, OSError) as e:
        logger.warning("Key vault unavailable: %s", e)
        return None


def _active_api_key(config_key: str, key_ref: str = VAULT_KEY_ID) -> str:
    """The key this run bets with. For `default`: WOLFBET_API_KEY, else the vault, else the
    config's copy. Any other key comes from the vault only, never from another account."""
    if key_ref != VAULT_KEY_ID:
        return _vault_secret(key_ref) or ""
    return os.environ.get("WOLFBET_API_KEY") or _vault_secret() or config_key or ""


def _resolve_lane_secrets(merged: dict) -> dict:
    """Put the vault proxy a lane names with proxy_ref into its proxy (read at start and reload)."""
    ref = merged.get("proxy_ref")
    if not ref:
        return merged
    url = _vault_secret(ref, "proxy")
    if url is None:
        return {**merged, "proxy": None,
                "_account_problem": f"Lane {state.lane}: proxy {ref} is not in the vault"}
    return {**merged, "proxy": url}


def _store_key_in_vault() -> bool:
    """Make the vault's default equal the key in use (lane 1 only). True once it reads back equal."""
    if (getattr(state, "lane", 1) > 1 or not state.api_key
            or getattr(state, "key_ref", VAULT_KEY_ID) != VAULT_KEY_ID):
        return False
    vault = _vault()
    if vault is None:
        return False
    from shared.core.vault import VaultError
    try:
        if vault.secret(VAULT_KEY_ID) != state.api_key:
            vault.put(VAULT_KEY_ID, state.api_key, "WolfBet main key")
            logger.info("API key stored in the vault as %s", VAULT_KEY_ID)
        return vault.secret(VAULT_KEY_ID) == state.api_key
    except (VaultError, OSError) as e:
        logger.warning("Key vault unavailable: %s", e)
        return False


def _sync_vault_key() -> None:
    """Move the key into the vault, then drop the config file's plaintext copy (one backup kept)."""
    if not _store_key_in_vault():
        return
    raw = _read_config_raw()
    if "api_key" not in raw:
        return
    _backup_config_once(".pre-vault", "moving the API key to the vault")
    raw.pop("api_key")
    _write_config_raw(raw)
    logger.info("API key removed from %s; the vault keeps it", CONFIG_PATH)


KEY_SECRET_RE = re.compile(r"^[\x21-\x7e]{8,4096}$")
KEY_SECRET_HELP = "the key must be 8-4096 printable characters with no spaces"


def _key_users(key_id: str, kind: str = "key") -> list:
    """The enabled lanes that bet with vault entry `key_id`: as their key, or their proxy."""
    accounts = _lane_accounts(_read_config_raw())
    at = 0 if kind == "key" else 3
    return [lane for lane, account in accounts.items() if account[at] == key_id]


def _key_row(info) -> dict:
    return {"id": info.key_id, "label": info.label, "kind": info.kind, "last4": info.last4,
            "owner_user_id": info.owner_user_id, "created_at": info.created_at,
            "rotated_at": info.rotated_at, "used_by_lanes": _key_users(info.key_id, info.kind)}


def _read_key_secret(kind: str = "key") -> str:
    """One line of stdin: the key travels there so it never shows in a process list.

    A proxy must be an http, https or socks5 URL; a refusal never repeats what was sent.
    """
    secret = sys.stdin.readline().rstrip("\r\n")
    if not KEY_SECRET_RE.match(secret):
        raise _CliError("invalid_secret", KEY_SECRET_HELP, style="red", exit_code=2)
    if kind == "telegram" and not TG_TOKEN_RE.match(secret):
        raise _CliError("invalid_secret", "That doesn't look like a Telegram bot token.",
                        style="red", exit_code=2)
    if kind == "proxy":
        try:
            ok = _validate_proxy(secret) is not None
        except ValueError:
            ok = False
        if not ok:
            raise _CliError("invalid_secret", "a proxy must be an http://, https:// or socks5:// "
                            "URL", style="red", exit_code=2)
    return secret


def cmd_key(action: str, key_id: Optional[str] = None, label: Optional[str] = None,
            owner: Optional[int] = None, kind: str = "key") -> None:
    """List, add, rotate or delete vault keys as one JSON document; no secret is ever printed."""
    vault = _vault()
    if vault is None:
        _cli_fail(_CliError("vault_unavailable", "The key vault can't be used on this host.",
                            exit_code=1), True)
        return
    from shared.core.vault import KEY_ID_RE, VaultError
    try:
        if action != "list" and not KEY_ID_RE.match(key_id or ""):
            raise _CliError("invalid_argument", "key id must be 1-32 lowercase letters, digits, "
                            "_ or -", style="red", exit_code=2)
        if action == "list":
            _print_json({"keys": [_key_row(i) for i in vault.keys()]})
            return
        if action == "put" and (kind == "telegram") != (key_id == TG_KEY_ID):
            raise _CliError("invalid_argument", f"The Telegram bot token is stored as {TG_KEY_ID}, "
                            "and only there.", style="red", exit_code=2)
        if vault.info(key_id) is None and action != "put":
            raise _CliError("key_unknown", f"No key named {key_id}.", style="red", exit_code=2)
        if action == "delete":
            users = _key_users(key_id, vault.info(key_id).kind)
            if users:
                lanes = ", ".join(str(lane) for lane in users)
                raise _CliError("key_in_use", f"This key is used by lane {lanes}. "
                                "Move the lanes to another key first.", style="red", exit_code=2)
            vault.delete(key_id)
            logger.info("Vault key %s deleted", key_id)
            _print_json({"deleted": key_id})
            return
        if action == "put" and vault.info(key_id) is not None:
            raise _CliError("key_exists", f"A key named {key_id} already exists.",
                            style="red", exit_code=2)
        old = vault.info(key_id)
        kind = old.kind if old else kind
        secret = _read_key_secret(kind)
        info = vault.put(key_id, secret, label or (old.label if old else key_id),
                         owner if action == "put" else (old.owner_user_id if old else None),
                         kind=kind)
        logger.info("Vault key %s %s", key_id, "added" if action == "put" else "rotated")
        note = None
        if action == "rotate":
            note = "Lanes using this key pick up the new one when they restart."
            if kind == "proxy":
                note = "Lanes using this proxy pick up the new one when they restart."
            if key_id == VAULT_KEY_ID and os.environ.get("WOLFBET_API_KEY"):
                note += (" WOLFBET_API_KEY in .env still replaces it when lane 1 starts:"
                         " remove it from .env to keep this key.")
        if kind == "telegram":
            note = "Restart the Telegram bot to use the new token."
        _print_json({"key": _key_row(info), "note": note})
    except VaultError as e:
        logger.warning("Key vault unavailable: %s", e)
        _cli_fail(_CliError("vault_unavailable", "The key vault can't be used on this host.",
                            exit_code=1), True)
    except _CliError as e:
        _cli_fail(e, True)


def save_config():
    if getattr(state, "lane", 1) > 1:
        # v2.35.0: a lane's state holds base + its overrides; writing it back
        # would leak the overrides into the shared base config.
        logger.info("save_config skipped on lane %d (base config is lane 1's)", state.lane)
        return
    data = {k: getattr(state, k) for k in _CONFIG_KEYS}
    if _store_key_in_vault():
        if "api_key" in _read_config_raw():
            _backup_config_once(".pre-vault", "moving the API key to the vault")
        data.pop("api_key")
    # v2.36.0: bet split: an untouched split base bet is saved as the full one.
    _sb = getattr(state, "base_bet_split", None)
    if _sb is not None and state.base_bet == _sb:
        data["base_bet"] = state.base_bet_disk
    # v2.35.0: keep the lanes block (count + per-lane overrides) intact.
    try:
        with open(CONFIG_PATH) as f:
            _old = json.load(f)
        if "lanes" in _old:
            data["lanes"] = _old["lanes"]
    except (OSError, ValueError):
        pass
    # v2.28.2: streak-tuple fields round-trip as "N:X" strings (or null when
    # disabled). Same encoding presets already use: one grammar, no forks.
    for k in _STREAK_TUPLE_KEYS:
        tup = getattr(state, k, None)
        if tup and tup[0] > 0:
            data[k] = f"{tup[0]}:{tup[1]}"
        else:
            data[k] = None
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Config saved (key=%s)", _mask_key(state.api_key))

def load_config() -> bool:
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        for k in _CONFIG_KEYS:
            if k in data:
                setattr(state, k, data[k])
        # v2.28.2: decode streak-tuple fields via parse_streak_delay.
        # Older configs (pre-v2.28.2) lack these keys; the loop silently
        # skips them and defaults from BotState.__init__ apply.
        for k in _STREAK_TUPLE_KEYS:
            if k in data:
                setattr(state, k, parse_streak_delay(data[k]))
        # v2.35.0: lane count + this lane's overrides on top of the base.
        state.lanes_count = _lanes_count(data)
        state.split_lanes = _split_lanes(data, state.lane)
        state.key_lanes = _key_lanes(data, state.lane)
        state.lane_split = _lane_split(data)
        _set_lane_log_prefix(state.lane, _lane_name(data, state.lane))
        merged = _resolve_lane_secrets(_lane_config(data, state.lane))
        # Remember the on-disk base bet vs the split one so save_config never
        # writes the halved value back (it would halve again on each start).
        state.base_bet_disk = data.get("base_bet")
        state.base_bet_split = merged.get("base_bet") if merged.get("base_bet") != data.get("base_bet") else None
        for k, v in merged.items():
            if k in data and data[k] == v:
                continue                 # base value, already applied above
            if k in _STREAK_TUPLE_KEYS:
                setattr(state, k, parse_streak_delay(v))
            elif k in _CONFIG_KEYS:
                setattr(state, k, v)
        # ISO 27001: the key comes from the environment or the vault, a config copy is the fallback.
        state.key_ref = merged.get("key_ref") or VAULT_KEY_ID
        state.proxy_ref = merged.get("proxy_ref") or None
        state.api_key = _active_api_key(data.get("api_key") or "", state.key_ref)
        state.account_problem = merged.get("_account_problem") or (
            None if state.api_key or state.key_ref == VAULT_KEY_ID
            else f"Lane {state.lane}: key {state.key_ref} is not in the vault")
        env_events_url = os.environ.get("WOLFBET_EVENTS_URL")
        if env_events_url and not merged.get("events_url"):
            state.events_url = env_events_url
        secure_path(CONFIG_PATH)
        state.current_bet = state.base_bet
        # init profit milestone
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold
        # parse rules if present
        if state.custom_rules_text:
            state.custom_rules = _load_rules_from_text(state.custom_rules_text)
        # v2.26.0: normalize + apply loaded proxy to the shared session.
        try:
            state.proxy = _validate_proxy(state.proxy)
        except ValueError:
            state.proxy = None
        _apply_proxy_to_session()
        # v2.30.9: silently correct stale bet_value from disk. Was the top
        # cause of HTTP 422 on /auto/start when a previously-saved config
        # had `bet_value` from an older `multiplier` and `wolfctl config-set
        # multiplier=...` never recomputed. CHANGELOG v1262 promised this fix
        # but only the wizard + /bet-self-correct path actually derived it;
        # auto games never hit /bet so no self-correction ever ran. The
        # warning is silent-by-default but logged so we can spot recurrences.
        if state.multiplier > 0 and state.rule in ("over", "under"):
            derived_bv = _derive_bet_value(state.multiplier, state.rule)
            if abs(state.bet_value - derived_bv) > 0.01:
                logger.warning(
                    "bet_value from disk (%.2f) mismatched derived value "
                    "for multiplier=%.2f rule=%s (%.2f): corrected.",
                    state.bet_value, state.multiplier, state.rule, derived_bv)
                state.bet_value = derived_bv
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
    "profit_increment", "profit_threshold", "profit_increment_scope",
    # Phase 4a additions: JSON-native Phase 2 fields:
    "recurring", "basebet_pct",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    # Phase 5b: pipeline depth persists per preset:
    "pipeline_depth",
    # v2.25.3: pause between recurring cycles (TG↔CLI parity):
    "recurring_delay",
    # v2.26.0: proxy + retention (TG↔CLI parity):
    "proxy", "purge_days",
    # v2.27.0: async bet writer mode:
    "db_write_mode",
    # v2.32.0: loss-streak reset restart:
    "streak_reset_losses", "streak_reset_delay",
    *_SESSION_RULE_KEYS,
]

# Fields stored on state as (N, X) tuples but round-tripped through JSON as "N:X" strings.
# Decoded on load via _parse_streak_delay (Phase 2 helper).
_STREAK_TUPLE_KEYS = [
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets",
    "streakbet_loss",
]

def _reload_live_config() -> dict:
    """Re-read CONFIG_PATH and apply only safe fields; return {field: (old, new)}.

    Never touches session_id / session_start / counters. Streak-tuple fields
    decode from "N:X" strings via parse_streak_delay (v2.28.2 grammar). Unsafe
    keys present in the file get logged at WARN so the user knows they need
    to `wolfctl restart` for those.
    """
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("live reload: failed to parse config: %s", e)
        return {}
    state.lanes_count = _lanes_count(data)
    state.split_lanes = _split_lanes(data, getattr(state, "lane", 1))
    state.key_lanes = _key_lanes(data, getattr(state, "lane", 1))
    _set_lane_log_prefix(getattr(state, "lane", 1), _lane_name(data, getattr(state, "lane", 1)))
    data = _resolve_lane_secrets(_lane_config(data, getattr(state, "lane", 1)))
    data.pop("_account_problem", None)

    applied = {}
    skipped_unsafe = []
    streak_keys_set = set(_STREAK_TUPLE_KEYS)
    config_keys_set = set(_CONFIG_KEYS)
    with state.lock:
        for k, v in data.items():
            if k in _LIVE_RELOAD_KEYS:
                new_val = parse_streak_delay(v) if k in streak_keys_set else v
                old_val = getattr(state, k, None)
                if old_val != new_val:
                    setattr(state, k, new_val)
                    applied[k] = (old_val, new_val)
            elif k in config_keys_set or k in streak_keys_set:
                # Present but not live-safe: warn if it actually changed.
                if getattr(state, k, None) != v:
                    skipped_unsafe.append(k)

    # Side effects for keys that need them.
    # v2.37.0: increment turned off (none / 0) ends the run; turned on with
    # no run starts one at the current session.
    if state.profit_increment_scope == "run" and (
            "profit_increment" in applied or "profit_threshold" in applied):
        if not _increment_on():
            _end_run("increment off")
        elif state.run_first_session is None and state.session_id:
            _attach_run(state.session_id)
            state.next_profit_milestone = state.profit_threshold
    if "proxy" in applied:
        _apply_proxy_to_session()
    if "base_bet" in applied:
        _rebase_run_for_base_change(applied["base_bet"][1])
    if "events_enabled" in applied or "events_url" in applied:
        _stop_events()
        _start_events()
    if "custom_rules_text" in applied and state.custom_rules_text:
        try:
            state.custom_rules = _load_rules_from_text(state.custom_rules_text)
        except Exception as e:
            logger.warning("live reload: rule reparse failed: %s", e)
    if skipped_unsafe:
        logger.warning(
            "live reload: skipped %d unsafe field(s): restart daemon to apply: %s",
            len(skipped_unsafe), ", ".join(sorted(skipped_unsafe)),
        )
    state.pending_restart = {
        k: [_config_show_value(k, getattr(state, k, None)), _config_show_value(k, data[k])]
        for k in sorted(skipped_unsafe) if k in _SETTINGS and k != "api_key"}
    return applied


def _redact_url(url: Optional[str]) -> str:
    """Show an events URL as scheme://host[:port] (unix: the socket path), never credentials."""
    if not url:
        return "off"
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        if parts.scheme == "unix":
            return f"unix://{parts.path}"
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    except ValueError:
        return "(invalid url)"


_RELOAD_MASKS = {
    "proxy": lambda v: _mask_proxy(v),
    "events_url": lambda v: _redact_url(v),
}


def _sig_reload(sig, frame):
    """SIGHUP handler: re-read config file for live-safe fields.

    Signal handlers must be short and never raise. Any error is logged
    and swallowed.
    """
    try:
        applied = _reload_live_config()
        if applied:
            logger.info("SIGHUP: reloaded %d live field(s): %s",
                        len(applied), ", ".join(sorted(applied.keys())))
            _emit_lifecycle("reload", {"applied": {
                k: [_RELOAD_MASKS[k](old), _RELOAD_MASKS[k](new)] if k in _RELOAD_MASKS
                else [old, new]
                for k, (old, new) in applied.items()}})
        else:
            logger.info("SIGHUP: no live-safe changes to apply")
    except Exception as e:
        logger.warning("SIGHUP handler raised: %s", e)


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
#  RULE-BASED STRATEGY ENGINE  (v2: DarkDice-inspired)
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
#  BOT STATE: single shared object
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

        # ── session rules: pre-bet cap, loss restart, balance goal, phases ──
        self.hard_cap           : bool = False
        self.restart_after_loss : bool = False
        self.balance_goal       : Optional[float] = None
        self.hybrid_recover_at       : Optional[float] = None
        self.hybrid_recover_strategy : str = "2"
        self.hybrid_recover_bet      : float = 1.0
        self.hybrid_press_at         : Optional[float] = None
        self.hybrid_press_floor      : float = 0.0
        self.hybrid_press_strategy   : str = "5"
        self.hybrid_press_bet        : float = 1.0
        self.hybrid_phase            : str = PHASE_BASE
        self.hybrid_switches         : int = 0

        # ── session info ──
        self.session_id     = None
        self.session_start  = time.time()
        # v2.34.0: bot start; never reset by recurring / streak-reset cycles
        self.run_started_at = time.time()
        # v2.35.0: parallel lanes (see _apply_lane). lane_share None = no split.
        self.lane            = 1
        self.lanes_count     = 1
        self.split_lanes     = 1     # lanes sharing this lane's account (key + currency)
        self.key_lanes       = 1     # lanes sharing this lane's key, so its rate limit
        self.key_ref         = "default"
        self.proxy_ref       : Optional[str] = None
        self.account_problem : Optional[str] = None
        self.lane_share      : Optional[float] = None
        self.lane_name       : Optional[str] = None
        self.pending_restart : dict = {}
        self.lane_split      = "none"     # v2.36.0: see LANE_SPLIT_MODES
        self.base_bet_disk   = None       # base_bet as saved (before the bet split)
        self.base_bet_split  = None       # split base_bet applied at load, else None
        self.lane_run_profit = 0.0
        self.soft_pace_delay = 0.0
        # v2.28.4: when a session enters `_reset_session_for_recurring`, this
        # is set to time.time() BEFORE the sleep so uptime + ended_at freeze
        # at the real bet-end. Cleared right before the new session's
        # session_start is set. None during active betting.
        self.session_bet_ended_at : Optional[float] = None
        # Pause start timestamp (for "Paused Xm/Ym" display in monitor/status).
        self.session_pause_started_at : Optional[float] = None
        self.total_bets     = 0
        self.wins           = 0
        self.losses         = 0
        self.profit         = 0.0
        self.wagered        = 0.0
        self.start_balance  = 0.0
        self.current_balance= 0.0

        # ── streaks ──
        self.current_streak  = 0   # + = win streak, - = loss streak
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
        self._auto_restart_pending: bool = False
        self._auto_start_last_time: float = 0.0
        self._auto_start_remaining: Optional[int] = None

        # ── Phase 3 observability metrics ──
        self._last_api_ms   : float = 0.0
        self._api_ms_total  : float = 0.0
        self._api_ms_count  : int   = 0
        self.peak_api_ms    : float = 0.0
        self._api_samples   : deque = deque(maxlen=500)
        self._on_target_count: int   = 0
        self._cycle_ms      : float = 0.0            # v2.30.4: aliased to cycle_p50 for state-file compat
        self._cycle_overhead_ms: float = 0.0         # cycle − api − delay*1000
        self._last_cycle_start: float = 0.0
        # v2.30.4: rolling per-iteration cycle window (mirrors _api_samples).
        # Replaces α=0.1 EMA which held onto outliers for 5-10 samples.
        self._cycle_samples : deque = deque(maxlen=500)

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
        self.status     = "Initializing..."
        self.last_error = ""

        # ── profit-based base bet increment ──
        self.profit_increment    : Optional[float] = None   # amount to add to base_bet each milestone
        self.profit_threshold    : Optional[float] = None   # profit interval that triggers increment
        self.next_profit_milestone: float = 0.0              # next profit level to trigger bump
        # v2.37.0: run-wide increment: profit summed across sessions since
        # the run started (persisted in RUN_PATH until `wolfctl run-reset`).
        self.profit_increment_scope = "session"
        self.run_first_session: Optional[int] = None
        self.run_since: Optional[str] = None   # ISO time the run began
        self.run_bumps        = 0
        self.run_profit_done  = 0.0     # finished sessions of this run
        self.run_base_origin: Optional[float] = None  # base the bumps were earned on

        # ── strategy internal state ──
        self.dalembert_unit = 0
        self.paroli_count   = 0
        self.delay_martin_threshold = 3   # Delay Martingale: flat for this many consecutive losses

        # ── Phase 2 session robustness ──
        # Format: "N:seconds": after every N consecutive losses/wins/bets, pause X seconds
        self.streak_delay_loss: tuple = (0, 0.0)
        self.streak_delay_win : tuple = (0, 0.0)
        self.streak_delay_bets: tuple = (0, 0.0)
        # Format: "N:multiplier": after every N consecutive losses, multiply current_bet by X (0<X<1 reduces)
        self.streakbet_loss   : tuple = (0, 1.0)
        # Base bet as % of balance (0 disables); recomputed periodically from live balance
        self.basebet_pct      : float = 0.0
        # Milestone thresholds: 0 disables each
        self.milestone_bets   : int = 100
        self.milestone_wins   : int = 0
        self.milestone_losses : int = 0
        self.milestone_profit : float = 0.0
        self._last_profit_milestone: float = 0.0
        # Recurring: restart session after non-loss, non-insufficient-balance stop with profit≥0
        self.recurring        : bool = False
        # Seconds to pause between one recurring cycle ending and the next starting.
        # v2.30.6: default 60, floor 0, cap 86400 (24 h). Was 3600 (arbitrary
        # TG-mirror cap that silently swallowed longer recurring pauses).
        self.recurring_delay  : float = 60.0
        # Streak reset: end + restart the session on the first win after this
        # many consecutive losses (None = off). Own pause, default 10 min.
        self.streak_reset_losses : Optional[int] = None
        self.streak_reset_delay  : float = 600.0
        # Length of the loss streak the latest win just broke (0 if none).
        self.last_broken_loss_streak : int = 0
        # Pause length of the current recurring gap (for monitor countdown).
        self.session_pause_total : float = 0.0

        # ── v2.26.0: proxy + retention (TG↔CLI parity) ──
        # HTTP/HTTPS/SOCKS5 proxy URL, or None for direct connection.
        # Credentials in the URL are masked in show + logs via _mask_proxy.
        self.proxy            : Optional[str] = None
        # Bet-row retention window in days (used by hourly cleanup thread + --cleanup).
        self.purge_days       : int = 3

        # ── v2.27.0: bet DB write mode ──
        # "async" (default): bet rows batched via background writer thread -
        # 30-40 ms per-bet overhead savings via persistent connection +
        # PRAGMA synchronous=NORMAL + batched INSERTs.
        # "sync": legacy path: fresh connection + fsync per bet (safe but slow).
        # Fallback to sync on queue overflow preserves nothing-lost semantics.
        self.db_write_mode    : str = "async"
        self.events_enabled   : bool = False
        self.events_url       : Optional[str] = os.environ.get("WOLFBET_EVENTS_URL") or None

        # ── Phase 5c: auto-bet recovery (max nonce / session expired) ──
        # Tag from _classify_auto_stop_reason: "max_nonce", "expired", or "".
        # Signals the loop to call _recover_expired_session (seed rotation)
        # instead of a bare api_auto_start.
        self.last_stop_reason           : str = ""
        self.consecutive_recovery_fails : int = 0

        # ── Phase 5b: pipelined betting (Flat-only) ──
        # Requested depth (config/preset/CLI); 1 = serial (default, unchanged behavior).
        self.pipeline_depth      : int = 1
        # Effective depth after Flat-only + auto-game guardrail. Set by dispatcher.
        self.pipeline_effective  : int = 1
        # Live counters shown on the dashboard.
        self.pipeline_in_flight  : int = 0
        self.pipeline_dispatched : int = 0
        # Human-readable reason if guardrail coerced depth down.
        self.pipeline_reason     : str = ""


state   = BotState()
console = Console()

# ══════════════════════════════════════════════════════════
#  v2.26.0: Shared HTTP session (proxy-aware)
# ══════════════════════════════════════════════════════════
# All wolfbet.com API calls go through _http_session so proxy config lives in
# ONE place. Mirrors tg/engine.py:_recreate_http (httpx.AsyncClient(proxy=...))
# but stays on `requests` for CLI's synchronous / threaded model.
_http_session = requests.Session()
_http_session.headers.update({"User-Agent": f"wolfbet-cli/{VERSION}"})


# _validate_proxy / _mask_proxy: extracted to core.net_helpers in v2.28.0.
# Aliases kept so every call-site (and every v2.26.0+ test) keeps working
# without edits. TG bot's tg/handlers.py imports the same functions.
_validate_proxy = validate_proxy
_mask_proxy     = mask_proxy


def _apply_proxy_to_session() -> None:
    """Update _http_session.proxies from state.proxy: single point of change."""
    if state.proxy:
        _http_session.proxies = {"http": state.proxy, "https": state.proxy}
    else:
        _http_session.proxies = {}

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
            state.soft_pace_delay = soft_pace_delay(remaining, limit)
            if remaining <= 2 and not state.rate_wait:
                state.rate_wait      = True
                state.rate_backoff   = 2.0   # start probing at 2s
    except Exception:
        pass

SOFT_PACE_LOW_WATER = 0.10   # start easing off below 10% of the quota left
SOFT_PACE_MAX_DELAY = 0.25   # seconds per bet when the quota is nearly gone


def soft_pace_delay(remaining: int, limit: int) -> float:
    """v2.35.0: per-bet delay that rises smoothly as the shared quota runs out.

    The rate-limit headers are per key, so every lane on the same key sees the
    same `remaining`. Above the low-water mark this is 0, so a single lane never
    slows down. Below it, lanes ease off together instead of all hitting
    the hard 'remaining <= 2 → pause' cliff at the same instant.
    """
    try:
        remaining, limit = int(remaining), int(limit)
    except (TypeError, ValueError):
        return 0.0
    low = max(5, int(limit * SOFT_PACE_LOW_WATER))
    if limit <= 0 or remaining >= low:
        return 0.0
    frac = max(0.0, min(1.0, 1.0 - remaining / low))
    return round(SOFT_PACE_MAX_DELAY * frac, 4)


def _autostart_lock_path() -> str:
    """The lock file the lanes on this lane's key share; the main key keeps the original path."""
    key_ref = getattr(state, "key_ref", VAULT_KEY_ID) or VAULT_KEY_ID
    if key_ref == VAULT_KEY_ID:
        return AUTOSTART_LOCK_PATH
    root, ext = os.path.splitext(AUTOSTART_LOCK_PATH)
    return f"{root}.{key_ref}{ext}"


def _autostart_global_pace() -> None:
    """v2.35.0: space /auto/start calls across the lane processes on the same key.

    Each lane restarts its auto-cycle every session. A lock file per key holds
    the last start time so lanes on one key never fire /auto/start in the same
    instant (which only buys 429s); lanes on other keys have their own limit and
    never wait. No-op for a lane alone on its key or without fcntl.
    """
    if getattr(state, "key_lanes", 1) <= 1:
        return
    try:
        import fcntl
    except ImportError:          # pragma: no cover: non-POSIX
        return
    try:
        fd = os.open(_autostart_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.pread(fd, 64, 0).decode(errors="ignore").strip()
        try:
            last = float(raw) if raw else 0.0
        except ValueError:
            last = 0.0
        wait = last + AUTO_START_MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(min(wait, AUTO_START_MIN_INTERVAL))
        stamp = f"{time.time():.3f}".encode()
        os.ftruncate(fd, 0)
        os.pwrite(fd, stamp, 0)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def api_get_balances():
    r = _http_session.get(f"{API_BASE}/v1/user/balances", headers=_headers("dice"), timeout=12)
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
        _active_strategy_key(), state.loss_mult, state.win_mult, _active_base_bet(),
        state.game, state.custom_rules,
        _auto_stop_conditions(),
        state.delay_martin_threshold, logger,
    )


def _build_auto_start_payload() -> dict:
    """Adapter: state.* → shared build_auto_start_payload()."""
    return build_auto_start_payload(
        state.game, state.currency, _active_base_bet(), state.multiplier,
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
        r = _http_session.get(url, headers=_headers("dice"), timeout=12)
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
        _autostart_global_pace()
        state._auto_start_last_time = time.monotonic()
        try:
            t0 = time.time()
            r = _http_session.post(url, headers=_headers(body_str=body_str),
                              json=payload, timeout=15)
            _record_api_ms((time.time() - t0) * 1000)
            _update_auto_start_remaining(r.headers)
            if r.status_code == 200:
                data = r.json()
                uuid = data.get("autoBet", {}).get("uuid")
                if uuid:
                    with state.lock:
                        state._auto_uuid = uuid
                        state.last_stop_reason = ""
                        state.consecutive_recovery_fails = 0
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
            body = (r.text or "")[:400]
            if _is_insufficient_balance(body):
                logger.warning("INSUFFICIENT BALANCE (auto/start %d): %s", r.status_code, body)
                with state.lock:
                    state.last_error = "Insufficient balance"
                    state._insufficient_balance = True
                return False
            classified = _classify_auto_stop_reason(body)
            # v2.30.9: include a short body snippet in state.last_error so
            # operators self-diagnose from `wolfctl status` instead of
            # trawling logs. Was: bare "HTTP 422": actual reason was hidden.
            body_snip = body.strip().replace("\n", " ")[:120]
            with state.lock:
                if classified:
                    state.last_stop_reason = classified
                state.last_error = (
                    f"Auto-bet start failed: HTTP {r.status_code}"
                    + (f": {body_snip}" if body_snip else "")
                )
            logger.error("auto/start HTTP %d (classified=%s): %s",
                         r.status_code, classified or "none", body)
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
        _http_session.post(url, headers=_headers(body_str=body_str),
                      json=payload, timeout=12)
        logger.info("Auto-bet stopped, uuid=%s", state._auto_uuid)
    except Exception:
        pass
    with state.lock:
        state._auto_uuid = None
        state._auto_restart_pending = False


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


# _classify_auto_stop_reason: extracted to core.net_helpers in v2.28.0.
# TG bot now imports the same helper, closing its max-nonce blind spot.
_classify_auto_stop_reason = classify_auto_stop_reason


def _is_insufficient_balance(text) -> bool:
    """True when a WolfBet error body/message reports insufficient balance."""
    return "insufficient" in str(text or "").lower()


def _midchain_loss_reason() -> str:
    """Non-empty stop reason when restarting the server auto-cycle would abandon a loss.

    A fresh /auto/start resets the server-side progression to base_bet. Mid loss
    streak or with the session in the red, that loss can never be recovered and
    the streak-reset (profit >= 0) trigger can never fire: stop instead.
    """
    if state.current_streak < 0 or state.profit < 0:
        return (f"Auto-cycle lost mid-chain (streak {state.current_streak}, "
                f"profit {state.profit:+.8f})")
    return ""


def _maybe_apply_pending_auto_restart() -> None:
    """Restart the auto cycle for a pending base-bet change once the chain is closed.

    Clearing the UUID while the session is in the red makes the next loop tick hit
    the mid-chain guard, which stops the daemon. The increment waits here until
    profit and streak are non-negative instead.
    """
    if not (is_auto_game(state.game) and state._auto_restart_pending):
        return
    if _midchain_loss_reason():
        return
    with state.lock:
        state._auto_uuid = None
        state._auto_restart_pending = False


def _recover_expired_session() -> bool:
    """Recover from 'Session has ended' / 'Max nonce achieved'.

    Rotates the server seed, best-effort stops the dead UUID, respects the
    current rate-limit backoff, then starts a fresh auto-bet cycle. Does
    NOT touch state.session_id or DB rows: the CLI session identity is
    preserved across many WolfBet server-side auto-cycles.
    """
    reason = state.last_stop_reason or "expired"
    logger.warning("Auto-bet recovery: %s: rotating seed + restarting", reason)
    with state.lock:
        state.status = f"Recovering from {reason}: rotating server seed..."
    _rotate_server_seed()
    api_auto_stop()
    settle = max(2.0, state.rate_backoff)
    time.sleep(settle)
    ok = api_auto_start()
    if ok:
        with state.lock:
            state.last_stop_reason = ""
            state.consecutive_recovery_fails = 0
            state.status = "Seed rotated: auto-cycle restarted"
    else:
        with state.lock:
            state.consecutive_recovery_fails += 1
    return ok


def api_place_bet(amount: float) -> Optional[dict]:
    payload = _build_bet_payload(amount)
    ep = GAME_ENDPOINTS.get(state.game, ("/v1/bet/place", 1, 480, None))
    url = f"{API_BASE}{ep[0]}"
    body_str = json.dumps(payload, separators=(",", ":"))
    logger.debug("BET payload=%s url=%s", body_str, url)
    try:
        t0 = time.time()
        r = _http_session.post(url, headers=_headers(body_str=body_str),
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
                # Auto-bet UUID expired / server-seed exhausted: clear UUID so
                # next loop iteration recovers (seed rotation if last_stop_reason set).
                if is_auto_game(state.game):
                    classified = _classify_auto_stop_reason(err_msg)
                    if classified:
                        logger.warning("AUTO-BET %s: %s", classified.upper(), err_msg[:120])
                        with state.lock:
                            state._auto_uuid = None
                            state.last_stop_reason = classified
                            state.last_error = (
                                "Max nonce achieved: rotating server seed..."
                                if classified == "max_nonce"
                                else "Auto-bet session expired: restarting..."
                            )
                        return None
                with state.lock:
                    state.last_error = f"API error: {err_msg[:120]}"
                return None
            # Auto-bet: server may report a stop reason inline (max_profit/max_loss/max_nonce/etc.)
            if is_auto_game(state.game):
                stop_reason = data.get("stopReason") or data.get("stop_reason")
                if stop_reason:
                    reason_str = _fmt_auto_stop_reason(stop_reason)
                    classified = _classify_auto_stop_reason(reason_str)
                    logger.info("AUTO-BET STOP from server: %s (classified=%s)",
                                reason_str, classified or "terminal")
                    with state.lock:
                        state._auto_uuid = None
                        if classified:
                            state.last_stop_reason = classified
                        state.last_error = f"WolfBet auto-stop: {reason_str}"
                    # Let caller record this bet; loop then either recovers (if classified)
                    # or lets should_stop / user-side thresholds decide.
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
            logger.warning("RATE LIMIT 429: backoff %.1fs (Retry-After=%s)", delay, retry_after)
            with state.lock:
                state.rate_wait    = True
                state.rate_backoff = max(2.0, delay)
                state.last_error   = f"Rate limit 429: retrying in {delay:.0f}s..."
        elif r.status_code == 400 and is_auto_game(state.game):
            # UUID expired / seed exhausted: classify body so loop recovers correctly.
            body = r.text[:200]
            # v2.32.1: insufficient balance is terminal. It used to fall through
            # to the "expired" default, so the loop restarted a fresh auto-cycle
            # at base bet with the session deep in the red.
            if _is_insufficient_balance(body):
                logger.warning("INSUFFICIENT BALANCE (auto-bet 400): %s", body)
                with state.lock:
                    state._auto_uuid = None
                    state.last_error = "Insufficient balance"
                    state._insufficient_balance = True
                return None
            classified = _classify_auto_stop_reason(body) or "expired"
            logger.warning("AUTO-BET 400 (%s): %s", classified, body)
            with state.lock:
                state._auto_uuid = None
                state.last_stop_reason = classified
                state.last_error = (
                    "Max nonce achieved: rotating server seed..."
                    if classified == "max_nonce"
                    else "Auto-bet session expired: restarting..."
                )
        else:
            body = r.text[:500]
            logger.error("HTTP %d  response=%s  payload=%s",
                         r.status_code, body, body_str)
            with state.lock:
                state.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
    except requests.exceptions.Timeout:
        logger.warning("REQUEST TIMEOUT on bet #%d", state.total_bets + 1)
        with state.lock:
            state.last_error = "Request timeout: retrying..."
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
#  SESSION RULES: pre-bet cap, loss restart, balance goal, phases
# ══════════════════════════════════════════════════════════
def _hybrid_on() -> bool:
    """True when a phase threshold is set."""
    return state.hybrid_recover_at is not None or state.hybrid_press_at is not None


def _hard_cap_on() -> bool:
    """True when a bet is checked against the loss cap before it is placed. Phases always do."""
    return bool(state.hard_cap) or _hybrid_on()


def _active_strategy_key() -> str:
    """Strategy that sizes the next bet: the current phase's, else the configured one."""
    if _hybrid_on():
        if state.hybrid_phase == PHASE_RECOVER:
            return str(state.hybrid_recover_strategy)
        if state.hybrid_phase == PHASE_PRESS:
            return str(state.hybrid_press_strategy)
    return state.strategy_key


def _active_base_bet() -> float:
    """First bet of the current phase: base_bet times the phase's multiple."""
    if _hybrid_on():
        if state.hybrid_phase == PHASE_RECOVER:
            return state.base_bet * state.hybrid_recover_bet
        if state.hybrid_phase == PHASE_PRESS:
            return state.base_bet * state.hybrid_press_bet
    return state.base_bet


def _strategy_label() -> str:
    """Strategy name for the session row; marks sessions that ran with phases."""
    return f"Hybrid: {state.strategy}" if _hybrid_on() else state.strategy


def _with_phase(strategy: str, phase) -> str:
    """Strategy text for the live screens, with the current phase when phases are on."""
    return f"{strategy}  phase {phase}" if phase else strategy


def _auto_stop_conditions() -> dict:
    """Stop rules sent with a server auto-bet cycle.

    With phases on a cycle starts mid-session, so the profit and loss limits
    are the distances left from the session profit so far.
    """
    max_profit, max_loss = state.max_profit, state.max_loss
    if _hybrid_on():
        if max_profit is not None:
            max_profit = max(max_profit - state.profit, 0.0)
        if max_loss is not None:
            max_loss = max(abs(max_loss) + state.profit, 0.0)
    return {"max_profit": max_profit, "max_loss": max_loss,
            "max_bets": state.max_bets, "stop_on_balance": state.stop_on_balance}


def _session_rule_errors() -> list:
    """Reasons the configured session rules cannot work (empty when usable or all off)."""
    cfg = {key: getattr(state, key) for key in _SESSION_RULE_KEYS}
    for key in ("base_bet", "loss_mult", "win_mult", "max_profit", "max_loss",
                "stop_on_balance", "recurring"):
        cfg[key] = getattr(state, key)
    return config_errors(cfg)


def _exit_on_session_rule_errors(exit_code: int = 2, as_json: bool = False) -> None:
    """Refuse to start betting with session rules that cannot work, naming each setting.

    The service start passes exit_code 0. Its unit restarts the bot after a
    failure code, which would retry every few seconds and then start betting by
    itself the moment the config is fixed.
    """
    problems = _session_rule_errors()
    if not problems:
        return
    if as_json:
        _print_json({"error": {"code": "invalid_config",
                               "message": "Cannot start: fix these settings first: " + "; ".join(problems)}})
        logger.error("START REFUSED: %s", "; ".join(problems))
        sys.exit(exit_code)
    console.print("[red]Cannot start: fix these settings first.[/]")
    for problem in problems:
        console.print(f"  [yellow]{escape(problem)}[/]")
    logger.error("START REFUSED: %s", "; ".join(problems))
    sys.exit(exit_code)


def _hybrid_step() -> bool:
    """Move to the phase the session profit calls for. Returns True on a switch.

    A switch starts the phase's strategy from its first bet. On an auto game it
    also ends the server cycle, so the next tick starts one with that strategy.
    """
    target = PHASE_BASE
    if _hybrid_on():
        target = next_phase(state.hybrid_phase, state.profit, state.hybrid_recover_at,
                            state.hybrid_press_at, state.hybrid_press_floor)
    if target == state.hybrid_phase:
        return False
    previous, previous_name = state.hybrid_phase, STRATEGY_NAMES.get(_active_strategy_key(), "?")
    with state.lock:
        state.hybrid_phase = target
        state.hybrid_switches += 1
        state.current_bet = _active_base_bet()
        state.dalembert_unit = 0
        state.paroli_count = 0
    logger.warning("PHASE session %s: %s (%s) -> %s (%s) at profit %+.8f, next bet %.8f",
                   state.session_id, previous, previous_name, target,
                   STRATEGY_NAMES.get(_active_strategy_key(), "?"),
                   round(state.profit, 8) or 0.0, state.current_bet)
    if is_auto_game(state.game):
        api_auto_stop()
    return True


# ══════════════════════════════════════════════════════════
#  STRATEGY ENGINE
# ══════════════════════════════════════════════════════════
def compute_next_bet(last_result: str) -> float:
    """Compute next bet amount (delegates to core.engine)."""
    nxt, state.dalembert_unit, state.paroli_count = _core_compute_next_bet(
        _active_strategy_key(), _active_base_bet(), state.current_bet,
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

def _bet_fit_stop(profit: float, balance: float) -> str:
    """Stop reason when the next bet would pass the loss cap or the balance floor, else ''.

    Compares the amount as it is sent (8 decimals), so the cap is exact wherever
    the bot sizes the bet itself.
    """
    bet = round(state.current_bet, 8)
    room = loss_room(profit, state.max_loss)
    if bet > room + HALF_SAT:
        if state.max_loss is None:
            return f"{LOSS_CAP_REASON}: hard_cap needs max_loss"
        return f"{LOSS_CAP_REASON}: next bet {bet:.8f} does not fit (left {room:.8f})"
    if bet > floor_room(balance, state.stop_on_balance) + HALF_SAT:
        return (f"Balance floor: next bet {bet:.8f} does not fit above "
                f"{state.stop_on_balance:.8f}")
    return ""


def _reached(value: float, target: float) -> bool:
    """True when value has reached target, to the nearest satoshi.

    Session profit is a sum of bet results and carries float dust, while real
    amounts are whole satoshis, so a value less than half a satoshi short counts
    as reached. The slack shrinks for a target under one satoshi, so zero profit
    never counts.
    """
    return value > target - min(HALF_SAT, abs(target) / 2)


def should_stop() -> tuple:
    p = state.profit
    b = state.current_balance
    if state.balance_goal is not None and b >= state.balance_goal:
        return True, f"{BALANCE_GOAL_REASON}: {b:.8f}"
    if state.max_profit is not None and _reached(p, state.max_profit):
        return True, f"Profit target reached: {p:+.8f}"
    if state.max_loss is not None and _reached(-p, abs(state.max_loss)):
        return True, f"Max loss hit: {p:+.8f}"
    if state.max_bets is not None and state.total_bets >= state.max_bets:
        return True, f"Max bets reached: {state.total_bets}"
    if state.max_wins is not None and state.wins >= state.max_wins:
        return True, f"Max wins reached: {state.wins}"
    if state.stop_on_balance is not None and b <= state.stop_on_balance:
        return True, f"Balance floor hit: {b:.8f}"
    # Streak reset: fires only on the win that broke an N+ loss streak
    # (current_streak == 1) and only while the session is not in the red.
    n = state.streak_reset_losses
    if (n and state.current_streak == 1
            and state.last_broken_loss_streak >= n and p >= 0):
        return True, f"{STREAK_RESET_REASON}: {state.last_broken_loss_streak} losses then win"
    # v2.35.0: split budget: a lane may only risk its share of the balance.
    eq = lane_equity()
    if eq is not None and state.current_bet > eq:
        return True, (f"{LANE_BUDGET_REASON} (next bet {state.current_bet:.8f}, "
                      f"lane equity {eq:.8f})")
    if _hard_cap_on():
        reason = _bet_fit_stop(p, b)
        if reason:
            return True, reason
    return False, ""


LANE_BUDGET_REASON = "Lane budget exceeded"
PROFIT_INCREMENT_SCOPES = ("session", "run")


def _increment_on() -> bool:
    """v2.37.0: increment active only with both values set and > 0."""
    return bool(state.profit_increment) and bool(state.profit_threshold) \
        and state.profit_increment > 0 and state.profit_threshold > 0


def _check_profit_increment() -> None:
    """Bump base_bet when profit reaches the next milestone.

    v2.37.0: scope "session" measures this session's profit (milestone resets
    each recurring cycle); scope "run" measures the total since the run
    started and persists each bump in the run file. Caller holds state.lock.
    """
    if not _increment_on():
        return
    run = state.profit_increment_scope == "run"
    p = run_profit_total() if run else state.profit
    if p < state.next_profit_milestone:
        return
    state.base_bet += state.profit_increment
    state.next_profit_milestone += state.profit_threshold
    if run:
        state.run_bumps += 1
        _save_run()
    tag = "RUN PROFIT INCREMENT" if run else "PROFIT INCREMENT"
    # For auto-games the Martingale progression runs server-side on WolfBet
    # using the base_bet submitted at /auto/start, so a new base needs a new
    # UUID. The restart is deferred to the loop tick that owns the UUID swap,
    # which skips it while the session is in the red. Same CLI session, new
    # WolfBet UUID.
    if is_auto_game(state.game) and state._auto_uuid:
        state._auto_restart_pending = True
        logger.info("%s: base_bet → %.8f + auto-cycle restart pending once the loss chain closes "
                    "(next milestone at %.8f profit)",
                    tag, state.base_bet, state.next_profit_milestone)
    else:
        logger.info("%s: base_bet → %.8f (next milestone at %.8f profit)",
                    tag, state.base_bet, state.next_profit_milestone)


# ── v2.37.0: run-wide profit increment ───────────────────
def _run_path(lane: int = None) -> str:
    return _lane_path(RUN_PATH, getattr(state, "lane", 1) if lane is None else lane)


def _load_run(lane: int = None):
    try:
        with open(_run_path(lane)) as f:
            d = json.load(f)
        return {"first_session": int(d["first_session"]),
                "started_at": d.get("started_at"),
                "bumps": max(0, int(d.get("bumps") or 0)),
                "base": float(d["base"]) if d.get("base") is not None else None}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_run() -> None:
    if state.run_first_session is None:
        return
    path = _run_path()
    tmp = path + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"first_session": state.run_first_session,
                       "started_at": state.run_since,
                       "bumps": state.run_bumps,
                       "base": state.run_base_origin}, f)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("run file save failed: %s", e)


def _run_profit_done_from_db(first_sid: int, exclude_sid=None) -> float:
    """Sum of this lane's session profits since `first_sid` (excluding one)."""
    try:
        conn = _db_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(profit),0) FROM sessions "
            "WHERE id >= ? AND id != ? AND COALESCE(lane,1) = ?",
            (first_sid, exclude_sid if exclude_sid is not None else -1,
             getattr(state, "lane", 1))).fetchone()
        conn.close()
        return float(row[0] or 0.0)
    except Exception as e:
        logger.warning("run profit query failed: %s", e)
        return 0.0


def _db_max_session_id() -> int:
    try:
        conn = _db_conn()
        row = conn.execute("SELECT COALESCE(MAX(id),0) FROM sessions").fetchone()
        conn.close()
        return int(row[0] or 0)
    except Exception:
        return 0


def run_profit_total() -> float:
    return state.run_profit_done + state.profit


def _init_run() -> None:
    """Restore (or prepare) the run before the first session starts.

    Scope "run" only. An existing run file restores the bumps on top of the
    loaded base_bet and the profit of the run's finished sessions. A file
    pointing past the DB's last session (stats were reset) starts a new run.
    """
    state.run_first_session = None
    state.run_bumps = 0
    state.run_profit_done = 0.0
    if state.profit_increment_scope != "run" or not _increment_on():
        # Off (none / 0) or per-session scope: no run in the background, and
        # a leftover run never comes back when the increment is re-enabled.
        _end_run("increment off" if state.profit_increment_scope == "run" else "scope session")
        return
    run = _load_run()
    if run and run["first_session"] > _db_max_session_id() + 1:
        logger.warning("RUN file points past the stats DB (reset?): starting a new run")
        run = None
    if run:
        state.run_first_session = run["first_session"]
        state.run_since = run["started_at"]
        origin = run.get("base")
        if origin is not None and abs(origin - state.base_bet) > 1e-12:
            logger.warning("RUN base changed (%.8f -> %.8f) - dropping %d bump(s), re-anchoring",
                           origin, state.base_bet, run["bumps"])
            state.run_bumps = 0
        else:
            state.run_bumps = run["bumps"]
        state.run_base_origin = state.base_bet if origin is None else origin
        state.run_profit_done = _run_profit_done_from_db(run["first_session"])
        if state.profit_increment and state.run_bumps:
            state.base_bet = round(state.base_bet + state.run_bumps * state.profit_increment, 8)
            state.current_bet = state.base_bet
    if state.profit_threshold:
        state.next_profit_milestone = _next_milestone_above(state.run_bumps,
                                                            state.profit_threshold,
                                                            state.run_profit_done)
    logger.warning("RUN %s  since #%s  bumps=%d  run profit=%+.8f  base_bet=%.8f",
                   "resumed" if run else "new", state.run_first_session or "next",
                   state.run_bumps, state.run_profit_done, state.base_bet)


def _next_milestone_above(bumps: int, threshold: float, profit: float) -> float:
    """Next bump level for a run: one step past `bumps`, never below `profit`."""
    thr = float(threshold or 0)
    if thr <= 0:
        return 0.0
    return ((max(int(bumps), math.floor(profit / thr)) + 1) * thr)


def _rebase_run_for_base_change(new_base: float) -> None:
    """Make an operator-set base authoritative for the rest of the run.

    Bumps earned on the previous base are dropped and the milestone re-anchors
    above the run's current profit, so the next bump needs fresh profit.
    """
    with state.lock:
        state.current_bet = new_base
        if state.profit_increment_scope == "run" and state.run_first_session is not None:
            state.run_bumps = 0
            state.run_base_origin = new_base
            state.next_profit_milestone = _next_milestone_above(
                0, state.profit_threshold, run_profit_total())
            _save_run()
        if is_auto_game(state.game):
            state._auto_restart_pending = True


def _end_run(reason: str) -> None:
    """Drop this lane's run: file removed, run counters cleared."""
    had = state.run_first_session is not None
    try:
        os.remove(_run_path())
        had = True
    except OSError:
        pass
    state.run_first_session = None
    state.run_since = None
    state.run_bumps = 0
    state.run_profit_done = 0.0
    state.run_base_origin = None
    if had:
        logger.warning("RUN ended (%s): the next run starts fresh", reason)


def _attach_run(session_id: int) -> None:
    """A new run begins at the first session this bot starts."""
    if (state.profit_increment_scope == "run" and _increment_on()
            and state.run_first_session is None):
        state.run_first_session = session_id
        state.run_since = datetime.now().isoformat()
        state.run_base_origin = state.base_bet
        _save_run()


def cmd_run_reset(lane: int = None) -> int:
    """Delete the run file(s): the next start begins a new run. Returns count removed."""
    n = 0
    for k in ([lane] if lane else _lane_file_ids(RUN_PATH)):
        try:
            os.remove(_lane_path(RUN_PATH, k))
            n += 1
        except OSError:
            continue
    console.print(f"[green]Run reset[/]: {n} run file(s) removed. "
                  "The next start begins a new run total.")
    return n


def lane_equity():
    """Money this lane may still risk: share + its result so far. None = no split."""
    if state.lane_share is None:
        return None
    return state.lane_share + state.lane_run_profit + state.profit


def _init_lane_budget() -> None:
    """v2.35.0: fix this lane's share of the balance at bot start."""
    if _split_budget_on() and state.current_balance > 0:
        state.lane_share = state.current_balance / state.split_lanes
        logger.warning("LANE %d/%d  share=%.8f of balance %.8f",
                       state.lane, state.split_lanes, state.lane_share,
                       state.current_balance)
    else:
        state.lane_share = None

# ══════════════════════════════════════════════════════════
#  BETTING LOOP  (runs in background thread)
# ══════════════════════════════════════════════════════════
def betting_loop():
    last_result = "none"

    while state.running:
        loop_start = time.monotonic()
        # ── paused ──────────────────────────────────────
        if state.paused:
            state.status = "PAUSED: press [R] to resume"
            time.sleep(0.4)
            continue

        # ── rate-limit cooldown: wait then resume ──
        if state.rate_wait:
            backoff = state.rate_backoff
            state.status = f"Rate limit: resuming in {backoff:.0f}s..."
            time.sleep(backoff)
            with state.lock:
                state.rate_wait      = False
                state.rate_backoff   = 2.0
                state.rate_remaining = 50  # assume recovered; bet response will update
            logger.info("Rate limit cooldown done, resuming betting")
            continue

        # The phase step runs first so the stop check sees the new phase's bet.
        _hybrid_step()

        # ── stop-condition check ─────────────────────────
        stop, reason = should_stop()
        if stop:
            logger.warning("SESSION END %s: %s after %d bets, %d phase switches, balance %.8f",
                           state.session_id, reason, state.total_bets,
                           state.hybrid_switches, state.current_balance)
            if _should_recurring_restart(reason):
                logger.info("RECURRING RESTART after: %s", reason)
                # Best-effort: stop the old auto UUID before spinning up a new session.
                if is_auto_game(state.game):
                    api_auto_stop()
                _db_save_session()
                _reset_session_for_recurring(_restart_delay_for(reason), reason)
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

        # ── auto-bet: ensure we have a live UUID (with max-nonce recovery) ──
        _maybe_apply_pending_auto_restart()
        if is_auto_game(state.game) and not state._auto_uuid:
            # v2.32.1: a new server cycle restarts the progression at base_bet.
            # Mid loss chain / in the red that abandons the loss with no way out.
            # With phases on the phase owns recovery and the loss cap bounds it.
            midchain = "" if _hybrid_on() else _midchain_loss_reason()
            if state._insufficient_balance or midchain:
                reason = "Insufficient balance" if state._insufficient_balance else midchain
                logger.warning("AUTO-CYCLE not restarted: %s", reason)
                state.running = False
                state.status = f"STOPPED: {reason}"
                _db_save_session()
                break
            needs_recovery = state.last_stop_reason in ("max_nonce", "expired")
            state.status = (
                f"Recovering from {state.last_stop_reason}: rotating seed..."
                if needs_recovery else "Starting auto-bet session..."
            )
            ok = _recover_expired_session() if needs_recovery else api_auto_start()
            if not ok:
                if state._insufficient_balance:
                    state.running = False
                    state.status = "STOPPED: Insufficient balance"
                    _db_save_session()
                    break
                # Circuit breaker: 3 consecutive recovery failures → hard stop.
                if state.consecutive_recovery_fails >= 3:
                    reason = state.last_stop_reason or "unknown"
                    logger.error("Recovery failed 3×: hard stop (reason=%s)", reason)
                    with state.lock:
                        state.running = False
                        state.status  = f"STOPPED: Recovery failed after 3 attempts ({reason})"
                        state.last_error = state.status
                    _db_save_session()
                    break
                with state.lock:
                    state.consecutive_errors += 1
                    state.backoff_delay = min(state.backoff_delay * 2, state.max_backoff)
                time.sleep(state.backoff_delay)
                continue
            with state.lock:
                state.consecutive_errors = 0
                state.backoff_delay = 1.0
                if _hybrid_on():
                    # A fresh server cycle sizes its first bet from the phase's base.
                    state.current_bet = _active_base_bet()
                    state.dalembert_unit = 0
                    state.paroli_count = 0

        # ── bet pacing delay ───────────────────────────────
        if state.bet_delay > 0:
            time.sleep(state.bet_delay)
        # v2.35.0: ease off as the shared rate-limit quota runs out
        if state.soft_pace_delay > 0:
            time.sleep(state.soft_pace_delay)

        # ── place bet ────────────────────────────────────
        state.status = f"Placing bet #{state.total_bets + 1}..."
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

        # Use API's profit directly: it's positive on win, negative on loss
        raw_profit = float(bet_data.get("profit", 0))

        with state.lock:
            # reset backoff + recovery counter on any successful bet
            state.consecutive_errors = 0
            state.backoff_delay      = 1.0
            state.consecutive_recovery_fails = 0

            state.total_bets     += 1
            state.wagered        += amount_used
            state.profit         += raw_profit
            state.current_balance = new_balance

            if bet_state == "win":
                state.wins += 1
                if raw_profit > state.highest_win:
                    state.highest_win = raw_profit
                state.last_broken_loss_streak = max(-state.current_streak, 0)
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
            _check_profit_increment()

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
                # new second: record the old second's count
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
                # new minute: record the old minute's count
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

        # persist to DB (async writer thread when db_write_mode=async)
        _db_save_bet_dispatch(bet_data, raw_profit, new_balance)

        # ── WolfRider: switch over/under after bet ──
        active_key = _active_strategy_key()
        if active_key == "6":
            wolfrider_switch()

        # ── Rule-Based: apply custom rules after bet ──
        if active_key == "8" and state.custom_rules:
            apply_rules(bet_state)

        # ── milestone check (bets/wins/losses/profit) ──
        ms_reason = _check_milestone(bet_state)
        if ms_reason:
            logger.info("MILESTONE: %s (balance %.8f, profit %+.8f)",
                        ms_reason, state.current_balance, state.profit)
            with state.lock:
                state.status = f"Milestone: {ms_reason}"

        # calculate next bet: no cap, API rejects if insufficient balance
        raw_next  = compute_next_bet(bet_state)
        floor_bet = _active_base_bet()
        next_bet  = max(floor_bet, raw_next)
        # ── streakbet_loss: reduce bet after N consecutive losses ──
        sb_factor = _get_streakbet_loss_factor()
        if sb_factor != 1.0:
            next_bet = max(floor_bet, next_bet * sb_factor)
            logger.info("STREAKBET_LOSS: current_bet ×%.2f → %.8f (loss streak %d)",
                        sb_factor, next_bet, state.current_streak)
        state.current_bet = next_bet
        last_result = bet_state

        # ── cycle-time metric (EMA-smoothed, informs Efficiency + overhead) ──
        _record_cycle_ms((time.monotonic() - loop_start) * 1000)

# ══════════════════════════════════════════════════════════
#  PHASE 5b: PIPELINED BETTING (Flat-only)
# ══════════════════════════════════════════════════════════
#
# Mirrors tg/engine.py:_pipelined_betting_loop but sync + threaded to fit the
# CLI's existing model (requests + threading, no asyncio). Guardrail forces
# depth=1 for anything other than Flat on a non-auto game: sequential
# strategies (Martingale, D'Alembert, Paroli, WolfRider, Delay-M, Rule-Based)
# depend on outcome ordering, and auto-bet is server-side single-flight.
_PIPELINE_MAX = 8


def _effective_pipeline_depth() -> int:
    """Clamp state.pipeline_depth against guardrails. Sets state.pipeline_reason.

    Returns 1 when the loop must run serially (no threading overhead).
    """
    requested = int(state.pipeline_depth or 1)
    if requested <= 1:
        state.pipeline_reason = ""
        return 1
    if is_auto_game(state.game):
        state.pipeline_reason = f"forced to 1: auto-bet game ({state.game}) is server-side single-flight"
        return 1
    if state.strategy_key != "1":
        state.pipeline_reason = f"forced to 1: {state.strategy} needs outcome-ordered bets (Flat-only)"
        return 1
    if _hard_cap_on():
        state.pipeline_reason = "forced to 1: the pre-bet loss cap checks one bet at a time"
        return 1
    state.pipeline_reason = ""
    return min(requested, _PIPELINE_MAX)


def _run_betting_loop():
    """Dispatcher: selects serial or pipelined loop based on effective depth."""
    depth = _effective_pipeline_depth()
    state.pipeline_effective = depth
    if depth <= 1:
        if state.pipeline_reason:
            logger.info("PIPELINE %s", state.pipeline_reason)
        return betting_loop()
    logger.info("PIPELINE running depth=%d (Flat)", depth)
    return pipelined_betting_loop(depth)


def _prepare_next_bet_flat() -> dict:
    """Snapshot the exact payload for a single Flat bet under state.lock.

    Returns {"amount": float, "bet_no": int}: bet_no is monotonic dispatch order.
    Never mutates strategy state (Flat has none). Records the streak_delay if any.
    """
    with state.lock:
        state.pipeline_dispatched += 1
        return {
            "amount": state.current_bet,
            "bet_no": state.pipeline_dispatched,
        }


def _one_bet_worker(payload: dict) -> dict:
    """Worker thread body: places a single bet via existing api_place_bet.

    Returns a result envelope; api errors surface as {"result": None}. Never
    raises: the outcome handler decides how to react.
    """
    try:
        result = api_place_bet(payload["amount"])
    except Exception as e:                                # pragma: no cover: defensive
        logger.exception("PIPELINE worker crashed: %s", e)
        result = None
    return {"payload": payload, "result": result}


def _apply_outcome_locked(envelope: dict) -> None:
    """Update counters + persist + strategy hooks for one bet outcome.

    Full mirror of the serial loop's post-bet block, wrapped in state.lock so
    concurrent workers can't race total_bets / wagered / profit / streaks.
    """
    result = envelope["result"]
    if result is None:
        # api_place_bet already logged + set last_error under its own lock.
        with state.lock:
            state.consecutive_errors += 1
            state.backoff_delay = min(state.backoff_delay * 2, state.max_backoff)
        return

    bet_data     = result.get("bet", {})
    balance_data = result.get("userBalance", {})
    bet_state    = bet_data.get("state", "loss")
    result_value = float(bet_data.get("result_value", 0))
    amount_used  = float(bet_data.get("amount", envelope["payload"]["amount"]))
    new_balance  = float(balance_data.get("amount", state.current_balance))
    raw_profit   = float(bet_data.get("profit", 0))

    with state.lock:
        state.consecutive_errors = 0
        state.backoff_delay      = 1.0
        state.consecutive_recovery_fails = 0

        state.total_bets     += 1
        state.wagered        += amount_used
        state.profit         += raw_profit
        state.current_balance = new_balance

        if bet_state == "win":
            state.wins += 1
            if raw_profit > state.highest_win:
                state.highest_win = raw_profit
            state.last_broken_loss_streak = max(-state.current_streak, 0)
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

        _check_profit_increment()

        if state.total_bets % 5 == 0:
            state.profit_history.append(state.profit)

        state.recent_bets.append({
            "n":     state.total_bets,
            "time":  datetime.now().isoformat(),
            "amt":   amount_used,
            "roll":  result_value,
            "state": bet_state,
            "pnl":   raw_profit,
            "bal":   new_balance,
        })

        now = time.time()
        elapsed = now - state.session_start
        if elapsed > 0:
            state.bets_per_second = state.total_bets / elapsed
            state.bets_per_minute = state.bets_per_second * 60

        sec_key = int(now)
        min_key = int(now) // 60
        if sec_key != state._current_sec:
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

    _db_save_bet_dispatch(bet_data, raw_profit, new_balance)

    # Rule-Based never reaches here (guardrail forces serial), but keep the
    # milestone hook for parity with the serial loop.
    ms_reason = _check_milestone(bet_state)
    if ms_reason:
        logger.info("MILESTONE: %s (balance %.8f, profit %+.8f)",
                    ms_reason, state.current_balance, state.profit)
        with state.lock:
            state.status = f"Milestone: {ms_reason}"


def pipelined_betting_loop(depth: int):
    """Flat-only pipelined bet loop.

    Concurrency: `depth` in-flight bets via ThreadPoolExecutor + Semaphore.
    Ordering: outcomes apply in COMPLETION order (Flat's strategy state
    doesn't depend on ordering: every bet is base_bet).
    """
    executor = ThreadPoolExecutor(max_workers=depth, thread_name_prefix="wb-pipe")
    sem = threading.Semaphore(depth)
    inflight_lock = threading.Lock()

    def _done(fut):
        try:
            envelope = fut.result()
            _apply_outcome_locked(envelope)
        except Exception as e:                            # pragma: no cover
            logger.exception("PIPELINE done_callback crash: %s", e)
        finally:
            with inflight_lock:
                state.pipeline_in_flight -= 1
            sem.release()

    try:
        while state.running:
            loop_start = time.monotonic()

            if state.paused:
                state.status = "PAUSED: press [R] to resume"
                time.sleep(0.4)
                continue

            if state.rate_wait:
                # Drain in-flight before honoring the probe so no worker
                # dispatches into the cooldown window.
                backoff = state.rate_backoff
                state.status = f"Rate limit: draining pipeline, resuming in {backoff:.0f}s..."
                while state.pipeline_in_flight > 0:
                    time.sleep(0.05)
                time.sleep(backoff)
                with state.lock:
                    state.rate_wait      = False
                    state.rate_backoff   = 2.0
                    state.rate_remaining = 50
                logger.info("Rate limit cooldown done, resuming pipelined betting")
                continue

            stop, reason = should_stop()
            if stop:
                # Drain workers before ending so counters + DB rows are consistent.
                while state.pipeline_in_flight > 0:
                    time.sleep(0.05)
                if _should_recurring_restart(reason):
                    logger.info("RECURRING RESTART after: %s", reason)
                    _db_save_session()
                    _reset_session_for_recurring(_restart_delay_for(reason), reason)
                    with state.lock:
                        state.pipeline_dispatched = 0
                    continue
                state.running = False
                state.status  = f"STOPPED: {reason}"
                _db_save_session()
                break

            if state._insufficient_balance:
                while state.pipeline_in_flight > 0:
                    time.sleep(0.05)
                state.running = False
                state.status = "STOPPED: Insufficient balance"
                _db_save_session()
                break

            sd = _get_streak_delay()
            if sd > 0:
                state.status = f"Streak delay {sd:.2f}s (streak {state.current_streak})"
                time.sleep(sd)

            if state.basebet_pct > 0 and state.total_bets > 0 and state.total_bets % 10 == 0:
                _apply_basebet_pct()

            if state.bet_delay > 0:
                time.sleep(state.bet_delay)
            if state.soft_pace_delay > 0:
                time.sleep(state.soft_pace_delay)

            payload = _prepare_next_bet_flat()

            if not sem.acquire(timeout=1.0):
                # No slot free within 1s: loop back to re-check stop/pause/rate.
                with state.lock:
                    state.pipeline_dispatched -= 1  # payload never dispatched
                continue

            state.status = f"Placing bet #{payload['bet_no']} (in-flight {state.pipeline_in_flight + 1}/{depth})"
            with inflight_lock:
                state.pipeline_in_flight += 1
            fut = executor.submit(_one_bet_worker, payload)
            fut.add_done_callback(_done)

            _record_cycle_ms((time.monotonic() - loop_start) * 1000)
    finally:
        # Best-effort drain on any exit path.
        while state.pipeline_in_flight > 0:
            time.sleep(0.05)
        executor.shutdown(wait=True)


# ══════════════════════════════════════════════════════════
#  DATABASE  I/O
# ══════════════════════════════════════════════════════════
def _db_conn():
    return db_connect(DB_PATH)


# ── Phase 5a: session read + export helpers ───────────────────────
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


# ── Phase 5c: backtest / replay data sources ─────────────────────
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
            state.last_broken_loss_streak = max(-state.current_streak, 0)
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


def backtest_loop(source, progress_every: int = 100, progress=None, planned: int = 0) -> str:
    """Run the strategy over `source` (iterator of bet-result dicts) with no API calls.

    Honours should_stop() and state.running. Stops are checked before a roll is
    drawn, so sessions can run back to back on one source without losing a roll.
    Returns the stop reason, or "" when the rolls ran out or the run was halted.
    Prints progress every `progress_every` bets (0 disables). With `progress`
    and `planned`, calls progress(bets / planned) every 256 bets.
    """
    rolls = iter(source)
    while state.running:
        _hybrid_step()
        stop, reason = should_stop()
        if stop:
            state.status = f"[BACKTEST] STOPPED: {reason}"
            return reason
        result = next(rolls, None)
        if result is None:
            break

        bet_state = _apply_backtest_result(result)

        # Strategy hooks: reuse from betting_loop path
        _check_milestone(bet_state)
        active_key = _active_strategy_key()
        if active_key == "6":
            wolfrider_switch()
        if active_key == "8" and state.custom_rules:
            apply_rules(bet_state)

        # Next-bet computation with Phase 2 streakbet reduction
        raw_next = compute_next_bet(bet_state)
        floor_bet = _active_base_bet()
        next_bet = max(floor_bet, raw_next)
        sb_factor = _get_streakbet_loss_factor()
        if sb_factor != 1.0:
            next_bet = max(floor_bet, next_bet * sb_factor)
        state.current_bet = next_bet

        state.status = f"[BACKTEST] bet {state.total_bets}: {bet_state}  bal {state.current_balance:.8f}  p/l {state.profit:+.8f}"
        if progress is not None and planned and state.total_bets % 256 == 0:
            progress(state.total_bets / planned)
        if progress_every > 0 and state.total_bets % progress_every == 0:
            console.print(f"  [dim][BACKTEST][/] {state.total_bets} bets  "
                          f"W:{state.wins} L:{state.losses}  "
                          f"P/L: [{'green' if state.profit >= 0 else 'red'}]"
                          f"{'+' if state.profit >= 0 else ''}{state.profit:.8f}[/]  "
                          f"bal: {state.current_balance:.8f}")
    return ""


_BACKTEST_ROLL_BUDGET = 5_000_000
_BACKTEST_SESSION_ENDS = ("Profit target", "Max bets", "Max wins", "Max loss hit",
                          LOSS_CAP_REASON, STREAK_RESET_REASON)
_RUN_ROLLS_OUT = "the rolls ran out"
_RUN_RULE_STOP = "a strategy rule stopped it"
# The game pays 99% back at every multiplier (see _derive_bet_value).
_HOUSE_EDGE = 0.01


def _backtest_new_session(balance: Optional[float] = None) -> None:
    """Zero the in-memory session counters for a backtest session.

    With `balance` the session starts from that balance; without it the balance
    carries over from the session before, as it does between live sessions.
    """
    if balance is not None:
        state.current_balance = balance
    state.session_start    = time.time()
    state.total_bets       = 0
    state.wins             = 0
    state.losses           = 0
    state.profit           = 0.0
    state.wagered          = 0.0
    state.current_streak   = 0
    state.max_win_streak   = 0
    state.max_loss_streak  = 0
    state.last_broken_loss_streak = 0
    state.highest_bet      = 0.0
    state.highest_win      = 0.0
    state.biggest_loss     = 0.0
    state.start_balance    = state.current_balance
    state.highest_balance  = state.current_balance
    state.lowest_balance   = state.current_balance
    state.current_bet      = state.base_bet
    state.hybrid_phase     = PHASE_BASE
    state.hybrid_switches  = 0
    state.dalembert_unit   = 0
    state.paroli_count     = 0


def _can_pay_next_bet() -> bool:
    """True while the balance covers the next bet; the casino refuses a larger one."""
    return round(state.current_bet, 8) <= state.current_balance + HALF_SAT


def _affordable_rolls(rolls):
    """Pass rolls through for as long as the balance can pay the next bet."""
    while _can_pay_next_bet():
        roll = next(rolls, None)
        if roll is None:
            return
        yield roll


def _run_backtest_sessions(source, sessions: Optional[int], runs: int, balance: float,
                           progress=None) -> dict:
    """Play sessions back to back on one roll source and total the results.

    A session that ends on its own stop (cash-out, loss cap, bet or win count)
    is followed by a new one, whatever the live restart settings say. A run
    ends at the balance goal, at the balance floor, when the next bet cannot
    be paid, after `sessions` sessions, or when the rolls run out. `progress`,
    when given, is called with the share of runs (and sessions) done.
    """
    totals = {"sessions": 0, "green": 0, "won": 0.0, "lost": 0.0, "worst": 0.0,
              "net": 0.0, "wagered": 0.0, "bets": 0, "switches": 0,
              "runs": {}, "finals": []}
    for run in range(runs):
        outcome = None
        rolls = _affordable_rolls(source)
        _backtest_new_session(balance)
        played = 0
        while outcome is None:
            reason = backtest_loop(rolls, progress_every=0)
            totals["net"] += state.profit
            totals["wagered"] += state.wagered
            totals["bets"] += state.total_bets
            totals["switches"] += state.hybrid_switches
            if reason.startswith(_BACKTEST_SESSION_ENDS) and state.total_bets > 0:
                totals["sessions"] += 1
                played += 1
                if state.profit > 0:
                    totals["green"] += 1
                    totals["won"] += state.profit
                else:
                    totals["lost"] += state.profit
                totals["worst"] = min(totals["worst"], state.profit)
                if sessions is not None and played >= sessions:
                    outcome = "played every session"
                else:
                    _backtest_new_session()
                if progress is not None and sessions:
                    progress((run + min(played, sessions) / sessions) / runs)
            elif not reason and not state.running:
                outcome = _RUN_RULE_STOP
            elif not reason and not _can_pay_next_bet():
                outcome = "balance too low for the next bet"
            elif not reason:
                outcome = _RUN_ROLLS_OUT
            elif reason.startswith(BALANCE_GOAL_REASON):
                outcome = "reached the balance goal"
            elif reason.startswith("Balance floor"):
                outcome = "at the balance floor"
            else:
                outcome = reason
        totals["runs"][outcome] = totals["runs"].get(outcome, 0) + 1
        totals["finals"].append(state.current_balance)
        if progress is not None:
            progress((run + 1) / runs)
        if outcome in (_RUN_ROLLS_OUT, _RUN_RULE_STOP):
            break
    return totals


def _backtest_sessions_data(totals: dict, balance: float) -> dict:
    """The multi-session backtest figures the result table shows (None where it shows "none")."""
    n, green = totals["sessions"], totals["green"]
    red = n - green
    wagered = totals["wagered"]
    finals = sorted(totals["finals"])
    many = len(finals) > 1
    return {
        "sessions": n, "runs": len(finals), "green": green,
        "green_pct": green / n * 100 if n else None,
        "avg_win": totals["won"] / green if green else None,
        "avg_loss": totals["lost"] / red if red else None,
        "worst": totals["worst"],
        "bets": totals["bets"], "bets_per_session": totals["bets"] / n if n else None,
        "switches": totals["switches"],
        "switches_per_session": totals["switches"] / n if n else None,
        "wagered": wagered, "net": totals["net"],
        "net_per_100": totals["net"] / wagered * 100 if wagered else None,
        "long_run_cost": -_HOUSE_EDGE * wagered,
        "start_balance": balance,
        "final_balance": finals[0] if len(finals) == 1 else None,
        "final_balance_typical": finals[len(finals) // 2] if many else None,
        "final_balance_unlucky": finals[len(finals) // 20] if many else None,
        "final_balance_lucky": finals[-1 - len(finals) // 20] if many else None,
        "runs_ended": dict(sorted(totals["runs"].items(), key=lambda item: -item[1])),
    }


def _print_backtest_table(totals: dict, balance: float) -> None:
    """Print the multi-session backtest result: session odds, cost and how the runs ended."""
    d = _backtest_sessions_data(totals, balance)
    n, green = d["sessions"], d["green"]
    tbl = Table(title="Backtest result", title_style="bold cyan", show_header=False)
    tbl.add_column(style="bold yellow", no_wrap=True)
    tbl.add_column(style="cyan")
    tbl.add_row("Sessions finished", f"{n:,} in {d['runs']:,} run(s)")
    tbl.add_row("Green sessions", f"{d['green_pct']:.2f}%  ({green:,} of {n:,})" if n else "none")
    tbl.add_row("Average win", f"{d['avg_win']:+.8f}" if d["avg_win"] is not None else "none")
    tbl.add_row("Average loss", f"{d['avg_loss']:+.8f}" if d["avg_loss"] is not None else "none")
    tbl.add_row("Worst session", f"{d['worst']:+.8f}")
    tbl.add_row("Bets per session", f"{d['bets_per_session']:.1f}" if n else str(d["bets"]))
    tbl.add_row("Phase switches per session",
                f"{d['switches_per_session']:.1f}" if n else str(d["switches"]))
    tbl.add_row("Wagered", f"{d['wagered']:.8f}")
    tbl.add_row("Net result", f"{d['net']:+.8f}"
                + (f"  ({d['net_per_100']:+.2f} per 100 wagered)" if d["net_per_100"] is not None else ""))
    tbl.add_row("Long-run cost of that wagering", f"{d['long_run_cost']:+.8f}  (-1.00 per 100 wagered)")
    if d["final_balance"] is not None:
        tbl.add_row("Balance", f"{balance:.8f} to {d['final_balance']:.8f}")
    elif d["final_balance_typical"] is not None:
        tbl.add_row("Final balance", f"typical {d['final_balance_typical']:.8f}, "
                                     f"unlucky 1 in 20 {d['final_balance_unlucky']:.8f}, "
                                     f"lucky 1 in 20 {d['final_balance_lucky']:.8f}")
    for outcome, count in d["runs_ended"].items():
        tbl.add_row(f"Runs ended: {escape(outcome)}", f"{count:,}")
    console.print(tbl)
    if not state.recurring:
        console.print("[dim]  Live, recurring is off: the bot stops after its first session.[/]")
    elif not state.restart_after_loss:
        console.print("[dim]  Live, the bot stops after its first losing session "
                      "(restart_after_loss is off).[/]")
    console.print("[dim]  The net result of a short run is partly luck. The long-run cost is 1% of "
                  "the amount wagered, whatever the settings.[/]")


def _run_backtest(args) -> None:
    """Run a backtest without writing to the live log.

    Each simulated phase switch would otherwise log a warning, and a 200,000
    session backtest would bury the live bot's timeline. Errors still log.
    """
    previous_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        _run_backtest_body(args)
    finally:
        logger.setLevel(previous_level)


def _json_progress(interval: float = 0.5):
    """A progress(fraction, final=False) callback writing {"progress": x} lines to stderr.

    At most one line per `interval` seconds, so a long backtest streams about 2 lines
    a second; final=True always writes.
    """
    last = [None]

    def report(fraction: float, final: bool = False) -> None:
        now = time.monotonic()
        if not final and last[0] is not None and now - last[0] < interval:
            return
        last[0] = now
        sys.stderr.write(json.dumps({"progress": round(min(1.0, max(0.0, fraction)), 4)}) + "\n")
        sys.stderr.flush()
    return report


def _backtest_session_data() -> dict:
    """One backtest session's figures, the ones the session summary prints."""
    bets = state.total_bets
    low = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
    return {"bets": bets, "wins": state.wins, "losses": state.losses,
            "win_rate_pct": state.wins / bets * 100 if bets else 0.0,
            "profit": state.profit, "wagered": state.wagered,
            "avg_pnl": state.profit / bets if bets else 0.0,
            "start_balance": state.start_balance, "end_balance": state.current_balance,
            "highest_balance": state.highest_balance, "lowest_balance": low,
            "highest_win": state.highest_win, "biggest_loss": state.biggest_loss,
            "max_win_streak": state.max_win_streak, "max_loss_streak": state.max_loss_streak}


def _run_backtest_body(args) -> None:
    """Backtest entry point: load config → build source → run backtest_loop → summary.

    Config source is (in order): explicit --preset NAME, else the wizard's
    saved ~/.wolfbet_autobot.json, else sensible defaults. Never touches the
    DB except to read historical bets in replay mode. With --sessions or --runs
    it plays sessions back to back and prints the result table; otherwise one
    session and the session summary. With --json the console stays silent,
    progress goes to stderr as JSON lines and the result (or error) is one
    JSON document on stdout; exit codes are the same.
    """
    as_json = bool(getattr(args, "json", False))
    if as_json:
        console.quiet = True

    def fail(code: str, message: str, exit_code: int, rich_line: str) -> None:
        if as_json:
            _print_json({"error": {"code": code, "message": message}})
        else:
            console.print(rich_line)
        sys.exit(exit_code)

    console.clear()
    console.print("[bold cyan]🧪 BACKTEST MODE[/]  [dim]- no API calls, no real balance risk[/]\n")
    init_db()  # needed for _read_bets in replay; harmless otherwise

    # 1. Load config
    if args.preset:
        if not load_preset(args.preset):
            fail("preset_not_found", f"Preset '{args.preset}' not found.", 1,
                 f"[red]Preset '{args.preset}' not found.[/]")
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

    _exit_on_session_rule_errors(as_json=as_json)
    multi = args.sessions is not None or args.runs is not None
    balance = 1.0 if args.balance is None else args.balance     # $1 sandbox default
    if not (math.isfinite(balance) and balance > 0) \
            or (args.sessions is not None and args.sessions < 1) \
            or (args.runs is not None and args.runs < 1):
        message = "--balance must be above 0; --sessions and --runs must be 1 or more."
        fail("invalid_argument", message, 2, f"[red]{message}[/]")

    # 2. Initialise virtual session state (in-memory only)
    state.session_id       = -1
    state.running          = True
    state.paused           = False
    _backtest_new_session(balance)
    if state.custom_rules_text:
        state.custom_rules = _load_rules_from_text(state.custom_rules_text)

    # 3. Build the outcome source
    if args.replay_session:
        # The replay source is a generator, so count the bets now: an empty
        # session must be refused here, not as a failure halfway through the run.
        planned = len(_read_bets(args.replay_session))
        if not planned:
            message = f"Session #{args.replay_session} has no bets to replay"
            fail("replay_empty", message, 1, f"[red]✗ {message}[/]")
        source = _replay_bets_from_db(args.replay_session, lambda: state.current_bet)
        console.print(f"  [dim]Replaying session[/] [cyan]#{args.replay_session}[/]\n")
        source_info = {"type": "replay", "session_id": args.replay_session}
    else:
        n = args.simulate or (_BACKTEST_ROLL_BUDGET if multi else 1000)
        rng = random.Random(args.seed) if args.seed is not None else random.Random()
        seed_str = f"seed={args.seed}" if args.seed is not None else "seed=system-entropy"
        console.print(f"  [dim]Simulating{' up to' if multi else ''}[/] [cyan]{n}[/] "
                      f"[dim]bets with[/] [cyan]{seed_str}[/]\n")

        def _gen():
            for _ in range(n):
                yield _simulate_bet_outcome(
                    state.multiplier, state.rule, state.current_bet,
                    state.current_balance, rng,
                )
        source = _gen()
        source_info = {"type": "simulate", "bets": n, "seed": args.seed}
        planned = n

    progress = _json_progress() if as_json else None
    head = {"source": source_info,
            "strategy": {"name": state.strategy, "base_bet": state.base_bet,
                         "multiplier": state.multiplier, "rule": state.rule}}

    # 4. Run the loop (synchronous: fast enough for CLI, no thread noise)
    console.print(f"  [dim]Strategy:[/] [bold]{state.strategy}[/]  "
                  f"[dim]base_bet:[/] {state.base_bet:.8f}  "
                  f"[dim]multiplier:[/] {state.multiplier}x  "
                  f"[dim]rule:[/] {state.rule}\n")
    if multi:
        runs = args.runs or 1
        if args.replay_session and runs > 1:
            console.print("  [dim]A replay has one set of rolls: running it once.[/]")
            runs = 1
        try:
            totals = _run_backtest_sessions(source, args.sessions, runs, balance, progress=progress)
        except Exception as e:
            detail = f": {e}" if APP_ENV != "production" else ""
            logger.exception("BACKTEST error")
            fail("backtest_failed", f"Backtest failed{detail}. Check the log and try again.", 1,
                 f"[red]Backtest failed{detail}. Check the log and try again.[/]")
        if as_json:
            progress(1.0, final=True)
            _print_json({"mode": "sessions", **head,
                         "results": _backtest_sessions_data(totals, balance)})
            return
        _print_backtest_table(totals, balance)
        return
    try:
        reason = backtest_loop(source, progress_every=0 if as_json else max(100, (args.simulate or 1000) // 20),
                               progress=progress, planned=planned)
    except Exception as e:
        logger.exception("BACKTEST error")
        if as_json:
            detail = f": {e}" if APP_ENV != "production" else ""
            fail("backtest_failed", f"Backtest failed{detail}. Check the log and try again.", 1, "")
        console.print(f"[red]✗ Backtest failed:[/] {e}")
        sys.exit(1)
    if as_json:
        progress(1.0, final=True)
        _print_json({"mode": "session", **head, "stop_reason": reason or None,
                     "summary": _backtest_session_data()})
        return

    # 5. Summary
    console.print()
    console.print(format_session_summary(f"Backtest complete: {state.total_bets} bets"))
    console.print()


class _ExportPathError(ValueError):
    """An export path that could redirect the write: a symlink, a non-file, or no such directory."""


def _export_target(path: str, session_id: int) -> tuple:
    """Where an export of `session_id` goes: (absolute file path, "csv" | "json").

    An existing directory gets wolfbet-session-<id>.csv inside it. Otherwise the
    file must sit in an existing directory and must not be a symlink or any other
    non-regular file, so the write can never land on a file somewhere else.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(path) and not os.path.islink(path):
        path = os.path.join(path, f"wolfbet-session-{session_id}.csv")
    if (not os.path.isdir(os.path.dirname(path)) or os.path.islink(path)
            or (os.path.exists(path) and not os.path.isfile(path))):
        raise _ExportPathError(
            "Export path must be a regular file in an existing directory (not a symlink)")
    return path, "json" if path.lower().endswith(".json") else "csv"


def _export_session(session_id: int, path: str) -> tuple:
    """Write a session + its bets to `path` (see _export_target). Format detected from extension.

    Returns (bet_count, format). Raises ValueError if the session is missing,
    _ExportPathError for an unsafe path, OSError if the file cannot be written.
    """
    path, fmt = _export_target(path, session_id)
    session = _read_session(session_id)
    if not session:
        raise ValueError(f"Session #{session_id} not found")
    bets = _read_bets(session_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
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

def _session_config_snapshot() -> str:
    """The settings a session starts with, as JSON, with proxy and events URL masked."""
    values = {}
    for field, _ in _CONFIG_SHOW_FIELDS:
        try:
            values[field] = _config_show_value(field, getattr(state, field))
        except AttributeError:
            continue
    return json.dumps(values, default=str, sort_keys=True)


def _db_start_session() -> int:
    conn = _db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions
            (started_at, currency, game, strategy, base_bet, multiplier, start_balance, lane,
             config_snapshot)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        state.currency, state.game, _strategy_label(),
        state.base_bet, state.multiplier, state.start_balance,
        getattr(state, "lane", 1),
        _session_config_snapshot(),
    ))
    conn.commit()
    sid = c.lastrowid
    conn.close()
    _emit_lifecycle("session_open", _session_open_data(sid))
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


# ══════════════════════════════════════════════════════════
#  v2.27.0: Async bet writer (kill the per-bet CLI overhead)
# ══════════════════════════════════════════════════════════
# Bounded queue prevents unbounded memory growth if writer stalls.
_bet_write_queue: queue.Queue = queue.Queue(maxsize=1000)
_bet_writer_thread: Optional[threading.Thread] = None
_bet_writer_conn: Optional[sqlite3.Connection] = None
_bet_writer_stop = threading.Event()
_bet_writer_stats = {
    "queued": 0,
    "flushed": 0,
    "batches": 0,
    "fallback_sync": 0,
    "queue_high_watermark": 0,
}

_BET_WRITER_BATCH_SIZE   = 32
_BET_WRITER_BATCH_WINDOW = 0.1   # seconds
_BET_WRITER_INSERT_SQL   = (
    "INSERT INTO bets "
    "(session_id, timestamp, amount, multiplier, rule, "
    " bet_value, result_value, state, profit, balance_after) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)"
)


def _open_bet_writer_conn():
    """Persistent, tuned SQLite connection for the async writer thread.

    Applied PRAGMAs (writer-only: never touches read connections):
    - synchronous=NORMAL: safe under WAL; skips per-commit fsync (30-40 ms saved).
    - busy_timeout=5000: WAL + concurrent readers can race; be patient.
    - cache_size=-32000: 32 MB in-memory cache.
    - temp_store=MEMORY: temp tables in RAM.
    """
    conn = db_connect(DB_PATH)  # WAL journal_mode already set by shared/core/database.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA cache_size = -32000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def _bet_writer_worker():
    """Drain _bet_write_queue in batches; one commit per batch."""
    global _bet_writer_conn
    try:
        _bet_writer_conn = _open_bet_writer_conn()
    except Exception as e:
        logger.error("bet writer failed to open DB (%s): falling back to sync mode", e)
        return
    while not (_bet_writer_stop.is_set() and _bet_write_queue.empty()):
        batch = []
        deadline = time.monotonic() + _BET_WRITER_BATCH_WINDOW
        while len(batch) < _BET_WRITER_BATCH_SIZE:
            timeout = max(0.0, deadline - time.monotonic())
            if timeout == 0:
                break
            try:
                batch.append(_bet_write_queue.get(timeout=timeout))
            except queue.Empty:
                break
        if not batch:
            if _bet_writer_stop.is_set() and _bet_write_queue.empty():
                break
            continue
        try:
            _bet_writer_conn.executemany(_BET_WRITER_INSERT_SQL, batch)
            _bet_writer_conn.commit()
            _bet_writer_stats["batches"] += 1
            _bet_writer_stats["flushed"] += len(batch)
        except Exception as e:
            logger.warning("bet writer batch failed (%d rows dropped): %s",
                           len(batch), e)
            try:
                _bet_writer_conn.close()
            except Exception:
                pass
            try:
                _bet_writer_conn = _open_bet_writer_conn()
            except Exception as reopen_err:
                logger.error("bet writer reopen failed: %s: exiting worker",
                             reopen_err)
                return
    # Final close on exit.
    if _bet_writer_conn is not None:
        try:
            _bet_writer_conn.close()
        except Exception:
            pass
        _bet_writer_conn = None


def _start_bet_writer():
    """Spawn the writer thread if not already running. Idempotent."""
    global _bet_writer_thread
    if _bet_writer_thread is not None and _bet_writer_thread.is_alive():
        return
    _bet_writer_stop.clear()
    _bet_writer_thread = threading.Thread(
        target=_bet_writer_worker, daemon=True, name="BetWriter",
    )
    _bet_writer_thread.start()


def _stop_bet_writer(timeout: float = 5.0):
    """Signal writer to drain remaining queue, then join. Bounded wait."""
    global _bet_writer_thread
    _bet_writer_stop.set()
    if _bet_writer_thread is not None and _bet_writer_thread.is_alive():
        _bet_writer_thread.join(timeout=timeout)
        if _bet_writer_thread.is_alive():
            logger.warning(
                "bet writer did not drain within %.1fs; %d rows may be lost",
                timeout, _bet_write_queue.qsize(),
            )
    _bet_writer_thread = None


def _make_bet_row(bet_data: dict, profit: float, balance: float) -> tuple:
    """Build the 10-tuple used by both sync and async INSERT paths."""
    return (
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
    )


def _db_save_bet_async(bet_data: dict, profit: float, balance: float):
    """Non-blocking bet-row enqueue for the writer thread.

    Overflow safety: if the bounded queue is full, fall back to the sync
    path so no bet is lost: the writer is stalled but data still lands.
    """
    row = _make_bet_row(bet_data, profit, balance)
    try:
        _bet_write_queue.put_nowait(row)
        _bet_writer_stats["queued"] += 1
        qs = _bet_write_queue.qsize()
        if qs > _bet_writer_stats["queue_high_watermark"]:
            _bet_writer_stats["queue_high_watermark"] = qs
    except queue.Full:
        _bet_writer_stats["fallback_sync"] += 1
        _db_save_bet(bet_data, profit, balance)


def _db_save_bet_dispatch(bet_data: dict, profit: float, balance: float):
    """One-line entry point used by both betting_loop and pipeline outcome.

    Routes to the async writer when state.db_write_mode == "async"; falls
    back to the legacy sync path otherwise. Preserves the existing
    call-site signature so no other code needs to change.
    """
    if state.db_write_mode == "async" and _bet_writer_thread is not None:
        _db_save_bet_async(bet_data, profit, balance)
    else:
        _db_save_bet(bet_data, profit, balance)
    pub = _event_publisher
    if pub is not None and pub.reserve():
        try:
            pub.emit_bet((
                state.session_id,
                float(bet_data.get("amount", 0)),
                float(bet_data.get("multiplier", 0)),
                state.rule,
                float(bet_data.get("bet_value", 0)),
                float(bet_data.get("result_value", 0)),
                bet_data.get("state", ""),
                profit,
                balance,
                state.profit,
                state.total_bets,
                state.current_streak,
                state._last_api_ms,
            ))
        except Exception:
            pass


_event_publisher: Optional[EventPublisher] = None


def _start_events() -> None:
    """Start the event publisher when events are enabled and a URL is set."""
    global _event_publisher
    if not state.events_enabled or _event_publisher is not None:
        return
    if not state.events_url:
        logger.warning("EVENTS enabled but no events_url is set: events stay off")
        return
    pub = EventPublisher(state.events_url, lane=state.lane)
    if pub.start():
        _event_publisher = pub
        logger.info("EVENTS publisher started (lane %d)", state.lane)
        _emit_lifecycle("start", {
            "version": VERSION, "lane": state.lane, "lanes_count": state.lanes_count,
            "game": state.game, "currency": state.currency, "strategy": state.strategy,
            "strategy_key": state.strategy_key, "base_bet": state.base_bet,
            "multiplier": state.multiplier, "rule": state.rule,
            "db_write_mode": state.db_write_mode, "pipeline_depth": state.pipeline_depth,
            "recurring": state.recurring,
        })
        if state.session_id:
            _emit_lifecycle("session_open", _session_open_data(state.session_id))


def _emit_lifecycle(kind: str, data: dict) -> None:
    pub = _event_publisher
    if pub is not None:
        pub.emit_lifecycle(kind, data)


def _session_open_data(session_id: int) -> dict:
    return {"session_id": session_id, "game": state.game, "currency": state.currency,
            "strategy": state.strategy, "base_bet": state.base_bet,
            "multiplier": state.multiplier, "start_balance": state.start_balance}


def _session_close_data() -> dict:
    return {"session_id": state.session_id, "total_bets": state.total_bets,
            "wins": state.wins, "losses": state.losses, "profit": state.profit,
            "wagered": state.wagered, "end_balance": state.current_balance}


_EVENTS_STATE_EVERY_S = 2.0
_events_next_state = 0.0
_events_last_paused = False


def _events_tick(snapshot: Optional[dict] = None) -> None:
    """Send the state snapshot every 2 s and pause/resume transitions, from the main loop."""
    global _events_next_state, _events_last_paused
    pub = _event_publisher
    if pub is None:
        return
    paused = bool(state.paused)
    if paused != _events_last_paused:
        _events_last_paused = paused
        pub.emit_lifecycle("pause" if paused else "resume", {})
    now = time.monotonic()
    if now < _events_next_state:
        return
    _events_next_state = now + _EVENTS_STATE_EVERY_S
    try:
        pub.emit_state(snapshot if snapshot is not None else _state_snapshot())
    except Exception:
        pass


def _emit_session_end() -> None:
    """Final events for a stopping bot: terminal error, session totals, stop reason."""
    if state._insufficient_balance:
        _emit_lifecycle("error", {"reason": "Insufficient balance"})
    _emit_lifecycle("session_close", _session_close_data())
    _emit_lifecycle("stop", {"reason": state.status})


def _stop_events() -> None:
    """Flush and stop the event publisher if it is running."""
    global _event_publisher
    pub, _event_publisher = _event_publisher, None
    if pub is not None:
        pub.stop()


def _db_save_session():
    try:
        conn = _db_conn()
        lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
        # v2.28.4: when a session is in the recurring pause, freeze ended_at
        # at bet-end time (session_bet_ended_at) so the DB reflects the real
        # betting window instead of tracking the sleep.
        _ended_at_ts = state.session_bet_ended_at or time.time()
        _ended_at_iso = datetime.fromtimestamp(_ended_at_ts).isoformat()
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
            _ended_at_iso,
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
def _state_snapshot() -> dict:
    """Live session state, as written to the state file and sent as the state event."""
    pub = _event_publisher
    return {
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
        # v2.30.1: actionable profit-increment target (distinct from
        # last_profit_milestone which is the notification tracker).
        "next_profit_milestone": state.next_profit_milestone,
        # Phase 3 observability metrics
        "api_ms":              state._last_api_ms,
        "api_avg_ms":          _compute_api_avg(),
        "api_peak_ms":         state.peak_api_ms,
        "api_p50_ms":          _compute_api_p50(),
        "api_p99_ms":          _compute_api_p99(),
        "on_target_pct":       _compute_on_target_pct(),
        "cycle_ms":            state._cycle_ms,
        "overhead_ms":         state._cycle_overhead_ms,
        # v2.30.4: windowed cycle metrics + recent BPS for consistent
        # efficiency computation. cycle_ms above is aliased to cycle_p50
        # for backwards-compat readers.
        "cycle_p50_ms":        _compute_cycle_p50(),
        "cycle_p99_ms":        _compute_cycle_p99(),
        # v2.30.5: mean cycle drives Recent BPS + efficiency (was p50).
        # Same statistic on both sides of the ratio; no more >100% edge.
        "cycle_mean_ms":       _compute_cycle_mean(),
        "cycle_sample_count":  len(state._cycle_samples),
        "bets_per_second_recent": _compute_recent_bps(),
        "efficiency_pct":      _compute_efficiency_pct(),
        "theoretical_max_bps": _compute_theoretical_max_bps(),
        "pipeline_effective":  state.pipeline_effective,
        "pipeline_dispatched": state.pipeline_dispatched,
        # v2.27.0: async bet-writer visibility (surfaced by --diagnose)
        "db_write_mode":       state.db_write_mode,
        "db_writer_stats":     dict(_bet_writer_stats),
        "db_writer_queue":     _bet_write_queue.qsize(),
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
        "hybrid_phase": state.hybrid_phase if _hybrid_on() else None,
        "profit_increment": state.profit_increment,
        "profit_threshold": state.profit_threshold,
        # rule-based info
        "rule_count": len(state.custom_rules) if state.strategy_key == "8" else 0,
        "rule_descriptions": [r.description for r in state.custom_rules] if state.strategy_key == "8" else [],
        # v2.28.4: uptime freezes at bet-end when session is in recurring
        # pause, so "5 min bet + 1 h pause" no longer displays as "1 h 5 min
        # session". Pause remaining is surfaced separately for the monitor.
        "uptime_sec": int(
            (state.session_bet_ended_at or time.time()) - state.session_start
        ),
        # v2.34.0: wall-clock time since bot start (pauses included)
        "run_uptime_sec": int(time.time() - state.run_started_at),
        # v2.35.0: parallel lanes + shared throttle state
        "lane": state.lane,
        "lanes_count": state.lanes_count,
        "lane_share": state.lane_share,
        "lane_split": state.lane_split,
        "lane_name": state.lane_name,
        "pending_restart": state.pending_restart,
        # v2.37.0: run-wide profit increment
        "profit_increment_scope": state.profit_increment_scope,
        "run_profit_total": run_profit_total(),
        "run_first_session": state.run_first_session,
        "run_bumps": state.run_bumps,
        "lane_equity": lane_equity(),
        "soft_pace_delay": state.soft_pace_delay,
        "pause_remaining_sec": (
            max(0, int(state.session_pause_total - (time.time() - state.session_pause_started_at)))
            if state.session_pause_started_at else 0
        ),
        "recurring_delay_sec": int(state.recurring_delay),
        # Length of the pause in progress (streak reset has its own delay).
        "pause_total_sec": int(state.session_pause_total),
        "streak_reset_losses": state.streak_reset_losses,
        "streak_reset_delay_sec": int(state.streak_reset_delay),
        "status": state.status,
        "last_error": state.last_error,
        "events": dict({"enabled": state.events_enabled, "running": pub is not None},
                       **(pub.stats() if pub is not None else {})),
        "updated_at": datetime.now().isoformat(),
    }


def _save_state_file():
    """Write live session state to JSON so --status can read it. Returns the dict written."""
    try:
        data = _state_snapshot()
        fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        return data
    except Exception:
        return None

def _cleanup_state_file():
    for p in (STATE_PATH, PID_PATH):
        try:
            os.remove(p)
        except OSError:
            pass

# ══════════════════════════════════════════════════════════
#  TUI: BORDERLESS ULTRA-COMPACT DASHBOARD
# ══════════════════════════════════════════════════════════
SPARKLINE = "▁▂▃▄▅▆▇█"

def _sparkline(values) -> str:
    if not values or len(values) < 2:
        return "-"
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
      Row 9..N-2:  Recent bets (flex: fills remaining, padded with blanks)
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

    run_seg = f"  Run {_fmt_duration(time.time() - state.run_started_at)}"
    hdr_text = f"WolfBet v{VERSION}  #{state.session_id}  {uptime}{run_seg}  "
    # visible chars used: 4 + len(hdr_text) + len("● STATUS") + 1
    hdr_vis = 4 + len(hdr_text) + len("● STOPPED") + 1
    fill = max(0, term_w - hdr_vis)
    add(f"{_a('dim_blue', '─── ')}{_a('bold_cyan', f'WolfBet v{VERSION}')}"
        f"{_a('white', f'  #{state.session_id}  {uptime}{run_seg}')}  {st_ind} "
        f"{_a('dim_blue', '─' * fill)}")

    # ── Row 2: Balance bar ────────────────────────────
    p = state.profit
    ps = "+" if p >= 0 else ""
    pc = "bold_green" if p >= 0 else "bold_red"
    wr = (state.wins / state.total_bets * 100) if state.total_bets else 0
    wr_c = "bold_green" if wr >= 50 else "bold_red"
    # ROI %: profit / wagered × 100 (edge indicator).
    roi = (state.profit / state.wagered * 100) if state.wagered > 0 else 0.0
    roi_c = "dim" if state.wagered == 0 else ("bold_green" if roi >= 0 else "bold_red")

    add(f"{_COLORS['bg_blue']}{_BOLD}{_COLORS['white']} BAL {_RST} "
        f"{_a('bold_white', f'{state.current_balance:.8f} {state.currency.upper()}')}"
        f"  {_a('dim', 'PnL')} {_a(pc, f'{ps}{p:.8f}')}"
        f"  {_a('dim', 'WAG')} {_a('yellow', f'{state.wagered:.8f}')}"
        f"  {_a('dim', 'WR')} {_a(wr_c, f'{wr:.1f}%')}"
        f"  {_a('dim', 'ROI')} {_a(roi_c, f'{roi:+.2f}%')}")

    # ── Row 3: Stats line 1 ──────────────────────────
    cs = state.current_streak
    if cs > 0:
        sk = _a("green", f"W+{cs}")
    elif cs < 0:
        sk = _a("red", f"L{cs}")
    else:
        sk = _a("dim", "-")

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
           f"  {_a('cyan', _with_phase(state.strategy, state.hybrid_phase if _hybrid_on() else None))}"
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

    # ── Row 6: API timing + rate limit + Efficiency + Cycle (always shown) ──
    api_avg = _compute_api_avg()
    api_p50 = _compute_api_p50()
    api_p99 = _compute_api_p99()
    add(f" {_a('dim', 'API')} last {_a('cyan', f'{state._last_api_ms:.0f}ms')}"
        f"  p50 {_a('cyan', f'{api_p50:.0f}ms')}"
        f"  p99 {_a('cyan', f'{api_p99:.0f}ms')}"
        f"  avg {_a('cyan', f'{api_avg:.0f}ms')}"
        f"  peak {_a('cyan', f'{state.peak_api_ms:.0f}ms')}")

    rl_line = f" {_a('dim', 'Rate limit')} {_a('yellow', f'{state.rate_remaining}/{state.rate_limit}')} play"
    if state._auto_start_remaining is not None:
        # /auto/start quota row (auto-bet only): warn when ≤3 of 20 left.
        quota_c = "bold_red" if state._auto_start_remaining <= 3 else "yellow"
        rl_line += f"  · {_a(quota_c, f'{state._auto_start_remaining}/20')} auto-start"
    add(rl_line)

    if state.bet_delay > 0:
        ot = _compute_on_target_pct()
        ot_str = f"{ot:.0f}%" if ot is not None else "n/a"
        add(f" {_a('dim', 'API stable')} {_a('cyan', f'{api_p50:.0f}ms')} median"
            f"  · {_a('cyan', ot_str)} on-target  (API ≤ bet delay)")

    # Efficiency + Cycle: always visible (previously delay-gated only).
    tmax = _compute_theoretical_max_bps()
    eff = _compute_efficiency_pct()
    eff_c = 'green' if eff >= 90 else 'yellow'
    if state.bet_delay > 0:
        add(f" {_a('dim', 'Efficiency')} {_a(eff_c, f'{eff:.0f}%')}"
            f"  (max {_a('cyan', f'{tmax:.2f}')} bps @ delay={state.bet_delay:.3f}s)")
    else:
        add(f" {_a('dim', 'Efficiency')} {_a(eff_c, f'{eff:.0f}%')}"
            f"  (raw throughput, no delay)")

    cycle_ms = state._cycle_ms
    # v2.30.3: use the unified theoretical ceiling instead of `1000/cycle_ms`
    # (misleading for auto-games where cycle_ms is per outer iteration, not
    # per bet). Matches cmd_status + cmd_diagnose.
    bps_ceiling = _compute_theoretical_max_bps()
    note = "  (batched)" if is_auto_game(state.game) else ""
    add(f" {_a('dim', 'Cycle')} {_a('magenta', f'{cycle_ms:.0f}ms')}/bet"
        f"  (overhead {_a('cyan', f'{state._cycle_overhead_ms:.0f}ms')})"
        f"  {_a('dim', '→')} {_a('magenta', f'{bps_ceiling:.2f}')} bps{note}")

    # ── Pipeline row (only when pipelining is active) ──
    if state.pipeline_effective > 1:
        add(f" {_a('dim', 'Pipeline')} depth={_a('magenta', str(state.pipeline_effective))}"
            f"  · in-flight={_a('cyan', str(state.pipeline_in_flight))}"
            f"  · dispatched={_a('cyan', str(state.pipeline_dispatched))}")
    elif state.pipeline_depth > 1 and state.pipeline_reason:
        add(f" {_a('dim', 'Pipeline')} {_a('yellow', state.pipeline_reason)}")

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
            (ended   or "-").replace("T", " "),
            (cur or "").upper(),
            strat or "-",
            str(bets or 0),
            Text(f"{ps}{profit:.8f}" if profit else "0.00000000", style=pc),
            f"{wagered:.8f}" if wagered else "-",
            f"W{mws or 0}/L{mls or 0}",
        )

    console.print(t)
    console.print("\n[dim]  Press any key to return...[/]")
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
        ("srloss / srdelay",    "srloss 20",         "restart on win after N losses"),
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
    panel from the profit sign: matches the ✅/🛑 emoji policy from v2.16.9.
    """
    p = state.profit
    ps = "+" if p >= 0 else ""
    pe = "✅" if p >= 0 else "🛑"
    bal_change = state.current_balance - state.start_balance
    bc_s = "+" if bal_change >= 0 else ""
    avg_pnl = p / state.total_bets if state.total_bets > 0 else 0.0
    avg_s = "+" if avg_pnl >= 0 else ""
    wr = (state.wins / state.total_bets * 100) if state.total_bets > 0 else 0.0

    header = Rule(f"[bold]{pe} Session #{state.session_id} ended[/]  [dim]- {reason or 'Session ended'}[/]")

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
    if state.streak_reset_losses:
        cfg.add_row("Streak reset:",
                    f"after {state.streak_reset_losses}L, pause {state.streak_reset_delay:.0f}s")

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
    console.print("\n[dim]  Press any key to return...[/]")
    _getch()
    console.clear()
    state.paused = was_paused


def show_tweak():
    """Modal runtime tweak surface: parity with tg/handlers.cmd_tweak.

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
            state.status   = "Stopping: saving session..."
            logger.warning("SESSION END (user quit)  bets=%d  profit=%+.8f  balance=%.8f",
                        state.total_bets, state.profit, state.current_balance)
            _db_save_session()
            # Phase 5a: session summary before exit
            try:
                console.clear()
                console.print(format_session_summary("Stopped by user"))
                console.print("\n[dim]  Press any key to close...[/]")
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
            console.print("    [dim]Building replacement rule...[/]")
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
                # Type changed: reset related fields to defaults
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
            f"[bold cyan]WolfBet AutoBot  v{VERSION}[/]\n"
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
        console.print(f"    [dim]API key:[/]    [yellow]{saved_key[:8]}...{saved_key[-4:]}[/]")
        console.print()

    # ── Step functions ────────────────────────────────────
    def step_api_key():
        nonlocal balances, saved_key
        console.print(Rule("[bold cyan]Step 1 / 7: API Authentication[/]"))
        if saved_key:
            console.print(f"  Saved key: [yellow]{saved_key[:8]}...{saved_key[-4:]}[/]")
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

        console.print("  [dim]Testing connection...[/]")
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
        console.print(Rule("[bold cyan]Step 2 / 7: Currency[/]"))
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
        console.print(Rule("[bold cyan]Step 3 / 7: Bet Configuration[/]"))
        console.print("  [dim]Available games:[/]")
        for gk, label in GAME_LABELS.items():
            ep = GAME_ENDPOINTS[gk]
            console.print(f"    [yellow]{gk:14s}[/]: {label}  (rate limit: {ep[2]}/min)")
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
            # v2.30.9: derivation now lives in _derive_bet_value (single
            # source of truth shared with load_config + _apply_mutation).
            state.bet_value = _derive_bet_value(state.multiplier, state.rule)
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
        console.print(Rule("[bold cyan]Step 4 / 7: Betting Strategy[/]"))
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
            console.print("  [bold cyan]WolfRider[/]: your dedicated strategy:")
            console.print(f"    [green]• On win:[/]  reset to base bet")
            console.print(f"    [green]• On loss:[/] multiply by {state.loss_mult}x")
            console.print("    [green]• Switch over/under[/] on every 2nd, 6th, and 9th bet")
            console.print()

        elif v == "7":
            console.print()
            console.print("  [bold cyan]Delay Martingale[/]: flat bet through the first N")
            console.print("  consecutive losses, then start doubling.")
            console.print()
            d = _ask("  Losses before doubling starts", default=str(state.delay_martin_threshold))
            if d is _BACK:
                return False
            state.delay_martin_threshold = int(d)
            console.print(f"    [green]→ Flat for {state.delay_martin_threshold} losses, then Martingale[/]\n")

        elif v == "8":
            console.print()
            console.print("  [bold cyan]Rule-Based Strategy[/]: build custom conditions & actions")
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
                    # No existing rules: go straight to builder
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
                console.print("  [yellow]No rules configured: will use Flat bet.[/]\n")

        console.print()
        return True

    def step_stop_conditions():
        console.print(Rule("[bold cyan]Step 5 / 7: Stop Conditions[/]"))
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

        console.print("\n  [bold]Profit-based bet increment[/] [dim]- auto-raise base bet as profit grows[/]")
        v = _ask_optional(f"Profit threshold to trigger increment ({state.currency.upper()})", state.profit_threshold)
        if v is _BACK:
            return False
        state.profit_threshold = float(v) if v else None

        v = _ask_optional(f"Increment amount to add to base bet ({state.currency.upper()})", state.profit_increment)
        if v is _BACK:
            return False
        state.profit_increment = float(v) if v else None

        if state.profit_threshold and state.profit_increment:
            v = _ask("Measure profit per session or across the whole run? (session/run)",
                     default=state.profit_increment_scope or "session",
                     choices=list(PROFIT_INCREMENT_SCOPES) + ["back"])
            if v is _BACK or v == "back":
                return False
            state.profit_increment_scope = v

        # init milestone
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold

        console.print()
        return True

    def step_advanced_tuning():
        """Optional Phase 2 session tuning: recurring, streak delays, basebet_pct, milestones."""
        console.print(Rule("[bold cyan]Step 5b / 7: Advanced session tuning (optional)[/]"))
        gate = _ask("Configure advanced tuning? (y/N)",
                    default="y" if (state.recurring or state.streak_reset_losses or state.basebet_pct or state.streak_delay_loss[0] or
                                    state.streak_delay_win[0] or state.streak_delay_bets[0] or
                                    state.streakbet_loss[0]) else "n",
                    choices=["y", "n"])
        if gate.lower() != "y":
            console.print()
            return True

        console.print("  [dim]Enter to keep current  |  'none' to clear  |  'back' for previous step[/]\n")

        # Recurring: boolean gate; whitelisted stop-reasons drive the restart.
        rec = _ask("Recurring restart on non-loss stop? (y/N)",
                   default="y" if state.recurring else "n", choices=["y", "n"])
        state.recurring = (rec.lower() == "y")

        # Recurring delay: seconds to pause between cycles (TG parity, default 60).
        v = _ask_optional("Pause between recurring cycles in seconds (0 = back-to-back)",
                          state.recurring_delay if state.recurring_delay > 0 else None)
        if v is _BACK:
            return False
        try:
            # v2.30.6: cap raised 3600 → 86400 (24 h); no downstream break.
            state.recurring_delay = max(0.0, min(86400.0, float(v))) if v else 0.0
        except ValueError:
            state.recurring_delay = 60.0

        # Streak reset: restart on the first win after N+ losses in a row.
        v = _ask_optional("Restart session on first win after N losses in a row (blank/none = off)",
                          state.streak_reset_losses)
        if v is _BACK:
            return False
        try:
            state.streak_reset_losses = max(1, int(v)) if v else None
        except ValueError:
            state.streak_reset_losses = None
        if state.streak_reset_losses:
            v = _ask_optional("Pause before streak-reset restart in seconds",
                              state.streak_reset_delay if state.streak_reset_delay > 0 else None)
            if v is _BACK:
                return False
            try:
                state.streak_reset_delay = max(0.0, min(86400.0, float(v))) if v else 0.0
            except ValueError:
                state.streak_reset_delay = 600.0

        # basebet_pct: dynamic base bet as % of balance.
        v = _ask_optional("Base bet as % of balance (0-1, blank/none to disable)",
                          state.basebet_pct or None)
        if v is _BACK:
            return False
        try:
            state.basebet_pct = float(v) if v else 0.0
        except ValueError:
            state.basebet_pct = 0.0

        # Four streak-format fields: all use "N:X" grammar via _parse_streak_delay.
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

        # Phase 5b: pipeline depth (Flat-only, coerced at runtime for other strategies).
        v = _ask_optional("Pipeline depth (1 = serial / off, up to 8: Flat only)",
                          state.pipeline_depth if state.pipeline_depth > 1 else None)
        if v is _BACK:
            return False
        try:
            state.pipeline_depth = max(1, min(8, int(v))) if v else 1
        except ValueError:
            state.pipeline_depth = 1

        console.print()
        return True

    def step_preset():
        console.print(Rule("[bold cyan]Step 6 / 7: Presets[/]"))
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
        console.print(Rule("[bold green]Step 7 / 7: Review & Confirm[/]"))
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
            ("Max profit",      str(state.max_profit) if state.max_profit else "-"),
            ("Max loss",        str(state.max_loss)   if state.max_loss   else "-"),
            ("Max bets",        str(state.max_bets)   if state.max_bets   else "Unlimited"),
            ("Max wins",        str(state.max_wins)   if state.max_wins   else "Unlimited"),
            ("Min balance",     str(state.stop_on_balance) if state.stop_on_balance else "-"),
            ("Profit increment", f"+{state.profit_increment:.8f} every {state.profit_threshold} profit"
                if state.profit_increment is not None and state.profit_threshold else "-"),
        ]
        for k, val in rows:
            console.print(f"  [dim]{k:18s}[/] [yellow]{val}[/]")

        # Show rule details for strategy 8
        if state.strategy_key == "8" and state.custom_rules:
            console.print()
            for i, r in enumerate(state.custom_rules, 1):
                console.print(f"    [dim]{i}.[/] [green]{r.description}[/]")

        # ── v2.22.1: heads-up warnings for risky config combinations ──
        risk_warnings = _check_risky_config()
        if risk_warnings:
            console.print()
            body_lines = [Text.from_markup(f"[yellow]•[/] {w}") for w in risk_warnings]
            console.print(Panel(
                Group(*body_lines),
                title="[bold yellow]⚠️  Heads-up: review before starting[/]",
                border_style="yellow",
            ))
            console.print("[dim]  These are safety hints, not blockers. Press y to proceed anyway.[/]")

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
        ("Stop reason",     escape(state.status) if state.status else "-"),
        ("Total Bets",      str(state.total_bets)),
        ("Wins / Losses",   f"{state.wins} / {state.losses}"),
        ("Win Rate",        f"{state.wins/state.total_bets*100:.1f}%" if state.total_bets else "-"),
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

# ── Monitor layout budget ────────────────────────────
# Fixed rows produced by _build_monitor_screen (must match add() calls):
#   Row 1: session-title header (WolfBet v... #N uptime ● LIVE)
#   Row 2: BAL / PnL / WAG / WR
#   Row 3: Stats line 1 (Bets / W / L / Str / Best)
#   Row 4: Stats line 2 (Bet / Base / Hi / Strategy / mult / BPS / BPM / RL / Speed)
#   Row 5: s5 (Peak / Low / BestW / WorstL / Avg / Bal)
#   Row 6: Config-progress row (Ms/MaxP/MaxL/MaxB/MaxW/MinBal/Rec)
#   Row 7: top separator
#   Row 8: bets-table header
#   Row N-2: bottom separator
#   Row N-1: MONITOR + keys
#   Row N:   v2.28.1 tweak/export hint
# = 11 rows total. Update this constant whenever you add/remove a header
# or footer row, or v2.28.1's regression (top row clipped) reappears.
_MONITOR_FIXED_ROWS = 11


def _pct_color(pct: float) -> str:
    if pct >= 95:
        return "bold_red"
    if pct >= 80:
        return "yellow"
    return "green"


def _build_config_progress_row(d: dict) -> str:
    """Row 6: live config-progress summary. One line, elides unset fields."""
    segments = []

    profit_threshold  = d.get("profit_threshold")
    profit_increment  = d.get("profit_increment")
    if profit_threshold and profit_increment is not None:
        # v2.30.1: use the actionable target the bump code writes, not the
        # notification-milestone tracker. Fall back to profit_threshold for
        # pre-v2.30.1 state files that never persisted next_profit_milestone.
        next_ms = float(d.get("next_profit_milestone")
                        or profit_threshold or 0.0)
        profit  = float(d.get("profit", 0.0) or 0.0)
        # v2.37.0: run scope measures the total since the run started.
        _run_scope = d.get("profit_increment_scope") == "run"
        if _run_scope:
            profit = float(d.get("run_profit_total", profit) or 0.0)
        # v2.30.7: display window-relative progress ("how close to the NEXT
        # bump?") not cumulative profit-vs-next-ms. Denominator is one
        # threshold unit; numerator resets after every bump. Matches operator
        # expectation: "Ms 39%" means the current window is 39% done,
        # regardless of how many bumps have already fired this session.
        window_start = max(0.0, next_ms - float(profit_threshold))
        window_progress = max(0.0, profit - window_start)
        pct = min(100.0, (window_progress / float(profit_threshold) * 100)
                  if profit_threshold else 0.0)
        segments.append(
            f"{_a('dim','RunMs' if _run_scope else 'Ms')} "
            f"{_a(_pct_color(pct), f'+{window_progress:.8f}/{float(profit_threshold):.8f} ({pct:.0f}%)')}"
        )

    max_profit = d.get("max_profit")
    if max_profit is not None:
        profit = float(d.get("profit", 0.0) or 0.0)
        pct = max(0.0, min(100.0, (profit / float(max_profit) * 100) if max_profit else 0.0))
        segments.append(
            f"{_a('dim','MaxP')} "
            f"{_a(_pct_color(pct), f'+{max(0.0, profit):.8f}/{float(max_profit):.8f} ({pct:.0f}%)')}"
        )

    max_loss = d.get("max_loss")
    if max_loss is not None:
        profit = float(d.get("profit", 0.0) or 0.0)
        loss_target = abs(float(max_loss))
        loss_now = abs(profit) if profit < 0 else 0.0
        pct = max(0.0, min(100.0, (loss_now / loss_target * 100) if loss_target > 0 else 0.0))
        color = _pct_color(pct) if profit < 0 else "dim"
        segments.append(
            f"{_a('dim','MaxL')} "
            f"{_a(color, f'-{loss_now:.8f}/-{loss_target:.8f} ({pct:.0f}%)')}"
        )

    max_bets = d.get("max_bets")
    if max_bets is not None:
        total = int(d.get("total_bets", 0) or 0)
        pct = max(0.0, min(100.0, (total / int(max_bets) * 100) if max_bets else 0.0))
        segments.append(
            f"{_a('dim','MaxB')} "
            f"{_a(_pct_color(pct), f'{total}/{int(max_bets)} ({pct:.0f}%)')}"
        )

    max_wins = d.get("max_wins")
    if max_wins is not None:
        wins = int(d.get("wins", 0) or 0)
        pct = max(0.0, min(100.0, (wins / int(max_wins) * 100) if max_wins else 0.0))
        segments.append(
            f"{_a('dim','MaxW')} "
            f"{_a(_pct_color(pct), f'{wins}/{int(max_wins)} ({pct:.0f}%)')}"
        )

    floor = d.get("stop_on_balance")
    if floor is not None:
        bal = float(d.get("current_balance", 0.0) or 0.0)
        color = "yellow" if bal <= float(floor) * 1.05 else "dim"
        segments.append(
            f"{_a('dim','MinBal')} {_a(color, f'{bal:.8f}/{float(floor):.8f}')}"
        )

    if d.get("recurring"):
        delay = int(d.get("recurring_delay_sec", 0) or 0)
        segments.append(f"{_a('dim','Rec')} {_a('cyan', f'on({delay}s)')}")

    if d.get("streak_reset_losses"):
        n = int(d["streak_reset_losses"])
        sr_delay = int(d.get("streak_reset_delay_sec", 0) or 0)
        segments.append(f"{_a('dim','SR')} {_a('cyan', f'{n}L→{sr_delay}s')}")

    if not segments:
        return f" {_a('dim','CFG')}  {_a('dim','─')}  {_a('dim','no goals set')}"
    return " " + _a("dim", "CFG") + "  " + "  ".join(segments)


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
    strategy  = _with_phase(d.get("strategy", "?"), d.get("hybrid_phase"))
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
    # v2.28.4: surface recurring-pause countdown right in the LIVE indicator
    # so the user can see "sleeping 45m/60m of the recurring_delay" at a glance.
    _pause_left = int(d.get("pause_remaining_sec", 0) or 0)
    _pause_total = int(d.get("pause_total_sec") or d.get("recurring_delay_sec", 0) or 0)
    if pid_alive and _pause_left > 0:
        st_ind = _a("bold_cyan",
                    f"◐ PAUSED {timedelta(seconds=_pause_left)}/{timedelta(seconds=_pause_total)}")
        st_ind_plain = f"◐ PAUSED {timedelta(seconds=_pause_left)}/{timedelta(seconds=_pause_total)}"
    elif pid_alive and not paused:
        st_ind = _a("bold_green", "● LIVE")
        st_ind_plain = "● LIVE"
    elif pid_alive and paused:
        st_ind = _a("bold_yellow", "● PAUSED")
        st_ind_plain = "● PAUSED"
    else:
        st_ind = _a("bold_red", "● STOPPED")
        st_ind_plain = "● STOPPED"
    _run = d.get("run_uptime_sec")
    run_seg = f"  Run {_fmt_duration(_run)}" if _run is not None else ""
    hdr_vis = 4 + len(f"WolfBet v{VERSION}  #{sid}  {uptime}{run_seg}  ") + len(st_ind_plain) + 1
    fill = max(0, term_w - hdr_vis)
    add(f"{_a('dim_blue', '─── ')}{_a('bold_cyan', f'WolfBet v{VERSION}')}"
        f"{_a('white', f'  #{sid}  {uptime}{run_seg}')}  {st_ind} "
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
        sk = _a("dim", "-")
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

    # ── Row 6: Config-progress row (v2.30.0) ──
    add(_build_config_progress_row(d))

    # ── Row 7: Separator ──────────────────────────────
    add(_a("dim_blue", "─" * term_w))

    # ── Row 8: Bets table header ──────────────────────
    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Amount':>14}  {'Roll':>7}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    # ── Rows 9..N-2: Recent bet rows ─────────────────
    bets_avail = max(0, term_h - _MONITOR_FIXED_ROWS)
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
    status_txt = status[:60] if status else "Monitoring..."
    keys = "[P]ause [R]esume [Q]uit/Detach [S]top daemon"
    add(f" {_a('bold_white', 'MONITOR')} {_a('white', status_txt)}  {_a('dim', keys)}")
    # v2.28.1: advertise the config-change path since T/E/H are foreground-TUI only.
    add(f" {_a('dim', 'Tweak config:')} {_a('cyan', 'wolfctl config-set KEY=VALUE')}   "
        f"{_a('dim', '· Export/history:')} {_a('cyan', 'wolfctl export / stats / analytics')}")

    return "\r\n".join(lines)


def _is_systemd_managed(pid) -> bool:
    """Return True when the given pid runs inside a systemd wolfbet unit.

    Used by cmd_monitor's stop path so pressing 'S' from monitor goes through
    `systemctl --user stop wolfbet` (systemd knows to keep it stopped) instead
    of raw SIGTERM (systemd sees a clean exit and respawns per Restart= policy).
    """
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f:
            return "wolfbet.service" in f.read()
    except OSError:
        return False


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
    console.print(f"[dim]Attaching to session #{sid} (PID {pid})... "
                  f"Keys: P/R/S/Q · Tweak: [cyan]wolfctl config-set KEY=VALUE[/][/]")
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

        # Handle stop action: go through systemctl when systemd manages the
        # daemon so the unit's Restart= policy doesn't respawn on a clean exit.
        # Fall back to SIGTERM for non-systemd installs.
        stopped = False
        if _monitor_action[0] == "stop" or (locals().get("action") == "stop"):
            _pid = locals().get("pid") or 0
            stopped_via_systemd = False
            if _is_systemd_managed(_pid):
                try:
                    import subprocess as _sp
                    _sp.run(["systemctl", "--user", "stop", "wolfbet"],
                            check=False, timeout=10)
                    stopped_via_systemd = True
                except Exception:
                    pass
            if not stopped_via_systemd and _pid:
                try:
                    os.kill(_pid, signal.SIGTERM)
                except OSError:
                    pass
            stopped = True
        _monitor_action[0] = None
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

    if stopped:
        console.print("[bold red]Daemon stopped.[/]")
    else:
        console.print("[dim]Detached from monitor. Daemon continues running.[/]")


# ══════════════════════════════════════════════════════════
#  --json OUTPUT for the read commands
# ══════════════════════════════════════════════════════════
CLI_JSON_VERSION = 1
_JSON_COMMANDS = ("status", "lanes_status", "config_show", "stats", "analytics",
                  "last_bets", "session_bets", "export", "backtest",
                  "key_list", "key_put", "key_rotate", "key_delete")


class _CliError(Exception):
    """Empty or failed state of a read command: a stable code, the message, its Rich style and exit code."""

    def __init__(self, code: str, message: str, style: str = "yellow",
                 hint: Optional[str] = None, exit_code: Optional[int] = None):
        super().__init__(message)
        self.code, self.message, self.style = code, message, style
        self.hint, self.exit_code = hint, exit_code


def _print_json(doc: dict) -> None:
    """Write one JSON document to stdout. Under --json it is the only stdout output."""
    sys.stdout.write(json.dumps({"v": CLI_JSON_VERSION, **doc}, default=str) + "\n")
    sys.stdout.flush()


def _cli_fail(err: _CliError, as_json: bool) -> None:
    """Report a read command's empty or failed state, as JSON or Rich, then exit with its code."""
    if as_json:
        _print_json({"error": {"code": err.code, "message": err.message}})
    else:
        console.print(f"[{err.style}]{err.message}[/]")
        if err.hint:
            console.print(f"[dim]{err.hint}[/]")
    if err.exit_code is not None:
        sys.exit(err.exit_code)


def _json_window(since, until, lane) -> dict:
    return {"since": _normalize_window_bound(since), "until": _normalize_window_bound(until),
            "lane": int(lane) if lane else None}


def _span_dict(first, last, span_sec, bet_sec) -> dict:
    return {"first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
            "span_sec": span_sec, "bet_sec": bet_sec}


def _span_range(span: dict) -> tuple:
    """(first, last) datetimes back from a _span_dict, for the Rich renderers."""
    if not span["first"]:
        return None, None
    return datetime.fromisoformat(span["first"]), datetime.fromisoformat(span["last"])


# ══════════════════════════════════════════════════════════
#  --status COMMAND
# ══════════════════════════════════════════════════════════
def _read_lane_states() -> list:
    """v2.35.0: [(lane, state-dict | None, alive)] for registry lanes that have a state file."""
    out = []
    for k in _lane_ids(_read_config_raw()):
        path = _lane_path(_BASE_STATE_PATH, k)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            out.append((k, None, False))
            continue
        alive = False
        try:
            if d.get("pid"):
                os.kill(int(d["pid"]), 0)
                alive = True
        except (OSError, ValueError, TypeError):
            pass
        out.append((k, d, alive))
    return out


def _lanes_status_data() -> dict:
    """Every lane's state file (None when unreadable), whether it runs or pauses, and the totals."""
    rows = _read_lane_states()
    if not rows:
        raise _CliError("no_session", "No lane state files found.")
    lanes = []
    tot_bps = tot_profit = 0.0
    for k, d, alive in rows:
        paused = bool(alive and d is not None and int(d.get("pause_remaining_sec", 0) or 0) > 0)
        lanes.append({"lane": k, "alive": alive, "paused": paused, "state": d})
        if d is None:
            continue
        tot_profit += float(d.get("profit", 0) or 0)
        tot_bps += float(d.get("bets_per_second", 0) or 0) if alive else 0.0
    return {"lanes": lanes, "totals": {"bps": tot_bps, "profit": tot_profit,
                                       "live": sum(1 for r in lanes if r["alive"]),
                                       "count": len(lanes)}}


def cmd_lanes_status(as_json: bool = False):
    """v2.35.0: one row per lane + totals (wolfctl status when lanes > 1)."""
    try:
        data = _lanes_status_data()
    except _CliError as e:
        _cli_fail(e, as_json)
        return
    if as_json:
        _print_json(data)
        return
    names = {lane: entry["name"] for lane, entry in _lane_registry(_read_config_raw()).items()
             if entry["name"] != _default_lane_name(lane)}
    t = Table(title="Parallel lanes", title_style="bold cyan", box=box.SIMPLE)
    cols = ("Lane",) + (("Name",) if names else ()) + (
        "State", "Session", "Uptime", "Run", "Profit", "Equity", "bps", "Pace")
    for col in cols:
        t.add_column(col, justify="left" if col in ("Name", "State") else "right")
    for row in data["lanes"]:
        k, d, alive = row["lane"], row["state"], row["alive"]
        lead = (str(k), escape(names.get(k, ""))) if names else (str(k),)
        if d is None:
            t.add_row(*lead, "[red]unreadable[/]", *["-"] * 7)
            continue
        p = float(d.get("profit", 0) or 0)
        bps = float(d.get("bets_per_second", 0) or 0) if alive else 0.0
        eq = d.get("lane_equity")
        st = "[green]LIVE[/]" if alive else "[red]STOPPED[/]"
        if row["paused"]:
            st = "[cyan]PAUSED[/]"
        t.add_row(*lead, st, f"#{d.get('session_id', '?')}",
                  str(timedelta(seconds=int(d.get("uptime_sec", 0) or 0))),
                  _fmt_duration(d.get("run_uptime_sec")) if d.get("run_uptime_sec") is not None else "-",
                  f"[{'green' if p >= 0 else 'red'}]{p:+.8f}[/]",
                  f"{eq:.8f}" if isinstance(eq, (int, float)) else "-",
                  f"{bps:.1f}",
                  f"{float(d.get('soft_pace_delay', 0) or 0) * 1000:.0f}ms")
    console.print(t)
    tot = data["totals"]
    console.print(f"  [dim]Total:[/] [bold]{tot['bps']:.1f}[/] bps   "
                  f"[dim]session P/L[/] [{'green' if tot['profit'] >= 0 else 'red'}]{tot['profit']:+.8f}[/]   "
                  f"[dim]lanes live[/] {tot['live']}/{tot['count']}")


def _fmt_events_row(ev: Optional[dict]) -> str:
    """One-line Events summary for --status from the state file's events dict."""
    if not ev or not ev.get("enabled"):
        return "[dim]off[/]"
    if not ev.get("running"):
        return "[red]on, not running[/] [dim](check events_url / wolfctl doctor)[/]"
    conn = "[green]yes[/]" if ev.get("connected") else "[red]no[/]"
    line = (f"[green]on[/]  · sent [cyan]{ev.get('sent', 0)}[/]  · dropped "
            f"[yellow]{ev.get('dropped', 0) + ev.get('overflow', 0)}[/]  · connected {conn}")
    if ev.get("last_error"):
        line += f"  · last error [red]{ev['last_error']}[/]"
    return line


def cmd_events_check() -> None:
    """Ping the events Redis once with a 1 s timeout and print the result. Never prints credentials."""
    load_config()
    if not state.events_enabled:
        console.print("  Events: off")
        return
    if not state.events_url:
        console.print("  Events: on but no events_url is set")
        return
    shown = _redact_url(state.events_url)
    try:
        import redis
    except ImportError:
        console.print("  Events: redis package not installed (pip install -r requirements.txt)")
        return
    try:
        redis.Redis.from_url(state.events_url, socket_connect_timeout=1,
                             socket_timeout=1).ping()
        console.print(f"  Events: Redis OK ({shown})")
    except Exception as e:
        console.print(f"  Events: Redis unreachable at {shown} ({type(e).__name__})")


def _status_data() -> dict:
    """This lane's state file as the daemon wrote it, and whether its pid is alive."""
    if not os.path.exists(STATE_PATH):
        raise _CliError("no_session", "No running session found.", exit_code=0)
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception:
        raise _CliError("state_unreadable", "Could not read state file.",
                        style="red", exit_code=1) from None
    return {"running": bool(_monitor_get_pid(d)), "state": d}


def cmd_status(as_json: bool = False):
    try:
        data = _status_data()
    except _CliError as e:
        _cli_fail(e, as_json)
        return
    if as_json:
        _print_json(data)
        return
    d, alive = data["state"], data["running"]

    status = "[bold green]RUNNING[/]" if alive else "[bold red]STOPPED[/]"
    p = d.get("profit", 0)
    ps = "+" if p >= 0 else ""
    pc = "green" if p >= 0 else "red"
    wr = (d["wins"] / d["total_bets"] * 100) if d.get("total_bets") else 0
    uptime = str(timedelta(seconds=d.get("uptime_sec", 0)))
    # v2.28.4: recurring-pause countdown: uptime freezes at bet-end; this
    # line surfaces "Xm remaining / Ym total" so users know why the session
    # isn't placing bets.
    _pause_left = int(d.get("pause_remaining_sec", 0) or 0)
    _pause_total = int(d.get("pause_total_sec") or d.get("recurring_delay_sec", 0) or 0)
    pause_str = (f"[cyan]{timedelta(seconds=_pause_left)}[/] remaining / "
                 f"{timedelta(seconds=_pause_total)} total") if _pause_left > 0 else None

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
        sk_now = "-"

    console.print(Rule(f"[bold cyan]WolfBet Session #{d.get('session_id', '?')}  {status}[/]"))

    # ── Session Info ──
    strat_str = (f"{_with_phase(d.get('strategy', '?'), d.get('hybrid_phase'))}  "
                 f"{d.get('multiplier', '?')}x  {d.get('rule', '?')}")
    sk = d.get("strategy_key", "")
    if sk in ("2", "6", "7"):
        strat_str += f"  [dim](loss mult: {d.get('loss_mult', '?')}x)[/]"
    elif sk in ("3", "5"):
        strat_str += f"  [dim](win mult: {d.get('win_mult', '?')}x)[/]"

    rows = [
        ("Uptime",         uptime + (f"  [dim]· Recurring pause:[/] {pause_str}" if pause_str else "")),
        *([("Running for", _fmt_duration(d["run_uptime_sec"]))]
          if d.get("run_uptime_sec") is not None else []),
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
        if d.get("profit_increment_scope") == "run":
            rows.append(("Profit Bump", f"+{pi:.8f} every {pt} run profit"))
            nxt = d.get("next_profit_milestone")
            rows.append(("Run Profit",
                         f"{float(d.get('run_profit_total') or 0):+.8f}  "
                         f"[dim](since #{d.get('run_first_session') or '?'}, "
                         f"{d.get('run_bumps') or 0} bump(s)"
                         + (f", next at {float(nxt):+.8f}" if nxt else "") + ")[/]"))
        else:
            rows.append(("Profit Bump",   f"+{pi:.8f} every {pt} profit"))

    # ── Observability (v2.26.1: from state file) ──
    api_p50 = d.get("api_p50_ms")
    api_p99 = d.get("api_p99_ms")
    api_peak = d.get("api_peak_ms")
    if api_p50 or api_peak:
        rows.append(("API",
                     f"p50 [cyan]{api_p50 or 0:.0f}ms[/]  · "
                     f"p99 [cyan]{api_p99 or 0:.0f}ms[/]  · "
                     f"peak [cyan]{api_peak or 0:.0f}ms[/]"))
    cycle_ms = d.get("cycle_ms")
    overhead_ms = d.get("overhead_ms")
    if cycle_ms:
        # v2.30.3: was `1000/cycle_ms` (misleading for auto-games where the
        # server batches multiple bets per outer-iteration cycle). Use the
        # same theoretical_max_bps cmd_diagnose displays so status + diagnose
        # agree. Auto-game users get a batching footnote.
        tmax = float(d.get("theoretical_max_bps") or 0)
        auto = is_auto_game(d.get("game", ""))
        note = "  [dim](batched: cycle is per outer iteration)[/]" if auto else ""
        rows.append(("Cycle",
                     f"[magenta]{cycle_ms:.0f}ms[/]/bet "
                     f"(overhead [cyan]{overhead_ms or 0:.0f}ms[/])"
                     f"  → [magenta]{tmax:.2f}[/] BPS ceiling{note}"))
    eff_pct = d.get("efficiency_pct")
    if eff_pct is not None:
        eff_c = "green" if eff_pct >= 90 else "yellow"
        rows.append(("Efficiency", f"[{eff_c}]{eff_pct:.0f}%[/] of theoretical"))
    pipe_eff = d.get("pipeline_effective") or 1
    if pipe_eff > 1:
        rows.append(("Pipeline",
                     f"depth=[magenta]{pipe_eff}[/]  · "
                     f"dispatched=[cyan]{d.get('pipeline_dispatched', 0)}[/]"))

    rows.append(("Events", _fmt_events_row(d.get("events"))))
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
#  CONFIG CLI COMMANDS  (v2.25.0: non-interactive config surface)
# ══════════════════════════════════════════════════════════
_CONFIG_SHOW_FIELDS = [
    ("currency",              "Betting currency"),
    ("game",                  "Game (dice / limbo / dice-auto / limbo-auto / ...)"),
    ("rule",                  "Dice rule (over / under)"),
    ("multiplier",            "Payout multiplier"),
    ("bet_value",             "Threshold value on the dice roll"),
    ("strategy",              "Strategy name"),
    ("strategy_key",          "Strategy key (1-8)"),
    ("base_bet",              "Base bet amount"),
    ("loss_mult",             "Loss multiplier (Martingale)"),
    ("win_mult",              "Win multiplier (Paroli / Anti-M)"),
    ("delay_martin_threshold","Delay Martingale N-loss threshold"),
    ("bet_delay",             "Seconds between bets (0 = full speed)"),
    ("max_profit",            "Stop when profit ≥ this"),
    ("max_loss",              "Stop when loss ≥ this"),
    ("max_bets",              "Stop after this many bets"),
    ("max_wins",              "Stop after this many wins"),
    ("stop_on_balance",       "Stop when balance floor reached"),
    ("balance_goal",          "Stop the bot when the balance reaches this"),
    ("hard_cap",              "Never place a bet that would pass max_loss or the balance floor"),
    ("restart_after_loss",    "New session after a capped loss (needs recurring, hard_cap, max_loss, stop_on_balance)"),
    ("hybrid_recover_at",     "Session loss that switches to the recovery strategy"),
    ("hybrid_recover_strategy", "Recovery strategy key (1-7)"),
    ("hybrid_recover_bet",    "First recovery bet, as a multiple of base_bet"),
    ("hybrid_press_at",       "Session profit that switches to the press strategy"),
    ("hybrid_press_floor",    "Profit at which press hands back to the opening strategy"),
    ("hybrid_press_strategy", "Press strategy key (1-7)"),
    ("hybrid_press_bet",      "First press bet, as a multiple of base_bet"),
    ("profit_increment",      "Bump base_bet by this at every profit milestone"),
    ("profit_threshold",      "Profit interval between increments"),
    ("profit_increment_scope", "session = each session's profit · run = total since run start (wolfctl run-reset)"),
    ("recurring",             "Auto-restart cycle on non-loss stop"),
    ("recurring_delay",       "Seconds to pause between recurring cycles (0 = back-to-back)"),
    ("streak_reset_losses",   "Restart session on first win after N losses in a row (profit ≥ 0)"),
    ("streak_reset_delay",    "Seconds to pause before the streak-reset restart"),
    ("basebet_pct",           "Base bet as % of balance (0 = off)"),
    ("streak_delay_loss",     "Extra pause after N losses in a row  (N:sec)"),
    ("streak_delay_win",      "Extra pause after N wins in a row  (N:sec)"),
    ("streak_delay_bets",     "Extra pause every N bets  (N:sec)"),
    ("streakbet_loss",        "Bet reduction after N losses  (N:factor)"),
    ("milestone_bets",        "Milestone every N bets (0 = off)"),
    ("milestone_wins",        "Milestone every N wins (0 = off)"),
    ("milestone_losses",      "Milestone every N losses (0 = off)"),
    ("milestone_profit",      "Milestone every X profit crossed (0 = off)"),
    ("pipeline_depth",        "Pipelined betting depth (1 = serial, Flat-only)"),
    ("proxy",                 "HTTP/HTTPS/SOCKS5 proxy URL (credentials masked)"),
    ("purge_days",            "Bet-row retention window in days (1-30)"),
    ("db_write_mode",         "Bet DB writer: async (default, batched) or sync (legacy, per-bet fsync)"),
    ("key_ref",               "Vault key the lane bets with (default: the main key)"),
    ("proxy_ref",             "Vault proxy the lane bets through, for a proxy with a password"),
    ("events_enabled",        "Send bets, status and lifecycle events to Redis for the dashboard"),
    ("events_url",            "Redis URL for events (set via config-set or WOLFBET_EVENTS_URL)"),
    ("custom_rules_text",     "Rule-Based custom rules (JSON per line)"),
]


def _fmt_config_value(field: str, val) -> str:
    """Render a config value for the --config-show table.

    Streak tuples → "N:X"; None → "off"; long strings truncated.
    Proxy is credential-masked via _mask_proxy: NEVER leak userinfo.
    """
    if field == "proxy":
        return f"[cyan]{_mask_proxy(val)}[/]" if val else "[dim]direct[/]"
    if field == "events_url":
        return f"[cyan]{_redact_url(val)}[/]" if val else "[dim]off[/]"
    if val is None:
        return "[dim]off[/]"
    if isinstance(val, bool):
        return "[green]on[/]" if val else "[dim]off[/]"
    if isinstance(val, tuple) and len(val) == 2:
        n, x = val
        return f"[dim]off[/]" if n == 0 else f"{n}:{x}"
    if isinstance(val, float):
        if field in ("base_bet", "bet_value", "max_profit", "max_loss",
                     "stop_on_balance", "profit_increment", "profit_threshold",
                     "milestone_profit", "balance_goal", "hybrid_recover_at",
                     "hybrid_press_at", "hybrid_press_floor"):
            return f"{val:.8f}"
        return f"{val:.4f}".rstrip("0").rstrip(".") or "0"
    if isinstance(val, str) and len(val) > 60:
        return val[:57] + "..."
    return str(val)


def _config_show_value(field: str, val):
    """A config value safe to print or send: proxy and events URL without credentials."""
    if field == "proxy":
        return _mask_proxy(val) if val else None
    if field == "events_url":
        return _redact_url(val) if val else None
    if field in _STREAK_TUPLE_KEYS and isinstance(val, str):
        return parse_streak_delay(val)
    return val


def _pending_restarts() -> dict:
    """Each running lane's saved changes it picks up only on a restart: {lane: [{key, running, saved}]}."""
    pending = {}
    for lane, d, alive in _read_lane_states():
        rows = (d or {}).get("pending_restart") or {}
        if alive and isinstance(rows, dict) and rows:
            pending[str(lane)] = [{"key": k, "running": v[0], "saved": v[1]}
                                  for k, v in sorted(rows.items())
                                  if isinstance(v, list) and len(v) == 2]
    return pending


def _config_show_data() -> dict:
    """Saved config (secrets masked), the running daemon's live base bet, lanes and the file's metadata."""
    if not load_config():
        raise _CliError("no_config", "No config saved yet: run the wizard first.",
                        hint="  Try: python3 wolfbet.py --setup-only", exit_code=1)
    config = {}
    for field, _ in _CONFIG_SHOW_FIELDS:
        try:
            config[field] = _config_show_value(field, getattr(state, field))
        except AttributeError:
            continue
    live = _monitor_read_state()
    if not (live and _monitor_get_pid(live)):
        live = None
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    overrides = {}
    for lane in _lane_ids(raw):
        ov = _lane_overrides(raw, lane)
        if ov:
            overrides[str(lane)] = {k: _config_show_value(k, v) for k, v in sorted(ov.items())}
    try:
        st = os.stat(CONFIG_PATH)
        config_file = {"path": CONFIG_PATH, "mode": oct(st.st_mode & 0o777), "size": st.st_size}
    except OSError:
        config_file = None
    return {
        "api_key": {"set": bool(state.api_key), "masked": _mask_key(state.api_key)},
        "config": config,
        "live_base_bet": (float(live["base_bet"])
                          if live and live.get("base_bet") is not None else None),
        "run_bumps": int(live.get("run_bumps") or 0) if live else None,
        "lanes": _lanes_count(raw),
        "lane_split": _lane_split(raw),
        "lane_overrides": overrides,
        "pending": _pending_restarts(),
        "config_file": config_file,
    }


def cmd_config_show(as_json: bool = False):
    """Render the current saved config as a Rich table, or as JSON. No wizard, no side effects."""
    from rich.table import Table   # local import: avoids top-level cost when unused
    try:
        data = _config_show_data()
    except _CliError as e:
        _cli_fail(e, as_json)
        return
    if as_json:
        _print_json(data)
        return
    tbl = Table(title="WolfBet AutoBot: Current Config",
                title_style="bold cyan", show_lines=False)
    tbl.add_column("Field",       style="bold yellow", no_wrap=True)
    tbl.add_column("Value",       style="cyan")
    tbl.add_column("Description", style="dim")

    key_state = "[bold green](SET)[/]" if data["api_key"]["set"] else "[bold red](NOT SET)[/]"
    tbl.add_row("api_key", f"[dim]{data['api_key']['masked']}[/] {key_state}",
                "API bearer token (set via WOLFBET_API_KEY env var)")
    live_base = data["live_base_bet"]
    for field, desc in _CONFIG_SHOW_FIELDS:
        if field not in data["config"]:
            continue
        val = data["config"][field]
        shown = _fmt_config_value(field, val)
        if field == "base_bet" and live_base is not None and abs(live_base - float(val)) > 1e-12:
            shown = (f"{shown}  [dim](live {live_base:.8f}, "
                     f"+{data['run_bumps']} bumps)[/]")
        tbl.add_row(field, shown, desc)
    console.print(tbl)
    n_lanes, _mode = data["lanes"], data["lane_split"]
    console.print(f"\n[dim]Lanes:[/] [bold]{n_lanes}[/]  [dim]split:[/] [bold]{_mode}[/]"
                  + (f"  [dim]({_LANE_SPLIT_HELP[_mode]})[/]" if n_lanes > 1 else ""))
    for lane, ov in data["lane_overrides"].items():
        parts = ", ".join(f"{k}={_fmt_config_value(k, v)}" for k, v in ov.items())
        console.print(f"  [magenta]lane{lane}[/] overrides: {parts}")
    cf = data["config_file"]
    if cf:
        console.print(f"\n[dim]Config file:[/] [cyan]{cf['path']}[/]  "
                      f"[dim]mode={cf['mode']}  size={cf['size']}B[/]")
    console.print(f"[dim]Tweak with:[/] "
                  f"[cyan]python3 wolfbet.py --config-set KEY=VALUE ...[/]")


# Value rules for every key config-set accepts, read by _apply_mutation for its
# clamps and choices and published by --config-schema. A rule states only what
# _apply_mutation enforces; "clamps" means out-of-range input is clamped, not refused.
_URL_SCHEMES_PROXY = ("http", "https", "socks5")
_CONFIG_VALUE_RULES = {
    "bet_delay":               {"type": "number"},
    "basebet_pct":             {"type": "number", "off_allowed": True},
    "streak_delay_loss":       {"type": "string", "format": "N:X", "off_allowed": True},
    "streak_delay_win":        {"type": "string", "format": "N:X", "off_allowed": True},
    "streak_delay_bets":       {"type": "string", "format": "N:X", "off_allowed": True},
    "streakbet_loss":          {"type": "string", "format": "N:X", "off_allowed": True},
    "max_profit":              {"type": "number", "off_allowed": True},
    "max_loss":                {"type": "number", "off_allowed": True},
    "max_bets":                {"type": "integer", "off_allowed": True},
    "max_wins":                {"type": "integer", "off_allowed": True},
    "stop_on_balance":         {"type": "number", "off_allowed": True},
    "milestone_bets":          {"type": "integer", "off_allowed": True},
    "milestone_wins":          {"type": "integer", "off_allowed": True},
    "milestone_losses":        {"type": "integer", "off_allowed": True},
    "milestone_profit":        {"type": "number", "off_allowed": True},
    "profit_increment":        {"type": "number", "off_allowed": True},
    "profit_threshold":        {"type": "number", "off_allowed": True},
    "profit_increment_scope":  {"type": "string", "choices": PROFIT_INCREMENT_SCOPES},
    "recurring":               {"type": "boolean"},
    "recurring_delay":         {"type": "number", "min": 0.0, "max": 86400.0, "clamps": True,
                                "off_allowed": True},
    "streak_reset_losses":     {"type": "integer", "min": 1, "off_allowed": True},
    "streak_reset_delay":      {"type": "number", "min": 0.0, "max": 86400.0, "clamps": True,
                                "off_allowed": True},
    "base_bet":                {"type": "number"},
    "multiplier":              {"type": "number"},
    "loss_mult":               {"type": "number"},
    "win_mult":                {"type": "number"},
    "game":                    {"type": "string", "choices": tuple(sorted(GAME_ENDPOINTS))},
    "strategy_key":            {"type": "string", "choices": tuple(sorted(STRATEGY_NAMES))},
    "currency":                {"type": "string", "choices": tuple(CURRENCIES)},
    "rule":                    {"type": "string", "choices": DICE_RULES},
    "delay_martin_threshold":  {"type": "integer", "min": 1},
    "custom_rules_text":       {"type": "string", "format": "rules", "max_length": RULES_MAX_CHARS,
                                "off_allowed": True},
    "pipeline_depth":          {"type": "integer", "min": 1, "max": 8, "clamps": True,
                                "off_allowed": True},
    "proxy":                   {"type": "string", "format": "url", "schemes": _URL_SCHEMES_PROXY,
                                "off_allowed": True},
    "purge_days":              {"type": "integer", "min": 1, "max": 30, "clamps": True,
                                "off_allowed": True},
    "db_write_mode":           {"type": "string", "choices": ("async", "sync")},
    "events_enabled":          {"type": "boolean"},
    "events_url":              {"type": "string", "format": "url", "schemes": _EVENTS_URL_SCHEMES,
                                "off_allowed": True},
    "key_ref":                 {"type": "string", "format": "vault_id"},
    "proxy_ref":               {"type": "string", "format": "vault_id", "off_allowed": True},
    "hard_cap":                {"type": "boolean"},
    "restart_after_loss":      {"type": "boolean"},
    "balance_goal":            {"type": "number", "min": 0.0, "off_allowed": True},
    "hybrid_recover_at":       {"type": "number", "min": 0.0, "off_allowed": True},
    "hybrid_press_at":         {"type": "number", "min": 0.0, "off_allowed": True},
    "hybrid_press_floor":      {"type": "number", "min": 0.0, "off_allowed": True},
    "hybrid_recover_bet":      {"type": "number", "min": 1.0},
    "hybrid_press_bet":        {"type": "number", "min": 1.0},
    "hybrid_recover_strategy": {"type": "string", "choices": PHASE_STRATEGY_KEYS},
    "hybrid_press_strategy":   {"type": "string", "choices": PHASE_STRATEGY_KEYS},
}


def _rule_bounds(key: str) -> tuple:
    """(min, max) of a clamped config key, as _apply_mutation clamps it."""
    rule = _CONFIG_VALUE_RULES[key]
    return rule["min"], rule["max"]


_VALUELESS_ACTIONS = frozenset({"switch", "stop"})


def _rule_options() -> dict:
    """The wizard's rule menus (condition, fields, operators, actions) for a rule builder."""
    return {
        "cond_types": list(COND_TYPES.values()),
        "seq_modes": [{"key": key, "label": label} for key, label in SEQ_MODES.values()],
        "seq_triggers": list(SEQ_TRIGGERS.values()),
        "profit_fields": list(PROFIT_FIELDS.values()),
        "bet_fields": list(BET_FIELDS.values()),
        "ops": [{"key": key, "label": label} for key, label in CMP_OPS.values()],
        "actions": [{"key": key, "label": label,
                     "value": not (key.startswith("reset_") or key in _VALUELESS_ACTIONS)}
                    for key, label in RULE_ACTIONS.values()],
    }


def _config_schema_data() -> dict:
    """Every key --config-set accepts: value rule, aliases, default, live reload, lane override, help."""
    defaults = BotState()
    aliases = {}
    for alias, key in _TWEAK_ALIAS.items():
        aliases.setdefault(key, []).append(alias)
    helps = dict(_CONFIG_SHOW_FIELDS)
    keys = [{"key": key,
             "off_allowed": False,
             **_CONFIG_VALUE_RULES[key],
             "aliases": sorted(aliases.get(key, [])),
             "default": _config_show_value(key, getattr(defaults, key, None)),
             "live_reload": key in _LIVE_RELOAD_KEYS,
             "lane_overridable": key not in _LANE_FORBIDDEN_KEYS,
             "restart": _SETTINGS[key][0],
             "help": helps[key]}
            for key in sorted(_TWEAK_PARAMS)]
    for entry in keys:
        if entry["key"] == "custom_rules_text":
            entry["rule_options"] = _rule_options()
    return {"keys": keys,
            "lanes": {"count": {"min": 1, "max": _lanes_max(_read_config_raw())},
                      "split": {"choices": list(LANE_SPLIT_MODES)},
                      "override_lanes": {"min": 2, "max": LANE_ID_MAX},
                      "unset_values": list(_LANE_UNSET_VALUES)}}


def _parse_config_set_pairs(pairs: list) -> dict:
    """Parse ['max_profit=0.005', 'base_bet=0.0001'] → {'max_profit': '0.005', ...}.

    Applies _TWEAK_ALIAS. Raises ValueError on malformed input.
    """
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Malformed pair (expected KEY=VALUE): {pair!r}")
        key, val = pair.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        canonical = _TWEAK_ALIAS.get(key, key)
        if canonical not in _TWEAK_PARAMS:
            raise ValueError(
                f"Unknown config field: {key!r}. Valid: {', '.join(sorted(_TWEAK_PARAMS))}"
            )
        out[canonical] = val
    return out


_LANE_UNSET_VALUES = ("default", "unset", "inherit")


def _split_lane_pairs(pairs: list):
    """Split config-set pairs into (base, {lane: [pairs]}, block).

    block holds the lanes-wide settings given: "count" (lanes=N), "split" (lane_split)
    and "max" (lanes_max). The count is checked against the limit when the plan is built.
    """
    import re as _re
    base, per_lane, block = [], {}, {}
    for pair in pairs:
        if "=" not in pair:
            base.append(pair)          # _parse_config_set_pairs reports it
            continue
        key, val = pair.split("=", 1)
        key = key.strip().lower()
        m = _re.match(r"^lane(\d+)\.(.+)$", key)
        if key in ("lanes", "lanes_max"):
            try:
                n = int(val.strip())
            except ValueError:
                raise ValueError(f"{key} must be an integer 1-{LANE_ID_MAX}, got {val!r}")
            if not 1 <= n <= LANE_ID_MAX:
                raise ValueError(f"{key} must be 1-{LANE_ID_MAX}, got {n}")
            block["count" if key == "lanes" else "max"] = n
        elif key == "lane_split":
            split = val.strip().lower()
            if split not in LANE_SPLIT_MODES:
                raise ValueError(f"lane_split must be one of {', '.join(LANE_SPLIT_MODES)}, "
                                 f"got {val!r}")
            block["split"] = split
        elif m and m.group(2) == "name":
            lane, name = int(m.group(1)), val.strip().strip('"').strip("'").strip()
            if not 1 <= lane <= LANE_ID_MAX:
                raise ValueError(f"lanes are 1-{LANE_ID_MAX}, got lane{lane}")
            if not LANE_NAME_RE.match(name):
                raise ValueError(LANE_NAME_HELP)
            per_lane.setdefault(lane, []).append(f"name={name}")
        elif m:
            lane = int(m.group(1))
            if not 2 <= lane <= LANE_ID_MAX:
                raise ValueError(f"lane overrides are for lanes 2-{LANE_ID_MAX} "
                                 f"(lane 1 uses the base config), got lane{lane}")
            if m.group(2) == "lane_split":
                raise ValueError("lane_split can't differ per lane (shared by all lanes)")
            per_lane.setdefault(lane, []).append(f"{m.group(2)}={val}")
        else:
            base.append(pair)
    return base, per_lane, block


def _backup_config_once(suffix: str, reason: str) -> None:
    """Copy the config file to CONFIG_PATH + suffix (0600) unless that copy already exists."""
    backup = CONFIG_PATH + suffix
    if os.path.exists(backup) or not os.path.exists(CONFIG_PATH):
        return
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, open(CONFIG_PATH, "rb") as src:
            out.write(src.read())
    except OSError as e:
        logger.warning("could not back up the config before %s: %s", reason, e)


def _backup_pre_registry_config() -> None:
    """Keep one copy of a config still in the numbered-lanes shape, so older code can be restored."""
    lanes = _read_config_raw().get("lanes")
    if not isinstance(lanes, dict) or "items" in lanes:
        return
    _backup_config_once(".pre-registry", "the lane registry")


def _write_config_raw(data: dict) -> None:
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def _encode_config_value(k, v):
    if k in _STREAK_TUPLE_KEYS:
        return f"{v[0]}:{v[1]}" if v and v[0] > 0 else None
    return v


def _lane_override_changes(lane: int, lane_pairs: list):
    """Validate laneK.* pairs through the normal mutation path.

    Returns (sets, removals): sets = {key: encoded value} including derived
    fields the mutation changes (e.g. bet_value after multiplier); removals =
    keys whose override goes away (value default / unset / inherit).
    """
    removals = set()
    to_set = []
    for pair in lane_pairs:
        k, v = pair.split("=", 1)
        canon = _TWEAK_ALIAS.get(k.strip().lower(), k.strip().lower())
        if canon in _LANE_FORBIDDEN_KEYS:
            raise ValueError(f"{canon} can't differ per lane (shared by all lanes)")
        if canon == "proxy" and "@" in v:
            raise ValueError("a proxy with a password goes in the vault: add it as a proxy key "
                             "and set proxy_ref")
        if v.strip().lower() in _LANE_UNSET_VALUES:
            removals.add(_TWEAK_ALIAS.get(k.strip().lower(), k.strip().lower()))
        else:
            to_set.append(pair)
    parsed = _parse_config_set_pairs(to_set) if to_set else {}
    for k in list(parsed) + list(removals):
        if k in _LANE_FORBIDDEN_KEYS:
            raise ValueError(f"{k} can't differ per lane (shared by all lanes)")
    if not load_config():
        raise ValueError("No config saved yet: run the wizard first.")
    keys = list(_CONFIG_KEYS) + list(_STREAK_TUPLE_KEYS)
    before = {k: getattr(state, k, None) for k in keys}
    _apply_mutation(parsed)
    changes = {k: _encode_config_value(k, getattr(state, k, None))
               for k in keys if getattr(state, k, None) != before[k]}
    for k in parsed:   # same value as base still pins the override
        changes.setdefault(k, _encode_config_value(k, getattr(state, k, None)))
    return changes, removals


def _signal_lane_daemons(lanes=None) -> int:
    """SIGHUP every running lane daemon (or just `lanes`). Returns count signalled."""
    n = 0
    for k in (lanes or _lane_ids(_read_config_raw())):
        try:
            with open(_lane_path(_BASE_PID_PATH, k)) as f:
                os.kill(int(f.read().strip()), signal.SIGHUP)
            n += 1
        except (OSError, ValueError):
            continue
    return n


def _lane_config_or_fail() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raise _CliError("no_config", "No config saved yet: run the wizard first.",
                        exit_code=1) from None
    return raw if isinstance(raw, dict) else {}


def _next_lane_id(raw: dict, reg: dict) -> int:
    """The id the next new lane gets: never one a removed lane had."""
    try:
        stored = int((raw.get("lanes") or {}).get("next_id") or 0)
    except (TypeError, ValueError):
        stored = 0
    return max(stored, max(reg) + 1)


def _lanes_block(raw: dict, reg: dict, next_id: int, split=None, lanes_max=None) -> dict:
    """The `lanes` block to write: split, the owner limit when set, next_id and the items."""
    block = {"split": split if split is not None else _lane_split(raw)}
    limit = lanes_max if lanes_max is not None else (raw.get("lanes") or {}).get("max")
    if limit is not None:
        block["max"] = limit
    block["next_id"] = next_id
    block["items"] = {str(lane): reg[lane] for lane in sorted(reg)}
    return block


def _set_enabled_count(reg: dict, count: int, next_id: int) -> int:
    """Enable `count` lanes: disabled lanes come back first (lowest id), then new ids.

    Lowering the count disables the highest enabled ids; lane 1 always stays. Returns next_id.
    """
    enabled = [lane for lane in sorted(reg) if reg[lane]["enabled"]]
    for lane in enabled[count:]:
        reg[lane]["enabled"] = False
    need = count - len(enabled)
    for lane in sorted(reg):
        if need > 0 and not reg[lane]["enabled"]:
            reg[lane]["enabled"] = True
            need -= 1
    while need > 0:
        if next_id > LANE_ID_MAX:
            raise ValueError(f"lane ids run out at {LANE_ID_MAX}: removed ids are never reused")
        reg[next_id] = {"name": _default_lane_name(next_id), "enabled": True, "overrides": {}}
        next_id += 1
        need -= 1
    return next_id


def _plan_lane_changes(per_lane: dict, block: dict) -> dict:
    """Validate lanes=N, lanes_max, lane_split and laneK.* pairs and build the lanes block to write.

    Returns {"raw": config dict with the new lanes block, "before": the config as read,
    "changes": [...], "lanes": [K, ...]}; each change is {lane (None for the lanes-wide
    settings), key, old, new, removed}.
    """
    raw = _lane_config_or_fail()
    before = json.loads(json.dumps(raw))
    reg = _lane_registry(raw)
    next_id = _next_lane_id(raw, reg)
    count, split, limit = block.get("count"), block.get("split"), block.get("max")
    changes = []
    if limit is not None:
        changes.append({"lane": None, "key": "lanes_max", "old": _lanes_max(raw), "new": limit,
                        "removed": False})
    effective_limit = limit if limit is not None else _lanes_max(raw)
    if count is not None:
        if count > effective_limit:
            raise _CliError("invalid_argument",
                            f"Invalid --config-set argument: lanes must be 1-{effective_limit}, "
                            f"got {count}", style="red", exit_code=2,
                            hint="  Raise the limit with: wolfctl lanes --max N")
        changes.append({"lane": None, "key": "lanes", "old": _lanes_count(raw),
                        "new": count, "removed": False})
        try:
            next_id = _set_enabled_count(reg, count, next_id)
        except ValueError as e:
            raise _CliError("invalid_argument", f"Invalid --config-set argument: {e}",
                            style="red", exit_code=2) from None
    running = sum(1 for entry in reg.values() if entry["enabled"])
    if limit is not None and limit < running:
        raise _CliError("invalid_argument",
                        f"Invalid --config-set argument: lanes_max can't be below the "
                        f"{running} lane(s) configured", style="red", exit_code=2)
    if split is not None:
        changes.append({"lane": None, "key": "lane_split", "old": _lane_split(raw),
                        "new": split, "removed": False})
    for lane, lane_pairs in sorted(per_lane.items()):
        names = [p.split("=", 1)[1] for p in lane_pairs if p.startswith("name=")]
        lane_pairs = [p for p in lane_pairs if not p.startswith("name=")]
        entry = reg.setdefault(lane, {"name": _default_lane_name(lane), "enabled": False,
                                      "overrides": {}})
        for name in names[-1:]:
            changes.append({"lane": lane, "key": "name", "old": entry["name"], "new": name,
                            "removed": False})
            entry["name"] = name
        if not lane_pairs:
            continue
        try:
            sets, removals = _lane_override_changes(lane, lane_pairs)
        except ValueError as e:
            raise _CliError("invalid_lane_override", f"Invalid lane{lane} override: {e}",
                            style="red", exit_code=2) from None
        ov = entry["overrides"]
        for k in sorted(removals):
            changes.append({"lane": lane, "key": k, "old": ov.pop(k, None), "new": None,
                            "removed": True})
        for k, v in sets.items():
            changes.append({"lane": lane, "key": k, "old": ov.get(k), "new": v, "removed": False})
            ov[k] = v
    raw["lanes"] = _lanes_block(raw, reg, max(next_id, max(reg) + 1), split, limit)
    return {"raw": raw, "before": before, "changes": changes, "lanes": sorted(per_lane)}


def _save_lane_registry(raw: dict, reg: dict, next_id: int) -> None:
    _backup_pre_registry_config()
    raw["lanes"] = _lanes_block(raw, reg, next_id)
    _write_config_raw(raw)


def _signal_share_change(before: dict, after: dict) -> None:
    """After the lane count changes, bring the remaining lanes their new share.

    With the bet split each running lane reloads, so its base bet ÷ lanes follows at once.
    The budget share (balance ÷ lanes) is fixed when a lane starts, so it waits for a restart.
    """
    if _lanes_count(before) == _lanes_count(after):
        return
    split = _lane_split(after)
    if split in ("bet", "both"):
        enabled = [lane for lane, entry in _lane_registry(after).items() if entry["enabled"]]
        n = _signal_lane_daemons(enabled)
        if n:
            console.print(f"[dim]{n} running lane(s) reloaded their base bet share.[/]")
    if split in ("budget", "both"):
        console.print("[dim]Budget shares update when each lane restarts: "
                      "[cyan]wolfctl restart K[/].[/]")


def cmd_lane_add(name: str, src=None) -> None:
    """Add one enabled lane named `name`, copying lane `src`'s overrides (lane 1: none)."""
    src = 1 if src is None else int(src)
    name = (name or "").strip()
    try:
        if not LANE_NAME_RE.match(name):
            raise _CliError("invalid_argument", LANE_NAME_HELP, style="red", exit_code=2)
        raw = _lane_config_or_fail()
        before = json.loads(json.dumps(raw))
        reg = _lane_registry(raw)
        if src not in reg:
            raise _CliError("no_lane", f"No lane {src}.", style="red", exit_code=2)
        limit, running = _lanes_max(raw), _lanes_count(raw)
        if running >= limit:
            raise _CliError("lane_limit", f"This bot already runs {running} lane(s), the limit.",
                            style="red", exit_code=2,
                            hint="  Raise it with: wolfctl lanes --max N")
        lane = _next_lane_id(raw, reg)
        if lane > LANE_ID_MAX:
            raise _CliError("lane_ids_used",
                            f"Lane ids run out at {LANE_ID_MAX}: removed ids are never reused.",
                            style="red", exit_code=2)
    except _CliError as e:
        _cli_fail(e, False)
        return
    overrides = dict(reg[src]["overrides"]) if src > 1 else {}
    reg[lane] = {"name": name, "enabled": True, "overrides": overrides}
    _save_lane_registry(raw, reg, lane + 1)
    copied = f" with lane {src}'s overrides" if overrides else ""
    console.print(f"Lane {lane} ({name}) added{copied}.")
    _signal_share_change(before, raw)


def cmd_lane_remove(lane: int) -> None:
    """Drop lane `lane` from the registry; its id is never reused and its history stays."""
    try:
        if lane == 1:
            raise _CliError("lane_one", "Lane 1 holds the base config and can't be removed.",
                            style="red", exit_code=2)
        raw = _lane_config_or_fail()
        reg = _lane_registry(raw)
        if lane not in reg:
            raise _CliError("no_lane", f"No lane {lane}.", style="red", exit_code=2)
    except _CliError as e:
        _cli_fail(e, False)
        return
    before = json.loads(json.dumps(raw))
    next_id = _next_lane_id(raw, reg)
    name = reg.pop(lane)["name"]
    _save_lane_registry(raw, reg, next_id)
    console.print(f"Lane {lane} ({name}) removed.")
    _signal_share_change(before, raw)


def _config_set_plan(pairs: list) -> dict:
    """Validate every --config-set pair and work out the result, writing and signalling nothing.

    Lane pairs are checked first and the base pairs last, the order the writes
    happen in, so state ends up holding the new base config. Returns
    {"lanes": _plan_lane_changes result or None, "parsed", "old", "before", "msgs"}.
    Raises _CliError with the message and exit code the command has always used.
    """
    for p in pairs:
        if p.lower().startswith("api_key="):
            raise _CliError("api_key_refused", "Refusing to write api_key via --config-set.",
                            style="red", exit_code=2,
                            hint="  Set the WOLFBET_API_KEY environment variable instead "
                                 "(never in shell history / process args).")
    try:
        pairs, per_lane, lane_block = _split_lane_pairs(pairs)
    except ValueError as e:
        raise _CliError("invalid_argument", f"Invalid --config-set argument: {e}",
                        style="red", exit_code=2) from None
    plan = {"lanes": None, "parsed": {}, "old": {}, "before": {}, "msgs": []}
    if per_lane or lane_block:
        plan["lanes"] = _plan_lane_changes(per_lane, lane_block)
        if not pairs:
            return plan
    try:
        parsed = _parse_config_set_pairs(pairs)
    except ValueError as e:
        raise _CliError("invalid_argument", f"Invalid --config-set argument: {e}",
                        style="red", exit_code=2) from None
    if not load_config():
        raise _CliError("no_config", "No config saved yet: run the wizard first.",
                        hint="  Try: python3 wolfbet.py --setup-only", exit_code=1)
    keys = list(_CONFIG_KEYS) + list(_STREAK_TUPLE_KEYS)
    plan["before"] = {k: getattr(state, k, None) for k in keys}
    plan["old"] = {k: getattr(state, k, None) for k in parsed}
    try:
        plan["msgs"] = _apply_mutation(parsed) or []
    except ValueError as e:
        raise _CliError("invalid_value", f"Mutation failed: {e}", style="red", exit_code=2) from None
    plan["parsed"] = parsed
    return plan


def _config_dry_run_data(plan: dict) -> dict:
    """The diff a config-set would write: base keys (parsed and derived), lane overrides, warnings.

    A lane entry with removed=True drops the override, so the lane uses the base value again.
    """
    def entry(key, old, new):
        return {"key": key, "old": _config_show_value(key, old), "new": _config_show_value(key, new),
                "apply": "live" if key in _LIVE_RELOAD_KEYS or key == "name" else "restart"}

    base, lanes = [], {}
    for c in (plan["lanes"] or {}).get("changes", []):
        if c["lane"] is None:
            base.append({**entry(c["key"], c["old"], c["new"]), "apply": "restart"})
        else:
            lanes.setdefault(str(c["lane"]), []).append(
                {**entry(c["key"], c["old"], c["new"]), "removed": c["removed"]})
    if plan["parsed"]:
        derived = [k for k, v in plan["before"].items()
                   if k not in plan["parsed"] and getattr(state, k, None) != v]
        for k in list(plan["parsed"]) + derived:
            old = plan["old"][k] if k in plan["old"] else plan["before"][k]
            base.append(entry(k, old, getattr(state, k, None)))
    warnings = [m[1:].strip() for m in plan["msgs"] if isinstance(m, str) and m.startswith("⚠")]
    return {"base": base, "lanes": lanes, "warnings": warnings}


def _print_config_dry_run(data: dict) -> None:
    console.print(Rule("[bold cyan]Dry run: nothing saved[/]"))
    rows = [("", e) for e in data["base"]]
    rows += [(f"lane{lane}.", e) for lane, entries in data["lanes"].items() for e in entries]
    for prefix, e in rows:
        new = "base" if e.get("removed") else _fmt_config_value(e["key"], e["new"])
        console.print(f"  [yellow]{prefix}{e['key']}[/]: [dim]{_fmt_config_value(e['key'], e['old'])}[/]"
                      f"  →  [green]{new}[/]  [dim]({e['apply']})[/]")
    for w in data["warnings"]:
        console.print(f"  [yellow]⚠ {w}[/]")


def _write_lane_plan(lp: dict) -> None:
    """Write the planned lanes block, print each change and SIGHUP the lanes it touched."""
    for c in lp["changes"]:
        k, lane = c["key"], c["lane"]
        if lane is None and k in ("lanes", "lanes_max"):
            console.print(f"  [yellow]{k}[/]: [dim]{c['old']}[/]  →  [green]{c['new']}[/]")
        elif lane is None:
            console.print(f"  [yellow]lane_split[/]: [dim]{c['old']}[/]  →  [green]{c['new']}[/]  "
                          f"[dim]({_LANE_SPLIT_HELP[c['new']]})[/]")
        elif k == "name":
            console.print(f"  [yellow]lane{lane} name[/]: [dim]{c['old']}[/]  →  [green]{c['new']}[/]")
        elif c["removed"]:
            console.print(f"  [yellow]lane{lane}.{k}[/]: [dim]override removed (uses base)[/]")
        else:
            v = c["new"]
            console.print(f"  [yellow]lane{lane}.{k}[/]  →  "
                          f"[green]{_fmt_config_value(k, parse_streak_delay(v) if k in _STREAK_TUPLE_KEYS else v)}[/]")
    _backup_pre_registry_config()
    _write_config_raw(lp["raw"])
    console.print(f"\n[dim]Saved to[/] [cyan]{CONFIG_PATH}[/]")
    if lp["lanes"]:
        n = _signal_lane_daemons(lp["lanes"])
        if n:
            console.print(f"[dim]{n} running lane(s) reloaded live config.[/]")
        waiting: dict = {}
        for c in lp["changes"]:
            if c["lane"] is not None and c["key"] != "name":
                waiting.setdefault(c["lane"], []).append(c["key"])
        for lane, keys in sorted(waiting.items()):
            if later := _restart_only(keys):
                console.print(f"[yellow]Restart lane {lane} to apply:[/] {', '.join(later)} "
                              f"([cyan]wolfctl restart {lane}[/]).")
    _signal_share_change(lp["before"], lp["raw"])
    count = next((c["new"] for c in lp["changes"] if c["lane"] is None and c["key"] == "lanes"), None)
    if count is not None:
        console.print(f"[dim]Run [cyan]wolfctl lanes {count}[/] to start or stop lane processes.[/]")
    elif any(c["lane"] is None and c["key"] == "lane_split" for c in lp["changes"]):
        console.print("[dim]lane_split applies on the next lane start: run "
                      "[cyan]wolfctl restart[/].[/]")


def _lane_accounts(raw: dict) -> dict:
    reg = _lane_registry(raw)
    return {lane: _lane_account(raw, lane) for lane in sorted(reg) if reg[lane]["enabled"]}


def _lane_running(lane: int) -> bool:
    try:
        with open(_lane_path(_BASE_PID_PATH, lane)) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def _restart_lane(lane: int) -> bool:
    """Restart one lane's unit (lane 1 is the main service). False when systemd can't do it."""
    import shutil
    import subprocess
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    unit = "wolfbet" if lane <= 1 else f"wolfbet-lane@{lane}"
    try:
        done = subprocess.run([systemctl, "--user", "restart", unit],
                              capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _restart_changed_accounts(before: dict, after: dict) -> None:
    """Restart, one by one, the running lanes whose key, currency or vault proxy changed.

    A plain proxy URL already applies live on reload, so it alone restarts nothing.
    """
    def needs_restart(account) -> tuple:
        return account[:2] + account[3:] if account else ()

    for lane, account in after.items():
        if needs_restart(before.get(lane)) == needs_restart(account) or not _lane_running(lane):
            continue
        if _restart_lane(lane):
            console.print(f"Lane {lane} restarted: its key, currency or proxy changed.")
        else:
            console.print(f"[yellow]Restart lane {lane} to apply: "
                          f"[cyan]wolfctl restart {lane}[/][/]")


def cmd_config_set(pairs: list, dry_run: bool = False, as_json: bool = False):
    """Apply KEY=VALUE mutations, persist them, then restart the lanes whose account changed."""
    before = _lane_accounts(_read_config_raw())
    _config_set_write(pairs, dry_run, as_json)
    if not dry_run:
        _restart_changed_accounts(before, _lane_accounts(_read_config_raw()))


def _config_set_write(pairs: list, dry_run: bool = False, as_json: bool = False):
    """Apply KEY=VALUE mutations to the saved config, then persist. No wizard.

    Every pair is validated before anything is written, so a bad pair leaves the
    file and the running lanes untouched. With dry_run, print the diff instead
    (JSON with as_json) and write or signal nothing. api_key is refused:
    credentials belong in env vars, not shell history.
    """
    try:
        plan = _config_set_plan(pairs)
    except _CliError as e:
        _cli_fail(e, as_json)
        return
    if dry_run:
        data = _config_dry_run_data(plan)
        if as_json:
            _print_json(data)
        else:
            _print_config_dry_run(data)
        return

    if plan["lanes"]:
        console.print(Rule("[bold cyan]Lane config updated[/]"))
        _write_lane_plan(plan["lanes"])
    parsed = plan["parsed"]
    if not parsed:
        return
    old, mutation_msgs = plan["old"], plan["msgs"]

    save_config()
    console.print(Rule("[bold cyan]Config updated[/]"))
    for canonical in parsed:
        new = getattr(state, canonical, None)
        console.print(
            f"  [yellow]{canonical}[/]: "
            f"[dim]{_fmt_config_value(canonical, old[canonical])}[/]"
            f"  →  [green]{_fmt_config_value(canonical, new)}[/]"
        )
    # v2.30.6: surface clamp warnings (and any other advisory notes) so
    # silently swallowed input becomes visible to the operator.
    for m in mutation_msgs:
        if isinstance(m, str) and m.startswith("⚠"):
            console.print(f"  [yellow]{m}[/]")
    console.print(f"\n[dim]Saved to[/] [cyan]{CONFIG_PATH}[/]")

    unsafe = _restart_only(parsed)
    if unsafe:
        console.print(
            f"[yellow]Restart every lane to apply:[/] {', '.join(unsafe)} "
            "([cyan]wolfctl restart[/])."
        )

    try:
        if not os.path.exists(PID_PATH):
            console.print("[dim]Daemon not running; changes apply on next "
                          "[cyan]wolfctl start[/].[/]")
            return
        with open(PID_PATH) as f:
            daemon_pid = int(f.read().strip())
        if not hasattr(signal, "SIGHUP"):
            console.print("[dim]SIGHUP not supported on this platform; "
                          "restart the daemon to apply.[/]")
            return
        os.kill(daemon_pid, signal.SIGHUP)
        console.print("[dim]Daemon reloaded live config.[/]")
    except (OSError, ValueError):
        console.print("[dim]Daemon PID stale; changes apply on next "
                      "[cyan]wolfctl start[/].[/]")
    # v2.35.0: the base config is shared: reload lanes 2..N too.
    n_lanes = _signal_lane_daemons([k for k in _lane_ids(_read_config_raw()) if k > 1])
    if n_lanes:
        console.print(f"[dim]{n_lanes} more lane(s) reloaded live config.[/]")


# ══════════════════════════════════════════════════════════
#  RETENTION / CLEANUP  (v2.26.0)
# ══════════════════════════════════════════════════════════
def cmd_cleanup(days=None):
    """Purge old bet rows from the DB, preserving session aggregates.

    Delegates to shared/core/database.cleanup_old_bets: session rows
    (with pre-aggregated stats) are preserved, only per-bet rows for
    sessions ended more than `days` ago are deleted.
    """
    load_config()
    n = int(days) if days is not None else state.purge_days
    n = max(1, min(30, n))
    try:
        deleted = cleanup_old_bets(DB_PATH, bet_age_days=n)
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Cleanup failed{detail}.[/]")
        sys.exit(1)
    deleted = int(deleted or 0)
    if deleted:
        console.print(
            f"[green]✓[/] Purged [bold]{deleted}[/] bet rows from sessions older than "
            f"[bold]{n}[/] days. Session aggregates preserved."
        )
    else:
        console.print(f"[dim]Nothing to purge ({n}-day window; no old session bets found).[/]")


# ══════════════════════════════════════════════════════════
#  BPS DIAGNOSTICS  (v2.26.1: --benchmark, --diagnose)
# ══════════════════════════════════════════════════════════
# _percentile: extracted to core.diagnostics in v2.28.0. TG /benchmark
# will use the same helper.
_percentile = percentile


def _bench_lanes(n: int, lanes: int) -> dict:
    """v2.35.0: `lanes` concurrent clients each fire `n` read-only GETs.

    Each client gets its own requests.Session (like separate lane processes).
    Returns {"rps": aggregate req/s, "p50": ms, "ok": int, "total": int,
    "limit": header|None, "remaining_min": header|None, "s429": int}.
    """
    import requests as _rq
    url = f"{API_BASE}/v1/user/balances"
    lock = threading.Lock()
    out = {"samples": [], "ok": 0, "s429": 0, "limit": None, "remaining_min": None}

    def _client():
        sess = _rq.Session()
        sess.proxies.update(getattr(_http_session, "proxies", {}) or {})
        for _ in range(n):
            t0 = time.time()
            try:
                r = sess.get(url, headers=_headers("dice"), timeout=15)
            except Exception:
                continue
            ms = (time.time() - t0) * 1000
            with lock:
                out["samples"].append(ms)
                out["ok"] += r.status_code == 200
                out["s429"] += r.status_code == 429
                lim = r.headers.get("x-ratelimit-limit")
                rem = r.headers.get("x-ratelimit-remaining")
                if lim is not None:
                    out["limit"] = lim
                if rem is not None:
                    try:
                        rv = int(rem)
                        cur = out["remaining_min"]
                        out["remaining_min"] = rv if cur is None else min(cur, rv)
                    except ValueError:
                        pass

    t_start = time.time()
    threads = [threading.Thread(target=_client) for _ in range(lanes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = max(1e-6, time.time() - t_start)
    samples = sorted(out["samples"])
    return {"rps": len(samples) / wall,
            "p50": _percentile(samples, 0.50) if samples else 0.0,
            "ok": out["ok"], "total": n * lanes, "s429": out["s429"],
            "limit": out["limit"], "remaining_min": out["remaining_min"]}


def recommend_lanes(base_rps: float, rows: dict) -> int:
    """Highest lane count whose throughput still gains ≥ 30% per added lane
    over the previous count, with no 429s. Pure helper (tested)."""
    best, prev = 1, base_rps
    for k in sorted(rows):
        r = rows[k]
        if r["s429"] or r["rps"] < prev * 1.3:
            break
        best, prev = k, r["rps"]
    return best


def cmd_benchmark_lanes(n: int, lanes: int):
    """Probe 1..N concurrent clients and recommend a lane count (no bets)."""
    from rich.table import Table
    lanes = max(2, min(_lanes_max(_read_config_raw()), int(lanes)))
    console.print(f"[dim]Lane benchmark: 1..{lanes} concurrent clients × {n} × "
                  f"GET /v1/user/balances (read-only, no bets) ...[/]")
    rows = {}
    for k in range(1, lanes + 1):
        rows[k] = _bench_lanes(n, k)
    tbl = Table(title="Concurrent clients", title_style="bold cyan")
    for col in ("lanes", "req/s", "×1 lane", "p50 ms", "ok", "429", "limit", "min remaining"):
        tbl.add_column(col, justify="right")
    base = rows[1]["rps"] or 1e-6
    for k, r in rows.items():
        tbl.add_row(str(k), f"{r['rps']:.1f}", f"{r['rps'] / base:.2f}×", f"{r['p50']:.0f}",
                    f"{r['ok']}/{r['total']}", str(r["s429"]),
                    str(r["limit"] or "-"), str(r["remaining_min"] if r["remaining_min"] is not None else "-"))
    console.print(tbl)
    rec = recommend_lanes(rows[1]["rps"], {k: v for k, v in rows.items() if k > 1})
    console.print(f"\n[bold]Recommended lanes:[/] [bold magenta]{rec}[/]  "
                  "[dim](more lanes stop adding speed or start getting throttled)[/]")
    console.print("[dim]Read-only endpoint; the bet endpoint has its own quota. "
                  "Confirm live: `wolfctl lanes N` then compare total bps in `wolfctl status`.[/]")


def cmd_benchmark(n=None, lanes=None):
    """Fire N read-only API requests and report latency distribution.

    Uses GET /v1/user/balances via the same _http_session as production -
    so proxy, User-Agent, and connection pooling all match the live path.
    No bets placed, no DB write, no session started.
    """
    from rich.table import Table
    n = 20 if n is None else int(n)
    n = max(3, min(200, n))
    if not load_config():
        console.print("[red]No config saved yet: run the wizard first.[/]")
        sys.exit(1)
    if lanes and int(lanes) > 1:
        return cmd_benchmark_lanes(n, lanes)
    url = f"{API_BASE}/v1/user/balances"
    console.print(
        f"[dim]Benchmarking {n} × GET /v1/user/balances "
        f"(proxy={_mask_proxy(state.proxy)}) ...[/]"
    )
    samples = []
    statuses = []
    failures = 0
    for i in range(n):
        t0 = time.time()
        try:
            r = _http_session.get(url, headers=_headers("dice"), timeout=15)
            samples.append((time.time() - t0) * 1000)
            statuses.append(r.status_code)
        except Exception:
            failures += 1
            statuses.append("ERR")
    if not samples:
        console.print("[red]All requests failed. Check API key + network.[/]")
        sys.exit(1)
    samples.sort()
    p50 = _percentile(samples, 0.50)
    p99 = _percentile(samples, 0.99)
    mean = sum(samples) / len(samples)
    tbl = Table(title="API latency (ms)", title_style="bold cyan", show_lines=False)
    tbl.add_column("min",  style="green",   justify="right")
    tbl.add_column("p50",  style="cyan",    justify="right")
    tbl.add_column("mean", style="cyan",    justify="right")
    tbl.add_column("p99",  style="yellow",  justify="right")
    tbl.add_column("max",  style="red",     justify="right")
    tbl.add_column("ok",   style="dim",     justify="right")
    ok_count = sum(1 for s in statuses if s == 200)
    tbl.add_row(f"{min(samples):.0f}", f"{p50:.0f}", f"{mean:.0f}",
                f"{p99:.0f}", f"{max(samples):.0f}", f"{ok_count}/{n}")
    console.print(tbl)
    # Theoretical max BPS from p50
    max_bps = 1000.0 / p50 if p50 > 0 else 0.0
    console.print(f"\n[dim]Theoretical max BPS from p50:[/] "
                  f"[bold magenta]{max_bps:.1f}[/] bps")
    # Verdict: shared with TG /benchmark (extracted to core.diagnostics in v2.28.0).
    colour, msg = benchmark_verdict(p50=p50, p99=p99, statuses=statuses, sample_count=n)
    console.print(f"[bold {colour}]{msg}[/]")
    if failures:
        console.print(f"[dim]Note: {failures}/{n} network errors.[/]")


def cmd_diagnose():
    """One-shot bottleneck analysis of the running daemon session.

    Reads STATE_PATH and picks the top blocker (API latency vs CLI
    overhead vs configured delay vs rate-limit vs auto-start throttle).
    """
    from rich.panel import Panel
    if not os.path.exists(STATE_PATH):
        console.print(
            "[yellow]No running daemon session found.[/]\n"
            "  Start one with [cyan]wolfctl start[/] then re-run "
            "[cyan]wolfctl diagnose[/]."
        )
        return
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Failed to read state file{detail}.[/]")
        return

    api_p50   = float(d.get("api_p50_ms") or 0)
    api_p99   = float(d.get("api_p99_ms") or 0)
    overhead  = float(d.get("overhead_ms") or 0)
    cycle_ms  = float(d.get("cycle_ms") or 0)
    # v2.30.4: windowed cycle metrics + windowed recent BPS.
    cycle_p50 = float(d.get("cycle_p50_ms") or cycle_ms)
    cycle_p99 = float(d.get("cycle_p99_ms") or 0)
    cycle_n   = int(d.get("cycle_sample_count") or 0)
    recent_bps = float(d.get("bets_per_second_recent") or 0)
    bet_delay = float(d.get("bet_delay") or 0)
    bps       = float(d.get("bets_per_second") or 0)
    tmax      = float(d.get("theoretical_max_bps") or 0)
    rate_rem  = d.get("rate_remaining")
    rate_lim  = d.get("rate_limit")
    auto_rem  = d.get("auto_start_remaining")
    game      = d.get("game", "?")
    strategy  = d.get("strategy", "?")
    pipe_eff  = d.get("pipeline_effective") or 1
    delay_ms  = bet_delay * 1000

    # v2.30.4: Recent BPS + Efficiency use the same window as Theoretical,
    # so the ratio is algebraically consistent. Session-wide Actual stays
    # visible for context. `>` prefix flags the (rare) case where the
    # windowed actual exceeds theoretical (api_avg drift within the window).
    _eff_ref = recent_bps if recent_bps > 0 else bps
    if tmax > 0:
        _eff_raw = _eff_ref / tmax * 100.0
        eff_display = ">100%" if _eff_raw > 100.0 else f"{_eff_raw:.0f}%"
    else:
        eff_display = "-"

    lines = [
        f"[dim]Game:[/] [cyan]{game}[/]  ·  "
        f"[dim]Strategy:[/] [cyan]{strategy}[/]  ·  "
        f"[dim]Pipeline:[/] [cyan]{pipe_eff}[/]",
        "",
        (f"[bold]Actual:[/] [magenta]{bps:.1f}[/] BPS (session)  ·  "
         f"[bold]Recent:[/] [magenta]{recent_bps:.1f}[/] BPS  ·  "
         f"[bold]Theoretical:[/] [magenta]{tmax:.1f}[/] BPS  ·  "
         f"[bold]Efficiency:[/] [magenta]{eff_display}[/]"),
        (f"[dim]Cycle:[/] median [cyan]{cycle_p50:.0f}ms[/]  "
         f"p99 [cyan]{cycle_p99:.0f}ms[/]  "
         f"[dim](n={cycle_n} samples)[/]"),
        # v2.30.3: the three components come from different windows/formulas,
        # so their sum is a decomposition, not a real equality. Show the
        # measured cycle separately so a divergence is a real signal.
        f"[dim]Cycle breakdown:[/] api_p50=[cyan]{api_p50:.0f}ms[/] "
        f"+ overhead=[cyan]{overhead:.0f}ms[/] "
        f"+ delay=[cyan]{delay_ms:.0f}ms[/] "
        f"[dim]≈[/] [magenta]{api_p50 + overhead + delay_ms:.0f}ms[/]/bet "
        f"[dim](decomposition)[/]",
        f"[dim]Measured cycle:[/]  [magenta]{cycle_ms:.0f}ms[/]/bet "
        f"[dim](median of last {cycle_n} iterations)[/]",
    ]
    if rate_rem is not None and rate_lim:
        lines.append(f"[dim]Rate limit:[/] {rate_rem}/{rate_lim} remaining")
    if auto_rem is not None:
        c = "yellow" if int(auto_rem) <= 3 else "cyan"
        lines.append(f"[dim]/auto/start quota:[/] [{c}]{auto_rem}/20[/]")

    # v2.27.0: DB writer visibility
    db_mode = d.get("db_write_mode") or "sync"
    dbw = d.get("db_writer_stats") or {}
    dbw_q  = int(d.get("db_writer_queue") or 0)
    dbw_fb = int(dbw.get("fallback_sync") or 0)
    dbw_ba = int(dbw.get("batches") or 0)
    dbw_fl = int(dbw.get("flushed") or 0)
    dbw_hwm = int(dbw.get("queue_high_watermark") or 0)
    if db_mode == "async":
        lines.append(
            f"[dim]DB writer:[/] mode=[cyan]async[/]  queue=[cyan]{dbw_q}/1000[/]  "
            f"batches=[cyan]{dbw_ba}[/]  flushed=[cyan]{dbw_fl}[/]  "
            f"fallback_sync=[{'red' if dbw_fb else 'cyan'}]{dbw_fb}[/]  hwm=[cyan]{dbw_hwm}[/]"
        )
    else:
        lines.append(f"[dim]DB writer:[/] mode=[yellow]sync[/] (legacy: per-bet fsync)")

    # Deterministic top blocker
    blockers = [
        ("Network / API latency",  api_p50),
        ("CLI overhead",           overhead),
        ("User-configured delay",  delay_ms),
    ]
    top_name, top_val = max(blockers, key=lambda x: x[1])

    advice = []
    if top_name == "Network / API latency" and api_p50 >= 100:
        advice.append("• Run [cyan]wolfctl benchmark 20[/] to isolate the network path.")
        advice.append("• Consider a proxy in a region closer to wolfbet.com, "
                      "or move to a faster server.")
    elif top_name == "User-configured delay" and delay_ms > 10:
        advice.append(f"• You have bet_delay={bet_delay:.3f}s. Drop it with "
                      "[cyan]wolfctl config-set delay=0[/] for max throughput.")
    elif top_name == "CLI overhead" and overhead > 30:
        if db_mode == "sync":
            advice.append("• CLI overhead high AND db_write_mode=sync: flip to async: "
                          "[cyan]wolfctl config-set dbmode=async && wolfctl restart[/]")
        elif dbw_fb == 0 and dbw_hwm < 100 and int(rate_rem or 999) > 200:
            # v2.30.4: DB writer + rate limits are all healthy, so overhead
            # is almost certainly shared-tenancy CPU/disk jitter (VPS silicon),
            # not a fixable code issue. Prior investigation confirmed this
            # pattern on the older-EPYC VPS: 14% slower CPU + 40% slower disk.
            advice.append("• CLI overhead is high on this host.")
            advice.append("  DB writer + rate limits are healthy: overhead is likely "
                          "shared-tenancy CPU/disk jitter (VPS silicon), not a fixable "
                          "code issue. Consider redeploying on a fresh VPS instance "
                          "(silicon lottery) or a higher-tier plan.")
        else:
            advice.append("• CLI overhead is high. Check disk I/O (DB writes) "
                          "and CPU load on the daemon host.")
    if dbw_fb > 0:
        advice.append(f"• DB writer queue overflowed {dbw_fb} times "
                      "(async writer is saturating: check disk perf).")
    elif int(rate_rem or 999) <= 3:
        advice.append("• Rate-limit headroom is low. WolfBet may throttle soon.")
    if pipe_eff == 1 and strategy in ("Flat Bet",) and delay_ms == 0:
        advice.append("• Strategy is Flat with no delay: try "
                      "[cyan]wolfctl pipeline 3[/] for concurrent bets.")
    if not advice:
        advice.append("• Nothing obvious. Session is running near its natural ceiling.")

    lines.append("")
    lines.append(f"[bold yellow]Top blocker:[/] {top_name} "
                 f"([magenta]{top_val:.0f}ms[/])")
    lines.append("")
    lines.append("[bold]Advice:[/]")
    lines.extend(advice)

    console.print(Panel("\n".join(lines),
                        title="💡 Diagnostic bottleneck analysis",
                        border_style="cyan"))


def _retention_worker():
    """Hourly bet-row purge: mirrors tg/engine.py:747-758.

    Runs on the CLI daemon; exits when the session ends.
    """
    while state.running or state.paused:
        for _ in range(3600):
            if not (state.running or state.paused):
                return
            time.sleep(1)
        try:
            deleted = cleanup_live_bets(DB_PATH, bet_age_days=state.purge_days)
            if deleted:
                logger.info("RETENTION purged %d bet rows (>%dd)", deleted, state.purge_days)
        except Exception as e:
            logger.warning("retention purge failed: %s", e)


# ══════════════════════════════════════════════════════════
#  DATABASE QUERY COMMANDS
# ══════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════
#  ANALYTICS: lifetime aggregates (v2.23.0)
# ══════════════════════════════════════════════════════════
def _analytics_lifetime(where: str = "", params=()) -> dict:
    """Top-line aggregates (optionally date-windowed via `where` from
    `_stats_date_window`). Returns a dict with all-zero defaults on empty DB."""
    conn = _db_conn()
    try:
        row = conn.execute(f"""
            SELECT COUNT(*)                     AS sessions_total,
                   COALESCE(SUM(total_bets), 0) AS bets_total,
                   COALESCE(SUM(wins), 0)       AS wins_total,
                   COALESCE(SUM(losses), 0)     AS losses_total,
                   COALESCE(SUM(wagered), 0)    AS wagered_total,
                   COALESCE(SUM(profit), 0)     AS profit_total,
                   COALESCE(MAX(profit), 0)     AS best_profit,
                   COALESCE(MIN(profit), 0)     AS worst_profit,
                   SUM(CASE WHEN profit >= 0 THEN 1 ELSE 0 END) AS sessions_in_profit,
                   SUM(CASE WHEN profit <  0 THEN 1 ELSE 0 END) AS sessions_in_loss
            FROM sessions{where}
        """, tuple(params)).fetchone()
    finally:
        conn.close()
    keys = ("sessions_total", "bets_total", "wins_total", "losses_total",
            "wagered_total", "profit_total", "best_profit", "worst_profit",
            "sessions_in_profit", "sessions_in_loss")
    d = dict(zip(keys, row))
    d["win_rate_pct"] = (d["wins_total"] / d["bets_total"] * 100) if d["bets_total"] > 0 else 0.0
    d["sessions_in_profit"] = d["sessions_in_profit"] or 0
    d["sessions_in_loss"]   = d["sessions_in_loss"]   or 0
    return d


def _analytics_by_game(where: str = "", params=()) -> list:
    """Aggregate sessions grouped by game (optionally date-windowed)."""
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT COALESCE(game, '?')          AS game,
                   COUNT(*)                     AS sessions,
                   COALESCE(SUM(total_bets), 0) AS bets,
                   COALESCE(SUM(wins), 0)       AS wins,
                   COALESCE(SUM(losses), 0)     AS losses,
                   COALESCE(SUM(profit), 0)     AS profit,
                   COALESCE(SUM(wagered), 0)    AS wagered
            FROM sessions{where}
            GROUP BY game
            ORDER BY profit DESC
        """, tuple(params)).fetchall()
    finally:
        conn.close()
    return [dict(zip(("game", "sessions", "bets", "wins", "losses", "profit", "wagered"), r))
            for r in rows]


def _analytics_by_lane(where: str = "", params=()) -> list:
    """v2.35.0: aggregate sessions grouped by parallel lane."""
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT 'Lane ' || COALESCE(lane, 1)  AS lane,
                   COUNT(*)                     AS sessions,
                   COALESCE(SUM(total_bets), 0) AS bets,
                   COALESCE(SUM(wins), 0)       AS wins,
                   COALESCE(SUM(losses), 0)     AS losses,
                   COALESCE(SUM(profit), 0)     AS profit,
                   COALESCE(SUM(wagered), 0)    AS wagered
            FROM sessions{where}
            GROUP BY COALESCE(lane, 1)
            ORDER BY COALESCE(lane, 1)
        """, tuple(params)).fetchall()
    finally:
        conn.close()
    return [dict(zip(("lane", "sessions", "bets", "wins", "losses", "profit", "wagered"), r))
            for r in rows]


def _analytics_by_strategy(where: str = "", params=()) -> list:
    """Aggregate sessions grouped by strategy (optionally date-windowed)."""
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT COALESCE(strategy, '?')      AS strategy,
                   COUNT(*)                     AS sessions,
                   COALESCE(SUM(total_bets), 0) AS bets,
                   COALESCE(SUM(wins), 0)       AS wins,
                   COALESCE(SUM(losses), 0)     AS losses,
                   COALESCE(SUM(profit), 0)     AS profit,
                   COALESCE(SUM(wagered), 0)    AS wagered
            FROM sessions{where}
            GROUP BY strategy
            ORDER BY profit DESC
        """, tuple(params)).fetchall()
    finally:
        conn.close()
    return [dict(zip(("strategy", "sessions", "bets", "wins", "losses", "profit", "wagered"), r))
            for r in rows]


def _analytics_top_sessions(order: str = "profit_desc", limit: int = 5,
                            where: str = "", params=()) -> list:
    """Top-N sessions by a whitelisted order key (optionally date-windowed)."""
    order_sql = {
        "profit_desc": "profit DESC",
        "profit_asc":  "profit ASC",
        "bets_desc":   "total_bets DESC",
    }.get(order, "profit DESC")
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT id, COALESCE(game, '?'), COALESCE(strategy, '?'),
                   COALESCE(total_bets, 0), COALESCE(wins, 0), COALESCE(losses, 0),
                   COALESCE(profit, 0), COALESCE(wagered, 0), started_at
            FROM sessions{where}
            ORDER BY {order_sql}
            LIMIT ?
        """, (*params, limit)).fetchall()
    finally:
        conn.close()
    return [dict(zip(
        ("id", "game", "strategy", "bets", "wins", "losses",
         "profit", "wagered", "started_at"), r)) for r in rows]


def _print_analytics_table(title: str, rows: list, key_attr: str, key_label: str):
    """Render a by-X aggregation table."""
    if not rows:
        return
    t = Table(title=title, box=box.SIMPLE, expand=True)
    t.add_column(key_label, style="cyan")
    t.add_column("Sessions", justify="right")
    t.add_column("Bets",     justify="right")
    t.add_column("Win rate", justify="right")
    t.add_column("P/L",      justify="right")
    t.add_column("Wagered",  justify="right")
    for r in rows:
        wr = f"{r['wins']/r['bets']*100:.1f}%" if r['bets'] > 0 else "-"
        pc = 'green' if r['profit'] >= 0 else 'red'
        ps = '+' if r['profit'] >= 0 else ''
        t.add_row(str(r[key_attr]), f"{r['sessions']:,}", f"{r['bets']:,}",
                  wr, f"[{pc}]{ps}{r['profit']:.8f}[/]", f"{r['wagered']:.8f}")
    console.print(t)


def _print_top_sessions_table(title: str, rows: list):
    if not rows:
        return
    t = Table(title=title, box=box.SIMPLE, expand=True)
    t.add_column("ID",       justify="right")
    t.add_column("Game",     style="cyan")
    t.add_column("Strategy", style="magenta")
    t.add_column("Bets",     justify="right")
    t.add_column("Win rate", justify="right")
    t.add_column("P/L",      justify="right")
    t.add_column("Wagered",  justify="right")
    t.add_column("Started",  style="dim")
    for r in rows:
        wr = f"{r['wins']/r['bets']*100:.1f}%" if r['bets'] > 0 else "-"
        pc = 'green' if r['profit'] >= 0 else 'red'
        ps = '+' if r['profit'] >= 0 else ''
        started_short = (r['started_at'] or "").replace("T", " ")[:19]
        t.add_row(str(r['id']), r['game'], r['strategy'], f"{r['bets']:,}",
                  wr, f"[{pc}]{ps}{r['profit']:.8f}[/]", f"{r['wagered']:.8f}",
                  started_short)
    console.print(t)


def _analytics_data(since: str = None, until: str = None, lane: int = None) -> dict:
    """Aggregates over the sessions in the window: totals, time span, by game / strategy / lane, top 5 each way."""
    if not os.path.exists(DB_PATH):
        raise _CliError("no_database", "No database found: run a session first.")
    migrate_db(DB_PATH)  # v2.35.0: lane column on DBs the bot hasn't opened yet
    try:
        where, wparams, _ = _stats_date_window(since, until, lane)
    except ValueError as e:
        raise _CliError("invalid_window", str(e), style="red") from None
    conn = _db_conn()
    try:
        span = _span_dict(*_sessions_time_span(conn, where, wparams))
    finally:
        conn.close()
    return {
        "window": _json_window(since, until, lane),
        "lifetime": _analytics_lifetime(where, wparams),
        "span": span,
        "by_game": _analytics_by_game(where, wparams),
        "by_strategy": _analytics_by_strategy(where, wparams),
        "by_lane": _analytics_by_lane(where, wparams),
        "top_best": _analytics_top_sessions("profit_desc", 5, where, wparams),
        "top_worst": _analytics_top_sessions("profit_asc", 5, where, wparams),
    }


def cmd_analytics(since: str = None, until: str = None, lane: int = None, as_json: bool = False):
    """Analytics report: aggregates over all saved sessions, optionally
    restricted to a started_at date window (v2.31.0)."""
    try:
        data = _analytics_data(since, until, lane)
    except _CliError as e:
        _cli_fail(e, as_json)
        return
    if as_json:
        _print_json(data)
        return
    wlabel = _stats_date_window(since, until, lane)[2]
    lifetime = data["lifetime"]
    if lifetime["sessions_total"] == 0:
        console.print("[yellow]No sessions in the DB yet: run a session first.[/]")
        return

    console.print(Rule(
        f"[bold cyan]{'Range' if (since or until) else 'Lifetime'} analytics[/]: "
        f"{lifetime['sessions_total']:,} sessions, "
        f"{lifetime['bets_total']:,} bets, {lifetime['wagered_total']:.8f} wagered"
        f"{wlabel}"))

    # Top-line panel
    p = lifetime['profit_total']
    pc = 'green' if p >= 0 else 'red'
    ps = '+' if p >= 0 else ''
    grid = Table.grid(padding=(0, 2))
    grid.add_row("Total sessions:",     f"{lifetime['sessions_total']:,}")
    a_first, a_last = _span_range(data["span"])
    a_span, a_bet = data["span"]["span_sec"], data["span"]["bet_sec"]
    if a_first:
        grid.add_row("Time range:", f"{a_first.isoformat(' ', 'seconds')} → "
                                    f"{a_last.isoformat(' ', 'seconds')}")
    grid.add_row("Time span:",          _fmt_duration(a_span))
    grid.add_row("Bet time:",           _fmt_duration(a_bet))
    grid.add_row("Sessions in profit:", f"{lifetime['sessions_in_profit']:,}")
    grid.add_row("Sessions in loss:",   f"{lifetime['sessions_in_loss']:,}")
    grid.add_row("Total bets:", f"{lifetime['bets_total']:,}   W: {lifetime['wins_total']:,}   L: {lifetime['losses_total']:,}")
    grid.add_row("Win rate:",           f"{lifetime['win_rate_pct']:.1f}%")
    grid.add_row("Total wagered:",      f"{lifetime['wagered_total']:.8f}")
    grid.add_row("Total P/L:",          f"[{pc}]{ps}{p:.8f}[/]")
    grid.add_row("Best session:",       f"+{lifetime['best_profit']:.8f}")
    grid.add_row("Worst session:",      f"{lifetime['worst_profit']:.8f}")
    console.print(Panel(grid, title="[bold]💰 Overall[/]", border_style=pc))

    _print_analytics_table("[bold]🎲 By game[/]",     data["by_game"],     "game",     "Game")
    _print_analytics_table("[bold]🎯 By strategy[/]", data["by_strategy"], "strategy", "Strategy")
    if len(data["by_lane"]) > 1:
        _print_analytics_table("[bold]🛣 By lane[/]", data["by_lane"], "lane", "Lane")

    _print_top_sessions_table("[bold]🏆 Top 5 best sessions[/]", data["top_best"])
    _print_top_sessions_table("[bold]🔻 Top 5 worst sessions[/]", data["top_worst"])

    console.print("\n[dim]  Use --stats for recent 20 sessions in detail, "
                  "or --session-bets ID to drill into one.[/]\n")


def _fmt_duration(sec) -> str:
    """v2.34.0: '1mo 20d 2h 3m 2s' (30-day months). Leading zero units are
    dropped; seconds are always shown."""
    sec = max(0, int(sec or 0))
    parts = []
    for name, size in (("mo", 30 * 86400), ("d", 86400), ("h", 3600), ("m", 60)):
        q, sec = divmod(sec, size)
        if q or parts:
            parts.append(f"{q}{name}")
    parts.append(f"{sec}s")
    return " ".join(parts)


def _sessions_time_span(conn, where: str = "", params=()):
    """v2.34.0: (first_start, last_end, span_sec, bet_sec) for sessions in the window.

    span = first started_at → last ended_at (a running session ends "now").
    bet  = Σ per-session (ended_at − started_at): pauses between sessions excluded.
    Returns (None, None, 0, 0) when no row has a parseable started_at.
    """
    def _local(ts):
        t = datetime.fromisoformat(ts)
        # tz-aware rows (e.g. written with a UTC offset) → local naive, so they
        # compare with the naive local timestamps the bot writes.
        return t.astimezone().replace(tzinfo=None) if t.tzinfo else t

    now = datetime.now()
    first = last = None
    bet = 0.0
    for started, ended in conn.execute(
            f"SELECT started_at, ended_at FROM sessions{where}", tuple(params)):
        try:
            t0 = _local(started)
            t1 = _local(ended) if ended else now
        except (TypeError, ValueError):
            continue
        bet += max(0.0, (t1 - t0).total_seconds())
        first = t0 if first is None or t0 < first else first
        last = t1 if last is None or t1 > last else last
    if first is None:
        return None, None, 0, 0
    return first, last, max(0.0, (last - first).total_seconds()), bet


def _stats_date_window(since: str = None, until: str = None, lane: int = None):
    """v2.30.9: build an optional date-window clause for the sessions table.

    `since`/`until` accept bare dates (YYYY-MM-DD) or full ISO timestamps.
    A bare `until` date is extended to end-of-day so the day is inclusive.
    ISO strings compare correctly lexically in SQLite. Returns
    (clause, params, label) where clause is "" or " WHERE ..." and label
    describes the window for headers.
    """
    since = _normalize_window_bound(since)
    until = _normalize_window_bound(until)
    conds, params = [], []
    if since:
        conds.append("started_at >= ?")
        params.append(since)
    if until:
        u = until if len(until) > 10 else until + "T23:59:59.999999"
        conds.append("started_at <= ?")
        params.append(u)
    if lane:
        # v2.35.0: rows from before lanes existed count as lane 1
        conds.append("COALESCE(lane, 1) = ?")
        params.append(int(lane))
    if not conds:
        return "", [], ""
    clause = " WHERE " + " AND ".join(conds)
    label = ""
    if since or until:
        label = (f"  [{(since or '...').replace('T', ' ')} → "
                 f"{(until or '...').replace('T', ' ')}]")
    if lane:
        label += f"  \\[lane {int(lane)}]"   # escaped: Rich would eat [lane N] as a tag
    return clause, params, label


def _normalize_window_bound(value):
    """v2.33.0: turn a --since/--until value into the stored ISO form.

    started_at is stored as `datetime.isoformat()` (`2026-11-13T11:35:20.75`).
    A bound typed with a space (`2026-11-13 14:00`) compared wrong as a string
    (' ' sorts before 'T'), so every session that day matched. Swap the first
    space for 'T' and validate. Raises ValueError on anything unparseable.
    """
    if not value:
        return None
    v = str(value).strip().replace(" ", "T", 1)
    try:
        datetime.fromisoformat(v)
    except ValueError:
        raise ValueError(
            f'Invalid --since/--until "{value}": use YYYY-MM-DD[ HH:MM[:SS]]') from None
    return v


_STATS_SESSION_COLS = (
    "id", "started_at", "ended_at", "currency", "strategy", "multiplier", "base_bet",
    "total_bets", "wins", "losses", "profit", "wagered", "start_balance", "end_balance",
    "max_win_streak", "max_loss_streak", "highest_balance", "lowest_balance",
    "highest_win", "biggest_loss", "bets_per_minute", "bets_per_second",
    "peak_bps", "low_bps", "peak_bpm", "low_bpm", "lane")
_STATS_TOTAL_COLS = (
    "sessions", "bets", "wins", "losses", "profit", "wagered",
    "best_win_streak", "best_loss_streak", "best_profit", "worst_profit",
    "max_bets", "avg_bets", "peak_balance", "best_win", "worst_loss", "avg_bpm",
    "peak_bps", "peak_bpm", "avg_bps", "max_wins", "avg_wins", "max_losses", "avg_losses")


def _stats_data(since: str = None, until: str = None, lane: int = None) -> dict:
    """Recent sessions in the window (20, or 50 for a date range), the window's totals and time span."""
    if not os.path.exists(DB_PATH):
        raise _CliError("no_database", "No database found.")
    migrate_db(DB_PATH)  # v2.35.0: lane column on DBs the bot hasn't opened yet
    try:
        where, params, _ = _stats_date_window(since, until, lane)
    except ValueError as e:
        raise _CliError("invalid_window", str(e), style="red") from None
    # v2.30.9: range reviews want more rows than the default 20.
    limit = 50 if (since or until) else 20
    conn = _db_conn()
    try:
        rows = conn.execute(f"""
            SELECT id, started_at AS started, ended_at AS ended,
                   UPPER(currency) AS cur, strategy, multiplier, base_bet,
                   total_bets, wins, losses, profit, wagered,
                   start_balance, end_balance,
                   max_win_streak, max_loss_streak,
                   COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
                   COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
                   COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
                   COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
                   COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0),
                   COALESCE(lane, 1)
            FROM sessions{where} ORDER BY id DESC LIMIT ?
        """, (*params, limit)).fetchall()
        span = _span_dict(*_sessions_time_span(conn, where, params))
        totals = conn.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
                   COALESCE(SUM(losses),0), COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0),
                   COALESCE(MAX(max_win_streak),0), COALESCE(MAX(max_loss_streak),0),
                   COALESCE(MAX(profit),0), COALESCE(MIN(profit),0),
                   COALESCE(MAX(total_bets),0), COALESCE(AVG(total_bets),0),
                   COALESCE(MAX(highest_balance),0), COALESCE(MAX(highest_win),0),
                   COALESCE(MAX(biggest_loss),0), COALESCE(AVG(bets_per_minute),0),
                   COALESCE(MAX(peak_bps),0), COALESCE(MAX(peak_bpm),0),
                   COALESCE(AVG(bets_per_second),0),
                   COALESCE(MAX(wins),0), COALESCE(AVG(wins),0),
                   COALESCE(MAX(losses),0), COALESCE(AVG(losses),0)
            FROM sessions{where}
        """, tuple(params)).fetchone()
    finally:
        conn.close()
    return {"window": _json_window(since, until, lane), "limit": limit,
            "sessions": [dict(zip(_STATS_SESSION_COLS, r)) for r in rows],
            "totals": dict(zip(_STATS_TOTAL_COLS, totals)), "span": span}


def cmd_stats(since: str = None, until: str = None, lane: int = None, as_json: bool = False):
    """Show session statistics, optionally filtered to a date window."""
    try:
        data = _stats_data(since, until, lane)
    except _CliError as e:
        _cli_fail(e, as_json)
        return
    if as_json:
        _print_json(data)
        return
    window_label = _stats_date_window(since, until, lane)[2]
    limit = data["limit"]

    console.print(Rule(f"[bold cyan]Session History (last {limit})[/]{window_label}"))

    # Print each session as a detailed block
    for s in data["sessions"]:
        sid, started, ended, cur, strat, mult, base, bets, wins, losses, \
            profit, wagered, start_bal, end_bal, mws, mls, \
            hi_bal, lo_bal, hi_win, big_loss, \
            bpm, bps, s_peak_bps, s_low_bps, s_peak_bpm, s_low_bpm, s_lane = \
            (s[c] for c in _STATS_SESSION_COLS)
        lane_tag = f" [magenta]\\[L{s_lane}][/]" if s_lane and s_lane > 1 else ""
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
        wr = f"{wins/bets*100:.1f}%" if bets > 0 else "-"
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

        console.print(f"  [bold cyan]Session #{sid}[/]{lane_tag}  [dim]{(started or '?').replace('T', ' ')} → {(ended or 'running').replace('T', ' ')}[/]{uptime_str}")
        console.print(f"    [dim]Currency:[/] {cur or '?'}  [dim]Strategy:[/] {strat or '?'}  [dim]Mult:[/] {mult}x  [dim]Base:[/] {base:.8f}")
        console.print(f"    [dim]Bets:[/] {bets}  [dim]W:[/] [green]{wins}[/]  [dim]L:[/] [red]{losses}[/]  [dim]WR:[/] {wr}")
        console.print(f"    [dim]Speed:[/] {bps:.1f} avg/s  {bpm:.0f} avg/m  [dim]BPS:[/] {s_low_bps}-{s_peak_bps}/s  [dim]BPM:[/] {s_low_bpm}-{s_peak_bpm}/m")
        console.print(f"    [dim]Profit:[/] [{pc}]{ps}{profit:.8f}[/]  [dim]Wagered:[/] {wagered:.8f}  [dim]Avg P/L:[/] [{avg_c}]{avg_s}{avg_pnl:.8f}[/]")
        console.print(f"    [dim]Balance:[/] {start_bal:.8f} → {end_bal:.8f}  [dim]Peak:[/] [green]{hi_bal:.8f}[/]  [dim]Low:[/] [red]{lo_bal:.8f}[/]")
        console.print(f"    [dim]Streaks:[/] W+{mws}  L-{mls}")
        console.print()

    # totals: v2.30.9: honor the same date window as the listing.
    span_first, span_last = _span_range(data["span"])
    span_sec, bet_sec = data["span"]["span_sec"], data["span"]["bet_sec"]
    sessions, tot_bets, tot_wins, tot_losses, tot_profit, tot_wagered, \
        best_ws, best_ls, best_profit, worst_profit, max_bets, avg_bets, \
        all_hi_bal, all_hi_win, all_big_loss, avg_bpm, \
        all_peak_bps, all_peak_bpm, avg_bps, \
        max_wins_s, avg_wins_s, max_losses_s, avg_losses_s = \
        (data["totals"][c] for c in _STATS_TOTAL_COLS)
    pc = "green" if tot_profit >= 0 else "red"
    ps = "+" if tot_profit >= 0 else ""
    wr = f"{tot_wins/tot_bets*100:.1f}%" if tot_bets > 0 else "-"
    bp_c = "green" if best_profit >= 0 else "red"
    wp_c = "green" if worst_profit >= 0 else "red"
    avg_pnl = tot_profit / tot_bets if tot_bets > 0 else 0
    avg_pc = "green" if avg_pnl >= 0 else "red"
    avg_ps = "+" if avg_pnl >= 0 else ""

    console.print(Rule(f"[bold cyan]{'Range' if (since or until or lane) else 'All-Time'} Totals[/]{window_label}"))
    rng = (f"  [dim]({span_first.isoformat(' ', 'seconds')} → "
           f"{span_last.isoformat(' ', 'seconds')})[/]") if span_first else ""
    console.print(f"  [dim]Sessions:[/]      {sessions}{rng}")
    console.print(f"  [dim]Time Span:[/]     {_fmt_duration(span_sec)}   "
                  f"[dim]Bet Time:[/] {_fmt_duration(bet_sec)}")
    console.print(f"  [dim]Total Bets:[/]    {tot_bets}  (W {tot_wins} / L {tot_losses}  WR {wr})")
    console.print(f"  [dim]Wagered:[/]       {tot_wagered:.8f}")
    console.print(f"  [dim]Profit:[/]        [{pc}]{ps}{tot_profit:.8f}[/]")
    console.print(f"  [dim]Avg P/L:[/]       [{avg_pc}]{avg_ps}{avg_pnl:.8f}[/] per bet")
    console.print(f"  [dim]Best Session:[/]  [{bp_c}]+{best_profit:.8f}[/]")
    console.print(f"  [dim]Worst Session:[/] [{wp_c}]{worst_profit:.8f}[/]")
    console.print(f"  [dim]Peak Balance:[/]  {all_hi_bal:.8f}")
    console.print(f"  [dim]Best Streaks:[/]  W+{best_ws}  L-{best_ls}")
    console.print(f"  [dim]Speed:[/]         {avg_bps:.1f} avg/s  {avg_bpm:.0f} avg/m  [dim]Peak:[/] {int(all_peak_bps)}/s  {int(all_peak_bpm)}/m")
    console.print(f"  [dim]Bets/Session:[/]  Max {max_bets}  Avg {avg_bets:.0f}")
    console.print(f"  [dim]Wins/Session:[/]  Max {max_wins_s}  Avg {avg_wins_s:.1f}")
    console.print(f"  [dim]Losses/Session:[/] Max {max_losses_s}  Avg {avg_losses_s:.1f}")
    console.print()

_LAST_BETS_COLS = ("id", "session_id", "timestamp", "amount", "result_value",
                   "state", "profit", "balance_after")


def _last_bets_data(n: int) -> dict:
    """The newest N bets across all sessions, newest first."""
    if not os.path.exists(DB_PATH):
        raise _CliError("no_database", "No database found.")
    conn = _db_conn()
    try:
        rows = conn.execute("""
            SELECT b.id, b.session_id, substr(b.timestamp,1,19) AS dt,
                   b.amount, b.result_value, b.state, b.profit, b.balance_after
            FROM bets b ORDER BY b.id DESC LIMIT ?
        """, (n,)).fetchall()
    finally:
        conn.close()
    return {"bets": [dict(zip(_LAST_BETS_COLS, r)) for r in rows]}


def cmd_last_bets(n: int, as_json: bool = False):
    """Show last N bets from database."""
    try:
        data = _last_bets_data(n)
    except _CliError as e:
        _cli_fail(e, as_json)
        return
    if as_json:
        _print_json(data)
        return
    rows = [tuple(b[c] for c in _LAST_BETS_COLS) for b in data["bets"]]

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


_SESSION_BETS_COLS = (
    "id", "started_at", "ended_at", "currency", "strategy", "base_bet", "multiplier",
    "total_bets", "wins", "losses", "profit", "wagered", "start_balance", "end_balance",
    "max_win_streak", "max_loss_streak", "highest_balance", "lowest_balance",
    "highest_win", "biggest_loss", "bets_per_minute", "bets_per_second",
    "peak_bps", "low_bps", "peak_bpm", "low_bpm")
_SESSION_BET_ROW_COLS = ("id", "time", "amount", "result_value", "state", "profit", "balance_after")


def _session_uptime_sec(started, ended):
    """Seconds from started_at to ended_at (to now while running), or None when unparseable."""
    try:
        t0 = datetime.fromisoformat(started) if started else None
        t1 = datetime.fromisoformat(ended) if ended else datetime.now()
        return int((t1 - t0).total_seconds()) if t0 else None
    except Exception:
        return None


def _session_bets_data(session_id: int) -> dict:
    """One session's row, its uptime, its win and loss streak lengths, and its last 50 bets."""
    if not os.path.exists(DB_PATH):
        raise _CliError("no_database", "No database found.")
    conn = _db_conn()
    try:
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
            raise _CliError("session_not_found", f"Session #{session_id} not found.", style="red")
        streaks = _compute_streak_distribution(conn, session_id)
        bets = conn.execute("""
            SELECT id, substr(timestamp,12,8), amount, result_value, state, profit, balance_after
            FROM bets WHERE session_id = ? ORDER BY id DESC LIMIT 50
        """, (session_id,)).fetchall()
    finally:
        conn.close()
    session = dict(zip(_SESSION_BETS_COLS, sess))
    return {"session": session,
            "uptime_sec": _session_uptime_sec(session["started_at"], session["ended_at"]),
            "streaks": streaks,
            "bets": [dict(zip(_SESSION_BET_ROW_COLS, r)) for r in bets]}


def cmd_session_bets(session_id: int, as_json: bool = False):
    """Show full detailed stats and bets for a specific session."""
    try:
        data = _session_bets_data(session_id)
    except _CliError as e:
        _cli_fail(e, as_json)
        return
    if as_json:
        _print_json(data)
        return

    sid, started, ended, cur, strat, base, mult, bets, wins, losses, \
        profit, wagered, start_bal, end_bal, mws, mls, \
        hi_bal, lo_bal, hi_win, big_loss, \
        bpm, bps, s_pk_bps, s_lw_bps, s_pk_bpm, s_lw_bpm = \
        (data["session"][c] for c in _SESSION_BETS_COLS)

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
    wr = f"{wins/bets*100:.1f}%" if bets > 0 else "-"
    avg_pnl = profit / bets if bets > 0 else 0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""

    uptime_str = "-"
    if data["uptime_sec"] is not None:
        h, rem = divmod(max(data["uptime_sec"], 0), 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h}h {m}m {s}s"

    console.print(Rule(f"[bold cyan]Session #{sid}: Full Stats[/]"))
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
    console.print(f"  [dim]Speed:[/]         {bps:.1f} avg/s  {bpm:.0f} avg/m  Range: {s_lw_bps}-{s_pk_bps}/s  {s_lw_bpm}-{s_pk_bpm}/m")

    # ── Streak Distribution ──────────────────────────
    console.print()
    console.print(Rule("[bold cyan]Streak Distribution[/]"))

    dist = data["streaks"]

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
    rows = [tuple(b[c] for c in _SESSION_BET_ROW_COLS) for b in data["bets"]]

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

def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cmd_export(args) -> None:
    """--export: write one session to a file; with --json report {session_id, path, format, rows, bytes, sha256}."""
    init_db()
    sid = args.export_session_id or _latest_session_id()
    if sid <= 0:
        _cli_fail(_CliError("no_sessions", "No sessions in the DB yet.", style="red", exit_code=1),
                  args.json)
    try:
        target, _ = _export_target(args.export, sid)
        n, fmt = _export_session(sid, target)
    except Exception as e:
        if not args.json:
            console.print(f"[red]✗ Export failed:[/] {e}")
            if "not found" in str(e).lower():
                console.print("[dim]  Tip: run `python wolfbet.py --stats` to see recent session IDs.[/]")
            sys.exit(1)
        if isinstance(e, _ExportPathError):
            code, message = "invalid_path", str(e)
        elif isinstance(e, ValueError):
            code, message = "session_not_found", str(e)
        else:
            detail = f": {e}" if APP_ENV != "production" else ""
            code, message = "write_failed", f"Could not write the export file{detail}"
        _cli_fail(_CliError(code, message, exit_code=1), True)
    if args.json:
        _print_json({"session_id": sid, "path": target, "format": fmt, "rows": n,
                     "bytes": os.path.getsize(target), "sha256": _file_sha256(target)})
        sys.exit(0)
    shown = target if os.path.isdir(os.path.expanduser(args.export)) else os.path.expanduser(args.export)
    console.print(f"[green]✓[/] Exported [bold]{n}[/] bets from session #{sid} → "
                  f"[cyan]{shown}[/] ({fmt.upper()})")
    sys.exit(0)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def _load_and_connect(daemon: bool = False):
    """Load saved config and fetch fresh balance. Used by --resume and --daemon."""
    if not load_config():
        console.print("[red]No saved config found. Run without --resume first.[/]")
        sys.exit(1)
    if state.account_problem:
        logger.error("%s", state.account_problem)
        console.print(f"[red]{state.account_problem}. Add it on the Keys page or fix the lane.[/]")
        sys.exit(1)
    _sync_vault_key()
    _exit_on_session_rule_errors(exit_code=0 if daemon else 2)
    console.print("[dim]Loading saved config...[/]")
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
        console.print(f"[green]Resumed: {state.strategy} on {state.currency.upper()} "
                      f"@ {state.base_bet:.8f} base bet[/]")
    except Exception as e:
        detail = f": {e}" if APP_ENV != "production" else ""
        console.print(f"[red]Connection failed{detail}. Check your settings and try again.[/]")
        sys.exit(1)

_HELP_DESCRIPTION = """\
WolfBet AutoBot: high-speed auto-betting engine.

Runs a betting strategy against WolfBet, either as an interactive TUI
dashboard or as an unattended daemon. Ships with a rich strategy engine,
backtest / replay mode, lifetime analytics, session export, safety
warnings, and a Telegram bot with feature parity.

Docs: https://github.com/snad1/wolfbet
"""

_HELP_EPILOG = """\
Examples:
  Daemon control
    wolfbet.py                        # interactive wizard + TUI
    wolfbet.py --resume               # reuse saved config, show TUI
    wolfbet.py --daemon               # background daemon (no TUI)
    wolfbet.py --monitor              # attach live TUI to a running daemon
    wolfbet.py --status               # snapshot of the running session
    wolfbet.py --stop                 # gracefully stop the daemon

  Config inspection (no wizard, read-only)
    wolfbet.py --config-show          # dump saved config as a table
    wolfbet.py --list-presets         # list named presets
    wolfbet.py --stats                # last 20 sessions in detail
    wolfbet.py --analytics            # lifetime aggregates + top 5
    wolfbet.py --analytics --json     # same data as one JSON document (also --status, --stats, ...)
    wolfbet.py --benchmark 20         # standalone API-latency probe (no bets)
    wolfbet.py --diagnose             # analyse running session's bottleneck

  Config mutation (no wizard, one-shot)
    wolfbet.py --config-set max_profit=0.005 base_bet=0.0001
    wolfbet.py --config-set delay=0.35 pipeline=3
    wolfbet.py --config-set recurring=on recdelay=90 maxbets=1000
    wolfbet.py --config-set proxy=https://user:pass@10.0.0.1:8080
    wolfbet.py --config-set purgedays=7
    wolfbet.py --dry-run --json --config-set maxloss=0.01   # validate + diff, nothing saved
    wolfbet.py --config-schema        # every settable key as JSON
    wolfbet.py --preset martingale-safe --daemon

  Data
    wolfbet.py --last-bets 20         # last 20 bets across sessions
    wolfbet.py --session-bets 42      # every bet from session #42
    wolfbet.py --export /tmp/out.csv  # export sessions (CSV or JSON by ext)
    wolfbet.py --cleanup 7            # purge bet rows > 7 days (aggregates kept)

  Backtest / replay (no API calls, no real balance risk)
    wolfbet.py --backtest --simulate 10000 --seed 42
    wolfbet.py --backtest --replay-session 42 --preset new-strategy
    wolfbet.py --backtest --simulate 100000 --sessions 200 --json   # result JSON; progress lines on stderr
    wolfbet.py --export /some/dir/ --json   # writes wolfbet-session-<id>.csv; prints path, rows, bytes, sha256

  Concurrency
    wolfbet.py --resume --pipeline 3  # 3 in-flight bets (Flat strategies only)

Env vars: see README §Environment Variables (`WOLFBET_API_KEY`, `WOLFBET_DB_PATH`, ...)
Tweak grammar: see README §Runtime tweak grammar (22 params, same on `T` hotkey / TG /tweak / --config-set)
"""


def main():
    parser = argparse.ArgumentParser(
        prog="wolfbet.py",
        description=_HELP_DESCRIPTION,
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"WolfBet AutoBot v{VERSION}")
    parser.add_argument("--resume",  action="store_true", help="Skip wizard, reuse saved config")
    parser.add_argument("--daemon",  action="store_true", help="Run in background (no TUI, implies --resume)")
    parser.add_argument("--setup-only", action="store_true", help="Run wizard, save config, don't start betting")
    parser.add_argument("--monitor", action="store_true", help="Attach live TUI to a running daemon session")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status",  action="store_true", help="Show status of running session")
    group.add_argument("--lanes-status", action="store_true",
                       help="Show one row per parallel lane + totals")
    group.add_argument("--stop",    action="store_true", help="Stop a running daemon session")
    group.add_argument("--run-reset", action="store_true",
                       help="Start a new run total for the run-wide profit increment "
                            "(all lanes, or --lane K)")
    group.add_argument("--list-presets", action="store_true", help="List saved presets")
    group.add_argument("--stats",   action="store_true", help="Show recent 20 sessions in detail")
    # v2.30.9 fix: modifiers, not commands: must combine WITH --stats, so they
    # live on the standalone parser, NOT the mutually-exclusive command group.
    parser.add_argument("--since", metavar="DATE", help="With --stats/--analytics: window start, server-local time "
                             "(YYYY-MM-DD, 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS')")
    parser.add_argument("--until", metavar="DATE",
                        help="With --stats/--analytics: window end, same formats "
                             "(bare date = inclusive end of day)")
    group.add_argument("--analytics", action="store_true",
                       help="Show lifetime analytics: aggregates over ALL sessions with "
                            "breakdowns by game and strategy. Complement to --stats.")
    group.add_argument("--last-bets", type=int, metavar="N", help="Show last N bets from database")
    group.add_argument("--session-bets", type=int, metavar="ID", help="Show bets for a specific session")
    group.add_argument("--config-show", action="store_true",
                       help="Dump the currently saved config as a table (no wizard, no side effects)")
    group.add_argument("--config-set", nargs="+", metavar="KEY=VALUE",
                       help="Non-interactive config mutation. Aliases match the T-hotkey grammar "
                            "(delay=0.35, maxprofit=0.005, pipeline=3, ...). api_key is refused: "
                            "use the WOLFBET_API_KEY env var.")
    group.add_argument("--lane-ids", nargs="?", const="enabled", choices=("enabled", "all"),
                       help="Print the enabled lane ids (or every registry id with 'all'), "
                            "space-separated")
    group.add_argument("--lane-add", metavar="NAME",
                       help="Add one lane named NAME (copies --lane-from K's overrides)")
    group.add_argument("--lane-remove", type=int, metavar="K",
                       help="Remove lane K from the config; its id is never reused")
    group.add_argument("--key-list", action="store_true",
                       help="List the vault keys as JSON (label, last 4 characters, lanes); "
                            "never a secret")
    group.add_argument("--key-put", metavar="ID",
                       help="Add vault key ID; the key itself is read from stdin")
    group.add_argument("--key-rotate", metavar="ID",
                       help="Replace vault key ID; the new key is read from stdin")
    group.add_argument("--key-delete", metavar="ID",
                       help="Delete vault key ID unless a lane uses it")
    parser.add_argument("--key-label", metavar="TEXT", default=None,
                        help="With --key-put: the key's label")
    parser.add_argument("--key-owner", type=int, metavar="USER", default=None,
                        help="With --key-put: the dashboard user who owns the key")
    parser.add_argument("--key-kind", choices=("key", "proxy", "telegram"), default="key",
                        help="With --key-put: a casino key (default), a proxy URL with a password, "
                             "or the Telegram bot token (stored as telegram)")
    parser.add_argument("--lane-from", type=int, metavar="K", default=None,
                        help="With --lane-add: copy lane K's overrides (default: lane 1, none)")
    group.add_argument("--config-schema", action="store_true",
                       help="Print every key --config-set accepts as JSON: type, choices or range, "
                            "aliases, default, live reload, lane override, help")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --config-set: validate and print the old -> new diff "
                             "without saving or reloading anything")
    group.add_argument("--cleanup", nargs="?", type=int, metavar="DAYS", const=-1,
                       help="Purge old bet rows from the DB (default: state.purge_days, "
                            "explicit: --cleanup N for N days, clamped 1-30). "
                            "Session aggregates preserved.")
    group.add_argument("--benchmark", nargs="?", type=int, metavar="N", const=20,
                       help="Standalone API-latency probe (no bets placed). Default N=20, "
                            "clamped 3-200. Reports p50/p99/mean + BPS ceiling + verdict.")
    group.add_argument("--events-check", action="store_true",
                       help="Ping the events Redis (1 s timeout) and report; used by wolfctl doctor.")
    group.add_argument("--diagnose", action="store_true",
                       help="One-shot bottleneck analysis of the running daemon session "
                            "(reads live-state file; no side effects).")
    parser.add_argument("--preset", type=str, metavar="NAME",
                        help="Load a named preset and start (skips wizard)")
    parser.add_argument("--export", type=str, metavar="PATH",
                        help="Export a session to CSV or JSON (format from extension) and exit")
    parser.add_argument("--export-session-id", type=int, metavar="ID",
                        help="Session ID for --export (default: latest)")
    parser.add_argument("--backtest", action="store_true",
                        help="Run strategy against saved rolls or random samples (no API calls). "
                             "Examples: '--backtest --simulate 500 --seed 42' | "
                             "'--backtest --replay-session 42' | "
                             "'--backtest --simulate 10000 --preset aggressive'")
    parser.add_argument("--replay-session", type=int, metavar="ID",
                        help="Backtest data source: replay bets from session #ID")
    parser.add_argument("--simulate", type=int, metavar="N",
                        help="Backtest data source: simulate N synthetic bets (default 1000)")
    parser.add_argument("--seed", type=int, metavar="N",
                        help="RNG seed for --simulate (deterministic reproducibility)")
    parser.add_argument("--sessions", type=int, metavar="N",
                        help="Backtest: play N sessions back to back and print session odds and cost")
    parser.add_argument("--runs", type=int, metavar="R",
                        help="Backtest: repeat from a fresh balance R times and report how the runs ended "
                             "(balance goal, balance floor, played out)")
    parser.add_argument("--balance", type=float, metavar="X",
                        help="Backtest: starting balance (default 1.0)")
    parser.add_argument("--lane", type=int, metavar="K", default=None,
                        help=f"Parallel lane to run / inspect (1-{LANE_ID_MAX}; default 1). "
                             "With --stats/--analytics: only that lane's sessions")
    parser.add_argument("--lanes", type=int, metavar="N", default=None,
                        help="With --benchmark: probe with N concurrent clients")
    parser.add_argument("--pipeline", type=int, metavar="N", default=None,
                        help="Pipeline depth (Flat-only, 1-8). 1 = serial (default). "
                             "Non-Flat strategies and auto-bet games are auto-coerced to 1.")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging (dev mode)")
    parser.add_argument("--json", action="store_true",
                        help="With --status, --lanes-status, --config-show, --stats, --analytics, "
                             "--last-bets or --session-bets: print one JSON document to stdout "
                             "(secrets masked, exit codes unchanged)")

    args = parser.parse_args()

    if args.dry_run and not args.config_set:
        sys.stderr.write("--dry-run works with --config-set only\n")
        sys.exit(2)
    if args.json:
        if not (any(getattr(args, c) not in (None, False) for c in _JSON_COMMANDS)
                or args.config_schema or args.dry_run):
            sys.stderr.write("--json works with " + ", ".join(
                "--" + c.replace("_", "-") for c in _JSON_COMMANDS)
                + ", --config-schema and --config-set --dry-run\n")
            sys.exit(2)
        console.file = sys.stderr   # stdout carries only the JSON document

    # v2.35.0: lane identity first: every command below (status, monitor,
    # stop, the daemon itself) then reads/writes this lane's state + pid files.
    try:
        _apply_lane(args.lane)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(2)

    # Pipeline depth CLI override: applies after config/preset load below.
    if args.pipeline is not None:
        args.pipeline = max(1, min(8, args.pipeline))

    # ── Verbose mode ──────────────────────────────────────
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # ── --daemon implies --resume ──────────────────────────
    if args.daemon:
        args.resume = True

    # ── Quick commands (no DB/state needed) ───────────────
    if args.export:
        _cmd_export(args)
    if args.backtest:
        _run_backtest(args)
        return
    if args.status:
        cmd_status(as_json=args.json)
        return
    if args.lanes_status:
        cmd_lanes_status(as_json=args.json)
        return
    if args.stop:
        cmd_stop()
        return
    if args.run_reset:
        cmd_run_reset(args.lane)
        return
    if args.list_presets:
        cmd_list_presets()
        return
    if args.stats:
        cmd_stats(since=args.since, until=args.until, lane=args.lane, as_json=args.json)
        return
    if args.analytics:
        cmd_analytics(since=args.since, until=args.until, lane=args.lane, as_json=args.json)
        return
    if args.last_bets:
        cmd_last_bets(args.last_bets, as_json=args.json)
        return
    if args.session_bets is not None:
        cmd_session_bets(args.session_bets, as_json=args.json)
        return
    if args.config_show:
        cmd_config_show(as_json=args.json)
        return
    if args.config_set:
        cmd_config_set(args.config_set, dry_run=args.dry_run, as_json=args.json)
        return
    if args.config_schema:
        _print_json(_config_schema_data())
        return
    if args.lane_ids:
        reg = _lane_registry(_read_config_raw())
        ids = [k for k in sorted(reg) if args.lane_ids == "all" or reg[k]["enabled"]]
        sys.stdout.write(" ".join(map(str, ids)) + "\n")
        return
    if args.lane_add is not None:
        cmd_lane_add(args.lane_add, args.lane_from)
        return
    if args.lane_remove is not None:
        cmd_lane_remove(args.lane_remove)
        return
    if args.key_list:
        cmd_key("list")
        return
    for action in ("put", "rotate", "delete"):
        key_id = getattr(args, f"key_{action}")
        if key_id is not None:
            cmd_key(action, key_id, args.key_label, args.key_owner, args.key_kind)
            return
    if args.cleanup is not None:
        init_db()
        # `--cleanup` with no value comes through as -1 (const), meaning "use configured default".
        cmd_cleanup(None if args.cleanup == -1 else args.cleanup)
        return
    if args.benchmark is not None:
        cmd_benchmark(args.benchmark, lanes=args.lanes)
        return
    if args.diagnose:
        cmd_diagnose()
        return
    if args.events_check:
        cmd_events_check()
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
        _exit_on_session_rule_errors()
        # need API key from main config
        if not state.api_key:
            state.api_key = _active_api_key(_read_config_raw().get("api_key") or "")
            if not state.api_key:
                console.print("[red]No API key found. Run wizard first.[/]")
                sys.exit(1)
        console.print(f"[green]Loaded preset '{args.preset}': {state.strategy} on "
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
        _load_and_connect(daemon=args.daemon)
    else:
        setup_wizard()
        _exit_on_session_rule_errors()

    # Apply CLI --pipeline override AFTER config/preset load so it wins.
    if args.pipeline is not None:
        state.pipeline_depth = args.pipeline

    # v2.35.0: split budget. Lane K>1 also staggers its first /auto/start.
    _init_lane_budget()
    if state.lane > 1:
        time.sleep((state.lane - 1) * 2.0)

    _init_run()           # v2.37.0: restore run-wide increment (scope run)
    state.session_id    = _db_start_session()
    _attach_run(state.session_id)
    state.session_start = time.time()
    state.run_started_at = state.session_start
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
        state.status  = "Interrupted: saving session..."
        logger.warning("SESSION END (signal)  bets=%d  profit=%+.8f  balance=%.8f",
                    state.total_bets, state.profit, state.current_balance)
        # Best-effort auto-bet cleanup so the server drops the UUID.
        if is_auto_game(state.game) and state._auto_uuid:
            try:
                api_auto_stop()
            except Exception:
                pass
        # v2.27.0: drain the async bet-writer so no queued rows are lost.
        _stop_bet_writer(timeout=5.0)
        _db_save_session()
        _save_state_file()
        _emit_session_end()
        _stop_events()
        _cleanup_state_file()
        # Phase 5a: session summary before exit (best-effort; never blocks save path)
        try:
            console.clear()
            console.print(format_session_summary("Interrupted"))
            console.print("\n[dim]  Press any key to close...[/]")
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
    # v2.28.3: SIGHUP triggers live config reload from disk (used by
    # `wolfctl config-set` after save to hot-apply mid-session tweaks).
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _sig_reload)

    # ── Startup retention purge: matches TG's tg/engine.py:979 pattern.
    try:
        _startup_deleted = cleanup_old_bets(DB_PATH, bet_age_days=state.purge_days)
        if _startup_deleted:
            logger.info("STARTUP RETENTION purged %d old bet rows (>%dd)",
                        int(_startup_deleted), state.purge_days)
    except Exception as _e:
        logger.warning("startup retention purge failed: %s", _e)

    # ── Async bet-writer thread (v2.27.0: off the hot path) ──
    if state.db_write_mode == "async":
        _start_bet_writer()
        logger.info("BET WRITER started (async, batch=%d)", _BET_WRITER_BATCH_SIZE)
    _start_events()

    # ── Betting thread ────────────────────────────────────
    t_bet = threading.Thread(target=_run_betting_loop, daemon=True, name="BettingLoop")
    t_bet.start()

    # ── Retention thread (hourly bet-row purge) ───────────
    t_retention = threading.Thread(target=_retention_worker, daemon=True, name="Retention")
    t_retention.start()

    # ── Periodic save (DB + state file) every 5s ─────────
    def periodic_save():
        while state.running or state.paused:
            time.sleep(5)
            _db_save_session()
            _save_state_file()
    t_save = threading.Thread(target=periodic_save, daemon=True, name="PeriodicSave")
    t_save.start()

    # ══════════════════════════════════════════════════════
    #  DAEMON MODE: no TUI, just bet in background
    # ══════════════════════════════════════════════════════
    if args.daemon:
        console.print(f"[bold green]Daemon started[/]  PID={os.getpid()}  Session #{state.session_id}")
        console.print(f"[dim]  Check status:  python3 wolfbet.py --status[/]")
        console.print(f"[dim]  Stop:          python3 wolfbet.py --stop[/]")
        console.print(f"[dim]  Logs:          tail -f ~/.wolfbet_logs/wolfbet.log[/]")
        try:
            while state.running:
                time.sleep(2)
                _events_tick(_save_state_file())
        except KeyboardInterrupt:
            pass
        state.running = False
        _stop_bet_writer(timeout=5.0)   # v2.27.0: drain async writer
        _db_save_session()
        _save_state_file()
        _emit_session_end()
        _stop_events()
        _cleanup_state_file()
        logger.warning("DAEMON EXIT  session=%d  bets=%d  profit=%+.8f  reason=%s",
                       state.session_id, state.total_bets, state.profit, state.status)
        _print_summary()
        return

    # ══════════════════════════════════════════════════════
    #  TUI MODE: interactive dashboard (direct ANSI rendering)
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
            _events_tick()
            time.sleep(0.5)

        # Final render
        sys.stdout.write("\033[H")
        sys.stdout.write(build_dashboard_screen())
        sys.stdout.flush()
        time.sleep(1.0)
    finally:
        # Restore terminal: order matters
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

    _stop_bet_writer(timeout=5.0)   # v2.27.0: drain async writer
    _db_save_session()
    _emit_session_end()
    _stop_events()
    _cleanup_state_file()
    _print_summary()


if __name__ == "__main__":
    main()

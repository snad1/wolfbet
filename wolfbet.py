#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║   🐺  WolfBet AutoBot v2.31.0  — Live Dashboard         ║
║   Auto-betting engine with live TUI & session logging   ║
╚══════════════════════════════════════════════════════════╝
"""

import os, sys, json, time, sqlite3, threading, signal, random, string, argparse, logging, hashlib, csv, queue, math  # noqa: E401
from concurrent.futures import ThreadPoolExecutor
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
from core.database import cleanup_old_bets, cleanup_live_bets
# v2.28.0 — shared helpers extracted for CLI ↔ TG parity + dedup.
from core.net_helpers import (
    validate_proxy, mask_proxy, classify_auto_stop_reason, parse_streak_delay,
)
from core.diagnostics import percentile, benchmark_verdict
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
# _parse_streak_delay — extracted to core.net_helpers in v2.28.0; alias kept for
# call-site + test compatibility. Same behaviour (empty/malformed → (0, 0.0)).
_parse_streak_delay = parse_streak_delay


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

    Returns a list of human-readable heads-up strings. Never blocks — every
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
            f"Aggressive Martingale (loss_mult={loss_mult}) — a 5-loss streak needs "
            f"~{loss_mult**5:.0f}× base_bet in reserve."
        )
    # Doubling with no loss floor
    if sk in ("2", "7") and state.max_loss is None:
        warnings.append(
            "No `max_loss` set — Martingale-style strategies can drain balance "
            "on a bad streak. Consider `t maxloss <amount>` (or set it in the wizard) "
            "to establish a floor."
        )
    # Streak reset can't fire in the red — the balance caps the chain it can survive
    depth = martingale_chain_depth(state.current_balance, state.base_bet, loss_mult)
    if sk in ("2", "7") and state.streak_reset_losses and state.max_loss is None and depth:
        warnings.append(
            f"Streak reset with no `max_loss`: balance covers a {depth}-loss chain. "
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
            f"{state.current_balance:.8f}) — small bankrolls drain in ~7 losses at this size."
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
            "Recurring restart is on with no max_profit and no max_bets — "
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
            f"basebet_pct = {state.basebet_pct*100:.1f}% of balance — "
            "the base bet grows quickly on winning streaks."
        )
    # Pipeline on a strategy that can't safely pipeline
    if state.pipeline_depth > 1 and sk != "1":
        warnings.append(
            f"pipeline_depth={state.pipeline_depth} but strategy is {state.strategy} — "
            "will be forced to 1 (Flat-only). Set strategy 1 (Flat) to actually pipeline."
        )
    if state.pipeline_depth > 1 and is_auto_game(state.game):
        warnings.append(
            f"pipeline_depth={state.pipeline_depth} but game is {state.game} — "
            "auto-bet is server-side single-flight, will be forced to 1."
        )
    # Proxy on cleartext channel — credentials on the wire (non-blocking)
    if state.proxy and state.proxy.startswith("http://"):
        warnings.append(
            f"proxy scheme is http:// ({_mask_proxy(state.proxy)}) — "
            "credentials + traffic travel unencrypted between you and the proxy. "
            "Prefer https:// or socks5:// when possible."
        )
    return warnings


STREAK_RESET_REASON = "Loss streak reset"


def _restart_delay_for(reason: str) -> float:
    """Pause before the next cycle: streak reset has its own delay."""
    if reason.startswith(STREAK_RESET_REASON):
        return state.streak_reset_delay
    return state.recurring_delay


def _reset_session_for_recurring(delay: Optional[float] = None) -> None:
    """Reset per-session counters while preserving config. Starts a new DB session row."""
    if delay is None:
        delay = state.recurring_delay
    # v2.28.4 — freeze the "bet end" timestamp BEFORE the sleep so uptime +
    # DB ended_at reflect the real betting window (not "5 min bet + 1 h pause"
    # lumped together). Cleared just before we set the new session_start.
    _bet_end = time.time()
    with state.lock:
        state.session_bet_ended_at = _bet_end
        state.session_pause_started_at = _bet_end
        state.session_pause_total = delay
    # Recurring delay — pause BEFORE the next cycle so the user has time to
    # intervene, the server settles, and the dashboard shows why the bot is idle.
    if delay > 0:
        with state.lock:
            state.status = f"Recurring pause {delay:.0f}s before next cycle…"
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
        # Reset auto-bet UUID so betting_loop fires a fresh /auto/start next tick.
        state._auto_uuid     = None
        state._last_profit_milestone = 0.0
        # v2.30.1 — reset the profit-increment milestone so each cycle hunts
        # a fresh target. Base_bet is deliberately preserved above (bumps
        # stack across cycles). Without this, cycle 2+ would need to accumulate
        # past cycle 1's high-water mark before any bump could fire.
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold
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
    "pipeline":    "pipeline_depth",
    "recdelay":    "recurring_delay",
    "recurringdelay": "recurring_delay",
    "srloss":      "streak_reset_losses",
    "srdelay":     "streak_reset_delay",
    "purgedays":   "purge_days",
    "dbmode":      "db_write_mode",
    "dbwritemode": "db_write_mode",
}
_TWEAK_PARAMS = {
    "bet_delay", "basebet_pct",
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    "max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold", "recurring", "recurring_delay",
    "streak_reset_losses", "streak_reset_delay",
    "base_bet", "multiplier", "loss_mult", "win_mult",
    "pipeline_depth",
    "proxy", "purge_days",
    "db_write_mode",
}
_TWEAK_OFF_VALUES = (None, "", "off", "none", "0", "disable", "false", "no", "n")


def _derive_bet_value(multiplier: float, rule: str) -> float:
    """Canonical WolfBet win-chance derivation. 99% RTP (1% house edge).

    v2.30.9 — single source of truth used by the wizard, `load_config`,
    and `_apply_mutation`. Was inline in three places and silently stale
    in any config where `wolfctl config-set multiplier=…` ran without the
    wizard — server returned HTTP 422 "Incorrect win chance given" on
    /auto/start (top cause of the 2026-08-01 outage on vmi3142201).
    """
    if multiplier <= 0:
        return 50.74  # safe default (matches the wizard's fallback at mult=2.01)
    wc = 99.0 / multiplier
    if rule == "over":
        return round(100.0 - wc - 0.01, 2)
    return round(wc, 2)


def _apply_mutation(changes: dict) -> list:
    """Apply {param → value} mutations to state.* — returns human status list.

    Mirrors tg/engine.py:1274-1387 for CLI parity. Raises ValueError on malformed
    numeric input; caller is responsible for surfacing that cleanly."""
    msgs = []

    def _is_off(v):
        return isinstance(v, str) and v.lower() in _TWEAK_OFF_VALUES or v is None

    def _clamped(name: str, raw: float, lo: float, hi: float) -> float:
        """v2.30.6 — clamp raw into [lo, hi]; append a warning to msgs when
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

    # Recurring delay — pause between cycles in seconds (0 = back-to-back).
    if "recurring_delay" in changes:
        v = changes["recurring_delay"]
        if _is_off(v):
            state.recurring_delay = 0.0
        else:
            try:
                # v2.30.6 — cap raised from 3600 (arbitrary TG mirror) to 86400
                # (24 h). Silent clamp swapped for visible warning via _clamped.
                state.recurring_delay = _clamped(
                    "recurring_delay", float(v), 0.0, 86400.0)
            except (TypeError, ValueError):
                raise ValueError(f"recurring_delay must be a number 0-86400, got {v!r}")
        msgs.append(f"Recurring delay: {state.recurring_delay:.0f}s"
                    if state.recurring_delay > 0 else "Recurring delay: off")

    # Streak reset — end + restart the session on the first win after
    # N+ consecutive losses (only when session profit >= 0).
    if "streak_reset_losses" in changes:
        v = changes["streak_reset_losses"]
        if _is_off(v):
            state.streak_reset_losses = None
        else:
            try:
                n = int(v)
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
                    "streak_reset_delay", float(v), 0.0, 86400.0)
            except (TypeError, ValueError):
                raise ValueError(f"streak_reset_delay must be a number 0-86400, got {v!r}")
        msgs.append(f"Streak reset delay: {state.streak_reset_delay:.0f}s"
                    if state.streak_reset_delay > 0 else "Streak reset delay: off")

    # Proxy — validated + applied to the shared HTTP session.
    if "proxy" in changes:
        try:
            state.proxy = _validate_proxy(changes["proxy"])
        except ValueError as e:
            raise ValueError(f"proxy: {e}")
        _apply_proxy_to_session()
        msgs.append(f"Proxy: {_mask_proxy(state.proxy)}")

    # Purge days — bet-row retention window (1-30). "off" resets to default 3;
    # numeric values (including 0) clamp to [1, 30] — 0 days is nonsensical, so it maps to 1.
    if "purge_days" in changes:
        v = changes["purge_days"]
        if isinstance(v, str) and v.strip().lower() in ("off", "none", "false", "disable", "no"):
            state.purge_days = 3
        else:
            try:
                state.purge_days = int(_clamped("purge_days", float(v), 1.0, 30.0))
            except (TypeError, ValueError):
                raise ValueError(f"purge_days must be an integer 1-30, got {v!r}")
        msgs.append(f"Purge days: {state.purge_days}")

    # DB write mode — "async" (batched, tuned) or "sync" (legacy, per-bet fsync).
    if "db_write_mode" in changes:
        v = str(changes["db_write_mode"]).strip().lower()
        if v not in ("async", "sync"):
            raise ValueError(f"db_write_mode must be 'async' or 'sync', got {v!r}")
        state.db_write_mode = v
        msgs.append(f"DB write mode: {state.db_write_mode}")

    # Pipeline depth — instant int (dispatcher re-evaluates on next iteration).
    # Guardrail is enforced by _effective_pipeline_depth(); we just clamp to [1, 8].
    if "pipeline_depth" in changes:
        v = changes["pipeline_depth"]
        if _is_off(v):
            state.pipeline_depth = 1
        else:
            try:
                state.pipeline_depth = int(_clamped("pipeline_depth", float(v), 1.0, 8.0))
            except (TypeError, ValueError):
                raise ValueError(f"pipeline_depth must be an integer 1-8, got {v!r}")
        msgs.append(f"Pipeline depth: {state.pipeline_depth}")

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
        # v2.30.9 — re-derive bet_value to match new multiplier (was silently
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
    # v2.30.9 — explicit bet_value override (advanced; normally derived from
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

    if strategy_touched and is_auto_game(state.game) and state._auto_uuid:
        with state.lock:
            state._auto_uuid = None
        msgs.append("Auto-bet UUID cleared — restarts on next bet")

    return msgs


def _should_recurring_restart(reason: str) -> bool:
    """Whitelist safe stop-reasons for recurring restart. Loss / insufficient-balance never restart."""
    if state.profit < 0:
        return False
    # Streak reset is its own restart rule — independent of the recurring flag.
    if reason.startswith(STREAK_RESET_REASON):
        return bool(state.streak_reset_losses)
    if not state.recurring:
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

    v2.30.3 — same window as p50/p99. Prevents cold-start samples from
    biasing the mean over long sessions.
    """
    if not state._api_samples:
        return 0.0
    return sum(state._api_samples) / len(state._api_samples)


def _compute_cycle_p50() -> float:
    """Median per-iteration cycle time in ms from the rolling window (0 when empty).

    v2.30.4 — replaces the α=0.1 EMA. Same window semantics as api_p50, so
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

    v2.30.5 — symmetrical with _compute_api_rolling_avg so Recent BPS and
    Theoretical BPS both derive from mean(latency). Little's-Law-consistent:
    throughput ≈ 1 / mean_latency. Median (cycle_p50) stays available in the
    display for tail-jitter visibility but no longer drives the ratio.
    """
    if not state._cycle_samples:
        return 0.0
    return sum(state._cycle_samples) / len(state._cycle_samples)


def _compute_recent_bps() -> float:
    """Windowed BPS from cycle_mean — same statistic as theoretical_max_bps.

    v2.30.5 — was cycle_p50 (median). On right-skewed cycle distributions
    (a few slow iterations from GC / retention / /auto/start handshakes)
    median < mean, so Recent > Theoretical fired ">100% efficiency" as the
    default reading rather than a rare drift signal. Both sides now use
    the mean; efficiency is a real ratio.
    """
    cycle_mean = _compute_cycle_mean()
    return 1000.0 / cycle_mean if cycle_mean > 0 else 0.0


def _compute_theoretical_max_bps() -> float:
    """Ceiling bps = 1 / max(bet_delay, api_rolling_avg). 0 when both are 0.

    v2.30.3 — was all-time mean; now rolling to match p50/p99 window semantics.
    """
    api_avg_s = _compute_api_rolling_avg() / 1000.0
    floor = max(state.bet_delay, api_avg_s)
    return (1.0 / floor) if floor > 0 else 0.0


def _compute_efficiency_pct() -> float:
    """Recent bps as % of theoretical max (capped at 100). 0 when no ceiling yet.

    v2.30.4 — uses _compute_recent_bps (windowed) instead of session-wide
    bets_per_second so the ratio stays algebraically consistent with the
    same window feeding theoretical_max_bps. Session-wide actual can exceed
    the windowed ceiling when api_avg drifts up momentarily — that used
    to render as impossible ">100% efficiency" or clamp-and-hide.
    """
    tmax = _compute_theoretical_max_bps()
    if tmax <= 0:
        return 0.0
    recent = _compute_recent_bps()
    if recent <= 0:
        # Cycle window hasn't populated yet — fall back to session-wide
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

    v2.30.4 — replaces the α=0.1 EMA that held onto outliers for 5-10 samples
    (a single 400 ms iteration would bump displayed cycle by ~40 ms and hold).
    Now uses a rolling deque, mirroring _api_samples. Median (cycle_p50) is
    the stable "typical" cycle; p99 shows tail jitter separately.
    """
    with state.lock:
        state._cycle_samples.append(cycle_observed_ms)
        # _cycle_ms aliased to cycle_p50 for backwards state-file compat —
        # existing readers see a stable median instead of a spiky EMA.
        state._cycle_ms = _compute_cycle_p50()
        api_ref = _compute_api_p50() if state._api_samples else state._last_api_ms
        state._cycle_overhead_ms = max(0.0, state._cycle_ms - api_ref - state.bet_delay * 1000)
DB_PATH      = os.environ.get("WOLFBET_DB_PATH",     os.path.expanduser("~/.wolfbet_autobot.db"))
CONFIG_PATH  = os.environ.get("WOLFBET_CONFIG_PATH",  os.path.expanduser("~/.wolfbet_autobot.json"))
STATE_PATH   = os.environ.get("WOLFBET_STATE_PATH",   os.path.expanduser("~/.wolfbet_autobot_live.json"))
PID_PATH     = os.environ.get("WOLFBET_PID_PATH",     os.path.expanduser("~/.wolfbet_autobot.pid"))
PRESET_PATH  = os.environ.get("WOLFBET_PRESET_PATH",  os.path.expanduser("~/.wolfbet_presets.json"))
LOG_DIR      = os.environ.get("WOLFBET_LOG_DIR",      os.path.expanduser("~/.wolfbet_logs"))
VERSION      = "2.33.1"
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
    "pipeline_depth", "recurring_delay",
    "proxy", "purge_days",
    "db_write_mode",
    # v2.28.2 — previously missing from _CONFIG_KEYS despite living in
    # _PRESET_KEYS / _CONFIG_SHOW_FIELDS / _TWEAK_PARAMS. Every wolfctl
    # config-set on these fields silently reverted on the next process
    # launch since Phase 2 (v2.16.0). Now honestly persisted.
    "recurring", "basebet_pct",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "streak_reset_losses", "streak_reset_delay",
]

def _mask_key(key: str) -> str:
    """Mask API key for safe logging: show first 8 + last 4 chars."""
    if not key or len(key) < 16:
        return "***"
    return f"{key[:8]}…{key[-4:]}"

def save_config():
    data = {k: getattr(state, k) for k in _CONFIG_KEYS}
    # v2.28.2 — streak-tuple fields round-trip as "N:X" strings (or null when
    # disabled). Same encoding presets already use — one grammar, no forks.
    for k in _STREAK_TUPLE_KEYS:
        tup = getattr(state, k, None)
        if tup and tup[0] > 0:
            data[k] = f"{tup[0]}:{tup[1]}"
        else:
            data[k] = None
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
        # v2.28.2 — decode streak-tuple fields via parse_streak_delay.
        # Older configs (pre-v2.28.2) lack these keys; the loop silently
        # skips them and defaults from BotState.__init__ apply.
        for k in _STREAK_TUPLE_KEYS:
            if k in data:
                setattr(state, k, parse_streak_delay(data[k]))
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
        # v2.26.0 — normalize + apply loaded proxy to the shared session.
        try:
            state.proxy = _validate_proxy(state.proxy)
        except ValueError:
            state.proxy = None
        _apply_proxy_to_session()
        # v2.30.9 — silently correct stale bet_value from disk. Was the top
        # cause of HTTP 422 on /auto/start when a previously-saved config
        # had `bet_value` from an older `multiplier` and `wolfctl config-set
        # multiplier=…` never recomputed. CHANGELOG v1262 promised this fix
        # but only the wizard + /bet-self-correct path actually derived it;
        # auto games never hit /bet so no self-correction ever ran. The
        # warning is silent-by-default but logged so we can spot recurrences.
        if state.multiplier > 0 and state.rule in ("over", "under"):
            derived_bv = _derive_bet_value(state.multiplier, state.rule)
            if abs(state.bet_value - derived_bv) > 0.01:
                logger.warning(
                    "bet_value from disk (%.2f) mismatched derived value "
                    "for multiplier=%.2f rule=%s (%.2f) — corrected.",
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
    "profit_increment", "profit_threshold",
    # Phase 4a additions — JSON-native Phase 2 fields:
    "recurring", "basebet_pct",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    # Phase 5b — pipeline depth persists per preset:
    "pipeline_depth",
    # v2.25.3 — pause between recurring cycles (TG↔CLI parity):
    "recurring_delay",
    # v2.26.0 — proxy + retention (TG↔CLI parity):
    "proxy", "purge_days",
    # v2.27.0 — async bet writer mode:
    "db_write_mode",
    # v2.32.0 — loss-streak reset restart:
    "streak_reset_losses", "streak_reset_delay",
]

# Fields stored on state as (N, X) tuples but round-tripped through JSON as "N:X" strings.
# Decoded on load via _parse_streak_delay (Phase 2 helper).
_STREAK_TUPLE_KEYS = [
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets",
    "streakbet_loss",
]

# v2.28.3 — fields that can safely be re-read from disk into a running daemon
# on SIGHUP without corrupting the active session identity or the Martingale
# ladder. Everything NOT in this set requires `wolfctl restart`.
_LIVE_RELOAD_KEYS = {
    # Recurring + delays
    "recurring", "recurring_delay",
    "streak_reset_losses", "streak_reset_delay",
    # Stop conditions
    "max_profit", "max_loss", "max_bets", "max_wins", "stop_on_balance",
    # Pacing + basebet scaling
    "bet_delay", "basebet_pct",
    # Streak-based pacing
    "streak_delay_loss", "streak_delay_win", "streak_delay_bets", "streakbet_loss",
    # Milestones + profit ramp
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold",
    # Concurrency + retention + network
    "pipeline_depth", "proxy", "purge_days", "db_write_mode",
    # Rule DSL (regenerated wholesale by _load_rules_from_text; strategy_key stays)
    "custom_rules_text",
}


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
                # Present but not live-safe — warn if it actually changed.
                if getattr(state, k, None) != v:
                    skipped_unsafe.append(k)

    # Side effects for keys that need them.
    if "proxy" in applied:
        _apply_proxy_to_session()
    if "custom_rules_text" in applied and state.custom_rules_text:
        try:
            state.custom_rules = _load_rules_from_text(state.custom_rules_text)
        except Exception as e:
            logger.warning("live reload: rule reparse failed: %s", e)
    if skipped_unsafe:
        logger.warning(
            "live reload: skipped %d unsafe field(s) — restart daemon to apply: %s",
            len(skipped_unsafe), ", ".join(sorted(skipped_unsafe)),
        )
    return applied


def _sig_reload(sig, frame):
    """SIGHUP handler — re-read config file for live-safe fields.

    Signal handlers must be short and never raise. Any error is logged
    and swallowed.
    """
    try:
        applied = _reload_live_config()
        if applied:
            logger.info("SIGHUP: reloaded %d live field(s): %s",
                        len(applied), ", ".join(sorted(applied.keys())))
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
        # v2.28.4 — when a session enters `_reset_session_for_recurring`, this
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
        self._cycle_ms      : float = 0.0            # v2.30.4 — aliased to cycle_p50 for state-file compat
        self._cycle_overhead_ms: float = 0.0         # cycle − api − delay*1000
        self._last_cycle_start: float = 0.0
        # v2.30.4 — rolling per-iteration cycle window (mirrors _api_samples).
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
        # Seconds to pause between one recurring cycle ending and the next starting.
        # v2.30.6 — default 60, floor 0, cap 86400 (24 h). Was 3600 (arbitrary
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

        # ── v2.26.0 — proxy + retention (TG↔CLI parity) ──
        # HTTP/HTTPS/SOCKS5 proxy URL, or None for direct connection.
        # Credentials in the URL are masked in show + logs via _mask_proxy.
        self.proxy            : Optional[str] = None
        # Bet-row retention window in days (used by hourly cleanup thread + --cleanup).
        self.purge_days       : int = 3

        # ── v2.27.0 — bet DB write mode ──
        # "async" (default): bet rows batched via background writer thread —
        # 30-40 ms per-bet overhead savings via persistent connection +
        # PRAGMA synchronous=NORMAL + batched INSERTs.
        # "sync": legacy path — fresh connection + fsync per bet (safe but slow).
        # Fallback to sync on queue overflow preserves nothing-lost semantics.
        self.db_write_mode    : str = "async"

        # ── Phase 5c — auto-bet recovery (max nonce / session expired) ──
        # Tag from _classify_auto_stop_reason: "max_nonce", "expired", or "".
        # Signals the loop to call _recover_expired_session (seed rotation)
        # instead of a bare api_auto_start.
        self.last_stop_reason           : str = ""
        self.consecutive_recovery_fails : int = 0

        # ── Phase 5b — pipelined betting (Flat-only) ──
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
#  v2.26.0 — Shared HTTP session (proxy-aware)
# ══════════════════════════════════════════════════════════
# All wolfbet.com API calls go through _http_session so proxy config lives in
# ONE place. Mirrors tg/engine.py:_recreate_http (httpx.AsyncClient(proxy=…))
# but stays on `requests` for CLI's synchronous / threaded model.
_http_session = requests.Session()
_http_session.headers.update({"User-Agent": f"wolfbet-cli/{VERSION}"})


# _validate_proxy / _mask_proxy — extracted to core.net_helpers in v2.28.0.
# Aliases kept so every call-site (and every v2.26.0+ test) keeps working
# without edits. TG bot's tg/handlers.py imports the same functions.
_validate_proxy = validate_proxy
_mask_proxy     = mask_proxy


def _apply_proxy_to_session() -> None:
    """Update _http_session.proxies from state.proxy — single point of change."""
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
            if remaining <= 2 and not state.rate_wait:
                state.rate_wait      = True
                state.rate_backoff   = 2.0   # start probing at 2s
    except Exception:
        pass

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
            # v2.30.9 — include a short body snippet in state.last_error so
            # operators self-diagnose from `wolfctl status` instead of
            # trawling logs. Was: bare "HTTP 422" — actual reason was hidden.
            body_snip = body.strip().replace("\n", " ")[:120]
            with state.lock:
                if classified:
                    state.last_stop_reason = classified
                state.last_error = (
                    f"Auto-bet start failed: HTTP {r.status_code}"
                    + (f" — {body_snip}" if body_snip else "")
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


# _classify_auto_stop_reason — extracted to core.net_helpers in v2.28.0.
# TG bot now imports the same helper, closing its max-nonce blind spot.
_classify_auto_stop_reason = classify_auto_stop_reason


def _is_insufficient_balance(text) -> bool:
    """True when a WolfBet error body/message reports insufficient balance."""
    return "insufficient" in str(text or "").lower()


def _midchain_loss_reason() -> str:
    """Non-empty stop reason when restarting the server auto-cycle would abandon a loss.

    A fresh /auto/start resets the server-side progression to base_bet. Mid loss
    streak or with the session in the red, that loss can never be recovered and
    the streak-reset (profit >= 0) trigger can never fire — stop instead.
    """
    if state.current_streak < 0 or state.profit < 0:
        return (f"Auto-cycle lost mid-chain (streak {state.current_streak}, "
                f"profit {state.profit:+.8f})")
    return ""


def _recover_expired_session() -> bool:
    """Recover from 'Session has ended' / 'Max nonce achieved'.

    Rotates the server seed, best-effort stops the dead UUID, respects the
    current rate-limit backoff, then starts a fresh auto-bet cycle. Does
    NOT touch state.session_id or DB rows — the CLI session identity is
    preserved across many WolfBet server-side auto-cycles.
    """
    reason = state.last_stop_reason or "expired"
    logger.warning("Auto-bet recovery: %s — rotating seed + restarting", reason)
    with state.lock:
        state.status = f"Recovering from {reason} — rotating server seed…"
    _rotate_server_seed()
    api_auto_stop()
    settle = max(2.0, state.rate_backoff)
    time.sleep(settle)
    ok = api_auto_start()
    if ok:
        with state.lock:
            state.last_stop_reason = ""
            state.consecutive_recovery_fails = 0
            state.status = "Seed rotated — auto-cycle restarted"
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
                # Auto-bet UUID expired / server-seed exhausted — clear UUID so
                # next loop iteration recovers (seed rotation if last_stop_reason set).
                if is_auto_game(state.game):
                    classified = _classify_auto_stop_reason(err_msg)
                    if classified:
                        logger.warning("AUTO-BET %s: %s", classified.upper(), err_msg[:120])
                        with state.lock:
                            state._auto_uuid = None
                            state.last_stop_reason = classified
                            state.last_error = (
                                "Max nonce achieved — rotating server seed…"
                                if classified == "max_nonce"
                                else "Auto-bet session expired — restarting…"
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
            logger.warning("RATE LIMIT 429 — backoff %.1fs (Retry-After=%s)", delay, retry_after)
            with state.lock:
                state.rate_wait    = True
                state.rate_backoff = max(2.0, delay)
                state.last_error   = f"Rate limit 429 — retrying in {delay:.0f}s…"
        elif r.status_code == 400 and is_auto_game(state.game):
            # UUID expired / seed exhausted — classify body so loop recovers correctly.
            body = r.text[:200]
            # v2.32.1 — insufficient balance is terminal. It used to fall through
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
                    "Max nonce achieved — rotating server seed…"
                    if classified == "max_nonce"
                    else "Auto-bet session expired — restarting…"
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
    # Streak reset — fires only on the win that broke an N+ loss streak
    # (current_streak == 1) and only while the session is not in the red.
    n = state.streak_reset_losses
    if (n and state.current_streak == 1
            and state.last_broken_loss_streak >= n and p >= 0):
        return True, f"{STREAK_RESET_REASON}: {state.last_broken_loss_streak} losses then win"
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
                _reset_session_for_recurring(_restart_delay_for(reason))
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
        if is_auto_game(state.game) and not state._auto_uuid:
            # v2.32.1 — a new server cycle restarts the progression at base_bet.
            # Mid loss chain / in the red that abandons the loss with no way out.
            midchain = _midchain_loss_reason()
            if state._insufficient_balance or midchain:
                reason = "Insufficient balance" if state._insufficient_balance else midchain
                logger.warning("AUTO-CYCLE not restarted — %s", reason)
                state.running = False
                state.status = f"STOPPED: {reason}"
                _db_save_session()
                break
            needs_recovery = state.last_stop_reason in ("max_nonce", "expired")
            state.status = (
                f"Recovering from {state.last_stop_reason} — rotating seed…"
                if needs_recovery else "Starting auto-bet session…"
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
                    logger.error("Recovery failed 3× — hard stop (reason=%s)", reason)
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
            if (state.profit_increment is not None and state.profit_threshold
                    and state.profit >= state.next_profit_milestone):
                state.base_bet += state.profit_increment
                state.next_profit_milestone += state.profit_threshold
                # v2.29.0 — for auto-games the Martingale progression runs
                # server-side on WolfBet using the base_bet submitted at
                # /auto/start. A CLI-side base_bet bump means nothing until
                # we restart the WolfBet auto-cycle. Clearing _auto_uuid
                # makes the next loop tick call api_auto_start with the
                # fresh base — same CLI session, new WolfBet UUID. Same
                # invariant as v2.25.0 max-nonce recovery.
                if is_auto_game(state.game) and state._auto_uuid:
                    state._auto_uuid = None
                    logger.info(
                        "PROFIT INCREMENT: base_bet → %.8f + auto-bet UUID "
                        "cleared for restart with new base (next milestone at "
                        "%.8f profit)", state.base_bet, state.next_profit_milestone,
                    )
                else:
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

        # persist to DB (async writer thread when db_write_mode=async)
        _db_save_bet_dispatch(bet_data, raw_profit, new_balance)

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
#  PHASE 5b — PIPELINED BETTING (Flat-only)
# ══════════════════════════════════════════════════════════
#
# Mirrors tg/engine.py:_pipelined_betting_loop but sync + threaded to fit the
# CLI's existing model (requests + threading, no asyncio). Guardrail forces
# depth=1 for anything other than Flat on a non-auto game — sequential
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
        state.pipeline_reason = f"forced to 1 — auto-bet game ({state.game}) is server-side single-flight"
        return 1
    if state.strategy_key != "1":
        state.pipeline_reason = f"forced to 1 — {state.strategy} needs outcome-ordered bets (Flat-only)"
        return 1
    state.pipeline_reason = ""
    return min(requested, _PIPELINE_MAX)


def _run_betting_loop():
    """Dispatcher — selects serial or pipelined loop based on effective depth."""
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

    Returns {"amount": float, "bet_no": int} — bet_no is monotonic dispatch order.
    Never mutates strategy state (Flat has none). Records the streak_delay if any.
    """
    with state.lock:
        state.pipeline_dispatched += 1
        return {
            "amount": state.current_bet,
            "bet_no": state.pipeline_dispatched,
        }


def _one_bet_worker(payload: dict) -> dict:
    """Worker thread body — places a single bet via existing api_place_bet.

    Returns a result envelope; api errors surface as {"result": None}. Never
    raises — the outcome handler decides how to react.
    """
    try:
        result = api_place_bet(payload["amount"])
    except Exception as e:                                # pragma: no cover — defensive
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

        if (state.profit_increment is not None and state.profit_threshold
                and state.profit >= state.next_profit_milestone):
            state.base_bet += state.profit_increment
            state.next_profit_milestone += state.profit_threshold
            # v2.29.0 — auto-game UUID reset so WolfBet-side base_bet follows.
            if is_auto_game(state.game) and state._auto_uuid:
                state._auto_uuid = None
                logger.info(
                    "PROFIT INCREMENT: base_bet → %.8f + auto-bet UUID "
                    "cleared for restart with new base (next milestone at "
                    "%.8f profit)", state.base_bet, state.next_profit_milestone,
                )
            else:
                logger.info(
                    "PROFIT INCREMENT: base_bet → %.8f (next milestone at %.8f profit)",
                    state.base_bet, state.next_profit_milestone,
                )

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
    doesn't depend on ordering — every bet is base_bet).
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
                state.status = "PAUSED — press [R] to resume"
                time.sleep(0.4)
                continue

            if state.rate_wait:
                # Drain in-flight before honoring the probe so no worker
                # dispatches into the cooldown window.
                backoff = state.rate_backoff
                state.status = f"Rate limit — draining pipeline, resuming in {backoff:.0f}s…"
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
                    _reset_session_for_recurring(_restart_delay_for(reason))
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

            payload = _prepare_next_bet_flat()

            if not sem.acquire(timeout=1.0):
                # No slot free within 1s — loop back to re-check stop/pause/rate.
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
    state.last_broken_loss_streak = 0
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


# ══════════════════════════════════════════════════════════
#  v2.27.0 — Async bet writer (kill the per-bet CLI overhead)
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

    Applied PRAGMAs (writer-only — never touches read connections):
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
        logger.error("bet writer failed to open DB (%s) — falling back to sync mode", e)
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
                logger.error("bet writer reopen failed: %s — exiting worker",
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
    path so no bet is lost — the writer is stalled but data still lands.
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


def _db_save_session():
    try:
        conn = _db_conn()
        lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
        # v2.28.4 — when a session is in the recurring pause, freeze ended_at
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
            # v2.30.1 — actionable profit-increment target (distinct from
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
            # v2.30.4 — windowed cycle metrics + recent BPS for consistent
            # efficiency computation. cycle_ms above is aliased to cycle_p50
            # for backwards-compat readers.
            "cycle_p50_ms":        _compute_cycle_p50(),
            "cycle_p99_ms":        _compute_cycle_p99(),
            # v2.30.5 — mean cycle drives Recent BPS + efficiency (was p50).
            # Same statistic on both sides of the ratio; no more >100% edge.
            "cycle_mean_ms":       _compute_cycle_mean(),
            "cycle_sample_count":  len(state._cycle_samples),
            "bets_per_second_recent": _compute_recent_bps(),
            "efficiency_pct":      _compute_efficiency_pct(),
            "theoretical_max_bps": _compute_theoretical_max_bps(),
            "pipeline_effective":  state.pipeline_effective,
            "pipeline_dispatched": state.pipeline_dispatched,
            # v2.27.0 — async bet-writer visibility (surfaced by --diagnose)
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
            "profit_increment": state.profit_increment,
            "profit_threshold": state.profit_threshold,
            # rule-based info
            "rule_count": len(state.custom_rules) if state.strategy_key == "8" else 0,
            "rule_descriptions": [r.description for r in state.custom_rules] if state.strategy_key == "8" else [],
            # v2.28.4 — uptime freezes at bet-end when session is in recurring
            # pause, so "5 min bet + 1 h pause" no longer displays as "1 h 5 min
            # session". Pause remaining is surfaced separately for the monitor.
            "uptime_sec": int(
                (state.session_bet_ended_at or time.time()) - state.session_start
            ),
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
    # ROI % — profit / wagered × 100 (edge indicator).
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
    # v2.30.3 — use the unified theoretical ceiling instead of `1000/cycle_ms`
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
            # v2.30.9 — derivation now lives in _derive_bet_value (single
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
                    default="y" if (state.recurring or state.streak_reset_losses or state.basebet_pct or state.streak_delay_loss[0] or
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

        # Recurring delay — seconds to pause between cycles (TG parity, default 60).
        v = _ask_optional("Pause between recurring cycles in seconds (0 = back-to-back)",
                          state.recurring_delay if state.recurring_delay > 0 else None)
        if v is _BACK:
            return False
        try:
            # v2.30.6 — cap raised 3600 → 86400 (24 h); no downstream break.
            state.recurring_delay = max(0.0, min(86400.0, float(v))) if v else 0.0
        except ValueError:
            state.recurring_delay = 60.0

        # Streak reset — restart on the first win after N+ losses in a row.
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

        # Phase 5b — pipeline depth (Flat-only, coerced at runtime for other strategies).
        v = _ask_optional("Pipeline depth (1 = serial / off, up to 8 — Flat only)",
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

        # ── v2.22.1: heads-up warnings for risky config combinations ──
        risk_warnings = _check_risky_config()
        if risk_warnings:
            console.print()
            body_lines = [Text.from_markup(f"[yellow]•[/] {w}") for w in risk_warnings]
            console.print(Panel(
                Group(*body_lines),
                title="[bold yellow]⚠️  Heads-up — review before starting[/]",
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

# ── Monitor layout budget ────────────────────────────
# Fixed rows produced by _build_monitor_screen (must match add() calls):
#   Row 1: session-title header (WolfBet v… #N uptime ● LIVE)
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
    """Row 6 — live config-progress summary. One line, elides unset fields."""
    segments = []

    profit_threshold  = d.get("profit_threshold")
    profit_increment  = d.get("profit_increment")
    if profit_threshold and profit_increment is not None:
        # v2.30.1 — use the actionable target the bump code writes, not the
        # notification-milestone tracker. Fall back to profit_threshold for
        # pre-v2.30.1 state files that never persisted next_profit_milestone.
        next_ms = float(d.get("next_profit_milestone")
                        or profit_threshold or 0.0)
        profit  = float(d.get("profit", 0.0) or 0.0)
        # v2.30.7 — display window-relative progress ("how close to the NEXT
        # bump?") not cumulative profit-vs-next-ms. Denominator is one
        # threshold unit; numerator resets after every bump. Matches operator
        # expectation: "Ms 39%" means the current window is 39% done,
        # regardless of how many bumps have already fired this session.
        window_start = max(0.0, next_ms - float(profit_threshold))
        window_progress = max(0.0, profit - window_start)
        pct = min(100.0, (window_progress / float(profit_threshold) * 100)
                  if profit_threshold else 0.0)
        segments.append(
            f"{_a('dim','Ms')} "
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
    # v2.28.4 — surface recurring-pause countdown right in the LIVE indicator
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
    hdr_vis = 4 + len(f"WolfBet v{VERSION}  #{sid}  {uptime}  ") + len(st_ind_plain) + 1
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
    status_txt = status[:60] if status else "Monitoring…"
    keys = "[P]ause [R]esume [Q]uit/Detach [S]top daemon"
    add(f" {_a('bold_white', 'MONITOR')} {_a('white', status_txt)}  {_a('dim', keys)}")
    # v2.28.1 — advertise the config-change path since T/E/H are foreground-TUI only.
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
    console.print(f"[dim]Attaching to session #{sid} (PID {pid})… "
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

        # Handle stop action — go through systemctl when systemd manages the
        # daemon so the unit's Restart= policy doesn't respawn on a clean exit.
        # Fall back to SIGTERM for non-systemd installs.
        if _monitor_action[0] == "stop" or (locals().get("action") == "stop"):
            stopped_via_systemd = False
            if _is_systemd_managed(pid):
                try:
                    import subprocess as _sp
                    _sp.run(["systemctl", "--user", "stop", "wolfbet"],
                            check=False, timeout=10)
                    stopped_via_systemd = True
                except Exception:
                    pass
            if not stopped_via_systemd and pid:
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
    # v2.28.4 — recurring-pause countdown: uptime freezes at bet-end; this
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
        ("Uptime",         uptime + (f"  [dim]· Recurring pause:[/] {pause_str}" if pause_str else "")),
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

    # ── Observability (v2.26.1 — from state file) ──
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
        # v2.30.3 — was `1000/cycle_ms` (misleading for auto-games where the
        # server batches multiple bets per outer-iteration cycle). Use the
        # same theoretical_max_bps cmd_diagnose displays so status + diagnose
        # agree. Auto-game users get a batching footnote.
        tmax = float(d.get("theoretical_max_bps") or 0)
        auto = is_auto_game(d.get("game", ""))
        note = "  [dim](batched — cycle is per outer iteration)[/]" if auto else ""
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
#  CONFIG CLI COMMANDS  (v2.25.0 — non-interactive config surface)
# ══════════════════════════════════════════════════════════
_CONFIG_SHOW_FIELDS = [
    ("currency",              "Betting currency"),
    ("game",                  "Game (dice / limbo / dice-auto / limbo-auto / …)"),
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
    ("profit_increment",      "Bump base_bet by this at every profit milestone"),
    ("profit_threshold",      "Profit interval between increments"),
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
    ("custom_rules_text",     "Rule-Based custom rules (JSON per line)"),
]


def _fmt_config_value(field: str, val) -> str:
    """Render a config value for the --config-show table.

    Streak tuples → "N:X"; None → "off"; long strings truncated.
    Proxy is credential-masked via _mask_proxy — NEVER leak userinfo.
    """
    if field == "proxy":
        return f"[cyan]{_mask_proxy(val)}[/]" if val else "[dim]direct[/]"
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
                     "milestone_profit"):
            return f"{val:.8f}"
        return f"{val:.4f}".rstrip("0").rstrip(".") or "0"
    if isinstance(val, str) and len(val) > 60:
        return val[:57] + "…"
    return str(val)


def cmd_config_show():
    """Render the current saved config as a Rich table. No wizard, no side effects."""
    from rich.table import Table   # local import — avoids top-level cost when unused
    if not load_config():
        console.print("[yellow]No config saved yet — run the wizard first.[/]")
        console.print("[dim]  Try: python3 wolfbet.py --setup-only[/]")
        sys.exit(1)
    tbl = Table(title="WolfBet AutoBot — Current Config",
                title_style="bold cyan", show_lines=False)
    tbl.add_column("Field",       style="bold yellow", no_wrap=True)
    tbl.add_column("Value",       style="cyan")
    tbl.add_column("Description", style="dim")

    key_state = "[bold green](SET)[/]" if state.api_key else "[bold red](NOT SET)[/]"
    tbl.add_row("api_key", f"[dim]{_mask_key(state.api_key)}[/] {key_state}",
                "API bearer token (set via WOLFBET_API_KEY env var)")
    for field, desc in _CONFIG_SHOW_FIELDS:
        try:
            val = getattr(state, field)
        except AttributeError:
            continue
        tbl.add_row(field, _fmt_config_value(field, val), desc)
    console.print(tbl)
    try:
        st = os.stat(CONFIG_PATH)
        mode = oct(st.st_mode & 0o777)
        console.print(f"\n[dim]Config file:[/] [cyan]{CONFIG_PATH}[/]  "
                      f"[dim]mode={mode}  size={st.st_size}B[/]")
    except OSError:
        pass
    console.print(f"[dim]Tweak with:[/] "
                  f"[cyan]python3 wolfbet.py --config-set KEY=VALUE …[/]")


def _parse_config_set_pairs(pairs: list) -> dict:
    """Parse ['max_profit=0.005', 'base_bet=0.0001'] → {'max_profit': '0.005', …}.

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


def cmd_config_set(pairs: list):
    """Apply KEY=VALUE mutations to the saved config, then persist. No wizard."""
    # Security: reject api_key — credentials belong in env vars, not shell history.
    for p in pairs:
        if p.lower().startswith("api_key="):
            console.print("[red]Refusing to write api_key via --config-set.[/]")
            console.print("[dim]  Set the WOLFBET_API_KEY environment variable instead "
                          "(never in shell history / process args).[/]")
            sys.exit(2)

    try:
        parsed = _parse_config_set_pairs(pairs)
    except ValueError as e:
        console.print(f"[red]Invalid --config-set argument:[/] {e}")
        sys.exit(2)

    if not load_config():
        console.print("[yellow]No config saved yet — run the wizard first.[/]")
        console.print("[dim]  Try: python3 wolfbet.py --setup-only[/]")
        sys.exit(1)

    # Snapshot old values BEFORE mutation for the diff print.
    old = {}
    for canonical in parsed:
        try:
            old[canonical] = getattr(state, canonical)
        except AttributeError:
            old[canonical] = None

    try:
        # v2.30.6 — capture msgs to surface clamp warnings (⚠ …) beside
        # the standard "old → new" panel. Was: return value discarded and
        # silent clamps went unseen.
        mutation_msgs = _apply_mutation(parsed) or []
    except ValueError as e:
        console.print(f"[red]Mutation failed:[/] {e}")
        sys.exit(2)

    save_config()
    console.print(Rule("[bold cyan]Config updated[/]"))
    for canonical in parsed:
        new = getattr(state, canonical, None)
        console.print(
            f"  [yellow]{canonical}[/]: "
            f"[dim]{_fmt_config_value(canonical, old[canonical])}[/]"
            f"  →  [green]{_fmt_config_value(canonical, new)}[/]"
        )
    # v2.30.6 — surface clamp warnings (and any other advisory notes) so
    # silently swallowed input becomes visible to the operator.
    for m in mutation_msgs:
        if isinstance(m, str) and m.startswith("⚠"):
            console.print(f"  [yellow]{m}[/]")
    console.print(f"\n[dim]Saved to[/] [cyan]{CONFIG_PATH}[/]")

    # v2.28.3 — hot-reload the running daemon via SIGHUP so mid-session
    # tweaks (recurring, stop conditions, delays, milestones, proxy, etc.)
    # take effect immediately without `wolfctl restart`. Unsafe keys
    # (game / strategy_key / base_bet / api_key / …) get a heads-up here
    # AND are logged by the daemon's SIGHUP handler.
    unsafe = [k for k in parsed if k not in _LIVE_RELOAD_KEYS]
    if unsafe:
        console.print(
            f"[yellow]Note:[/] {len(unsafe)} field(s) need a full restart to apply — "
            f"{', '.join(sorted(unsafe))}. Run [cyan]wolfctl restart[/] when ready."
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


# ══════════════════════════════════════════════════════════
#  RETENTION / CLEANUP  (v2.26.0)
# ══════════════════════════════════════════════════════════
def cmd_cleanup(days=None):
    """Purge old bet rows from the DB, preserving session aggregates.

    Delegates to shared/core/database.cleanup_old_bets — session rows
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
#  BPS DIAGNOSTICS  (v2.26.1 — --benchmark, --diagnose)
# ══════════════════════════════════════════════════════════
# _percentile — extracted to core.diagnostics in v2.28.0. TG /benchmark
# will use the same helper.
_percentile = percentile


def cmd_benchmark(n=None):
    """Fire N read-only API requests and report latency distribution.

    Uses GET /v1/user/balances via the same _http_session as production —
    so proxy, User-Agent, and connection pooling all match the live path.
    No bets placed, no DB write, no session started.
    """
    from rich.table import Table
    n = 20 if n is None else int(n)
    n = max(3, min(200, n))
    if not load_config():
        console.print("[red]No config saved yet — run the wizard first.[/]")
        sys.exit(1)
    url = f"{API_BASE}/v1/user/balances"
    console.print(
        f"[dim]Benchmarking {n} × GET /v1/user/balances "
        f"(proxy={_mask_proxy(state.proxy)}) …[/]"
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
    # Verdict — shared with TG /benchmark (extracted to core.diagnostics in v2.28.0).
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
    # v2.30.4 — windowed cycle metrics + windowed recent BPS.
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

    # v2.30.4 — Recent BPS + Efficiency use the same window as Theoretical,
    # so the ratio is algebraically consistent. Session-wide Actual stays
    # visible for context. `>` prefix flags the (rare) case where the
    # windowed actual exceeds theoretical (api_avg drift within the window).
    _eff_ref = recent_bps if recent_bps > 0 else bps
    if tmax > 0:
        _eff_raw = _eff_ref / tmax * 100.0
        eff_display = ">100%" if _eff_raw > 100.0 else f"{_eff_raw:.0f}%"
    else:
        eff_display = "—"

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
        # v2.30.3 — the three components come from different windows/formulas,
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

    # v2.27.0 — DB writer visibility
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
        lines.append(f"[dim]DB writer:[/] mode=[yellow]sync[/] (legacy — per-bet fsync)")

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
            advice.append("• CLI overhead high AND db_write_mode=sync — flip to async: "
                          "[cyan]wolfctl config-set dbmode=async && wolfctl restart[/]")
        elif dbw_fb == 0 and dbw_hwm < 100 and int(rate_rem or 999) > 200:
            # v2.30.4 — DB writer + rate limits are all healthy, so overhead
            # is almost certainly shared-tenancy CPU/disk jitter (VPS silicon),
            # not a fixable code issue. Prior investigation confirmed this
            # pattern on the older-EPYC VPS: 14% slower CPU + 40% slower disk.
            advice.append("• CLI overhead is high on this host.")
            advice.append("  DB writer + rate limits are healthy — overhead is likely "
                          "shared-tenancy CPU/disk jitter (VPS silicon), not a fixable "
                          "code issue. Consider redeploying on a fresh VPS instance "
                          "(silicon lottery) or a higher-tier plan.")
        else:
            advice.append("• CLI overhead is high. Check disk I/O (DB writes) "
                          "and CPU load on the daemon host.")
    if dbw_fb > 0:
        advice.append(f"• DB writer queue overflowed {dbw_fb} times "
                      "(async writer is saturating — check disk perf).")
    elif int(rate_rem or 999) <= 3:
        advice.append("• Rate-limit headroom is low. WolfBet may throttle soon.")
    if pipe_eff == 1 and strategy in ("Flat Bet",) and delay_ms == 0:
        advice.append("• Strategy is Flat with no delay — try "
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
    """Hourly bet-row purge — mirrors tg/engine.py:747-758.

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
#  ANALYTICS — lifetime aggregates (v2.23.0)
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
        wr = f"{r['wins']/r['bets']*100:.1f}%" if r['bets'] > 0 else "—"
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
        wr = f"{r['wins']/r['bets']*100:.1f}%" if r['bets'] > 0 else "—"
        pc = 'green' if r['profit'] >= 0 else 'red'
        ps = '+' if r['profit'] >= 0 else ''
        started_short = (r['started_at'] or "").replace("T", " ")[:19]
        t.add_row(str(r['id']), r['game'], r['strategy'], f"{r['bets']:,}",
                  wr, f"[{pc}]{ps}{r['profit']:.8f}[/]", f"{r['wagered']:.8f}",
                  started_short)
    console.print(t)


def cmd_analytics(since: str = None, until: str = None):
    """Analytics report — aggregates over all saved sessions, optionally
    restricted to a started_at date window (v2.31.0)."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found — run a session first.[/]")
        return
    try:
        where, wparams, wlabel = _stats_date_window(since, until)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return
    lifetime = _analytics_lifetime(where, wparams)
    if lifetime["sessions_total"] == 0:
        console.print("[yellow]No sessions in the DB yet — run a session first.[/]")
        return

    console.print(Rule(
        f"[bold cyan]{'Range' if (since or until) else 'Lifetime'} analytics[/] — "
        f"{lifetime['sessions_total']:,} sessions, "
        f"{lifetime['bets_total']:,} bets, {lifetime['wagered_total']:.8f} wagered"
        f"{wlabel}"))

    # Top-line panel
    p = lifetime['profit_total']
    pc = 'green' if p >= 0 else 'red'
    ps = '+' if p >= 0 else ''
    grid = Table.grid(padding=(0, 2))
    grid.add_row("Total sessions:",     f"{lifetime['sessions_total']:,}")
    grid.add_row("Sessions in profit:", f"{lifetime['sessions_in_profit']:,}")
    grid.add_row("Sessions in loss:",   f"{lifetime['sessions_in_loss']:,}")
    grid.add_row("Total bets:", f"{lifetime['bets_total']:,}   W: {lifetime['wins_total']:,}   L: {lifetime['losses_total']:,}")
    grid.add_row("Win rate:",           f"{lifetime['win_rate_pct']:.1f}%")
    grid.add_row("Total wagered:",      f"{lifetime['wagered_total']:.8f}")
    grid.add_row("Total P/L:",          f"[{pc}]{ps}{p:.8f}[/]")
    grid.add_row("Best session:",       f"+{lifetime['best_profit']:.8f}")
    grid.add_row("Worst session:",      f"{lifetime['worst_profit']:.8f}")
    console.print(Panel(grid, title="[bold]💰 Overall[/]", border_style=pc))

    _print_analytics_table("[bold]🎲 By game[/]",     _analytics_by_game(where, wparams),     "game",     "Game")
    _print_analytics_table("[bold]🎯 By strategy[/]", _analytics_by_strategy(where, wparams), "strategy", "Strategy")

    _print_top_sessions_table("[bold]🏆 Top 5 best sessions[/]",
                              _analytics_top_sessions("profit_desc", 5, where, wparams))
    _print_top_sessions_table("[bold]🔻 Top 5 worst sessions[/]",
                              _analytics_top_sessions("profit_asc", 5, where, wparams))

    console.print("\n[dim]  Use --stats for recent 20 sessions in detail, "
                  "or --session-bets ID to drill into one.[/]\n")


def _stats_date_window(since: str = None, until: str = None):
    """v2.30.9 — build an optional date-window clause for the sessions table.

    `since`/`until` accept bare dates (YYYY-MM-DD) or full ISO timestamps.
    A bare `until` date is extended to end-of-day so the day is inclusive.
    ISO strings compare correctly lexically in SQLite. Returns
    (clause, params, label) where clause is "" or " WHERE …" and label
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
    if not conds:
        return "", [], ""
    clause = " WHERE " + " AND ".join(conds)
    label = (f"  [{(since or '…').replace('T', ' ')} → "
             f"{(until or '…').replace('T', ' ')}]")
    return clause, params, label


def _normalize_window_bound(value):
    """v2.33.0 — turn a --since/--until value into the stored ISO form.

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


def cmd_stats(since: str = None, until: str = None):
    """Show session statistics, optionally filtered to a date window."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    try:
        where, params, window_label = _stats_date_window(since, until)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return
    conn = _db_conn()
    # v2.30.9 — range reviews want more rows than the default 20.
    limit = 50 if (since or until) else 20

    # session list — fetch all columns
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
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0)
        FROM sessions{where} ORDER BY id DESC LIMIT ?
    """, (*params, limit)).fetchall()

    console.print(Rule(f"[bold cyan]Session History (last {limit})[/]{window_label}"))

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
        console.print(f"    [dim]Streaks:[/] W+{mws}  L-{mls}")
        console.print()

    # totals — v2.30.9: honor the same date window as the listing.
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
    conn.close()

    sessions, tot_bets, tot_wins, tot_losses, tot_profit, tot_wagered, \
        best_ws, best_ls, best_profit, worst_profit, max_bets, avg_bets, \
        all_hi_bal, all_hi_win, all_big_loss, avg_bpm, \
        all_peak_bps, all_peak_bpm, avg_bps, \
        max_wins_s, avg_wins_s, max_losses_s, avg_losses_s = totals
    pc = "green" if tot_profit >= 0 else "red"
    ps = "+" if tot_profit >= 0 else ""
    wr = f"{tot_wins/tot_bets*100:.1f}%" if tot_bets > 0 else "—"
    bp_c = "green" if best_profit >= 0 else "red"
    wp_c = "green" if worst_profit >= 0 else "red"
    avg_pnl = tot_profit / tot_bets if tot_bets > 0 else 0
    avg_pc = "green" if avg_pnl >= 0 else "red"
    avg_ps = "+" if avg_pnl >= 0 else ""

    console.print(Rule(f"[bold cyan]{'Range' if (since or until) else 'All-Time'} Totals[/]{window_label}"))
    console.print(f"  [dim]Sessions:[/]      {sessions}")
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

_HELP_DESCRIPTION = """\
WolfBet AutoBot — high-speed auto-betting engine.

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
    wolfbet.py --benchmark 20         # standalone API-latency probe (no bets)
    wolfbet.py --diagnose             # analyse running session's bottleneck

  Config mutation (no wizard, one-shot)
    wolfbet.py --config-set max_profit=0.005 base_bet=0.0001
    wolfbet.py --config-set delay=0.35 pipeline=3
    wolfbet.py --config-set recurring=on recdelay=90 maxbets=1000
    wolfbet.py --config-set proxy=https://user:pass@10.0.0.1:8080
    wolfbet.py --config-set purgedays=7
    wolfbet.py --preset martingale-safe --daemon

  Data
    wolfbet.py --last-bets 20         # last 20 bets across sessions
    wolfbet.py --session-bets 42      # every bet from session #42
    wolfbet.py --export /tmp/out.csv  # export sessions (CSV or JSON by ext)
    wolfbet.py --cleanup 7            # purge bet rows > 7 days (aggregates kept)

  Backtest / replay (no API calls, no real balance risk)
    wolfbet.py --backtest --simulate 10000 --seed 42
    wolfbet.py --backtest --replay-session 42 --preset new-strategy

  Concurrency
    wolfbet.py --resume --pipeline 3  # 3 in-flight bets (Flat strategies only)

Env vars: see README §Environment Variables (`WOLFBET_API_KEY`, `WOLFBET_DB_PATH`, …)
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
    group.add_argument("--stop",    action="store_true", help="Stop a running daemon session")
    group.add_argument("--list-presets", action="store_true", help="List saved presets")
    group.add_argument("--stats",   action="store_true", help="Show recent 20 sessions in detail")
    # v2.30.9 fix — modifiers, not commands: must combine WITH --stats, so they
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
                            "(delay=0.35, maxprofit=0.005, pipeline=3, …). api_key is refused — "
                            "use the WOLFBET_API_KEY env var.")
    group.add_argument("--cleanup", nargs="?", type=int, metavar="DAYS", const=-1,
                       help="Purge old bet rows from the DB (default: state.purge_days, "
                            "explicit: --cleanup N for N days, clamped 1-30). "
                            "Session aggregates preserved.")
    group.add_argument("--benchmark", nargs="?", type=int, metavar="N", const=20,
                       help="Standalone API-latency probe (no bets placed). Default N=20, "
                            "clamped 3-200. Reports p50/p99/mean + BPS ceiling + verdict.")
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
    parser.add_argument("--pipeline", type=int, metavar="N", default=None,
                        help="Pipeline depth (Flat-only, 1-8). 1 = serial (default). "
                             "Non-Flat strategies and auto-bet games are auto-coerced to 1.")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging (dev mode)")

    args = parser.parse_args()

    # Pipeline depth CLI override — applies after config/preset load below.
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
            if "not found" in str(e).lower():
                console.print("[dim]  Tip: run `python wolfbet.py --stats` to see recent session IDs.[/]")
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
        cmd_stats(since=args.since, until=args.until)
        return
    if args.analytics:
        cmd_analytics(since=args.since, until=args.until)
        return
    if args.last_bets:
        cmd_last_bets(args.last_bets)
        return
    if args.session_bets is not None:
        cmd_session_bets(args.session_bets)
        return
    if args.config_show:
        cmd_config_show()
        return
    if args.config_set:
        cmd_config_set(args.config_set)
        return
    if args.cleanup is not None:
        init_db()
        # `--cleanup` with no value comes through as -1 (const), meaning "use configured default".
        cmd_cleanup(None if args.cleanup == -1 else args.cleanup)
        return
    if args.benchmark is not None:
        cmd_benchmark(args.benchmark)
        return
    if args.diagnose:
        cmd_diagnose()
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

    # Apply CLI --pipeline override AFTER config/preset load so it wins.
    if args.pipeline is not None:
        state.pipeline_depth = args.pipeline

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
        # v2.27.0 — drain the async bet-writer so no queued rows are lost.
        _stop_bet_writer(timeout=5.0)
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
    # v2.28.3 — SIGHUP triggers live config reload from disk (used by
    # `wolfctl config-set` after save to hot-apply mid-session tweaks).
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _sig_reload)

    # ── Startup retention purge — matches TG's tg/engine.py:979 pattern.
    try:
        _startup_deleted = cleanup_old_bets(DB_PATH, bet_age_days=state.purge_days)
        if _startup_deleted:
            logger.info("STARTUP RETENTION purged %d old bet rows (>%dd)",
                        int(_startup_deleted), state.purge_days)
    except Exception as _e:
        logger.warning("startup retention purge failed: %s", _e)

    # ── Async bet-writer thread (v2.27.0 — off the hot path) ──
    if state.db_write_mode == "async":
        _start_bet_writer()
        logger.info("BET WRITER started (async, batch=%d)", _BET_WRITER_BATCH_SIZE)

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
        _stop_bet_writer(timeout=5.0)   # v2.27.0 — drain async writer
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

    _stop_bet_writer(timeout=5.0)   # v2.27.0 — drain async writer
    _db_save_session()
    _cleanup_state_file()
    _print_summary()


if __name__ == "__main__":
    main()

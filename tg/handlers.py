"""Telegram command handlers: all bot commands and callback queries."""

import os
import re
import json
import time
import shutil
import asyncio
import sqlite3
import subprocess
from datetime import datetime
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

# v2.28.0: shared helpers (previously duplicated between CLI and TG).
# validate_proxy: broader off-value vocabulary than the old inline check.
# mask_proxy: port-safe (previously leaked "None" when the URL had no port).
# percentile + benchmark_verdict: TG /benchmark gains p50/p99 + plain verdict.
from core.net_helpers import validate_proxy, mask_proxy
from shared.core.lanes import LANE_NAME_RE
from core.validation import finite_float
from core.diagnostics import percentile, benchmark_verdict

from . import VERSION, dashboard_link
from .config import APP_ENV, CONFIG_KEYS, DATA_DIR, TIERS, get_user_tier, logger
from .database import (
    user_db_path, load_user_config, save_user_config,
    fetch_all, fetch_one, execute,
    load_presets, save_presets,
)
from core.database import migrate_db, cleanup_old_bets
from core.strategy import (
    STRATEGIES, STRATEGY_NAMES, STRATEGY_BY_NAME, StrategyRule,
    describe_rule, load_rules_from_text,
)
from .engine import BettingEngine, STREAK_RESET_REASON
from .formatter import (
    format_status, format_stop, format_milestone, format_session_row,
    format_session_detail, format_all_time, format_lastbets, format_lanes,
)


# ── Active engines (multiple per user) ───────────────────
# user_id → {slot: BettingEngine}
active_engines: Dict[int, Dict[int, BettingEngine]] = {}
_engine_chat_ids: Dict[int, int] = {}  # user_id → chat_id for resume
_next_slot: Dict[int, int] = {}  # user_id → next slot number


# ── Recurring bet state ─────────────────────────────────
# recurring_state[user_id][slot] = {"delay": int, "chat_id": int, "config": dict, "timer_task": Task|None}
recurring_state: Dict[int, Dict[int, dict]] = {}

# ── Active monitors ─────────────────────────────────────
# user_id → {slot: asyncio.Task}
active_monitors: Dict[int, Dict[int, asyncio.Task]] = {}

# ── Recurring notification throttle ──────────────────────
# Prevents Telegram flood bans during fast recurring cycles.
# user_id → {"stop": float, "start": float}  (unix timestamps)
_recurring_notify_ts: Dict[int, Dict[str, float]] = {}
RECURRING_NOTIFY_THROTTLE = 30  # max one stop + one start notification per 30s per user


def _throttle_recurring(user_id: int, kind: str) -> bool:
    """Return True (and record timestamp) if enough time has passed to send this notification."""
    now = time.time()
    user_ts = _recurring_notify_ts.setdefault(user_id, {})
    if now - user_ts.get(kind, 0) >= RECURRING_NOTIFY_THROTTLE:
        user_ts[kind] = now
        return True
    return False


def _get_running(user_id: int) -> Dict[int, BettingEngine]:
    """Return {slot: engine} for running engines only."""
    return {s: e for s, e in active_engines.get(user_id, {}).items() if e.running}


def _resolve_engine(user_id: int, args: list) -> tuple:
    """Resolve which engine a command targets.
    Returns (slot, engine, error_message).
    Single session auto-resolves. Multiple requires slot arg.
    """
    running = _get_running(user_id)
    if not running:
        return None, None, "No session running."
    if len(running) == 1:
        slot, engine = next(iter(running.items()))
        return slot, engine, None
    # Multiple: check if slot specified
    if args:
        try:
            slot = int(args[0])
            if slot in running:
                return slot, running[slot], None
            return None, None, f"No session in slot {slot}. Active: {', '.join(f'#{s}' for s in sorted(running))}"
        except ValueError:
            pass
    slots = ", ".join(f"#{s}" for s in sorted(running))
    return None, None, f"Multiple sessions running ({slots}). Specify slot number."


PRIVATE_CONFIG_KEYS = frozenset({"api_key", "proxy"})


def read_config(user_id):
    """The user's saved settings without the API key and proxy (a proxy URL can hold a password)."""
    config = load_user_config(user_id)
    return {k: config[k] for k in CONFIG_KEYS if k in config and k not in PRIVATE_CONFIG_KEYS}


def stop_slot(user_id: int, slot: int, engine: BettingEngine) -> None:
    """Stop one session as the user asked, without restarting it if recurring is on."""
    _cancel_recurring_for_slot(user_id, slot)
    engine.stop_reason = "Stopped by user"
    engine.stop()


def _alloc_slot(user_id: int) -> int:
    """Allocate the next available slot for a user."""
    slot = _next_slot.get(user_id, 1)
    existing = active_engines.get(user_id, {})
    while slot in existing:
        slot += 1
    _next_slot[user_id] = slot + 1
    return slot


# ── /start ───────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🐺 *WolfBet Bot v{VERSION}*\n\n"
        "Auto-betting engine for WolfBet Dice via Telegram.\n\n"
        "*Setup:*\n"
        "/setkey: Set your WolfBet API key\n"
        "/balance: Check your balances\n"
        "/seed: Rotate WolfBet server seed\n"
        "/config: View current configuration\n\n"
        "*Configure:*\n"
        "/set currency usdt\n"
        "/set game dice\n"
        "/set strategy martingale\n"
        "/set multiplier 2.01\n"
        "/set basebet 0.0001\n"
        "/set rule over\n"
        "/set maxprofit 0.01\n"
        "/set maxloss 0.005\n\n"
        "*Session:*\n"
        "/bet: Start betting session (up to 5 concurrent)\n"
        "/stop: Stop session (`/stop all` for all)\n"
        "/stop recurring: Cancel all recurring bets\n"
        "/tweak \u2014 Live-edit running session\n"
        "/pause / /resume (add slot# for multi-session)\n"
        "/status: Live status (`/status 2` for slot)\n"
        "/monitor: Auto-updating status\n"
        "/stats: Session history\n"
        "/session: Session detail\n\n"
        "/strategies: List all strategies\n"
        "/help: Full command reference",
        parse_mode="Markdown",
    )


# ── /help ────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands:*\n\n"
        "*Setup*\n"
        "/setkey: API key (message auto-deleted)\n"
        "/balance: Show all balances\n"
        "/seed: Rotate WolfBet server seed\n"
        "/config: Show current config\n"
        "/set: Set parameter\n"
        "/strategies: List strategies\n\n"
        "*Parameters for* /set*:*\n"
        "`currency`: usdt, btc, eth, ltc, trx, bch, doge, xrp\n"
        "`game`: dice (API v1), dice-manual (Manual v1), limbo (v2), limbo-auto (Auto v2), dice-auto (Auto v2)\n"
        "`proxy`: http://user:pass@ip:port or socks5://... (`off` to disable)\n"
        "`pipeline`: concurrent requests for auto-bet (1-50, default 1)\n"
        "`strategy`: flat, martingale, antimartingale, dalembert, paroli, wolfrider, delaymartingale, rulebased\n"
        "`multiplier`: target multiplier (e.g. 2.01)\n"
        "`basebet`: base bet amount\n"
        "`rule`: over / under\n"
        "`lossmult`: loss multiplier (default 2.0)\n"
        "`winmult`: win multiplier (default 1.0)\n"
        "`delay`: seconds between bets\n"
        "`maxprofit`: stop at profit\n"
        "`maxloss`: stop at loss\n"
        "`maxbets`: stop after N bets\n"
        "`maxwins`: stop after N wins\n"
        "`minbalance`: stop below balance\n"
        "`delaythreshold`: Delay Martingale threshold\n"
        "`milestonebets`: Notify every N bets (0=off)\n"
        "`milestonewins`: Notify every N wins (0=off)\n"
        "`milestonelosses`: Notify every N losses (0=off)\n"
        "`milestoneprofit`: Notify every X profit (0=off)\n"
        "`profitthreshold`: Auto-raise base bet every X profit (`off`/`0` to disable)\n"
        "`profitincrement`: Amount to add to base bet (`off`/`0` to disable)\n"
        "`piscope`: `session` (each session's profit) or `run` (your total since the run started)\n"
        "`recurring`: Auto-restart on stop condition (`on`/`off`)\n"
        "`recurringdelay`: Seconds before restart (default 60)\n"
        "`srloss`: Restart on first win after N losses in a row (profit ≥ 0; `off` to disable)\n"
        "`srdelay`: Seconds before streak-reset restart (default 600)\n"
        "`streakdelay_loss`: Delay after N losses (`5:1.0` = every 5 losses → 1s)\n"
        "`streakdelay_win`: Delay after N wins (`10:0.5` = every 10 wins → 0.5s)\n"
        "`streakdelay_bets`: Delay after every N bets (`100:0.5` = every 100 bets → 0.5s)\n"
        "`streakbet_loss`: Scale bet after N losses (`10:0.5` = every 10 losses → halve)\n"
        "`basebet_pct`: Base bet as fraction of balance (`0.001` = 0.1%, `off` to disable)\n"
        "`purgedays`: Auto-delete bets older than N days (1-30, default 1)\n\n"
        "*Session* _(up to 5 concurrent)_\n"
        "/bet: Start session\n"
        "/stop: Stop session (`/stop all` or `/stop 2`)\n"
        "/stop recurring: Cancel all recurring bets and stop sessions\n"
        "/runreset: Start a new run total for `piscope run` (stop sessions first)\n"
        "/tweak: Live-edit running session (stop conditions, pipeline, bet size)\n"
        "/pause: Pause (`/pause all` or `/pause 2`)\n"
        "/resume: Resume (`/resume all` or `/resume 2`)\n"
        "/status: Live status (`/status 2` for specific slot)\n"
        "/lanes: CLI lanes, their key and session (owner)\n"
        "/laneadd NAME [from K]: add a CLI lane (owner)\n"
        "/lanerename K NAME: rename a CLI lane (owner)\n"
        "/monitor: Auto-updating status (default 5s)\n"
        "/stats: Session history\n"
        "/session: Detailed session report\n"
        "/lastbets: Recent bets\n"
        "/diagnose: Bottleneck analysis (top blocker + advice)\n"
        "/analytics: Lifetime analytics (per-game, per-strategy, top-5)\n"
        "/analytics [SINCE] [UNTIL]: date-windowed (e.g. 2026-08-14)\n\n"
        "*Rules*\n"
        "/rules: List current rules\n"
        "/addrule: Add rule (JSON)\n"
        "/delrule: Delete rule by number\n"
        "/editrule: Edit rule by number (JSON patch)\n"
        "/clearrules: Clear all rules\n\n"
        "*Presets*\n"
        "/presets: List presets\n"
        "/savepreset: Save current config\n"
        "/loadpreset: Load preset\n\n"
        "*Maintenance*\n"
        "/cleanup: Purge old bet records (`/cleanup 3` for 3 days)\n"
        "/delsession: Delete a session by ID\n\n"
        "*Web*\n"
        "/web: Open web dashboard\n"
        "/benchmark: Test API response speed",
        parse_mode="Markdown",
    )


# ── /setkey ──────────────────────────────────────────────
async def cmd_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except Exception:
        pass

    if not context.args:
        await update.effective_chat.send_message(
            "Usage: /setkey <your\\_wolfbet\\_api\\_key>", parse_mode="Markdown")
        return

    user_id = update.effective_user.id
    api_key = context.args[0]
    config = load_user_config(user_id)
    config["api_key"] = api_key
    save_user_config(user_id, config)

    await update.effective_chat.send_message(
        "API key saved. Your message was deleted for security.\n"
        "Test with /balance", parse_mode="Markdown")


# ── /benchmark ───────────────────────────────────────────
async def _benchmark_post(client, url: str, payload: dict):
    """One timed zero-amount bet against the casino API."""
    return await client.post(url, json=payload)


async def cmd_benchmark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run test bets (amount=0) and measure API response times."""
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    api_key = config.get("api_key")
    if not api_key:
        await update.message.reply_text("Set your API key first: /setkey <key>", parse_mode="Markdown")
        return

    count = 10
    if context.args:
        try:
            count = min(int(context.args[0]), 50)
        except ValueError:
            pass

    msg = await update.message.reply_text(f"Running {count} test bets (amount=0)...")

    import httpx

    from .config import API_BASE
    from .engine import REQUEST_TIMEOUT

    currency = config.get("currency") or "btc"
    multiplier = float(config.get("multiplier") or 2.0)
    bet_value = round(100 - 99 / multiplier - 0.01, 2)
    mult_str = f"{multiplier:.4f}".rstrip("0").rstrip(".") or "0"
    times = []
    statuses = []   # v2.28.0: capture HTTP status for shared verdict tiers

    payload = {
        "currency": currency,
        "game": "dice",
        "amount": "0",
        "rule": config.get("rule") or "over",
        "multiplier": mult_str,
        "bet_value": f"{bet_value:.2f}",
    }

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=REQUEST_TIMEOUT,
    ) as client:
        for _ in range(count):
            try:
                t0 = time.time()
                r = await _benchmark_post(client, f"{API_BASE}/bet/place", payload)
                elapsed = (time.time() - t0) * 1000
                times.append(elapsed)
                statuses.append(r.status_code)
            except Exception:
                times.append(-1)
                statuses.append("ERR")

    valid = [t for t in times if t > 0]
    if not valid:
        await msg.edit_text("All requests failed.")
        return

    avg = sum(valid) / len(valid)
    mn, mx = min(valid), max(valid)
    failed = len(times) - len(valid)
    theoretical_bps = 1000 / avg if avg > 0 else 0

    # v2.28.0: shared with CLI's --benchmark verdict tiers.
    sorted_valid = sorted(valid)
    p50 = percentile(sorted_valid, 0.50)
    p99 = percentile(sorted_valid, 0.99)
    _colour, verdict = benchmark_verdict(
        p50=p50, p99=p99, statuses=statuses, sample_count=count,
    )

    lines = [
        f"*Benchmark Results ({count} bets)*\n",
        f"  Avg: `{avg:.0f}ms`",
        f"  p50: `{p50:.0f}ms`",
        f"  p99: `{p99:.0f}ms`",
        f"  Min: `{mn:.0f}ms`",
        f"  Max: `{mx:.0f}ms`",
        f"  Failed: `{failed}`\n",
        f"  Max theoretical: `{theoretical_bps:.1f}` bets/sec",
        f"\n_{verdict}_",
        f"\n*Individual times:*",
        f"`{'  '.join(f'{t:.0f}ms' if t > 0 else 'FAIL' for t in times)}`",
    ]
    await msg.edit_text("\n".join(lines), parse_mode="Markdown")


# ── /balance ─────────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    api_key = config.get("api_key")
    if not api_key:
        await update.message.reply_text(
            "Set your API key first: /setkey <token>", parse_mode="Markdown")
        return

    await update.message.reply_text("Fetching balances...")
    try:
        engine = BettingEngine(user_id, "", api_key, {})
        await engine._recreate_http()
        balances = await engine.api_get_balances()
        if not balances:
            await update.message.reply_text("No balances found or API key invalid.")
            return

        lines = ["*Balances:*"]
        for b in balances:
            cur = b.get("currency", "?").upper()
            amt = float(b.get("amount", 0))
            if amt > 0:
                lines.append(f"`{cur:>6}  {amt:.8f}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        from telegram.error import RetryAfter, NetworkError, TimedOut
        if isinstance(e, (RetryAfter, NetworkError, TimedOut)):
            logger.warning("cmd_balance send failed: %s", e)
            return
        logger.error("cmd_balance error: %s", e, exc_info=True)
        msg = f"Error: `{e}`" if APP_ENV != "production" else "Something went wrong. Please try again."
        try:
            await update.message.reply_text(msg, parse_mode="Markdown")
        except Exception:
            pass


# ── /config ──────────────────────────────────────────────
CTL_NAME = "wolfctl"
REPO_DIR = os.environ.get("BOT_REPO_DIR", "/root/wolfbet")
UPDATE_LOG_NAME = "update.log"


def _owner_id() -> int | None:
    """Configured Telegram owner id, or None when TG_OWNER_ID is missing or not numeric."""
    owner = os.environ.get("TG_OWNER_ID", "").strip()
    return int(owner) if owner.isdigit() else None


def _is_owner(user_id: int) -> bool:
    """True only for the explicitly configured TG_OWNER_ID."""
    return _owner_id() is not None and user_id == _owner_id()


def _validated_path(path: str, *, is_dir: bool) -> bool:
    return os.path.isabs(path) and (os.path.isdir(path) if is_dir else os.path.isfile(path))


def _ctl_path() -> str | None:
    """The installed wolfctl, or None when it is missing or not executable."""
    path = shutil.which(CTL_NAME) or f"/root/.local/bin/{CTL_NAME}"
    if _validated_path(path, is_dir=False) and os.access(path, os.X_OK):
        return path
    return None


LANES_DENIED = "Only the owner can manage lanes."
LANES_FAILED = "Could not read the lanes. Try again."


def _key_label(key_ref: str) -> str:
    """A vault key as `label (...last4)`; the bare id when the vault doesn't hold it."""
    try:
        from shared.core.vault import vault_info
        info = vault_info("wolfbet", key_ref, log=logger)
    except ImportError:
        info = None
    return f"{info.label} (...{info.last4})" if info else key_ref


def _error_suffix(e: Exception) -> str:
    return "" if APP_ENV == "production" else f" ({type(e).__name__})"


async def _read_lanes() -> dict:
    """`wolfctl lanes --json` parsed; raises OSError, TimeoutExpired, RuntimeError or ValueError."""
    ctl = _ctl_path()
    if not ctl:
        raise FileNotFoundError(CTL_NAME)
    result = await asyncio.to_thread(subprocess.run, [ctl, "lanes", "--json"],
                                     capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(f"wolfctl lanes rc={result.returncode}")
    return json.loads(result.stdout)


_LANE_READ_ERRORS = (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError)
_REFRESH_ROW = [InlineKeyboardButton("Refresh", callback_data="lanes_refresh")]


def _lane_state(row: dict) -> str:
    """`stopped`, `paused`, `running` or `nostate` for one `wolfctl lanes --json` row."""
    if not row.get("alive"):
        return "stopped"
    if row.get("state") is None:
        return "nostate"
    return "paused" if row.get("paused") else "running"


_LANE_BUTTONS = {
    "running": ("pause", "restart", "stop"),
    "paused": ("resume", "restart", "stop"),
    "nostate": ("restart", "stop"),
    "stopped": ("start",),
}


def _lane_buttons(data: dict) -> list:
    """One row of (label, callback_data) per lane, in lane order, for the actions its state allows."""
    rows = []
    for row in sorted(data.get("lanes") or [], key=lambda r: r.get("lane", 0)):
        k = row.get("lane")
        actions = _LANE_BUTTONS[_lane_state(row)] + (("remove",) if k != 1 else ())
        rows.append([(f"{action.capitalize()} {k}", f"lane:{action}:{k}") for action in actions])
    return rows


async def _lanes_view(note: str = "") -> tuple:
    """(text, keyboard) for the lane list, `note` first; Refresh only when the read fails."""
    try:
        data = await _read_lanes()
    except _LANE_READ_ERRORS as e:
        logger.warning("lanes: %s: %s", type(e).__name__, e)
        text = LANES_FAILED + _error_suffix(e)
        return (f"{note}\n\n{text}" if note else text), InlineKeyboardMarkup([_REFRESH_ROW])
    text = format_lanes(data, _key_label)
    keyboard = [[InlineKeyboardButton(label, callback_data=cb) for label, cb in row]
                for row in _lane_buttons(data)]
    return (f"{note}\n\n{text}" if note else text), InlineKeyboardMarkup(keyboard + [_REFRESH_ROW])


async def cmd_lanes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List every CLI lane with its state, key label, session and action buttons (owner only)."""
    if not _is_owner(update.effective_user.id):
        await update.message.reply_text(LANES_DENIED)
        return
    text, keyboard = await _lanes_view()
    await update.message.reply_text(text, reply_markup=keyboard)


async def _edit_lanes(query, note: str = "") -> None:
    text, keyboard = await _lanes_view(note)
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest:
        pass


async def _lanes_refresh(query) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer(LANES_DENIED, show_alert=True)
        return
    await query.answer()
    await _edit_lanes(query)


_LANE_CALLBACK = re.compile(
    r"^(lane|lanego):(pause|resume|stop|start|restart|restart_end|remove|remove_end):([1-9][0-9]?)$")
_LANE_NAMED_CALLBACK = re.compile(r"^(laneadd|lanern):([1-9][0-9]?):(.{1,32})$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LANE_K = re.compile(r"^[1-9][0-9]?$")
_CANCEL = InlineKeyboardButton("Cancel", callback_data="lanes_refresh")

LANE_NAME_RULE = ("Lane names are 1-32 letters, digits, space, _ or -, "
                  "not starting with - or a space.")
LANE_ONE_KEPT = "Lane 1 is the main lane and can't be removed."

_LANE_ARGV = {
    "pause": (("pause",), ()), "resume": (("resume",), ()), "stop": (("stop",), ()),
    "start": (("start",), ()), "restart": (("restart",), ()),
    "restart_end": (("restart",), ("--at-boundary",)),
    "remove": (("lane", "remove"), ()), "remove_end": (("lane", "remove"), ("--after-session",)),
}
_ALIVE = ("running", "paused", "nostate")
_LANE_NEEDS = {
    "pause": ("running",), "resume": ("paused",), "start": ("stopped",),
    "stop": _ALIVE, "restart": _ALIVE, "restart_end": _ALIVE,
    "remove": _ALIVE + ("stopped",), "remove_end": _ALIVE,
}
_LANE_IS = {"stopped": "already stopped", "paused": "already paused",
            "running": "already running", "nostate": "already running"}


def _lane_name_ok(name: str) -> bool:
    """The CLI's lane-name rule; a leading - would also reach wolfctl as an option."""
    return bool(LANE_NAME_RE.match(name)) and not name.startswith(("-", " "))


def _parse_lane_callback(data: str):
    """(stage, action, lane) from trusted-shape callback data, else None."""
    m = _LANE_CALLBACK.match(data or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def _parse_named_callback(data: str):
    """(verb, lane, name) from `laneadd:K:NAME` or `lanern:K:NAME`, else None."""
    m = _LANE_NAMED_CALLBACK.match(data or "")
    if not m or not _lane_name_ok(m.group(3)):
        return None
    return m.group(1), int(m.group(2)), m.group(3)


def _lane_confirm(action: str, k: int) -> tuple:
    """(question, buttons) asked before a lane action runs."""
    if action in ("restart", "remove"):
        verb = action.capitalize()
        return f"{verb} lane {k}?", InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{verb} now" if action == "restart" else "Now",
                                 callback_data=f"lanego:{action}:{k}"),
            InlineKeyboardButton("At session end", callback_data=f"lanego:{action}_end:{k}"),
            _CANCEL]])
    verb = action.capitalize()
    question = f"{verb} lane {k}?"
    if action == "stop" and k == 1:
        question = "Stop lane 1? It is the main lane."
    return question, InlineKeyboardMarkup([[
        InlineKeyboardButton(verb, callback_data=f"lanego:{action}:{k}"), _CANCEL]])


def _lane_row(data: dict, k: int):
    return next((r for r in data.get("lanes") or [] if r.get("lane") == k), None)


def _stale_reason(data: dict, action: str, k: int) -> str | None:
    """Why `action` no longer fits lane `k` (it changed since the message), else None."""
    if action.startswith("remove") and k == 1:
        return LANE_ONE_KEPT
    row = _lane_row(data, k)
    if row is None:
        return f"Lane {k} is not configured."
    state = _lane_state(row)
    if state in _LANE_NEEDS[action]:
        return None
    if action == "remove_end":
        return f"Lane {k} is stopped. Remove it with Now."
    if action == "resume":
        return f"Lane {k} is not paused."
    return f"Lane {k} is {_LANE_IS[state]}."


def _wolfctl_lines(text: str) -> list:
    lines = [_ANSI.sub("", line).strip() for line in (text or "").splitlines()]
    return [line for line in lines if line]


def _wolfctl_line(text: str, last: bool) -> str:
    lines = _wolfctl_lines(text)
    if not lines:
        return ""
    return (lines[-1] if last else lines[0])[:200]


async def _run_wolfctl(args: list, k: int, label: str):
    """Run `wolfctl *args`; (ok, note) where a failure note is wolfctl's own first line.

    The CLI prints its errors on stdout and bash usage errors on stderr, so both are read.
    """
    argv = [_ctl_path() or CTL_NAME, *args]
    try:
        result = await asyncio.to_thread(subprocess.run, argv, capture_output=True,
                                         text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("lane %s %d: %s", label, k, type(e).__name__)
        return False, f"Lane {k}: wolfctl did not answer. Check /lanes again.{_error_suffix(e)}"
    logger.info("lane %s %d: rc=%d", label, k, result.returncode)
    if result.returncode != 0:
        return False, (_wolfctl_line(result.stderr, last=False)
                       or _wolfctl_line(result.stdout, last=False) or f"Lane {k}: {label} failed.")
    return True, result.stdout


async def _run_lane_action(action: str, k: int) -> str:
    """Run one wolfctl verb on lane `k` only; the line wolfctl printed, or a safe message."""
    try:
        data = await _read_lanes()
    except _LANE_READ_ERRORS as e:
        logger.warning("lane %s %d: %s", action, k, type(e).__name__)
        return LANES_FAILED + _error_suffix(e)
    reason = _stale_reason(data, action, k)
    if reason:
        return reason
    before, after = _LANE_ARGV[action]
    ok, out = await _run_wolfctl([*before, str(k), *after], k, action)
    return (_wolfctl_line(out, last=True) or f"Lane {k}: done.") if ok else out


async def _run_named(verb: str, k: int, name: str) -> str:
    """Add a lane named `name` copying lane `k`, or rename lane `k`; the note to show."""
    if verb == "lanern":
        try:
            data = await _read_lanes()
        except _LANE_READ_ERRORS as e:
            logger.warning("lane rename %d: %s", k, type(e).__name__)
            return LANES_FAILED + _error_suffix(e)
        if _lane_row(data, k) is None:
            return f"Lane {k} is not configured."
        ok, out = await _run_wolfctl(["lane", "rename", str(k), name], k, "rename")
        return f"Lane {k} renamed to {name}." if ok else out
    ok, out = await _run_wolfctl(["lane", "add", name, "--from", str(k)], k, "add")
    if not ok:
        return out
    lines = _wolfctl_lines(out)
    added = next((line for line in lines if line.startswith("Lane ")), "")
    note = " ".join(dict.fromkeys(line for line in (added, lines[-1] if lines else "") if line))
    return note[:200] or f"Lane {name} added."


async def _lane_callback(query) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer(LANES_DENIED, show_alert=True)
        return
    parsed = _parse_lane_callback(query.data)
    if parsed is None:
        await query.answer("Unknown button.", show_alert=True)
        return
    stage, action, k = parsed
    await query.answer()
    if stage == "lane":
        question, keyboard = _lane_confirm(action, k)
        try:
            await query.edit_message_text(question, reply_markup=keyboard)
        except BadRequest:
            pass
        return
    await _edit_lanes(query, await _run_lane_action(action, k))


async def _named_lane_callback(query) -> None:
    if not _is_owner(query.from_user.id):
        await query.answer(LANES_DENIED, show_alert=True)
        return
    parsed = _parse_named_callback(query.data)
    if parsed is None:
        await query.answer("Unknown button.", show_alert=True)
        return
    await query.answer()
    await _edit_lanes(query, await _run_named(*parsed))


def _named_confirm(verb: str, k: int, name: str) -> tuple:
    if verb == "laneadd":
        copying = f", copying lane {k}'s settings" if k != 1 else ""
        question, button = f"Add the lane {name}{copying}?", "Add"
    else:
        question, button = f"Rename lane {k} to {name}?", "Rename"
    return question, InlineKeyboardMarkup([[
        InlineKeyboardButton(button, callback_data=f"{verb}:{k}:{name}"), _CANCEL]])


def _parse_laneadd(args: list):
    """(source lane, name) from `/laneadd NAME [from K]` args, else None."""
    args = list(args or [])
    k = 1
    if len(args) >= 3 and args[-2].lower() == "from":
        if not _LANE_K.match(args[-1]):
            return None
        k, args = int(args[-1]), args[:-2]
    name = " ".join(args)
    return (k, name) if args else None


async def _reply_named(update: Update, usage: str, parsed) -> None:
    if not _is_owner(update.effective_user.id):
        await update.message.reply_text(LANES_DENIED)
        return
    if parsed is None:
        await update.message.reply_text(f"Usage: {usage}")
        return
    verb, k, name = parsed
    if not _lane_name_ok(name):
        await update.message.reply_text(LANE_NAME_RULE)
        return
    question, keyboard = _named_confirm(verb, k, name)
    await update.message.reply_text(question, reply_markup=keyboard)


async def cmd_laneadd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add one CLI lane, optionally copying lane K's settings, after a confirm (owner only)."""
    parsed = _parse_laneadd(context.args)
    await _reply_named(update, "/laneadd NAME [from K]",
                       ("laneadd", *parsed) if parsed else None)


async def cmd_lanerename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rename one CLI lane after a confirm; it keeps betting (owner only)."""
    args = list(context.args or [])
    ok = len(args) >= 2 and _LANE_K.match(args[0])
    await _reply_named(update, "/lanerename K NAME",
                       ("lanern", int(args[0]), " ".join(args[1:])) if ok else None)


def _update_argv(ctl_path: str, repo_dir: str, log_file: str) -> list[str]:
    """Command for systemd-run: argv only, no shell, every value in its own element."""
    return [
        "systemd-run", "--user", "--collect", "--no-block",
        f"--unit=wolfbet-update-{int(time.time())}",
        "--on-active=2s",
        f"--working-directory={repo_dir}",
        f"--property=StandardOutput=append:{log_file}",
        f"--property=StandardError=append:{log_file}",
        "--", ctl_path, "update",
    ]


def _prepare_update_log() -> str:
    """Create the update log under the bot data dir with owner-only permissions."""
    os.makedirs(DATA_DIR, exist_ok=True)
    log_file = os.path.join(DATA_DIR, UPDATE_LOG_NAME)
    fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    return log_file


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pull latest code + restart the bot (owner only)."""
    user_id = update.effective_user.id
    if not _is_owner(user_id):
        await update.message.reply_text("Owner only.")
        return
    ctl_path = _ctl_path()
    if not ctl_path:
        await update.message.reply_text(f"{CTL_NAME} not found in PATH.")
        return
    if not _validated_path(REPO_DIR, is_dir=True):
        logger.error("cmd_update: BOT_REPO_DIR is not an absolute existing directory: %r", REPO_DIR)
        await update.message.reply_text("Repository directory is not configured correctly.")
        return
    if not shutil.which("systemd-run"):
        await update.message.reply_text("systemd-run not found: update manually via SSH.")
        return

    log_file = _prepare_update_log()
    cmd = _update_argv(ctl_path, REPO_DIR, log_file)
    try:
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True,
                                         text=True, timeout=10)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "no output").strip()[:300]
            logger.error("cmd_update: systemd-run rc=%d stderr=%s stdout=%s",
                         result.returncode, result.stderr, result.stdout)
            await update.message.reply_text(
                f"❌ systemd-run failed (rc={result.returncode}): `{err}`",
                parse_mode="Markdown")
            return
        await update.message.reply_text(
            f"🔄 Update started: bot will restart in ~2s.\n"
            f"Logs: `cat {log_file}`",
            parse_mode="Markdown")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("⏱ systemd-run timed out (>10s).")
    except Exception as e:
        logger.error("cmd_update error: %s", e, exc_info=True)
        msg = f"Failed: `{type(e).__name__}: {e}`" if APP_ENV != "production" else "Update launch failed."
        await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_seed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rotate the WolfBet server seed via /v1/game/seed/refresh."""
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    api_key = config.get("api_key")
    if not api_key:
        await update.message.reply_text(
            "Set your API key first: /setkey <token>", parse_mode="Markdown")
        return
    try:
        engine = BettingEngine(user_id, "", api_key, {})
        await engine._recreate_http()
        ok = await engine._rotate_server_seed()
        await update.message.reply_text(
            "Server seed rotated." if ok else "Seed rotation failed: check /balance / api key.")
    except Exception as e:
        logger.error("cmd_seed error: %s", e, exc_info=True)
        msg = f"Error: `{e}`" if APP_ENV != "production" else "Something went wrong. Please try again."
        try:
            await update.message.reply_text(msg, parse_mode="Markdown")
        except Exception:
            pass


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)

    try:
        strategy_key = config.get("strategy_key") or "2"
        strategy = config.get("strategy") or STRATEGY_NAMES.get(strategy_key, "Martingale")
        multiplier = float(config.get("multiplier") or 2.01)
        wc = round(99.0 / multiplier, 2) if multiplier > 0 else 0

        from .engine import GAME_LABELS
        game = config.get("game") or "dice"
        game_label = GAME_LABELS.get(game, game)

        lines = [
            "*Current Configuration:*",
            f"Currency: `{(config.get('currency') or 'usdt').upper()}`",
            f"Game: `{game_label}`",
            f"Strategy: `{strategy}`",
        ]
        if game in ("dice", "dice-manual"):
            lines.append(f"Rule: `{config.get('rule') or 'over'}`")
        lines.extend([
            f"Multiplier: `{multiplier:.2f}x` ({wc}% win chance)",
            f"Base bet: `{float(config.get('base_bet') or 0.0001):.8f}`",
        ])

        # Strategy-specific fields
        if strategy_key in ("2", "7"):  # Martingale, Delay Martingale
            lines.append(f"Loss mult: `{float(config.get('loss_mult') or 2.0):.2f}x`")
        if strategy_key in ("3", "5"):  # Anti-Martingale, Paroli
            lines.append(f"Win mult: `{float(config.get('win_mult') or 1.0):.2f}x`")
        if strategy_key == "7":  # Delay Martingale
            lines.append(f"Delay threshold: `{int(config.get('delay_martin_threshold') or 3)}`")

        lines.append(f"Bet delay: `{float(config.get('bet_delay') or 0)}s`")
        sdl = config.get("streak_delay_loss")
        sdw = config.get("streak_delay_win")
        sdb = config.get("streak_delay_bets")
        sbl = config.get("streakbet_loss")
        bbp = config.get("basebet_pct")
        if sdl:
            lines.append(f"Streak delay loss: `{sdl}`")
        if sdw:
            lines.append(f"Streak delay win: `{sdw}`")
        if sdb:
            lines.append(f"Streak delay bets: `{sdb}`")
        if sbl:
            lines.append(f"Streak bet loss: `{sbl}`")
        if bbp:
            lines.append(f"Basebet pct: `{float(bbp)*100:.4f}%` of balance")

        # Stop conditions
        stops = []
        if config.get("max_profit"): stops.append(f"Max profit: {float(config['max_profit']):.8f}")
        if config.get("max_loss"):   stops.append(f"Max loss: {float(config['max_loss']):.8f}")
        if config.get("max_bets"):   stops.append(f"Max bets: {config['max_bets']}")
        if config.get("max_wins"):   stops.append(f"Max wins: {config['max_wins']}")
        if config.get("stop_on_balance"): stops.append(f"Min balance: {float(config['stop_on_balance']):.8f}")
        if stops:
            lines.append(f"Stop: `{', '.join(stops)}`")
        else:
            lines.append("Stop conditions: _none_")

        # Profit increment
        pi = config.get("profit_increment")
        pt = config.get("profit_threshold")
        if pi is not None and pt:
            _scope = "run" if config.get("profit_increment_scope") == "run" else "session"
            lines.append(f"Profit increment: `+{float(pi):.8f} every {float(pt):.8f} profit` ({_scope})")

        # Milestones
        ms = []
        mb = config.get("milestone_bets", 100)
        if mb: ms.append(f"{mb} bets")
        mw = config.get("milestone_wins", 0)
        if mw: ms.append(f"{mw} wins")
        ml = config.get("milestone_losses", 0)
        if ml: ms.append(f"{ml} losses")
        mp = config.get("milestone_profit", 0)
        if mp: ms.append(f"{float(mp):.8f} profit")
        lines.append(f"Milestones: `{', '.join(ms) if ms else 'off'}`")

        rec_on = config.get("recurring", False)
        rec_delay = int(config.get("recurring_delay") or 60)
        lines.append(f"Recurring: `{'on (' + str(rec_delay) + 's)' if rec_on else 'off'}`")
        sr = config.get("streak_reset_losses")
        if sr:
            lines.append(f"Streak reset: `after {sr} losses, pause {_streak_reset_delay(config)}s`")

        # Rules (for rule-based strategy)
        if strategy_key == "8":
            rules_text = config.get("custom_rules_text", "")
            rules = load_rules_from_text(rules_text) if rules_text else []
            if rules:
                lines.append(f"\n*Rules ({len(rules)}):*")
                for i, r in enumerate(rules, 1):
                    lines.append(f"  `{i}.` {r.description}")
            else:
                lines.append("Rules: _none configured_")

        api_set = "Yes" if config.get("api_key") else "No"
        lines.append(f"API key: `{api_set}`")
        proxy = config.get("proxy")
        # v2.28.0: shared mask_proxy: strips userinfo + handles port-missing case.
        if proxy:
            lines.append(f"Proxy: `{mask_proxy(proxy)}`")
        else:
            lines.append("Proxy: `off`")
        pipeline = int(config.get("pipeline") or 1)
        if pipeline > 1:
            lines.append(f"Pipeline: `{pipeline}` (auto-bet only)")
        lines.append(f"Tier: `{get_user_tier(user_id)}`")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_config error: %s", e, exc_info=True)
        msg = f"Config error: `{e}`" if APP_ENV != "production" else "Configuration error. Please check your settings."
        await update.message.reply_text(msg, parse_mode="Markdown")


def _finite(value, cast=float):
    """Parse a numeric /set or /tweak value, rejecting nan, inf and fractional integers."""
    number = finite_float("value", value)
    if cast is int and not number.is_integer():
        raise ValueError("value must be a whole number")
    return cast(number)


# ── /set ─────────────────────────────────────────────────
class SettingError(Exception):
    """A /set value was refused; ``text`` is the reply and ``markdown`` its parse mode."""

    def __init__(self, text: str, markdown: bool = False):
        super().__init__(text)
        self.text = text
        self.markdown = markdown


def apply_setting(user_id: int, param: str, value: str) -> tuple:
    """Validate and save one /set parameter for ``user_id``.

    Returns (reply text, markdown). Raises SettingError with the reply when the
    parameter or value is refused, and nothing is saved then. /set and the
    control socket both use it, so both apply exactly the same rules.
    """
    param = param.lower()
    value = value.replace(",", "")
    config = load_user_config(user_id)

    try:
        if param == "currency":
            config["currency"] = value.lower()
        elif param == "proxy":
            # v2.28.0: shared validate_proxy: broader off-value vocabulary,
            # same scheme rules as CLI. Zero user-visible behaviour change on
            # the happy path; raises ValueError on unknown schemes.
            try:
                normalized = validate_proxy(value)
            except ValueError:
                raise SettingError(
                    "Proxy format: `http://user:pass@ip:port` or `socks5://user:pass@ip:port`\n"
                    "Use `/set proxy off` to disable.",
                    markdown=True)
            config["proxy"] = normalized
            if normalized is None:
                save_user_config(user_id, config)
                return "Proxy disabled: using direct connection.", True
        elif param in ("streakdelay_loss", "sdloss"):
            if value.lower() == "off":
                config["streak_delay_loss"] = None
            elif ":" not in value:
                raise SettingError("Format: `/set streakdelay_loss 5:1.0` (every 5 losses → 1s delay)\nUse `off` to disable.", markdown=True)
            else:
                config["streak_delay_loss"] = value
        elif param in ("streakdelay_win", "sdwin"):
            if value.lower() == "off":
                config["streak_delay_win"] = None
            elif ":" not in value:
                raise SettingError("Format: `/set streakdelay_win 10:0.5` (every 10 wins → 0.5s delay)\nUse `off` to disable.", markdown=True)
            else:
                config["streak_delay_win"] = value
        elif param in ("streakdelay_bets", "sdbets"):
            if value.lower() == "off":
                config["streak_delay_bets"] = None
            elif ":" not in value:
                raise SettingError("Format: `/set streakdelay_bets 100:0.5` (every 100 bets → 0.5s delay)\nUse `off` to disable.", markdown=True)
            else:
                config["streak_delay_bets"] = value
        elif param in ("streakbet_loss", "sbloss"):
            if value.lower() == "off":
                config["streakbet_loss"] = None
            elif ":" not in value:
                raise SettingError("Format: `/set streakbet_loss 10:0.5` (every 10 losses → halve bet)\nUse `off` to disable.", markdown=True)
            else:
                config["streakbet_loss"] = value
        elif param in ("basebet_pct", "bbpct"):
            if value.lower() == "off":
                config["basebet_pct"] = None
            else:
                try:
                    val = _finite(value)
                except ValueError:
                    raise SettingError("basebet_pct must be a fraction (e.g. `0.001` = 0.1% of balance)", markdown=True)
                if val < 0 or val > 1:
                    raise SettingError("basebet_pct must be between 0 and 1.")
                config["basebet_pct"] = val
        elif param in ("purgedays", "purge_days"):
            val = _finite(value, int)
            if val < 1 or val > 30:
                raise SettingError("Purge days must be 1-30.")
            config["purge_days"] = val
        elif param == "pipeline":
            val = _finite(value, int)
            if val < 1 or val > 50:
                raise SettingError("Pipeline must be 1-50. Only works with auto-bet games.")
            config["pipeline"] = val
            strategy_key = config.get("strategy_key", "2")
            if val > 1 and strategy_key != "1":
                save_user_config(user_id, config)
                return (
                    f"Set `pipeline` = `{val}`\n\n"
                    f"Pipeline > 1 only works correctly with **Flat** strategy.\n"
                    f"Your current strategy is sequential: pipeline will be forced to 1 at start.\n"
                    f"Use `/set strategy flat` first, then `/set pipeline {val}`.",
                    True,
                )
        elif param == "game":
            from .engine import GAME_LABELS
            g = value.lower()
            if g not in GAME_LABELS:
                raise SettingError(
                    f"Unknown game. Available: {', '.join(GAME_LABELS.keys())}", markdown=True)
            config["game"] = g
        elif param == "strategy":
            name = value.lower().replace("-", "").replace(" ", "").replace("_", "")
            # Short aliases
            _aliases = {"flat": "flatbet", "anti": "antimartingale", "delay": "delaymartingale",
                        "paroli": "paroli3step", "rule": "rulebased", "rules": "rulebased",
                        "dalembert": "dalembert", "wolf": "wolfrider"}
            name = _aliases.get(name, name)
            key = STRATEGY_BY_NAME.get(name)
            if not key:
                if value in STRATEGIES:
                    key = value
                else:
                    raise SettingError(
                        "Unknown strategy. Use /strategies to see options.", markdown=True)
            config["strategy_key"] = key
            config["strategy"] = STRATEGY_NAMES[key]
        elif param == "multiplier":
            mult = _finite(value)
            if mult <= 1.0:
                raise SettingError("Multiplier must be > 1.0")
            config["multiplier"] = mult
            wc = 99.0 / mult
            rule = config.get("rule", "over")
            config["bet_value"] = round(100.0 - wc - 0.01, 2) if rule == "over" else round(wc, 2)
        elif param == "basebet":
            val = _finite(value)
            if val < 0:
                raise SettingError("Base bet must be >= 0.")
            config["base_bet"] = val
        elif param == "rule":
            if value.lower() not in ("over", "under"):
                raise SettingError("Rule must be `over` or `under`", markdown=True)
            config["rule"] = value.lower()
            mult = config.get("multiplier", 2.01)
            wc = 99.0 / mult
            config["bet_value"] = round(100.0 - wc - 0.01, 2) if value.lower() == "over" else round(wc, 2)
        elif param == "lossmult":
            val = _finite(value)
            if val <= 0:
                raise SettingError("Loss multiplier must be > 0.")
            config["loss_mult"] = val
        elif param == "winmult":
            val = _finite(value)
            if val <= 0:
                raise SettingError("Win multiplier must be > 0.")
            config["win_mult"] = val
        elif param == "delay":
            val = _finite(value)
            if val < 0:
                raise SettingError("Delay must be >= 0.")
            config["bet_delay"] = val
        elif param == "maxprofit":
            if value.lower() != "off":
                val = _finite(value)
                if val <= 0:
                    raise SettingError("Max profit must be > 0.")
                config["max_profit"] = val
            else:
                config["max_profit"] = None
        elif param == "maxloss":
            if value.lower() != "off":
                val = _finite(value)
                if val <= 0:
                    raise SettingError("Max loss must be > 0.")
                config["max_loss"] = val
            else:
                config["max_loss"] = None
        elif param == "maxbets":
            if value.lower() != "off":
                val = _finite(value, int)
                if val <= 0:
                    raise SettingError("Max bets must be > 0.")
                config["max_bets"] = val
            else:
                config["max_bets"] = None
        elif param == "maxwins":
            if value.lower() != "off":
                val = _finite(value, int)
                if val <= 0:
                    raise SettingError("Max wins must be > 0.")
                config["max_wins"] = val
            else:
                config["max_wins"] = None
        elif param == "minbalance":
            if value.lower() != "off":
                val = _finite(value)
                if val < 0:
                    raise SettingError("Min balance must be >= 0.")
                config["stop_on_balance"] = val
            else:
                config["stop_on_balance"] = None
        elif param == "delaythreshold":
            config["delay_martin_threshold"] = _finite(value, int)
        elif param in ("milestonebets", "msbets"):
            config["milestone_bets"] = _finite(value, int)
        elif param in ("milestonewins", "mswins"):
            config["milestone_wins"] = _finite(value, int)
        elif param in ("milestonelosses", "mslosses"):
            config["milestone_losses"] = _finite(value, int)
        elif param in ("milestoneprofit", "msprofit"):
            config["milestone_profit"] = _finite(value)
        elif param in ("profitthreshold", "pt"):
            # v1.21.0: off / none / 0 all mean off (ends a run total too).
            val = None if value.lower() in ("off", "none") else _finite(value)
            if val is not None and val < 0:
                raise SettingError("Profit threshold must be > 0 (or 'off').")
            config["profit_threshold"] = val or None
        elif param in ("profitincrement", "pi"):
            val = None if value.lower() in ("off", "none") else _finite(value)
            if val is not None and val < 0:
                raise SettingError("Profit increment must be > 0 (or 'off').")
            config["profit_increment"] = val or None
        elif param in ("piscope", "pscope", "profitincrementscope"):
            v = value.lower()
            if v not in ("session", "run"):
                raise SettingError("piscope must be `session` or `run`.", markdown=True)
            config["profit_increment_scope"] = v
        elif param == "recurring":
            if value.lower() in ("on", "true", "yes", "1"):
                config["recurring"] = True
            elif value.lower() in ("off", "false", "no", "0"):
                config["recurring"] = False
            else:
                raise SettingError("Use: `/set recurring on` or `/set recurring off`", markdown=True)
        elif param in ("recurringdelay", "recdelay"):
            val = _finite(value, int)
            if val < 5:
                raise SettingError("Recurring delay must be >= 5 seconds.")
            config["recurring_delay"] = val
        elif param in ("streakreset", "srloss"):
            if value.lower() in ("off", "none", "0"):
                config["streak_reset_losses"] = None
            else:
                val = _finite(value, int)
                if val < 1:
                    raise SettingError("Streak reset must be >= 1 losses (or 'off').")
                config["streak_reset_losses"] = val
        elif param in ("streakresetdelay", "srdelay"):
            val = _finite(value, int)
            if val < 5 or val > 86400:
                raise SettingError("Streak reset delay must be 5-86400 seconds.")
            config["streak_reset_delay"] = val
        else:
            raise SettingError(f"Unknown parameter: `{param}`\nSee /help", markdown=True)

        save_user_config(user_id, config)
        return f"Set `{param}` = `{value}`", True
    except ValueError:
        raise SettingError(f"Invalid value for `{param}`: `{value}`", markdown=True) from None


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /set <param> <value>\nSee /help for parameters.",
            parse_mode="Markdown")
        return

    try:
        text, markdown = apply_setting(user_id, context.args[0], " ".join(context.args[1:]))
    except SettingError as e:
        text, markdown = e.text, e.markdown
    if markdown:
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text)


# ── /strategies ──────────────────────────────────────────
async def cmd_strategies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["*Available Strategies:*\n"]
    for k, (name, desc) in STRATEGIES.items():
        lines.append(f"`{k}` *{name}*: {desc}")
    lines.append("\nSet with: /set strategy <name>")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /bet ─────────────────────────────────────────────────
class StartError(Exception):
    """A session could not start; ``text`` is the reason shown to the user."""

    def __init__(self, text: str, connected: bool = False):
        super().__init__(text)
        self.text = text
        self.connected = connected


async def start_session(user_id: int, chat_id: int, app, on_connecting=None) -> tuple:
    """Start a new betting session for ``user_id`` from the saved config.

    Returns (slot, engine, config). Notifications go to ``chat_id``. ``on_connecting``
    is awaited just before contacting WolfBet. Raises StartError with the user-facing
    reason; ``connected`` tells whether the failure came after that point. /bet and
    the control socket both start sessions here.
    """
    config = load_user_config(user_id)
    if not config.get("api_key"):
        raise StartError("Set your API key first: /setkey <token>")

    # Apply tier rate limit
    tier = get_user_tier(user_id)
    min_delay = TIERS.get(tier, 1.0)
    config_delay = float(config.get("bet_delay") or 0)
    config["bet_delay"] = max(config_delay, min_delay)

    db_path = user_db_path(user_id)
    try:
        engine = BettingEngine(user_id, db_path, config["api_key"], config)
    except Exception as e:
        logger.error("cmd_start config error: %s", e, exc_info=True)
        raise StartError(
            f"Config error: `{e}`" if APP_ENV != "production"
            else "Configuration error. Please check your settings.") from None

    slot = _alloc_slot(user_id)

    def _make_on_stop(cid, uid, s):
        def on_stop(reason):
            t = asyncio.create_task(_notify_stop(cid, uid, s, reason, app))
            t.add_done_callback(_log_task_exception)
        return on_stop

    def _make_on_milestone(cid):
        def on_milestone(data):
            t = asyncio.create_task(_notify_milestone(cid, data, app))
            t.add_done_callback(_log_task_exception)
        return on_milestone

    def _make_on_error(cid):
        def on_error(msg):
            t = asyncio.create_task(_notify_error(cid, msg, app))
            t.add_done_callback(_log_task_exception)
        return on_error

    engine.on_stop = _make_on_stop(chat_id, user_id, slot)
    engine.on_milestone = _make_on_milestone(chat_id)
    engine.on_error = _make_on_error(chat_id)

    if on_connecting is not None:
        await on_connecting()

    if not await engine.start():
        detail = f": {engine.last_error}" if APP_ENV != "production" else ""
        raise StartError(f"Failed to start{detail}. Check /config and try again.", connected=True)

    active_engines.setdefault(user_id, {})[slot] = engine
    _engine_chat_ids[user_id] = chat_id

    # Always register a recurring snapshot per session so the user can toggle
    # /set recurring on/off mid-flight and have it apply to every running session.
    # Whether to actually restart is decided at stop time by re-reading the live config.
    recurring_state.setdefault(user_id, {})[slot] = {
        "delay": int(config.get("recurring_delay") or 60),
        "chat_id": chat_id,
        "config": dict(config),
        "timer_task": None,
        "app": app,
    }
    return slot, engine, config


async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = None

    async def connecting():
        nonlocal msg
        msg = await update.message.reply_text("Connecting to WolfBet...")

    try:
        slot, engine, config = await start_session(
            user_id, update.effective_chat.id, context.application, on_connecting=connecting)
    except StartError as e:
        if e.connected:
            await msg.edit_text(e.text)
        else:
            await update.message.reply_text(e.text, parse_mode="Markdown")
        return

    from .engine import GAME_LABELS, GAME_ENDPOINTS
    game_label = GAME_LABELS.get(engine.game, engine.game)
    ep = GAME_ENDPOINTS.get(engine.game, ("", 1, 480, None))

    rate_warn = ""
    if len(_get_running(user_id)) > 1:
        rate_warn = f"\n\n⚠️ WolfBet rate limit: {ep[2]}/min. Multiple sessions share this."

    slot_info = f" (slot {slot})" if len(_get_running(user_id)) > 1 else ""
    rec_info = f"\nRecurring: `{int(config.get('recurring_delay') or 60)}s`" if config.get("recurring") else ""
    from .formatter import format_full_config
    cfg_lines = "\n".join(format_full_config(engine.get_status()))
    try:
        await msg.edit_text(
            f"Session #{engine.session_id}{slot_info} started!\n\n"
            f"\u2699\ufe0f *Config*\n{cfg_lines}\n\n"
            f"\U0001f4b0 Balance: `{engine.current_balance:.8f} {engine.currency.upper()}`"
            f"{rec_info}\n\n"
            f"Use /status to check progress.{rate_warn}",
            parse_mode="Markdown",
        )
    except Exception:
        # Telegram timeout: session is already running, just couldn't send confirmation
        logger.warning("User %d: Session started but Telegram confirmation timed out", user_id)


async def _safe_send(app, chat_id, text, **kwargs):
    """Engine-driven send that swallows flood/network errors to avoid unraised Task exceptions.

    On a Markdown parse error, retries once as plain text so the user always
    sees the notification: preventing silent /status-style outages.
    """
    from telegram.error import RetryAfter, NetworkError, TimedOut
    try:
        await app.bot.send_message(chat_id, text, **kwargs)
    except RetryAfter as e:
        logger.warning("Telegram flood: dropping notification (retry in %ss)", e.retry_after)
    except (NetworkError, TimedOut) as e:
        logger.warning("Telegram network: dropping notification: %s", e)
    except BadRequest as e:
        msg = str(e).lower()
        if "parse" in msg or "entity" in msg:
            logger.warning("Markdown parse failed (%s); resending notification as plain text", e)
            kwargs.pop("parse_mode", None)
            try:
                await app.bot.send_message(chat_id, text, **kwargs)
                return
            except (RetryAfter, NetworkError, TimedOut, BadRequest) as e2:
                logger.warning("Plain-text retry also failed: %s", e2)
                return
        logger.warning("Telegram rejected notification: %s", e)


async def safe_reply_markdown(reply_target, text, **kwargs):
    """Reply with parse_mode=Markdown; on parse failure, retry as plain text.

    Telegram silently rejects messages with unbalanced Markdown entities;
    /status looks dead from the user's side. This wrapper guarantees the
    user always sees the content (formatting falls back to literal *foo*).
    """
    try:
        return await reply_target.reply_text(text, parse_mode="Markdown", **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if "parse" in msg or "entity" in msg:
            logger.warning("Markdown parse failed (%s); resending reply as plain text", e)
            kwargs.pop("parse_mode", None)
            return await reply_target.reply_text(text, **kwargs)
        raise


def _log_task_exception(task: asyncio.Task):
    """Log exceptions from fire-and-forget tasks so they aren't silently swallowed."""
    if not task.cancelled() and task.exception():
        logger.error("Unhandled task exception: %s", task.exception(), exc_info=task.exception())


async def _notify_stop(chat_id: int, user_id: int, slot: int, reason: str, app):
    user_engines = active_engines.get(user_id, {})
    engine = user_engines.pop(slot, None)
    if not user_engines:
        active_engines.pop(user_id, None)
        _engine_chat_ids.pop(user_id, None)
    if not engine:
        return

    # Throttle stop notifications for fast recurring cycles to avoid Telegram flood bans.
    # Only throttle when recurring is actually on AND it's a non-loss condition stop.
    user_rec = recurring_state.get(user_id, {})
    try:
        is_recurring_active = bool(load_user_config(user_id).get("recurring"))
    except Exception:
        is_recurring_active = False
    is_notable = (
        not is_recurring_active
        or reason == "Stopped by user"
        or "insufficient" in reason.lower()
        or engine.profit < 0
    )
    send_stop = is_notable or _throttle_recurring(user_id, "stop")

    if send_stop:
        try:
            s = engine.get_status()
            text = format_stop(s, reason)
            await _safe_send(app, chat_id, text, parse_mode="Markdown")
        except Exception as exc:
            logger.error("_notify_stop formatting error (slot %d): %s", slot, exc, exc_info=True)
            await _safe_send(app, chat_id, f"Session #{engine.session_id} stopped: {reason}")
    else:
        logger.info("User %d slot %d: stop notification throttled (profit %.8f, reason: %s)",
                    user_id, slot, engine.profit, reason)

    # Recurring bet: schedule restart if condition stop (not manual) AND live config says recurring is on.
    # Re-read the saved config at stop time so toggling /set recurring on/off applies to every session.
    rec = user_rec.get(slot)
    # Streak reset (first win after N+ losses, profit >= 0) is its own restart
    # rule with its own delay: independent of the recurring flag.
    is_streak_reset = reason.startswith(STREAK_RESET_REASON)
    try:
        live_config = load_user_config(user_id)
        recurring_on = bool(live_config.get("recurring"))
        live_delay = int(live_config.get("recurring_delay") or rec["delay"]) if rec else 60
        if is_streak_reset:
            recurring_on = bool(live_config.get("streak_reset_losses"))
            live_delay = _streak_reset_delay(live_config)
    except Exception as e:
        logger.warning("User %d: failed to reload config at stop, falling back to snapshot: %s", user_id, e)
        live_config = None
        recurring_on = bool(rec)
        live_delay = rec["delay"] if rec else 60
        if is_streak_reset and rec:
            live_delay = _streak_reset_delay(rec["config"])

    if rec and recurring_on and reason != "Stopped by user" and "insufficient" not in reason.lower():
        config = rec["config"]
        if live_config is not None:
            # Carry live /set changes to the streak-reset settings into the next cycle.
            for k in ("streak_reset_losses", "streak_reset_delay"):
                config[k] = live_config.get(k)
        # Carry forward the current base_bet (profit increment may have raised it)
        config["base_bet"] = engine.base_bet
        # v1.21.0: run scope: base before run bumps, so they never stack twice.
        config["_run_base_origin"] = getattr(engine, "_run_base_origin", None)
        if live_config is not None:
            for k in ("profit_increment", "profit_threshold", "profit_increment_scope"):
                config[k] = live_config.get(k)
        task = asyncio.create_task(
            _recurring_restart(chat_id, user_id, slot, live_delay, config, app))
        task.add_done_callback(_log_task_exception)
        rec["timer_task"] = task
    elif rec and not recurring_on:
        # User turned recurring off: clean up the snapshot so it doesn't linger
        recurring_state.get(user_id, {}).pop(slot, None)


# ── /runreset (v1.21.0) ──────────────────────────────────
async def cmd_runreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start a new run total for profit_increment_scope=run."""
    user_id = update.effective_user.id
    if _get_running(user_id) or recurring_state.get(user_id):
        await _reply(update, "Stop your sessions first (`/stop recurring`), then /runreset: "
                             "running sessions hold the run's raised base bet.",
                     parse_mode="Markdown")
        return
    path = os.path.join(os.path.dirname(user_db_path(user_id)), "run.json")
    try:
        os.remove(path)
        msg = "Run total reset. The next /bet starts a new run (base bet from /config)."
    except OSError:
        msg = "No run total to reset."
    await _reply(update, msg)


def _streak_reset_delay(config: dict) -> int:
    """Pause before a streak-reset restart (default 600s, floor 5s like recurring_delay)."""
    v = config.get("streak_reset_delay")
    return max(5, int(v)) if v is not None else 600


async def _recurring_restart(chat_id: int, user_id: int, old_slot: int,
                             delay: int, config: dict, app):
    await asyncio.sleep(delay)

    user_rec = recurring_state.get(user_id, {})
    if old_slot not in user_rec:
        return  # cancelled while waiting

    tier = get_user_tier(user_id)
    min_delay = TIERS.get(tier, 1.0)
    config_delay = float(config.get("bet_delay") or 0)
    config["bet_delay"] = max(config_delay, min_delay)

    db_path = user_db_path(user_id)
    try:
        engine = BettingEngine(user_id, db_path, config["api_key"], config)
    except Exception as e:
        logger.error("Recurring restart config error: %s", e, exc_info=True)
        await _safe_send(app, chat_id, "Recurring restart failed: config error.")
        user_rec.pop(old_slot, None)
        return

    # Reuse old_slot if free (keeps /monitor + callbacks bound to same slot)
    existing = active_engines.get(user_id, {})
    slot = old_slot if old_slot not in existing else _alloc_slot(user_id)

    def _make_on_stop(cid, uid, s):
        def on_stop(reason):
            t = asyncio.create_task(_notify_stop(cid, uid, s, reason, app))
            t.add_done_callback(_log_task_exception)
        return on_stop

    def _make_on_milestone(cid):
        def on_milestone(data):
            t = asyncio.create_task(_notify_milestone(cid, data, app))
            t.add_done_callback(_log_task_exception)
        return on_milestone

    def _make_on_error(cid):
        def on_error(msg):
            t = asyncio.create_task(_notify_error(cid, msg, app))
            t.add_done_callback(_log_task_exception)
        return on_error

    engine.on_stop = _make_on_stop(chat_id, user_id, slot)
    engine.on_milestone = _make_on_milestone(chat_id)
    engine.on_error = _make_on_error(chat_id)

    if not await engine.start():
        err = engine.last_error or "unknown error"
        logger.warning("Recurring restart failed for user %d slot %d: %s: retrying in %ds",
                       user_id, old_slot, err, delay)
        await _safe_send(app, chat_id,
                         f"⚠️ Recurring restart failed: {err}\nRetrying in {delay}s...")
        # Re-schedule retry: keep recurring state alive (don't pop)
        rec = user_rec.get(old_slot)
        if rec:
            retry_task = asyncio.create_task(
                _recurring_restart(chat_id, user_id, old_slot, delay, config, app))
            retry_task.add_done_callback(_log_task_exception)
            rec["timer_task"] = retry_task
        return

    active_engines.setdefault(user_id, {})[slot] = engine
    _engine_chat_ids[user_id] = chat_id

    # Move recurring state to new slot
    rec_data = user_rec.pop(old_slot, None)
    if rec_data:
        rec_data["timer_task"] = None
        user_rec[slot] = rec_data
        recurring_state.setdefault(user_id, {})[slot] = rec_data

    if _throttle_recurring(user_id, "start"):
        await _safe_send(
            app, chat_id,
            f"Recurring #{engine.session_id} · base `{engine.base_bet:.8f}` · bal `{engine.current_balance:.8f} {engine.currency.upper()}`",
            parse_mode="Markdown",
        )


async def _notify_milestone(chat_id: int, data: dict, app):
    text = format_milestone(data)
    await _safe_send(app, chat_id, text, parse_mode="Markdown")


async def _notify_error(chat_id: int, message: str, app):
    await _safe_send(app, chat_id, f"⚠️ {message}", parse_mode="Markdown")


def _cancel_recurring_for_slot(user_id: int, slot: int):
    user_rec = recurring_state.get(user_id, {})
    rec = user_rec.pop(slot, None)
    if rec:
        task = rec.get("timer_task")
        if task and not task.done():
            task.cancel()
    if not user_rec:
        recurring_state.pop(user_id, None)


def _cancel_recurring_for_user(user_id: int):
    user_rec = recurring_state.pop(user_id, {})
    for rec in user_rec.values():
        task = rec.get("timer_task")
        if task and not task.done():
            task.cancel()


# ── /stop ────────────────────────────────────────────────
async def _reply(update: Update, text: str, **kwargs):
    """Reply via message or callback query, whichever is available."""
    if update.message:
        await update.message.reply_text(text, **kwargs)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, **kwargs)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    running = _get_running(user_id)

    if context.args and context.args[0].lower() == "recurring":
        # Cancel all recurring bets and stop all sessions
        user_rec = recurring_state.pop(user_id, {})
        cancelled = 0
        for rec in user_rec.values():
            task = rec.get("timer_task")
            if task and not task.done():
                task.cancel()
                cancelled += 1
        if running:
            for engine in running.values():
                engine.stop_reason = "Stopped by user"
                engine.stop()
        config = load_user_config(user_id)
        config["recurring"] = False
        save_user_config(user_id, config)
        parts = []
        if cancelled:
            parts.append(f"Cancelled {cancelled} pending restart(s)")
        if running:
            parts.append(f"Stopping {len(running)} session(s)")
        parts.append("Recurring mode disabled")
        await _reply(update, ". ".join(parts) + ".")
        return

    # /stop all: stop all sessions
    if context.args and context.args[0].lower() == "all":
        _cancel_recurring_for_user(user_id)
        if running:
            for engine in running.values():
                engine.stop_reason = "Stopped by user"
                engine.stop()
            await _reply(update, f"Stopping {len(running)} session(s)...")
            return

    if running:
        slot, engine, err = _resolve_engine(user_id, context.args or [])
        if err:
            await _reply(update, err + "\nUse `/stop all` to stop all.", parse_mode="Markdown")
            return
        stop_slot(user_id, slot, engine)
        return

    # Clean up zombie sessions (no active engine but DB says running)
    db_path = user_db_path(user_id)
    if os.path.exists(db_path):
        row = await fetch_one(db_path, "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL")
        zombies = row[0]
        if zombies:
            await execute(
                db_path,
                "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL",
                (datetime.now().isoformat(),),
            )
            await _reply(update, f"Cleaned up {zombies} stale session(s).")
            return
    await _reply(update, "No session running.")


# ── /pause ───────────────────────────────────────────────
# ── /tweak: mutate running session ─────────────────────
async def cmd_tweak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Change settings on a running session without stopping it.
    Usage: /tweak <param> <value> [slot]
    Examples: /tweak pipeline 20, /tweak basebet 0.00000002, /tweak delay 0.1
    """
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "*Tweak a running session:*\n\n"
            "*Instant (no restart):*\n"
            "`/tweak pipeline 20`: concurrent requests\n"
            "`/tweak delay 0.1`: bet delay\n"
            "`/tweak maxwins 5000`: stop at wins\n"
            "`/tweak maxbets 10000`: stop at bets\n"
            "`/tweak maxprofit 0.001`: stop at profit\n"
            "`/tweak maxloss 0.001`: stop at loss\n"
            "`/tweak minbalance 1.0`: stop below balance\n"
            "`/tweak srloss 20`: restart on win after 20 losses\n"
            "`/tweak milestonewins 1000`: milestone interval\n"
            "`/tweak sdloss 5:1.0`: delay every 5 losses\n"
            "`/tweak sdwin 10:0.5`: delay every 10 wins\n"
            "`/tweak sdbets 100:0.5`: delay every 100 bets\n\n"
            "*Restarts auto-bet:*\n"
            "`/tweak basebet 0.00000002`\n"
            "`/tweak multiplier 2.5`\n"
            "`/tweak lossmult 2.01`\n"
            "`/tweak winmult 1.5`\n\n"
            "Use `off` to disable: `/tweak maxwins off`\n"
            "Add slot: `/tweak maxwins 5000 2`",
            parse_mode="Markdown")
        return

    param = context.args[0].lower()
    value = context.args[1]
    slot_args = context.args[2:] if len(context.args) > 2 else []

    slot, engine, err = _resolve_engine(user_id, slot_args)
    if err:
        await _reply(update, err)
        return

    off = value.lower() in ("off", "none", "0", "disable")
    changes = {}
    try:
        if param == "pipeline":
            val = _finite(value, int)
            if val < 1 or val > 50:
                await update.message.reply_text("Pipeline must be 1-50.")
                return
            changes["pipeline"] = val
        elif param in ("basebet", "base_bet"):
            changes["base_bet"] = _finite(value)
        elif param == "delay":
            changes["bet_delay"] = _finite(value)
        elif param in ("sdloss", "streakdelay_loss"):
            changes["streak_delay_loss"] = None if off else value
        elif param in ("sdwin", "streakdelay_win"):
            changes["streak_delay_win"] = None if off else value
        elif param in ("sdbets", "streakdelay_bets"):
            changes["streak_delay_bets"] = None if off else value
        elif param in ("sbloss", "streakbet_loss"):
            changes["streakbet_loss"] = None if off else value
        elif param in ("bbpct", "basebet_pct"):
            changes["basebet_pct"] = None if off else _finite(value)
        elif param == "multiplier":
            changes["multiplier"] = _finite(value)
        elif param in ("lossmult", "loss_mult"):
            changes["loss_mult"] = _finite(value)
        elif param in ("winmult", "win_mult"):
            changes["win_mult"] = _finite(value)
        elif param == "maxprofit":
            changes["max_profit"] = None if off else _finite(value)
        elif param == "maxloss":
            changes["max_loss"] = None if off else _finite(value)
        elif param == "maxbets":
            changes["max_bets"] = None if off else _finite(value, int)
        elif param == "maxwins":
            changes["max_wins"] = None if off else _finite(value, int)
        elif param == "minbalance":
            changes["stop_on_balance"] = None if off else _finite(value)
        elif param in ("streakreset", "srloss"):
            val = None if off else _finite(value, int)
            if val is not None and val < 1:
                raise ValueError(value)
            changes["streak_reset_losses"] = val
        elif param in ("milestonebets", "msbets"):
            changes["milestone_bets"] = _finite(value, int)
        elif param in ("milestonewins", "mswins"):
            changes["milestone_wins"] = _finite(value, int)
        elif param in ("profitincrement", "pi"):
            changes["profit_increment"] = None if off or _finite(value) <= 0 else _finite(value)
        elif param in ("profitthreshold", "pt"):
            changes["profit_threshold"] = None if off or _finite(value) <= 0 else _finite(value)
        else:
            await update.message.reply_text(f"Unknown tweak param: `{param}`\nUse `/tweak` for options.", parse_mode="Markdown")
            return
    except ValueError:
        await update.message.reply_text(f"Invalid value: `{value}`", parse_mode="Markdown")
        return

    result = await engine.mutate(changes)
    await update.message.reply_text(f"Tweaked slot {slot}: {result}", parse_mode="Markdown")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # /pause all
    if context.args and context.args[0].lower() == "all":
        running = _get_running(user_id)
        for e in running.values():
            e.pause()
        await _reply(update, f"Paused {len(running)} session(s)." if running else "No sessions running.")
        return
    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await _reply(update, err)
        return
    engine.pause()
    await _reply(update, f"Session (slot {slot}) paused. /resume to continue.", parse_mode="Markdown")


# ── /resume ──────────────────────────────────────────────
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # /resume all
    if context.args and context.args[0].lower() == "all":
        running = _get_running(user_id)
        for e in running.values():
            e.resume()
        await _reply(update, f"Resumed {len(running)} session(s)." if running else "No sessions running.")
        return
    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await _reply(update, err)
        return
    engine.resume()
    await _reply(update, f"Session (slot {slot}) resumed.")


# ── /status ──────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    running = _get_running(user_id)

    if not running:
        await update.message.reply_text(
            "No session running. Start one with /bet", parse_mode="Markdown")
        return

    # Multiple sessions and no slot specified: show summary
    if len(running) > 1 and not context.args:
        lines = [f"*{len(running)} Active Sessions:*\n"]
        for slot, engine in sorted(running.items()):
            s = engine.get_status()
            p = s["profit"]
            ps = "+" if p >= 0 else ""
            lines.append(
                f"*#{slot}*  `{s['strategy']}`  `{ps}{p:.8f}`  ({s['bets']} bets)")
        lines.append(f"\nUse `/status <slot>` for detail.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await update.message.reply_text(err)
        return

    text = format_status(engine.get_status())
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Refresh", callback_data=f"refresh_status:{slot}"),
        InlineKeyboardButton("Stop", callback_data=f"stop_session:{slot}"),
    ]])
    await safe_reply_markdown(update.message, text, reply_markup=keyboard)


# ── /monitor ─────────────────────────────────────────────
async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await update.message.reply_text(err)
        return

    interval = 5
    # Parse interval from args (skip slot number if present)
    for a in (context.args or []):
        try:
            v = int(a)
            if v != slot:
                interval = max(3, min(60, v))
        except ValueError:
            pass

    user_monitors = active_monitors.get(user_id, {})
    old = user_monitors.pop(slot, None)
    if old and not old.done():
        old.cancel()

    text = format_status(engine.get_status())
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Auto {interval}s", callback_data="noop"),
        InlineKeyboardButton("Stop Monitor", callback_data=f"stop_monitor:{slot}"),
        InlineKeyboardButton("Stop Session", callback_data=f"stop_session:{slot}"),
    ]])
    msg = await safe_reply_markdown(update.message, text, reply_markup=keyboard)

    task = asyncio.create_task(
        _monitor_loop(msg, user_id, slot, interval))
    active_monitors.setdefault(user_id, {})[slot] = task


async def _monitor_loop(msg, user_id: int, slot: int, interval: int):
    """Periodically edit the status message with fresh data."""
    try:
        while True:
            await asyncio.sleep(interval)
            engine = active_engines.get(user_id, {}).get(slot)
            if not engine or not engine.running:
                # Check if a recurring restart is pending for this slot
                user_rec = recurring_state.get(user_id, {})
                rec = user_rec.get(slot)
                if rec:
                    timer = rec.get("timer_task")
                    if timer and not timer.done():
                        # Keep monitor alive while waiting for recurring restart
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton(f"Auto {interval}s", callback_data="noop"),
                            InlineKeyboardButton("Stop Monitor", callback_data=f"stop_monitor:{slot}"),
                            InlineKeyboardButton("Stop Session", callback_data=f"stop_session:{slot}"),
                        ]])
                        try:
                            await msg.edit_text(
                                "⏳ Session ended. Recurring restart pending...",
                                reply_markup=keyboard)
                        except Exception:
                            pass
                        continue  # keep looping: new engine will appear after restart
                try:
                    await msg.edit_text(
                        "Session ended. Monitor stopped.",
                        reply_markup=None)
                except Exception:
                    pass
                break

            text = format_status(engine.get_status())
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"Auto {interval}s", callback_data="noop"),
                InlineKeyboardButton("Stop Monitor", callback_data=f"stop_monitor:{slot}"),
                InlineKeyboardButton("Stop Session", callback_data=f"stop_session:{slot}"),
            ]])
            try:
                await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
            except BadRequest as e:
                m = str(e).lower()
                if "parse" in m or "entity" in m:
                    try:
                        await msg.edit_text(text, reply_markup=keyboard)
                    except Exception:
                        pass
            except Exception:
                pass
    except asyncio.CancelledError:
        pass
    finally:
        user_monitors = active_monitors.get(user_id, {})
        user_monitors.pop(slot, None)
        if not user_monitors:
            active_monitors.pop(user_id, None)


# ── Inline button callbacks ─────────────────────────────
def _parse_callback_slot(data: str) -> tuple:
    """Parse 'action:slot' from callback data. Returns (action, slot)."""
    if ":" in data:
        action, slot_str = data.rsplit(":", 1)
        try:
            return action, int(slot_str)
        except ValueError:
            pass
    return data, None


# ── /web: generate dashboard login link ─────────────────
NOT_SET_UP = "The web dashboard isn't set up yet."


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from .config import DASHBOARD_SECRET, DASHBOARD_URL
    if not (DASHBOARD_URL and DASHBOARD_SECRET):
        logger.warning("/web: set WOLFBET_DASHBOARD_URL and WOLFBET_TG_DASHBOARD_SECRET to send sign-in links")
        await update.message.reply_text(NOT_SET_UP)
        return
    problem = dashboard_link.secret_problem(DASHBOARD_SECRET)
    if problem is not None:
        logger.warning("/web: WOLFBET_TG_DASHBOARD_SECRET %s; no sign-in link sent", problem)
        await update.message.reply_text(NOT_SET_UP)
        return
    link = dashboard_link.login_url(DASHBOARD_URL, DASHBOARD_SECRET, update.effective_user.id)
    await update.message.reply_text(
        f"🌐 *Web Dashboard*\n\n"
        f"[Open Dashboard]({link})\n\n"
        f"_Link expires in 5 minutes and works once._",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "lanes_refresh":
        await _lanes_refresh(query)
        return
    if (query.data or "").startswith(("laneadd:", "lanern:")):
        await _named_lane_callback(query)
        return
    if (query.data or "").startswith(("lane:", "lanego:")):
        await _lane_callback(query)
        return
    await query.answer()
    user_id = query.from_user.id

    if query.data == "noop":
        return

    action, slot = _parse_callback_slot(query.data)

    # Resolve engine: use slot from callback, or auto-resolve single session
    def _get_engine():
        if slot is not None:
            return slot, active_engines.get(user_id, {}).get(slot)
        # Backwards compat: no slot encoded, auto-resolve
        running = _get_running(user_id)
        if len(running) == 1:
            s, e = next(iter(running.items()))
            return s, e
        return None, None

    if action == "refresh_status":
        s, engine = _get_engine()
        if not engine or not engine.running:
            # Keep buttons alive if a recurring restart is pending
            user_rec = recurring_state.get(user_id, {})
            rec = user_rec.get(s) if s is not None else None
            timer = rec.get("timer_task") if rec else None
            if timer and not timer.done():
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Refresh", callback_data=f"refresh_status:{s}"),
                    InlineKeyboardButton("Stop", callback_data=f"stop_session:{s}"),
                ]])
                try:
                    await query.edit_message_text(
                        "⏳ Session ended. Recurring restart pending...",
                        reply_markup=keyboard)
                except Exception:
                    pass
            else:
                try:
                    await query.edit_message_text("Session ended.")
                except Exception:
                    pass
            return
        text = format_status(engine.get_status())
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Refresh", callback_data=f"refresh_status:{s}"),
            InlineKeyboardButton("Stop", callback_data=f"stop_session:{s}"),
        ]])
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except BadRequest as e:
            m = str(e).lower()
            if "parse" in m or "entity" in m:
                try:
                    await query.edit_message_text(text, reply_markup=keyboard)
                except Exception:
                    pass
        except Exception:
            pass

    elif action == "stop_monitor":
        s, engine = _get_engine()
        user_mons = active_monitors.get(user_id, {})
        task = user_mons.pop(s, None) if s else None
        if task and not task.done():
            task.cancel()
        if engine and engine.running:
            text = format_status(engine.get_status())
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Refresh", callback_data=f"refresh_status:{s}"),
                InlineKeyboardButton("Monitor", callback_data=f"start_monitor_5:{s}"),
                InlineKeyboardButton("Stop", callback_data=f"stop_session:{s}"),
            ]])
            try:
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
            except BadRequest as e:
                m = str(e).lower()
                if "parse" in m or "entity" in m:
                    try:
                        await query.edit_message_text(text, reply_markup=keyboard)
                    except Exception:
                        pass
            except Exception:
                pass
        else:
            await query.edit_message_text("Monitor stopped. Session ended.")

    elif action.startswith("start_monitor_"):
        interval = int(action.split("_")[-1])
        s, engine = _get_engine()
        if not engine or not engine.running:
            await query.edit_message_text("Session ended.")
            return
        user_mons = active_monitors.get(user_id, {})
        old = user_mons.pop(s, None) if s else None
        if old and not old.done():
            old.cancel()
        text = format_status(engine.get_status())
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"Auto {interval}s", callback_data="noop"),
            InlineKeyboardButton("Stop Monitor", callback_data=f"stop_monitor:{s}"),
            InlineKeyboardButton("Stop Session", callback_data=f"stop_session:{s}"),
        ]])
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except BadRequest as e:
            m = str(e).lower()
            if "parse" in m or "entity" in m:
                try:
                    await query.edit_message_text(text, reply_markup=keyboard)
                except Exception:
                    pass
        except Exception:
            pass
        task = asyncio.create_task(
            _monitor_loop(query.message, user_id, s, interval))
        active_monitors.setdefault(user_id, {})[s] = task

    elif action == "stop_session":
        s, engine = _get_engine()
        user_mons = active_monitors.get(user_id, {})
        task = user_mons.pop(s, None) if s else None
        if task and not task.done():
            task.cancel()
        if engine and engine.running:
            engine.stop_reason = "Stopped by user"
            engine.stop()
        await query.edit_message_text("Stopping session...")


# ── /diagnose ────────────────────────────────────────────
async def cmd_diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v1.18.0: mirrors CLI wolfctl diagnose: top bottleneck + advice."""
    user_id = update.effective_user.id
    running = _get_running(user_id)
    if not running:
        await update.message.reply_text(
            "No session running. Use /bet to start one.", parse_mode="Markdown")
        return
    slot, engine, err = _resolve_engine(user_id, context.args or [])
    if err:
        await update.message.reply_text(err)
        return

    st = engine.get_status()
    api_p50 = float(st.get("api_p50_ms") or 0)
    overhead = float(st.get("overhead_ms") or 0)
    delay_ms = float(engine.bet_delay or 0) * 1000
    bps = float(st.get("bets_per_second") or 0)
    cycle_ms = api_p50 + overhead + delay_ms

    blockers = [("Network / API latency", api_p50),
                ("CLI overhead",           overhead),
                ("User-configured delay",  delay_ms)]
    top_name, top_val = max(blockers, key=lambda x: x[1])

    advice = []
    if top_name == "Network / API latency" and api_p50 >= 100:
        advice.append("• Try a proxy closer to wolfbet.com or a faster VPS.")
    elif top_name == "User-configured delay" and delay_ms > 10:
        advice.append(f"• You have bet_delay={engine.bet_delay:.3f}s. "
                      "Drop it with `/tweak delay=0`.")
    elif top_name == "CLI overhead" and overhead > 30:
        advice.append("• CLI overhead high. Check disk I/O + CPU on host.")
    if engine.strategy_key == "1" and delay_ms == 0:
        advice.append("• Strategy is Flat with no delay: CLI has a pipeline "
                      "option for concurrent bets.")
    if not advice:
        advice.append("• Nothing obvious. Session is running near its ceiling.")

    lines = [
        f"*💡 Diagnostic bottleneck analysis (slot #{slot})*",
        "",
        f"Game: `{st.get('game', '?')}`  ·  Strategy: `{st.get('strategy', '?')}`",
        f"Actual: `{bps:.1f}` BPS",
        (f"Cycle: api\\_p50=`{api_p50:.0f}ms` + overhead=`{overhead:.0f}ms` "
         f"+ delay=`{delay_ms:.0f}ms` = `{cycle_ms:.0f}ms/bet`"),
        "",
        f"*Top blocker:* {top_name} (`{top_val:.0f}ms`)",
        "",
        "*Advice:*",
    ] + advice
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /analytics ───────────────────────────────────────────
async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """v1.18.0: mirrors CLI wolfctl analytics: lifetime + per-game + top-N.
    v1.19.0: optional date window: /analytics [SINCE] [UNTIL] (bare dates
    or ISO timestamps; bare UNTIL = inclusive end of day)."""
    user_id = update.effective_user.id
    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No session history yet.")
        return

    # v1.19.0: /analytics [since] [until]
    since = until = None
    args = list(context.args or [])
    if len(args) > 2:
        await update.message.reply_text(
            "Usage: `/analytics [SINCE] [UNTIL]`: e.g. `/analytics 2026-08-14` "
            "or `/analytics 2026-08-01 2026-08-15`",
            parse_mode="Markdown")
        return
    if args:
        since = args[0]
        if len(args) == 2:
            until = args[1]
            if len(until) == 10:
                until += "T23:59:59"
    wconds, wparams = [], []
    if since:
        wconds.append("started_at >= ?")
        wparams.append(since)
    if until:
        wconds.append("started_at <= ?")
        wparams.append(until)
    where = (" WHERE " + " AND ".join(wconds)) if wconds else ""
    window_label = f"  ({since or '...'} → {args[1] if len(args) == 2 else '...'})" if args else ""

    await asyncio.to_thread(migrate_db, db_path)
    lifetime = await fetch_one(db_path, f"""
        SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
               COALESCE(SUM(losses),0), COALESCE(SUM(wagered),0),
               COALESCE(SUM(profit),0), COALESCE(MAX(profit),0),
               COALESCE(MIN(profit),0),
               SUM(CASE WHEN profit>=0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN profit<0  THEN 1 ELSE 0 END)
        FROM sessions{where}
    """, tuple(wparams))
    by_game = await fetch_all(db_path, f"""
        SELECT COALESCE(game,'?'), COUNT(*), COALESCE(SUM(total_bets),0),
               COALESCE(SUM(wins),0), COALESCE(SUM(losses),0),
               COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0)
        FROM sessions{where} GROUP BY game ORDER BY 6 DESC
    """, tuple(wparams))
    by_strat = await fetch_all(db_path, f"""
        SELECT COALESCE(strategy,'?'), COUNT(*), COALESCE(SUM(total_bets),0),
               COALESCE(SUM(wins),0), COALESCE(SUM(losses),0),
               COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0)
        FROM sessions{where} GROUP BY strategy ORDER BY 6 DESC
    """, tuple(wparams))
    top_best = await fetch_all(db_path, f"""
        SELECT id, COALESCE(game,'?'), COALESCE(strategy,'?'),
               COALESCE(total_bets,0), COALESCE(profit,0)
        FROM sessions{where} ORDER BY profit DESC LIMIT 5
    """, tuple(wparams))
    top_worst = await fetch_all(db_path, f"""
        SELECT id, COALESCE(game,'?'), COALESCE(strategy,'?'),
               COALESCE(total_bets,0), COALESCE(profit,0)
        FROM sessions{where} ORDER BY profit ASC LIMIT 5
    """, tuple(wparams))

    (sess_total, bets_total, wins_total, losses_total,
     wagered_total, profit_total, best_p, worst_p, in_prof, in_loss) = lifetime
    if sess_total == 0:
        await update.message.reply_text("No sessions in the DB yet.")
        return
    wr = (wins_total / bets_total * 100) if bets_total > 0 else 0
    ps = "+" if profit_total >= 0 else ""

    lines = [
        f"*📈 {'Range' if args else 'Lifetime'} analytics*{window_label}",
        "",
        f"Sessions: `{sess_total}` (`{in_prof or 0}` in profit / `{in_loss or 0}` in loss)",
        f"Bets: `{bets_total:,}`  ·  Win rate: `{wr:.1f}%`",
        f"P/L: `{ps}{profit_total:.8f}`  ·  Wagered: `{wagered_total:.8f}`",
        f"Best: `+{best_p:.8f}`  ·  Worst: `{worst_p:.8f}`",
    ]
    if by_game:
        lines += ["", "*🎲 By game*"]
        for g, n, b, w, l, p, wag in by_game:
            gr = f"{w/b*100:.1f}%" if b > 0 else "-"
            sp = "+" if p >= 0 else ""
            lines.append(f"`{g}`  ·  {n} sess  ·  {b} bets  ·  {gr}  ·  `{sp}{p:.8f}`")
    if by_strat:
        lines += ["", "*🎯 By strategy*"]
        for s, n, b, w, l, p, wag in by_strat:
            gr = f"{w/b*100:.1f}%" if b > 0 else "-"
            sp = "+" if p >= 0 else ""
            lines.append(f"`{s}`  ·  {n} sess  ·  {b} bets  ·  {gr}  ·  `{sp}{p:.8f}`")
    if top_best:
        lines += ["", "*🏆 Top 5 best sessions*"]
        for i, g, s, b, p in top_best:
            lines.append(f"#{i}  `{g}`  `{s}`  {b} bets  `+{p:.8f}`")
    if top_worst:
        lines += ["", "*💀 Top 5 worst sessions*"]
        for i, g, s, b, p in top_worst:
            lines.append(f"#{i}  `{g}`  `{s}`  {b} bets  `{p:.8f}`")

    text = "\n".join(lines)
    if len(text) <= 4096:
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        mid = text.find("*🏆")
        await update.message.reply_text(text[:mid], parse_mode="Markdown")
        await update.message.reply_text(text[mid:], parse_mode="Markdown")


# ── /stats ───────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No session history yet.")
        return

    # Optional game filter: /stats limbo, /stats dice-manual
    game_filter = None
    filter_clause = ""
    filter_params = ()
    if context.args:
        from .engine import GAME_LABELS
        g = context.args[0].lower()
        if g in GAME_LABELS:
            game_filter = g
            filter_clause = " WHERE game = ?"
            filter_params = (g,)

    await asyncio.to_thread(migrate_db, db_path)
    rows = await fetch_all(db_path, f"""
        SELECT id, started_at, ended_at,
               UPPER(currency), COALESCE(game, 'dice'), strategy, multiplier, base_bet,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0),
               COALESCE(config_snapshot, '')
        FROM sessions{filter_clause} ORDER BY id DESC LIMIT 10
    """, filter_params)

    totals = await fetch_one(db_path, f"""
        SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
               COALESCE(SUM(losses),0), COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0),
               COALESCE(MAX(max_win_streak),0), COALESCE(MAX(max_loss_streak),0),
               COALESCE(MAX(profit),0), COALESCE(MIN(profit),0),
               COALESCE(MAX(total_bets),0), COALESCE(AVG(total_bets),0),
               COALESCE(MAX(highest_balance),0), COALESCE(MAX(highest_win),0),
               COALESCE(MAX(biggest_loss),0), COALESCE(AVG(bets_per_minute),0),
               COALESCE(MAX(peak_bps),0), COALESCE(MAX(peak_bpm),0),
               COALESCE(AVG(bets_per_second),0)
        FROM sessions{filter_clause}
    """, filter_params)

    if not rows:
        msg = f"No sessions found for `{game_filter}`." if game_filter else "No sessions found."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    from .engine import GAME_LABELS
    title = f"*{GAME_LABELS.get(game_filter, game_filter)}*" if game_filter else "last 10"
    lines = [f"\U0001f4ca *Session History ({title})*\n"]
    for row in rows:
        lines.append(format_session_row(row))

    # All-time totals
    lines.append(format_all_time(totals))
    lines.append(f"\n_View session detail:_ /session <id>")

    text = "\n".join(lines)
    # Telegram limit is 4096 chars: split if needed
    if len(text) <= 4096:
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        # Send sessions first, then totals
        mid = text.rfind("\U0001f4ca *All-Time")
        await update.message.reply_text(text[:mid], parse_mode="Markdown")
        await update.message.reply_text(text[mid:], parse_mode="Markdown")


# ── /lastbets ────────────────────────────────────────────
async def cmd_lastbets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    n = int(context.args[0]) if context.args else 10
    n = min(n, 50)

    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No data.")
        return

    rows = await fetch_all(db_path, """
        SELECT timestamp, amount, result_value, state, profit, balance_after
        FROM bets ORDER BY id DESC LIMIT ?
    """, (n,))

    if not rows:
        await update.message.reply_text("No bets found.")
        return

    await update.message.reply_text(format_lastbets(rows), parse_mode="Markdown")


# ── /rules ───────────────────────────────────────────────
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []

    if not rules:
        await update.message.reply_text(
            "No rules configured.\nAdd with /addrule <json>", parse_mode="Markdown")
        return

    lines = ["*Current Rules:*\n"]
    for i, r in enumerate(rules, 1):
        lines.append(f"`{i}.` {r.description}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /addrule ─────────────────────────────────────────────
async def cmd_addrule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "*Usage:* /addrule <json>\n\n"
            "*Condition types (`cond_type`):*\n"
            "`sequence`: count-based (streaks, every N)\n"
            "`profit`: profit/balance threshold\n"
            "`bet`: bet amount threshold\n\n"
            "*Sequence modes (`cond_mode`):*\n"
            "`every`: Every N total (wins/losses/bets)\n"
            "`every_streak`: Every N in streak (N, 2N, 3N...)\n"
            "`first_streak`: First streak of N (once, resets)\n"
            "`streak_above`: Every bet past streak N\n"
            "`streak_below`: Streak below N\n\n"
            "*Profit/bet modes (`cond_mode`):*\n"
            "`gte` >=  `gt` >  `lte` <=  `lt` <\n\n"
            "*Profit fields (`cond_field`):*\n"
            "`profit` `balance` `wagered`\n\n"
            "*Triggers (`cond_trigger`):*\n"
            "`win` `loss` `bet`\n\n"
            "*Actions (`action`):*\n"
            "_Bet amount:_\n"
            "`reset_amount`: Reset to base bet\n"
            "`increase_amount`: Increase by % (value=1 → +1%)\n"
            "`decrease_amount`: Decrease by %\n"
            "`add_amount`: Add fixed amount\n"
            "`deduct_amount`: Deduct fixed amount\n"
            "`set_amount`: Set exact amount\n"
            "_Win chance:_\n"
            "`reset_winchance`: Reset win chance\n"
            "`set_winchance`: Set win chance\n"
            "`increase_wc`: Increase win chance by %\n"
            "`decrease_wc`: Decrease win chance by %\n"
            "`add_wc`: Add to win chance\n"
            "`deduct_wc`: Deduct from win chance\n"
            "_Payout (multiplier):_\n"
            "`reset_payout`: Reset payout\n"
            "`set_payout`: Set payout\n"
            "`increase_payout`: Increase payout by %\n"
            "`decrease_payout`: Decrease payout by %\n"
            "`add_payout`: Add to payout\n"
            "`deduct_payout`: Deduct from payout\n"
            "_Other:_\n"
            "`switch`: Switch over/under (dice rule)\n"
            "`stop`: Stop betting\n"
            "`reset_game`: Full reset\n\n"
            "*Examples:*\n"
            '`/addrule {"cond_type":"sequence","cond_mode":"every","cond_value":1,"cond_trigger":"loss","action":"increase_amount","action_value":1}`\n'
            "_Every loss → increase bet by 1%_\n\n"
            '`/addrule {"cond_type":"sequence","cond_mode":"first_streak","cond_value":5,"cond_trigger":"loss","action":"stop","action_value":0}`\n'
            "_Stop after 5 losses in a row_\n\n"
            '`/addrule {"cond_type":"profit","cond_field":"profit","cond_mode":"gte","cond_value":0.01,"action":"stop","action_value":0}`\n'
            "_Stop when profit >= 0.01_",
            parse_mode="Markdown",
        )
        return

    raw = " ".join(context.args)
    # Telegram replaces " with smart quotes: fix them
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        d = json.loads(raw)
        r = StrategyRule.from_dict(d)
        r.description = describe_rule(r)
    except Exception as e:
        logger.error("cmd_addrule error: %s", e, exc_info=True)
        msg = f"Invalid JSON: `{e}`" if APP_ENV != "production" else "Invalid rule format. Please check the syntax."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []
    rules.append(r)

    config["custom_rules_text"] = json.dumps([rl.to_dict() for rl in rules])
    save_user_config(user_id, config)
    await update.message.reply_text(f"Rule added: `{r.description}`", parse_mode="Markdown")


# ── /clearrules ──────────────────────────────────────────
async def cmd_delrule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /delrule <number>\nSee /rules for rule numbers.")
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid number. Usage: /delrule <number>")
        return

    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []

    if idx < 1 or idx > len(rules):
        await update.message.reply_text(f"Invalid rule number. You have {len(rules)} rule(s).")
        return

    removed = rules.pop(idx - 1)
    config["custom_rules_text"] = json.dumps([rl.to_dict() for rl in rules]) if rules else ""
    save_user_config(user_id, config)
    await update.message.reply_text(f"Deleted rule {idx}: `{removed.description}`", parse_mode="Markdown")


# ── /editrule ───────────────────────────────────────────
async def cmd_editrule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /editrule <number> <json>\n"
            "Example: /editrule 2 {\"action_value\":200}\n\n"
            "Editable fields: `cond_type`, `cond_mode`, `cond_value`, "
            "`cond_trigger`, `cond_field`, `action`, `action_value`",
            parse_mode="Markdown",
        )
        return

    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("First argument must be a rule number.")
        return

    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []

    if idx < 1 or idx > len(rules):
        await update.message.reply_text(f"Invalid rule number. You have {len(rules)} rule(s).")
        return

    raw = " ".join(context.args[1:])
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    try:
        patch = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("cmd_editrule JSON error: %s", e, exc_info=True)
        msg = f"Invalid JSON: `{e}`" if APP_ENV != "production" else "Invalid rule format. Please check the syntax."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    # Merge patch into existing rule
    old_dict = rules[idx - 1].to_dict()
    old_dict.update(patch)
    try:
        new_rule = StrategyRule.from_dict(old_dict)
        new_rule.description = describe_rule(new_rule)
    except Exception as e:
        logger.error("cmd_editrule validation error: %s", e, exc_info=True)
        msg = f"Invalid rule after edit: `{e}`" if APP_ENV != "production" else "Invalid rule. Please check the syntax."
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    rules[idx - 1] = new_rule
    config["custom_rules_text"] = json.dumps([rl.to_dict() for rl in rules])
    save_user_config(user_id, config)
    await update.message.reply_text(f"Updated rule {idx}: `{new_rule.description}`", parse_mode="Markdown")


# ── /clearrules ──────────────────────────────────────────
async def cmd_clearrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    config["custom_rules_text"] = ""
    save_user_config(user_id, config)
    await update.message.reply_text("All rules cleared.")


# ── /presets ─────────────────────────────────────────────
async def cmd_presets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    presets = load_presets(user_id)
    if not presets:
        await update.message.reply_text("No presets saved.")
        return

    lines = ["*Presets:*\n"]
    for name, p in presets.items():
        lines.append(f"`{name}`: {p.get('strategy', '?')} {p.get('multiplier', '?')}x")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /savepreset ──────────────────────────────────────────
async def cmd_savepreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /savepreset <name>", parse_mode="Markdown")
        return
    name = context.args[0]
    config = load_user_config(user_id)

    presets = load_presets(user_id)
    presets[name] = {k: config.get(k) for k in CONFIG_KEYS if k != "api_key"}
    save_presets(user_id, presets)
    await update.message.reply_text(f"Preset `{name}` saved.", parse_mode="Markdown")


# ── /loadpreset ──────────────────────────────────────────
async def cmd_loadpreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /loadpreset <name>", parse_mode="Markdown")
        return
    name = context.args[0]

    presets = load_presets(user_id)
    if name not in presets:
        await update.message.reply_text(
            f"Preset `{name}` not found.", parse_mode="Markdown")
        return

    config = load_user_config(user_id)
    for k, v in presets[name].items():
        config[k] = v
    save_user_config(user_id, config)
    await update.message.reply_text(
        f"Preset `{name}` loaded. Check with /config", parse_mode="Markdown")


# ── /session <id> ───────────────────────────────────────
async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /session <id>\nSee /stats for session IDs.",
            parse_mode="Markdown")
        return

    try:
        session_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Session ID must be a number.")
        return

    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No data.")
        return

    await asyncio.to_thread(migrate_db, db_path)
    sess = await fetch_one(db_path, """
        SELECT id, started_at, ended_at,
               UPPER(currency), COALESCE(game, 'dice'), strategy, multiplier, base_bet,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0),
               COALESCE(config_snapshot, '')
        FROM sessions WHERE id = ?
    """, (session_id,))

    if not sess:
        await update.message.reply_text(f"Session #{session_id} not found.")
        return

    # streak distribution
    bet_rows = await fetch_all(
        db_path,
        "SELECT state FROM bets WHERE session_id=? ORDER BY id",
        (session_id,),
    )

    dist = {"win": {}, "loss": {}}
    cur_type = None
    cur_len = 0
    for (st,) in bet_rows:
        kind = "win" if st == "win" else "loss"
        if kind == cur_type:
            cur_len += 1
        else:
            if cur_type and cur_len > 0:
                dist[cur_type][cur_len] = dist[cur_type].get(cur_len, 0) + 1
            cur_type = kind
            cur_len = 1
    if cur_type and cur_len > 0:
        dist[cur_type][cur_len] = dist[cur_type].get(cur_len, 0) + 1

    # recent bets
    recent = await fetch_all(db_path, """
        SELECT timestamp, amount, result_value, state, profit, balance_after
        FROM bets WHERE session_id = ? ORDER BY id DESC LIMIT 20
    """, (session_id,))

    text = format_session_detail(sess, dist, recent)
    if len(text) <= 4096:
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        # split: detail first, then bets
        marker = "\U0001f4dd *Last"
        idx = text.find(marker)
        if idx > 0:
            await update.message.reply_text(text[:idx], parse_mode="Markdown")
            await update.message.reply_text(text[idx:], parse_mode="Markdown")
        else:
            await update.message.reply_text(text[:4096], parse_mode="Markdown")


# ── Resume support ──────────────────────────────────────
RESUME_FILE = os.path.join(DATA_DIR, "_resume.json")


def save_resume_state():
    """Called on graceful shutdown. Pause all engines, flush DB, save state."""
    if not active_engines:
        if os.path.exists(RESUME_FILE):
            os.remove(RESUME_FILE)
        return

    # Pause all engines immediately
    for engines_dict in active_engines.values():
        for engine in engines_dict.values():
            engine.paused = True
    time.sleep(1)  # let in-flight bets finish

    snapshots = []
    for user_id, engines_dict in list(active_engines.items()):
        for slot, engine in list(engines_dict.items()):
            try:
                engine._flush_bets()
                engine._db_save_session()
                snap = engine.snapshot_state()
                snap["chat_id"] = _engine_chat_ids.get(user_id)
                snap["slot"] = slot
                snapshots.append(snap)
                engine.running = False
                engine._close_conn()
            except Exception as e:
                logger.error("Failed to snapshot user %d slot %d: %s", user_id, slot, e)

    if snapshots:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(RESUME_FILE, "w") as f:
            json.dump(snapshots, f)
        os.chmod(RESUME_FILE, 0o600)
        logger.info("Saved %d engine(s) for resume", len(snapshots))


# ── /cleanup ──────────────────────────────────────────────
async def cmd_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No session data yet.")
        return

    days = 3
    if context.args:
        try:
            days = max(0, int(context.args[0]))
        except ValueError:
            await update.message.reply_text("Usage: /cleanup [days]\nDefault: 3")
            return

    await update.message.reply_text(f"Cleaning up bet records older than {days} day(s)...")
    deleted = cleanup_old_bets(db_path, bet_age_days=days)
    await update.message.reply_text(
        f"Deleted *{deleted:,}* bet record(s).\n"
        f"Session statistics are preserved.",
        parse_mode="Markdown",
    )


# ── /delsession ──────────────────────────────────────────
async def cmd_delsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No session data yet.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /delsession <session\\_id>")
        return

    try:
        session_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Session ID must be a number.")
        return

    # Block deletion of currently running sessions
    running = _get_running(user_id)
    for engine in running.values():
        if engine.session_id == session_id:
            await update.message.reply_text(
                f"Session #{session_id} is currently running. Stop it first with /stop.")
            return

    def _delete_session_rows() -> tuple | None:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            row = conn.execute(
                "SELECT id, total_bets, profit FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM bets WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            conn.execute("VACUUM")
            return row
        finally:
            conn.close()

    row = await asyncio.to_thread(_delete_session_rows)
    if not row:
        await update.message.reply_text(f"Session #{session_id} not found.")
        return

    await update.message.reply_text(
        f"Deleted session *#{session_id}* ({row[1]:,} bets, profit: {row[2]:.8f}).",
        parse_mode="Markdown",
    )


async def load_resume_state(application):
    """Called on startup. Restore engines from resume file."""
    if not os.path.exists(RESUME_FILE):
        return

    try:
        with open(RESUME_FILE) as f:
            snapshots = json.load(f)
    except Exception as e:
        logger.error("Failed to load resume file: %s", e)
        return
    finally:
        os.remove(RESUME_FILE)  # consume it: don't re-resume on crash

    resumed = 0

    for snap in snapshots:
        user_id = snap["user_id"]
        chat_id = snap.get("chat_id")
        config = snap["config"]
        db_path = snap["db_path"]
        slot = snap.get("slot", _alloc_slot(user_id))

        if not chat_id:
            logger.warning("No chat_id for user %d, skipping resume", user_id)
            continue

        try:
            engine = BettingEngine(user_id, db_path, config["api_key"], config)
            engine.restore_state(snap)

            def _make_on_stop(cid, uid, s):
                def on_stop(reason):
                    t = asyncio.create_task(_notify_stop(cid, uid, s, reason, application))
                    t.add_done_callback(_log_task_exception)
                return on_stop

            def _make_on_milestone(cid):
                def on_milestone(data):
                    t = asyncio.create_task(_notify_milestone(cid, data, application))
                    t.add_done_callback(_log_task_exception)
                return on_milestone

            def _make_on_error(cid):
                def on_error(msg):
                    t = asyncio.create_task(_notify_error(cid, msg, application))
                    t.add_done_callback(_log_task_exception)
                return on_error

            engine.on_stop = _make_on_stop(chat_id, user_id, slot)
            engine.on_milestone = _make_on_milestone(chat_id)
            engine.on_error = _make_on_error(chat_id)

            # Retry connection up to 3 times (API may not be ready immediately)
            started = False
            for attempt in range(3):
                if await engine.start_resumed():
                    started = True
                    break
                logger.warning("Resume attempt %d/3 failed for user %d slot %d: %s",
                               attempt + 1, user_id, slot, engine.last_error)
                await asyncio.sleep(5)

            if started:
                active_engines.setdefault(user_id, {})[slot] = engine
                _engine_chat_ids[user_id] = chat_id
                # Register recurring snapshot so a stop after resume can still recur
                recurring_state.setdefault(user_id, {})[slot] = {
                    "delay": int(config.get("recurring_delay") or 60),
                    "chat_id": chat_id,
                    "config": dict(config),
                    "timer_task": None,
                    "app": application,
                }
                resumed += 1
                await application.bot.send_message(
                    chat_id,
                    f"Bot updated: session #{engine.session_id} (slot {slot}) resumed.\n"
                    f"Use /status to check progress.",
                )
            else:
                logger.error("Resume failed for user %d slot %d: %s", user_id, slot, engine.last_error)
                await application.bot.send_message(
                    chat_id,
                    f"Resume failed for session #{snap.get('session_id', '?')} (slot {slot}):\n"
                    + (f"`{engine.last_error}`\n" if APP_ENV != "production" else "")
                    + "Use /bet to start a new session.",
                    parse_mode="Markdown",
                )
        except Exception as e:
            logger.error("Resume failed for user %d: %s", user_id, e)

    if resumed:
        logger.info("Resumed %d/%d engines", resumed, len(snapshots))

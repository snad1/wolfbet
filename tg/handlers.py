"""Telegram command handlers — all bot commands and callback queries."""

import os
import json
import asyncio
import sqlite3
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from . import VERSION
from .config import CONFIG_KEYS, TIERS, get_user_tier, logger
from .database import (
    user_db_path, load_user_config, save_user_config,
    load_presets, save_presets,
)
from core.strategy import (
    STRATEGIES, STRATEGY_NAMES, STRATEGY_BY_NAME, StrategyRule,
    describe_rule, load_rules_from_text,
)
from .engine import BettingEngine
from .formatter import format_status, format_stop, format_milestone, format_session_row


# ── Active engines (one per user) ────────────────────────
active_engines: Dict[int, BettingEngine] = {}


# ── /start ───────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"*WolfBet Bot v{VERSION}*\n\n"
        "Auto-betting engine for WolfBet Dice via Telegram.\n\n"
        "*Setup:*\n"
        "`/setkey <token>` — Set your WolfBet API key\n"
        "`/balance` — Check your balances\n"
        "`/config` — View current configuration\n\n"
        "*Configure:*\n"
        "`/set currency usdt`\n"
        "`/set strategy martingale`\n"
        "`/set multiplier 2.01`\n"
        "`/set basebet 0.0001`\n"
        "`/set rule over`\n"
        "`/set maxprofit 0.01`\n"
        "`/set maxloss 0.005`\n\n"
        "*Session:*\n"
        "`/bet` — Start betting session\n"
        "`/stop` — Stop session\n"
        "`/pause` / `/resume`\n"
        "`/status` — Live status\n"
        "`/stats` — Session history\n\n"
        "`/strategies` — List all strategies\n"
        "`/help` — Full command reference",
        parse_mode="Markdown",
    )


# ── /help ────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands:*\n\n"
        "*Setup*\n"
        "`/setkey <token>` — API key (message auto-deleted)\n"
        "`/balance` — Show all balances\n"
        "`/config` — Show current config\n"
        "`/set <param> <value>` — Set parameter\n"
        "`/strategies` — List strategies\n\n"
        "*Parameters for /set:*\n"
        "`currency` — usdt, btc, eth, ltc, trx, bch, doge, xrp\n"
        "`strategy` — flat, martingale, antimartingale, dalembert, paroli, wolfrider, delaymartingale, rulebased\n"
        "`multiplier` — target multiplier (e.g. 2.01)\n"
        "`basebet` — base bet amount\n"
        "`rule` — over / under\n"
        "`lossmult` — loss multiplier (default 2.0)\n"
        "`winmult` — win multiplier (default 1.0)\n"
        "`delay` — seconds between bets\n"
        "`maxprofit` — stop at profit\n"
        "`maxloss` — stop at loss\n"
        "`maxbets` — stop after N bets\n"
        "`maxwins` — stop after N wins\n"
        "`minbalance` — stop below balance\n"
        "`delaythreshold` — Delay Martingale threshold\n\n"
        "*Session*\n"
        "`/bet` — Start session\n"
        "`/stop` — Stop session\n"
        "`/pause` — Pause betting\n"
        "`/resume` — Resume betting\n"
        "`/status` — Live status (with refresh button)\n"
        "`/stats` — Session history\n"
        "`/session <id>` — Session details\n"
        "`/lastbets [n]` — Recent bets\n\n"
        "*Rules*\n"
        "`/rules` — List current rules\n"
        "`/addrule <json>` — Add rule\n"
        "`/clearrules` — Clear all rules\n\n"
        "*Presets*\n"
        "`/presets` — List presets\n"
        "`/savepreset <name>` — Save current config\n"
        "`/loadpreset <name>` — Load preset",
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
            "Usage: `/setkey <your_wolfbet_api_key>`", parse_mode="Markdown")
        return

    user_id = update.effective_user.id
    api_key = context.args[0]
    config = load_user_config(user_id)
    config["api_key"] = api_key
    save_user_config(user_id, config)

    await update.effective_chat.send_message(
        "API key saved. Your message was deleted for security.\n"
        "Test with `/balance`", parse_mode="Markdown")


# ── /balance ─────────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    api_key = config.get("api_key")
    if not api_key:
        await update.message.reply_text(
            "Set your API key first: `/setkey <token>`", parse_mode="Markdown")
        return

    await update.message.reply_text("Fetching balances…")
    try:
        engine = BettingEngine(user_id, "", api_key, {})
        balances = engine.api_get_balances()
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
        await update.message.reply_text(f"Error: `{e}`", parse_mode="Markdown")


# ── /config ──────────────────────────────────────────────
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)

    strategy_key = config.get("strategy_key", "2")
    strategy = config.get("strategy", STRATEGY_NAMES.get(strategy_key, "Martingale"))
    multiplier = config.get("multiplier", 2.01)
    wc = round(99.0 / multiplier, 2)

    lines = [
        "*Current Configuration:*",
        f"Currency: `{config.get('currency', 'usdt').upper()}`",
        f"Strategy: `{strategy}`",
        f"Rule: `{config.get('rule', 'over')}`",
        f"Multiplier: `{multiplier}x` ({wc}% win chance)",
        f"Base bet: `{config.get('base_bet', 0.0001):.8f}`",
        f"Loss mult: `{config.get('loss_mult', 2.0)}x`",
        f"Win mult: `{config.get('win_mult', 1.0)}x`",
        f"Bet delay: `{config.get('bet_delay', 0)}s`",
    ]

    stops = []
    if config.get("max_profit"): stops.append(f"Max profit: {config['max_profit']}")
    if config.get("max_loss"):   stops.append(f"Max loss: {config['max_loss']}")
    if config.get("max_bets"):   stops.append(f"Max bets: {config['max_bets']}")
    if config.get("max_wins"):   stops.append(f"Max wins: {config['max_wins']}")
    if config.get("stop_on_balance"): stops.append(f"Min balance: {config['stop_on_balance']}")
    if stops:
        lines.append(f"Stop: `{', '.join(stops)}`")
    else:
        lines.append("Stop conditions: _none_")

    api_set = "Yes" if config.get("api_key") else "No"
    lines.append(f"API key: `{api_set}`")
    lines.append(f"Tier: `{get_user_tier(user_id)}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /set ─────────────────────────────────────────────────
async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/set <param> <value>`\nSee `/help` for parameters.",
            parse_mode="Markdown")
        return

    param = context.args[0].lower()
    value = " ".join(context.args[1:])
    config = load_user_config(user_id)

    try:
        if param == "currency":
            config["currency"] = value.lower()
        elif param == "strategy":
            name = value.lower().replace("-", "").replace(" ", "").replace("_", "")
            key = STRATEGY_BY_NAME.get(name)
            if not key:
                if value in STRATEGIES:
                    key = value
                else:
                    await update.message.reply_text(
                        "Unknown strategy. Use `/strategies` to see options.",
                        parse_mode="Markdown")
                    return
            config["strategy_key"] = key
            config["strategy"] = STRATEGY_NAMES[key]
        elif param == "multiplier":
            mult = float(value)
            config["multiplier"] = mult
            wc = 99.0 / mult
            rule = config.get("rule", "over")
            config["bet_value"] = round(100.0 - wc - 0.01, 2) if rule == "over" else round(wc, 2)
        elif param == "basebet":
            config["base_bet"] = float(value)
        elif param == "rule":
            if value.lower() not in ("over", "under"):
                await update.message.reply_text(
                    "Rule must be `over` or `under`", parse_mode="Markdown")
                return
            config["rule"] = value.lower()
            mult = config.get("multiplier", 2.01)
            wc = 99.0 / mult
            config["bet_value"] = round(100.0 - wc - 0.01, 2) if value.lower() == "over" else round(wc, 2)
        elif param == "lossmult":
            config["loss_mult"] = float(value)
        elif param == "winmult":
            config["win_mult"] = float(value)
        elif param == "delay":
            config["bet_delay"] = float(value)
        elif param == "maxprofit":
            config["max_profit"] = float(value) if value.lower() != "off" else None
        elif param == "maxloss":
            config["max_loss"] = float(value) if value.lower() != "off" else None
        elif param == "maxbets":
            config["max_bets"] = int(value) if value.lower() != "off" else None
        elif param == "maxwins":
            config["max_wins"] = int(value) if value.lower() != "off" else None
        elif param == "minbalance":
            config["stop_on_balance"] = float(value) if value.lower() != "off" else None
        elif param == "delaythreshold":
            config["delay_martin_threshold"] = int(value)
        else:
            await update.message.reply_text(
                f"Unknown parameter: `{param}`\nSee `/help`", parse_mode="Markdown")
            return

        save_user_config(user_id, config)
        await update.message.reply_text(f"Set `{param}` = `{value}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text(
            f"Invalid value for `{param}`: `{value}`", parse_mode="Markdown")


# ── /strategies ──────────────────────────────────────────
async def cmd_strategies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["*Available Strategies:*\n"]
    for k, (name, desc) in STRATEGIES.items():
        lines.append(f"`{k}` *{name}* — {desc}")
    lines.append("\nSet with: `/set strategy <name>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /bet ─────────────────────────────────────────────────
async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in active_engines and active_engines[user_id].running:
        await update.message.reply_text(
            "Session already running. `/stop` first.", parse_mode="Markdown")
        return

    config = load_user_config(user_id)
    if not config.get("api_key"):
        await update.message.reply_text(
            "Set your API key first: `/setkey <token>`", parse_mode="Markdown")
        return

    # Apply tier rate limit
    tier = get_user_tier(user_id)
    min_delay = TIERS.get(tier, 1.0)
    config_delay = config.get("bet_delay", 0)
    config["bet_delay"] = max(config_delay, min_delay)

    db_path = user_db_path(user_id)
    engine = BettingEngine(user_id, db_path, config["api_key"], config)

    # Set up async callbacks from betting thread
    loop = asyncio.get_event_loop()
    chat_id = update.effective_chat.id
    app = context.application

    def on_stop(reason):
        asyncio.run_coroutine_threadsafe(
            _notify_stop(chat_id, user_id, reason, app), loop)

    def on_milestone(data):
        asyncio.run_coroutine_threadsafe(
            _notify_milestone(chat_id, data, app), loop)

    engine.on_stop = on_stop
    engine.on_milestone = on_milestone

    msg = await update.message.reply_text("Connecting to WolfBet…")

    if not engine.start():
        await msg.edit_text(f"Failed to start: {engine.last_error}")
        return

    active_engines[user_id] = engine

    wc = round(99.0 / engine.multiplier, 2)
    await msg.edit_text(
        f"Session #{engine.session_id} started!\n\n"
        f"Strategy: `{engine.strategy}`\n"
        f"Multiplier: `{engine.multiplier}x` ({wc}%)\n"
        f"Base bet: `{engine.base_bet:.8f} {engine.currency.upper()}`\n"
        f"Balance: `{engine.current_balance:.8f} {engine.currency.upper()}`\n\n"
        f"Use `/status` to check progress.",
        parse_mode="Markdown",
    )


async def _notify_stop(chat_id: int, user_id: int, reason: str, app):
    engine = active_engines.pop(user_id, None)
    if not engine:
        return
    s = engine.get_status()
    text = format_stop(s, reason)
    await app.bot.send_message(chat_id, text, parse_mode="Markdown")


async def _notify_milestone(chat_id: int, data: dict, app):
    text = format_milestone(data)
    await app.bot.send_message(chat_id, text, parse_mode="Markdown")


# ── /stop ────────────────────────────────────────────────
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    engine = active_engines.get(user_id)
    if not engine or not engine.running:
        await update.message.reply_text("No session running.")
        return
    engine.stop_reason = "Stopped by user"
    engine.stop()


# ── /pause ───────────────────────────────────────────────
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    engine = active_engines.get(user_id)
    if not engine or not engine.running:
        await update.message.reply_text("No session running.")
        return
    engine.pause()
    await update.message.reply_text(
        "Session paused. `/resume` to continue.", parse_mode="Markdown")


# ── /resume ──────────────────────────────────────────────
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    engine = active_engines.get(user_id)
    if not engine or not engine.running:
        await update.message.reply_text("No session running.")
        return
    engine.resume()
    await update.message.reply_text("Session resumed.")


# ── /status ──────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    engine = active_engines.get(user_id)
    if not engine or not engine.running:
        await update.message.reply_text(
            "No session running. Start one with `/bet`", parse_mode="Markdown")
        return

    text = format_status(engine.get_status())
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Refresh", callback_data="refresh_status"),
        InlineKeyboardButton("Stop", callback_data="stop_session"),
    ]])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


# ── Inline button callbacks ─────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "refresh_status":
        engine = active_engines.get(user_id)
        if not engine or not engine.running:
            await query.edit_message_text("Session ended.")
            return
        text = format_status(engine.get_status())
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Refresh", callback_data="refresh_status"),
            InlineKeyboardButton("Stop", callback_data="stop_session"),
        ]])
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            pass  # message unchanged

    elif query.data == "stop_session":
        engine = active_engines.get(user_id)
        if engine and engine.running:
            engine.stop_reason = "Stopped by user"
            engine.stop()
        await query.edit_message_text("Stopping session…")


# ── /stats ───────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No session history yet.")
        return

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT id, substr(started_at,1,19), substr(ended_at,1,19),
               strategy, total_bets, wins, losses, profit, wagered,
               start_balance, end_balance, max_win_streak, max_loss_streak
        FROM sessions ORDER BY id DESC LIMIT 10
    """).fetchall()

    totals = conn.execute("""
        SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
               COALESCE(SUM(losses),0), COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0)
        FROM sessions
    """).fetchone()
    conn.close()

    if not rows:
        await update.message.reply_text("No sessions found.")
        return

    lines = ["*Session History:*\n"]
    for row in rows:
        lines.append(format_session_row(row))

    sessions, tot_bets, tot_wins, tot_losses, tot_profit, tot_wagered = totals
    ps = "+" if tot_profit >= 0 else ""
    twr = f"{tot_wins/tot_bets*100:.1f}%" if tot_bets > 0 else "—"
    lines.append(
        f"\n*All-time ({sessions} sessions):*\n"
        f"Bets: `{tot_bets}`  W: `{tot_wins}`  L: `{tot_losses}`  WR: `{twr}`\n"
        f"Profit: `{ps}{tot_profit:.8f}`\n"
        f"Wagered: `{tot_wagered:.8f}`"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /lastbets ────────────────────────────────────────────
async def cmd_lastbets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    n = int(context.args[0]) if context.args else 10
    n = min(n, 50)

    db_path = user_db_path(user_id)
    if not os.path.exists(db_path):
        await update.message.reply_text("No data.")
        return

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT timestamp, amount, result_value, state, profit, balance_after
        FROM bets ORDER BY id DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No bets found.")
        return

    lines = [f"*Last {len(rows)} bets:*\n```"]
    for ts, amt, rv, st, pnl, bal in rows:
        t = ts[11:19] if ts else "?"
        w = "W" if st == "win" else "L"
        ps = "+" if pnl >= 0 else ""
        lines.append(f"{t} {amt:.8f} {rv:>7.2f} {w} {ps}{pnl:.8f} {bal:.8f}")
    lines.append("```")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /rules ───────────────────────────────────────────────
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []

    if not rules:
        await update.message.reply_text(
            "No rules configured.\nAdd with `/addrule <json>`", parse_mode="Markdown")
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
            "Usage: `/addrule <json>`\n\n"
            "Example:\n"
            '`/addrule {"cond_type":"sequence","cond_mode":"every","cond_value":1,"cond_trigger":"loss","action":"increase_amount","action_value":101}`',
            parse_mode="Markdown",
        )
        return

    raw = " ".join(context.args)
    try:
        d = json.loads(raw)
        r = StrategyRule.from_dict(d)
        r.description = describe_rule(r)
    except Exception as e:
        await update.message.reply_text(f"Invalid JSON: `{e}`", parse_mode="Markdown")
        return

    config = load_user_config(user_id)
    rules_text = config.get("custom_rules_text", "")
    rules = load_rules_from_text(rules_text) if rules_text else []
    rules.append(r)

    config["custom_rules_text"] = json.dumps([rl.to_dict() for rl in rules])
    save_user_config(user_id, config)
    await update.message.reply_text(f"Rule added: `{r.description}`", parse_mode="Markdown")


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
        lines.append(f"`{name}` — {p.get('strategy', '?')} {p.get('multiplier', '?')}x")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /savepreset ──────────────────────────────────────────
async def cmd_savepreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Usage: `/savepreset <name>`", parse_mode="Markdown")
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
            "Usage: `/loadpreset <name>`", parse_mode="Markdown")
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
        f"Preset `{name}` loaded. Check with `/config`", parse_mode="Markdown")

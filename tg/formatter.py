"""Message formatting helpers for Telegram responses."""

from datetime import datetime


def format_status(s: dict) -> str:
    """Format engine status dict into a Telegram-friendly Markdown message."""
    ps = "+" if s["profit"] >= 0 else ""
    streak_s = f"+{s['streak']}" if s["streak"] >= 0 else str(s["streak"])
    return (
        f"*Session #{s['session_id']}* — {s['uptime']}\n"
        f"{'PAUSED' if s['paused'] else 'LIVE'}\n\n"
        f"Balance: `{s['balance']:.8f} {s['currency']}`\n"
        f"P/L: `{ps}{s['profit']:.8f}`\n"
        f"Wagered: `{s['wagered']:.8f}`\n\n"
        f"Bets: `{s['bets']}`  W: `{s['wins']}`  L: `{s['losses']}`  WR: `{s['win_rate']}`\n"
        f"Streak: `{streak_s}`  Best: `W+{s['max_win_streak']}` / `L-{s['max_loss_streak']}`\n\n"
        f"Strategy: `{s['strategy']}` {s['multiplier']}x\n"
        f"Bet: `{s['current_bet']:.8f}`  Hi: `{s['highest_bet']:.8f}`\n"
        f"Peak: `{s['highest_balance']:.8f}`  Low: `{s['lowest_balance']:.8f}`\n"
        f"BPS: `{s['bps']:.1f}`\n\n"
        f"`{s['status']}`"
    )


def format_stop(s: dict, reason: str) -> str:
    """Format session-ended summary."""
    ps = "+" if s["profit"] >= 0 else ""
    return (
        f"Session #{s['session_id']} ended.\n"
        f"Reason: `{reason}`\n\n"
        f"Bets: `{s['bets']}`  W: `{s['wins']}`  L: `{s['losses']}`\n"
        f"Profit: `{ps}{s['profit']:.8f} {s['currency']}`\n"
        f"Balance: `{s['balance']:.8f}`\n"
        f"Uptime: `{s['uptime']}`"
    )


def format_milestone(data: dict) -> str:
    """Format milestone notification (every 100 bets)."""
    ps = "+" if data["profit"] >= 0 else ""
    return (
        f"*{data['bets']} bets* | P/L: `{ps}{data['profit']:.8f}` | "
        f"Bal: `{data['balance']:.8f}` | WR: `{data['win_rate']}` | {data['uptime']}"
    )


def format_session_row(row) -> str:
    """Format a single session row for the stats view."""
    sid, started, ended, strat, bets, wins, losses, profit, wagered, sb, eb, mws, mls = row
    bets = bets or 0; wins = wins or 0; losses = losses or 0; profit = profit or 0
    ps = "+" if profit >= 0 else ""
    wr = f"{wins/bets*100:.1f}%" if bets > 0 else "—"
    uptime = _calc_uptime(started, ended)
    return (
        f"*#{sid}* {strat} | {bets} bets | WR {wr}\n"
        f"  P/L: `{ps}{profit:.8f}` | {uptime}\n"
    )


def _calc_uptime(started: str, ended: str) -> str:
    """Calculate uptime string from started/ended timestamps."""
    try:
        t0 = datetime.strptime(started[:19], "%Y-%m-%d %H:%M:%S") if started else None
        t1 = datetime.strptime(ended[:19], "%Y-%m-%d %H:%M:%S") if ended else datetime.now()
        if t0:
            d = int((t1 - t0).total_seconds())
            h, rem = divmod(max(d, 0), 3600)
            m, s = divmod(rem, 60)
            return f"{h}h{m}m{s}s"
    except Exception:
        pass
    return "—"

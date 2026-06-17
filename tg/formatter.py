"""Message formatting helpers for Telegram responses."""

import json
from datetime import datetime


def _fmt_ts(ts: str, fallback: str = "?") -> str:
    """Format an ISO timestamp to full display: YYYY-MM-DD HH:MM:SS.μs"""
    if not ts:
        return fallback
    # Replace T separator, keep full precision
    return ts.replace("T", " ")


# ── Emoji helpers (Telegram has no ANSI colors) ──────────
def _pnl_emoji(v: float) -> str:
    return "\U0001f7e2" if v >= 0 else "\U0001f534"  # green/red circle


def _streak_emoji(s: int) -> str:
    if s > 0:
        return "\U0001f525"  # fire for win streak
    elif s < 0:
        return "\u2744\ufe0f"  # snowflake for loss streak
    return "\u2796"  # minus


def _fmti(n) -> str:
    """Format integer with comma separators for readability."""
    return f"{int(n):,}"


def format_full_config(s: dict) -> list:
    """Return full config as list of formatted lines. Used by status, stop, and session-start."""
    lines = []
    lines.append(f"  Game: `{s.get('game_label', s.get('game', '?'))}`")
    lines.append(f"  Strategy: `{s.get('strategy', '?')}` `{s.get('multiplier', 0):.2f}x`")
    sk = s.get("strategy_key", "")
    if sk in ("2", "6", "7"):
        lines.append(f"  Loss mult: `{s.get('loss_mult', 0):.2f}x`")
    if sk in ("3", "4", "5"):
        lines.append(f"  Win mult: `{s.get('win_mult', 0):.2f}x`")
    rule = s.get("rule")
    bv = s.get("bet_value")
    if rule and bv is not None:
        lines.append(f"  Rule: `{rule}` value `{bv}`")

    # base bet + adaptive
    base_bet = s.get("base_bet", 0)
    bbp = s.get("basebet_pct") or 0
    if bbp > 0:
        lines.append(f"  Base bet: `{base_bet:.8f}` (`{bbp*100:.4f}%` of balance)")
    else:
        lines.append(f"  Base bet: `{base_bet:.8f}`")

    # delays
    bd = s.get("bet_delay", 0)
    if bd:
        lines.append(f"  Bet delay: `{bd:.3f}s`")
    sdl = s.get("streak_delay_loss")
    if sdl and sdl[0] > 0:
        lines.append(f"  Streak delay loss: every `{sdl[0]}` → `{sdl[1]:.3f}s`")
    sdw = s.get("streak_delay_win")
    if sdw and sdw[0] > 0:
        lines.append(f"  Streak delay win: every `{sdw[0]}` → `{sdw[1]:.3f}s`")
    sdb = s.get("streak_delay_bets")
    if sdb and sdb[0] > 0:
        lines.append(f"  Streak delay bets: every `{sdb[0]}` bets → `{sdb[1]:.3f}s`")
    sbl = s.get("streakbet_loss")
    if sbl and sbl[0] > 0:
        lines.append(f"  Streak bet loss: every `{sbl[0]}` losses → `x{sbl[1]:.3f}`")

    # pipeline
    pipe = s.get("pipeline", 1)
    if pipe and pipe > 1:
        lines.append(f"  Pipeline: `{pipe}`")

    # stops
    stops = s.get("stop_conditions", {}) or {}
    if stops:
        parts = []
        if "max_profit" in stops: parts.append(f"profit≥`{float(stops['max_profit']):.8f}`")
        if "max_loss" in stops:   parts.append(f"loss≥`{float(stops['max_loss']):.8f}`")
        if "max_bets" in stops:   parts.append(f"bets≥`{_fmti(stops['max_bets'])}`")
        if "max_wins" in stops:   parts.append(f"wins≥`{_fmti(stops['max_wins'])}`")
        if "min_balance" in stops: parts.append(f"bal≤`{float(stops['min_balance']):.8f}`")
        lines.append(f"  Stops: {', '.join(parts)}")

    # profit bump
    pi = s.get("profit_increment")
    pt = s.get("profit_threshold")
    npm = s.get("next_profit_milestone")
    if pi is not None and pt:
        nxt = f" (next at `{float(npm):.8f}`)" if npm else ""
        lines.append(f"  Profit bump: `+{float(pi):.8f}` every `{float(pt):.8f}`{nxt}")

    # milestone
    mb = s.get("milestone_bets")
    if mb:
        lines.append(f"  Milestone: every `{_fmti(mb)}` bets")

    return lines


def _bar(value: float, total: float, width: int = 10) -> str:
    """Simple text progress bar."""
    if total <= 0:
        return "\u2591" * width
    filled = int(value / total * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


# ── /status ──────────────────────────────────────────────
def format_status(s: dict) -> str:
    """Format engine status dict into a detailed Telegram message."""
    p = s["profit"]
    ps = "+" if p >= 0 else ""
    pe = _pnl_emoji(p)
    streak = s["streak"]
    se = _streak_emoji(streak)
    streak_s = f"+{streak}" if streak >= 0 else str(streak)

    # avg P/L per bet
    avg = p / s["bets"] if s["bets"] > 0 else 0
    avg_s = "+" if avg >= 0 else ""
    avg_e = _pnl_emoji(avg)

    # balance change
    bal_change = s["balance"] - s["start_balance"]
    bc_s = "+" if bal_change >= 0 else ""
    bc_e = _pnl_emoji(bal_change)

    state = "\U0001f7e2 LIVE" if not s["paused"] else "\U0001f7e1 PAUSED"

    game_label = s.get("game_label", "Dice")
    pipeline = s.get("pipeline", 1)
    pipe_tag = f" x{pipeline}" if pipeline and pipeline > 1 else ""
    proxy_tag = " \U0001f310" if s.get("proxy") else ""

    lines = [
        f"*Session #{s['session_id']}* \u2014 {s['uptime']}",
        f"{state} \u2014 {game_label}{pipe_tag}{proxy_tag}",
        "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "\U0001f4b0 *Balance*",
        f"  Current: `{s['balance']:.8f} {s['currency']}`",
        f"  {pe} P/L: `{ps}{p:.8f}`",
        f"  Wagered: `{s['wagered']:.8f}`",
        f"  {bc_e} Change: `{bc_s}{bal_change:.8f}`",
        "",
        "\U0001f4ca *Stats*",
        f"  Bets: `{_fmti(s['bets'])}`  W: `{_fmti(s['wins'])}`  L: `{_fmti(s['losses'])}`",
        f"  Win Rate: `{s['win_rate']}`",
        f"  {avg_e} Avg P/L: `{avg_s}{avg:.8f}`",
        "",
        "\U0001f4c8 *Extremes*",
        f"  Peak Bal: `{s['highest_balance']:.8f}`",
        f"  Low Bal: `{s['lowest_balance']:.8f}`",
        f"  Best Win: `+{s['highest_win']:.8f}`",
        f"  Worst Loss: `{'-' if s['biggest_loss'] > 0 else ''}{s['biggest_loss']:.8f}`",
        "",
        f"{se} *Streaks*",
        f"  Current: `{streak_s}`",
        f"  Best: `W+{_fmti(s['max_win_streak'])}` / `L-{_fmti(s['max_loss_streak'])}`",
        "",
        "\u2699\ufe0f *Config*",
    ]
    lines += format_full_config(s)
    lines += [
        f"  Bet now: `{s['current_bet']:.8f}`  Hi: `{s['highest_bet']:.8f}`",
    ]

    pk_bps = int(s.get("peak_bps", 0))
    lw_bps = int(s.get("low_bps", 0))
    pk_bpm = int(s.get("peak_bpm", 0))
    lw_bpm = int(s.get("low_bpm", 0))
    rb = s.get("recent_bps", s["bps"])  # fallback for old sessions without sliding window
    rbm = s.get("recent_bpm", s["bpm"])
    lines += [
        f"  Now:   `{rb:.1f}` bps / `{rbm:.0f}` bpm  (last 60s)",
        f"  Avg:   `{s['bps']:.1f}` bps / `{s['bpm']:.0f}` bpm  (session)",
        f"  Range: `{lw_bps}-{pk_bps}` bps / `{lw_bpm}-{pk_bpm}` bpm",
        f"  API: `{s.get('api_ms', 0):.0f}ms` last / `{s.get('api_avg_ms', 0):.0f}ms` avg",
        f"  Cycle: `{s.get('cycle_ms', 0):.0f}ms`/bet  (1000/{max(s.get('cycle_ms', 1), 1):.0f} = `{(1000.0 / max(s.get('cycle_ms', 1), 1)):.2f}` bps)",
        f"  Overhead: `{s.get('overhead_ms', 0):.0f}ms`/bet  (engine + scheduler)",
    ]

    # custom rules
    rules = s.get("rules", [])
    if rules:
        lines.append("")
        lines.append(f"\U0001f4dc *Rules ({len(rules)})*")
        for i, desc in enumerate(rules, 1):
            lines.append(f"  {i}. `{desc}`")

    # auto-bet UUID
    auto_uuid = s.get("auto_uuid")
    if auto_uuid:
        lines.append("")
        pending = " (restart pending — waiting for win)" if s.get("pending_restart") else ""
        lines.append(f"\U0001f517 UUID: `{auto_uuid}`{pending}")

    lines += ["", f"`{s['status']}`"]

    return "\n".join(lines)


# ── Session stopped ──────────────────────────────────────
def format_stop(s: dict, reason: str) -> str:
    """Format session-ended summary with full details."""
    p = s["profit"]
    ps = "+" if p >= 0 else ""
    pe = _pnl_emoji(p)

    avg = p / s["bets"] if s["bets"] > 0 else 0
    avg_s = "+" if avg >= 0 else ""

    bal_change = s["balance"] - s["start_balance"]
    bc_s = "+" if bal_change >= 0 else ""

    config_lines = "\n".join(format_full_config(s))
    return (
        f"\U0001f6d1 *Session #{s['session_id']} ended* ({s.get('game_label', 'Dice')})\n"
        f"Reason: _{reason}_\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\U0001f4b0 *Balance*\n"
        f"  `{s['start_balance']:.8f}` \u2192 `{s['balance']:.8f}`\n"
        f"  {pe} P/L: `{ps}{p:.8f} {s['currency']}`\n"
        f"  Change: `{bc_s}{bal_change:.8f}`\n"
        f"  Wagered: `{s['wagered']:.8f}`\n"
        f"\n"
        f"\U0001f4ca *Stats*\n"
        f"  Bets: `{_fmti(s['bets'])}`  W: `{_fmti(s['wins'])}`  L: `{_fmti(s['losses'])}`\n"
        f"  Win Rate: `{s['win_rate']}`\n"
        f"  Avg P/L: `{avg_s}{avg:.8f}`\n"
        f"\n"
        f"\U0001f4c8 *Extremes*\n"
        f"  Peak: `{s['highest_balance']:.8f}`  Low: `{s['lowest_balance']:.8f}`\n"
        f"  Best Win: `+{s['highest_win']:.8f}`\n"
        f"  Worst Loss: `{'-' if s['biggest_loss'] > 0 else ''}{s['biggest_loss']:.8f}`\n"
        f"  Streaks: `W+{_fmti(s['max_win_streak'])}` / `L-{_fmti(s['max_loss_streak'])}`\n"
        f"\n"
        f"\u2699\ufe0f *Config used*\n"
        f"{config_lines}\n"
        f"\n"
        f"\u23f1 Uptime: `{s['uptime']}`\n"
        f"Avg speed: `{s['bps']:.1f}` bps / `{s['bpm']:.0f}` bpm"
    )


# ── Milestone (every 100 bets) ───────────────────────────
def format_milestone(data: dict) -> str:
    """Format milestone notification."""
    p = data["profit"]
    ps = "+" if p >= 0 else ""
    pe = _pnl_emoji(p)
    reason = data.get("milestone_reason", f"{data['bets']} bets")
    return (
        f"\U0001f3af *Milestone: {reason}* ({data.get('game_label', 'Dice')})\n"
        f"  {pe} P/L: `{ps}{p:.8f}`\n"
        f"  Bal: `{data['balance']:.8f} {data['currency']}`\n"
        f"  Bets: `{_fmti(data['bets'])}` W: `{_fmti(data['wins'])}` L: `{_fmti(data['losses'])}`\n"
        f"  WR: `{data['win_rate']}` | {data['uptime']}"
    )


# ── Single session row for /stats list ───────────────────
def format_session_row(row) -> str:
    """Format a session row — expects 27 or 28 columns (with game + config_snapshot)."""
    if len(row) >= 28:
        (sid, started, ended, cur, game, strat, mult, base, bets, wins, losses,
         profit, wagered, start_bal, end_bal, mws, mls,
         hi_bal, lo_bal, hi_win, big_loss,
         bpm, bps, pk_bps, lw_bps, pk_bpm, lw_bpm, config_snap) = row
    elif len(row) >= 27:
        (sid, started, ended, cur, game, strat, mult, base, bets, wins, losses,
         profit, wagered, start_bal, end_bal, mws, mls,
         hi_bal, lo_bal, hi_win, big_loss,
         bpm, bps, pk_bps, lw_bps, pk_bpm, lw_bpm) = row
        config_snap = ""
    else:
        # Legacy rows without game column
        (sid, started, ended, cur, strat, mult, base, bets, wins, losses,
         profit, wagered, start_bal, end_bal, mws, mls,
         hi_bal, lo_bal, hi_win, big_loss,
         bpm, bps, pk_bps, lw_bps, pk_bpm, lw_bpm) = row[:26]
        game = "dice"
        config_snap = row[26] if len(row) > 26 else ""

    bets = bets or 0; wins = wins or 0; losses = losses or 0
    profit = profit or 0; wagered = wagered or 0
    start_bal = start_bal or 0; end_bal = end_bal or 0
    mws = mws or 0; mls = mls or 0
    mult = mult or 0; base = base or 0
    hi_bal = hi_bal or 0; lo_bal = lo_bal or 0
    hi_win = hi_win or 0; big_loss = big_loss or 0
    bpm = bpm or 0; bps = bps or 0

    ps = "+" if profit >= 0 else ""
    pe = _pnl_emoji(profit)
    wr = f"{wins/bets*100:.1f}%" if bets > 0 else "\u2014"
    avg = profit / bets if bets > 0 else 0
    avg_s = "+" if avg >= 0 else ""
    uptime = _calc_uptime(started, ended)

    # Build brief config tags from snapshot
    tags = []
    if config_snap:
        try:
            snap = json.loads(config_snap)
            stops = snap.get("stops", {})
            if stops:
                tags.extend(f"{k}:{v}" for k, v in stops.items())
            rules = snap.get("rules", [])
            if rules:
                tags.append(f"{len(rules)} rules")
            if snap.get("profit_threshold"):
                tags.append(f"pi:{snap['profit_increment']}")
        except Exception:
            pass
    tag_line = f"  `{' | '.join(tags)}`\n" if tags else ""

    return (
        f"{pe} *#{sid}* `{(game or 'dice').upper()}`  `{(strat or '?')}`  {float(mult or 0):.2f}x  {(cur or '?')}\n"
        f"  {_fmt_ts(started)} \u2192 {_fmt_ts(ended, 'running')}\n"
        f"  Bets: `{_fmti(bets)}` W: `{_fmti(wins)}` L: `{_fmti(losses)}` WR: `{wr}`\n"
        f"  P/L: `{ps}{profit:.8f}`  Wag: `{wagered:.8f}`\n"
        f"  Bal: `{start_bal:.8f}` \u2192 `{end_bal:.8f}`\n"
        f"  Peak: `{hi_bal:.8f}` Low: `{lo_bal:.8f}`\n"
        f"  Streaks: `W+{_fmti(mws)}` / `L-{_fmti(mls)}`\n"
        f"  Best: `+{hi_win:.8f}` Worst: `-{big_loss:.8f}`\n"
        f"  Avg: `{avg_s}{avg:.8f}` | Speed: `{bps:.1f}`/s\n"
        f"{tag_line}"
        f"  \u23f1 {uptime}\n"
    )


# ── Detailed session view for /session <id> ──────────────
def format_session_detail(sess, streak_dist: dict, recent_bets: list) -> str:
    """Format a full session detail view (like CLI's session detail)."""
    if len(sess) >= 28:
        (sid, started, ended, cur, game, strat, mult, base, bets, wins, losses,
         profit, wagered, start_bal, end_bal, mws, mls,
         hi_bal, lo_bal, hi_win, big_loss,
         bpm, bps, pk_bps, lw_bps, pk_bpm, lw_bpm, config_snap) = sess
    elif len(sess) >= 27:
        (sid, started, ended, cur, game, strat, mult, base, bets, wins, losses,
         profit, wagered, start_bal, end_bal, mws, mls,
         hi_bal, lo_bal, hi_win, big_loss,
         bpm, bps, pk_bps, lw_bps, pk_bpm, lw_bpm) = sess
        config_snap = ""
    else:
        (sid, started, ended, cur, strat, mult, base, bets, wins, losses,
         profit, wagered, start_bal, end_bal, mws, mls,
         hi_bal, lo_bal, hi_win, big_loss,
         bpm, bps, pk_bps, lw_bps, pk_bpm, lw_bpm) = sess[:26]
        game = "dice"
        config_snap = sess[26] if len(sess) > 26 else ""

    bets = bets or 0; wins = wins or 0; losses = losses or 0
    profit = profit or 0; wagered = wagered or 0
    start_bal = start_bal or 0; end_bal = end_bal or 0
    mws = mws or 0; mls = mls or 0
    mult = mult or 0; base = base or 0
    hi_bal = hi_bal or 0; lo_bal = lo_bal or 0
    hi_win = hi_win or 0; big_loss = big_loss or 0
    bpm = bpm or 0; bps = bps or 0
    pk_bps = int(pk_bps or 0); lw_bps = int(lw_bps or 0)
    pk_bpm = int(pk_bpm or 0); lw_bpm = int(lw_bpm or 0)

    ps = "+" if profit >= 0 else ""
    pe = _pnl_emoji(profit)
    wr = f"{wins/bets*100:.1f}%" if bets > 0 else "\u2014"
    avg = profit / bets if bets > 0 else 0
    avg_s = "+" if avg >= 0 else ""
    avg_e = _pnl_emoji(avg)
    uptime = _calc_uptime(started, ended)

    bal_change = end_bal - start_bal
    bc_s = "+" if bal_change >= 0 else ""
    bc_e = _pnl_emoji(bal_change)

    lines = [
        f"\U0001f4cb *Session #{sid} \u2014 {(game or 'dice').upper()} Full Report*",
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        f"\u23f1 *Time*",
        f"  Started: `{_fmt_ts(started)}`",
        f"  Ended: `{_fmt_ts(ended, 'running')}`",
        f"  Uptime: `{uptime}`",
        f"",
        f"\u2699\ufe0f *Config*",
        f"  Currency: `{cur or '?'}`",
        f"  Strategy: `{strat or '?'}`  Mult: `{mult}x`",
        f"  Base bet: `{base:.8f}`",
    ]

    # Parse and display config snapshot
    if config_snap:
        try:
            snap = json.loads(config_snap)
            sk = snap.get("strategy_key", "")
            if sk in ("2", "7"):
                lines.append(f"  Loss mult: `{float(snap.get('loss_mult', 2.0)):.2f}x`")
            if sk in ("3", "5"):
                lines.append(f"  Win mult: `{float(snap.get('win_mult', 1.0)):.2f}x`")
            if sk == "7":
                lines.append(f"  Delay threshold: `{snap.get('delay_threshold', 3)}`")
            if "bet_rule" in snap:
                lines.append(f"  Dice: `{snap['bet_rule']}` value `{snap.get('bet_value', '?')}`")
            stops = snap.get("stops", {})
            if stops:
                parts = [f"{k}: {v}" for k, v in stops.items()]
                lines.append(f"  Stops: `{', '.join(parts)}`")
            if snap.get("profit_threshold"):
                lines.append(f"  Profit incr: `+{float(snap['profit_increment']):.8f} every {float(snap['profit_threshold']):.8f}`")
            rules = snap.get("rules", [])
            if rules:
                from core.strategy import StrategyRule, describe_rule
                lines.append(f"  *Rules ({len(rules)}):*")
                for i, rd in enumerate(rules, 1):
                    r = StrategyRule.from_dict(rd)
                    lines.append(f"    `{i}.` {r.description or describe_rule(r)}")
        except (json.JSONDecodeError, Exception):
            pass

    lines += [
        f"",
        f"\U0001f4ca *Performance*",
        f"  Bets: `{_fmti(bets)}`  W: `{_fmti(wins)}`  L: `{_fmti(losses)}`",
        f"  Win Rate: `{wr}`  {_bar(wins, bets, 12)}",
        f"  {pe} P/L: `{ps}{profit:.8f}`",
        f"  Wagered: `{wagered:.8f}`",
        f"  {avg_e} Avg P/L: `{avg_s}{avg:.8f}` /bet",
        f"",
        f"\U0001f4b0 *Balance*",
        f"  `{start_bal:.8f}` \u2192 `{end_bal:.8f}`",
        f"  {bc_e} Change: `{bc_s}{bal_change:.8f}`",
        f"  \U0001f4c8 Peak: `{hi_bal:.8f}`",
        f"  \U0001f4c9 Low: `{lo_bal:.8f}`",
        f"",
        f"\U0001f3c6 *Extremes*",
        f"  Best Win: `+{hi_win:.8f}`",
        f"  Worst Loss: `{'-' if big_loss > 0 else ''}{big_loss:.8f}`",
        f"  Win Streak: `W+{_fmti(mws)}`",
        f"  Loss Streak: `L-{_fmti(mls)}`",
        f"",
        f"\u26a1 *Speed*",
        f"  Avg: `{bps:.1f}`/s  `{bpm:.0f}`/m",
        f"  BPS range: `{lw_bps}` \u2013 `{pk_bps}`/s",
        f"  BPM range: `{lw_bpm}` \u2013 `{pk_bpm}`/m",
    ]

    # streak distribution
    if streak_dist:
        lines.append("")
        lines.append(f"\U0001f4ca *Streak Distribution*")

        loss_d = streak_dist.get("loss", {})
        if loss_d:
            lines.append(f"  \U0001f534 *Loss Streaks:*")
            max_c = max(loss_d.values()) if loss_d else 1
            for length in sorted(loss_d.keys()):
                count = loss_d[length]
                bar = "\u2588" * int(count / max_c * 8)
                lines.append(f"    L-{length}: `{_fmti(count)}` {bar}")

        win_d = streak_dist.get("win", {})
        if win_d:
            lines.append(f"  \U0001f7e2 *Win Streaks:*")
            max_c = max(win_d.values()) if win_d else 1
            for length in sorted(win_d.keys()):
                count = win_d[length]
                bar = "\u2588" * int(count / max_c * 8)
                lines.append(f"    W+{length}: `{_fmti(count)}` {bar}")

    # recent bets
    if recent_bets:
        lines.append("")
        lines.append(f"\U0001f4dd *Last {len(recent_bets)} Bets*")
        lines.append("```")
        lines.append(f"{'Time':<16} {'Amt':>12} {'Roll':>7} {'W/L':^3} {'P/L':>13} {'Bal':>13}")
        for tm, amt, rv, st, pnl, bal in recent_bets:
            t = tm[11:] if tm else "?"
            w = "W" if st == "win" else "L"
            p_s = "+" if (pnl or 0) >= 0 else ""
            lines.append(
                f"{t:<16} {amt:>12.8f} {rv:>7.2f}  {w}  {p_s}{pnl:>12.8f} {bal:>12.8f}"
            )
        lines.append("```")

    return "\n".join(lines)


# ── All-time totals for /stats ───────────────────────────
def format_all_time(totals) -> str:
    """Format all-time totals block."""
    (sessions, tot_bets, tot_wins, tot_losses, tot_profit, tot_wagered,
     best_ws, best_ls, best_profit, worst_profit, max_bets, avg_bets,
     all_hi_bal, all_hi_win, all_big_loss, avg_bpm, all_pk_bps, all_pk_bpm,
     avg_bps) = totals

    tot_profit = tot_profit or 0
    tot_wagered = tot_wagered or 0
    ps = "+" if tot_profit >= 0 else ""
    pe = _pnl_emoji(tot_profit)
    wr = f"{tot_wins/tot_bets*100:.1f}%" if tot_bets > 0 else "\u2014"
    avg = tot_profit / tot_bets if tot_bets > 0 else 0
    avg_s = "+" if avg >= 0 else ""
    bp_s = "+" if (best_profit or 0) >= 0 else ""
    wp_s = "+" if (worst_profit or 0) >= 0 else ""

    return (
        f"\U0001f4ca *All-Time Totals ({_fmti(sessions)} sessions)*\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"  Bets: `{_fmti(tot_bets)}`  W: `{_fmti(tot_wins)}`  L: `{_fmti(tot_losses)}`\n"
        f"  Win Rate: `{wr}`  {_bar(tot_wins, tot_bets, 12)}\n"
        f"  {pe} Profit: `{ps}{tot_profit:.8f}`\n"
        f"  Wagered: `{tot_wagered:.8f}`\n"
        f"  Avg P/L: `{avg_s}{avg:.8f}` /bet\n"
        f"\n"
        f"  Best Session: `{bp_s}{best_profit:.8f}`\n"
        f"  Worst Session: `{wp_s}{worst_profit:.8f}`\n"
        f"  Peak Balance: `{all_hi_bal:.8f}`\n"
        f"  Best Win: `+{all_hi_win:.8f}`\n"
        f"  Worst Loss: `{'-' if all_big_loss > 0 else ''}{all_big_loss:.8f}`\n"
        f"  Best Streaks: `W+{_fmti(best_ws)}` / `L-{_fmti(best_ls)}`\n"
        f"\n"
        f"  Speed: `{avg_bps:.1f}` avg/s  Peak: `{_fmti(all_pk_bps)}`/s `{_fmti(all_pk_bpm)}`/m\n"
        f"  Bets/Session: max `{_fmti(max_bets)}` avg `{_fmti(avg_bets)}`"
    )


# ── /lastbets formatted ─────────────────────────────────
def format_lastbets(rows: list) -> str:
    """Format last N bets with visual indicators."""
    lines = [f"\U0001f4dd *Last {len(rows)} Bets*\n```"]
    lines.append(f"{'Time':<16} {'Amt':>12} {'Roll':>7} {'W/L':^3} {'P/L':>13} {'Bal':>13}")
    lines.append("\u2500" * 62)
    for ts, amt, rv, st, pnl, bal in rows:
        t = ts[11:] if ts else "?"
        w = "W" if st == "win" else "L"
        p_s = "+" if (pnl or 0) >= 0 else ""
        lines.append(
            f"{t:<16} {amt:>12.8f} {rv:>7.2f}  {w}  {p_s}{pnl:>12.8f} {bal:>12.8f}"
        )
    lines.append("```")

    # summary line
    total = len(rows)
    wins = sum(1 for _, _, _, st, _, _ in rows if st == "win")
    losses = total - wins
    total_pnl = sum(pnl or 0 for _, _, _, _, pnl, _ in rows)
    pnl_s = "+" if total_pnl >= 0 else ""
    pnl_e = _pnl_emoji(total_pnl)
    wr = f"{wins/total*100:.1f}%" if total > 0 else "\u2014"

    lines.append(f"\n{pnl_e} W: `{_fmti(wins)}` L: `{_fmti(losses)}` WR: `{wr}` P/L: `{pnl_s}{total_pnl:.8f}`")
    return "\n".join(lines)


def _calc_uptime(started: str, ended: str) -> str:
    """Calculate uptime string from started/ended timestamps."""
    try:
        t0 = datetime.fromisoformat(started) if started else None
        t1 = datetime.fromisoformat(ended) if ended else datetime.now()
        if t0:
            delta = int((t1 - t0).total_seconds())
            h, rem = divmod(max(delta, 0), 3600)
            m, sec = divmod(rem, 60)
            return f"{h}h {m}m {sec}s"
    except Exception:
        pass
    return "\u2014"

# WolfBet AutoBot v2.5

High-speed auto-betting engine for WolfBet Dice with a live, ultra-compact terminal dashboard. Runs on any VPS or local machine — leave it running in tmux and check stats any time.

---

## Features

| Feature | Details |
|---|---|
| **Pure ANSI TUI** | Direct ANSI rendering (no Rich flicker), fits any terminal, refreshes 2x/sec |
| **8 betting strategies** | Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, WolfRider, Delay Martingale, Rule-Based |
| **Rule-based engine** | Build custom IF/THEN rules with interactive wizard — sequence, profit, and bet conditions |
| **Editable multipliers** | Customize loss/win multipliers per strategy (no more hardcoded 2x) |
| **Rule editor** | Edit any part of existing rules, delete, add, clear, or rebuild from scratch |
| **Named presets** | Save & load strategy configs by name with `--preset` |
| **Rate-limit aware** | Reads `x-ratelimit-remaining` header; auto-pauses & resumes |
| **Smart stop conditions** | Max profit, max loss, max bets, max wins, min balance floor |
| **Safety cap** | Never bets more than 20% of current balance in one go |
| **Session database** | SQLite log of every bet and session — survives restarts |
| **Config persistence** | Save/load config with `--resume`, smart wizard with defaults |
| **Monitor mode** | `--monitor` to attach a live TUI to a running daemon — pause/resume/stop remotely |
| **Daemon mode** | `--daemon` for background running, `--status` to check, `--stop` to halt |
| **Performance tracking** | BPS, BPM, peak/low speed ranges, peak/low balance, best win, worst loss, avg P/L |
| **Streak distribution** | Per-session bar charts showing win/loss streak frequency |
| **Daily log rotation** | Logs at `~/.wolfbet_logs/wolfbet.log`, rotates at midnight, 30-day retention |
| **Interactive keyboard** | `P` pause, `R` resume, `H` history, `Q` quit — all from dashboard |

---

## Quick Start

### Mac / Local

```bash
# 1. Setup
python3 -m venv .venv && source .venv/bin/activate
pip install requests rich

# 2. Run (wizard guides you through config)
python3 wolfbet.py

# 3. Get your API key from:
#    https://wolfbet.com/user/settings?tab=api

# 4. Resume with saved config
python3 wolfbet.py --resume
```

### VPS

```bash
# Upload and install
scp wolfbet.py requirements.txt install.sh user@your-vps:~/
ssh user@your-vps
chmod +x install.sh && ./install.sh

# Run in tmux (survives SSH disconnect)
tmux new -s wolfbet
python3 wolfbet.py
# Detach: Ctrl+B then D
# Re-attach: tmux attach -t wolfbet
```

---

## Dashboard

The dashboard uses pure ANSI rendering (no Rich flicker) — 10 pinned rows + adaptive recent bets. Fits any terminal.

```text
─── WolfBet v2.4  #3  00:12:34  ● LIVE ──────────────────────────
 BAL  0.00845321 USDT  PnL +0.00012500  WAG 0.00832100  WR 51.2%
 Bets 2042  W 1046  L 996  Str W+4  Best W+11/L-8
 Bet 0.00010000  Hi 0.00640000  Martingale  2.0x  BPS 5.2  BPM 312  RL 211/480  Speed 4-7/s 240-420/m
 Peak 0.00860000  Low 0.00800000  BestW +0.00009800  WorstL -0.00640000  Avg +0.00000006  Bal +0.00012500
 P/L ▃▅▇▆▄▅▇█▆▅▃▄▆▇
──────────────────────────────────────────────────────────────────
     #  Time      Amount          Roll  W/L   P/L              Balance
  2042  14:22:01  0.00010000     61.20   W   +0.00009800     0.00845321
  2041  14:22:00  0.00010000     23.40   L   -0.00010000     0.00835521
──────────────────────────────────────────────────────────────────
 W WIN | Roll: 61.20 | P/L: +0.00009800      [P]ause [R]esume [Q]uit [H]istory
```

---

## Strategies

| # | Name | How it works | Risk |
|---|---|---|---|
| 1 | **Flat Bet** | Same amount every time | Very Low |
| 2 | **Martingale** | Multiply on loss, reset on win (configurable multiplier) | High |
| 3 | **Anti-Martingale** | Multiply on win, reset on loss (configurable multiplier) | Medium |
| 4 | **D'Alembert** | +1 unit on loss, -1 on win | Low-Medium |
| 5 | **Paroli (3-step)** | Multiply on win up to 3x then reset (configurable multiplier) | Medium |
| 6 | **WolfRider** | Multiply on loss, reset on win + switch over/under on bet 2/6/9 | High |
| 7 | **Delay Martingale** | Flat for N consecutive losses, then start multiplying | High |
| 8 | **Rule-Based** | Build custom IF/THEN conditions & actions (see below) | Variable |

> Strategies 2, 3, 5, 6, 7 now let you customize the loss/win multiplier in the wizard (e.g. 2.01x, 1.5x, 3x).
>
> The built-in 20% balance safety cap limits runaway bets, but always set a **Max Loss** stop condition.

---

## Rule-Based Strategy (Strategy 8)

Build custom rules interactively using the wizard. Each rule is an **IF condition → THEN action** pair.

### Conditions

| Type | Options | Description |
|---|---|---|
| **Sequence** | Every N / Every streak of N / Streak above N / Streak below N | Trigger on win/loss/bet counts or streaks |
| **Profit** | On profit / loss / balance | Compare with `>=`, `>`, `<=`, `<` against a value |
| **Bet** | On bet amount / number / win chance / payout | Compare with `>=`, `>`, `<=`, `<` against a value |

### Actions

| Action | Description |
|---|---|
| Reset bet amount | Reset to base bet |
| Increase amount by % | Multiply current bet (e.g. 101% = ×2.01) |
| Decrease amount by % | Reduce current bet by percentage |
| Add to amount | Add a fixed value to bet |
| Deduct from amount | Subtract a fixed value from bet |
| Set amount | Set bet to an exact value |
| Switch over/under | Toggle bet direction |
| Stop betting | Stop the bot |
| Set win chance | Set exact win chance (recalculates multiplier) |
| Increase/Decrease win chance by % | Adjust win chance relatively |
| Reset game | Full reset (bet + streak counters) |

### Rule Management

When you have existing rules, the wizard shows:
```
  Current rules (6):
    1. Every 1 win(s) → Reset bet amount
    2. Every 1 loss(s) → Increase amount by 101%
    ...

    [1] Add rule   [2] Edit rule   [3] Delete rule   [4] Clear all   [5] Continue
```

**Editing a rule** shows each part as a numbered menu — change just the condition type, mode, value, event, or action without recreating the whole rule. You can also **Rebuild from scratch** from within the editor.

### Example

A Martingale variant with direction switching:
- Every 1 win → Reset bet amount
- Every 1 loss → Increase amount by 101%
- Every 500 wins → Stop betting
- Every 2 bets → Switch over/under
- Every 6 bets → Switch over/under
- Every 9 bets → Switch over/under

---

## Named Presets

Save your strategy configuration and reuse it instantly:

```bash
# During wizard: Step 6 asks to save as preset.

# List saved presets
python3 wolfbet.py --list-presets

# Run with a saved preset (skips wizard)
python3 wolfbet.py --preset my_strategy
```

Presets are stored in `~/.wolfbet_presets.json`.

---

## Stop Conditions

Configure during setup wizard:

- **Max profit** — stop when session profit reaches X

- **Max loss** — stop when session loss exceeds X
- **Max bets** — stop after N bets
- **Max wins** — stop after N wins
- **Min balance** — stop if balance drops below X

---

## CLI Options

```bash
python3 wolfbet.py                 # Interactive wizard + TUI dashboard
python3 wolfbet.py --resume        # Skip wizard, reuse saved config
python3 wolfbet.py --preset X      # Load preset X and start
python3 wolfbet.py --daemon        # Run in background (no TUI, implies --resume)
python3 wolfbet.py --monitor       # Attach live TUI to running daemon
python3 wolfbet.py --status        # Check running session status (one-shot)
python3 wolfbet.py --stop          # Stop a running daemon
python3 wolfbet.py --setup-only    # Run wizard, save config, don't start
python3 wolfbet.py --list-presets  # Show saved presets
python3 wolfbet.py --stats         # Show all session history + totals
python3 wolfbet.py --last-bets N   # Show last N bets across sessions
python3 wolfbet.py --session-bets N  # Full stats + streak distribution for session N
```

---

## Keyboard Controls

| Key | Action |
|---|---|
| `P` | Pause betting |
| `R` | Resume betting |
| `H` | Show session history screen |
| `Q` | Stop bot and save session |
| `Ctrl+C` | Emergency stop (also saves) |
| `Ctrl+\` | Emergency stop (also saves) |

---

## Rate Limits

WolfBet allows **480 requests per minute**. The bot:

- Reads `x-ratelimit-remaining` from every response header
- When it drops to ≤2, enters **automatic 62-second cooldown**
- Shows the countdown in the status bar
- Reports live **bets/minute** so you can monitor throughput

---

## Files & Data

| Path | Purpose |
|---|---|
| `wolfbet.py` | Main bot (single file) |
| `~/.wolfbet_autobot.db` | SQLite database (sessions + bets) |
| `~/.wolfbet_autobot.json` | Saved config for `--resume` |
| `~/.wolfbet_presets.json` | Named strategy presets |
| `~/.wolfbet_logs/wolfbet.log` | Daily rotating log (30 days) |
| `~/.wolfbet_autobot_live.json` | Live state for `--status` |
| `~/.wolfbet_autobot.pid` | PID file for `--stop` |

```bash
# Query session history
sqlite3 ~/.wolfbet_autobot.db "SELECT * FROM sessions ORDER BY id DESC LIMIT 5;"

# Bet stats
sqlite3 ~/.wolfbet_autobot.db "SELECT state, COUNT(*), SUM(profit) FROM bets GROUP BY state;"
```

---

## API

| Endpoint | Purpose |
|---|---|
| `GET /user/balances` | Fetch current balance on startup |
| `POST /bet/place` | Place each dice bet |

Base URL: `https://wolfbet.com/api/v1`

---

## Disclaimer

This software is provided as-is for educational purposes. Gambling involves real financial risk. Never bet more than you can afford to lose. Use the stop conditions. Be responsible.

# WolfBet AutoBot v2.0

High-speed auto-betting engine for WolfBet Dice with a live, ultra-compact terminal dashboard. Runs on any VPS or local machine — leave it running in tmux and check stats any time.

---

## Features

| Feature | Details |
|---|---|
| **Ultra-compact TUI** | Borderless dashboard, fits any terminal size, refreshes 2x/sec |
| **7 betting strategies** | Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, Custom, Rule-Based |
| **Rule-based engine** | Define custom rules: `onEvery1loseIncrease amount by 101%` |
| **Named presets** | Save & load strategy configs by name with `--preset` |
| **Rate-limit aware** | Reads `x-ratelimit-remaining` header; auto-pauses & resumes |
| **Smart stop conditions** | Max profit, max loss, max bets, max wins, min balance floor |
| **Safety cap** | Never bets more than 20% of current balance in one go |
| **Session database** | SQLite log of every bet and session — survives restarts |
| **Config persistence** | Save/load config with `--resume`, smart wizard with defaults |
| **Daemon mode** | `--daemon` for background running, `--status` to check, `--stop` to halt |
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

The dashboard is borderless and ultra-compact — 7 fixed rows + adaptive recent bets. Fits even in small terminals.

```
─── WolfBet v2.0  #3  00:12:34  ● LIVE ──────────────────────────
 BAL  0.00845321 USDT  PnL +0.00012500  WAG 0.00832100  WR 51.2%
 Bets 2042  W 1046  L 996  Str W+4  Best W+11/L-8
 Bet 0.00010000  Hi 0.00640000  Martingale  BPM 312  RL 211/480
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
| 2 | **Martingale** | Double on loss, reset on win | High |
| 3 | **Anti-Martingale** | Double on win, reset on loss | Medium |
| 4 | **D'Alembert** | +1 unit on loss, -1 on win | Low-Medium |
| 5 | **Paroli (3-step)** | Double on win up to 3x then reset | Medium |
| 6 | **Custom** | You set win_mult and loss_mult | Variable |
| 7 | **Rule-Based** | Define custom rules (see below) | Variable |

> The built-in 20% balance safety cap limits runaway bets, but always set a **Max Loss** stop condition.

---

## Rule-Based Strategy

Define custom rules using the format: `onEvery<N><trigger><Action> [params]`

**Triggers:** `win`, `lose`, `bet`

**Actions:**
- `Reset amount` — reset bet to base amount
- `Stop` — stop the bot
- `Increase amount by X%` — increase bet by X% (e.g., 101% = multiply by 2.01)
- `Switch over/under` — toggle bet direction

**Example rules:**
```
onEvery1winReset amount
onEvery1loseIncrease amount by 101%
onEvery100000winStop
onEvery2betSwitch over/under
onEvery6betSwitch over/under
onEvery9betSwitch over/under
```

This creates a Martingale variant that increases 101% on every loss, resets on every win, stops after 100k wins, and switches direction on bets 2, 6, and 9 (and their multiples).

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
python3 wolfbet.py              # Interactive wizard + TUI dashboard
python3 wolfbet.py --resume     # Skip wizard, reuse saved config
python3 wolfbet.py --preset X   # Load preset X and start
python3 wolfbet.py --daemon     # Run in background (no TUI)
python3 wolfbet.py --status     # Check running session status
python3 wolfbet.py --stop       # Stop a running daemon
python3 wolfbet.py --list-presets  # Show saved presets
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

# WolfBet AutoBot v2.6 + Telegram Bot v1.6.0

High-speed auto-betting engine for WolfBet Dice with a live dashboard + Telegram bot. Runs on any server or local machine — leave it running and manage via CLI or Telegram.

---

## Architecture

```text
wolfbet/
├── wolfbet.py          CLI bot (TUI dashboard, wizard, monitor)
├── core/               Shared package (single source of truth)
│   ├── strategy.py     Strategies, rule engine, StrategyRule class
│   ├── database.py     DB schema, migrations, WAL connections
│   └── engine.py       Strategy computation, rule evaluation
├── tg/                 Telegram bot package
│   ├── bot.py          Entry point, command registration
│   ├── handlers.py     All /command handlers
│   ├── engine.py       Per-user BettingEngine (uses core/)
│   ├── database.py     Per-user DB/config paths
│   ├── formatter.py    Telegram message formatting
│   ├── config.py       Env-based configuration
│   └── strategy.py     Re-exports from core/
├── install.sh          Server installer (systemd, wolfctl, venv)
├── .env.example        Environment variable template
└── requirements.txt    Python dependencies (includes casino-shared)
```

Both the CLI bot and Telegram bot import shared logic from `core/` — change once, applies everywhere. Cross-bot shared code (`shared/`) is installed as a pip package (`casino-shared`) from GitHub.

---

## Features

| Feature | Details |
|---|---|
| **Pure ANSI TUI** | Direct ANSI rendering, fits any terminal, refreshes 2x/sec |
| **Telegram bot** | Full control via Telegram — start/stop/pause/status/config |
| **8 betting strategies** | Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, WolfRider, Delay Martingale, Rule-Based |
| **5 rule condition modes** | Every N, Every streak of N, First streak of N, Streak above/below N |
| **21 rule actions** | Full Stake-matching action set: bet amount, win chance, payout multiplier, switch, stop, reset |
| **Named presets** | Save & load strategy configs by name |
| **Env-based config** | API key, paths, Telegram token — all via environment variables |
| **Secure file handling** | Config files written with 0600 permissions, API keys masked in logs |
| **Smart rate recovery** | Exponential probe (2s→4s→8s→32s) instead of hard 60s freeze |
| **Session database** | SQLite log of every bet and session — survives restarts |
| **Auto data retention** | Old bet records auto-purged after 3 days; session stats preserved forever |
| **Daemon + monitor** | `--daemon` for background, `--monitor` to attach live TUI |
| **Performance tracking** | BPS, BPM, peak/low speed, peak/low balance, best win, worst loss |
| **Streak distribution** | Per-session bar charts showing win/loss streak frequency |
| **Daily log rotation** | Logs at `~/.wolfbet_logs/`, rotates at midnight, 30-day retention |

---

## Quick Start

### Local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run wizard (guides you through config)
python3 wolfbet.py

# Get your API key from: https://wolfbet.com/user/settings?tab=api
```

### Remote Server

```bash
# Upload files
scp -r wolfbet.py requirements.txt install.sh core/ tg/ .env.example user@your-server:~/wolfbet/

# Install (sets up venv, systemd, wolfctl)
# shared/ library is auto-installed via pip from GitHub (requires SSH key)
ssh user@your-server
cd ~/wolfbet && chmod +x install.sh && ./install.sh
```

### wolfctl Management

```bash
wolfctl setup           # Run wizard to configure
wolfctl start           # Start CLI bot as daemon
wolfctl stop            # Stop CLI bot
wolfctl monitor         # Attach live TUI to running daemon
wolfctl status          # Check running session status
wolfctl stats           # Show all-time session statistics

wolfctl tg env          # Edit .env (set Telegram token)
wolfctl tg start        # Start Telegram bot
wolfctl tg stop         # Stop Telegram bot
wolfctl tg logs         # Stream Telegram bot logs

wolfctl start-all       # Start both CLI + Telegram bots
wolfctl stop-all        # Stop both bots
wolfctl status-all      # Check all services
```

---

## Telegram Bot

The Telegram bot provides full betting control from your phone. Each Telegram user gets isolated storage (DB, config, presets).

### Setup

1. Open Telegram → search **@BotFather** → `/newbot`
2. Choose a name and username → copy the token
3. On your server: `wolfctl tg env` → set `WOLFBET_TG_TOKEN=<token>`
4. `wolfctl tg start`
5. Open your bot in Telegram → `/start`

Commands auto-register on startup (20 commands shown in the `/` menu).

### Commands

| Command | Description |
|---|---|
| `/start` | Welcome and quick start guide |
| `/help` | Full command reference |
| `/setkey` | Set WolfBet API key |
| `/balance` | Check balances |
| `/config` | View current config |
| `/set <param> <value>` | Set a parameter |
| `/strategies` | List available strategies |
| `/bet` | Start betting session (up to 5 concurrent) |
| `/stop [slot\|all]` | Stop session — `/stop 2` or `/stop all` |
| `/pause [slot\|all]` / `/resume [slot\|all]` | Pause/resume betting |
| `/status [slot]` | Live status — summary when multiple sessions |
| `/monitor [slot] [interval]` | Auto-updating status (3-60s) |
| `/stats` | Session history |
| `/session <id>` | Full session report with config snapshot + streaks |
| `/lastbets [N]` | Recent bets |
| `/rules` | List current rules |
| `/addrule <JSON>` | Add a rule |
| `/delrule <N>` | Delete rule by number |
| `/editrule <N> <JSON>` | Edit rule by number (JSON patch) |
| `/clearrules` | Clear all rules |
| `/presets` | List presets |
| `/savepreset <name>` | Save current config |
| `/loadpreset <name>` | Load a preset |
| `/cleanup [days]` | Purge old bet records (default 3 days), keep session stats |
| `/delsession <id>` | Delete a specific session and its bets |

---

## Environment Variables

All configuration can be set via environment variables (recommended for servers):

| Variable | Default | Description |
|---|---|---|
| `WOLFBET_API_KEY` | *(none)* | API Bearer token (overrides saved config) |
| `WOLFBET_API_BASE` | `https://wolfbet.com/api/v1` | API base URL |
| `WOLFBET_DB_PATH` | `~/.wolfbet_autobot.db` | SQLite database path |
| `WOLFBET_CONFIG_PATH` | `~/.wolfbet_autobot.json` | Saved config path |
| `WOLFBET_LOG_DIR` | `~/.wolfbet_logs` | Log directory |
| `WOLFBET_TG_TOKEN` | *(none)* | Telegram bot token |
| `WOLFBET_TG_DATA` | `~/.wolfbot_tg` | Per-user Telegram data dir |

---

## Strategies

| Nr | Name | How it works | Risk |
| --- | --- | --- | --- |
| 1 | **Flat Bet** | Same amount every time | Very Low |
| 2 | **Martingale** | Multiply on loss, reset on win | High |
| 3 | **Anti-Martingale** | Multiply on win, reset on loss | Medium |
| 4 | **D'Alembert** | +1 unit on loss, -1 on win | Low-Medium |
| 5 | **Paroli (3-step)** | Multiply on win up to 3x then reset | Medium |
| 6 | **WolfRider** | Multiply on loss + switch over/under on bet 2/6/9 | High |
| 7 | **Delay Martingale** | Flat for N losses, then multiply | High |
| 8 | **Rule-Based** | Custom IF/THEN conditions & actions | Variable |

---

## Rule-Based Strategy (Strategy 8)

### Condition Modes

| Mode | Behavior |
|---|---|
| **Every N** | Fires at every Nth total win/loss/bet |
| **Every streak of N** | Fires at N, 2N, 3N… within a streak (modulus) |
| **First streak of N** | Fires once when streak first hits N (resets on flip) |
| **Streak above N** | Fires every bet past N in streak |
| **Streak below N** | Fires when streak is below N |

### Actions

**Bet Amount:** Reset, Increase by %, Decrease by %, Add fixed, Deduct fixed, Set exact
**Win Chance:** Reset, Set, Increase by %, Decrease by %, Add, Deduct
**Payout:** Reset, Set, Increase by %, Decrease by %, Add, Deduct
**Other:** Switch over/under, Stop, Reset game

---

## CLI Options

```bash
python3 wolfbet.py                   # Interactive wizard + TUI
python3 wolfbet.py --resume          # Skip wizard, reuse saved config
python3 wolfbet.py --preset X        # Load preset X and start
python3 wolfbet.py --daemon          # Background (no TUI, implies --resume)
python3 wolfbet.py --monitor         # Attach live TUI to running daemon
python3 wolfbet.py --status          # Check running session status
python3 wolfbet.py --stop            # Stop a running daemon
python3 wolfbet.py --setup-only      # Wizard only, don't start
python3 wolfbet.py --stats           # Session history + totals
python3 wolfbet.py --last-bets N     # Last N bets across sessions
python3 wolfbet.py --session-bets N  # Full stats + streaks for session N
python3 wolfbet.py --verbose         # Enable DEBUG logging
```

---

## Disclaimer

This software is provided as-is for educational purposes. Gambling involves real financial risk. Never bet more than you can afford to lose. Use the stop conditions. Be responsible.

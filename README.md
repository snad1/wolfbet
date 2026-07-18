# WolfBet AutoBot v2.27.0 + Telegram Bot v1.16.0

High-speed auto-betting engine for WolfBet with multi-game support (Dice, Dice Manual, Limbo, Dice-Auto, Limbo-Auto), per-session proxy routing, async I/O, live TUI dashboard, session summary + exports, backtest / replay mode, lifetime analytics, and a Telegram bot with full feature parity. Runs on any server or local machine — leave it running and manage via CLI or Telegram.

---

## Architecture

```text
wolfbet/
├── wolfbet.py            CLI bot (TUI dashboard, wizard, monitor, backtest, analytics)
├── core/                 Shared package (single source of truth)
│   ├── strategy.py       Strategies, rule engine, StrategyRule class
│   ├── database.py       DB schema, migrations, WAL connections
│   ├── engine.py         Strategy computation, rule evaluation
│   └── games.py          Shared game registry (endpoints, hashing, auto-bet config)
├── tg/                   Telegram bot package
│   ├── bot.py            Entry point, command registration
│   ├── handlers.py       All /command handlers
│   ├── engine.py         Per-user BettingEngine (uses core/)
│   ├── database.py       Per-user DB/config paths
│   ├── formatter.py      Telegram message formatting
│   ├── config.py         Env-based configuration
│   └── strategy.py       Re-exports from core/
├── tests/                Regression suite (196 tests, < 300 ms)
├── audits/               Parity audits (cli-vs-tg-parity.md)
├── CHANGELOG.md          Per-version history (source of truth)
├── install.sh            Server installer (systemd, wolfctl, venv)
├── .env.example          Environment variable template
└── requirements.txt      Python dependencies (includes casino-shared)
```

Both the CLI bot and the Telegram bot import shared logic from `core/` (including `core/games.py` — Phase 6 unified the game registry, auto-bet payload builder, and API hash). Cross-bot shared code (`shared/`) is installed as a pip package (`casino-shared`) from GitHub.

---

## Features

| Feature | Details |
|---|---|
| **Rich TUI dashboard** | Live panels — session state, streaks, observability metrics, rate-limit status |
| **Telegram bot** | Full control — start/stop/pause/status/config/tweak/export/rules/presets |
| **Multi-game** | Dice (client-hash), Dice Manual, Limbo, **Dice-Auto**, **Limbo-Auto** (server-side) |
| **8 betting strategies** | Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, WolfRider, Delay Martingale, Rule-Based |
| **5 rule condition modes** | Every N, Every streak of N, First streak of N, Streak above/below N |
| **21 rule actions** | Bet amount, win chance, payout multiplier, switch, stop, reset |
| **Named presets** | Save & load full configs by name (SQLite-backed, `0600` file perms) |
| **Live hotkeys** | `P`ause · `R`esume · `T`weak · `E`xport · `H`istory · `Q`uit — mid-session |
| **Runtime tweak grammar** | 22 tunable params via `T` hotkey or `/tweak` — instant vs auto-restart semantics |
| **Advanced session tuning** | Recurring restart, streak delays, streakbet-on-loss, dynamic basebet_pct, milestones |
| **Session summary panel** | End-of-session recap: profit, wagered, ROI, streaks, top wins/losses |
| **Session exports** | `--export` (CSV/JSON) or `E` hotkey — per session or all sessions |
| **Backtest / replay** | `--simulate N` (random, seedable) or `--replay-session ID` — zero API calls, zero real balance risk |
| **Lifetime analytics** | `--analytics` — overall + by-game + by-strategy + top 5 best/worst sessions |
| **Safety warnings** | 8 heads-up categories at wizard exit (aggressive Martingale, missing max_loss, runaway recurring, …) |
| **Observability metrics** | API p50/p99 timing, cycle_ms EMA, betting efficiency %, rate-limit line |
| **Smart rate recovery** | Exponential probe (2s → 4s → 8s → 32s) instead of hard 60s freeze |
| **Session database** | SQLite log of every bet and session — WAL mode, survives restarts |
| **Auto data retention** | Old bet records auto-purged after 3 days; session stats preserved forever |
| **Daemon + monitor** | `--daemon` for background, `--monitor` to attach live TUI |
| **Secure file handling** | Config, presets, live state all written with `0600` perms; API keys masked in logs |
| **Daily log rotation** | Logs at `~/.wolfbet_logs/`, rotate at midnight, 30-day retention |

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
# shared/ library is auto-installed via pip from GitHub (public repo, no SSH key needed)
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

**Config + tools** (v2.25.1 — parity with `python3 wolfbet.py --<flag>`):

```bash
wolfctl config-show                     # dump current config (API key masked)
wolfctl config-set maxprofit=0.005 delay=0.35   # non-interactive tweak
wolfctl pipeline 3                      # set pipeline depth (Flat-only)

wolfctl analytics                       # lifetime aggregates + top 5
wolfctl last-bets 20                    # last N bets across sessions
wolfctl export /tmp/out.csv [SID]       # CSV/JSON export (per session or all)

wolfctl backtest --simulate 10000 --seed 42     # backtest (no API calls)
wolfctl preset martingale-safe          # load preset + start daemon
wolfctl cleanup [DAYS]                  # purge old bet rows (default: state.purge_days)
wolfctl benchmark [N]                   # API-latency probe (no bets, default N=20)
wolfctl diagnose                        # analyse running session bottleneck
wolfctl version                         # print CLI + wolfctl versions
```

### Non-interactive config

Inspect or mutate the saved config without walking the wizard:

```bash
python3 wolfbet.py --config-show                        # dump current config as a table
python3 wolfbet.py --config-set maxprofit=0.005 delay=0.35
python3 wolfbet.py --config-set pipeline=3 recurring=on
```

- `--config-show` reads `~/.wolfbet_autobot.json`, renders a Rich table (Field / Value / Description), and prints file path + mode. API key is masked.
- `--config-set KEY=VALUE [KEY=VALUE …]` mutates one or more fields, then persists via `save_config()`. Aliases are the same as the `T` hotkey grammar (`delay` → `bet_delay`, `maxprofit` → `max_profit`, `pipeline` → `pipeline_depth`, …).
- **`api_key=…` is refused.** Credentials belong in the `WOLFBET_API_KEY` env var, not shell history.
- Unknown fields and malformed pairs exit non-zero with a clear error.

### First real run

1. `python3 wolfbet.py` — the wizard walks you through currency → game → strategy → base bet → multiplier → stop conditions.
2. **Step 5b (Advanced tuning)** — optional: recurring restart, streak delays, dynamic basebet_pct, milestones. Press Enter to skip any field.
3. **Safety warnings panel** — before starting, the wizard reviews your config and flags anything risky (aggressive Martingale, missing max_loss, high multiplier, …). Read it, then confirm.
4. **Dashboard** — hotkeys are shown in the footer. `P` to pause, `T` to tweak a parameter mid-session, `Q` to quit and see the summary.
5. When the session ends (stop condition hit, or you press `Q`), the summary panel appears with the full recap.

---

## Live-session hotkeys

Available while the dashboard is running:

| Key | Action |
|---|---|
| `P` | Pause betting (session stays alive; resume with `R`) |
| `R` | Resume betting after pause |
| `T` | Tweak a parameter — runtime mutation (grammar below) |
| `E` | Export the current session (CSV or JSON) |
| `H` | Show history — recent bets, session stats |
| `Q` | Quit — saves session, renders the summary panel |

---

## Runtime tweak grammar

Press `T` in the dashboard (or `/tweak <param> <value>` on Telegram). Aliases are the short form; the canonical name is what the wizard uses.

**Instant params** (change applies immediately, no restart):

| Alias | Canonical | Example |
|---|---|---|
| `delay` | `bet_delay` | `delay 0.35` |
| `bbpct` | `basebet_pct` | `bbpct 0.001` |
| `sdloss` | `streak_delay_loss` | `sdloss 3` |
| `sdwin` | `streak_delay_win` | `sdwin 2` |
| `sdbets` | `streak_delay_bets` | `sdbets 5` |
| `sbloss` | `streakbet_loss` | `sbloss 4` |
| `maxprofit` | `max_profit` | `maxprofit 0.005` |
| `maxloss` | `max_loss` | `maxloss 0.001` |
| `maxbets` | `max_bets` | `maxbets 1000` |
| `maxwins` | `max_wins` | `maxwins 100` |
| `minbalance` | `stop_on_balance` | `minbalance 0.5` |
| `msbets` | `milestone_bets` | `msbets 500` |
| `mswins` | `milestone_wins` | `mswins 50` |
| `mslosses` | `milestone_losses` | `mslosses 100` |
| `msprofit` | `milestone_profit` | `msprofit 0.001` |
| `pi` | `profit_increment` | `pi 0.0001` |
| `pt` | `profit_threshold` | `pt 0.005` |
| `recurring` | `recurring` | `recurring on` |

**Auto-restart params** (change forces a clean cycle restart):

| Alias | Canonical | Example |
|---|---|---|
| `basebet` | `base_bet` | `basebet 0.0001` |
| — | `multiplier` | `multiplier 2.01` |
| `lossmult` | `loss_mult` | `lossmult 2.0` |
| `winmult` | `win_mult` | `winmult 1.0` |

Pass `off` / `none` / `0` to clear an optional value (e.g. `maxloss off`).

---

## Advanced session tuning

Available in wizard Step 5b and via `T` at runtime:

- **Recurring restart** — when a stop condition fires, auto-open a new cycle with the same config (bounded by `max_bets` / `max_profit` for safety).
- **Recurring delay** — `recurring_delay` (alias `recdelay`) — seconds to pause between cycles (default 60, `0` = back-to-back, max 3600). Dashboard shows `"Recurring pause Ns before next cycle…"` during the pause. TG↔CLI parity.
- **Proxy** — `proxy` — HTTP/HTTPS/SOCKS5 proxy URL. Credentials in the URL are automatically masked in `config-show` and logs. `wolfctl config-set proxy=https://user:pass@host:8080`, or `proxy=off` to go direct. SOCKS5 requires `pip install requests[socks]`. Warning surfaced if you use `http://` (unencrypted).
- **Retention** — `purge_days` (alias `purgedays`, default 3, range 1-30). Old bet rows are purged at startup + hourly by a background thread; session aggregates preserved. One-shot: `wolfctl cleanup [DAYS]` / `wolfbet.py --cleanup [DAYS]`.
- **DB write mode** — `db_write_mode` (alias `dbmode`, default `async`, values `async` / `sync`). Async writer batches bet rows through a background thread + tuned PRAGMAs (`synchronous=NORMAL`, `busy_timeout=5000`, `cache_size=-32000`, `temp_store=MEMORY`) — kills the per-bet fsync cost on slow disks. `sync` is the legacy path (fresh connection + fsync per bet). Async loses at most ~32 bets on power failure (session aggregates always safe); `sync` mode preserves today's exact durability. Toggle with `wolfctl config-set dbmode=async && wolfctl restart`.

### Overriding paths under systemd

`WOLFBET_DB_PATH`, `WOLFBET_LOG_DIR`, etc. are read when `wolfbet.py` is imported. Passing them inline to `wolfctl restart` (`WOLFBET_DB_PATH=… wolfctl restart`) sends the env var to the `wolfctl` shell only — `systemctl --user restart` launches the daemon under its own environment. To override under systemd, add a drop-in:

```bash
systemctl --user edit wolfbet
# In the [Service] block:
Environment=WOLFBET_DB_PATH=/mnt/tmpfs/wolfbet.db
```

With v2.27.0's async writer + tuned PRAGMAs, the tmpfs hack shouldn't be necessary on any reasonable disk — start with `wolfctl config-set dbmode=async` first and only reach for tmpfs if the async writer's `fallback_sync` counter is non-zero in `wolfctl diagnose`.
- **Streak delays** — `streak_delay_loss`, `streak_delay_win`, `streak_delay_bets` — extra pause after N-in-a-row.
- **`streakbet_loss`** — after N losses in a row, flip base bet direction (used by WolfRider-style strategies).
- **`basebet_pct`** — dynamic base bet as % of current balance (recomputes every cycle reset).
- **Milestones** — `milestone_bets`, `milestone_wins`, `milestone_losses`, `milestone_profit` — pause + log at each checkpoint.
- **Profit-lock** — `profit_increment` + `profit_threshold` — raise floor as profit climbs.
- **Pipeline depth** — `--pipeline N` / `T pipeline N` (see below).

---

## Pipelined betting

Opt-in concurrency: place up to **N in-flight bets in parallel** for higher throughput on Flat strategies.

```bash
python3 wolfbet.py --pipeline 3           # 3 in-flight bets, Flat only
python3 wolfbet.py --resume --pipeline 4  # override the saved config
```

**How it works.** `ThreadPoolExecutor(N)` + `threading.Semaphore(N)` bound the in-flight count. Every counter mutation is gated by `state.lock`, so `total_bets` / `wagered` / `profit` / streaks stay coherent under concurrency.

**Guardrails** (mirror the Telegram bot's `_pipelined_betting_loop`):

- **Flat-only.** Sequential strategies (Martingale, Anti-Martingale, D'Alembert, Paroli, WolfRider, Delay-Martingale, Rule-Based) depend on outcome ordering — running two bets in parallel changes the next-bet calculation. Any non-Flat strategy is **hard-forced to depth 1** at dispatch time; a heads-up warning is logged and shown on the dashboard.
- **Auto-bet excluded.** `dice-auto` / `limbo-auto` are server-side single-flight (one UUID). Auto-bet games are hard-forced to depth 1.
- **Depth capped at 8.** Higher values are silently clamped.
- **Rate-limit drain.** When the exponential probe trips, the pipeline drains all in-flight workers before the cooldown starts — no worker races into the wait window.

**Dashboard.** When active, the dashboard shows: `Pipeline depth=N · in-flight=K · dispatched=M`. When the guardrail refuses a requested depth, the row shows the coerce reason instead.

**Runtime tweak.** `T pipeline 3` (or `off` / `1` to disable) — takes effect on the next loop iteration.

---

## Observability metrics

The dashboard renders these each refresh (post-v2.17.0 trimmed set):

| Row | Meaning |
|---|---|
| **BPS / BPM** | Bets per second / minute (rolling) |
| **API** | p50 / p99 request latency (rolling `deque(maxlen=500)`) |
| **Cycle** | EMA-smoothed cycle time in ms (`0.9·old + 0.1·new`) |
| **Efficiency** | (bet time) / (cycle time) — how much of the loop is real work |
| **Rate limit** | Live status — normal, probing, or backing off (with cooldown seconds) |

---

## Session summary + exports

At session end (stop condition, quit, or crash), the summary panel shows profit, wagered, ROI, wins / losses, longest streaks, best win, worst loss, and the config snapshot.

Exports:

- `E` hotkey — export the current session to CSV or JSON.
- `--export <path>` — write all sessions or one session (`--export-session-id <ID>`) to `<path>` (`.csv` or `.json` inferred from extension).
- Telegram: `/session <id>` renders the same summary; `/lastbets` and `/stats` cover the running numbers.

---

## Backtest mode

Run strategy simulations with zero API calls and zero real balance risk.

```bash
# Random simulation — 10,000 bets against a synthetic RNG
python3 wolfbet.py --backtest --simulate 10000 --preset martingale-safe

# Deterministic (repeatable) — pass a seed
python3 wolfbet.py --backtest --simulate 10000 --seed 42 --preset martingale-safe

# Replay a real historical session against your current strategy
python3 wolfbet.py --backtest --replay-session 123 --preset new-tweak
```

Guarantees:

- **`--backtest` never calls the WolfBet API.** Balance changes are simulated only.
- **`--simulate`** uses `random.Random(seed)` — same seed → same outcome sequence, every time.
- **`--replay-session`** re-runs the strategy against the actual bet outcomes recorded in `sessions/bets` for that session ID.
- Results print as a summary panel; no DB rows are written (backtests don't pollute your session history).

---

## Lifetime analytics

```bash
python3 wolfbet.py --analytics
```

Renders:

- **Overall panel** — total sessions, bets, wagered, profit, win rate, sessions in profit vs. loss, best / worst profit.
- **By game** — one row per game, ordered by profit desc.
- **By strategy** — one row per strategy, aggregate bets + profit.
- **Top 5 best sessions** — highest profit.
- **Top 5 worst sessions** — lowest profit.

Relationship to `--stats`: `--stats` shows the last 20 sessions in detail; `--analytics` aggregates *all* sessions ever recorded and groups them.

---

## Safety warnings

Before the dashboard starts, the wizard reviews your config and prints warnings for:

1. **Aggressive Martingale** — `loss_mult > 2.0` on Martingale/Delay-Martingale.
2. **Doubling without a floor** — Martingale/Delay-Martingale with no `max_loss`.
3. **Anti-Martingale without profit lock** — Anti-Martingale with no `max_profit`.
4. **Base bet vs balance** — base bet > 1 % of current balance.
5. **High multiplier** — `multiplier > 10` (win chance below 10 %).
6. **Recurring runaway** — `recurring on` with no `max_profit` and no `max_bets`.
7. **Custom rules without max_bets** — Rule-Based strategy with no bet cap.
8. **Aggressive dynamic base** — `basebet_pct >= 5 %`.

Not blocking — just a heads-up. You confirm and continue.

---

## Telegram Bot

The Telegram bot provides full betting control from your phone. Each Telegram user gets isolated storage (DB, config, presets).

### Setup

1. Open Telegram → search **@BotFather** → `/newbot`
2. Choose a name and username → copy the token
3. On your server: `wolfctl tg env` → set `WOLFBET_TG_TOKEN=<token>`
4. `wolfctl tg start`
5. Open your bot in Telegram → `/start`

Commands auto-register on startup.

### Commands

| Command | Description |
|---|---|
| `/start` · `/help` | Welcome + full command reference |
| `/setkey` | Set WolfBet API key |
| `/balance` | Check balances |
| `/config` · `/set <param> <value>` | View / set a parameter |
| `/tweak <param> <value>` | Runtime mutation (same grammar as CLI `T`) |
| `/strategies` | List available strategies |
| `/bet` · `/stop [slot\|all]` | Start / stop session (up to 5 concurrent) |
| `/pause [slot\|all]` · `/resume [slot\|all]` | Pause / resume |
| `/status [slot]` · `/monitor [slot] [interval]` | Live status (auto-updating 3-60 s) |
| `/stats` · `/session <id>` | Session history / full session report |
| `/lastbets [N]` | Recent bets |
| `/export <fmt> [session_id]` | Export CSV / JSON (all or one session) |
| `/rules` · `/addrule <JSON>` · `/delrule <N>` · `/editrule <N> <JSON>` · `/clearrules` | Rule management |
| `/presets` · `/savepreset <name>` · `/loadpreset <name>` | Preset management |
| `/cleanup [days]` · `/delsession <id>` | DB maintenance |

---

## Environment Variables

All configuration can be set via environment variables (recommended for servers):

| Variable | Default | Description |
|---|---|---|
| `WOLFBET_API_KEY` | *(none)* | API Bearer token (overrides saved config) |
| `WOLFBET_API_BASE` | `https://wolfbet.com/api` | API base URL |
| `WOLFBET_DB_PATH` | `~/.wolfbet_autobot.db` | SQLite database path |
| `WOLFBET_CONFIG_PATH` | `~/.wolfbet_autobot.json` | Saved config path |
| `WOLFBET_STATE_PATH` | `~/.wolfbet_autobot_live.json` | Live-state snapshot (monitor reads this) |
| `WOLFBET_PID_PATH` | `~/.wolfbet_autobot.pid` | Daemon PID file |
| `WOLFBET_PRESET_PATH` | `~/.wolfbet_presets.json` | Named preset store |
| `WOLFBET_LOG_DIR` | `~/.wolfbet_logs` | Log directory (daily rotation, 30-day retention) |
| `WOLFBET_TG_TOKEN` | *(none)* | Telegram bot token |
| `WOLFBET_TG_DATA` | `~/.wolfbot_tg` | Per-user Telegram data dir |
| `APP_ENV` | `production` | `production` → user-friendly errors; `testing` / `dev` / `staging` → full technical details |

---

## Strategies

| Nr | Name | How it works | Risk |
| --- | --- | --- | --- |
| 1 | **Flat Bet** | Same amount every time | Very Low |
| 2 | **Martingale** | Multiply on loss, reset on win | High |
| 3 | **Anti-Martingale** | Multiply on win, reset on loss | Medium |
| 4 | **D'Alembert** | +1 unit on loss, -1 on win | Low-Medium |
| 5 | **Paroli (3-step)** | Multiply on win up to 3× then reset | Medium |
| 6 | **WolfRider** | Multiply on loss + switch over/under on bet 2/6/9 | High |
| 7 | **Delay Martingale** | Flat for N losses, then multiply | High |
| 8 | **Rule-Based** | Custom IF/THEN conditions & actions | Variable |

**Auto-bet variant** (`dice-auto` / `limbo-auto`): the strategy is translated into WolfBet's server-side auto-bet config (`/v2/{dice,limbo}/auto/start`). WolfBet executes each bet server-side — the CLI polls status. This is throttled by WolfBet's 20-per-window rate limit; the bot backs off automatically.

**Auto-recovery** (v2.25.0): when WolfBet ends the server-side cycle with `Max nonce achieved` or `Session has ended`, the CLI **rotates the server seed** (`GET /v1/game/seed/refresh`), starts a fresh `/auto/start`, and **stays in the same CLI DB session** — one CLI session can span many WolfBet auto-cycles. Circuit breaker: three consecutive recovery failures → hard stop with a clear message.

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

**Daemon control**

| Flag | Description |
|---|---|
| *(none)* | Interactive wizard + TUI |
| `--resume` | Skip wizard, reuse saved config |
| `--daemon` | Run in background, no TUI (implies `--resume`) |
| `--setup-only` | Run wizard, save config, don't start betting |
| `--monitor` | Attach live TUI to a running daemon |
| `--status` | Show status of running session |
| `--stop` | Stop a running daemon |
| `--verbose` | Enable DEBUG logging |

**Inspection**

| Flag | Description |
|---|---|
| `--stats` | Show last 20 sessions in detail |
| `--analytics` | Lifetime analytics: overall + by-game + by-strategy + top 5 |
| `--last-bets N` | Show last N bets from database |
| `--session-bets ID` | Show bets for session `ID` |
| `--list-presets` | List saved presets |
| `--config-show` | Dump the currently saved config as a Rich table (API key masked) |
| `--config-set KEY=VALUE [KEY=VALUE …]` | Non-interactive config mutation (same grammar as the `T` hotkey; `api_key` refused) |
| `--version` | Print version and exit |

**Data export**

| Flag | Description |
|---|---|
| `--export PATH` | Export all sessions to CSV or JSON (extension inferred) |
| `--export-session-id ID` | Restrict `--export` to a single session |
| `--cleanup [DAYS]` | Purge bet rows older than DAYS (default: state.purge_days). Session aggregates preserved. |
| `--benchmark [N]` | Standalone API-latency probe (no bets placed). Default N=20, clamp [3, 200]. Reports p50/p99/mean + BPS ceiling + verdict. |
| `--diagnose` | One-shot bottleneck analysis of the running daemon (top blocker + advice). Read-only. |

**Backtest / replay**

| Flag | Description |
|---|---|
| `--backtest` | Enable backtest mode — no API calls, no real balance risk |
| `--simulate N` | Random simulation of N bets (uses `random.Random(seed)`) |
| `--seed N` | Deterministic RNG seed for `--simulate` |
| `--replay-session ID` | Replay strategy against recorded outcomes of session `ID` |
| `--preset NAME` | Load preset `NAME` before running (works with all modes) |

**Concurrency**

| Flag | Description |
|---|---|
| `--pipeline N` | Pipeline depth (1-8). Flat-only; auto-bet games and non-Flat strategies auto-coerce to 1. |

---

## File locations

| Path | Purpose | Perms |
|---|---|---|
| `~/.wolfbet_autobot.db` | SQLite DB — sessions, bets (WAL mode) | 0600 |
| `~/.wolfbet_autobot.json` | Saved wizard config | 0600 |
| `~/.wolfbet_autobot_live.json` | Live-state snapshot (monitor reads this) | 0600 |
| `~/.wolfbet_autobot.pid` | Daemon PID file | 0600 |
| `~/.wolfbet_presets.json` | Named preset store | 0600 |
| `~/.wolfbet_logs/` | Daily-rotated logs (30-day retention) | 0700 |
| `~/.wolfbot_tg/` | Per-user Telegram bot data dir | 0700 |

Every writable file is created with `0600` (owner-only read/write) — API keys and session data never leak through group-readable perms.

---

## Development / testing

```bash
# Set up
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest

# Run the full regression suite (196 tests, < 300 ms)
WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q

# Run one phase's tests
WOLFBET_ENV=testing .venv/bin/python -m pytest tests/test_phase8_analytics.py -q
```

Suite layout (`tests/`): Phase 2 helpers, Phase 3 metrics, Phase 4a presets, Phase 4b mutation grammar, Phase 5a export, Phase 5c backtest, Phase 6 shared registry, Phase 7 polish (safety warnings), Phase 8 analytics.

Parity between CLI and Telegram is tracked in [audits/cli-vs-tg-parity.md](audits/cli-vs-tg-parity.md).

---

## Version history

See [CHANGELOG.md](CHANGELOG.md) for the full per-version log. Recent milestones:

- **Phase 1-3** — CLI ↔ TG parity: auto-bet endpoints, session robustness, observability metrics.
- **Phase 4** — Named presets + runtime tweak grammar (22 params).
- **Phase 5a** — Session summary panel + CSV/JSON export.
- **Phase 5c** — Backtest / replay mode with deterministic seeds.
- **Phase 6** — Shared `core/games.py` registry (endpoints, hashing, auto-bet payload).
- **Phase 7 (v2.22.1)** — Wizard safety warnings.
- **Phase 8 (v2.23.0)** — Lifetime `--analytics` command.
- **v2.23.1** — README documentation refresh.
- **Phase 5b (v2.24.0)** — Pipelined betting: `--pipeline N` (Flat-only, ThreadPoolExecutor + Semaphore, guardrail forces depth 1 for non-Flat / auto-bet). Last outstanding CLI ↔ TG parity gap closed.
- **v2.25.0** — Auto-bet max-nonce recovery (seed rotation, circuit breaker, same-CLI-session guarantee); non-interactive config surface (`--config-show`, `--config-set`); rich `--help` with grouped examples; monitor stats (ROI %, API p50/p99 unconditional, Efficiency + Cycle unconditional, `/auto/start` quota with low-warning colour).
- **v2.25.1** — `wolfctl` refresh (v3.3) — feature parity with the Python CLI: nine new subcommands (`config-show`, `config-set`, `analytics`, `backtest`, `export`, `last-bets`, `preset`, `pipeline`, `version`). `install.sh` no longer inlines a stale v3.1 heredoc; installs the repo `wolfctl` directly. One source of truth.
- **v2.25.2** — `wolfctl update` now uses `git pull --ff-only --autostash` with a dirty-tree preflight, so local changes are safely stashed rather than blocking the pull. On failure, prints the exact recovery command (`git stash list  |  git stash pop`). Zero destructive fallbacks.
- **v2.25.3** — `recurring_delay` (TG↔CLI parity): configurable pause between recurring cycles (default 60s, `0` = back-to-back). Wizard prompt, `_apply_mutation` grammar (`recdelay` alias), config-show row, risky-config warning phrasing. `api_key` row now shows `(SET)` / `(NOT SET)` after the masked value.
- **v2.26.0** — Closes remaining TG↔CLI parity gaps. **Proxy support**: shared `_http_session` routes every API call through an optional HTTP/HTTPS/SOCKS5 proxy; credentials always masked in `config-show` and logs. **Retention**: `purge_days` config + `--cleanup [DAYS]` command + startup + hourly background purge (reuses `cleanup_old_bets` / `cleanup_live_bets` from `shared/core/database`). **wolfctl v3.4**: new `wolfctl cleanup` subcommand; `casino-shared` pip install now uses `git+https://` (no more github SSH host-key prompt on fresh hosts).
- **v2.26.1** — BPS diagnostic tools. **`--benchmark [N]`** / **`wolfctl benchmark`**: standalone API-latency probe (no bets placed) with plain-English verdict — "Network bottleneck / Rate-limited / Network fast / …". **`--diagnose`** / **`wolfctl diagnose`**: one-shot analysis of the running daemon, picks the top blocker (Network / CLI overhead / User delay) and gives targeted advice. **`--status` extension**: now surfaces `p50 / p99 / cycle_ms / efficiency%` rows that already existed in the state file but weren't printed. wolfctl v3.5. Zero behavior changes to the bet loop.
- **v2.27.0** — Async bet-writer + SQLite tuning. Kills the per-bet CLI overhead (~67 ms → ~10 ms on typical VPS SSD) via a background writer thread with a persistent tuned connection (`synchronous=NORMAL`, `busy_timeout=5000`, `cache_size=-32000`, `temp_store=MEMORY`) and batched `executemany` inserts. Bounded queue with nothing-lost sync fallback on overflow, drained on shutdown. New `db_write_mode` config (`async` default, `sync` opt-out). `--diagnose` gains a DB writer row with queue depth + fallback counter + advice.

---

## Disclaimer

This software is provided as-is for educational purposes. Gambling involves real financial risk. Never bet more than you can afford to lose. Use the stop conditions. Be responsible.

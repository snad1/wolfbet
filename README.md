# WolfBet AutoBot v2.32.0 + Telegram Bot v1.20.0

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
wolfctl monitor [K]     # Attach live TUI to running daemon (lane K, default 1)
wolfctl lanes N [--split MODE]  # Run N parallel lanes (see Parallel lanes)
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
wolfctl benchmark [N] [--lanes M]       # API-latency probe (no bets, default N=20; --lanes: concurrent)
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
- **Live reload** (v2.28.3): if the daemon is running, `wolfctl config-set` sends `SIGHUP` after saving, and the daemon hot-applies safe fields (recurring, stop conditions, delays, milestones, pipeline depth, proxy, retention, …) in place. Unsafe fields (`game`, `strategy_key`, `base_bet`, `api_key`, …) print a heads-up that a full `wolfctl restart` is required. Nothing to remember — the command tells you which path it took.

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

**Monitor mode caveat** (`wolfctl monitor` / `wolfctl interactive`): the daemon runs headless (no TTY), so only `P` / `R` / `S` / `Q` are wired through signals (SIGUSR1 / SIGUSR2 / SIGTERM). `T` / `E` / `H` are foreground-TUI-only. For tweaks while a daemon session runs, use `wolfctl config-set KEY=VALUE`; for exports and history, use `wolfctl export`, `wolfctl stats`, `wolfctl analytics`.

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
| `piscope` | `profit_increment_scope` | `piscope run` |
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
- **Streak reset** — `streak_reset_losses` (alias `srloss`) + `streak_reset_delay` (alias `srdelay`, default 600 s, max 86400). After **N or more consecutive losses**, the first win ends the session, pauses `streak_reset_delay` seconds, then starts a fresh session (same reset as recurring). Fires only if session profit ≥ 0 at that win; in the red, betting just continues in the same session. Works whether or not `recurring` is on. Example: `wolfctl config set streak_reset_losses=20 streak_reset_delay=600`. TG: `/set srloss 20`, `/set srdelay 600`, `/tweak srloss 20`.
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
  - **Scope (v2.37.0).** `profit_increment_scope=session` (default) measures each session's profit, and the milestone resets every recurring / streak-reset cycle. With `run`, profit is totalled across every session since the run started, so a large threshold can be reached even with many short sessions. The run, its bumps and its total survive stop / start / restart / update. `wolfctl run-reset` starts a new run, and `wolfctl reset` also starts one. Setting `pi` or `pt` to `none`, `off` or `0` turns the increment fully off and ends the run, so nothing keeps counting in the background. Turning it back on starts a new run. Each lane has its own run. `wolfctl status` shows `Run Profit` (since session #N, bumps, next milestone), and the monitor shows `RunMs`.
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

## Parallel lanes

Pipeline is **one** session with several bets in flight. Lanes are **several independent sessions** running side by side, and they work with any strategy and with `dice-auto` / `limbo-auto`. Each lane has its own session, Martingale chain, counters, stop conditions, pauses and recurring / streak-reset cycle, so lanes drift apart naturally.

```bash
wolfctl benchmark 20 --lanes 4   # read-only probe: throughput at 1..4 concurrent clients + recommendation
wolfctl lanes 3                  # run 3 lanes (1 = the classic single bot)
wolfctl status                   # per-lane table + total bps
wolfctl monitor 2                # live TUI for lane 2
wolfctl stats --lane 2           # only lane 2's sessions (analytics has a "By lane" table)
```

**How it runs.** Lane 1 is the usual `wolfbet.service`. Lanes 2..N are `wolfbet-lane@K.service` processes (`wolfbet.py --daemon --resume --lane K`), each with its own state / pid file (`~/.wolfbet_autobot_live.laneK.json`, `~/.wolfbet_autobot.laneK.pid`). All lanes share the config file and the stats DB. Log lines are prefixed `[LK]`. Every session row records its lane.

**Money split (`lane_split`, v2.36.0).** This setting chooses how lanes share the money. The default, `none`, uses full amounts: size `base_bet` yourself.

| `lane_split` | Base bet per lane | Lane budget guard |
|---|---|---|
| `none` (default) | full `base_bet` | off |
| `budget` | full `base_bet` | each lane may risk `balance ÷ lanes` |
| `bet` | `base_bet ÷ lanes` (and `basebet_pct ÷ lanes`) | off |
| `both` | `base_bet ÷ lanes` | each lane may risk `balance ÷ lanes` |

```bash
wolfctl lanes 2 --split bet          # or: wolfctl config-set lane_split=both
```

- **Lane budget guard.** A lane's equity is its share plus its own result across its sessions. When the next bet would exceed that equity, the lane stops with `Lane budget exceeded`. This is a loss stop with no restart, and the other lanes keep running.
- **Overrides.** A lane with its own `laneK.base_bet` override always uses that exact amount. It is never divided.
- **Saved value.** The divided bet only applies while the lanes run. The config file keeps the full `base_bet`.
- **When changes apply.** `lane_split` is shared by all lanes and applies on the next start (`wolfctl restart`).

**Per-lane overrides.** Lanes use the base config plus optional overrides:

```bash
wolfctl config-set lanes=3
wolfctl config-set lane2.base_bet=0.00001 lane3.max_wins=2
wolfctl config-set lane2.base_bet=default      # drop the override (use base)
```

`api_key`, `currency`, `game`, `proxy`, `purge_days`, `db_write_mode` and `pipeline_depth` are shared and can't be overridden. `config-show` lists the overrides. Base changes reload every running lane (SIGHUP). Lane overrides reload just that lane.

**Speed and throttling.**
- The rate-limit headers are account-wide, so every lane sees the same remaining quota. Below 10% of the quota left, each lane adds a small per-bet delay (up to 250 ms) that grows as the quota runs out. Lanes ease off together instead of all hitting the hard pause at once. A single lane never reaches this zone in normal use, so it doesn't slow down. 429 / Retry-After handling stays as the backstop.
- `/auto/start` calls (one per session per lane) are spaced across all lanes through a shared lock file (`~/.wolfbet_autostart.lock`), and lane K's first start is staggered by `(K−1)·2s`, so restarts don't collide into 429s.
- Total speed rises only while the account quota has headroom. Use `wolfctl benchmark --lanes N` and then compare total bps in `wolfctl status` at 2, 3, … lanes. Keep the count where total bps still rises and `RATE LIMIT` lines stay rare in the log.

**Changing the count** with `wolfctl lanes N` restarts the running lanes so each recomputes its share. A chain in progress is saved and ended.

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
- **v2.28.0 + TG v1.17.0** — Extract shared helpers to `core/`. Six pure-Python helpers duplicated between the CLI and TG bot (`validate_proxy`, `mask_proxy`, `classify_auto_stop_reason`, `parse_streak_delay`, `percentile`, `benchmark_verdict`) move to `core/net_helpers.py` + `core/diagnostics.py`. **TG bot fixes for free**: gains max-nonce recovery (previously fell through to generic "Bet request failed") and a proxy-mask that no longer leaks `"None"` on port-less URLs. TG `/benchmark` gains p50/p99 and a plain-English verdict line. Zero CLI behaviour change.
- **v2.28.1** — Monitor UX + systemd `Restart=` fix. Monitor footer advertises `wolfctl config-set` for tweaks + `wolfctl export/stats/analytics` for data. `S` from monitor now routes through `systemctl --user stop wolfbet` when the daemon is systemd-managed, so it stays stopped (previously systemd's `Restart=always` respawned the daemon within ~10 s). `install.sh` writes `Restart=on-failure` for CLI + TG units; `wolfctl update` idempotently migrates existing installs (`Restart=always` → `Restart=on-failure` + `daemon-reload`). Real crashes still auto-recover.
- **v2.28.2** — Restored the monitor's session-title row (v2.28.1's footer had pushed it above the viewport). `_MONITOR_FIXED_ROWS = 11` constant with a regression test locks the layout invariant. Also closed a silent-persistence gap present since Phase 2 (v2.16.0): `_CONFIG_KEYS` was missing `recurring`, `basebet_pct`, and all four `milestone_*` fields; streak-tuple fields had no round-trip in `save_config`/`load_config`. Now honestly persisted.
- **v2.28.4** — Uptime freezes during recurring pause. `session_bet_ended_at` + `session_pause_started_at` split bet-time from pause-time in `wolfctl status`, `wolfctl monitor`, and `sessions.ended_at`. Pauses no longer inflate reported session runtime.
- **v2.32.0 + TG v1.20.0** — Streak reset: `streak_reset_losses` / `streak_reset_delay` end + restart the session on the first win after N+ losses in a row (profit ≥ 0 only), with its own pause. CLI wizard, tweak (`srloss`/`srdelay`), config set/show, live reload, monitor `SR NL→Ss` segment; TG `/set`, `/tweak`, `/config`; web settings shows both keys.
- **v2.31.0 + TG v1.19.0** — Date-range analytics everywhere. CLI: `--analytics --since/--until DATE` threads the same window as `--stats` into all four analytics helpers (header reads "Range analytics (since → until)"); `wolfctl analytics` passes args through. TG: `/analytics [SINCE] [UNTIL]` (bare dates; bare UNTIL = inclusive end of day; >2 args → usage hint); header labels the window. Same window semantics as CLI — parameterized `started_at >= ? AND <= ?`. Sibling bots get CLI stats + TG analytics dates in their own repos. 14 new tests, 659 passing.
- **v2.30.9 + wolfctl v3.9** — New `wolfctl reset [--yes]`: hard statistics reset that **archives** (never deletes) the DB to a timestamped file, stops the daemon if running, clears stale live-state, and leaves config untouched — next `wolfctl start` begins fresh statistics. New `wolfctl archives` lists every archive with session counts + date spans + total size. New `--stats --since/--until DATE` date-range filtering (bare dates inclusive end-of-day; listing + totals both honor the window; limit bumps to 50 while filtering). Operator flow: `wolfctl reset` → `wolfctl start` → `wolfctl stats --since …` → `wolfctl archives`. 21 new tests, 643 passing.
- **v2.30.8** — Fixed HTTP 422 outage on `/auto/start` (root cause: stale `bet_value` on disk never re-derived from `multiplier + rule`). New `_derive_bet_value(multiplier, rule)` single-source-of-truth helper; `load_config()` auto-corrects on boot (logs a warning when it fires); `_apply_mutation({"multiplier": …})` re-derives (explicit `bet_value` override wins); wizard factored through it (DRY). Also: `api_auto_start` now includes the WolfBet response-body snippet in `state.last_error` so future 4xx errors self-diagnose via `wolfctl status` instead of log-trawling. CHANGELOG includes the immediate recovery one-liner for operators on v2.30.7 or earlier. 16 new tests, 622 passing.
- **v2.30.7** — CFG-row `Ms` shows per-window progress instead of cumulative profit. Was `profit / next_ms` — after 2 bumps that read "80%" when actually only 39% through the current window. Now `window_progress / profit_threshold` — pct resets to 0% after each bump and climbs to 100% before the next fires. Denominator is always one threshold unit. 8 new tests, 606 passing.
- **v2.30.6** — Raise arbitrary `recurring_delay` cap from 3600 s → **86400 s** (24 h). Was a TG-mirror default cap, not a technical limit — `time.sleep(any positive)` is fine, nothing downstream breaks. Real fix: `wolfctl config-set recurring_delay=10800` (3-hour recurring pause) now works instead of silently clamping to 3600 and displaying a confusing `3600 → 3600` no-op. Also fixes the class-wide silent-clamp issue on `purge_days` (1..30) and `pipeline_depth` (1..8) — new `_clamped()` helper appends `⚠ <name> clamped from <raw> to <clamped>` warnings to msgs, and `cmd_config_set` now captures + prints them (was: discarded return value). 12 new tests. 598 passing.
- **v2.30.5 + wolfctl v3.8** — Three follow-up defects surfaced by real `wolfctl doctor` runs. (A) `Efficiency: >100%` fired on every diagnose call — Recent BPS used cycle_p50 (median) while Theoretical BPS used api_mean (mean). Right-skewed traffic (few slow requests) made median < mean, so Recent > Theoretical was the default reading. Fix: new `_compute_cycle_mean` — Recent now uses cycle_mean (Little's-Law consistent, throughput ≈ 1/mean). `cycle_p50` + `cycle_p99` still displayed for tail-jitter visibility. (B) Daemon `renice -n -5` / `ionice` was erased on every `wolfctl update` (CLI auto-restart changes PID). Fix: `install.sh` writes `Nice=-5` + `IOSchedulingClass=best-effort` + `IOSchedulingPriority=0` into the systemd unit; `wolfctl update` migrates existing units. Daemon starts pre-reniced on every restart automatically. (C) `wolfctl doctor`'s `[SKIP]` block for cpupower stayed silent when governor path was entirely unreadable (VPS hides `/sys/devices/system/cpu/*`). Fix: new `elif` branch prints `[SKIP]` when `$gov` empty AND `systemd-detect-virt` reports virtualization. 10 new tests. 586 passing.
- **wolfctl v3.7** — New `wolfctl doctor` subcommand: read-only health check (DB fragmentation, journal size, CPU governor, daemon nice/ionice, load average) + prescriptive fixes grouped `[SAFE]` / `[PRIV]` / `[SKIP]`. `wolfctl doctor --fix` applies the safe subset (`cleanup 1` + `sqlite3 VACUUM` + `journalctl --vacuum-size=100M`) with per-fix confirmation (`--yes` to bypass). Privileged fixes (renice/ionice/cpupower) are prescribed as text; on QEMU-KVM VPS the governor prescription is auto-downgraded to `[SKIP]` because guests can't influence host governor. Bundles the manual runbook that dropped cycle 143 → 79 ms on the old server. 12 new tests. 576 passing.
- **v2.30.4** — Windowed cycle metrics (rolling deque + p50/p99, mirrors api_p50/p99) kill the overhead swing (46→120→46 ms was one 400 ms iteration lingering in an α=0.1 EMA). Windowed efficiency uses the same window as theoretical_max_bps, so `Actual > Theoretical` (arithmetically impossible >100% efficiency) stops. Sharpened "CLI overhead is high" advice: when DB writer + rate limits are healthy, it now says "shared-tenancy CPU/disk jitter (VPS silicon lottery), not a fixable bug" instead of misdirecting the operator. `cmd_diagnose` shows `Recent BPS`, `Efficiency %`, `Cycle: median p99 (n=N samples)`. 13 new tests. 564 passing.
- **v2.30.3** — Fix metric correctness in `wolfctl diagnose` + `wolfctl status`. Overhead was mixing a smoothed cycle EMA with the unsmoothed last-single API ms — a single slow request flipped displayed overhead 0↔peak. Now uses `_compute_api_p50()` so overhead is stable. Diagnose's cycle-breakdown line uses `≈` (not fake `=`) and adds a "Measured cycle" companion — divergence between decomposition and measured is now a real signal. `cmd_status` "BPS ceiling" was `1000/cycle_ms` — misleading for auto-games where server batches many bets per outer iteration. Both status and dashboard now use the unified `theoretical_max_bps` that diagnose uses; auto-games get a batching footnote. Theoretical max uses a rolling window (not all-time mean) so 18h sessions aren't biased by cold-start samples. Display-only, no behaviour change. 12 new tests, 551 passing.
- **v2.30.2 + wolfctl v3.6** — Ops hygiene. `wolfctl update` now (a) warns when the installed wolfctl is stale (drift detection with an `exec wolfctl update` escape hatch — surfaces the v3.1 chicken-and-egg that previously silently persisted), (b) auto-restarts the CLI daemon after refreshing wolfbet.py (was: only TG + web got restarted, so the CLI process kept the old code until manual `wolfctl restart`), and (c) the wizard banner no longer says "v2.6" — it interpolates the current `VERSION`. Server-recovery one-liner: `cd ~/wolfbet && git pull --ff-only && install -m 0755 wolfctl ~/.local/bin/wolfctl && systemctl --user restart wolfbet wolfbet-tg`. 10 new tests, 539 passing.
- **v2.30.1** — Fix profit_increment: CFG row was reading the wrong milestone field (notification tracker vs actionable target), always rendering 100% once profit crossed `profit_threshold`; and `_reset_session_for_recurring` never reset `state.next_profit_milestone`, so cycle 2+ inherited cycle 1's high-water target and couldn't fire bumps. Now `next_profit_milestone` is persisted to the state file, the CFG row reads it directly (fallback to `profit_threshold` for pre-v2.30.1 states), and each recurring cycle hunts a fresh milestone. `base_bet` still stacks across cycles. 8 new tests, 529 passing.
- **v2.30.0** — Monitor Row 6 filled with live config-progress. The blank `" P/L —"` placeholder becomes a compact `CFG` summary showing profit-milestone %, active stop conditions with `current/target (pct%)` progress, `MinBal` proximity, and `Rec on(60s)`. Green <80%, yellow ≥80%, bold-red ≥95% visual alarm. Zero new state — every value already lives in the state file. 18 new tests. 521 passing.
- **v2.29.0 + TG v1.18.0** — `profit_increment` restarts the WolfBet auto-bet UUID mid-session so `base_bet` bumps actually take effect on `limbo-auto` / `dice-auto` (previously bumped only in memory until session end). Same-session invariant preserved — one CLI session now spans multiple WolfBet auto UUIDs, one per increment tick. TG gains `/diagnose` + `/analytics` (parity with CLI), uptime pause bookkeeping (mirrors v2.28.4), and the four SQLite tuning PRAGMAs from v2.27.0. 503 tests passing.
- **v2.28.3** — SIGHUP-driven live config reload. `wolfctl config-set` now hot-reloads the running daemon for a bounded allowlist of safe fields (recurring, all stop conditions, delays, milestones, pipeline depth, proxy, retention, custom rules, streak tuples). Unsafe fields (game / strategy_key / base_bet / api_key / …) print a heads-up that `wolfctl restart` is required. Kills the "silent success then no-op" antipattern.

---

## Disclaimer

This software is provided as-is for educational purposes. Gambling involves real financial risk. Never bet more than you can afford to lose. Use the stop conditions. Be responsible.

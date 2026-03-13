# Changelog

## v1.5.4 — Resume Display Fixes (2026-03-13)

### TG Bot v1.5.4

#### Fixed

- **Worst Loss negative zero** — "Worst Loss: -0.00000000" no longer shows a spurious minus sign when loss is zero.
- **Speed 0 after resume** — BPS/BPM are now recalculated from session totals on resume instead of showing 0.

## v1.5.3 — Request Timeouts + Session Recovery (2026-03-13)

### TG Bot v1.5.3

#### Fixed

- **Request timeouts** — All API calls now use `(5s connect, 15s read)` timeout tuple to prevent indefinite hangs.
- **Automatic HTTP session recovery** — After 3 consecutive timeouts, the HTTP session is closed and recreated to recover from stale/dead connections.
- **Connection error handling** — `ConnectionError` exceptions are now caught alongside `Timeout`, preventing silent freezes from dropped TCP connections.

## v1.5.2 — Reliable Session Resume (2026-03-13)

### TG Bot v1.5.2

#### Fixed

- **Session resume on SIGTERM** — Added signal handler and atexit hook so sessions are reliably saved when the bot is stopped via `systemctl restart`. Previously `post_shutdown` wasn't always reached.

## v1.5.1 — Number Formatting + Input Parsing (2026-03-13)

### TG Bot v1.5.1

#### Improved

- **Comma-formatted number input** — `/set maxwins 36,000,000` now works. Commas are stripped from all numeric `/set` values.
- **Readable number output** — All integer stats (bets, wins, losses, streaks, session counts) now display with comma separators (e.g. `276,000` instead of `276000`).

## v1.5.0 — TG Bot: Multi-Session + Fixes (2026-03-13)

### TG Bot v1.5.0

#### Added

- **Multi-session support** — Run up to 5 concurrent betting sessions per user. Each session gets its own slot number. Use `/bet` multiple times to start additional sessions.
- **Rate limit warning** — Starting 2nd+ session shows a warning about WolfBet's 480 req/min rate limit being shared across sessions.
- **Slot-based commands** — `/stop 2`, `/status 2`, `/pause 2`, `/resume 2` to target specific sessions. Single session auto-resolves (no slot needed).
- **Bulk control** — `/stop all`, `/pause all`, `/resume all` to control all sessions at once.
- **Multi-session status summary** — `/status` shows compact overview when multiple sessions are running.
- **`/editrule <N> <json>`** — Edit an existing rule by number with a JSON patch (merge into current fields).
- **Config snapshot in session history** — `/session <id>` shows the exact strategy, rules, stops, and config used at session start.
- **Strategy-aware `/config`** — Only shows relevant fields per strategy (e.g. loss_mult for Martingale, delay_threshold for Delay Martingale, rules for Rule-Based).
- **21 rule actions** — Added all missing actions: reset/set/increase/decrease/add/deduct for win chance and payout, reset_game.
- **Zero-downtime updates** — `wolfctl update` now auto-restarts TG bot with session auto-resume. Active sessions pause, save state, and resume after restart.
- **Persistent HTTP session** — Reuses a single `requests.Session` for all API calls, reducing connection overhead and boosting BPS from ~4 to 18+.

#### Fixed

- **`/delrule`** — Now correctly deletes rules by number.
- **Milestone notifications** — Fixed silent error swallowing that prevented milestone callbacks from firing.
- **Smart quotes in `/addrule`** — Telegram auto-replaces `"` with curly quotes, breaking JSON. Now sanitized automatically.
- **Rule trigger normalization** — "lose" → "loss", "wins" → "win", "losses" → "loss", "bets" → "bet".
- **`/set basebet 0`** — Fixed falsy value override (`0` was treated as missing and defaulted to `0.0001`).
- **Insufficient balance** — Bot now stops session cleanly when API returns insufficient balance error.
- **SQLite cross-thread bug** — Fixed connection sharing across threads that caused all DB writes to silently fail.

## v2.9.0 (2026-03-12)

### Telegram Bot v1.4.0

- **Profit-based bet increment** — Auto-raise base bet as profit grows. Set with `/set profitthreshold <X>` and `/set profitincrement <Y>` (aliases: `pt`, `pi`). Base bet increases by Y every time profit crosses another X increment.
- **Configurable milestone notifications** — `/set milestonebets`, `milestonewins`, `milestonelosses`, `milestoneprofit` (aliases: `msbets`, `mswins`, `mslosses`, `msprofit`). Set to 0 to disable.
- **Milestone reasons** — Notifications now show what triggered them (e.g. "50 bets", "25 wins", "profit +0.00100000").
- **Status shows base bet** — `/status` and `/monitor` now display current base bet separately from current bet.
- **`/config` enhanced** — Shows profit increment config and milestone settings.

## v2.8.0 (2026-03-12)

### Telegram Bot v1.3.0

- **`/monitor` command** — Auto-updating live status that refreshes every N seconds (default 5s, range 3–60s). Edits the same message in place. Buttons: Stop Monitor (switch to manual refresh), Stop Session. Also launchable via inline "Monitor" button on `/status`.
- **Monitor auto-stop** — Monitor automatically stops and notifies when the session ends.
- **`/status` enhanced** — After stopping a monitor, status shows a "Monitor" button to restart auto-refresh.

## v2.7.0 (2026-03-12)

### Telegram Bot v1.2.0

- **Detailed `/status`** — Now shows balance change, avg P/L per bet, peak/low balance, best win/worst loss, speed (bps/bpm), emoji indicators (green/red circles, fire for win streaks, snowflake for loss streaks).
- **Detailed `/stop` summary** — Full session report: start-to-end balance, P/L, wagered, win rate, extremes, streaks, speed, uptime.
- **Detailed `/stats`** — Fetches all 26 columns per session (currency, multiplier, base bet, speed ranges, balance extremes). All-time totals with best/worst session, peak balance, speed stats, bets/session averages.
- **New `/session <id>` command** — Full session report with streak distribution (how many times each streak length occurred), last 20 bets table, speed ranges, balance progression, and all extremes.
- **Enhanced `/lastbets`** — Aligned table with column headers, separator, and summary line (W/L/WR/P&L).
- **Milestone notifications** — Every 100 bets now shows formatted block with emoji.
- **Uptime fix** — Fixed timestamp parsing for ISO format with `T` separator.

## v2.6.0 (2026-03-12)

### Architecture

- **Shared `core/` package** — Extracted strategy, database, and engine logic into `core/strategy.py`, `core/database.py`, `core/engine.py`. Both CLI bot and Telegram bot import from the same source of truth.
- **SOLID/DRY refactoring** — Removed ~350 lines of duplicated code from `wolfbet.py` (StrategyRule, describe_rule, cmp, load_rules_from_text, compute_next_bet, wolfrider_switch, apply_action, apply_rules, init_db, db_conn all now delegate to core/).
- **Telegram bot** — Full Telegram integration in `tg/` package with 20 commands, per-user isolation, inline keyboards, auto-command registration via `set_my_commands`.

### Security (ISO 27001)

- **Environment variables** — All paths and secrets configurable via env vars (`WOLFBET_API_KEY`, `WOLFBET_DB_PATH`, `WOLFBET_TG_TOKEN`, etc.) with sensible defaults.
- **Secure file permissions** — Config and state files written with `0o600` (owner read/write only).
- **API key masking** — Keys masked in all log output (`key[:8]...key[-4:]`).
- **Audit logging** — Config saves logged with masked key.

### New Features

- **Telegram bot commands** — `/start`, `/help`, `/setkey`, `/balance`, `/config`, `/set`, `/strategies`, `/bet`, `/stop`, `/pause`, `/resume`, `/status`, `/stats`, `/lastbets`, `/rules`, `/addrule`, `/clearrules`, `/presets`, `/savepreset`, `/loadpreset`.
- **Auto command registration** — Bot registers all 20 commands with Telegram on startup via `post_init` hook.
- **wolfctl tg** — `wolfctl tg start/stop/restart/status/logs/env` for Telegram bot management.
- **wolfctl start-all / stop-all / status-all** — Manage both CLI and Telegram bots together.
- **Unified installer** — `install.sh` v3.1 sets up CLI bot, Telegram bot, core/, venv, systemd services, and wolfctl in one step.

### Changes

- Version bump to 2.6.0.
- Removed "VPS", "Ubuntu", "Terminal Dashboard" identifiers from all files.
- `EnvironmentFile` added to CLI bot systemd service for env var support.
- `requirements.txt` now includes `python-telegram-bot>=20.0`.

## v2.5.0 (2026-03-11)

### New Features

- **Rule-Based Strategy (Strategy 8)** — Full DarkDice-inspired condition/action engine with interactive wizard.
  - 3 condition types: Sequence (every N, every streak, streak above/below), Profit (profit/loss/balance with comparisons), Bet (amount/number/winchance/payout with comparisons).
  - 12 actions: reset/increase/decrease/add/deduct/set amount, switch over/under, stop, set/increase/decrease win chance, reset game.
  - Rule management menu: add, edit, delete, clear, rebuild from scratch.
  - Part-by-part rule editor — change any individual field (condition type, mode, value, event, action) without recreating the whole rule.
- **Customizable strategy multipliers** — Strategies 2, 3, 5, 6, 7 now prompt for loss/win multiplier in the wizard instead of using hardcoded values (2.01x, 2x). Set any value you want (1.5x, 2.01x, 3x, etc.).
- **Smart rate limit recovery** — Replaced fixed 62-second cooldown with exponential probe system (2s→4s→8s→16s→32s). Probes `GET /user/balances` to check remaining capacity and resumes immediately when capacity restores. Status bar shows probe interval and remaining count.

### Enhancements

- **Detailed `--status`** — Now shows stop conditions (max profit, max loss, max bets, max wins, min balance), strategy multipliers (loss/win mult), bet delay, profit increment config, and full rule list for strategy 8. State file enriched with all config fields.

### Fixes

- **Multiplier validation** — Entering 0 or values below 1.01 now shows an error and re-prompts instead of crashing with division by zero.
- **Empty input handling** — `_ask()` with choices now loops and re-prompts when user presses Enter without a default, instead of returning an empty string that causes KeyErrors.

## v2.4.0 (2026-03-11)

### New Features

- **Monitor mode** (`--monitor`) — Attach a live TUI viewer to a running daemon session. Press `P`/`R` to pause/resume remotely (via SIGUSR1/SIGUSR2), `S` to stop the daemon, `Q` to detach without stopping.
- **Streak distribution** — `--session-bets N` now shows a full streak distribution chart (bar graph of win/loss streak lengths and their frequency).
- **`--setup-only` flag** — Run the wizard and save config without starting the bot.

### Fixes

- **Balance tracking** — Use `userBalance` (not `user_balance`) from the API response for accurate balance updates.
- **Profit calculation** — Use the API's `profit` field directly (positive on win, negative on loss) instead of manual calculation.

## v2.3.0 (2026-03-11)

### Enhancements

- **`--resume` + `--daemon` combined** — `--daemon` now implies `--resume`, no longer mutually exclusive.
- **`--status` enriched** — Shows all stats matching the dashboard: strategy details, multiplier, rule, base/current bet, highest bet, peak/low balance, best win, worst loss, avg P/L, speed ranges, current streak.
- **BPM accuracy** — Uses actual bets counted in the current minute instead of extrapolating from BPS.
- **wolfctl stats** — Uses `python3 wolfbet.py --stats` internally instead of hardcoded SQL.

## v2.2.0 (2026-03-10)

### New Features

- **Row 5 stats** — Dashboard now shows peak/low balance, best win, worst loss, avg P/L, and balance change.
- **BPS + speed range tracking** — Tracks bets-per-second (session average), peak/low BPS and BPM per individual second/minute.
- **Dedicated sparkline row** — P/L sparkline graph moved to its own row for better readability.
- **Richer `--stats`** — Session history now shows per-session detail blocks with all tracked metrics. All-time totals include best/worst session, peak balance, speed stats, bets/session averages.
- **Full datetime on bets** — `--last-bets` shows full datetime (19 chars) instead of just time.

### Changes

- Session dates now include seconds (substr 19 instead of 16).
- Start balance shown in session summary; speed range displays `0-0/m` when no data.
- DB schema extended with 10 new session columns (highest_balance, lowest_balance, highest_win, biggest_loss, bets_per_minute, bets_per_second, peak_bps, low_bps, peak_bpm, low_bpm) with automatic migration for existing databases.

## v2.1.0 (2026-03-10)

### Breaking Changes

- **Pure ANSI dashboard** — Completely replaced Rich `Table`/`Live` rendering with direct ANSI escape codes. The TUI no longer uses Rich for the main dashboard, eliminating terminal corruption issues and flickering. Rich is still used for wizard prompts and CLI output (--stats, --status, etc.).

### Changes

- Dashboard uses alternate screen buffer (`\033[?1049h`) with proper terminal state save/restore.
- Terminal cleanup on exit: restores saved termios settings, runs `stty sane`, and issues a full terminal reset (RIS).
- Fixed dashboard rendering corruption that occurred with Rich's Live refresh.

## v2.0.0 (2026-03-10)

### New Features

- **Borderless ultra-compact dashboard** — No more panels/borders eating terminal space. 7 fixed rows + adaptive recent bets. Fits any terminal size.
- **Rule-based strategy engine** (Strategy #7) — Define custom rules like `onEvery1loseIncrease amount by 101%`, `onEvery2betSwitch over/under`, `onEvery100000winStop`.
- **Named presets** — Save strategy configurations by name during wizard. Load instantly with `--preset NAME`. List with `--list-presets`.
- **Wizard colors restored** — Rich-formatted prompts with colored labels, yellow defaults, cyan step headers.

### Changes

- Dashboard uses plain Text lines instead of Panel/Layout — dramatically reduced vertical space usage.
- Wizard now has 7 steps (added preset save step).
- Version bump to 2.0.0.

## v1.3.0 (2026-03-10)

- **Compact dashboard** — 16 fixed rows + adaptive recent bets (down from 54+).
- **`--daemon` mode** — Run in background with no TUI.
- **`--status`** — Read live state file, show session summary from another terminal.
- **`--stop`** — Send SIGTERM to running daemon.
- **State file** — `~/.wolfbet_autobot_live.json` updated every 5s for monitoring.
- **PID file** — `~/.wolfbet_autobot.pid` for process management.

## v1.2.0 (2026-03-10)

- **Smart wizard** — Loads saved config as defaults, asks "use saved API key?".
- **`max_wins` stop condition** — Stop after N wins.
- **Quit fix** — Input handler catches Q, Ctrl+C, Ctrl+\ in raw terminal mode.
- **Daily logs** — `~/.wolfbet_logs/wolfbet.log` with midnight rotation, 30-day retention.
- **`bet_value` fix** — "over" uses `100 - win_chance - 0.01` (50.49 for 2x, not 50.50).
- **Defaults** — USDT currency, 2.0x multiplier, 50.49 bet_value.

## v1.1.0 (2026-03-10)

- **Config persistence** — Save/load to `~/.wolfbet_autobot.json`.
- **`--resume` flag** — Skip wizard, reuse saved config.
- **Exponential backoff** — 1s -> 2s -> 4s -> ... -> 30s cap on consecutive API errors.
- **Wizard UX** — Back navigation with "back" keyword, clearable fields with "none"/"clear", editable input fields using `input()`.

## v1.0.0

- Initial release.
- 6 betting strategies: Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, Custom.
- Live TUI dashboard with Rich panels.
- SQLite session and bet persistence.
- Rate-limit aware with auto-cooldown.
- Stop conditions: max profit, max loss, max bets, min balance.
- 20% balance safety cap.
- Interactive keyboard controls: P/R/H/Q.

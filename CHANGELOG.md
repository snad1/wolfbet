# Changelog

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

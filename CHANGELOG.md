# Changelog

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

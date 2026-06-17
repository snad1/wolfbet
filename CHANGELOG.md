# Changelog

## v2.16.3 — API peak time on the dashboard (2026-06-17)

### Added

- **`API: X last / Y avg / Z peak`** — third field tracks the slowest single API response observed this session. Today's data showed `API last 563ms` (visible only for one bet, gone the next refresh). With `peak` it's permanently recorded — directly explains why `Cycle` is elevated above `bet_delay`.

### Audit (read-only)

Reviewed every `await`/`sleep`/timing call added since v1.13.9. **No regressions.** All added awaits replace previously-blocking synchronous DB calls or are O(microsecond) timing measurements. The slower bps the user is seeing comes from elevated WolfBet API latency (avg ~200ms vs ~100ms previously) and occasional 500ms+ spikes — not from any client-side change. `Overhead: 0–2ms` confirms the engine itself is essentially free.

## v2.16.2 — Cycle metric: full per-bet time (bps reciprocal) (2026-06-17)

### Added

- **`Cycle: Xms/bet`** on the dashboard — exp-smoothed time between consecutive bet starts. This is the bps reciprocal (`Cycle × bps ≈ 1000`), shown inline as `(1000/385 = 2.60 bps)` so the relationship is explicit.

Why: the previous v2.16.1 `Overhead` metric only measures post-API processing (~2ms typical). It misses the time between `_process_bet_result` and the next iteration's pacing sleep — custom rules eval, periodic-save await, milestone callback, asyncio scheduler wake-up jitter, other engines competing for the loop. Users seeing `Overhead: 2ms` thought the engine was at peak while bps was still ~17% below theoretical. `Cycle` is the honest number — it can't lie because it IS what produces bps.

How to read it:
- `Cycle ≈ bet_delay + a few ms` → bot is at peak
- `Cycle > bet_delay + 30ms` → the gap is scheduler/loop-sharing cost (visible, not hand-waved)

`Overhead` is kept for surgical diagnosis of post-API processing.

## v2.16.1 — Per-cycle overhead diagnostic + get_status() cache (2026-06-17)

### Added

- **`Overhead: Xms/bet`** on the dashboard — exponentially-smoothed measurement of time spent per bet OUTSIDE of `bet_delay` and the API call. Answers "is something blocking the bot?" with hard data: ~5–15ms = healthy (Python + asyncio scheduler overhead); 50ms+ = something is wrong (DB lock contention, other engines stalling the loop, etc.).
- **`get_status()` cache (250ms TTL)** — milestone notifier and `/status` handlers often call `get_status()` within ms of each other; both now hit the cache. Wins ~5–10ms per milestone (≈ every 33s) — small but eliminates the brief per-second bucket dip when milestones fire.

Honest expectation: single-session bps will inch from ~2.9 to ~3.0 with `bet_delay=0.320s`; the remaining 4% gap is fundamental Python + asyncio overhead and is **not fixable** without leaving the asyncio model. Multi-session bps unchanged numerically — but `Overhead` now shows the cost of concurrency directly.

## v2.16.0 — Sliding-window BPS + non-blocking SQLite writes (2026-06-17)

### Added

- **`Now` bps (sliding 60s window)** alongside the existing cumulative `Avg`. Dashboard now shows BOTH so you can tell what the bot is doing RIGHT NOW vs. the lifetime average — answers "why is my bps dropping over a long session?" (the average drags down forever after any slow period; `Now` doesn't).

### Changed

- **DB writes no longer block the event loop.** `_flush_bets`, `_db_save_session`, and the hourly `cleanup_live_bets` now run via `asyncio.to_thread`. Previously a single `_periodic_save` could stall the event loop for 10-50ms; with 6 concurrent sessions all saving every 30s, the loop spent a large fraction of time in DB I/O. Each engine still owns its own `sqlite3.Connection`; shared `db_connect` now opens with `check_same_thread=False` (safe — engines never write concurrently to their own connection). Bet flush is now driven exclusively by `_periodic_save` (every 30s) — the inline batch-size trigger was removed because it would still block the loop. Queue holding up to ~900 rows between saves (~115 KB) is harmless.

Result: 6 concurrent sessions should approach 6× single-session bps (still subject to WolfBet's per-account rate limits), and the dashboard's `Now` number stops gaslighting you about steady state.

## v2.15.0 — Auto-revive: systemd retry-forever + external stall watchdog (2026-06-17)

### Added

- **`Restart=always` + `StartLimitIntervalSec=0`** on both CLI and TG bot services. Bot now restarts after every exit (not just non-zero), and systemd never stops retrying after N quick failures.
- **External stall watchdog timer** — A new `wolfbet-tg-watchdog.timer` fires every 2 minutes and runs a bash check: if `/tmp/wolfbet-tg.heartbeat` is missing or older than 180s, it forces `systemctl restart wolfbet-tg`. Catches the stuck-but-alive case (process running, event loop frozen) that `Restart=` can't catch.
- **Heartbeat task in the bot** — `_heartbeat_loop()` runs as an asyncio background task started from `post_init`, writing a fresh mtime to the heartbeat file every 30 seconds.

Result: if the bot crashes, systemd brings it back in 10s. If the bot stalls, the watchdog brings it back within ~3 minutes. No more SSH-and-restart.

## v2.14.13 — Watchdog: kill or force-recover hung auto-bet sessions (2026-06-17)

### Added

- **Stall watchdog for auto-bet sessions** — Tracks `_last_successful_bet_time` per engine. In the betting loop:
  - At **3 min** of no successful bet: log `Watchdog — Xs since last bet (still trying)` so you see it in logs without action.
  - At **10 min**: force a full recovery (rotate seed + stop dead UUID + start new auto-bet). If recovery succeeds, reset and continue. If recovery fails, **kill the session** with `Session abandoned — no successful bet for Xs and recovery failed` so you know which bots need manual attention instead of them silently spinning forever.

  This catches the case the user reported: 5+ minutes of network timeouts where the bot's existing timeout-recovery (recreate HTTP client) kept firing but bets never resumed. After this fix, sessions either recover or fail loudly.

## v2.14.12 — Add session-expired recovery to the regular betting loop (2026-06-13)

### Fixed

- **Recovery only worked in the pipelined loop** — `_pipelined_betting_loop` is only used when `pipeline > 1`. Rule-Based strategy forces `pipeline = 1`, so the user's auto-bet sessions ran through the regular `_betting_loop`, which had NO recovery code. Result: 56+ hours of session #321-#324 spinning on dead UUIDs, never auto-recovering. Now `_betting_loop` has the same `_session_expired` check + `_recover_expired_session` call + retry-on-failure logic as the pipelined version.

## v2.14.11 — Broader dead-session detection + recovery doesn't kill session (2026-06-12)

### Fixed

- **Recovery missed `"No query results for model [App\BetSession]."`** — Same root cause as `"Session has ended or not exists."` (server-side BetSession deleted/expired), different error message. Bot kept retrying the dead UUID forever, never triggering seed-rotate + auto-bet restart. Detection now also matches `"no query results"` and `"betsession"` substrings so all Laravel variations of "dead UUID" route through `_recover_expired_session`.
- **Recovery failure killed the session** — If `_auto_start` failed during recovery (e.g. rate-limited), the session was marked `STOPPED: Auto-bet session expired — recovery failed` and the user had to manually `/bet` again. Now recovery failure logs a warning, sleeps 5s, and the next loop iteration retries automatically. Session keeps running until recovery succeeds.

## v2.14.10 — /update cd's into repo dir before running ctl (2026-06-11)

### Fixed

- **/update failed with "No wolfbet.py in current directory"** — `systemd-run` spawns the bash command with cwd=/ (not the bot's repo dir). The `wolfctl update` script looks for `wolfbet.py` in cwd to copy. Now `cd /root/wolfbet && wolfctl update` is the bash command. Override via `BOT_REPO_DIR` env var if your layout is different.

## v2.14.9 — /update reports real errors + writes update log (2026-06-10)

### Fixed

- **/update silently failed when systemd-run errored** — Previous version used `subprocess.Popen` with `stderr=DEVNULL`, so any failure (dbus issues, transient unit conflicts, etc.) was invisible. The bot replied "Update started" even when `systemd-run` returned non-zero. Now uses `subprocess.run --no-block` synchronously, captures stdout/stderr, and surfaces real errors to Telegram. Update output goes to `/tmp/wolfctl-update.log` so you can review what happened: `cat /tmp/wolfctl-update.log`.

## v2.14.8 — Fix /seed: endpoint is GET, not POST (2026-06-10)

### Fixed

- **`/seed` returned 405 "method not supported"** — WolfBet's `/v1/game/seed/refresh` only accepts GET/HEAD, but the bot was sending POST (assumed from the "change" wording in the API docs). Changed to `await self._http.get(url)`. `/seed` Telegram command and the auto-recovery seed rotation both fixed.

## v2.14.7 — /update Telegram command for self-update (2026-06-10)

### Added

- **`/update` Telegram command (owner-only)** — Pull latest code from git, copy files, reinstall `casino-shared`, restart the bot. Equivalent to running `wolfctl update` over SSH, but triggered from Telegram. Owner gate via `TG_OWNER_ID` env var (defaults to your user ID if unset). Implementation spawns `wolfctl update` via `systemd-run --user --collect` so the update survives the bot getting killed by the restart.

## v2.14.6 — Auto-recover from "Session has ended" + /seed command (2026-06-10)

### Added

- **`/seed` Telegram command** — Manually rotate the WolfBet server seed via `/v1/game/seed/refresh`.
- **Auto-recovery on expired auto-bet session** — When auto/play returns HTTP 400 `"Session has ended or not exists."` (often happens after long-running sessions, or when WolfBet expires the UUID server-side), the engine now:
  1. Rotates the server seed via `/v1/game/seed/refresh`.
  2. Stops the dead UUID (best-effort).
  3. Starts a new auto-bet session with a fresh UUID — using the same paced restart path as profit-increment switching.
  
  Previously the bot logged the 400 repeatedly until manually stopped (observed: 4 minutes of failed requests across 4 concurrent sessions). Recovery is now automatic and transparent.

## v2.14.5 — Remove 5-session concurrency limit (2026-05-12)

### Changed

- **Unlimited concurrent sessions per user** — `MAX_SESSIONS = 5` cap removed from `/bet` and from the recurring-restart path. Users can now run as many concurrent sessions as they want (limited only by the casino's own API rate limits, which are shared across sessions).

## v2.14.4 — bet_delay is now a minimum-interval, and applies to auto-bet (2026-05-11)

### Changed

- **`bet_delay` semantics** — Previously: `sleep(bet_delay)` before each bet, so cycle = `bet_delay + api_time`. Now: minimum interval since the previous bet *started*, so cycle = `max(bet_delay, api_time)`. With delay=1s and api=50ms, you now get a clean 1 bet/sec; with api=1500ms there's no extra sleep so the bot keeps up. More predictable pace.
- **`bet_delay` now applies to auto-bet** — the pipelined auto-bet loop (Dice Auto v2 / Limbo Auto) was silently ignoring `bet_delay`. It now paces between batches with the same minimum-interval semantics.

## v2.14.3 — Auto-bet start retries on transient network errors (2026-05-06)

### Fixed

- **Session killed by HTTP timeout during auto/start** — observed log showed `Auto start exception:` (empty `str(e)`) followed by session end. Cause was an `httpx.ReadTimeout` (15s response timeout) being caught by a generic `except Exception` that bailed immediately without retrying. Now `httpx.TimeoutException` / `NetworkError` / `RemoteProtocolError` are caught and retried with the same backoff schedule as 429 (~120s budget). Exception type and `repr()` are logged so future failures aren't invisible.

## v2.14.2 — Auto-bet starts paced + probing on 429 (2026-05-06)

### Fixed

- **Profit-increment restart killed sessions when WolfBet returned 429** — `_auto_stop` followed by `_auto_start` runs in ~130ms; WolfBet caps `auto/start` at 20 per window (length undocumented). The tight stop→start often hit the cap, returning HTTP 429 ("Too Many Attempts."), which ended the session entirely. Even after a first round of fixes, sessions still died on long runs because the bot wanted more restarts/min than the cap allows (e.g. threshold=0.001 + ~13 bps yields ~43 restart attempts/min vs 20/min cap).
  - Engine enforces a **1s floor between starts**.
  - Tracks `x-ratelimit-remaining`. As quota drops, the pre-wait scales up: 0 → 6s, 1 → 4s, 2 → 2s. Spreads bursts evenly across the window.
  - On 429, retries with backoffs **1 / 2 / 3 / 5 / 10 / 15 / 25 / 60 s** (~120s total budget) — long enough to outlast even a 60s window from any starting point. Probes the actual window rather than assuming a fixed length.
  - Honors `Retry-After` when the server provides it.

## v2.14.1 — Profit increment = 0 now visible in /config and works in CLI (2026-05-06)

### Fixed

- **`/config` hid `profit_increment = 0` setting** — Telegram `/config` line guarded by `if pi and pt:` treated `0` as falsy, so users who set `profitincrement 0` (restart-only mode, no bet bump) saw nothing under their config and assumed the feature was missing. Switched to `is not None`. Same fix applied to the snapshot builder used for session DB rows and to the `/tweak` reply that confirms the change.
- **CLI bot ignored `profit_increment = 0`** — `wolfbet.py` engine, TUI, and config display all guarded on truthy `profit_increment`, so the milestone never advanced and the section never restarted when the increment was set to 0. Aligned with the Telegram engine's `is not None` check (the latter was fixed in v2.10 via commit 1d2e187).
- **`/help` clarified** — `profitincrement` line now notes that `0` means "restart only" and `off` disables.

## v2.14.0 — Recurring honors live config across all running sessions (2026-04-28)

### Fixed

- **Recurring only applied to sessions started AFTER `/set recurring on`** — Previously, `recurring_state` was registered at `/bet` time only if `config.get("recurring")` was true at that moment. If a user started a Limbo session, then later toggled recurring on and started a Dice session, only Dice would auto-restart on stop — Limbo had no recurring entry. Now `recurring_state` is registered for every session unconditionally (per-slot snapshot of game/strategy preserved). At stop time the engine re-reads the user's saved config to decide whether to actually fire the restart, so toggling `/set recurring on/off` applies instantly to **every** running session regardless of game type.
- **Recurring lost across bot restart** — `load_resume_state` now also registers `recurring_state` for each resumed session so a stop after a bot update still triggers the configured restart.
- **Notification throttle now driven by live config** — The "is this a recurring session" check used for stop-message rate-limiting now reads the live `recurring` flag instead of the snapshot, matching the new behavior.

Per-session game/strategy isolation is preserved: each session's snapshot keeps its own `game`, `strategy`, `multiplier`, `rule`, `bet_value` etc., so a Limbo restart stays Limbo and a Dice restart stays Dice — even with both running concurrently.

## v2.13.1 — Fix crash on Rule-Based rules without action_value (2026-04-21)

### Fixed

- **`could not convert string to float: ''` crash on `/bet`** — `_custom_rule_to_wolfbet` built an `action_map` dict where every value entry called `float(r.action_value)` eagerly. For rules like `reset_amount` or `switch` that don't take a value, `action_value` is `""`, which crashed the whole map construction even though the eventually-matched key didn't need the float. Hoisted the float conversion to a single `av = float(...)` with empty-string guard, then referenced `av` inside the dict — only evaluated once and only fails fast if conversion is genuinely impossible

## v2.13.0 — Rule-Based strategy now sends custom rules to WolfBet auto-bet (2026-04-21)

### Fixed

- **Rule-Based custom rules silently dropped on auto-bet sessions** — `_build_auto_config` had no branch for `strategy_key == "8"`, so user-defined custom rules fell through to the default flat fallback (only "reset on win"). The WolfBet auto-bet UI showed only that fallback rule — none of the user's actual rules executed server-side. Added a `_custom_rule_to_wolfbet` translator that converts each `StrategyRule` (from the shared rule engine) into WolfBet's auto-bet schema:
  - Sequence conditions (`every`, `every_streak`, `first_streak`, `streak_above`) map to WolfBet types `every`, `streak`, `streak`, `streakGreater`
  - Triggers `win`/`loss`/`bet` map to WolfBet's `win`/`lose`/`bet` (note WolfBet uses "lose" not "loss")
  - Actions: `reset_amount`, `increase_amount`, `decrease_amount`, `add_amount`, `deduct_amount`, `set_amount`, `switch`, `stop` all translated to their WolfBet command equivalents
  - Profit/bet-threshold conditions are not yet supported by WolfBet's auto-bet schema, so they're skipped with a warning log (and still enforced client-side via the engine's normal stop-condition checks)
  - If no rules can be translated, falls back to flat (reset on win) so the session still starts

## v2.12.3 — Real fix: WolfBet rejects "2.0" as multiplier string (2026-04-21)

### Fixed

- **"Incorrect win chance given" — true root cause was multiplier string format, not bet_value** — A real captured WolfBet payload showed the API expects `"multiplier": "2"` (no trailing zeros), not Python's default `f"{2.0}"` → `"2.0"`. WolfBet rejects "2.0" as the string and returns "Incorrect win chance given" even though `bet_value=50.49` is correct (which was always the right value — `100 - wc - 0.01` matches WolfBet's discrete `[0.00, 99.99]` roll boundary). Reverted v2.12.1's bet_value formula change. Multipliers are now formatted via a helper that strips trailing zeros: `2` not `2.0`, `2.01` kept, `2.5` kept. Applies to auto-start, manual bet, dice-manual, limbo, and the benchmark probe in /balance

### Reverted

- v2.12.1's removal of the `-0.01` term in the bet_value formula (it was correct all along)
- v2.12.2 kept the engine-level `bet_value` recompute on init (still useful — ensures consistency from multiplier even if config drifts)

## v2.12.2 — Auto-recompute bet_value to absorb stale configs (2026-04-21)

### Fixed

- **Stale `bet_value` in saved user configs persisted v2.12.1 fix never took effect** — Even after the formula fix, users whose config was saved under the old formula still had `bet_value=50.49` stored, which the engine read on every session start and sent to WolfBet, triggering the same HTTP 422. Engine now derives `bet_value` from `multiplier + rule` on init and ignores any stored value, so stale configs self-correct on the first `/bet`

## v2.12.1 — Fix "Incorrect win chance given" for whole multipliers (2026-04-21)

### Fixed

- **Multiplier 2.00 (and other whole-number multipliers) rejected with HTTP 422 "Incorrect win chance given"** — `bet_value` formula for "over" rule was `round(100 - wc - 0.01, 2)`, producing `50.49` for multiplier `2.00` (wc 49.5). WolfBet's API derives win_chance back from bet_value (`100 - 50.49 = 49.51%`) and compared it to the multiplier-derived win_chance (`99/2.00 = 49.5%`); the 0.01 mismatch failed validation. Removed the `-0.01` so `bet_value = 100 - wc` matches `99/multiplier` exactly. Affects both the manual `/set multiplier`, `/set rule` paths and the rule-engine `switch` action

## v2.12.0 — Streak Delay Bets (2026-04-21)

### Added

- **`streakdelay_bets`** — Delay every N total bets by X seconds, regardless of win/loss outcome. Format `N:seconds` (e.g. `/set streakdelay_bets 100:0.5` pauses 0.5s every 100 bets). Useful for throttling overall bet rate independent of streaks. Live-tweakable via `/tweak sdbets 100:0.5`. When multiple streak delays apply on the same bet (e.g. loss streak + bet count), the longest delay wins

## v2.11.2 — Recurring Notification Throttle (2026-04-21)

### Fixed

- **Telegram flood ban on fast recurring sessions** — Stop and start notifications are now rate-limited to at most one of each per 30 seconds per user during recurring cycles. Losses, errors, insufficient balance, and manual stops are always sent immediately regardless of throttle. This prevents WolfBet's 23 bps speed from generating 20+ messages/minute and triggering Telegram's flood ban

## v2.11.1 — Recurring Reliability Fixes (2026-04-21)

### Fixed

- **Recurring stops after N cycles** — `_notify_stop` now wraps formatting in try/except so a bad status snapshot never crashes before the recurring restart block. All fire-and-forget tasks converted from `ensure_future` to `create_task` with `add_done_callback` so exceptions are logged instead of silently swallowed
- **Recurring restart retries on transient failure** — When `engine.start()` fails during a recurring restart (e.g. transient API error), the recurring state is kept alive and a retry is scheduled after the same delay instead of permanently stopping the cycle
- **/monitor stops between recurring cycles** — Monitor loop now checks for a pending recurring restart (non-done `timer_task`) and shows "⏳ Recurring restart pending…" while waiting, then resumes live status updates once the new engine starts
- **/status Refresh button breaks during inter-session gap** — When Refresh is pressed while a recurring restart is pending, the message now shows "⏳ Recurring restart pending…" with buttons intact instead of "Session ended." (which permanently removed the inline keyboard)

## v2.11.0 — Adaptive Base Bet & Full Config Visibility (2026-04-14)

### Added

- **`basebet_pct`** — Set base bet as a fraction of current balance (e.g. `/set basebet_pct 0.001` = 0.1%). Recomputes on session start and at every profit-increment milestone, so base bet auto-shrinks during drawdowns and grows during profit. Live-tweakable via `/tweak bbpct 0.001`
- **`streakbet_loss`** — Reduce (or scale) bet after N consecutive losses. Format `N:multiplier` (e.g. `/set streakbet_loss 10:0.5` halves bet every 10 losses). Damping floor for Martingale-style strategies. Live-tweakable via `/tweak sbloss 10:0.5`

### Changed

- **Full config in every surface** — Session-start message, `/status`, and stop summary now display every active setting: streak delays, streak bet, basebet pct, profit bump + next milestone, milestone cadence, stops, pipeline, rule/value, win/loss multipliers. Single `format_full_config` helper in formatter; no more drift between surfaces

## v2.10.4 — Insufficient Balance Stop (2026-04-13)

### Fixed

- **Insufficient balance now always stops session** — Added pre-bet balance guard (`current_bet > current_balance`) and broadened non-200 HTTP response detection to catch insufficient balance across all bet paths (manual, auto-bet, pipelined). Previously, deep Martingale streaks could drive `current_bet` above balance without the session stopping when the server response format didn't match the narrow detection pattern

## v2.10.3 — Streak Delay & Recurring Bet Fixes (2026-04-09)

### Added

- **Streak delay** — Delay next bet after N consecutive wins/losses. Configure with `/set streakdelay_loss 5:1.0` (every 5 losses → 1s delay) and `/set streakdelay_win 10:0.5`. Works with all strategies including auto-bet. Live tweak with `/tweak sdloss 5:1.0` / `/tweak sdwin off`
- **Streak delay in /config** — Displays streak delay settings in configuration output

### Fixed

- **Recurring bet preserves profit increment** — When a session stops and recurring restarts, the current (profit-incremented) base_bet is now carried forward instead of resetting to the original config value. Useful for short recurring sessions to bypass server-side throttling while maintaining bet progression

## v2.10.2 — Bug Fixes (2026-04-07)

### Fixed

- **Live bet cleanup** — ISO timestamp `T` separator caused comparison failure with SQLite's `datetime()`. Bets older than purge_days now correctly deleted during running sessions
- **`/balance` command** — Missing `await` on async `api_get_balances()` caused "coroutine object is not iterable" error
- **Telegram timeout on session start** — Session start confirmation message wrapped in try/except. Session continues running even if Telegram API times out
- **Milestone bets=0** — Setting `milestonebets` to 0 now properly disables bet milestones (Python falsy `0` was falling through to default 100)
- **Help text** — Added missing cleanup, delsession, purgedays commands

---

## v2.10.1 — Safety, Cleanup, Charts (2026-04-03)

### Added

- **Deferred auto-bet restart** — `/tweak basebet`, multiplier, lossmult changes on auto-bet sessions wait for the next WIN before restarting, preventing Martingale recovery loss
- **Live bet cleanup** — Bets older than N days auto-deleted every hour during running sessions. Configurable via `/set purgedays 1` (default 1, range 1-30). Prevents DB bloat for long-running sessions
- **Chart snapshots** — Profit/balance saved every 100 bets in session row. Web dashboard charts now survive bet cleanup
- **Profit increment = 0** — Setting `/set profitincrement 0` creates new auto-bet sessions at profit threshold without increasing base bet
- **Pipeline safety** — Pipeline > 1 forced to 1 for non-Flat strategies (prevents Martingale loss from concurrent bets)
- **Strategy aliases** — `/set strategy flat` now works (was requiring 'flat bet')

### Fixed

- **CK audit** — 65 scientific notation violations, production error message gating, DRY refactoring
- **`/tweak basebet` safety** — No longer kills Martingale recovery during loss streak (applies on next win)
- **`/tweak` in Telegram menu** — Added missing BotCommand registration

---

## v2.10.0 — Pipeline Mode & Live Mutation (2026-04-01)

### Added

- **Pipeline mode** — Fire N concurrent auto-bet requests for 40+ bps (vs 23 bps sequential = +78% throughput). Tested optimal depth: 20. Configure with `/set pipeline 20` (auto-bet games only)
- **`/tweak` command** — Mutate running sessions without stopping:
  - `/tweak pipeline 20` — change concurrent requests (instant)
  - `/tweak basebet 0.00000002` — change base bet (auto-bet restarts seamlessly)
  - `/tweak delay 0.1` — add/change bet delay (instant)
  - `/tweak multiplier 2.5` — change multiplier (auto-bet restarts)
  - `/tweak lossmult 2.01` — change loss multiplier (auto-bet restarts)
  - Add slot number for multi-session: `/tweak pipeline 10 2`
- **Speed stats in pipeline mode** — BPS/BPM range tracking now works in pipelined loop

### Fixed

- **Auto-bet resume after bot restart** — Old UUID dies on restart; resume now calls `auto/start` for a fresh UUID instead of reusing the stale one (caused sessions to get stuck)
- **Scientific notation in /config** — Profit threshold displayed as `4.349e-05` instead of `0.00004349` (fixed across all 6 bots)
- **Pipeline safety** — Stop conditions checked after each result in batch; rate limit pauses all pipeline requests; `/stop` drains cleanly

---

## v2.9.1 — Auto-bet Fixes & Web Game Labels (2026-04-01)

### Added

- **Game type in web dashboard** — Blue badge (DICE, LIMBO, LIMBO-AUTO, etc.) on all session tables: user dashboard, history, admin user detail
- **Auto-bet UUID in /status** — Shows WolfBet UUID for verification and tracking
- **D'Alembert strategy for auto-bet** — Mapped to `increaseAmountValue` on loss / `decreaseAmountValue` on win
- **WolfBet stopReason detection** — Auto-bet sessions stop when server sends a stop signal

### Fixed

- **Dice auto-bet endpoints** — Corrected to `/api/v2/dice/auto/*` (was incorrectly v1); dice payload now includes `rule` and `bet_value`
- **Auto-bet profit increment** — Stops and restarts auto-bet with new base bet when profit threshold is hit (WolfBet's `resetAmount` always returns to original base). Session ID stays the same, only UUID changes
- **Auto-bet config commands** — Fixed command names to match WolfBet API: `increaseAmountPercent` (not `increaseAmount`), `lose` (not `loss`) for triggers

### Strategy Mapping (all 7 strategies)

- Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, WolfRider (dice switch), Delay Martingale — all translated to WolfBet server-side config

---

## v2.9.0 — Auto-bet, Async Engine, Proxy Support (2026-04-01)

### Added

- **Auto-bet mode** — Server-side strategy execution via WolfBet's auto-bet API with **4,000 req/min** rate limit (8x faster than manual):
  - `limbo-auto` — `/api/v2/limbo/auto/play`
  - `dice-auto` — `/api/v1/dice/auto/play`
  - Flow: `auto/start` (get UUID) → loop `auto/play` → `auto/stop`
  - Full strategy translation: Flat, Martingale, Anti-Martingale, Paroli, Delay Martingale
  - Stop conditions mapped to server-side config: max profit (gte), max loss (gt), max bets (every), min balance (lt)
- **Per-session proxy support** — Route each session through a different IP for independent throughput:
  - `/set proxy http://user:pass@ip:port` or `socks5://...`
  - `/set proxy off` to disable
  - Proxy indicator (globe icon) in `/status`
- **Dynamic x-hash-api** — Reverse-engineered WolfBet's hash algorithm: `interleave(md5(body), md5(nonce))`. Replaces static hardcoded hashes
- **Game label in all outputs** — `/status`, `/stop`, milestones, `/stats`, `/session` now show game type
- **Game filter for stats** — `/stats limbo` shows only limbo sessions
- **Game stored in DB** — Sessions table now records which game was played

### Changed

- **Async engine** — TG betting engine converted from blocking threads (`requests`) to async tasks (`httpx.AsyncClient`). All sessions run concurrently on the same event loop — true parallel I/O instead of GIL-limited threads
- **Rate limit recovery** — Wait-and-resume instead of probing wrong endpoint. Fixes sessions getting stuck in 32s probe loops when running multiple game types
- **Stop notification** — `/stop` now properly sends session summary (was silently dropped when `task.cancel()` killed the async task)
- **Reduced log noise** — Suppressed httpx/httpcore polling logs, added per-session progress log every 500 bets

---

## v2.8.0 — Multi-Game Support (2026-04-01)

### Added

- **Multi-game support** — Three WolfBet endpoints with different rate limits:
  - `dice` — API v1 `/api/v1/bet/place` (480 req/min) — existing default
  - `dice-manual` — Dashboard API `/api/v1/dice/manual/play` (720 req/min)
  - `limbo` — Dashboard API `/api/v2/limbo/manual/play` (500 req/min)
- **Game selection** — CLI wizard Step 3 now prompts for game; TG via `/set game <name>`
- **Per-game headers** — Manual endpoints automatically include `x-hash-api` and `x-client-type` headers
- **Concurrent multi-game sessions** — Run up to 5 sessions on different games simultaneously (up to 1,700 bets/min combined)

---

## v2.7.2 — Recurring Bets & Zero-Balance Fix (2026-03-29)

### Added

- **Recurring bet feature** — Sessions auto-restart after configurable delay when stopped by conditions (profit target, max bets, etc). Configure with `/set recurring on|off` and `/set recurringdelay <seconds>`. Cancel with `/stop recurring`

### Fixed

- **TG zero-balance start** — TG engine now allows zero-balance sessions when base bet is 0 (previously blocked with "No balance found")

---

## v2.7.1 — Fix BPM Calculation (2026-03-29)

### Fixed

- **BPM calculation** — `bets_per_minute` was set to raw count of bets in the current calendar minute instead of the actual rate; now correctly derived as `bets_per_second * 60`

---

## v1.6.0 — Insufficient Balance Fix (2026-03-29)

### Fixed

- **Insufficient balance detection** — CLI and TG now properly detect "insufficient balance" in API error responses and stop the session immediately
- **TG error notification** — Added `on_error` callback; sends `⚠️ Insufficient balance` message to user via Telegram before stopping

---

## v1.5.9 — Shared Library as pip Package (2026-03-29)

### Infrastructure

- **casino-shared pip package** — `shared/` library now installable via `pip install casino-shared` from GitHub, eliminating manual file copying on servers
- **requirements.txt** — Added `casino-shared @ git+ssh://git@github.com/snad1/casino-shared.git` to both bot and web requirements
- **install.sh** — Added `shared/` copy step as fallback for offline/non-pip installs
- **wolfctl update** — Now syncs `shared/` library alongside core/tg during updates

---

## v1.5.8 — DRY Shared Library Extraction (2026-03-29)

### Infrastructure

- **Shared library extraction** — Extracted ~6,600 lines of duplicated code into `shared/` library used across all 6 casino bots
  - `web/websocket.py` → thin shim delegating to `shared/web/websocket.py`
  - `web/database.py` → thin shim binding `settings.db_path` to `shared/web/database.py`
  - `web/auth.py` → thin shim using `make_auth()` factory from `shared/web/auth.py`
  - `web/services.py` → thin shim binding settings to `shared/web/services.py`
  - `web/bot_db.py` → thin shim with chart column config to `shared/web/bot_db.py`
  - `web/routes/auth_routes.py` → thin shim using `make_auth_router()` factory
  - `tg/database.py` → thin shim binding `DATA_DIR` to `shared/tg/database.py`
  - `core/strategy.py` → keeps local STRATEGIES dict, delegates rule engine to `shared/core/strategy.py`
  - `core/database.py` → keeps local `init_db`/`migrate_db`, delegates utilities to `shared/core/database.py`
- Zero downstream import changes — all existing imports continue working

### Security

- **Shell injection fix** — Replaced `create_subprocess_shell` with `shutil.copy2` in update flow
- **Deprecated datetime fix** — Replaced `datetime.utcnow()` with `datetime.now(timezone.utc)`
- **Narrowed exception handling** — Replaced broad `except Exception` with specific types
- **Removed unused imports** — Cleaned up `import os` from tg/database.py shim

### Testing

- **114-test suite** added covering all shared modules (database, auth, strategy, services, bot_db, tg persistence)

---

## v1.5.7 — Security Hardening (2026-03-29)

### TG Bot v1.5.7

#### Security

- **ENV-controlled error messages** — Error handlers now log full details server-side and show user-friendly messages in production. Controlled via `APP_ENV` environment variable
- **`wolfctl` self-update safety** — Script wrapped in `{ }` block to prevent parse errors when `cmd_update` replaces the file mid-run

---

## v1.5.6 — Data Retention & Cleanup (2026-03-24)

### TG Bot v1.5.6

#### Added

- **Automatic bet cleanup** — Old bet records (3+ days) from ended sessions are automatically purged when a new session starts. Session statistics (profit, bets, streaks, balance extremes, speed) are preserved — only raw bet rows are deleted. Disk space is reclaimed via VACUUM.
- **`/cleanup [days]`** — Manually purge bet records older than N days (default 3). Session stats remain intact. Usage: `/cleanup` or `/cleanup 1`.
- **`/delsession <id>`** — Delete a specific session and all its bets by session ID. Blocks deletion of currently running sessions. Shows bet count and profit before confirming.
- **`bets_purged` flag** — Sessions table gains a `bets_purged` column to track which sessions have already been cleaned, avoiding redundant queries on subsequent cleanups.

## v1.5.5 — Reliable Resume (2026-03-13)

### TG Bot v1.5.5

#### Fixed

- **Resume now tests API connection** — `start_resumed()` verifies the API is reachable before starting the betting loop.
- **Resume retries** — Connection is retried up to 3 times (5s apart) on startup.
- **Resume failure notification** — If resume fails, the user is notified via Telegram instead of silent failure.

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

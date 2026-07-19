# CLI ↔ Telegram parity audit

**Date**: 2026-07-17
**Purpose**: Systematically enumerate every feature the Telegram bot (`tg/engine.py`) has, and mark whether the terminal CLI (`wolfbet.py` + `core/engine.py`) has it. This is the source of truth for Phase 2–5 of the CLI↔TG parity roadmap.

**Phase completion log**:

- **Phase 1** — landed in **CLI v2.15.0** (commit `aaf01d4`). Section 1 P0 items now Y.
- **Phase 2** — landed in **CLI v2.16.0** (commit `683f6f3`). Section 3 recurring / streak delays / basebet_pct / streakbet_loss / milestones / 429 Retry-After now Y.
- **Phase 3** — landed in **CLI v2.17.0** (commit `3dc2b16`). Section 4 API timing (last/avg/peak/p50), on-target %, cycle_ms, efficiency %, rate-limit dashboard line, auto_start_remaining dashboard line now Y.
- **Phase 4a** — landed in **CLI v2.18.0** (commit `776fcb9`). Wizard prompts + preset persistence for the Phase 2 config fields (recurring / streak delays / streakbet_loss / basebet_pct / milestones). Follow-up debt from v2.16.0 / v2.17.0 is now **cleared** — new users reach every Phase 2 feature from the interactive wizard, and presets round-trip fully.
- **Phase 4b** — landed in **CLI v2.19.0** (commit `0e6a1ff`). Runtime tweak surface: press `T` during a session to change 22 parameters mid-flight (parity with TG `/tweak`). New `_apply_mutation(changes)` central mutation function reuses Phase 2 helpers; auto-game strategy tweaks clear `_auto_uuid` for a fresh restart on the next loop iteration.
- **Phase 5a** — landed in **CLI v2.20.0** (commit `42ff9cc`). First "beyond TG" release. Rich session summary at end (`Q` / Ctrl+C / SIGTERM), `--export <path>` CLI flag + `--export-session-id N`, and `E` hotkey for live-session export (CSV or JSON — detected from extension). Zero risk to the bet loop — display + export layer only.
- **Phase 5c** — landed in **CLI v2.21.0** (commit `1fc7c98`). Backtest / replay mode: `--backtest` skips the API entirely and runs the strategy against either synthetic outcomes (`--simulate N`, optional `--seed`) or a saved session's historical rolls (`--replay-session ID`). Strategy engine is now the single source of truth for both live and simulated runs.
- **Phase 6** — landed in **TG v1.16.0 / CLI v2.22.0** (commit `87c18dc`). Shared registry refactor: `GAME_ENDPOINTS` / `AUTO_ENDPOINTS` / `GAME_LABELS` / `wolfbet_hash` / `is_auto_game` / `fmt_multiplier` / `custom_rule_to_wolfbet` / `build_auto_config` / `build_auto_start_payload` all extracted from both engines into `wolfbet/core/games.py`. Both engines import from one truth. Zero user-visible change. Adding a new WolfBet game is now a single-file edit.
- **CLI v2.22.1 polish** — landed in commit `724cdfe`. `_check_risky_config()` heads-up panel at wizard exit (8 categories: aggressive Martingale, no max_loss, no max_profit on Anti-M, base_bet ≥ 1% of balance, multiplier > 10, recurring without stop, custom rules without max_bets, aggressive basebet_pct). Better `--export` error hint, expanded `--backtest --help` examples. Non-blocking; zero risk to bet loop.
- **CLI v2.23.0** — landed in this update. New `--analytics` command: lifetime aggregates across ALL sessions with breakdowns by game and strategy, plus top 5 best / worst sessions. Complement to `--stats` (recent 20 sessions in detail). Read-only SQL over the local DB.
- **CLI v2.23.1** — documentation refresh complete. `README.md` now covers every phase (hotkeys, tweak grammar, advanced tuning, observability, exports, backtest, analytics, safety warnings, file locations, testing). Zero code changes; ready for real-world use.
- **Phase 5b — CLI v2.24.0** — pipelined betting shipped (last outstanding parity gap closed). `--pipeline N` flag + `pipeline_depth` state, `ThreadPoolExecutor(N)` + `threading.Semaphore(N)` in `pipelined_betting_loop`, Flat-only guardrail via `_effective_pipeline_depth`, `_apply_outcome_locked` for counter integrity, rate-limit drain before cooldown, dashboard "Pipeline" row, wizard step 5b prompt, tweak alias, preset persistence, 25 new tests. CLI now matches TG bot's tg/engine.py:1373 pattern feature-for-feature.
- **Phase 5c + config CLI — CLI v2.25.0** — fixes real-world dashboard spam. `_classify_auto_stop_reason` + wired-in `_recover_expired_session` (rotates seed on "Max nonce achieved" / "session expired", keeps same CLI session, 3-strike circuit breaker). New `--config-show` / `--config-set` for non-interactive config surgery (reuses `_TWEAK_ALIAS`; `api_key` rejected, env-var only). `RawDescriptionHelpFormatter` + grouped `Examples:` epilog. Monitor stats: ROI %, API p50/p99 (unconditional), Efficiency + Cycle unconditional, `/auto/start` quota with red-tint at ≤3. 39 new tests. 260 total passing.
- **wolfctl refresh — CLI v2.25.1** — closes discoverability gap on the server. wolfctl v3.2 → v3.3 with 9 new subcommands (`config-show`, `config-set`, `analytics`, `backtest`, `export`, `last-bets`, `preset`, `pipeline`, `version`) — 1:1 parity with the Python CLI flags shipped after v2.19. install.sh no longer inlines a stale v3.1 heredoc; uses `install -m 0755` from the repo. 16 new tests. 276 total passing.
- **wolfctl update hardening — CLI v2.25.2** — `cmd_update` now uses `git pull --ff-only --autostash` with a dirty-tree preflight. Server was blocked because local `install.sh` changes clashed with the incoming v2.25.1 pull. Zero destructive fallbacks; stash-list + stash-pop recovery hint on failure. 3 new tests. 279 total passing.
- **recurring_delay + api_key indicator — CLI v2.25.3** — closes the last easily-portable TG↔CLI config gap. `recurring_delay` (default 60s, floor 0, cap 3600) mirrors `tg/handlers.py:801`; sleep site at the top of `_reset_session_for_recurring` covers both serial and pipelined loops. Wizard, tweak alias, config-show row, risky-config warning, help epilog all updated. `cmd_config_show` marks `api_key` as `(SET)` / `(NOT SET)`. **Deferred (TG-only, next phase)**: `purge_days` (needs CLI `--cleanup` command), `proxy` (needs HTTP-client rework). 20 new tests. 299 total passing.
- **Full CLI↔TG parity — CLI v2.26.0** — closes both deferred items and one operational hazard. **Proxy**: shared `_http_session` (`requests.Session`) routes 5 wolfbet.com call sites through an optional HTTP/HTTPS/SOCKS5 proxy; credentials always masked via `_mask_proxy` (`urlparse` → `scheme://host:port`); `_check_risky_config` warns non-blockingly on `http://` schemes. **Retention**: `purge_days` config (default 3, range 1-30) + `--cleanup [DAYS]` + `wolfctl cleanup` + startup purge + hourly `_retention_worker` thread (reuses `cleanup_old_bets` / `cleanup_live_bets` from `shared/core/database`). **wolfctl v3.4**: `git+ssh://` → `git+https://` for `casino-shared` (no github SSH host-key prompt on fresh hosts). 44 new tests. 343 total passing. Next: other bots (`stake/`, `cryptogames/`, `duckdice/`, `shuffle/`, `goated/`) can adopt the same CLI-parity template.
- **BPS diagnostics — CLI v2.26.1** — mirrors TG's `/benchmark` (`tg/handlers.py:239-309`) plus adds a CLI-first `--diagnose` for running-daemon analysis. `cmd_status` finally surfaces the p50/p99/cycle/efficiency rows that already existed in the state file. Answers "is it WolfBet or my CLI?" in one command (`wolfctl benchmark`). Zero behavior changes to the bet loop. 17 new tests. 360 total passing. wolfctl v3.5.
- **Async bet-writer + SQLite tuning — CLI v2.27.0** — attacks the ~67 ms per-bet CLI overhead diagnosed in v2.26.1. Background writer thread (`_bet_writer_worker`) with persistent tuned connection (`_open_bet_writer_conn`: `PRAGMA synchronous=NORMAL`, `busy_timeout=5000`, `cache_size=-32000`, `temp_store=MEMORY`) + batched `executemany` (up to 32 rows or 100 ms window). Bounded `queue.Queue(maxsize=1000)` with sync fallback on overflow (nothing-lost). Drain on shutdown with 5 s cap. New `db_write_mode` config (`async` default, `sync` opt-out). `_db_save_bet_dispatch` routes both `betting_loop` and pipelined `_apply_outcome_locked`. `--diagnose` shows DB-writer stats. Real-world verified: 8.6 → 16.4 BPS, overhead 67 → 29 ms, `fallback_sync=0`. 18 new tests. 378 total passing.
- **Shared helper extraction — CLI v2.28.0 + TG v1.17.0** — 6 pure-Python helpers previously duplicated between wolfbet.py and tg/*.py extracted to `core/net_helpers.py` (`validate_proxy`, `mask_proxy`, `classify_auto_stop_reason`, `parse_streak_delay`) and `core/diagnostics.py` (`percentile`, `benchmark_verdict`). **TG bot bug fixes carried by extraction (parity gains)**: (1) TG now recovers from `Max nonce achieved` via `_recover_expired_session` — same call sites, but classifier now catches the "nonce" substring TG's inline check missed. Same dashboard-spam class of bug the CLI shipped a fix for at v2.25.0. (2) TG's `mask_proxy` no longer leaks `"None:port"` on port-less URLs. (3) TG `/set proxy=off` accepts broader off-vocabulary (`false/disable/no`). (4) TG `/benchmark` gains p50/p99 + plain-English verdict. Zero CLI behaviour change (re-export aliases kept). **Deferred (structural, next phase)**: `_recover_expired_session` (transport-specific — `requests` vs `httpx.AsyncClient`), `AsyncBetWriter` port to TG (TG uses `asyncio.to_thread`). 32 new tests. 410 total passing. wolfctl unchanged (v3.5).
- **Monitor UX + systemd Restart= fix — CLI v2.28.1** — resolved server-side confusion where the CLI's `recurring=off` didn't prevent the daemon from respawning after `S` from monitor. Root cause: `install.sh` wrote `Restart=always` in the systemd unit; systemd saw the clean SIGTERM exit as success and applied its `Restart=always` policy. Fixed: `Restart=on-failure` in `install.sh` (both CLI + TG units); idempotent `wolfctl update` migration for existing installs (`sed` + `daemon-reload`); new `_is_systemd_managed(pid)` helper checks `/proc/{pid}/cgroup` for `wolfbet.service`; `cmd_monitor` stop path routes through `systemctl --user stop wolfbet` when managed, falls back to raw SIGTERM otherwise. Monitor footer + attach banner now advertise `wolfctl config-set` for tweaks + `wolfctl export/stats/analytics` for data (T/E/H remain foreground-TUI only). 15 new tests. 425 total passing. wolfctl unchanged.
- **Monitor header + config-persistence gap — CLI v2.28.2** — v2.28.1's footer added a row without bumping the `bets_avail` budget; session-title scrolled off top. Fixed via `_MONITOR_FIXED_ROWS = 11` constant + regression test locking `_build_monitor_screen` to render exactly `term_h` lines. Also closed a persistence gap present since Phase 2 (v2.16.0): `_CONFIG_KEYS` was missing `recurring`, `basebet_pct`, and all four `milestone_*` fields; streak-tuple fields (`_STREAK_TUPLE_KEYS`) had no round-trip in `save_config`/`load_config`. Every `wolfctl config-set` on those fields silently reverted on next process launch (including daemon `--resume`). Fixed by extending `_CONFIG_KEYS` and adding streak-tuple `"N:X"` (de)serialization reusing `parse_streak_delay`. Backward-compat verified. 21 new tests. 446 total passing.
- **SIGHUP live reload — CLI v2.28.3** — v2.28.2 fixed persistence but the running daemon still had no way to learn about disk config changes short of a full restart. Added `_LIVE_RELOAD_KEYS` allowlist (24 safe fields: recurring + stop conditions + delays + milestones + pipeline_depth + proxy + purge_days + db_write_mode + custom_rules_text), `_reload_live_config()` helper (re-reads JSON under `state.lock`, decodes streak tuples, applies side effects for proxy + rules), and `_sig_reload` SIGHUP handler registered in `main()`. `cmd_config_set` now reads `PID_PATH` after `save_config` and sends SIGHUP to hot-apply. Unsafe fields (`game`, `strategy_key`, `base_bet`, `api_key`, `currency`, `rule`, `multiplier`, `bet_value`, `strategy`, `loss_mult`, `win_mult`, `delay_martin_threshold`) are skipped mid-flight and both the CLI and daemon log a heads-up requesting `wolfctl restart`. 20 new tests. 466 total passing. wolfctl unchanged.

**Criticality legend**:
- **P0** — blocks feature parity (auto sessions can't run, sessions can't recover from a common failure)
- **P1** — quality of life; parity feels "smooth" (recurring, milestones, dashboard metrics)
- **P2** — nice-to-have (analytics, "beyond TG" additions)

**Status legend**: **Y** (implemented), **N** (missing), **P** (partial / behaviour differs meaningfully).

## Architectural gap

| Aspect | TG | CLI | Notes |
|---|---|---|---|
| HTTP client | `httpx.AsyncClient` (async) | `requests` (sync) | Whole port has to be sync-adapted; can't copy `async def` bodies verbatim. Retries use `time.sleep` not `await asyncio.sleep`. |
| Concurrency | asyncio task per user | single thread (`threading.Thread` for the bet loop) | CLI's `state` is a module-level singleton; only ever one session at a time. Multi-session TG maps to multi-slot state; CLI would need refactor for concurrent sessions. |
| Persistent connection | httpx client reuses TCP/TLS | `requests` opens per-call | CLI misses connection-pool speedup TG has (P2 speed opt). |
| Hash generation | Dynamic `md5(body) ⊕ md5(nonce)` via `_wolfbet_hash` | Hardcoded static string per game | Auto endpoints need dynamic hash — CLI must gain `_wolfbet_hash()` for the auto path. Manual endpoints keep the static hash pattern that already works. |

## 1. Auto-bet lifecycle (P0 for `dice-auto` / `limbo-auto` to work at all)

| Feature | TG source | CLI status | P | Notes |
|---|---|---|---|---|
| `dice-auto` / `limbo-auto` in `GAME_ENDPOINTS` | tg/engine.py:22-28 | N | P0 | Phase 1 delivers this. |
| `AUTO_ENDPOINTS` map (start / stop paths) | tg/engine.py:38-41 | N | P0 | Phase 1. |
| `AUTO_START_MIN_INTERVAL` pacing floor | tg/engine.py:47 | N | P0 | Phase 1. |
| `_auto_start()` — POST /auto/start, parse UUID, handle 429 + Retry-After, quota-floor pacing | tg/engine.py:364-426 | N | P0 | Phase 1. Sync port. |
| `_auto_stop()` — best-effort DELETE-equivalent | tg/engine.py:459-473 | N | P0 | Phase 1. |
| `_update_auto_start_remaining()` — parse `x-ratelimit-remaining` from start response | tg/engine.py:428-435 | N | P0 | Phase 1. |
| `_recover_expired_session()` — rotate seed + restart on "Session has ended" 400 | tg/engine.py:437-442 | N | P0 | Phase 1. |
| `_rotate_server_seed()` — GET /v1/game/seed/refresh | tg/engine.py:444-457 | N | P0 | Phase 1. |
| `_build_auto_start_payload()` — includes strategy config | tg/engine.py:484-498 | N | P0 | Phase 1. |
| `_build_auto_config()` — translates strategies 1-8 + stop conditions to WolfBet auto-bet format | tg/engine.py:542-601 | N | P0 | Phase 1. |
| `_custom_rule_to_wolfbet()` — translate our StrategyRule to WB rule format | tg/engine.py:500-540 | N | P0 | Phase 1 (Rule-Based strategy 8). |
| `_build_bet_payload()` returns `{"uuid": …}` for auto games | tg/engine.py:632-633 | N | P0 | Phase 1. |
| `_wolfbet_hash()` dynamic md5⊕md5 header | tg/engine.py:50-54 | N | P0 | Phase 1 (needed for `/auto/*`). |
| `_is_auto_game()` predicate | tg/engine.py:475-476 | N | P0 | Phase 1. |
| Auto UUID persisted to snapshot for restart resume | tg/engine.py:1153, 1236 | N | P0 | Phase 1. |
| Auto stop on clean shutdown (SIGINT etc.) | tg/engine.py | N | P0 | Phase 1. |
| Restart pending flag: re-arm auto on next win when profit-target hit but strategy wants more | tg/engine.py:218 `_pending_restart` | N | P1 | Phase 2. |

## 2. Strategy engine + rules

| Feature | TG source | CLI status | P | Notes |
|---|---|---|---|---|
| Strategy 1 Flat | shared via `core.engine.compute_next_bet` | Y | P0 | Same core code path — already reused. |
| Strategy 2 Martingale | shared | Y | P0 | |
| Strategy 3 Anti-Martingale | shared | Y | P0 | |
| Strategy 4 D'Alembert | shared | Y | P0 | |
| Strategy 5 Paroli | shared | Y | P0 | |
| Strategy 6 WolfRider (over/under switch) | shared | Y | P0 | Manual side works. |
| Strategy 7 Delay Martingale (`delay_martin_threshold`) | shared | Y | P0 | |
| Strategy 8 Rule-Based (custom `StrategyRule` list) | shared | Y | P0 | Manual apply via `apply_rules()`. |
| Server-side translation of strategy → WolfBet auto-bet rules | tg/engine.py:542-601 | N | P0 | Phase 1 (only meaningful for auto games — manual games run strategies client-side). |
| `_custom_rule_to_wolfbet()` — client-rule → server-rule translator | tg/engine.py:500-540 | N | P0 | Phase 1. |

## 3. Session robustness (P0 for "runs smoothly, knows how to recover")

| Feature | TG source | CLI status | P | Notes |
|---|---|---|---|---|
| Insufficient-balance detection → clean stop | tg/engine.py:1589 + earlier | Y | P0 | CLI has it (wolfbet.py:496-499). |
| 429 rate-limit backoff | tg/engine.py:406-411 + tight loop | P | P0 | CLI has a simpler backoff (`rate_wait` + `rate_backoff`) but no exponential-with-jitter. Phase 2. |
| `Retry-After` header respected on 429 | tg/engine.py:407 | Y (v2.16.0) | P1 | Landed in v2.16.0 — `api_place_bet` reads the header, floors at 2s. |
| `_recover_expired_session` on 400 "Session has ended" | tg/engine.py:437-442 | N | P0 | Phase 1 (auto only). |
| Timeout / NetworkError retry with backoff | tg/engine.py:414-421 (auto), similar in `_api_place_bet` | P | P0 | CLI catches Timeout → sleeps once then continues loop. TG has multi-step backoff. Phase 2. |
| `MAX_TIMEOUT_STREAK` → recreate HTTP client | tg/engine.py:60 | N | P1 | CLI doesn't recreate `requests` sessions — it doesn't use a Session object. Phase 2. |
| Recurring session (auto-restart on stop) | tg/handlers.py + engine `_pending_restart` | Y (v2.16.0) | P1 | Landed in v2.16.0. Positive-whitelist guard (`_should_recurring_restart`) — restarts only on Profit target / Max bets / Max wins **and** `profit ≥ 0`. Loss / insufficient balance always exit. |
| `streak_delay_loss`, `streak_delay_win`, `streak_delay_bets` — pause N seconds after N consecutive losses/wins/bets | tg/engine.py:96-99 | Y (v2.16.0) | P1 | Landed in v2.16.0. Format `"N:seconds"`. Applied by `_get_streak_delay` before each bet. |
| `streakbet_loss` — reduce bet after N consecutive losses | tg/engine.py + `_get_streakbet_loss_factor` | Y (v2.16.0) | P1 | Landed in v2.16.0. Format `"N:multiplier"`. Applied to `next_bet` after each round. |
| `basebet_pct` — base bet as % of current balance | tg/engine.py:292-303 `_apply_basebet_pct` | Y (v2.16.0) | P1 | Landed in v2.16.0. Recomputed every 10 bets from live balance. |
| `profit_increment` + `profit_threshold` (increment base bet every N profit) | tg/engine.py:640-647 (visible in CLI!) | Y | P0 | **CLI already has this** — wolfbet.py:640-647. |
| Milestone notifications (every N bets/wins/losses/profit) | tg/engine.py:200-202 + callbacks | Y (v2.16.0) | P1 | Landed in v2.16.0. `_check_milestone` returns reason string; CLI logs via `logger.info` and briefly shows in `state.status`. Push notifications remain TG-only. |
| Stop condition: `max_profit` | tg/engine.py:592-593 | Y | P0 | CLI has it. |
| Stop condition: `max_loss` | tg/engine.py:594-595 | Y | P0 | |
| Stop condition: `max_bets` | tg/engine.py:596-597 | Y | P0 | |
| Stop condition: `max_wins` | wolfbet.py `should_stop` | Y | P0 | CLI has, TG lacks explicit `max_wins`. TG has `wins` counter but no per-session cap. **CLI ahead here.** |
| Stop condition: `stop_on_balance` (balance floor) | tg/engine.py:598-599 | Y | P0 | CLI has it. |
| Server-side stop-reason parser (`_fmt_stop_reason`) — turns `{{token}}` templates into human messages | tg/engine.py:279-290 | N | P0 | Phase 1 (auto sessions get server-authored stop reasons). |
| Pipelined betting (`pipeline > 1`, parallel bets) | tg/engine.py:74 + `_pipelined_betting_loop` | N | P2 | Phase 5. Async-only speed opt. |

## 4. Observability

| Feature | TG source | CLI status | P | Notes |
|---|---|---|---|---|
| Live status field | tg/engine.py `self.status` | Y | P0 | CLI has `state.status`. |
| API timing: last/avg/peak | tg/engine.py `_api_ms`, `_api_ms_total`, `peak_api_ms` | Y (v2.17.0) | P1 | Landed in v2.17.0. Rolling counters + peak, displayed as `API last / avg / peak` row. |
| API p50 median (`api_p50_ms`) | tg/engine.py:1495-1499 | Y (v2.17.0) | P1 | Landed in v2.17.0. Median of a 500-sample deque; renders in the `API stable` row when `bet_delay > 0`. |
| On-target % (fraction of bets whose API ≤ bet_delay) | tg/engine.py:1497-1500 | Y (v2.17.0) | P1 | Landed in v2.17.0. Renders `n/a` when `bet_delay = 0` (matches TG). |
| Cycle time (`cycle_ms`) — full per-bet duration | tg/engine.py `_cycle_ms` | Y (v2.17.0) | P1 | Landed in v2.17.0. EMA-smoothed (`0.9*old + 0.1*new`). |
| Efficiency % (actual bps / theoretical max) | tg/engine.py:1501-1508 | Y (v2.17.0) | P1 | Landed in v2.17.0. Capped at 100%. Green ≥ 90%, yellow below. |
| Overhead ms (engine + scheduler) | tg/engine.py `_cycle_overhead_ms` | Y (parenthetical) | P2 | Value computed by v2.17.0; shown inside the Cycle row (`overhead …ms`) rather than a standalone row — matches TG v1.15.10 trim. |
| Rate-limit remaining line — live budget from `x-ratelimit-remaining` | tg/engine.py:343-354 + get_status | Y (v2.17.0) | P1 | Landed in v2.17.0. Dashboard row `Rate limit: N/M play`. |
| `auto_start_remaining` line — separate quota display for auto/start | tg/engine.py:221 + get_status | Y (v2.17.0) | P0 | Landed in v2.17.0. Rendered as `· K/20 start` on the Rate-limit row when an auto-bet session is active. |
| Bets per second (recent + session-avg) | tg/engine.py:180 `_bps_recent_history` | Y | P0 | CLI has `state.bets_per_second`. |
| Bets per minute | tg/engine.py | Y | P0 | CLI has `state.bets_per_minute`. |
| Peak / low bps + bpm | tg/engine.py | Y | P0 | CLI has `peak_bps`, `low_bps`, `peak_bpm`, `low_bpm`. |
| Session-uptime string | tg/engine.py | Y | P0 | CLI has. |
| Streak tracking (current, max win, max loss) | shared | Y | P0 | CLI has. |
| Best-win / worst-loss extremes | shared | Y | P0 | CLI has. |
| Recent bets list (last N) | tg/engine.py `recent_bets` deque | Y | P0 | CLI has. |
| Profit history / sparkline data | tg/engine.py `_chart_snapshots` | Y | P0 | CLI has `profit_history`. |

## 5. Persistence + resume

| Feature | TG source | CLI status | P | Notes |
|---|---|---|---|---|
| SQLite bets + sessions tables | `core.database` | Y | P0 | Shared. |
| Bet batching (flush every N bets or M seconds) | tg/engine.py:57-58 `BET_BATCH_SIZE`, `SESSION_SAVE_SECS` | P | P1 | CLI writes per-bet (`_db_save_bet` called after each bet) — no batching. Phase 2 opt. |
| Async flush via `asyncio.to_thread` | tg/engine.py | N | N/A | CLI is sync-threaded — DB writes already off the main render thread via the bet-loop thread. |
| `check_same_thread=False` for cross-thread reuse | tg/engine.py:1552, 1736 (post-v1.15.6 fix) | Y | P0 | Manual test: CLI's `db_connect` in `core.database` handles this. |
| Live-state snapshot file (`~/.wolfbet_autobot_live.json`) | tg/engine.py resume state | Y | P0 | CLI has `STATE_PATH` + `_save_state_file` + `_cleanup_state_file`. |
| `_auto_uuid` persisted in snapshot | tg/engine.py:1153, 1236 | N | P0 | Phase 1. |
| Snapshot version guard | tg/engine.py schema | Y | P0 | CLI has version key. |
| Cleanup old bets (`purge_days`) | tg/engine.py:213 | Y | P0 | Shared via `core.database.cleanup_old_bets`. |
| Live-bet cleanup periodic | tg/engine.py `_last_cleanup` | Y | P0 | Shared. |

## 6. Notifications / UX / control surface

| Feature | TG source | CLI status | P | Notes |
|---|---|---|---|---|
| Session-end notification (`format_stop`) with green ✅ / red 🛑 | tg/formatter.py | N/A | — | Telegram-specific. CLI's equivalent is the final TUI screen — Phase 4 could add "session summary" pane. |
| Milestone push notifications | tg/handlers.py `_notify_milestone` | N/A | — | TG-only pattern. CLI could log to console. Phase 4. |
| Recurring restart notification (throttled) | tg/handlers.py `_notify_stop` + `_throttle_recurring` | N/A | — | Depends on Phase 2 recurring feature. |
| `/tweak` command — mid-session parameter change | tg/handlers.py `cmd_tweak` | Y (v2.19.0) | P1 | Landed in v2.19.0. Press `T` during a session → modal `param value` prompt. 22 tweakable fields with TG-style aliases. Auto-game strategy tweaks clear `_auto_uuid`. |
| Pause / resume | tg/engine.py `self.paused` | Y | P0 | CLI has `state.paused` + `[P]` key. |
| Manual stop | tg/handlers.py `cmd_stop` | Y | P0 | CLI has Ctrl+C / `[Q]`. |
| Live status refresh (auto-poll every N seconds) | tg/handlers.py `cmd_monitor` | Y | P0 | CLI's rich Live layout redraws continuously. |
| Multi-slot / multi-session per user | tg/handlers.py slot system | N | P2 | Phase 5. CLI could gain `wolfctl start --slot=N`. |

## 7. Configuration / API surface

| Feature | TG source | CLI status | P | Notes |
|---|---|---|---|---|
| API key entry | `/setkey` | Y | P0 | CLI has interactive prompt + `~/.wolfbet_autobot.json` config. |
| Proxy support | `config["proxy"]` | Y | P0 | CLI honors `state.proxy`. |
| Currency selection | `config["currency"]` | Y | P0 | Menu. |
| Game selection | `config["game"]` | P | P0 | CLI menu missing `dice-auto` / `limbo-auto`. **This is the trigger for Phase 1.** |
| Strategy selection | `config["strategy_key"]` | Y | P0 | Menu. |
| Multiplier / rule (over/under) / bet_value | `config[...]` | Y | P0 | Menu. |
| Base bet | `config["base_bet"]` | Y | P0 | |
| `bet_delay` | `config["bet_delay"]` | Y | P0 | |
| `delay_martin_threshold` | `config["delay_martin_threshold"]` | Y | P0 | |
| Streak delay configs | `streak_delay_{loss,win,bets}` | N | P1 | Phase 2. |
| `streakbet_loss` | tg/engine.py | N | P1 | Phase 2. |
| `basebet_pct` | tg/engine.py | N | P1 | Phase 2. |
| `profit_increment` + `profit_threshold` | tg/engine.py | Y | P0 | CLI has these. |
| `pipeline` depth | `config["pipeline"]` | N | P2 | Phase 5 (needs async). |
| `purge_days` cleanup config | tg/engine.py:213 | Y | P0 | CLI has. |
| Presets (save/load/list/delete configurations) | TG has via `/set …` cmds + `_save_config` | Y | P0 | **CLI ahead** — has preset system (wolfbet.py:159-233). |

## 8. Testing gap (existing untested surface)

The CLI had **zero automated tests** before Phase 2. Phase 1 originally planned `tests/test_auto_endpoints.py` but the port scope pushed it out.

**Phase 2 (v2.16.0)** stood up the test-suite scaffolding: `wolfbet/tests/` directory + `tests/__init__.py` + `wolfbet/tests/test_phase2_helpers.py` — 29 tests covering every new pure helper (`_parse_streak_delay`, `_get_streak_delay`, `_get_streakbet_loss_factor`, `_apply_basebet_pct`, `_check_milestone`, `_should_recurring_restart`).

**Phase 3 (v2.17.0)** added `wolfbet/tests/test_phase3_metrics.py` — 22 tests covering `_compute_api_avg`, `_compute_api_p50`, `_compute_on_target_pct`, `_compute_theoretical_max_bps`, `_compute_efficiency_pct`, `_record_api_ms`, `_record_cycle_ms`.

**Phase 4a (v2.18.0)** added `wolfbet/tests/test_phase4a_presets.py` — 9 tests covering preset round-trip for all Phase 2 fields, streak-tuple encoding, backward-compat with legacy presets, and malformed-value fallback.

**Phase 4b (v2.19.0)** added `wolfbet/tests/test_phase4b_mutation.py` — 34 tests covering every branch of `_apply_mutation`: instant params, streak decoding, `off` synonyms, stop conditions, milestones, recurring toggle (all 10 boolean synonyms), strategy touches on manual vs auto games (UUID clear), type validation (`ValueError` propagation), alias resolution, and `_TWEAK_ALIAS` ↔ `_TWEAK_PARAMS` consistency.

**Phase 5a (v2.20.0)** added `wolfbet/tests/test_phase5a_export.py` — 15 tests covering: DB seed round-trip, `_read_session` / `_read_bets` / `_latest_session_id`, format detection (`.csv` / `.CSV` / `.json` / unknown), CSV `#`-metadata + header shape, JSON `{session, bets}` structure, file perms (`0o600`), missing-session raises `ValueError`, `format_session_summary` no-crash on zero-bet and populated states.

**Phase 5c (v2.21.0)** added `wolfbet/tests/test_phase5c_backtest.py` — 14 tests covering: `_simulate_bet_outcome` shape / determinism / win-rate for multipliers 2.0 and 10.0, `_replay_bets_from_db` correct count / bet size / raises on missing session, `_apply_backtest_result` win/loss/mixed streak tracking, `backtest_loop` runs to completion, respects `max_bets` stop condition, deterministic with the same seed.

**Phase 6 (TG v1.16.0 / CLI v2.22.0)** added `wolfbet/tests/test_phase6_games.py` — 35 tests covering the shared module: registry shape, `wolfbet_hash` length/hex validity, `is_auto_game` positive/negative, `fmt_multiplier` for 6 inputs, `request_hash_header` for 3 game classes, `custom_rule_to_wolfbet` for 5 rule types, `build_auto_config` for all 8 strategy branches + unknown-default + stop-condition appending, `build_auto_start_payload` for both auto games.

**Polish v2.22.1** added `wolfbet/tests/test_phase7_polish.py` — 20 tests for `_check_risky_config`: clean-config → no warnings, each of the 8 warning categories triggers correctly, boundary conditions (e.g. `loss_mult=2.0` doesn't trigger aggressive, `multiplier=10.0` doesn't trigger high-multiplier), combined config triggers multiple simultaneous warnings.

**Analytics v2.23.0** added `wolfbet/tests/test_phase8_analytics.py` — 18 tests seeding a temp DB with 3 sessions across 2 games and 2 strategies, then validating: lifetime totals match hand-computed values, `by_game` / `by_strategy` grouping + profit-desc ordering + aggregate columns, `top_sessions` `profit_desc` / `profit_asc` / `bets_desc` / unknown-order fallback / limit-respected, empty-DB edge case, `cmd_analytics` no-crash.

Combined: **196 tests** pass in `WOLFBET_ENV=testing pytest wolfbet/tests/ -q` (< 300 ms in the wolfbet `.venv`).

Still untested (Phase 1 debt + everything else):

- `_build_auto_start_payload` / `_build_auto_config` / `_custom_rule_to_wolfbet` — Phase 1 port. Deferred; low change-frequency.
- The bet loop (`betting_loop`) itself — has no test seams; would need refactor to be unit-testable.
- HTTP surfaces (`api_place_bet`, `api_auto_start`, `api_auto_stop`) — need mocked-`requests` tests.
- Database layer (`_db_save_bet`, `_db_save_session`, `_db_start_session`).

Broader coverage is best added incrementally when a bug lands or when refactoring — retrofitting all-at-once for a working script is low-ROI.

## Phase mapping — what lives where

- **Phase 1** (v2.15.0, ✅ landed): Section 1 P0 items + audit doc.
- **Phase 2** (v2.16.0, ✅ landed): Section 3 recurring / streak-delays / streakbet_loss / basebet_pct / milestones / 429 Retry-After. Testing scaffold introduced.
- **Phase 3** (v2.17.0, ✅ landed): Section 4 observability parity — API timing (last/avg/peak/p50), on-target %, cycle_ms, efficiency %, rate-limit dashboard line, auto_start_remaining dashboard line.
- **Phase 4a** (v2.18.0, ✅ landed): Wizard prompts + preset persistence for the Phase 2 config fields — follow-up debt cleared.
- **Phase 4b** (v2.19.0, ✅ landed): Runtime `/tweak`-equivalent — press `T` for mid-session parameter edits across 22 tweakable fields; auto-game strategy tweaks clear `_auto_uuid` for a fresh restart.
- **Phase 5a** (v2.20.0, ✅ landed): Session summary on exit + CSV/JSON export. `--export <path>` CLI flag + `E` hotkey; summary panel on `Q` / Ctrl+C / SIGTERM.
- **Phase 5c** (v2.21.0, ✅ landed): Backtest / replay mode. `--backtest --simulate N [--seed N]` (Monte Carlo) or `--backtest --replay-session ID` (historical replay). No API calls, no real balance risk. Strategy engine is now the single source of truth for both live and simulated runs.
- **Phase 6** (TG v1.16.0 / CLI v2.22.0, ✅ landed): Shared registry refactor. `GAME_ENDPOINTS` / `AUTO_ENDPOINTS` / `GAME_LABELS` / auto-bet builders extracted to `wolfbet/core/games.py`. Both engines import from one truth; new games are single-file edits.
- **Phase 5b** (last remaining roadmap item, on-demand): async refactor + pipelined betting + multi-slot. Requires an `httpx.AsyncClient` migration and threading/concurrency model choice. Deferred until speed becomes the bottleneck.

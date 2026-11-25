# Changelog

## v2.63.0 / TG v1.31.0 - `/sessions` on Telegram; `wolfctl stats --before ID` (2026-11-25)

- New Telegram command `/sessions [lane]` (owner only): the last 10 CLI sessions, one line each, such as `#14 · 11-25 12:04-12:31 · 9,812 bets · +0.00012000 USDT` (`12:04-running` while open). Without a lane every lane is listed and lines of lanes above 1 start with `L2 ·`. One button per session, and **Older** pages back in place.
- Tapping a session shows its start, end, duration, bets with W/L and win rate, wagered, profit, start and end balance, strategy, multiplier and base bet, with **Back** and **Open on dashboard**, a plain link to the dashboard's Sessions page. The bot can't know the dashboard's reset epoch, and a Telegram sign-in opens the limited Telegram account, so the owner signs in there as usual.
- No stop reason is shown: the sessions table doesn't record one.
- It reads `wolfctl stats --json` (argv list, no shell, 10 s timeout). No sessions answers `No sessions on lane 2 yet.`; a failed read answers `Could not read the sessions. Try again.`
- The key picker's **Open dashboard** is now a plain link to the dashboard's Keys page instead of a Telegram sign-in link, which opened the Telegram account rather than Keys.
- `wolfctl stats --before ID` (and `--stats --before ID`) keeps only sessions older than #ID, with the lane and date filters still applied; the totals follow the same window.

**Tests**: `tests/test_tg_sessions.py` (line and detail format, paging, lane filter, argv and timeout, empty and failed reads, usage, forged buttons, owner gate, dashboard link), `tests/test_tg_lanes.py` (Keys link), `tests/test_phase6d_reset_and_datestats.py` (`--before`). Version pins updated.

`VERSION` `2.62.0` -> `2.63.0`; TG `1.30.0` -> `1.31.0`.

## TG v1.30.0 - pick a lane's key and proxy from Telegram (2026-11-25)

- Lanes 2-99 in `/lanes` get **Key K** and **Proxy K**. Each lists the saved keys or proxies from the vault by label and last 4 only (`friend (...a1b2)`); the proxy list adds **No proxy**. The secret is never read, sent or logged. The picker says `Add keys on the dashboard Keys page.` and, when the dashboard sign-in is set up, has an **Open dashboard** button with the same one-use link as `/web`.
- Picking one asks `Use friend (...a1b2) on lane 2? Lane 2 restarts.` with **Use now** or **At session end**. It runs `wolfctl config-set lane2.key_ref=friend` (or `lane2.proxy_ref=...`, `off` for no proxy), then `wolfctl restart 2`, with `--at-boundary` for the session end. Only that lane restarts; a stopped lane keeps the new key for its next start.
- Lane 1 has no Key or Proxy button: its key is the base key that lanes without their own inherit, so it changes on the dashboard Config page. Forged data for lane 1 says so and runs nothing.
- The vault is read again before anything runs: a key deleted since the list answers `friend is no longer in the vault.`, and a key the lane already uses answers `Lane 2 already uses friend (...a1b2).`
- `/setkey` still sets only the Telegram bot's own key. Its reply now says CLI lanes use the saved keys and points at `/lanes`. If Telegram refuses to delete the message holding the key, the bot now says `I could not delete your message. Delete it now: it holds your key.` instead of staying silent.

**Tests**: `tests/test_tg_lanes.py` (picker labels and kinds, empty and unreadable vault, lane 1 refused, forged ids, confirm texts, argv per choice, stopped lane, key already in use, deleted key, failed config-set, owner gate, `/setkey` delete and its failure, no secret in replies or logs). TG version pins updated.

TG `VERSION` `1.29.0` -> `1.30.0`.

## TG v1.29.0 - add, rename and remove a lane from Telegram (2026-11-25)

- `/laneadd NAME [from K]` (owner only) asks `Add the lane friend-B, copying lane 2's settings?` and, on **Add**, runs `wolfctl lane add NAME --from K` (K defaults to 1, which copies nothing). The reply shows what wolfctl printed, such as `Lane 5 (friend-B) added. Other lanes keep betting.`, above the fresh list. No other lane restarts.
- `/lanerename K NAME` asks `Rename lane 4 to night-run?` and, on **Rename**, runs `wolfctl lane rename K NAME`. The lane keeps betting and picks the name up on its next reload.
- Every lane but lane 1 gets a **Remove K** button: `Remove lane 4?` with **Now** (`wolfctl lane remove K`) or **At session end** (`wolfctl lane remove K --after-session`). Lane 1 has no Remove; forged data for it answers `Lane 1 is the main lane and can't be removed.` A stopped lane can't wait for a session end: `Lane 3 is stopped. Remove it with Now.`
- Names follow the CLI's own rule (casino-shared `LANE_NAME_RE`) and may not start with `-`, which wolfctl would read as an option: `Lane names are 1-32 letters, digits, space, _ or -, not starting with - or a space.` The name in the button data is checked again before anything runs; anything else answers `Unknown button.`
- A failed lane action now shows wolfctl's own error line. The CLI prints its errors on stdout, so `No lane 3.` or `This bot already runs 3 lane(s), the limit.` used to show as `Lane 3: stop failed.`

**Tests**: `tests/test_tg_lanes.py` (name rule, command parsing and usage, confirm texts and button bytes, forged add and rename data, argv per verb, lane 1 and stopped-drain refusals, a CLI error on stdout, owner gate). TG version pins updated.

TG `VERSION` `1.28.0` -> `1.29.0`.

## TG v1.28.0 - one-lane buttons in /lanes (2026-11-25)

- Each lane in `/lanes` now has buttons for what its state allows: a running lane **Pause K**, **Restart K**, **Stop K**; a paused one **Resume K** instead of Pause; a stopped one **Start K**; a lane with no state yet **Restart K**, **Stop K**.
- Every button asks first: `Pause lane 2?`, `Stop lane 1? It is the main lane.`, and `Restart lane 2?` with **Restart now** or **At session end** (`wolfctl restart K --at-boundary`). **Cancel** brings the list back.
- A confirmed action runs one `wolfctl` verb on that lane only (argv list, no shell, 30 s timeout) and shows the line wolfctl printed above the fresh list. The other lanes keep betting.
- The lanes are read again before anything runs: a button from an old message answers `Lane 2 is already stopped.`, `Lane 1 is not paused.` or `Lane 9 is not configured.` and runs nothing.
- Only the owner can use the buttons; button data is checked against a strict pattern (known action, lane 1-99), and anything else answers `Unknown button.`

**Tests**: `tests/test_tg_lanes.py` (buttons per state, hostile callback data, owner gate, confirm texts, argv per action, stale buttons, failed and hung wolfctl, Cancel). TG version pins updated.

TG `VERSION` `1.27.0` -> `1.28.0`.

## v2.62.0 / TG v1.27.0 - `/lanes` on Telegram (2026-11-25)

- New Telegram command `/lanes` (owner only): one line per CLI lane with its name, state (running, paused, stopped, or no state yet), key, session id and session profit, plus a **Refresh** button that redraws the list in place. It reads `wolfctl lanes --json` (argv list, no shell, 10 s timeout). Any other chat gets `Only the owner can manage lanes.`, and the Refresh button checks the owner too.
- The key shows as its vault label and last 4 (`friend (...a1b2)`); a key the vault doesn't hold shows by its id (`default`). No secret reaches the chat or the log. A failed read answers `Could not read the lanes. Try again.`; outside production the error type is added.
- Each lane's state file now carries `key_ref`, the vault id of the key it bets with (never the key). The lanes-status JSON row shape is unchanged.
- `/help` and the bot's command menu list `/lanes`.

**Tests**: `tests/test_tg_lanes.py` (format, owner gate on the command and the button, argv and timeout, failed reads, key label), `tests/test_phase10_lanes.py` (the state carries `key_ref` and no secret). Version pins updated.

`VERSION` `2.61.3` -> `2.62.0`; TG `1.26.1` -> `1.27.0`.

## v2.61.3 - lane accounts and secret checks come from casino-shared; a lane-count preview no longer says restart (2026-11-25)

- `_lane_account`, `_split_lanes` and the checks on a key, proxy or Telegram token sent to the vault now use casino-shared 0.11.0. Nothing changes in what the bot or `wolfctl` does or prints. With an older casino-shared the CLI prints `casino-shared 0.11.0 or newer is needed: run wolfctl update`.
- `--config-dry-run` (the dashboard's **Preview** on the Lanes page) marked every `lanes` and `lane_split` change as `restart`, although `wolfctl lanes N` only starts or stops the lanes that differ and the running lanes keep betting. A count change, and a split change between `none` and `bet`, now say `live`.
- A split change to or from `budget` or `both` still says `restart`, because budget shares apply when each lane restarts. Any lane-count or split preview with a budget split also carries the warning `Budget shares update when each lane restarts: wolfctl restart K.`

**Tests**: the whole suite passes; `tests/test_phase11h_config_schema.py` adds the count and split live cases and the budget split restart plus warning. The version pins are updated.

`VERSION` `2.61.2` -> `2.61.3`.

## v2.61.2 - lane registry writes come from casino-shared (2026-11-24)

- The config-set lane pair parser, the next lane id, the enabled-count change and the written `lanes` block now come from casino-shared 0.10.0 instead of copies in `wolfbet.py`, so goated and the other bots use the same code. Nothing changes in what the bot or `wolfctl` does or prints.
- With casino-shared older than 0.10.0 the CLI now prints `casino-shared 0.10.0 or newer is needed: run wolfctl update` and exits 1. Push casino-shared first.

**Tests**: the whole suite passes unchanged. The version pins are updated.

`VERSION` `2.61.1` -> `2.61.2`.

## v2.61.1 / TG v1.26.1 - lanes, session-end requests and vault reads come from casino-shared (2026-11-24)

- The lane registry (named lanes and the older `lanes.count` shape), the lane file paths, the session-end request file and the vault reads now come from casino-shared 0.9.0 instead of copies in `wolfbet.py`, so the other bots reuse them. Nothing changes in what the bot or `wolfctl` does or prints.
- The Telegram bot reads its token from the vault through the same casino-shared helper.
- With casino-shared older than 0.9.0 the CLI prints `casino-shared 0.9.0 or newer is needed: run wolfctl update` and exits 1 instead of a traceback. Push casino-shared before this version: `wolfctl update` installs it from `main`.

**Tests**: `tests/test_phase16_shared_helpers.py` (the bot uses the shared helpers; an older casino-shared is named). The whole suite passes unchanged. The version pins are updated.

`VERSION` `2.61.0` -> `2.61.1`; TG `1.26.0` -> `1.26.1`.

## v2.61.0 - config-show reports the update rollout (2026-11-24)

- `--config-show --json` gains `rollout`: for each running lane, which code it runs during an update (`new`, `waiting for session end` or `old`, the same rule as the **Code** column in `wolfctl status`). The dashboard Control page shows it beside each lane's pending changes. Stopped lanes are left out. The table output is unchanged.

**Tests**: `tests/test_phase16_pending_restart.py` (rollout for running lanes only), the config-show contract. The version pins are updated.

`VERSION` `2.60.0` -> `2.61.0`.

## v2.60.0 / wolfctl v3.26 - an update rolls through the lanes (2026-11-24)

- `wolfctl update` installs the new code, then asks each running lane to restart at its own session end (the `--at-boundary` restart from v2.59.0) instead of stopping every lane at once: `Lane 1 restarts on the new code when its session ends.` per lane, then `Betting goes on; use wolfctl update --critical to restart every lane now.` Stopped lanes start on the new code whenever they are started. The Telegram bot restart is unchanged.
- `wolfctl update --critical` keeps the old behaviour: every lane stops and restarts at once.
- The lanes table in `wolfctl status` gains a **Code** column: `new` (running the installed version), `waiting for session end` (old code, restart pending) or `old`. Each lane's state file now records the version it runs (`code_version`). `--lanes-status --json` is unchanged.

**Tests**: `tests/test_phase16_rolling_update.py` (an update asks only the running lanes to restart and stops none, `--critical` restarts now, a stopped bot asks nothing, a bad option exits 2), `tests/test_phase16_lane_drain.py` (which code each lane runs), the lanes table snapshot. The version pins are updated.

`VERSION` `2.59.0` -> `2.60.0`; wolfctl `3.25` -> `3.26`.

## v2.59.0 / wolfctl v3.25 - restart or remove a lane at its session end (2026-11-24)

- `wolfctl restart K --at-boundary` asks lane K to restart when its current session ends: `Lane K restarts when its session ends. It keeps betting until then.` At that session close the lane flushes its bet writer, state file and events, then re-executes itself in place (same unit), so the next session runs on the saved config and code. Other lanes are not touched. `--cancel-boundary` forgets the request.
- `wolfctl lane remove K --after-session` removes lane K at its session end the same way `wolfctl lane remove K` does (the lane runs it itself), instead of stopping it at once. Lane 1 and unknown lanes are refused with the usual messages.
- With recurring off a lane already stops at its session end; a pending restart is then dropped and the lane starts on the new config whenever it is next started. A pending removal still removes it.
- The request is a small file per lane (`~/.wolfbet_autobot.drain`, `.laneK` for other lanes, 0600) read once per session close, never per bet, and deleted before it is acted on. New bot flag `--lane-drain K restart|remove|cancel`.

**Tests**: `tests/test_phase16_lane_drain.py` (request written, cancelled and refused; restart flushes then re-executes; recurring off drops it; removal hands the lane to wolfctl; the betting loop restarts or stops only at the session end), `tests/test_phase11i_wolfctl_verbs.py` (the three flags reach the bot and touch no unit; bad arguments exit 2). The version pins are updated.

`VERSION` `2.58.0` -> `2.59.0`; wolfctl `3.24` -> `3.25`.

## v2.58.0 - see which lanes still wait for a restart (2026-11-24)

- On every reload a running lane records the saved restart-only settings it is not using yet (for example `multiplier` 3 while 4 is saved). `--config-show --json` lists them per running lane under `pending` (`{"2": [{"key": "multiplier", "running": 3.0, "saved": 4.0}]}`); a restart clears them. Live settings still apply at once, as before. The raw key is never listed, and the proxy and events URL are masked.
- Each new session row stores the settings it started with in `config_snapshot`, masked the same way, written once when the session opens.
- No bet maths, stop logic or schema changed (`config_snapshot` already existed).

**Tests**: `tests/test_phase16_pending_restart.py` (a restart-only change is pending while a live one applies, a change set back clears it, a stopped lane lists nothing, no secret in the list, a session keeps a masked snapshot). The config-show contract copy and its checksum are updated; the version pins are updated.

`VERSION` `2.57.0` -> `2.58.0`.

## v2.57.0 / TG v1.26.0 - the Telegram bot token lives in the key vault (2026-11-24)

Needs casino-shared v0.8.0.

- `--key-put telegram --key-kind=telegram` (token on stdin) stores the Telegram bot token in the host vault, encrypted and never printed; `--key-rotate telegram` replaces it. The kind is only accepted under the id `telegram`, and a value that isn't a bot token is refused with `That doesn't look like a Telegram bot token.` without repeating it. Both answer `Restart the Telegram bot to use the new token.`
- The Telegram bot reads its token from the vault at start and falls back to `WOLFBET_TG_TOKEN`, so hosts that keep the token in `.env` work as before. A vault it can't open is logged without the token.

**Tests**: `tests/test_phase15_vault.py` (stored only as `telegram`, bad token refused without echo, put and rotate notes, the bot reads the vault first and the env second). The version pins are updated.

`VERSION` `2.56.0` -> `2.57.0`; TG `VERSION` `1.25.0` -> `1.26.0`.

## v2.56.0 - a saved events feed beats the host env (2026-11-24)

- `events_url` saved in the config (from `wolfctl config-set` or the dashboard) now applies at start even when `WOLFBET_EVENTS_URL` is set on the host. The env var only fills in a config that has no `events_url`, so fresh installs keep working as before. The Telegram bot's own `WOLFBET_EVENTS_URL` is unchanged.

**Tests**: `tests/test_phase11e_events_config.py` (a saved URL beats the env, the env seeds an empty config). The version pins are updated.

`VERSION` `2.55.0` -> `2.56.0`.

## v2.55.0 - every setting says what a change costs (2026-11-24)

- One table now holds every setting with how a change reaches running lanes (`live`: a reload applies it now; `lane`: the lanes using it restart; `service`: shared by every lane, so every lane restarts) and whether one lane may override it. The live-reload and shared-key lists are read from it; their contents are unchanged.
- `--config-schema --json` gives each key `"restart": "live" | "lane" | "service"`, next to `live_reload` and `lane_overridable`, which keep their values.
- `wolfctl config-set` names the cost: a restart-only base change prints `Restart every lane to apply: game (wolfctl restart).`, and a restart-only lane override (set or removed) prints `Restart lane 3 to apply: game (wolfctl restart 3).` A live change prints neither.
- No bet maths, stop logic or reload behaviour changed.

**Tests**: `tests/test_phase11h_config_schema.py` (one class per setting, both lists equal their old contents, `restart` in the schema, the lane and base notes), the config-set output snapshots, `tests/test_phase14_session_box.py` (the base note). The config-schema contract copy and its checksum are updated; the version pins are updated.

`VERSION` `2.54.0` -> `2.55.0`.

## v2.54.0 - lanes on different keys never slow each other (2026-11-24)

- The `/auto/start` spacing between lanes now applies per key: lanes on the same key share one lock file and still never fire `/auto/start` in the same instant, while a lane on another key uses its own file and never waits for them. The main key keeps the original `~/.wolfbet_autostart.lock`; another key uses `~/.wolfbet_autostart.<key>.lock`. A lane alone on its key takes no lock at all.
- The per-bet soft pace was already per lane process, from that lane's own rate-limit headers, so it already follows each key; unchanged.
- A bot with one key for every lane paces exactly as before.

**Tests**: `tests/test_phase10_lanes.py` (lanes counted per key whatever the currency, lock path per key, a lane alone on its key never waits and creates no file, two lanes on one key share only that key's file). The version pins are updated.

`VERSION` `2.53.0` -> `2.54.0`.

## v2.53.0 - each lane bets with its own key, currency and proxy (2026-11-24)

Needs casino-shared v0.7.0.

- New config keys `key_ref` (default `default`) and `proxy_ref` (default off), both lane-overridable: `wolfctl config-set lane3.key_ref=friend-a lane3.proxy_ref=friend-a-proxy`. Each must name a vault entry of the right kind, or the set is refused (`Invalid lane3 override: no key named friend-a in the vault`) and nothing is written. `--config-schema` gives them the format `vault_id`.
- `currency` and `proxy` may now differ per lane. A lane proxy that carries a password is refused (`a proxy with a password goes in the vault: add it as a proxy key and set proxy_ref`) without repeating it; the base `proxy` works as before.
- Key resolution: `default` keeps the order from v2.51.0 (`WOLFBET_API_KEY`, the vault, the config copy). Any other `key_ref` comes from the vault only. A lane whose key or proxy is missing from the vault logs `Lane 3: key friend-a is not in the vault` and stops at start instead of betting from another account. `proxy_ref` is read at start and on every reload, so a reload never drops the vault proxy.
- **Bet maths:** the lane split now counts only the enabled lanes on the same account (same `key_ref` and currency). With `budget`, lanes 1-2 on `default` each risk half of that balance and a lane on its own key risks all of its own; with `bet`, the base bet is divided the same way. A bot with one key and one currency splits exactly as before.
- After `--config-set`, the running lanes whose key, currency or `proxy_ref` changed are restarted one by one (`Lane 3 restarted: its key, currency or proxy changed.`); every other lane keeps its process. A plain `proxy` URL still applies live without a restart. Without systemd the command prints `Restart lane 3 to apply: wolfctl restart 3`.
- `--key-put ID --key-kind proxy` stores a proxy URL (http, https or socks5) in the vault from stdin; a bad one is refused without repeating it. `--key-list` rows carry `kind`, and `used_by_lanes` now follows each lane's `key_ref` or `proxy_ref`, so deleting an entry a lane uses is refused.
- The saved config gains `"key_ref": "default"` and `"proxy_ref": null`; `--config-show` lists both.
- **Rollback:** v2.52.0 ignores `key_ref` and `proxy_ref` and would bet every lane with the main key and the base currency. Remove the lane overrides `key_ref`, `proxy_ref` and `currency` (`laneK.key_ref=default`) before rolling back.

**Tests**: `tests/test_phase15_vault.py` (a lane's own key, currency and vault proxy; the proxy kept on reload; a missing key or proxy never falling back; four refusals without echo; only the changed lane restarted; a base currency change restarting the lanes that inherit it; key and proxy users; proxy entries), `tests/test_phase10_lanes.py` (split per account; one account as before). The tests that pinned `currency` and `proxy` as lane-forbidden now use `purge_days` and `db_write_mode`. The config-set and config-show snapshots, the config-schema fixture (`vault_id` format, checksum) and the version pins are updated.

`VERSION` `2.52.0` -> `2.53.0`.

## v2.52.0 - add, rotate, list and delete vault keys from the command line (2026-11-23)

Needs casino-shared v0.6.0. The dashboard agent calls these; nothing here prints a secret.

- `--key-list --json`: every vault key with `id`, `label`, `last4`, `owner_user_id`, `created_at`, `rotated_at` and `used_by_lanes`. Every enabled lane uses `default` until lanes can pick their own key.
- `--key-put ID [--key-label TEXT] [--key-owner USER] --json` and `--key-rotate ID --json` read the key from one line of stdin, so it never shows in a process list. A key must be 8-4096 printable characters with no spaces. `--key-put` refuses an id that exists (`key_exists`), `--key-rotate` one that does not (`key_unknown`). A rotate keeps the label and owner and notes that lanes pick the new key up when they restart; rotating `default` while `WOLFBET_API_KEY` is set adds that `.env` still replaces it when lane 1 starts.
- `--key-delete ID --json` refuses a key a lane uses: `This key is used by lane 1, 2. Move the lanes to another key first.` (`key_in_use`).
- Refusals exit 2 with `{"error": {"code", "message"}}`; a vault that can't be used exits 1 with `vault_unavailable`. The log records the key id and what was done, never the key.

**Tests**: `tests/test_phase15_vault.py` (put from stdin with nothing printed, list with lanes, six refusals that leave the vault alone, rotate keeping label and owner, the `.env` warning, delete, no vault module). The version pins are updated.

`VERSION` `2.51.0` -> `2.52.0`.

## v2.51.0 - the API key lives in the host key vault (2026-11-23)

Needs casino-shared v0.5.0 (`wolfctl update` installs it).

- The key the bot bets with is `WOLFBET_API_KEY` when it is set, else the vault key `default` (`~/.casino-vault/wolfbet/default.json`, AES-256-GCM, 0600), else the config file's `api_key`. It is read when a lane starts, never per bet.
- When lane 1 starts (`--resume`, `--daemon`), and when the config is saved (wizard, `--config-set`), the vault's `default` is made equal to the key in use: `WOLFBET_API_KEY` replaces it when it differs, a key found only in the config file is moved in. Once the vault returns the same key, `api_key` is removed from `~/.wolfbet_autobot.json` and is no longer written there. The first removal copies the file once to `~/.wolfbet_autobot.json.pre-vault` (0600). Lanes 2+ only read.
- A vault that can't be used (casino-shared without the vault module, a vault key file other users can read, a wrong or missing vault key, a damaged file) logs `Key vault unavailable: <reason>` with no secret in it. The bot then uses `WOLFBET_API_KEY` or the config copy as before, and the config keeps its key.
- The log says `API key stored in the vault as default` and never shows the key. Read-only commands (`--status`, `--config-show`, `--lanes-status`) read the vault but never write it.
- **Rollback:** v2.50.0 reads the key only from `WOLFBET_API_KEY` or the config file. Keep `WOLFBET_API_KEY` in `.env` before rolling back, or restore `~/.wolfbet_autobot.json.pre-vault`.

**Tests**: `tests/test_phase15_vault.py` covers the env key moving in with the one-time 0600 backup, an env change replacing the vault copy, a config-only key moved in, a later start on the vault key alone, the backup written once, a broken vault falling back with nothing stripped, lanes 2+ only reading, read-only commands writing nothing, `save_config` with and without a working vault, and the key never reaching the log. `tests/conftest.py` gives every test its own vault under its temp dir. The `set_live_clamped`, `set_mixed`, `set_restart` and `set_secrets` snapshots no longer hold `api_key`. The version pins are updated.

`VERSION` `2.50.0` -> `2.51.0`.

## v2.50.0 - the lane name is in the live state (2026-11-23)

- Each lane's state (the state file, `--status --json` and the state event) carries `lane_name`: the name set with `wolfctl lane rename` or `laneK.name=`, or null while the lane has its default `Lane K`. The dashboard shows it on the lane card.
- A live reload (SIGHUP, as `wolfctl lane rename` sends) now picks up a new name at once: the log prefix and `lane_name` change without a restart. Before, the new name only showed after the lane restarted.

**Tests**: `tests/test_phase10_lanes.py` checks the state carries the name of a named lane and null for a default one, and that a reload after a rename changes both the name and the log prefix. The version pins are updated.

`VERSION` `2.49.0` -> `2.50.0`.

## v2.49.0 + wolfctl v3.24 - add, remove or rename one lane; lanes N changes only the difference (2026-11-23)

- `wolfctl lane add NAME [--from K]` adds one lane under the next free id, copies lane K's overrides (lane 1 has none, so the new lane uses the base config) and starts it. The running lanes keep their process.
- `wolfctl lane remove K` stops lane K through systemd (state saved, session closed, as `wolfctl stop K`) and drops it from the config. Its id is never given to a new lane, so its history in the DB stays its own. Lane 1 can't be removed: it holds the base config.
- `wolfctl lane rename K NAME` is `--config-set laneK.name=NAME`; only lane K reloads.
- `wolfctl lanes N` starts or stops only the lanes that change. It no longer stops every lane and restarts the main service. Raising the count enables disabled lanes first (lowest id, with their names and overrides), then adds new ids; lowering it disables the highest ids.
- `lanes.max` (default 10, up to 99) is how many lanes may be enabled at once: `wolfctl lanes --max N` or `--config-set lanes_max=N`. It can't go below the lanes configured. Lane ids go up to 99 and are no longer capped at 10. `--config-schema` reports the configured limit as `lanes.count.max` and 99 as `override_lanes.max`.
- When the lane count changes, a lane split of `bet` or `both` reloads the remaining lanes, so each one's base bet ÷ lanes follows at once. On reload a lane now also takes the new lane count, so `basebet_pct` divides by it too. A `budget` or `both` split keeps each lane's budget share until that lane restarts, and wolfctl says so: `Budget shares update when each lane restarts: wolfctl restart K.`
- `wolfbet.py --lane-ids [all]` prints the enabled lane ids (or every id); wolfctl reads lanes through it instead of its own copy of the parser. `wolfctl pause|resume|reload all` signals every lane with a pid file; `stop`, `stop-all` and `restart` go through the registry ids. `wolfctl run-reset` clears every lane's run file.
- A config in the old shape with overrides above the count (for example lane 5) now brings that lane back first on `lanes N`, instead of a new empty lane 4.
- **Rollback:** v2.48.0 rejects lane ids above 10 and ignores `lanes.max`. Before going back, run `wolfctl lanes N` with N no higher than 10 and remove lanes whose id is above 10.

**Tests**: `tests/test_phase10_lanes.py` covers adding with copied overrides, never reusing a removed id, refused changes writing nothing (lane 1, unknown lanes, bad names, the limit), the id ceiling, `lanes=N` up and down on sparse ids, the owner limit, a lane id above 10, which lanes a count change signals for each split, a reload taking the new count, run-reset over every lane file and `--lane-ids` for both config shapes. `tests/test_phase11i_wolfctl_verbs.py` checks from the systemctl call log that `lane add` starts only the new unit, `lane remove` disables only its lane, `lanes N` starts or stops only the difference and never restarts the main service, plus rename, `--max`, refusals and bad arguments reaching no unit. The `set_lanes` and `err_lanes_count` snapshots and the version pins are updated.

`VERSION` `2.48.0` -> `2.49.0`, `WOLFCTL_VERSION` `3.23` -> `3.24`.

## wolfctl v3.23 - stop, start or restart any one lane, lane 1 included (2026-11-23)

- `wolfctl stop K`, `start K` and the new `restart K` act on lane K alone, for K from 1 to the lane count. Lane 1 is still the main service (`wolfbet.service`); `stop 1` stops only that unit and the other lanes keep betting.
- `start K` and `restart K` check the unit is active afterwards (`Lane K did not start (state).`, exit 1) and need a saved config. `stop K` checks the unit is down.
- A lane outside 1 to the lane count exits 2 with `No lane K: this bot runs N lane(s).`; a non-number prints the usage. Nothing is sent to systemd in either case.
- Plain `wolfctl stop`, `start` and `restart` still act on every lane, as before.

**Tests**: `tests/test_phase11i_wolfctl_verbs.py` records every systemctl call and checks that `stop 1`, `start 1` and `restart 2` touch only their own unit, that a lane that stays down fails the restart, that `start K` needs a config, that bad lane numbers reach no unit, and that plain `restart` still restarts every lane. The version pins are updated.

`WOLFCTL_VERSION` `3.22` -> `3.23`.

## v2.48.0 + wolfctl v3.22 - lanes have names (2026-11-23)

- Lanes live in a registry: `lanes.items` holds each lane by id with a `name`, an `enabled` flag and its `overrides`, plus `lanes.next_id`. It replaces `lanes.count` and `lanes.overrides`.
- A config in the old shape reads exactly as before: lanes 1 up to the count are enabled and named `Lane 1`, `Lane 2`, ... The file keeps its old shape until the next lane change (`lanes=N`, `lane_split`, `laneK.key=` or a rename) writes the registry.
- `--config-set laneK.name="friend-A"` names a lane; lane 1 can be named too. A name is 1 to 32 letters, digits, spaces, `_` or `-`; anything else exits 2 and nothing is written. Only that lane gets SIGHUP, and its log lines then read `[L3 friend-A]`. `--config-set --dry-run` reports a rename as `live`.
- `lanes=N` enables lanes 1 to N and disables the ones above N. A disabled lane keeps its name and overrides, as overrides above the count were kept before.
- `--lanes-status` (`wolfctl status` with lanes) adds a Name column once a lane has its own name.
- `wolfctl` counts the enabled lanes of the registry, or `lanes.count` in an old file.
- **Rollback:** the first registry write copies the old file to `~/.wolfbet_autobot.json.pre-registry` (mode 0600, written once). Older code reads a registry file as one lane, so restore that copy before going back to v2.47.0.

**Tests**: `tests/test_phase10_lanes.py` covers the old shape read as a registry, enabled count and disabled overrides, lane count changes keeping names and overrides, a rename signalling only its lane and setting the log prefix, a reload picking up a rename, refused names leaving the file alone, the one-time backup and its mode, the dry-run entry, the Name column and the wolfctl lane count for both shapes. The `set_lanes` and `set_mixed` snapshots now show the registry. The version pins are updated.

`VERSION` `2.47.0` -> `2.48.0`, `WOLFCTL_VERSION` `3.21` -> `3.22`.

## v2.47.0 - the config schema carries the rule menus (2026-11-22)

- `--config-schema` gives `custom_rules_text` a `rule_options` object: the condition types, sequence modes and events, profit and bet fields, comparison operators, and every rule action with whether it takes a value. These are the setup wizard's own tables, so a rule builder (the dashboard's Config page) offers exactly what the bot runs. The schema file allows the new object.

**Tests**: `tests/test_phase11h_config_schema.py` checks the menus match the wizard's tables, that reset, switch and stop take no value, and that only the rules key carries them. The version pins are updated.

`VERSION` `2.46.0` -> `2.47.0`.

## v2.46.0 + wolfctl v3.21 - each lane can bet its own game, and one lane can be stopped (2026-11-22)

- `game` and `rule` may now differ per lane: `--config-set lane2.game=limbo lane2.rule=under`. Each lane is its own process with its own auto-bet state, and it derives `bet_value` from its own multiplier and rule when it loads. `--config-schema` reports both as `lane_overridable`, so the dashboard's lane tabs show them. `currency`, the API key, proxy and storage settings stay shared.
- `wolfctl stop K` and `wolfctl start K` stop or start only lane K (2 up to the lane count). The other lanes keep betting. The result is checked with systemd: a lane still running after stop, or not running after start, exits 1; a systemd it cannot reach exits 1 too. Lane 1 is the main service, so `stop 1` exits 2 and points to plain `stop`; a lane above the count exits 2. `wolfctl start` and `restart` without K still bring every lane back.

**Tests**: `tests/test_phase11h_config_schema.py` and `tests/test_phase10_lanes.py` cover a per-lane game and rule, a lane loading them with its own bet value, and currency still refused per lane. `tests/test_phase11i_wolfctl_verbs.py` covers stop and start of one lane, a lane that stays up, one that does not start, and the refused lane numbers. The version pins are updated.

`VERSION` `2.45.0` -> `2.46.0`, `WOLFCTL_VERSION` `3.20` -> `3.21`.

## v2.45.0 - config-set takes currency, rule, the Delay Martingale threshold and custom rules (2026-11-22)

- `--config-set` (and the dashboard's Config page) now accepts four keys only the setup wizard could set: `currency` (one of the supported coins), `rule` (`over` or `under`), `delay_martin_threshold` (a whole number of at least 1) and `custom_rules_text` (Rule-Based rules as JSON or `onEvery...` lines, at most 4000 characters; `off` clears them).
- A `rule` change re-derives `bet_value` from the multiplier, as a `multiplier` change already does, so the casino never gets a stale win chance.
- `custom_rules_text` is parsed before it is saved; text that holds no valid rule is refused with "Custom rules: no valid rule found". It reloads live; the other three apply after a restart.
- `rule` and `delay_martin_threshold` cannot differ per lane, like `currency`.
- `--config-schema` publishes the rules key with `format: "rules"` and `max_length`; the schema file allows both.

**Tests**: `tests/test_phase11h_config_schema.py` covers each key, valid and refused, the bet_value re-derive, the lane refusal and the published limit. The unknown-key snapshot lists the new keys. The version pins are updated.

`VERSION` `2.44.0` -> `2.45.0`.

## wolfctl v3.20 - stop-all only reports what really stopped (2026-11-21)

- `wolfctl stop-all` now stops lanes 2-10 too, as `wolfctl stop` already does. Before, extra lanes kept betting after "Stop all".
- It then asks systemd for each bot unit (`wolfbet`, `wolfbet-tg`, `wolfbet-lane@2..10`). A unit still running prints `Still running: <unit> (<state>)`, and a user manager it cannot reach (no `XDG_RUNTIME_DIR`) prints `Cannot reach the user's systemd ...`; both exit 1. "All services stopped." is printed only when every unit is down. Before, each stop was `|| true`, so an unreachable systemd still printed "All services stopped." while the bots kept betting.

**Tests**: `tests/test_phase11i_wolfctl_verbs.py` gets a `systemctl` stub that prints unit states like systemd, and two new tests: a lane left running, and no reachable systemd. The version pins are updated.

`WOLFCTL_VERSION` `3.19` -> `3.20`.

## wolfctl v3.19 - dash finds the Coolify-deployed stack (2026-11-21)

- `wolfctl dash status|logs|start|stop` now run `docker compose -p wolfbet-dashboard`, the project name `deploy/compose.yml` sets for itself. They used the Coolify app id (`DASH_PROJECT`), but Coolify keeps the compose file's name, so they found no containers: `status` showed an empty table, and `stop` and `start` did nothing. To use another project, set `DASH_COMPOSE_PROJECT=` in `~/.config/casino-dashboard/dash.env`.
- `DASH_PROJECT` is now only used by `wolfctl dash update`, to queue the Coolify deploy. The stack commands no longer stop when it is unset.

**Tests**: `tests/test_phase11k_wolfctl_dash.py` expects the compose project. Two new tests: the stack commands work without a Coolify app, and an override is honoured. The version pins are updated.

`WOLFCTL_VERSION` `3.18` -> `3.19`.

## wolfctl v3.18 / TG v1.25.0 - the old web is removed (2026-11-21)

- `web/` is deleted: the FastAPI app, its `install.sh`, its requirements and its three test files. The dashboard serves its pages (`casino-dashboard/docs/parity/old-web.md` lists where each one went). `casino-shared/web` stays, because the five other bots still import `shared.web`.
- `wolfctl web remove` stops, disables and deletes a leftover `wolfbet-web` unit, and keeps `~/wolfbet-web` (its database and `.env`) for you to delete by hand. Every other `wolfctl web ...` only prints where the dashboard is.
- `status-all` lists the old web only while its unit file still exists. `stop-all` still stops it. `wolfctl update` no longer copies or restarts any web files.
- TG `/web` sends the dashboard sign-in link only. Without `WOLFBET_DASHBOARD_URL` and a good `WOLFBET_TG_DASHBOARD_SECRET`, it replies "The web dashboard isn't set up yet." and logs what is missing. The old-web JWT link, `WOLFBET_WEB_URL` and `WOLFBET_WEB_SECRET_KEY` are gone, and so is `python-jose`.
- **Rollback:** revert this commit and run `wolfctl update`. `wolfctl web restore` no longer exists.

**Tests**:
- `tests/test_phase11r_wolfctl_web_remove.py` (10) replaces the retire tests;
- the `/web` tests expect the "isn't set up yet" reply;
- the version pins are updated.

`WOLFCTL_VERSION` `3.17` -> `3.18`, `tg.VERSION` `1.24.0` -> `1.25.0`.

## wolfctl v3.17 - retire the old web (2026-11-21)

- `wolfctl web retire` stops and disables `wolfbet-web`. Its unit and files stay, so `wolfctl web restore` (enable and start) is the rollback. With no web unit installed, retire says so and exits 0.
- Every `wolfctl web ...` first prints that the old web is retired, with the dashboard URL from `~/.config/casino-dashboard/dash.env` (`DASH_HOST`). Without that file, it prints how to install the dashboard.
- `status-all` shows the web as retired once it is disabled, and flags it when it still runs. `stop-all` now stops it too; `start-all` still never starts it.
- `wolfctl update` keeps copying the web files, so a restore runs current code, but never restarts a retired web.
- Nothing changes on the host until `wolfctl web retire` is run.

**Tests**: `tests/test_phase11r_wolfctl_web_retire.py` (7), with a stubbed `systemctl`: retire and restore, a failed restore, retire with nothing installed, the dashboard line on every web command, `status-all`, `stop-all` and `start-all`.

`WOLFCTL_VERSION` `3.16` -> `3.17`.

## TG v1.24.0 - /web signs in to the dashboard (2026-11-21)

- `/web` sends a dashboard sign-in link once `WOLFBET_DASHBOARD_URL` and `WOLFBET_TG_DASHBOARD_SECRET` are set. Until then, or if the secret is weak (under 32 bytes, too few distinct characters, or a placeholder), it sends the old web link as before.
- The link is `<dashboard>/tg/login#token=...`:
  - valid for 5 minutes, for one sign-in, for the sender's own Telegram user only;
  - the token sits in the URL fragment, so it never reaches a server log or a Referer header;
  - it is `base64url(payload).base64url(HMAC-SHA256)` over compact JSON `{exp, id, uid, v}`, signed with the dashboard's `tg_login_secret` (`tg/dashboard_link.py`).
- New control socket command `config`: the user's saved settings without the API key and the proxy, for the dashboard's read-only settings view.

**Tests**: `tests/test_phase11q_tg_dashboard_link.py` (13):
- the signer reproduces the dashboard's golden token in `tests/fixtures/tg_login_v1` (SHA-256 pinned);
- the link shape and its five minutes, and two links never sharing an id;
- weak secrets refused, and `/web` falling back to the old web link;
- `config` scoped per user and never returning the API key or proxy.

`tg.VERSION` `1.23.0` -> `1.24.0`.

## TG v1.23.0 - control socket for the dashboard's host agent (2026-11-21)

- New, off by default: with `WOLFBET_TG_CONTROL=on` the bot listens on `<data dir>/control.sock` (`WOLFBET_TG_CONTROL_SOCKET`). It accepts `start`, `stop`, `pause`, `resume`, `set` and `status` for one Telegram user at a time (`uid`, plus `slot` when the user runs several sessions).
- Same protocol as the casino agent:
  - JSON lines signed with HMAC-SHA256 over the canonical JSON;
  - a 30 s timestamp window and one-time ids;
  - a closed request shape and a fixed command table;
  - generic error codes (`bad_request`, `not_found`, `unauthorized`, `replayed`, `busy`, `internal`). The reason is logged only in the bot, and never a token or a setting value.
- Only the bot's own uid may connect (add more with `WOLFBET_TG_CONTROL_ALLOW_UIDS`). The socket is 0600.
- The token is `<data dir>/control.token` (`WOLFBET_TG_CONTROL_TOKEN_FILE`). The bot creates it once with 0600 and never overwrites it. The bot refuses the token, and the socket stays off, if group or others can read it or it is too short or simple. The bot itself keeps running.
- A command only touches the named user's sessions:
  - a slot that belongs to someone else, or no running session, gives `not_found`;
  - several sessions and no slot gives `bad_request`.
- No logic is duplicated:
  - `start` runs `/bet`'s new `start_session`;
  - `set` runs `/set`'s new `apply_setting` (same rules, same messages);
  - `stop` runs `/stop`'s new `stop_slot`;
  - pause and resume call the engine as `/pause` and `/resume` do;
  - `status` returns the same snapshots as the state events.
- The Telegram replies of `/bet`, `/set` and `/stop` are unchanged.

**Tests**: `tests/test_phase11p_tg_control.py` (37):
- the wire format matches the dashboard's golden fixtures in `tests/fixtures/agent_proto_v1` (SHA-256 pinned);
- per-user scoping, and `/set` rules with nothing saved on refusal;
- the start path, every refusal code and the token file rules;
- a real unix socket: owner-only mode, a refused peer, and never replacing a non-socket file.

`tg.VERSION` `1.22.0` -> `1.23.0`.

## TG v1.22.0 - dashboard events from every Telegram user's engine (2026-11-21)

- New, off by default: with `WOLFBET_TG_EVENTS=on` and `WOLFBET_EVENTS_URL` (the same Redis URL the CLI daemon uses) in the TG environment, the bot starts one event publisher for the whole process when it starts, and flushes it on shutdown.
- Each user's engine publishes to `wolfbet-tg:<uid>:bets`, `:state` and `:lifecycle`. The envelope is the CLI's schema v1 with `svc` `wolfbet-tg`, and every payload carries `uid`.
- Bets use the CLI field order. State is a snapshot under the CLI's key names, every 2 s and while paused. Lifecycle covers start (`resumed` true after a restart), session_open, pause, resume, reload, error (insufficient balance), session_close and stop.
- A reload event lists only the names of the changed settings. Values such as the API key never leave the process.
- `seq` counts per user, so a gap in one user's stream means that user lost events.
- Engines only enqueue. A slow, down or refusing Redis drops and counts events and never slows or stops a session. A missing or invalid URL logs one warning and leaves events off.
- `core/events.py`: `EventPublisher(scoped=True)` serves many users from one sender thread. The unscoped path the CLI uses is unchanged, and the events URL validator moved here from `wolfbet.py` so both bots share it.
- The dashboard's Redis ACL lets the publisher append to, and the dashboard read, `wolfbet-tg:*` (casino-dashboard).

**Tests**: `tests/test_phase11l_tg_events.py` (15, plus one bench):
- per-user streams, `uid` in the payload and per-user seq;
- overflow that uses up only the dropped user's seq;
- the CLI stream names unchanged;
- the engine's bet, lifecycle and state events, with no secret in reload;
- 10,000 bet emits under 100 ms while Redis is stuck;
- events off imports no redis, and a bad URL gives one warning.

`tg.VERSION` `1.21.4` -> `1.22.0`.

## wolfctl v3.16 - dash: run the dashboard from wolfctl (2026-11-20)

- New `wolfctl dash status|logs [SERVICE|agent]|start|stop|update` for the casino dashboard, which runs as a Coolify Docker Compose resource next to the bots. It reads `~/.config/casino-dashboard/dash.env`, written by `casino-dashboard/deploy/install.sh`; without it every subcommand says to run the installer and exits 2.
- `status` shows whether the `casino-agent` unit runs, `docker compose -p <coolify app> ps` and the answer of `https://<host>/api/healthz`. `logs` follows one service (`api`, `caddy`, `ingest`, `mailer`, `migrate`, `redis`, `timescale`), the whole stack, or the agent's journal; any other name exits 2.
- `start` and `stop` start or stop the stack's containers and the agent. They never touch the `wolfbet*` units.
- `update` fast-forwards `~/casino-dashboard` (or `WOLFCTL_DASH_DIR`), re-runs the agent installer and asks Coolify to deploy the app. The Coolify token is sent from a 0600 header file, never on the command line. It refuses a checkout with local changes (exit 4, files listed), and without a saved token it says to redeploy in Coolify.

**Tests**: `tests/test_phase11k_wolfctl_dash.py` (11): stubbed docker, systemctl, journalctl and curl with real git against a temp origin: each subcommand's exact calls, the bots never touched, a missing install or app, an unknown service, update pulling, updating the agent and deploying, the token kept off the command line, a dirty checkout refused with nothing run, a refused deploy.

## wolfctl v3.15 - branch deploys and the deployed branch in status (2026-11-18)

- `wolfctl update --branch NAME`, run in the repo checkout, moves the checkout to NAME and then runs the normal update (copy, dependencies, restarts). It only fetches `origin`, switches (creating NAME to track `origin/NAME` the first time) and fast-forwards; it never resets or forces. Exit 4 with the changed files listed when the checkout has local changes (untracked files included), exit 2 for a name `git check-ref-format` rejects, a name starting with `-`, a branch that exists nowhere or a run outside a checkout, exit 1 when fetch or the fast-forward fails (for example after the branch was rewritten on origin).
- `wolfctl update` without `--branch` works as before (pull with autostash on the current branch); it now also accepts `--yes` explicitly and refuses unknown options with exit 2.
- `wolfctl status` adds a Code line: `Code: <branch> @ <sha>, N ahead of origin/main`, from the checkout the last update ran from (`~/wolfbet-bot/.repo`), using local refs only. Until an update has run it says `unknown`.

**Tests**: `tests/test_phase11o_wolfctl_branch.py` (20): real git against a temp origin and checkout: switch to a branch only on origin (with tracking), back to a local branch, new commits picked up, local or untracked changes refused with nothing moved, invalid and unknown names, a rewritten branch left untouched, plain update unchanged, the status line.

## CLI v2.44.0 - export and backtest results as JSON, safer export paths (2026-11-18)

- `--export PATH --json` prints `{session_id, path, format, rows, bytes, sha256}` for the file it wrote, so whoever picks the file up can check it. PATH may be an existing directory: the file is then `wolfbet-session-<id>.csv` inside it.
- Export now refuses a path that is a symlink, a missing directory, or anything other than a regular file, and opens the file without following a link created in between. Nothing is written in those cases; the message is `Export path must be a regular file in an existing directory (not a symlink)`, exit 1.
- `--backtest ... --json` prints one result document: for one session the figures of the session summary and the stop reason; with `--sessions` / `--runs` the figures of the result table (the table now renders from the same data, unchanged on screen). While it runs, stderr carries `{"progress": 0..1}` lines, at most about 2 a second, ending with 1.0. The console stays silent; errors are JSON documents with the usual exit codes.
- Fix: `--backtest --replay-session ID` on a session with no bets reported "Backtest failed"; the bets are now counted first and the message is `Session #ID has no bets to replay`, exit 1.
- With `--json`, a failed export write shows the operating-system detail only outside production.
- Schemas: `casino-dashboard/contracts/schemas/wolfbet-cli-export-v1.json`, `wolfbet-cli-backtest-v1.json`, `wolfbet-cli-progress-v1.json`.

**Tests**: `tests/test_phase11j_export_backtest_json.py` (25): snapshots of the backtest and export output taken before the change, the hash/bytes/rows against the written file, directory targets, symlink / missing directory / FIFO / directory-symlink refusals that leave every file untouched, the backtest figures against the table, identical output for a fixed seed, progress lines validated and throttled, error documents, the real CLI.

## wolfctl v3.14 - pause, resume and reload, and no command waits for a terminal (2026-11-18)

- New `wolfctl pause [K|all]`, `wolfctl resume [K|all]` and `wolfctl reload [K|all]` (default: every running lane). They send SIGUSR1, SIGUSR2 or SIGHUP to the lane's daemon, which pauses betting, resumes it, or re-reads the saved config. A lane is signalled only when its pid file holds a number and that pid's command line is `wolfbet.py`, so a stale pid file never reaches another program. Exit 1 when no running lane matched, 2 for a bad K.
- `reset`, `run-reset` and `doctor --fix` still ask before acting, but without a terminal they now refuse with exit 3 ("re-run with --yes") instead of hanging on an open pipe or exiting silently. `--yes` / `-y` skips the question as before.
- `lanes N`, `update`, `cleanup` and `stop-all` never ask anything; they now accept `--yes` / `-y` too, so scripts can pass it everywhere. `cleanup --yes 7` keeps the 7.
- `wolfctl status --json` and `wolfctl lanes --json` print only the JSON from `wolfbet.py --status --json` / `--lanes-status --json`.
- `wolfctl lanes --split MODE` without N now prints the usage instead of showing the lanes.
- Usage lists the new verbs, `--yes`, `--json` and the exit codes (0 ok, 1 failed or no lane matched, 2 bad arguments, 3 needs --yes).

**Tests**: `tests/test_phase11i_wolfctl_verbs.py` (25): real child processes stand in for the lane daemons and record each signal; a fake /proc, a stub python3 and stub systemctl/sqlite3 keep the run away from the host.

## CLI v2.43.0 - config schema, config-set dry run, no partial config writes (2026-11-18)

- `--config-schema` prints every key `--config-set` accepts as one JSON document: type, choices or min/max (and whether out-of-range input is clamped), whether `off` is accepted, aliases, default, whether it reloads live, whether a lane may override it, and its help text. It is built from the tables config-set validates with, and the clamp bounds and the `db_write_mode` choices now live in that one table, so the two cannot drift. Schema: `casino-dashboard/contracts/schemas/wolfbet-cli-config-schema-v1.json`.
- `--dry-run --config-set KEY=VALUE ...` validates exactly like the real set and prints the old and new value of each key, base and per lane, with `live` or `restart`, plus clamp warnings. It writes nothing and signals no daemon. Add `--json` for one document (`wolfbet-cli-config-dry-run-v1.json`); proxy and events URLs show no credentials. Put the flags before `--config-set`, since its values run to the end of the line.
- Fix: a `--config-set` call that mixed lane pairs with a bad base pair (for example `lane2.multiplier=3 maxloss=abc`) saved the lane change and reloaded that lane before failing on the base pair. Every pair is now validated before anything is written, so a failing call changes nothing. An invalid lane override no longer prints the "Lane config updated" header first.
- `--dry-run` without `--config-set` exits 2; `--json` also works with `--config-schema` and `--dry-run`.

**Tests**: `tests/test_phase11h_config_schema.py` (44): snapshots of the real config-set output taken before the change, the schema against the accepted keys, aliases, live-reload and lane tables, every listed choice accepted and a bogus one refused, every clamp bound against the validator, dry run leaves files and daemons untouched, invalid input gives the same message and exit 2 as the real set, the partial-write regression, masking, the real CLI.

## CLI v2.42.0 - JSON output for the read commands (2026-11-18)

`--json` prints one JSON document to stdout for `--status`, `--lanes-status`, `--config-show`, `--stats`, `--analytics`, `--last-bets N` and `--session-bets ID`, so the dashboard agent never has to parse the tables.

- Each document carries `"v": 1` and follows its schema in `casino-dashboard/contracts/schemas/wolfbet-cli-*-v1.json`. `--since`, `--until` and `--lane` filter the JSON the same way they filter the tables.
- Numbers come out unformatted and times as stored. The terminal view now reads from the same data, so both show the same figures. Without `--json` the output is unchanged.
- Nothing else reaches stdout: under `--json` every other message goes to stderr. Empty or failed states print `{"v":1,"error":{"code":...,"message":...}}` with the exit code the command already used (`no_database`, `no_session`, `state_unreadable`, `no_config`, `invalid_window`, `session_not_found`).
- `--config-show --json` never prints a secret: the API key appears only as its masked form plus a set flag, and the proxy and events URL appear as scheme, host and port only, in base config and in lane overrides.
- `--json` with any other command exits 2 and prints which commands support it.

**Tests**: `tests/test_phase11g_json_outputs.py` (34): snapshots of the terminal output taken before the change, schema validation of every document, the analytics numbers against the terminal view, filters, masking, error documents and exit codes, the real CLI entry point, checksums of the schema copies. `requirements-dev.txt` adds `jsonschema` for the tests only.

## CLI v2.41.2 - event sender waits up to 1 s for Redis to answer (2026-11-18)

- The event sender now waits up to 1 s for Redis to confirm a batch instead of 100 ms; connecting still gives up after 100 ms. On the live host Redis once answered late after it had already stored the batch, so a delivered event was counted as dropped. The wait runs on the sender thread only and never holds the betting thread.

**Tests**: `tests/test_phase11b_events.py` checks the timeouts handed to the Redis client.

## CLI v2.41.1 - event queue that never slows the betting thread (2026-11-18)

Measured with the bet-path bench at 20,000 fake bets per second, then fixed at the cause.

- Queued events are now flat runs of plain values instead of tuples. Before, every queued bet kept two garbage-collected objects alive, so with Redis down or slow the 50,000-event queue made the collector run 140 to 250 times per bench run, with pauses of 3 to 7 ms on the betting thread. Now 10,000 queued bets leave no collected object behind (guard test).
- When the queue is full, the betting thread only counts the drop and skips building the event (`EventPublisher.reserve`). The newest events are dropped instead of the oldest, and each drop still uses up its `seq`, so the dashboard sees the gap.
- The sender gives up the interpreter lock every 32 events while it encodes, so the betting thread never waits behind a whole 500-event batch.

**Tests**: `tests/test_phase11f_bench_guard.py` (3, always run); `tests/test_phase11b_events.py` updated for drop-newest; `tests/bench_bet_path.py` adds publisher off / fast / unreachable / slow variants.

## CLI v2.41.0 / wolfctl v3.13 - turn the dashboard event stream on from wolfctl (2026-11-18)

Events can now be switched on and off like any other setting, live, and checked from `wolfctl status` and `wolfctl doctor`.

- New config keys `events_enabled` and `events_url`, shared by all lanes and live-reloadable: `wolfctl config-set events_url=redis://user:pass@host:6379/0 events_enabled=true` starts the publisher on the running daemon with no restart; `events_enabled=false` stops it and flushes what is queued. `WOLFBET_EVENTS_URL` still overrides the file.
- `config-set` accepts only `redis://`, `rediss://` or `unix://` URLs with a host or socket path, and `events_enabled` only on/off; anything else exits 2 without echoing the value.
- The URL is shown as scheme, host and port only in `config-show`, in config-set output, in the `reload` event and in every check; the password never prints.
- `--status` / `wolfctl status` show an `Events` row: off, on but not running, or on with sent, dropped, connected and the last error. The state file and the state event carry the same counters under `events`.
- `wolfctl doctor` adds step 6: pings the events Redis once with a 1 s timeout via the new `wolfbet.py --events-check`.
- `wolfctl` output uses plain hyphens instead of long dashes.

**Tests**: `tests/test_phase11e_events_config.py` (30).

## CLI v2.40.0 - status and event log for the live dashboard (2026-11-18)

With events on, the dashboard now gets the bot's status and a log of what it did. With events off nothing changes.

- A `state` event every 2 s carries the same fields as the state file `--status` reads. The dict is now built once by `_state_snapshot()`; the file is written from it unchanged.
- Lifecycle events: `start` (version, lane, config summary) and `session_open` when the publisher starts; `pause` / `resume` within 2 s of any pause (signal, hotkey); `reload` with the fields `config-set` changed, proxy masked; on a recurring or streak-reset restart `session_close` (old totals), `cycle` (reason, delay) and `session_open` (new id); on stop `error` (insufficient balance only), `session_close` and `stop` with the stop reason.
- Signal handlers still only set flags: pause and resume are sent from the main loop.
- On every exit path the publisher now stops after the final session save, so the closing events are flushed.

**Tests**: `tests/test_phase11d_state_events.py` (9). Five older tests now read the state keys from `_state_snapshot`.

## CLI v2.39.0 - bet events for the live dashboard (2026-11-18)

Each saved bet can now also go to a Redis stream for the live dashboard. It is off by default; with it off the bot behaves exactly as before.

- `_db_save_bet_dispatch` hands one tuple per bet to the event publisher (`core/events.py`) after the existing save, in both `db_write_mode` values and in both the serial and pipelined loops. The betting thread only builds the tuple and appends it to a bounded queue; the publisher's own thread does the JSON and the network.
- New state fields `events_enabled` (default off) and `events_url` (from `WOLFBET_EVENTS_URL`). The publisher starts after the bet writer and stops after it on every exit path. The URL is never logged.
- Redis down, slow or missing never stops or slows betting: lost events are counted by the publisher and show as gaps in `seq`.
- The bet event carries session id, amount, multiplier, rule, bet value, roll, state, profit, balance after, session profit, total bets, current streak and the last API latency.

**Tests**: `tests/test_phase11c_bet_events.py` (7).

## CLI v2.38.2 - see the strategy switching live (2026-11-16)

With phases on, a strategy switch was logged at INFO while the live log level is WARNING, so nothing showed; the only sign was `phase recover` beside the strategy in the monitor. The opening strategy also could not be set from `config-set`.

- Every phase switch writes one line to the live log: `PHASE session 152: base (Flat Bet) -> recover (Martingale) at profit -0.00000100, next bet 0.00000025`.
- Every session end writes one line: `SESSION END 152: Profit target reached: +0.00000050 after 5040 bets, 7 phase switches, balance 2.21898968`.
- `grep -E "PHASE|SESSION END" ~/.wolfbet_logs/wolfbet.log` now reads as a timeline of the run. Both lines fire per switch and per session, never per bet.
- `config-set strategy_key=1` (alias `strategy`) sets the opening strategy, keys 1-8; an unknown key is refused with the valid keys named. It resets the D'Alembert and Paroli counters, clears a live auto-bet cycle and needs a restart.
- A backtest no longer writes to the live log, so 200,000 simulated sessions do not bury the live timeline.
- Fix: the switch count shown per session now starts at zero in every recurring session; it used to carry over.

**Tests**: 10 new cases across `tests/test_phase14_session_box.py` and `tests/test_phase5d_config_cli.py`. Version assertions bumped. **1001 total passing.**

`VERSION` `2.38.1` -> `2.38.2`.

## CLI v2.38.1 - config-set can change the game (2026-11-16)

`wolfctl config-set game=dice` answered `Unknown config field: 'game'`. The game was saved, shown by `config-show` and refused per lane, but it was missing from the list of fields `config-set` and the `T` hotkey accept, so the only ways to change it were the wizard or hand-editing the config file.

- `game` joins that list. The value is checked against the games the bot knows, and an unknown one is refused with the valid values named: `game must be one of dice, dice-auto, dice-manual, limbo, limbo-auto, got 'slots'`.
- Changing the game clears a live auto-bet cycle, so no server cycle keeps running under the old game.
- The change needs a full restart; `config-set` already names it in the restart note, and the setting is not live-reloadable.
- Per-lane overrides still refuse it, because every lane must bet the same game.

`rule`, `currency` and `strategy_key` remain wizard-only; adding `rule` also means re-deriving `bet_value`, which is a separate change.

**Tests**: seven cases added to `tests/test_phase5d_config_cli.py` (parse, every known game, unknown game refused with the state unchanged, the stale cycle cleared, no change no clear, restart-only, lane refusal). Version assertions bumped. **991 total passing.**

`VERSION` `2.38.0` -> `2.38.1`.

## CLI v2.38.0 - session box: pre-bet loss cap, phases, loss restart, balance goal (2026-11-16)

A session can now run inside a box: a loss cap checked before every bet, a cash-out, and an optional change of strategy by session profit. The bot can start a new session after a capped loss and stop for good at a balance goal or at the balance floor. A multi-session backtest shows what a setup does before it bets real money.

None of this changes the odds. A dice bet costs about 1% of its amount on average at any payout, so every setup loses about 1 per 100 wagered over time. The settings only choose how that loss arrives: many small wins and a few capped losses.

### New settings
All off by default. Set them with `config-set`; they apply on the next start.

| Setting | What it does |
|---|---|
| `hard_cap` | Checks the next bet before it is placed. A bet that would take the session loss past `max_loss`, or the balance below `stop_on_balance`, is not placed and the session ends. |
| `restart_after_loss` | Starts a new session after a session ends at its loss cap. Needs `recurring`, `hard_cap`, `max_loss` and `stop_on_balance`. |
| `balance_goal` | Stops the bot when the balance reaches this amount. Never restarts. |
| `hybrid_recover_at`, `hybrid_recover_strategy`, `hybrid_recover_bet` | At this session loss, bet with another strategy (key 1-7) from its own first bet (a multiple of `base_bet`) until the session is back to break-even. |
| `hybrid_press_at`, `hybrid_press_floor`, `hybrid_press_strategy`, `hybrid_press_bet` | At this session profit, bet with another strategy until profit falls to the floor or the session cashes out at `max_profit`. |

Setting a phase threshold switches the pre-bet cap on as well.

### Stop and restart behaviour
- Without `hard_cap` the cap works as before: `max_loss` is checked after a bet, so a doubled bet can pass it.
- A loss stop restarts only when every guard is set and the session ended on `Max loss hit` or `Loss cap`. Insufficient balance, the balance floor, a rule stop, the lane budget and a lost auto-bet cycle still stop the bot.
- With phases on, a server auto-bet cycle may restart while the session is in the red, because the phase owns the recovery and the loss cap bounds it. Without phases that stop is unchanged.
- A phase switch on `dice-auto` / `limbo-auto` ends the server cycle and starts one with the phase's strategy and first bet. Its stop rules carry the profit and loss still left in the session.
- With the cap on, pipelined betting runs one bet at a time.

### Start-up check
`config-set` prints a warning that names the setting when the rules cannot work: a cap without `max_loss`, a restart without a floor, a first bet larger than the cap, thresholds out of order, a win progression with `win_mult` 1. The bot then refuses to start with the same message. The service exits without an error code in that case, so its unit does not retry every 10 seconds and then start betting by itself once the config is fixed.

### Backtest
- `--sessions N` plays N sessions back to back on one roll stream and prints the share of sessions in profit, average win and loss, worst session, bets and phase switches per session, the amount wagered and the net result per 100 wagered.
- `--runs R` repeats from a fresh `--balance X` and reports how the runs ended: balance goal, balance floor, next bet unaffordable, or played out.
- `--replay-session ID --sessions N` prints the same table for the rolls of a recorded session.
- The backtest starts a new session after a loss whatever the live restart settings are, and says below the table what the live bot would do.
- Without `--simulate N` these modes draw up to 5,000,000 rolls.
- `backtest_loop` checks the stop conditions before it draws a roll, so no roll is dropped between sessions. A single `--simulate` run prints what it printed before.

### Fixes that apply with every new setting off
- **Profit target and max loss fire to the nearest satoshi.** Session profit is a sum of bet results and drifts off the exact value by float rounding. With flat 1 satoshi bets and a 1 satoshi target, 43% of the sessions that needed more than one bet missed the target and played on (4,142 of 9,625 simulated). A Martingale that doubles from 0.00000100 missed its target after 2, 3, 7, 8, 10 or 11 losses. A value less than half a satoshi short now counts as reached.
- **A new recurring session starts D'Alembert and Paroli from their first step.** The unit count and the win count used to carry over from the session before.

### Screens and records
- `monitor`, `status` and the dashboard show `phase base`, `phase recover` or `phase press` beside the strategy while phases are on. The live state file carries `hybrid_phase`.
- Sessions that ran with phases are stored as `Hybrid: <strategy>`, so `stats` and `analytics` list them apart.
- `config-show` lists the ten settings.

### Known limits
- The cap is exact on `dice`, `dice-manual` and `limbo`, where the bot sizes each bet. On `dice-auto` / `limbo-auto` the server sizes the bet and the bot mirrors it; a mismatch can overshoot by one bet.
- Every new session and every phase switch on an auto game costs one `/auto/start`.
- With `loss_mult` 2.01 a cap of exactly 15 first bets does not cover the fourth bet (87.2% of sessions in profit instead of 93.5%). Use `loss_mult=2` or size the cap from the real chain.
- `hybrid_press_floor` is where the press phase ends, not a guaranteed minimum.
- The new settings are not in the wizard, are not reloaded live, and the Telegram bot does not have them.

**Tests**: `tests/test_phase14_session_box.py` (145): phase choice, room for the next bet, value parsing, every config error, the cap in the stop check and in the bet loop, each guard of the loss restart, the balance goal, auto-bet config per phase, labels, config round trip, the start refusal and the multi-session backtest. Version assertions bumped. **984 total passing.**

`VERSION` `2.37.2` -> `2.38.0`.

## CLI v2.37.3 / TG v1.21.4 - limits reject nan and inf (2026-11-15)

A `nan` limit passes every `< 0` guard and every stop comparison, so `/set maxloss nan` silently disabled the stop-loss and `--config-set` accepted the same value.

- `_apply_mutation` parses every limit through `core.validation.finite_float`: rejects `nan`, `inf`, non-numeric and fractional integers, and names the field.
- `/set` and `/tweak` route their numeric values through the same check, so the command answers "Invalid value" and keeps the stored limit.
- `core/validation.py` re-exports the shared helper, so the CLI keeps its existing `core.*` imports.

**Tests**: `tests/test_phase11o_limit_validation.py` (10). **887 total passing** before this file.

`wolfbet.py` `2.37.2` -> `2.37.3`; `tg.VERSION` `1.21.3` -> `1.21.4`.

## TG v1.21.3 - `/update` owner gate and handlers off the event loop (2026-11-15)

### `/update`
- The owner is `TG_OWNER_ID` and nothing else. The built-in fallback id is gone: with the variable unset or non-numeric, `/update` answers "Owner only." and startup logs one warning.
- The command is an argument list for `systemd-run --user` with `--on-active=2s`, `--working-directory`, and `StandardOutput`/`StandardError` appended to the log. No `bash -c`, so a path from `BOT_REPO_DIR` is inert.
- The ctl path must be absolute, existing and executable; the repo directory must be absolute and exist.
- The log is `<data dir>/update.log`, created 0600, instead of a predictable file under `/tmp`.
- `systemd-run` itself runs in a worker thread so a slow start cannot hold the loop.

### Event loop
- `/benchmark` posts through `httpx.AsyncClient` with the engine's request timeout instead of up to 50 blocking `requests` calls, so other users' commands and engines keep running during a benchmark.
- `/stats`, the session history list, `/lastbets`, session detail, stop-zombie cleanup and session delete read and write through the new async helpers in `tg/database.py`; `migrate_db` runs in a worker thread too.

**Tests**: `tests/test_phase11n_tg_hardening.py` (16): owner gate with unset, non-numeric and configured ids, refusals for a non-owner and for the old fallback id, argv shape with a hostile path, path validation, a loop that keeps ticking during a benchmark, and the async DB helpers. **887 total passing.**

`tg.VERSION` `1.21.2` -> `1.21.3`.
## CLI v2.37.2 - the base bet you set is the base bet it bets (2026-11-15)

The operator saved `base_bet 0.00005805`, started the bot, and it kept betting `0.00006534`. Three separate defects produced that, and each one hid the others.

### A saved base reaches the running daemon
- `base_bet` joins `_LIVE_RELOAD_KEYS`, so `config-set` and the wizard's save take effect over SIGHUP instead of waiting for a restart that may never come.
- The reload re-baselines the run: bumps earned on the old base are dropped, the milestone re-anchors above the run's current profit, and the auto cycle restarts at a safe tick with the new base through `_auto_restart_pending`.

### Bumps belong to the base they were earned on
- The run file now stores `base`, the configured base the bumps were earned on.
- `_init_run` drops the bumps when that base no longer matches the configured one, so a newly set base is never re-inflated on start. Run files written before this version keep their bumps once and adopt the current base.

### The screen shows the live value
- `config-show` reads the daemon's live snapshot and marks the row `0.00005805  (live 0.00006534, +729 bumps)` when the running base differs from the file.
- `wolfctl start` on an already-active unit reports `already running - nothing restarted` with the `wolfctl restart` hint instead of claiming a start that did not happen.
- The monitor prints `Daemon stopped.` only when the stop action actually ran; every other exit reports a detach.

Known interaction, unchanged: with `basebet_pct > 0` the base is recomputed from balance every 10 bets, so a hand-set base is transient there. The live value is now visible either way.

**Tests**: `tests/test_phase13_base_bet_truth.py` (11) plus updated expectations in `test_phase5p_live_reload.py`. **839 total passing** on `main`; the dashboard branch adds the web suite on top.

`VERSION` `2.37.1` -> `2.37.2`.
## TG v1.21.2 - old web authorization and token hardening (2026-11-15)

The old web stops serving one Telegram user's data to another, and the shared auth layer behind all six bots loses its weakest assumptions. Most of the change is in `casino-shared` 0.3.2; this release wires it into WolfBet.

### Per-user access control
- `require_user_access(user_id)` replaces `get_current_user` on all six `/api/users/{user_id}/...` routes (sessions, CSVs, session detail, bets, bets CSV, chart). An admin reads any user; a Telegram user reads only their own id and gets 403 otherwise.

### Tokens
- A token with no `jti` is rejected when the session check runs; the one-time link exchange still accepts it because it carries its own `jti` and is now consumed on first use.
- `?token=` is no longer accepted on HTTP requests or on a WebSocket handshake; both read the httpOnly cookie. The browser sends it on the upgrade, so the live chart and the log stream keep working. `web/static/chart.js` and `admin/logs.html` drop the query string.
- A WebSocket handshake whose `Origin` host differs from the `Host` header is refused.

### Sessions and cookies
- `POST /logout` with a double-submit `csrf_token`; `GET /logout` now answers 405. Both nav bars post the form.
- Login sets a `csrf_token` cookie and both cookies are `Secure` whenever `APP_ENV=production`.
- A Telegram deep link opens one session: the link's `jti` is recorded on the exchange and a second use shows the invalid-link page.

### Login
- Five failed attempts for the same address and username lock the pair for 15 minutes; a successful login clears the counter. The state lives in the web database, so it holds across both uvicorn workers and restarts.

### Passwords
- PBKDF2 raised from 260k to 600k iterations with the count stored in the hash (`pbkdf2:sha256:600000:salt:hash`). Existing hashes still verify and are rehashed on the next successful login.

### Admin page
- The Tier and Block controls on a Telegram user's page are gone, replaced by a note: the bot never reads those columns.
- Every admin and dashboard page passed the template name and context to `TemplateResponse` in the pre-0.29 order. Starlette 1.x reads the first argument as the request, so each render raised `TypeError: unhashable type: 'dict'` on a fresh install (`install.sh` only pins `fastapi>=0.115`). All 16 call sites in `admin_routes.py` and `user_routes.py` now pass `request` first.

**Tests**: `tests/test_phase11m_web_authz.py` (20): every user route table-driven for owner 200, other user 403, admin 200; jti-less token 401; `?token=` 401. `tests/test_phase11m_web_pages.py` (4): the admin pages render and the user page carries the note instead of the controls. Shared suite adds the token, WebSocket-origin, lockout, single-use-link, logout and hash-upgrade cases. **860 total passing** in this repo.

`tg.VERSION` `1.21.1` -> `1.21.2`.

## CLI v2.37.1 - a profit increment no longer stops the bot (2026-11-15)

A run-scope increment that landed on a losing bet cleared the auto-bet UUID. The next loop tick found no UUID, the mid-chain guard saw a negative streak, and the daemon exited cleanly with the loss still open. On the live server this ended an overnight run at 05:00:37 after one bet. `Restart=on-failure` does not respawn a clean exit, and the unit was never enabled, so nothing brought it back.

### Increment defers its auto-cycle restart
- `_check_profit_increment` still bumps `base_bet` and saves the run file, but sets `_auto_restart_pending` instead of clearing `_auto_uuid`.
- `_maybe_apply_pending_auto_restart` performs the swap at the top of the betting loop, and only when profit and streak are both non-negative. A new base bet waits for the loss chain to close instead of abandoning it.
- The mid-chain guard is unchanged, so a cycle the server ended still stops the bot as designed.

### The daemon records why it exited
- Session summary carries a `Stop reason` row, and the daemon logs `DAEMON EXIT session=... reason=...` at WARNING on its way out. Previously the reason existed only in memory, and the state file was deleted before anything could read it.
- The reason string is escaped before rendering, so a value carrying brackets cannot break the console output.

### Installer
- `install.sh` enables `${SERVICE_NAME}`, so the CLI bot comes back after a reboot. `enable` without `--now`: a manual `wolfctl stop` stays stopped.

**Tests**: `tests/test_phase12_increment_restart.py` (9) plus updated expectations in `test_phase5s_cli_profit_increment_auto.py` and `test_phase10b_run_increment.py`. **836 total passing.**

`VERSION` `2.37.0` -> `2.37.1`.

## TG v1.21.1 - old web hardening (2026-11-15)

The admin web that ships with the TG bot no longer starts with the shipped credentials and no longer listens on a public interface.

### Startup refuses weak credentials
- `WOLFBET_WEB_SECRET_KEY` must be at least 32 bytes and must not be the shipped default.
- `WOLFBET_WEB_ADMIN_PASS` must be at least 12 characters and must not be `changeme123`.
- The check runs when `web/config.py` is imported, so `wolfbet-web` exits before binding. The message names the setting and never prints the value.

### Installer
- Generates the secret and prompts for the admin password (12+ characters); with no TTY it generates a password and prints it once so it can be stored.
- Writes `.env` with `printf`, so a password containing `$`, a backtick or `#` is stored verbatim. The file stays mode 0600.
- Refuses to continue when an existing `.env` still holds the shipped secret or password.
- Systemd `ExecStart` always binds `127.0.0.1`. Without `--domain` the installer prints the SSH tunnel command instead of exposing the app.
- The closing banner no longer prints the default password.

### Live session WebSocket
- `web/bot_db.py` exports the bound `_connect_ro(user_id)` wrapper, so `/ws/session/...` no longer fails on its first poll.
- One poller per session channel instead of one per connection: it starts with the first subscriber and stops with the last. New bets and the session row are broadcast to every subscriber from that single task, and the SQLite read runs in a worker thread.
- History replay on first subscribe is unchanged; the chart still drops points it already holds.

**Tests**: `tests/test_phase11a_web_hardening.py` (8): shipped and short secret refused, shipped and short password refused, a strong pair accepted, bets stream to a temp bot DB over the live socket, two subscribers share one fetch per tick, the poller stops with the last subscriber, and the installer binds loopback only. **827 total passing.**

`tg.VERSION` `1.21.0` -> `1.21.1`. The web ships with the TG bot.

## TG v1.21.0 — run-wide profit increment (parity with CLI v2.37.0) (2026-11-14)

The Telegram bot gains `profit_increment_scope`. The default, `session`, is unchanged.

### `/set piscope run|session`
- `run`: one total per user across **all slots and sessions** since the run started (DB sum of the run's other sessions, refreshed after each periodic save, plus the engine's live profit). The milestone is `(bumps + 1) × threshold`.
- Bumps are shared through `<user dir>/run.json` `{first_session, started_at, bumps}`. A bump in one slot raises every slot's base bet: running slots apply it on their next refresh and restart their auto-bet. Single event loop, so there is no race.
- `_run_base_origin` (the base before run bumps) is carried through recurring restarts and snapshots, so bumps never stack twice. Old snapshots without it subtract the bumps back out.
- The run survives recurring cycles, `/stop` + `/bet` and bot restarts until `/runreset`. A run file that points past the DB's last session starts a new run.
- **none / off / 0 = off** for `pi` / `pt` (`/set` and `/tweak`): no bumps and no background run. The run file is removed at the next start, or right away through `/tweak`. Re-enabling through `/tweak` starts a run at the current session. **Behaviour change:** `/set pi 0` used to mean "restart auto-bet only at each milestone" and now turns the increment off.
- `/runreset` deletes the run total. It refuses while sessions or recurring restarts are active (stop with `/stop recurring` first).
- The duplicated increment code in the pipelined and serial loops is merged into `_maybe_profit_increment()`.
- `/config` shows the scope. Status / full config show `run profit` plus `Run profit +x (since #N, k bump(s))`.

**Tests**: `tests/test_tg_run_increment.py` (20): session scope unchanged, run create / bump / save, other-session sum, shared bump picked up by a second slot, recurring and fresh-start origin, resume without origin, snapshot fields, stale file, off at start (4 cases), zero increment, `/tweak` off / on, run reset seen by a running slot, formatter, `/set piscope` + `pi 0`, `/runreset` refuse / delete, wiring. TG version pins bumped. **819 total passing.**

`tg.VERSION` `1.20.0` → `1.21.0`.

## CLI v2.37.0 / wolfctl v3.12 — run-wide profit increment (2026-11-14)

`profit_threshold` / `profit_increment` only measured the current session's profit, and the milestone reset every recurring / streak-reset cycle. With many short sessions, a large threshold was never reached. The increment can now measure the total across the whole run.

### `profit_increment_scope` (alias `piscope`)
- `session` (**default**): unchanged.
- `run`: profit = the sum of this lane's sessions since the run started + the current session. The milestone is kept across sessions, and each bump is persisted.
- Run file per lane: `~/.wolfbet_run.json` / `.laneK.json` (env `WOLFBET_RUN_PATH`), holding `{first_session, started_at, bumps}`. The run total is read from the stats DB, so a restart, update or crash keeps it.
- On start, `base_bet = config base_bet + bumps × profit_increment`, and the next milestone is `(bumps + 1) × threshold`. A run file that points past the DB's last session (stats were reset) starts a new run.
- The two duplicated increment blocks are merged into `_check_profit_increment()` (auto-bet UUID restart kept).
- `pi` / `pt` set to `none` / `off` / `0` / `0.0` (or ≤ 0) means off: no bumps and no background run. The run file is removed at the next start, or right away through live reload. Re-enabling starts a fresh run (on the current session when done live), so old bumps are never re-applied.
- Wizard asks for the scope after the increment prompts. `config-set` validates `session|run` (exit 2 otherwise). It needs a restart to apply.
- `wolfctl status`: `Run Profit` row (since #N, bumps, next milestone). Monitor: `RunMs` progress.

### wolfctl v3.12
- `wolfctl run-reset [--yes]` confirms, stops the running lanes, removes the run files and starts the lanes again. `--run-reset [--lane K]` on the CLI.
- `wolfctl reset` also removes the run files, because session ids restart at 1.

**Tests**: `tests/test_phase10b_run_increment.py` (23): session scope unchanged, run creation, DB sum with lane filter, bump persisted, resume restores bumps, stale file → new run, recurring keeps milestone, auto-UUID restart, config-set, run-reset, `RunMs` row, wolfctl wiring, none / 0 means off (mutation, start, live reload off / on). Source guards updated for the merged helper. Version pins bumped. **799 total passing.**

`VERSION` `2.36.0` → `2.37.0`. `WOLFCTL_VERSION` `3.11` → `3.12`.

## CLI v2.36.0 / wolfctl v3.11 — configurable lane money split, default full amounts (2026-11-13)

In v2.35.0 the lane budget guard (`balance ÷ lanes`) was always on, and the base bet was never divided. How lanes share money is now a setting.

### `lane_split` (stored as `lanes.split`, shared by all lanes)
- `none` (**default**): every lane uses the full `base_bet` and there is no lane budget guard. The user sizes the bets.
- `budget`: each lane may risk `balance ÷ lanes`. This is the v2.35.0 behaviour.
- `bet`: `base_bet ÷ lanes`, and `basebet_pct` is divided the same way.
- `both`: the `bet` and `budget` modes together.
- A lane with its own `laneK.base_bet` override keeps that exact amount. With one lane, every mode does nothing. A divided bet that would round to 0 stays full (logged).
- An untouched divided bet is saved as the full one, so the base bet never halves twice across restarts. Live reload applies the same split, so it gives no false "base_bet changed" warning.
- `config-set lane_split=MODE` validates the value (exit 2 on an unknown mode) and refuses `laneK.lane_split`. `config-show` prints `split: MODE` with its meaning.
- The chain-depth warning uses the lane share only in `budget` / `both` modes.

**Behaviour change:** existing lane setups no longer get the budget guard unless `lane_split=budget` or `both` is set.

### wolfctl v3.11
- `wolfctl lanes N --split none|budget|bet|both`. `wolfctl lanes` with no N shows the split mode.

**Tests**: 16 new or updated cases in `tests/test_phase10_lanes.py` (modes, override kept, rounding, save keeps the full bet, basebet_pct, live reload, config-set / show, wolfctl). Version pins bumped. **776 total passing.**

`VERSION` `2.35.0` → `2.36.0`. `WOLFCTL_VERSION` `3.10` → `3.11`.

## CLI v2.35.0 / wolfctl v3.10 — parallel lanes: N independent sessions side by side (2026-11-13)

Pipeline is one session with several bets in flight, and it is forced off for auto-bet games and non-Flat strategies. Lanes run several **independent sessions** at once. Each has its own Martingale chain, counters, stops, pauses and recurring / streak-reset cycle. Works with every strategy and with `dice-auto` / `limbo-auto`.

### Architecture
- One process per lane: `wolfbet.py --daemon --resume --lane K`. Lane 1 is unchanged (`wolfbet.service`, classic paths). Lanes 2..N run as `wolfbet-lane@K.service` with `~/.wolfbet_autobot_live.laneK.json` / `.laneK.pid`, and log lines are prefixed `[LK]`.
- Lanes share the config file and the stats DB (WAL). Sessions get a `lane` column (migrated; older rows count as lane 1).
- `_apply_lane` runs before any command, so `--status`, `--monitor` and `--stop` with `--lane K` act on that lane.

### Split budget
- Each lane's share = balance ÷ lanes at start. Equity = share + the lane's result across its sessions.
- If the next bet would exceed the equity, the lane stops with `Lane budget exceeded (…)`. This is not on the restart whitelist, so it is a loss stop; the other lanes keep going.
- The chain-depth warning uses the lane share.

### Per-lane overrides
- Config block `lanes: {count, overrides: {"K": {…}}}`. Set it with `config-set lanes=N`, `config-set laneK.key=value`, and `laneK.key=default` to remove an override. Values pass through the normal mutation validation.
- Shared-only keys are refused per lane: api_key, currency, game, proxy, purge_days, db_write_mode, pipeline_depth.
- `save_config` keeps the lanes block, and a lane ≥ 2 never writes the base config. Live reload (SIGHUP) merges the lane's overrides. A base `config-set` reloads all running lanes.
- `config-show` lists the lane count and overrides.

### Speed / throttling
- **Soft pacing**: the rate-limit headers are account-wide. Below 10% of the quota left, each bet waits up to 250 ms, scaled by how close the quota is to empty. Lanes ease off together instead of all hitting the `remaining ≤ 2` pause. It is 0 above the mark, so a single lane is unaffected.
- `/auto/start` is spaced across lanes by a shared `fcntl` lock file (`~/.wolfbet_autostart.lock`, `WOLFBET_AUTOSTART_LOCK`). Lane K's first start is staggered by `(K−1)·2s`. No-op with one lane.
- `wolfctl benchmark [N] --lanes M`: read-only probe at 1..M concurrent clients (req/s, scaling, p50, 429s, quota headers) plus a recommended lane count.

### wolfctl v3.10
- `wolfctl lanes [N]` sets the count, restarts the running lanes so shares recompute, and enables + starts lanes 2..N (so they return after a reboot) / disables + stops the extra ones. With no N it shows the current lanes.
- `start` / `stop` / `restart` / `update` / `reset` act on every lane. `status` adds a per-lane table (session, uptime, run time, profit, equity, bps, pace) with total bps. `monitor [K]` attaches to lane K.
- `stats` / `analytics` accept `--lane K`. Session headers show `[LK]`, and analytics has a "By lane" table.
- The lane systemd template is written by install.sh, and on demand by wolfctl.

**Tests**: `tests/test_phase10_lanes.py` (33): paths and identity, override load / save / live-reload, config-set parsing and errors, budget stop and no restart, share init, chain warning, soft-pace curve, header → pace, cross-lane auto-start spacing (threads), single-lane no-op, lane recommendation, DB tag, stats / analytics lane filter, lanes status table, wolfctl / install wiring. Version pins bumped (CLI + wolfctl). **760 total passing.**

`VERSION` `2.34.0` → `2.35.0`. `WOLFCTL_VERSION` `3.9` → `3.10`.

## CLI v2.34.0 — durations: range time span in stats/analytics + total run time in monitor (2026-11-13)

Each session already showed its own uptime, but nothing showed how long a set of sessions covered. With recurring / streak-reset cycles, the monitor clock showed only the current short session and restarted every cycle.

- New `_fmt_duration`: `1mo 20d 2h 3m 2s` (30-day months, leading zero units dropped, seconds always shown).
- New `_sessions_time_span(conn, where, params)`: first start, last end (a running session counts up to now), **span** (first start → last end) and **bet time** (Σ per-session durations, pauses between sessions excluded). It honours the `--since` / `--until` window, and tz-aware timestamps are converted to local time.
- `wolfctl stats` totals: `Sessions:  N  (first start → last end)` and a new `Time Span: …   Bet Time: …` line.
- `wolfctl analytics`: `Time range:`, `Time span:` and `Bet time:` rows after `Total sessions:`.
- Monitor and in-process dashboard header: `#N  0:00:01  Run 2h 3m 5s`. Run is the wall-clock time since the bot started (`state.run_started_at`, never reset by restart cycles, pauses included). It is exported in the state file as `run_uptime_sec`. `wolfctl status` gets a `Running for` row. Older state files without the key show no Run segment.

**Tests**: `tests/test_phase9d_durations.py` (17): formatter cases, span helper (plain, windowed, running, empty), stats and analytics output, monitor header with and without run time, run start not reset by recurring. Version pins bumped. **727 total passing.**

`VERSION` `2.33.1` → `2.34.0`.

## CLI v2.33.1 — stats: average number of wins / losses per session (2026-11-13)

v2.33.0 added Avg Win / Avg Loss as amounts per bet. The intended metric is a count: how many wins (and losses) a session has on average.

- `wolfctl stats` Range / All-Time Totals: new `Wins/Session:  Max N  Avg N.N` and `Losses/Session: Max N  Avg N.N` lines next to `Bets/Session`. They respect `--since` / `--until`.
- The Avg Win / Avg Loss amount lines are removed from the totals, the per-session Streaks line (back to `W+x  L-y`) and `wolfctl session ID`. Best Win / Worst Loss stay removed.
- The amount tracking added in v2.33.0 is removed: `win_total` / `loss_total` state, the outcome-site accumulation, the session-save columns, the helpers, and the migrate entries. Databases that already got those columns keep them unused; this is harmless.
- The v2.33.0 time-of-day `--since` / `--until` fix is unchanged.

**Tests**: `tests/test_phase9c_stats_window_avgs.py`: the date-window tests are kept; the amount tests are replaced by count tests (1/1/1 → Avg 1.0; mixed sessions in a time window; no amount lines in the session view). Version pins bumped. **709 total passing.**

`VERSION` `2.33.0` → `2.33.1`.

## CLI v2.33.0 — stats: time-of-day windows work + avg win / avg loss per bet (2026-11-13)

### Fix: `--since` / `--until` with a time
`started_at` is stored as ISO with a `T` (`2026-11-13T11:35:20.75`). A bound typed with a space (`--since "2026-11-13 14:00"`) was compared as a plain string. A space sorts before `T`, so every session of that day matched and the time part was ignored.
- `_stats_date_window` now normalises each bound with `_normalize_window_bound`: it swaps the first space for `T` and validates with `datetime.fromisoformat`. Accepted forms: `YYYY-MM-DD`, `YYYY-MM-DD HH:MM`, `YYYY-MM-DD HH:MM:SS` (fractions OK), and the same with `T`.
- A bare `--until` date extends to `T23:59:59.999999`.
- Invalid input prints `Invalid --since/--until "…": use YYYY-MM-DD[ HH:MM[:SS]]` and shows no rows, instead of showing the wrong rows. This applies to `--stats` and `--analytics`.
- Times are server-local (`--help` says so).

### New: average win / average loss per bet
- New session columns `win_total` (Σ profit of winning bets) and `loss_total` (Σ amount of losing bets), added by `migrate_db`. They are accumulated at every outcome site (serial, pipelined, backtest) and reset with the other per-session counters.
- `wolfctl stats`: each session's Streaks line shows `Avg Win: +x/bet  Avg Loss: -y/bet` instead of Best Win / Worst Loss. The Range / All-Time totals show `Avg Win` / `Avg Loss` per bet (Σ totals ÷ Σ wins / losses) instead of Best Win / Worst Loss.
- `wolfctl session ID`: same replacement in the Full Stats block.
- Sessions saved before this version fall back to their bet rows. If those rows were already purged (`purge_days`), the value shows `—`.
- `--stats` and `--session-bets` run `migrate_db` first, so they work on a DB the upgraded bot hasn't opened yet.
- The live monitor, `status` and the end-of-session summary are unchanged.

**Tests**: `tests/test_phase9c_stats_window_avgs.py` (17): bound normalisation and rejection, a same-day time window with seconds, bare until, error output for stats and analytics, per-session and total averages, bet-row fallback and `—`, session view, loop accumulation. The bare-until param assertion was updated in phase6d. Version pins bumped. **710 total passing.**

`VERSION` `2.32.1` → `2.33.0`.

## CLI v2.32.1 — hotfix: insufficient balance on auto-bet no longer "recovers" (2026-11-13)

**Incident.** A Martingale auto-bet session with streak reset on and no `max_loss`. The first 3+-loss run kept going until the balance could not cover the next doubled bet. WolfBet rejected that bet with HTTP 400 `You have insufficient balance…`. The CLI's 400 branch fell through to `classify_auto_stop_reason(body) or "expired"`, so the loop treated it as a dead session. It rotated the seed and started a fresh auto-cycle at base bet in the same session, still in the red. Streak reset only fires at profit ≥ 0, and no other stop was set, so nothing could end the session. Each time a later chain hit the balance limit, the same "recovery" ran again, until the balance was gone.

### Fixes
- `api_place_bet` 400 branch: an `insufficient` body sets `_insufficient_balance`. The loop hard-stops with `STOPPED: Insufficient balance`. It is never labelled `expired`.
- `api_auto_start`: an `insufficient` body sets the same flag. The loop stops on the first failure instead of retrying 3× through recovery.
- New guard before any auto-cycle (re)start: if the session is mid loss streak (`current_streak < 0`) or in the red (`profit < 0`), stop with `Auto-cycle lost mid-chain (streak …, profit …)`. A new server cycle resets the progression to base bet and would abandon the loss. This covers genuine `expired` / `max_nonce` mid-chain. Fresh starts, profit-increment restarts and safe recoveries are unchanged.
- `_check_risky_config`: when streak reset is on and `max_loss` is off (Martingale / Delay-M), the warning shows the longest loss chain the balance can pay for (`martingale_chain_depth`).
- TG engine already treated 400 insufficient as terminal. No change there.

**Tests**: `tests/test_phase9b_insufficient_400.py` (11): 400 insufficient vs real expiry, `/auto/start` single attempt, a loop replay of the incident, mid-chain and in-red stops, safe recovery and fresh start still work, chain depth, warning. Version assertions bumped. **693 total passing.**

`VERSION` `2.32.0` → `2.32.1`.

## CLI v2.32.0 / TG v1.20.0 — streak reset: restart after a long loss streak breaks (2026-10-29)

New recurring trigger. When a run of **N or more consecutive losses** is broken by a win, the session ends, pauses for its own delay, then a fresh session starts — same reset machinery as recurring restarts.

- New settings: `streak_reset_losses` (int, off by default; alias `srloss`) and `streak_reset_delay` (seconds, default 600, 0–86400 on CLI, 5–86400 on TG; alias `srdelay`).
- Fires only on the breaking win (`current_streak == 1` and the broken loss run ≥ N) and only while session profit ≥ 0. In the red, betting continues in the same session and the trigger disarms until the next N-loss run.
- Independent of `recurring`: the reason `Loss streak reset: K losses then win` restarts even when `recurring` is off. Other recurring reasons keep `recurring_delay`.

### CLI
- `should_stop()` clause, `_should_recurring_restart()` accepts the new reason, `_reset_session_for_recurring(delay)` takes the pause per reason (`_restart_delay_for`).
- Loss run captured at every outcome site (sequential, pipelined, backtest) via `last_broken_loss_streak`.
- Persisted in config + presets, live-reloadable (SIGHUP), `wolfctl config set/show`, tweak grammar, wizard (Step 5b), session summary, monitor CFG row `SR 20L→600s`.
- State file gains `pause_total_sec` so monitor/status countdown shows the active pause length (streak reset vs recurring).

### TG
- Engine stop clause + `/tweak srloss`; `/set srloss|srdelay`; `/config` line; `/help` entries; `CONFIG_KEYS` (presets keep both keys).
- `_notify_stop` restarts on the new reason using `streak_reset_delay`, independent of the recurring flag; live `/set` values carry into the next cycle.

### Web
- Settings page lists `streak_reset_losses` / `streak_reset_delay` with the stop conditions (read-only, as before).

**Tests**: `tests/test_phase9_streak_reset.py` — CLI + TG trigger, red/threshold/off cases, delay selection, mutation validation, persistence lists. Version assertions bumped. **679 total passing.**

`VERSION` `2.31.0` → `2.32.0`. `tg/__init__.py` `1.19.0` → `1.20.0`.

## CLI v2.31.0 / TG v1.19.0 — date-range analytics (CLI + TG) (2026-08-15)

v2.30.9 shipped date-range `--stats --since/--until`. This wave extends the same window to analytics everywhere: CLI `--analytics`, TG `/analytics`, and (in their own repos) the 5 sibling bots.

### CLI — `--analytics --since DATE [--until DATE]`

- All four analytics helpers (`_analytics_lifetime` / `_by_game` / `_by_strategy` / `_top_sessions`) gain optional `where`/`params` kwargs (backwards-compatible defaults).
- `cmd_analytics(since, until)` builds the window via `_stats_date_window` and threads it into all helpers. Header reads `Range analytics — N sessions … (since → until)` when a window is active; unchanged `Lifetime analytics` otherwise.
- main() dispatch threads `args.since/args.until`.
- `wolfctl analytics` now passes args through (`wolfctl analytics --since 2026-08-14` works).

### TG — `/analytics [SINCE] [UNTIL]`

- `/analytics 2026-08-14` → windowed lifetime + by-game + by-strategy + top-5 blocks; header shows the window.
- `/analytics 2026-08-01 2026-08-15` → explicit window; bare UNTIL date = inclusive end of day (`T23:59:59`).
- `>2 args` → friendly usage hint (never a traceback).
- `/help` documents the date form.
- Same window semantics as the CLI (`started_at >= ? AND started_at <= ?`, parameterized).

**Tests**: `tests/test_phase6e_analytics_dates.py` — 14 tests: helper-level filtered aggregates against a seeded temp DB (lifetime/by-game/by-strategy/top-5 honor the window), `cmd_analytics(since=…)` functional, no-filter Lifetime regression, dispatch threading, `--analytics --since` parses (exclusive-group regression), TG source-shape checks (args parsed, `{where}` threaded into all 5 queries, bare-until end-of-day, Range header, usage hint), `/help` mention, wolfctl passthrough. Version assertions bumped across phase5r + 8 prior files. **659 total passing.**

`VERSION` `2.30.9` → `2.31.0`. `tg/__init__.py` `1.18.0` → `1.19.0`. `wolfctl` unchanged (v3.9; only the analytics function gained `"$@"`).

## CLI v2.30.9 + wolfctl v3.9 — `wolfctl reset` (archive + fresh statistics) + date-range `stats` (2026-08-15)

You asked for a hard statistics reset: clear all sessions/bets and start a fresh stats flow — config untouched, old data archived — plus date-range evaluation of existing history.

### `wolfctl reset [--yes]`

Archive-first hard reset. **Nothing is ever deleted** — the DB is moved (not removed) to a timestamped archive:

- Confirmation prompt showing session count, total bets, and DB size (unless `--yes`/`-y`).
- Stops the daemon if running (does not auto-restart — you decide when the new flow starts).
- `mv ~/.wolfbet_autobot.db → ~/.wolfbet_autobot-YYYYMMDD-HHMMSS.db`.
- Clears the stale live-state file (`~/.wolfbet_autobot_live.json`) so `--resume` doesn't resurrect the old session number into the fresh DB.
- Config (`~/.wolfbet_autobot.json`) completely untouched.
- Next `wolfctl start` begins fresh statistics (`init_db` auto-recreates an empty DB).

### `wolfctl archives`

Lists every `~/.wolfbet_autobot-*.db` archive with session count, date span (`MIN(started_at) → MAX(started_at)`), and size — plus the total. Any archive remains a plain SQLite file queryable via `sqlite3 <archive> 'SELECT …'`.

### `--stats --since DATE [--until DATE]`

Date-window evaluation without resetting:

- `wolfctl stats --since 2026-08-14` → only sessions started Aug 14 onward; both the listing and the totals block honor the window (totals header reads "Range Totals [window]").
- Bare `--until` dates extend to end-of-day (`T23:59:59`) so the day is inclusive.
- Full ISO timestamps also accepted. ISO strings compare correctly lexically in SQLite; all values parameterized (no interpolation).
- Listing limit bumps 20 → 50 while a filter is active.

**Operator flow**:

```bash
wolfctl reset                    # archive + fresh start (config untouched)
wolfctl start                    # Session #1, clean statistics
wolfctl stats --since 2026-08-14 # date-range views of current DB
wolfctl archives                 # revisit any archived era anytime
```

**Tests**: `tests/test_phase6d_reset_and_datestats.py` — 21 tests: archive-never-deletes source lock (rm allowed only for stale live-state), timestamped archive name, confirmation + `--yes` bypass, daemon stop-if-running, `archives` glob + span reporting, `_stats_date_window` unit tests (empty / since-only / bare-until end-of-day / full-ts), functional filtered queries against a seeded temp DB (since-filter, inclusive bare-until, combined window, no-filter regression), limit-bump, argparse surface. Version assertions bumped across phase5e/5v/5w/5x/5z/6a/6b/6c. **643 total passing.**

**Errata (same-day hotfix)**: `--since`/`--until` initially landed inside the mutually-exclusive command group, so argparse rejected `--stats --since DATE` ("not allowed with argument --stats"). Fixed by moving both to the standalone parser (`parser.add_argument`) — they are modifiers, not commands. Regression tests added: source-shape check + functional subprocess smoke (`--stats --since` parses cleanly). **645 total passing.**

Zero changes to the bet loop, DB writer, config schema, TG bot, install.sh, or sibling bots.

`VERSION` `2.30.8` → `2.30.9`. `wolfctl` `v3.8` → `v3.9`.

## CLI v2.30.8 — Fix stale `bet_value` on load / config-set + surface `/auto/start` 4xx bodies (2026-08-01)

Live outage: `wolfctl start` on the freshly-updated daemon reported `Last Error: Auto-bet start failed: HTTP 422` and placed **zero bets**. Investigation confirmed a long-latent config-derivation bug.

### Root cause — stale `bet_value` never re-derives from `multiplier + rule`

Your config-show showed `multiplier=10` but `bet_value=50.49`. For `multiplier=10` the correct value is **90.09** (formula: `round(100 - 99/mult - 0.01, 2)`). Your `50.49` was the *default from old `multiplier=2` configs* persisted on disk. WolfBet server computed `win_chance = 100 - 50.49 = 49.51%`, compared to the required `99/10 = 9.9%`, mismatched, returned **HTTP 422 "Incorrect win chance given"**.

Three places that *should* have re-derived `bet_value`:
- `load_config()` copied every `_CONFIG_KEYS` field verbatim from disk — no derivation.
- `wolfctl config-set multiplier=10` treated `bet_value` as an independent field — didn't recompute.
- The wizard *did* derive it correctly, but you'd only hit that path via a full re-setup.

CHANGELOG v1262 claimed "engine derives `bet_value` from `multiplier + rule` on init" — but that fix only landed in `wolfbet.py`'s `/bet` self-correction path. Auto games never hit `/bet` (they use `/auto/start` → `/auto/play`), so no self-correction ever ran. Silent staleness forever.

### Second defect — `state.last_error` hid the actual server message

`api_auto_start` set `state.last_error = f"Auto-bet start failed: HTTP {r.status_code}"` — the WolfBet response body (`{"message": "Incorrect win chance given"}`) was logged but never surfaced to `wolfctl status`. You saw bare `HTTP 422` and had to trawl logs to find the actual reason.

### Fixes

- **New helper `_derive_bet_value(multiplier, rule)`** — single source of truth. Formula: `round(100 - 99/mult - 0.01, 2)` for `over`, `round(99/mult, 2)` for `under`. Safe default (`50.74`) when `multiplier ≤ 0`.
- **`load_config()` re-derives on load** when the disk value mismatches the canonical formula. Logs a one-line `logger.warning` only when the correction actually fires.
- **`_apply_mutation({"multiplier": …})` re-derives** when no explicit `bet_value` is in the same call. `msgs` shows `bet_value re-derived: 90.09 (was 50.49; from multiplier=10 rule=over)`. Explicit `bet_value` override in the same call wins.
- **Wizard step_multiplier** factored through the new helper (DRY — was inline).
- **`api_auto_start` 4xx handler** now includes a truncated response-body snippet in `state.last_error` so operators self-diagnose from `wolfctl status` instead of log-trawling. Format: `Auto-bet start failed: HTTP 422 — {"message":"Incorrect win chance given"}`.

### Immediate operator recovery (works today, before v2.30.8 ships)

```bash
python3 -c "import json,os; p=os.path.expanduser('~/.wolfbet_autobot.json'); c=json.load(open(p)); m=float(c.get('multiplier',2.01)); c['bet_value']=round(100-99/m-0.01,2) if c.get('rule')=='over' else round(99/m,2); json.dump(c,open(p,'w'),indent=2); print('bet_value ->',c['bet_value'])"
wolfctl restart
wolfctl status   # expect bets placing, no HTTP 422
```

**Tests**: `tests/test_phase6c_bet_value_derivation.py` — 16 tests covering `_derive_bet_value` for over/under/zero-multiplier, `load_config` auto-correct of stale values, `load_config` preserving matching values, `_apply_mutation` re-derive on multiplier change with `msgs` entry, explicit `bet_value` override wins, no spurious message when bet_value unchanged, `api_auto_start` includes body snippet in `state.last_error` (grep-based source check), helper defined at module scope, called from load_config + _apply_mutation + wizard. 6 version-assertion tests bumped (`2.30.7` → `2.30.8`). **622 total passing.**

Zero changes to the bet loop, DB writer, credential paths, config schema (existing `bet_value` field stays; now derived, not authoritative), install.sh, wolfctl, TG bot, or sibling bots.

`VERSION` `2.30.7` → `2.30.8`. `wolfctl` unchanged.

## CLI v2.30.7 — CFG-row Ms shows per-window progress (resets after each bump) (2026-07-20)

You saw:

```
CFG  Ms +1.54943550/1.94295672 (80%)
```

with `profit_threshold=0.64765224`. The 80% is `profit / next_ms` = cumulative session progress against the current milestone target. Since `next_ms = 3 × threshold` (2 bumps already fired), the display was actually saying "80% of the way to the 3rd milestone from zero" — technically true, but not what an operator asking "how close am I to the **next** bump?" would expect. The percentage never reset after each bump.

Fix: `_build_config_progress_row`'s `Ms` segment now shows window-relative progress:

- `window_start = max(0, next_ms - profit_threshold)` — the last-fired milestone.
- `window_progress = max(0, profit - window_start)` — progress inside the current window.
- `pct = window_progress / profit_threshold * 100` — always 0-100% within one threshold unit.

Your live scenario now reads:

```
CFG  Ms +0.25413102/0.64765224 (39%)
```

39% = the current threshold window is 39% done. After the next bump fires (profit crosses 1.94295672), the display resets to `+0.00000000/0.64765224 (0%)` and climbs to 100% again. Denominator is always one `profit_threshold` unit; numerator is the current window's progress.

**Tests**: `tests/test_phase6b_ms_window_display.py` — 8 tests including the exact user scenario (80% → 39%), fresh session, just-after-bump (~0%), just-before-bump (~99%), denominator always = threshold, backwards-compat with pre-v2.30.1 state files, segment elided when threshold unset. Two existing phase5u tests updated for the new window semantics. **606 total passing.**

Zero behaviour change to the bump-firing code (`_apply_mutation`, `_reset_session_for_recurring`, and the two profit-increment call sites are untouched). Purely a display arithmetic swap in one segment. `state.next_profit_milestone` remains the actionable target for the bump code; the display now derives the window from it instead of using it as the denominator.

`VERSION` `2.30.6` → `2.30.7`. `wolfctl` unchanged. TG untouched.

## CLI v2.30.6 — raise arbitrary `recurring_delay` cap 3600 → 86400 + surface silent numeric clamps (2026-07-20)

You ran `wolfctl config-set recurring_delay=10800` (3-hour recurring pause between cycles) and got a confusing panel:

```
Config updated
  recurring_delay: 3600  →  3600
Saved to /root/.wolfbet_autobot.json
Daemon reloaded live config.
```

Your typed 10800 never made it in. Three defects — all fixed here.

**Defect A — arbitrary 3600 s cap** on `recurring_delay` in `_apply_mutation` was a mirror of the TG bot's default cap, not a technical limit. Grep confirmed `time.sleep(state.recurring_delay)` is legal at any positive value; state-file writes cast to `int`; nothing breaks at 10800, 21600, or 86400 (24 h).

Fix: cap raised to **86400 s (24 h)** in `_apply_mutation`, the wizard clamp, and the doc comment at `BotState.recurring_delay`. TG side already allowed larger values (floor 5 s, no upper cap).

**Defect B — silent clamps** at three sites (`recurring_delay`, `purge_days`, `pipeline_depth`) swallowed out-of-range input with no user signal. `purge_days` (1–30) and `pipeline_depth` (1–8) still have legitimate technical caps; they just weren't visible.

Fix: new tiny `_clamped(name, raw, lo, hi)` helper inside `_apply_mutation` — clamps and appends `⚠ <name> clamped from <raw> to <clamped> (allowed: <lo>..<hi>)` to the returned `msgs` list. All three sites now use it.

**Defect C — `cmd_config_set` discarded `_apply_mutation`'s return value**, so warnings never reached the operator.

Fix: `cmd_config_set` now captures `mutation_msgs` and prints `⚠` lines beside the standard "old → new" diff. Silent clamps become impossible.

After this ships:
- `wolfctl config-set recurring_delay=10800` → panel shows `recurring_delay: 3600 → 10800` and stores 10800.
- `wolfctl config-set recurring_delay=100000` → `⚠ recurring_delay clamped from 100000 to 86400 (allowed: 0..86400)`.
- `wolfctl config-set pipeline=20` → `⚠ pipeline_depth clamped from 20 to 8 (allowed: 1..8)`.

**Tests**: `tests/test_phase6a_clamp_visibility.py` — 12 tests covering 10800 + 86400 accepted, over-cap clamps with warning, purge_days / pipeline_depth warnings, in-range no-warning, `_clamped` helper source markers, `cmd_config_set` captures msgs, doc comment updated. `test_phase5f_recurring_delay.py`'s old cap assertion updated (99999 → 86400 instead of 3600). Version assertions bumped in phase5v/5w/5x/5z. **598 total passing.**

Zero changes to the bet loop, DB writer, credential paths, install.sh, TG bot (already allowed larger values), or sibling bots.

`VERSION` `2.30.5` → `2.30.6`. `wolfctl` unchanged.

## CLI v2.30.5 + wolfctl v3.8 + install.sh — three follow-up defects from real-world doctor run (2026-07-20)

You pulled v2.30.4 + wolfctl v3.7, ran `wolfctl doctor --fix` on the old server, and surfaced three real defects. All three fixed in this patch.

### Defect A — `Efficiency: >100%` was firing on every diagnose call

v2.30.4's Efficiency ratio was `Recent (cycle_p50) / Theoretical (api_mean)` — median vs mean from different distributions. On right-skewed traffic (a few slow API requests dragging the mean up while the median stayed low), Recent > Theoretical made `>100%` the default reading instead of a rare drift signal.

Fix: new `_compute_cycle_mean()` (arithmetic mean over the rolling cycle window, symmetrical with `_compute_api_rolling_avg`). `_compute_recent_bps()` now uses cycle_mean instead of cycle_p50, so both sides of the efficiency ratio derive from mean(latency) — Little's-Law-consistent (throughput ≈ 1/mean). Efficiency now reads as a real percentage in [0, 100%] on healthy traffic; `>100%` flag stays as a safety net for pathological drift. `cycle_p50` + `cycle_p99` remain in the display for tail-jitter visibility.

### Defect B — Daemon `renice`/`ionice` wore off after every `wolfctl update`

v2.30.2 auto-restarts the CLI daemon after every `wolfctl update`. Strict improvement, but per-PID `renice -n -5` / `ionice -c 2 -n 0` the operator applied earlier get erased because the PID changes on restart.

Fix: `install.sh` now writes `Nice=-5` + `IOSchedulingClass=best-effort` + `IOSchedulingPriority=0` into the `[Service]` block of `wolfbet.service`. `wolfctl update`'s existing systemd-migration loop (which already handled `Restart=always → on-failure` in v2.28.1) is extended to add these three directives when absent. Idempotent: subsequent runs find `Nice=` already present and skip. `daemon-reload` fires whenever any migration writes. Daemon now starts pre-reniced on every restart automatically — no more manual dance after `wolfctl update`.

### Defect C — `wolfctl doctor`'s `[SKIP]` block stayed silent when governor path was entirely unreadable

The old server showed `CPU governor: unknown` (VPS host doesn't expose `/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor`). v3.7's `[SKIP]` fallback only fired when `$gov` was set AND ≠ "performance"; when unreadable, the whole cpupower block was silent. Technically correct but hid the honest "your VPS can't do this" advice.

Fix: new `elif` branch in `cmd_doctor` — when `$gov` is empty AND `systemd-detect-virt` shows virtualization, print `[SKIP]  CPU governor path not exposed on this virtualized host — VPS provider controls it, migrate to a dedicated-CPU plan if needed`. Zero behaviour change on hosts that do expose governor info.

### Server-side apply

Just run `wolfctl update` — v3.7's self-copy + drift warning handles the upgrade, and the daemon auto-restarts to pick up `Nice=-5`. On the first update after this ships, you'll see:

```
Added Nice=-5 + IO priority to wolfbet.service
CLI daemon restarted (session auto-resumed).
```

Then `ps -o ni= -p $(pgrep -f wolfbet.py)` returns `-5`.

**Tests**: `tests/test_phase5z_efficiency_alignment.py` — 8 tests covering cycle_mean helper, Recent-uses-mean swap, efficiency=100% on matched distributions, efficiency stays ≤100% on the right-skewed real-world distribution, state file emits `cycle_mean_ms`, `cycle_p50` still available. `test_phase5y_wolfctl_doctor.py` extended with `test_governor_skip_when_unreadable_on_virt`. Version assertions bumped in phase5v/5w/5x/5e. **586 total passing.**

Zero changes to the bet loop, DB writer, credential paths, config schema, TG bot, or sibling bots.

`VERSION` `2.30.4` → `2.30.5`. `wolfctl` `v3.7` → `v3.8`.

## wolfctl v3.7 — `wolfctl doctor` (health check + safe automated fixes) (2026-07-20)

After v2.30.4 sharpened the "CLI overhead is high" advice, you correctly asked: **"Is there anything I need to grant on the server to reduce overhead? Any command to clear space or free up resources?"** The answer turned out to be yes — a manual runbook applied on the old server (VACUUM, journal vacuum, renice, ionice) dropped cycle from **143 → 79 ms** and stabilised overhead from a **0→120 ms** swing to a stable 39 ms. Most of the win came from `journalctl --user --vacuum-size=100M` freeing **3.9 GB** of accumulated journals.

`wolfctl doctor` bundles that runbook into one discoverable subcommand.

**New `wolfctl doctor`** — read-only health check:

- Runs `wolfctl diagnose` inline (current bottleneck breakdown).
- Reports DB size + page fragmentation ratio.
- Reports journal size for `wolfbet.service`.
- Reports system resources (load average, memory + swap, disk usage on `$HOME`, CPU governor).
- Reports daemon PID + nice + ionice class.
- Emits a **prescriptions** block: exact commands to fix each detected issue, grouped by `[SAFE]` (no privilege) / `[PRIV]` (needs sudo) / `[SKIP]` (impossible on virtualized hosts).

**New `wolfctl doctor --fix`** — applies the SAFE subset only, with per-fix confirmation (`--yes` / `-y` to bypass). Ordered:

1. `wolfctl cleanup 1` — force bet-row retention.
2. `sqlite3 VACUUM` — only if fragmentation ≥ 20%.
3. `journalctl --user --vacuum-size=100M` — only if journal > 500 MB.

Privileged fixes (renice, ionice, cpupower) are prescribed as text only — never auto-applied. On virtualized hosts (QEMU-KVM VPS), `cpupower` is downgraded from `[PRIV]` to `[SKIP]` with honest advice ("guests cannot set the governor; ask your VPS provider or migrate to dedicated CPU") — this was the specific gotcha you hit when the real-world runbook errored out on `cpupower frequency-set`. Detected via `systemd-detect-virt`.

**Server-side runbook (works today, without waiting for the update)**:

```bash
wolfctl cleanup 1
sqlite3 ~/.wolfbet_autobot.db "VACUUM;"
journalctl --user --vacuum-size=100M
PID=$(pgrep -f 'wolfbet\.py.*--daemon' | head -1)
sudo renice -n -5 -p $PID && sudo ionice -c 2 -n 0 -p $PID
wolfctl diagnose   # confirm delta
```

After v3.7 ships, the equivalent is one command: `wolfctl doctor --fix`.

**Tests**: `tests/test_phase5y_wolfctl_doctor.py` — 12 tests covering version bump, dispatcher wiring, prescription block content, VACUUM threshold gating (freelist ratio ≥ 20%), journal-size threshold gating, virt detection + `[SKIP]` on VPS, `--fix` block never invokes sudo/renice/cpupower. Version assertions bumped in phase5v/5e. **576 total passing.**

Zero changes to `wolfbet.py`, `install.sh`, `tg/*`, `core/*`, sibling bots, bet loop, DB writer, credential paths, or config schema.

`wolfctl` `v3.6` → `v3.7`. `wolfbet.py` remains at v2.30.4.

## CLI v2.30.4 — Windowed cycle metrics + windowed efficiency + sharpened "CLI overhead" advice (2026-07-20)

v2.30.3 unified the metric formulas and killed the fake `=` sum, but two follow-on issues surfaced on the old server (which does ~40-50 ms of genuine per-iteration overhead due to slower silicon):

- `overhead` still swung `46 → 120 → 46 ms` across successive `wolfctl diagnose` calls. Root cause: `_record_cycle_ms` used an α=0.1 EMA — one 400 ms iteration (retention worker, GC pause, `/auto/start` handshake) bumped displayed cycle by ~40 ms and held it for 5-10 samples.
- `Actual: 13.6 BPS   Theoretical: 12.6 BPS` — efficiency > 100% is arithmetically impossible. Session-wide `Actual` and rolling-window `Theoretical` came from different windows; when the API happened to be slower recently than the session mean, the ratio broke.
- Advice tier fired "CLI overhead is high — check disk I/O + CPU" on a healthy shared-VPS host, misdirecting the operator into hunting a bug that doesn't exist.

**Fixes**:

- **Rolling cycle window** — replaces the α=0.1 EMA in `_record_cycle_ms` with a bounded deque (maxlen=500), mirroring `_api_samples`. New `_compute_cycle_p50` / `_compute_cycle_p99` helpers use the same percentile shape as `_compute_api_p50` / `_compute_api_p99`. Median cycle is now stable across single-sample perturbations; p99 exposes tail jitter separately. `_cycle_ms` field aliased to `cycle_p50` for backwards state-file compat.
- **Windowed efficiency** — new `_compute_recent_bps()` (= `1000 / cycle_p50`); `_compute_efficiency_pct` now uses `recent_bps / theoretical_max_bps` so both sides of the ratio come from the same window. Session-wide `bets_per_second` stays visible in diagnose for context. `cmd_diagnose` shows a `>100%` prefix (rare — indicates api_avg drift) instead of the impossible-arithmetic display or a silent clamp.
- **Sharpened "CLI overhead" advice** — when overhead is high AND DB writer + rate limits are all healthy (no fallback_sync, hwm < 100, rate_remaining > 200), the advice now says the delta is likely shared-tenancy CPU/disk jitter (VPS silicon lottery) rather than a fixable code issue. Points operators to the earlier "redeploy VPS / migrate tier" branch instead of chasing a ghost.
- **`cmd_diagnose` display additions** — new lines showing `Recent BPS`, `Efficiency`, `Cycle: median X p99 Y (n=N samples)`.

**Tests**: `tests/test_phase5x_cycle_windowing.py` — 13 tests covering median stability under outliers, window rolling, p99 tail behaviour, overhead stability, windowed efficiency vs session-wide fallback, state-file backward compat, sharpened advice text. Two existing phase3 tests updated to reflect new deque semantics (was: assert EMA smoothing). **564 total passing.**

Zero behaviour change to the bet loop, DB writer, credential paths, or config schema. `wolfctl` unchanged. TG untouched (reads the same underlying metrics; fix flows through automatically). Sibling bots untouched.

`VERSION` `2.30.3` → `2.30.4`.

## CLI v2.30.3 — Fix metric correctness: overhead jitter, fake diagnose "=" line, wrong BPS ceiling for auto-games (2026-07-21)

Real-world observation from 18h/1M-bet sessions: `wolfctl diagnose` output showed `api_p50=50ms + overhead=0ms + delay=0ms = 151ms/bet` (sum obviously doesn't match) and overhead swinging `0 → 43 → 0` between consecutive diagnose calls seconds apart. `wolfctl status` on same server: `Cycle 143ms/bet → 6.99 BPS ceiling` while actual was 14.9 BPS and diagnose's Theoretical max was 15.1. Two "ceilings" for the same session, both wrong in different ways. Four defects fixed.

**Defect A — overhead mixed smoothed cycle EMA with UNSMOOTHED last-single API ms.** `_record_cycle_ms` computed `overhead = max(0, cycle_ma - _last_api_ms - delay*1000)`, where `cycle_ma` is smoothed (α=0.1 EMA) but `_last_api_ms` is the single most recent request. When the most recent bet's API dt happened to exceed the smoothed cycle EMA, overhead clamped to 0 for a moment. Fix: use `_compute_api_p50()` (already smoothed via median over the rolling deque). Falls back to `_last_api_ms` on empty deque so cold-start still renders sensibly.

**Defect B — cmd_diagnose printed `= cycle_ms` but it's not a real sum.** The three components (`api_p50`, `overhead`, `cycle_ms`) come from three independently-smoothed windows. Fix: display uses `≈` and splits into two lines — `Cycle breakdown: … ≈ N ms/bet (decomposition)` and `Measured cycle: N ms/bet (outer-iteration EMA)`. Divergence between them is now a real diagnostic signal (queue backpressure / GC).

**Defect C — cmd_status "BPS ceiling" was `1000/cycle_ms`, wrong for auto-games.** For `limbo-auto`/`dice-auto`, WolfBet plays many bets per outer-loop iteration (server-side batching); `cycle_ms` measures ONE outer iteration; `bets_per_second` counts EVERY batched bet. So `1000/cycle_ms` gives an outer-iteration ceiling, not a per-bet ceiling — hence "6.99 BPS ceiling" alongside actual 14.9 BPS. Fix: both `cmd_status` and `build_dashboard_screen` now read the same `theoretical_max_bps` `cmd_diagnose` uses (unified formula). Auto-games get a `(batched — cycle is per outer iteration)` footnote so the meaning is clear.

**Defect D — `_compute_theoretical_max_bps` used all-time api_avg.** After 18h and 1M samples, early-session outliers still biased the mean. Fix: new `_compute_api_rolling_avg()` helper computes mean over the same `_api_samples` window that feeds p50/p99. Consistent with existing metric semantics.

Zero changes to the bet loop, DB writer, rate-limit paths, credential paths, or state-file schema. Display + smoothing correctness only. TG untouched (its `/diagnose` reads the same underlying metrics via `engine.get_status()`, so the fix flows through automatically). Sibling bots untouched.

**Tests**: `tests/test_phase5w_metric_correctness.py` — 12 tests covering overhead stability under single-sample perturbation, fallback path on empty deque, diagnose `≈` + "Measured cycle" line, unified `theoretical_max_bps` in both status + dashboard, rolling window vs all-time mean, backward-compat state-file field. Existing phase3 metric test updated for the new rolling-window semantics. **551 total passing.**

`VERSION` `2.30.2` → `2.30.3`. `wolfctl` unchanged.

## CLI v2.30.2 / wolfctl v3.6 — wolfctl update can now self-heal + CLI daemon auto-restarts + wizard banner (2026-07-20)

Real-world ops report: user ran `wolfctl update` on their server, `git pull` said `a25f600..bdefbe4` (our latest), and yet `wolfctl config-show` returned the v3.1 help menu and the wizard banner still read "WolfBet AutoBot v2.6". Three independent defects diagnosed and fixed.

### Defect A — `wolfctl update` couldn't self-upgrade old (v3.1) installs

The stale installed wolfctl at `~/.local/bin/wolfctl` predated the v3.5 self-copy block, so its `cmd_update` pulled bot files but never overwrote itself. Result: every update ran the same v3.1 dispatcher, no v3.3+ subcommand was reachable, and no drift warning surfaced. Chicken-and-egg — the old wolfctl can't be forced to self-upgrade retroactively.

Fix: introduced `WOLFCTL_VERSION=3.6` constant. `cmd_update` now reads the repo copy's version, compares it to the running interpreter's `$WOLFCTL_VERSION`, and prints a yellow "wolfctl was stale" warning + one-liner escape hatch (`exec wolfctl update`) when they diverge. Once operators break out of the v3.1 chicken-and-egg (one-liner below), every future update self-heals AND surfaces drift if it ever recurs.

### Defect B — `wolfctl update` never restarted the CLI daemon

`cmd_update` restarted `wolfbet-tg` and `wolfbet-web` (when active) but not `wolfbet.service`. The CLI daemon kept the pre-pull `wolfbet.py` mapped in memory until the operator ran `wolfctl restart` manually. That explains why "fixed" bugs (e.g. v2.30.1's profit_increment reset) could keep silently failing on the server — the daemon was running old code.

Fix: added a systemctl restart block mirroring the TG block. `wolfctl update` now: pulls → refreshes files → self-copies wolfctl → migrates systemd Restart= → reinstalls deps → restarts web (if active) → restarts TG (if active) → **restarts CLI (if active)**. --resume preserves session state.

### Defect C — wizard banner hardcoded to `v2.6`

`setup_wizard()` rendered `[bold cyan]WolfBet AutoBot v2.6[/]` — a literal that never got updated after v2.6.0 (2026-03-12). Dashboard header + `--version` correctly interpolated `VERSION`, but the wizard banner was stuck. Fix: one-line f-string swap — banner now reads `v{VERSION}` like the rest.

### Server upgrade one-liner

Operators still on the v3.1 chicken-and-egg need to run this **once** to break out:

```bash
cd ~/wolfbet && git pull --ff-only && install -m 0755 wolfctl ~/.local/bin/wolfctl && systemctl --user restart wolfbet wolfbet-tg
```

That gets them onto v3.6 wolfctl (all subcommands work) and forces the CLI daemon onto v2.30.2 wolfbet.py. Every subsequent `wolfctl update` self-heals AND restarts the CLI daemon AND surfaces drift automatically.

**Tests**: `tests/test_phase5v_wolfctl_update_self_heal.py` — 10 tests covering version bumps, drift-warning marker, CLI restart block, wizard-banner f-string, and a defensive dispatcher-lock test that asserts every `wolfctl X` command advertised in `usage()` maps to a `case` handler (prevents a future v3.1-style regression from shipping a stale dispatcher). **539 total passing.**

`VERSION` `2.30.1` → `2.30.2`. `wolfctl` `v3.5` → `v3.6`. TG untouched. install.sh unchanged.

## CLI v2.30.1 — fix profit_increment: CFG row lies + milestone doesn't reset across recurring cycles (2026-07-19)

Two independent v2.30.0 regressions that together made `profit_increment` look broken across recurring cycles.

Real-world report — user had `profit_threshold=0.00696011` + `profit_increment` (small positive) with `recurring=on`. Monitor showed `CFG  Ms +0.04527414/0.00696011 (100%)` for the entire third cycle, but base_bet had only bumped once (~`0.00000144 → 0.00000161`) instead of the 6+ bumps `profit / threshold` should have implied.

### Bug A — CFG row read the wrong milestone field

`_build_config_progress_row` (v2.30.0) read `last_profit_milestone` from the state file — but that field is the **notification** tracker (mutated only by `_check_milestone` under `state.milestone_profit > 0`), *not* the **actionable** target `state.next_profit_milestone` used by the profit-increment branch. With no notification milestone set, `last_profit_milestone` stayed at 0, so the row rendered `next_ms = 0 + threshold = threshold` and every profit above the threshold clamped to `100%`. Two unrelated variables were conflated by name and state-file key.

Fix: `state.next_profit_milestone` is now persisted to the state file (new `next_profit_milestone` key at `_save_state_file`). `_build_config_progress_row` reads it directly, with `profit_threshold` as fallback so pre-v2.30.1 state files still render sensibly. `last_profit_milestone` is untouched — still used correctly by the notification path.

### Bug B — `_reset_session_for_recurring` never reset the milestone

The recurring reset cleared `state.profit`, `state.total_bets`, `_last_profit_milestone`, etc., but left `state.next_profit_milestone` at cycle 1's high-water mark. Cycle 2's fresh `profit=0` would then need to accumulate up to `k × profit_threshold` before firing a single bump — cycle 3 needed even more. In practice, exactly one bump fired in cycle 1 and then the target was permanently out of reach. `state.base_bet` correctly persisted (matches the user's observed monitor).

Fix: `_reset_session_for_recurring` now sets `state.next_profit_milestone = state.profit_threshold` immediately after clearing `state.profit`. Each recurring cycle now hunts a fresh milestone from zero-profit. `state.base_bet` is deliberately preserved — bumps stack across cycles, matching the progressive-increment mental model.

**Tests**: `tests/test_phase5u_profit_increment_persistence.py` — 8 tests covering state-file persistence, CFG row read + backward-compat fallback, milestone reset, base_bet preservation, and a 3-cycle regression simulating the user's scenario (base bumps 3× instead of 1×). **529 total passing.**

`VERSION` `2.30.0` → `2.30.1`. TG untouched. `wolfctl` unchanged.

## CLI v2.30.0 — Monitor Row 6 filled with live config-progress (2026-07-19)

Row 6 of `wolfctl monitor` was a blank `" P/L —"` — a sparkline placeholder that never had backing history. User running with `profit_threshold=0.00050207` + `profit_increment=0.00000001` asked for the row to show *what is configured* alongside *what is happening*, so a glance tells them which stop conditions are armed and how close each budget is to firing.

New `_build_config_progress_row(d)` — pure function, one line, elides unset fields:

- **`Ms`** — profit-increment milestone: `Ms +0.00042/0.00050 (84%)`. Rendered only when `profit_threshold` + `profit_increment` set. Target = `last_profit_milestone + profit_threshold` (same derivation as `state.next_profit_milestone` at the two profit-increment call sites).
- **`MaxP` / `MaxL` / `MaxB` / `MaxW`** — stop-condition progress with `current/target (pct%)`. `MaxL` uses `abs(profit)/abs(max_loss)` when profit < 0; dim when profit ≥ 0.
- **`MinBal`** — floor semantics, no pct. `yellow` when balance ≤ floor × 1.05, else `dim`.
- **`Rec on(60s)`** — cyan; skipped when `recurring=False`.
- **Progress-tinting** — `green` <80%, `yellow` ≥80%, `bold_red` ≥95%. Same visual alarm across every budget-style condition.
- **Fallback** — with nothing armed, row renders `" CFG  ─  no goals set"` (dim). Row is never blank.

Zero new state fields — every value already lives in `_save_state_file`'s dict. Zero changes to the bet loop, `_LIVE_RELOAD_KEYS`, or `_MONITOR_FIXED_ROWS` (still 11). Row 6 label in the layout-budget comment updated from *"P/L sparkline placeholder"* to *"Config-progress row"*.

**Tests**: `tests/test_phase5t_monitor_config_row.py` — 18 tests covering fallback, single-line invariant, per-field elision, milestone %, MaxL sign handling, MinBal proximity, Rec on/off, all three color tiers. **521 total passing.**

`VERSION` `2.29.0` → `2.30.0`. TG untouched. `wolfctl` unchanged.

## CLI v2.29.0 / TG v1.18.0 — profit_increment restarts auto-bet cycle + TG parity ports (2026-07-19)

Two threads landed in the same release.

### CLI — `profit_increment` restarts the WolfBet auto-bet UUID (bug fix)

Real-world bug: with `profit_increment=0.00000001` + `profit_threshold=0.00050207` + `game=limbo-auto`, the daemon's monitor showed `Base 0.00000011` mid-session — but the actual bets on WolfBet stayed on the original base. Every subsequent auto-cycle from the same session kept the old base until session end.

Root cause: for auto-games (`limbo-auto`, `dice-auto`) the Martingale progression runs **server-side** on WolfBet — the CLI just polls `/auto/play` and gets each next bet amount computed from the `base_bet` submitted at `/auto/start`. When `state.base_bet` changes mid-session, the CLI never told WolfBet. The new base only took effect at the next `_reset_session_for_recurring` (cycle end) or full restart.

Fix — clear `state._auto_uuid` when the increment fires under `is_auto_game(state.game)`. Both call sites patched (`betting_loop` serial path + `_apply_outcome_locked` pipelined path). The next loop iteration's existing auto-restart branch calls `api_auto_start()` with the fresh `state.base_bet`. Same-session invariant preserved — `session_id`, `session_start`, counters, DB row all unchanged. One CLI session now spans multiple WolfBet auto UUIDs, one per increment tick, each with escalating base_bet.

### TG — /diagnose + /analytics + uptime freeze + SQLite PRAGMAs

- **`/diagnose`** (mirrors CLI `wolfctl diagnose`): top blocker between Network / CLI overhead / User delay, plain-English advice line. Reuses `core.diagnostics.percentile`. No new engine fields.
- **`/analytics`** (mirrors CLI `wolfctl analytics`): lifetime totals + by-game + by-strategy + top-5 best + top-5 worst. Uses the CLI's SQL aggregation patterns verbatim; renders as Telegram-Markdown.
- **Uptime freeze during pause** (mirrors CLI v2.28.4): `BettingEngine` gains `session_bet_ended_at` + `session_pause_started_at`. `pause()` freezes both; `resume()` clears. `get_status()` uses the frozen bet-end as the uptime ceiling. `_db_save_session(final=True)` writes the frozen `ended_at` when set. `/status` now emits `uptime_sec` + `pause_remaining_sec` for downstream renderers.
- **SQLite PRAGMA tuning** (mirrors CLI v2.27.0): `_get_conn` adds `synchronous=NORMAL` + `busy_timeout=5000` + `cache_size=-32000` + `temp_store=MEMORY` after the existing `journal_mode=WAL`. Same 30–40 ms per-commit fsync savings, WAL-safe.

**Tests**: `tests/test_phase5r_tg_ports.py` (19 tests) + `tests/test_phase5s_cli_profit_increment_auto.py` (7 tests). **503 total passing.**

`VERSION` `2.28.4` → `2.29.0`. `tg/__init__.py` `1.17.0` → `1.18.0`. `wolfctl` unchanged (v3.5).

## CLI v2.28.4 — Uptime freezes during recurring pause (bet-time vs pause-time split) (2026-07-19)

User confusion: `wolfctl status` showed a session's `Uptime` as ~1 h 5 min when only ~5 min of that was actual betting — the rest was the `recurring_delay=3600` sleep. Same session ran ~8000 bets in ~5 min then slept 1 h; the uptime clock kept ticking during the sleep because `state.session_start` wasn't advanced.

- **New `BotState.session_bet_ended_at` + `session_pause_started_at`** — frozen `time.time()` snapshots. Both are `None` during active betting.
- **`_reset_session_for_recurring` sets both timestamps immediately BEFORE `time.sleep(recurring_delay)`** and clears them right before the new `session_id` / `session_start` are set. Zero change to the reset semantics — only new bookkeeping.
- **`_save_state_file`** now computes `uptime_sec = int((session_bet_ended_at or now) - session_start)`. During the pause, uptime freezes at bet-end.
- **`_save_state_file`** also emits `pause_remaining_sec` (`max(0, recurring_delay - elapsed_since_pause_start)`) and `recurring_delay_sec` so the monitor + `--status` can show "45m remaining / 60m total" without recomputing.
- **`_db_save_session`** uses the frozen bet-end timestamp for `sessions.ended_at` during the pause. `wolfctl stats` now shows the real bet-window duration per session instead of "5 min bet + 1 h pause" lumped together.
- **Monitor Row 1 header** — when `pause_remaining_sec > 0`, the `● LIVE` indicator becomes `◐ PAUSED HH:MM:SS/HH:MM:SS` (bold cyan). Header width is recomputed against the plain-text length so the trailing `───` fill is correct.
- **`cmd_status` Uptime row** — appends `· Recurring pause: 45:00 remaining / 60:00 total` when in the pause window.

**Tests**: `tests/test_phase5q_uptime_pause.py` — 11 tests. Defaults, `_reset_session_for_recurring` freezes-then-clears (captured via a `time.sleep` monkeypatch that snapshots state during the pause), skip-when-delay-zero, `_save_state_file` uptime + pause remaining under three fixture states, `pause_remaining_sec` clamps to 0 when expired, `_db_save_session` freezes `ended_at` during pause and uses `now` when no freeze, monitor + status render pause indicator, state file always emits pause fields. **477 total passing.**

Zero behaviour change to the bet loop, recovery, retention, or config surfaces. Purely time-accounting.

`VERSION` `2.28.3` → `2.28.4`. `wolfctl` unchanged (v3.5).

## CLI v2.28.3 — SIGHUP-driven live config reload (fix silent mid-session tweaks) (2026-07-19)

Real-world bug: user set `recurring=on` via `wolfctl config-set`, saw the "Saved" confirmation and `wolfctl config-show` reported `on`. But when the daemon's session #30 hit `max_profit`, the cycle exited normally and no recurring restart fired.

Root cause: `wolfctl config-set` mutates only the CLI process's `state` object and persists to disk. The **running daemon** captured `state.recurring` at boot and never re-reads. No SIGHUP handler, no file watcher, no IPC existed. Every mid-session `wolfctl config-set` was silently ignored by the daemon until a full `wolfctl restart`. Classic S-13 "silent success then no-op" antipattern.

Fix — bounded reload via SIGHUP:

- **New `_LIVE_RELOAD_KEYS` allowlist** (24 fields safe to change mid-session): recurring + recurring_delay, all 5 stop conditions, bet_delay + basebet_pct + 4 streak-tuple fields + 4 milestones + profit_increment/threshold, pipeline_depth + proxy + purge_days + db_write_mode, custom_rules_text. Deliberately excludes api_key / currency / game / rule / multiplier / bet_value / strategy / strategy_key / base_bet / loss_mult / win_mult / delay_martin_threshold — those would corrupt session identity or the Martingale ladder mid-flight.
- **New `_reload_live_config()`**: re-reads `CONFIG_PATH` JSON, applies only allowlist keys under `state.lock`, decodes streak tuples via `parse_streak_delay` (v2.28.2 grammar), applies `_apply_proxy_to_session()` if `proxy` changed, re-parses custom rules if `custom_rules_text` changed. Returns `{field: (old, new)}` for logging. Never touches `session_id` / counters / DB.
- **New `_sig_reload(sig, frame)`**: SIGHUP handler. Calls `_reload_live_config`, logs applied fields at INFO. Never raises — errors are logged and swallowed (signal handlers must be short + safe).
- **SIGHUP registered in `main()`** alongside SIGUSR1/2, guarded by `hasattr(signal, "SIGHUP")` for Windows portability.
- **`cmd_config_set` extended**: after `save_config()`, reads `PID_PATH`, sends `SIGHUP` to the daemon via `os.kill`, prints "Daemon reloaded live config." On missing/stale PID: "Daemon not running; changes apply on next `wolfctl start`". If any mutation touches a NON-allowlist field, prints a heads-up: `N field(s) need a full restart to apply — game, strategy_key. Run wolfctl restart when ready.`
- **Daemon-side warning**: `_reload_live_config` also logs skipped unsafe keys at WARN so `journalctl --user -u wolfbet` shows what would need a restart.

**Tests**: `tests/test_phase5p_live_reload.py` — 20 tests. Allowlist membership (recurring in, api_key out, etc.), round-trip for recurring/max_profit/streak-tuples/pipeline/db_write_mode, unsafe-key skip semantics + WARN log, missing/malformed config file safety, `_sig_reload` exception-swallowing, `cmd_config_set` sends SIGHUP with correct PID when daemon alive + skips when PID_PATH missing, `main()` registers the handler. **466 total passing.**

`VERSION` `2.28.2` → `2.28.3`. `wolfctl` unchanged (v3.5).

## CLI v2.28.2 — monitor header restored + silent config-key persistence gap closed (2026-07-19)

Two real-world bugs found on the server, fixed in one release.

**Bug A — monitor session-title row clipped since v2.28.1.** The v2.28.1 tweak/export hint footer was added without updating `_build_monitor_screen`'s `bets_avail = term_h - 10` budget. Total rows overshot the terminal by one, so the top row (`─── WolfBet v… #N uptime ● LIVE ────`) scrolled off. Fixed by introducing `_MONITOR_FIXED_ROWS = 11` (with an inline row-inventory comment) and switching `bets_avail = max(0, term_h - _MONITOR_FIXED_ROWS)`. Bet list shows one fewer row; session-title returns to the top.

**Bug B — `wolfctl config-set recurring=on` silently reverted.** `_CONFIG_KEYS` was missing SIX fields that live in `_PRESET_KEYS`, `_CONFIG_SHOW_FIELDS`, and `_TWEAK_PARAMS`: `recurring`, `basebet_pct`, and all four `milestone_*`. Every `wolfctl config-set` mutation on those fields printed the diff, printed "Saved to …", but never wrote to `~/.wolfbet_autobot.json` — the next process launch (including the daemon's `--resume`) reverted them to `BotState.__init__` defaults. Silently broken since Phase 2 (v2.16.0). Additionally, the four streak-tuple fields (`streak_delay_loss/win/bets`, `streakbet_loss`) had a `_STREAK_TUPLE_KEYS` list for the preset system but no round-trip handling in `save_config` / `load_config` — same silent-failure symptom.

Fixed by:

- Adding the six missing fields to `_CONFIG_KEYS`.
- Extending `save_config` to encode `_STREAK_TUPLE_KEYS` as `"N:X"` strings (same grammar the preset system uses; `null` when disabled).
- Extending `load_config` to decode `_STREAK_TUPLE_KEYS` via `parse_streak_delay` (already imported from `core.net_helpers`).
- Backward compat: older configs without the new keys keep working; `load_config` skips missing keys, `BotState.__init__` defaults apply.

**Two regression guards** ship with the fix:

- `tests/test_phase5n_monitor_layout.py` — 7 tests locking the invariant that `_build_monitor_screen` renders exactly `term_h` lines for `term_h ∈ {24, 40, 80}`, and that the first line is the session title, and the last is the tweak hint. Any future footer/header add that breaks the budget fails this test.
- `tests/test_phase5o_config_persistence.py` — 14 tests round-tripping every newly-persisted field: `recurring` on/off, `basebet_pct`, all four `milestone_*`, all four streak-tuple keys (as strings for enabled tuples, `null` for disabled ones), plus a backward-compat check that legacy config files load without error.

**446 total passing.** `VERSION` `2.28.1` → `2.28.2`. `wolfctl` unchanged (v3.5).

## CLI v2.28.1 — monitor UX + systemd `Restart=` fix (2026-07-19)

Two real-world bugs from server usage:

1. **`wolfctl interactive` didn't advertise the working keys.** The monitor viewer handles `P/R/S/Q` (via SIGUSR1/USR2/TERM to the daemon) but not `T/E/H` — those require a foreground TTY. Users would press `T` expecting a tweak prompt, get nothing, and have no idea `wolfctl config-set` was the correct answer.

2. **Pressing `S` from monitor stopped the daemon — then systemd auto-restarted it ~10 s later.** Root cause was `install.sh` writing `Restart=always` in the systemd unit. When `S` sent SIGTERM the daemon exited cleanly (`code=exited, status=0/SUCCESS`), and systemd's `Restart=always` policy respawned it. The journal even printed `Scheduled restart job, restart counter is at 2`. The CLI's own `recurring` flag was `off` — the restart wasn't coming from there at all.

Fixes:

- **`_build_monitor_screen` footer** now includes a second line: `Tweak config: wolfctl config-set KEY=VALUE   · Export/history: wolfctl export / stats / analytics`. Correct alternative is one glance away.
- **`cmd_monitor` attach banner** extended: `Attaching to session #N (PID …)… Keys: P/R/S/Q · Tweak: wolfctl config-set KEY=VALUE`.
- **`cmd_monitor` stop path is now systemd-aware.** New `_is_systemd_managed(pid)` helper checks `/proc/{pid}/cgroup` for `wolfbet.service`. When true, `S` calls `systemctl --user stop wolfbet` (systemd knows to keep it down); when false, falls back to raw SIGTERM (unchanged for non-systemd installs).
- **`install.sh:108,136`**: `Restart=always` → `Restart=on-failure`. Clean stops stay stopped; real crashes still respawn.
- **`wolfctl update`** gains an **idempotent migration**: if `~/.config/systemd/user/wolfbet.service` (or `wolfbet-tg.service` / `wolfbet-web.service`) contains `Restart=always`, patches to `Restart=on-failure` and `daemon-reload`s. Existing installs get the fix automatically on their next `wolfctl update`. Subsequent updates find no match and skip.

**Tests**: `tests/test_phase5m_monitor_ux.py` — 15 tests (footer string presence, attach banner content, `_is_systemd_managed` under 3 fixture scenarios including a missing-`/proc/` fallback, `install.sh` `Restart=on-failure` grep, `wolfctl` migration branch grep, bash syntax valid for both scripts). **425 total passing.**

`VERSION` `2.28.0` → `2.28.1`. `wolfctl` unchanged (v3.5).

## CLI v2.28.0 + TG v1.17.0 — extract shared helpers to `core/` (CLI ↔ TG dedup + TG bug fixes) (2026-07-18)

User asked: "stop reimplementing what already exists — TG is the baseline, CLI ≥ TG, merge duplicated functions." Six pure-Python helpers were duplicated between `wolfbet.py` and `tg/*.py`. This release consolidates them, fixes two real bugs in the TG bot that only existed because the two engines evolved separately, and adds one small parity gain in `/benchmark`.

**Shared helpers** (new modules, both engines import them):

- `core/net_helpers.py`:
  - `validate_proxy(url)` — accepted schemes `http/https/socks5`, broader off-value vocabulary than TG's previous inline check.
  - `mask_proxy(url)` — strips userinfo. **Fixes a TG bug**: TG's inline masker built `f"{p.scheme}://{p.hostname}:{p.port}"` unconditionally and leaked `"None"` when the URL had no port.
  - `classify_auto_stop_reason(text)` — returns `"max_nonce" / "expired" / ""`. **Fixes a TG bug**: TG's inline check matched `session/expired/not exists/betsession/no query results` but not `"nonce"`. Every "Max nonce achieved" response fell through to the generic "Bet request failed" branch and `_recover_expired_session` never fired. Same dashboard-spam bug the CLI shipped a fix for at v2.25.0.
  - `parse_streak_delay("N:X")` — byte-for-byte identical logic that lived in both engines.

- `core/diagnostics.py`:
  - `percentile(sorted_ms, pct)` — used by both engines' latency reporting.
  - `benchmark_verdict(*, p50, p99, statuses, sample_count)` — returns `(colour, message)` so each engine renders as it prefers (CLI Rich markup, TG Markdown). Same five-tier logic (429 → rate-limited → network-bottleneck → tail-latency → fast → OK) now shared.

**CLI migration** (`wolfbet.py`):

- Deleted five local defs: `_validate_proxy`, `_mask_proxy`, `_classify_auto_stop_reason`, `_parse_streak_delay`, `_percentile`.
- Kept the underscore-prefixed names as re-export aliases so every v2.25.x-v2.27.x call site and test keeps working without edits.
- `cmd_benchmark` uses `benchmark_verdict` — one call replaces the five-branch if/elif ladder.

**TG migration** (`tg/handlers.py`, `tg/engine.py`):

- `tg/handlers.py`: `/config` uses `mask_proxy` (port-less URLs no longer leak `None`); `/set proxy=…` uses `validate_proxy` (broader off-value vocabulary — `off/none/direct/0/false/disable/no`).
- `tg/handlers.py` `/benchmark`: gains `p50`, `p99`, and a plain-English verdict line at the bottom of the results.
- `tg/engine.py`: `_parse_streak_delay` staticmethod becomes a thin delegator to the shared function (all `self._parse_streak_delay(...)` call sites keep working). Session-expired branch uses `classify_auto_stop_reason` — **TG bot now recovers from max-nonce automatically** (parity gain, no new TG-side code). Status-file p50 uses `percentile(sorted_samples, 0.5)`.

**Zero user-visible CLI change** beyond a version bump — the CLI already had every helper this ships. TG bot gains max-nonce recovery + a proxy-masking fix + a `/benchmark` verdict.

**Not touched**:

- `_recover_expired_session` — same 3-step shape both engines have, but different transports (`requests`/`_http_session` vs `httpx.AsyncClient`) and CLI has extras TG lacks (rate-backoff settle-sleep, `consecutive_recovery_fails`). Extraction needs a callback interface; separate phase.
- Async bet-writer / `AsyncBetWriter` — CLI has the thread pattern (v2.27.0); TG uses `asyncio.to_thread(_flush_bets)`. Different lifecycles; defer.

**Tests**: `tests/test_phase5k_core_helpers.py` — 32 tests covering the six helpers directly, plus re-export sanity checks against `wolfbet.py` and source-string checks confirming both `tg/engine.py` and `tg/handlers.py` import from `core.*`. Existing 378 tests all still pass because CLI re-exports preserve every name. **410 total passing.**

`VERSION` `2.27.0` → `2.28.0`. `tg/__init__.py:VERSION` `1.16.0` → `1.17.0` (TG max-nonce recovery is user-visible). `wolfctl` unchanged (v3.5).

## CLI v2.27.0 — async bet-writer + SQLite tuning (kill the CLI overhead) (2026-07-18)

v2.26.1's `--diagnose` proved the ~67 ms per-bet CLI overhead on the new server was disk-bound (fresh SQLite connection + per-bet fsync in `_db_save_bet`). This release moves bet writes off the hot path.

- **New `BotState.db_write_mode: str = "async"`** — default. `"sync"` for opt-out (legacy path preserved).

- **Async bet-writer thread** (`_bet_writer_worker`):
  - Persistent tuned SQLite connection (`_open_bet_writer_conn`): `PRAGMA synchronous = NORMAL`, `busy_timeout = 5000`, `cache_size = -32000` (32 MB), `temp_store = MEMORY`. Applied to the writer connection only — read connections are untouched.
  - Reads from a bounded `queue.Queue(maxsize=1000)`; batches up to 32 rows or 100 ms window; one `executemany` + one `commit()` per batch.
  - Reopens the connection on any DB error; drops the failed batch (bets are ephemeral; session aggregates preserved).
  - Started at daemon boot when `db_write_mode == "async"`. Drained cleanly at shutdown (SIGINT / SIGTERM / normal exit / monitor path) with a 5 s cap.

- **Dispatcher `_db_save_bet_dispatch`** — one entry point for both `betting_loop` and pipelined `_apply_outcome_locked`. Routes to `_db_save_bet_async` when async is on and the writer thread is running; otherwise falls back to the legacy sync `_db_save_bet`. **Nothing-lost semantics**: if the queue is full, sync fallback fires inline and increments `db_writer_stats["fallback_sync"]`.

- **`_make_bet_row`** — extracted from the sync path so both writers build the same 10-tuple. DRY.

- **Config plumbing**: `_CONFIG_KEYS`, `_PRESET_KEYS`, `_TWEAK_ALIAS` (`dbmode`, `dbwritemode`), `_TWEAK_PARAMS`, `_apply_mutation` (rejects anything but `async` / `sync`), `_CONFIG_SHOW_FIELDS`.

- **State file gains** `db_write_mode`, `db_writer_stats`, `db_writer_queue`.

- **`--diagnose` extension**: new "DB writer" line — `mode=async  queue=K/1000  batches=M  flushed=F  fallback_sync=X  hwm=W`. `fallback_sync > 0` → advice "queue overflowed; async writer is saturating". If `overhead_ms > 30` AND `db_write_mode == "sync"` → advice "flip to async: `wolfctl config-set dbmode=async && wolfctl restart`".

- **Not touching** `shared/core/database.py` — those PRAGMAs stay conservative for the multi-bot ecosystem. Wolfbet-specific tuning lives local in `_open_bet_writer_conn`.

**Tests**: `tests/test_phase5j_async_writer.py` — 18 tests using a tmpdir DB (defaults, config membership, alias, mutation with sync/async/reject-other, PRAGMAs applied on writer connection, single-row and 200-row end-to-end flush, queue-overflow triggers `fallback_sync` + row still lands via sync path, shutdown drain preserves all queued rows, dispatcher routing under three combinations — sync mode, async with writer running, async with writer NOT started falls back). **378 total passing.**

**Zero behavior changes** to the bet-loop control flow. `wolfctl` unchanged at v3.5 (no new subcommand — mode is a `config-set` knob).

`VERSION` `2.26.1` → `2.27.0` (minor — real perf feature). Expected effect on the user's new server: overhead drops from ~67 ms toward ~10 ms/bet; BPS climbs closer to the ~15 BPS network ceiling.

## CLI v2.26.1 — BPS diagnostic tools (`--benchmark`, `--diagnose`, latency-aware `--status`) (2026-07-18)

Answers the real-world question "is it WolfBet or is it my CLI?" — on a new server the user was seeing only ~7 BPS on `limbo-auto`/Martingale despite the old server hitting ~28 BPS. Same code, same account, same config — only the network path changed. Zero diagnostic tools existed on the CLI to prove where the ceiling was coming from.

- **`--benchmark [N]`** (`wolfctl benchmark [N]`): standalone API-latency probe. Fires N × `GET /v1/user/balances` through `_http_session` (same transport as production; proxy + User-Agent respected). Default N=20, clamp `[3, 200]`. **No bets placed, no DB touch, no session started.** Reports min / p50 / mean / p99 / max ms plus theoretical max BPS from p50 plus a plain-English verdict:
  - `p50 ≥ 100 ms`: "Network bottleneck — WolfBet response is the limit."
  - Any `429`: "Rate-limited — WolfBet is throttling this IP."
  - `p99 > 3·p50`: "Tail-latency spikes — unstable network path."
  - `p50 < 30 ms`: "Network fast — bottleneck is elsewhere; try `--diagnose`."
  - Otherwise: "Network OK."

- **`--diagnose`** (`wolfctl diagnose`): one-shot analysis of the running daemon. Reads the live-state file (`~/.wolfbet_autobot_live.json`), picks the top blocker deterministically (Network / CLI overhead / User-configured delay), prints a Rich panel with the cycle breakdown (`api_p50 + overhead + delay = cycle_ms → BPS ceiling`), rate-limit headroom, auto-start quota, and targeted advice (run `--benchmark`, drop `bet_delay`, try `pipeline` if strategy is Flat). Read-only; no side effects.

- **`--status` extension**: `cmd_status` (and thus `wolfctl status`) now surfaces the observability rows that already existed in the state file but were never printed:
  - `API` — `p50 Nms · p99 Nms · peak Nms`
  - `Cycle` — `Nms/bet (overhead Nms) → M.MM BPS ceiling`
  - `Efficiency` — `N% of theoretical`
  - `Pipeline` — `depth=N · dispatched=M` (when active)

- **State file gains** `api_p99_ms`, `pipeline_effective`, `pipeline_dispatched` — so `cmd_status` and `cmd_diagnose` read the same JSON.

- **wolfctl v3.4 → v3.5**: new `benchmark` and `diagnose` subcommands added to the TOOLS group of `usage()`.

- **`--help` epilog**: new example lines under Inspection group.

**Zero behavior changes to the bet loop.** Pure observability.

**Tests**: `tests/test_phase5i_diagnostics.py` — 15 tests (percentile edge cases, N clamping, verdict tier selection under mocked latencies, missing-state-file handling, top-blocker deterministic selection under three fixture states, state-file field inventory). Extended `test_phase5e_wolfctl.py` with 2 checks (v3.5 label, `benchmark` + `diagnose` dispatch). **360 total passing.**

`VERSION` `2.26.0` → `2.26.1`. `wolfctl` `v3.4` → `v3.5`.

## CLI v2.26.0 — proxy support + retention (`--cleanup`) + wolfctl HTTPS (2026-07-18)

Closes the last two deferred TG↔CLI parity items from the v2.25.3 audit and fixes a small operational hazard in `wolfctl update`.

- **Proxy support (A)**:
  - `BotState.proxy: Optional[str] = None` — full URL. `None` = direct.
  - `_validate_proxy(url)` — accepts `http://`, `https://`, `socks5://`; `off/none/direct/0/'' ` → `None`; raises `ValueError` on any other scheme. Mirrors `tg/handlers.py:569-581`.
  - `_mask_proxy(url)` — `urlparse` → `scheme://host:port`; strips userinfo. Used everywhere proxy is displayed / logged. Mirrors `tg/handlers.py:530-540`.
  - `_http_session: requests.Session` — new module-level shared session with `User-Agent: wolfbet-cli/<VERSION>` and (optional) proxies. **Every** wolfbet.com call (5 sites: `api_get_balances`, `_rotate_server_seed`, `api_auto_start`, `api_auto_stop`, `api_place_bet`) now goes through it. Zero bare `requests.get/post` calls remain.
  - `_apply_proxy_to_session()` — sets `_http_session.proxies`. Called from `load_config` (on boot) and `_apply_mutation` (on tweak).
  - `_apply_mutation` logs the **masked** URL, never the raw credentials.
  - `_check_risky_config` warns non-blockingly when `proxy` scheme is `http://` (cleartext credentials on the wire).
  - **SOCKS5 without `requests[socks]`**: `_validate_proxy` accepts the URL; the actual `requests` call will surface the missing-dep error to the user. Kept as an optional install to avoid inflating the base requirement set.

- **Retention (B)**:
  - `BotState.purge_days: int = 3` — bet-row retention window, clamped `[1, 30]`. Alias `purgedays`.
  - `_retention_worker()` — background thread, sleeps 3600 s, calls `cleanup_live_bets(DB_PATH, state.purge_days)`. Mirrors `tg/engine.py:747-758`.
  - **Startup call** — `cleanup_old_bets(DB_PATH, state.purge_days)` right after `init_db()`, matching `tg/engine.py:979`. First time the CLI daemon boots, old bet rows from long-ended sessions are purged; session aggregates preserved.
  - New `--cleanup [DAYS]` flag (arg optional; defaults to `state.purge_days`). Dispatches to `cmd_cleanup(days)` which prints a Rich summary of rows deleted.
  - New `wolfctl cleanup [DAYS]` subcommand.

- **`wolfctl` (C)**:
  - Version `v3.3` → `v3.4`.
  - New `cmd_cleanup [DAYS]` + dispatch + usage() row (DATA group).
  - `git+ssh://git@github.com/snad1/casino-shared.git` → `git+https://github.com/snad1/casino-shared.git`. No SSH key needed for a public repo; no interactive host-key prompt on fresh hosts.

- **`--help` epilog (D)**:
  - New example lines for `--config-set proxy=https://…`, `--config-set purgedays=7`, and `--cleanup 7`.

**Tests**: `tests/test_phase5g_proxy.py` (25 tests — defaults, `_validate_proxy` scheme matrix, off-value coverage, `_mask_proxy` credential stripping, `_apply_mutation` state+session sync, `_check_risky_config` `http://` warning, source-scan for zero bare `requests.*` calls, ≥5 `_http_session.*` sites); `tests/test_phase5h_purge_days.py` (14 tests — defaults, membership, alias, clamp edges, `cmd_cleanup` delegates to `cleanup_old_bets` with expected args); extended `tests/test_phase5e_wolfctl.py` (3 new — v3.4 label, `cmd_cleanup` presence, `git+https://` casino-shared URL). **343 total passing.**

`VERSION` `2.25.3` → `2.26.0` (minor — real new features). `wolfctl` `v3.3` → `v3.4`.

## CLI v2.25.3 — `recurring_delay` (TG↔CLI parity) + api_key SET/NOT SET (2026-07-18)

Closes a real CLI ↔ TG parity gap surfaced by the user's config-show audit: `recurring_delay` existed only in `tg/handlers.py` (default 60 s, floor 5 s). The CLI's `_reset_session_for_recurring` dropped straight into the next cycle with no pause — cycles were back-to-back.

- **New**: `BotState.recurring_delay: float = 60.0` — seconds to pause between one recurring cycle ending and the next starting. `0` allows back-to-back; clamped to `[0, 3600]`.
- **Sleep site**: top of `_reset_session_for_recurring` — dashboard shows `"Recurring pause 60s before next cycle…"` for the pause window; logged at INFO. Covers both `betting_loop` and `pipelined_betting_loop` since both call the same reset function.
- **Full plumbing**:
  - `_CONFIG_KEYS` + `_PRESET_KEYS`: `recurring_delay` round-trips through JSON.
  - `_TWEAK_ALIAS`: `recdelay` and `recurringdelay` both resolve to `recurring_delay` (TG alias parity).
  - `_TWEAK_PARAMS`: mutable via the `T` hotkey, `wolfctl config-set`, `--config-set`.
  - `_apply_mutation`: parses, clamps to `[0, 3600]`, `off` / `0` disables.
  - `_CONFIG_SHOW_FIELDS`: new row in `wolfctl config-show` / `--config-show`.
  - Wizard step 5b: new prompt after the `recurring` yes/no.
  - `_HELP_EPILOG`: `--config-set recurring=on recdelay=90 maxbets=1000` example.
- **`_check_risky_config`**: extended "Recurring runaway" warning to include the pause length (`"delay 60s between cycles"`) or `"back-to-back (no delay)"` when `recurring_delay=0`.
- **`cmd_config_show`**: `api_key` row now shows `(SET)` (bold green) or `(NOT SET)` (bold red) after the masked value — parity with TG `/config` Yes/No indicator.

**Deferred (out of scope, documented in audit)**:

- `purge_days` — TG-only DB retention config. CLI would need a `--cleanup` command; separate phase.
- `proxy` — TG-only HTTP routing. CLI would need HTTP-client rework; separate phase.

**Tests**: `tests/test_phase5f_recurring_delay.py` — 20 tests (default value, membership across all config surfaces, mutation clamps, alias resolution, sleep behavior with mocked `time.sleep`, dashboard smoke via `cmd_config_show` source-string check, risky-config warning phrasing). **299 total passing.**

`VERSION` bumped `2.25.2` → `2.25.3`. `wolfctl` stays at v3.3 (same public surface).

## CLI v2.25.2 — `wolfctl update` survives a dirty working tree (2026-07-18)

**Fixes real-world blocker** on the production server:

```
error: Your local changes to the following files would be overwritten by merge:
        install.sh
Please commit your changes or stash them before you merge.
```

`cmd_update` at `wolfctl:262` was running a bare `git pull --ff-only`. Any tracked-file local change on a file touched by the incoming commit aborted the update — leaving the user stuck without a wrapper-level recovery path.

Fix: extend `cmd_update`'s git-pull block:

- **Dirty-tree preflight** — `git diff --quiet` + `git diff --cached --quiet` detects local changes. When dirty, prints `"Local changes detected — autostashing before pull:"` followed by `git status --porcelain` so the user sees exactly what will be stashed.
- **`git pull --ff-only --autostash`** — git ≥ 2.9 stashes local changes, does the pull, then pops the stash. Pop-conflicts leave the stash in place for manual recovery.
- **Failure hint** — on pull failure the wrapper now prints `Recover with: git stash list  |  git stash pop` so the user knows exactly where their edits went.

Zero destructive fallbacks (no `git reset --hard`, no `git checkout --`). A wrapper must never silently discard user work.

**Tests**: three new grep-based checks in `tests/test_phase5e_wolfctl.py` guarantee the `--autostash` flag, porcelain print block, and recovery hint don't silently regress. 279 total passing.

Zero Python code changes. `VERSION` bumped `2.25.1` → `2.25.2`. `wolfctl` header stays at v3.3 (same public surface — one-line bug fix to one function).

## CLI v2.25.1 — wolfctl refresh (subcommand parity + install.sh drift fix) (2026-07-18)

**Fixes real-world discoverability gap** — `wolfctl --help` was still labelled v3.1 on production servers and didn't expose any of the flags shipped after v2.19 (`--analytics`, `--backtest`, `--export`, `--pipeline`, `--config-show`, `--config-set`, `--version`, `--last-bets`, `--preset`). Two drift sources fixed at once:

- **wolfctl v3.2 → v3.3**: nine new subcommands mapping 1:1 to CLI flags:
  - `wolfctl config-show` → `--config-show`
  - `wolfctl config-set KEY=VALUE …` → `--config-set KEY=VALUE …`
  - `wolfctl analytics` → `--analytics`
  - `wolfctl backtest [args…]` → `--backtest [args…]`
  - `wolfctl export PATH [SID]` → `--export PATH [--export-session-id SID]`
  - `wolfctl last-bets N` → `--last-bets N`
  - `wolfctl preset NAME` → `--preset NAME --daemon`
  - `wolfctl pipeline N` → `--config-set pipeline_depth=N` (convenience alias)
  - `wolfctl version` → `--version` + prints wolfctl's own v3.3
- **`usage()` refresh** — new `CONFIG` and `TOOLS` groups; 26+ subcommand rows (was 20).
- **Argument validation** — each new subcommand prints a one-line `Usage:` on missing args and exits 2. Matches existing `cmd_session` / `cmd_tg` pattern.
- **`help` / `--help` / `-h` / `--version` / `-v` aliases** — added to dispatch.

**install.sh drift fix**: the previous version inlined a 350-line heredoc of a stale v3.1 wolfctl — fresh installs got the old copy regardless of what the repo shipped. Replaced with `install -m 0755 "$SCRIPT_DIR/wolfctl" "$WOLFCTL"`. One source of truth: install.sh + `git pull` + `wolfctl update` all converge on the same script.

**Tests**: `tests/test_phase5e_wolfctl.py` — 16 tests (string presence + `bash wolfctl` subprocess smoke). Validates every new `cmd_*` / dispatch case / flag reference, `usage()` row count, install.sh drift removal + `install -m 0755` presence, bash syntax valid. **276 total passing.**

**Zero Python code changes.** `VERSION` bumped `2.25.0` → `2.25.1` (docs / wrapper / installer only).

## CLI v2.25.0 — max-nonce recovery + non-interactive config + monitor stats (2026-07-18)

**Fixes real-world dashboard spam** — when the WolfBet server-side auto-bet cycle hit `Max nonce achieved` (server seed nonce exhausted), the CLI kept re-calling `/auto/start` every 2 s with the same seed, one new dashboard row per attempt. Recovery function `_recover_expired_session` existed but was dead code — now wired in.

- **Recovery (A)**:
  - New `_classify_auto_stop_reason(text)` returns `"max_nonce"` / `"expired"` / `""`. Called at 3 sites: `api_place_bet` 200-body error branch, `api_place_bet` `stopReason` branch, `api_place_bet` 400 branch, plus `api_auto_start` non-200 body.
  - `BotState.last_stop_reason` + `BotState.consecutive_recovery_fails` track recovery state.
  - `betting_loop()` now routes to `_recover_expired_session` (rotate seed → auto-stop → wait → auto-start) when `last_stop_reason` is set; otherwise plain `api_auto_start` for fresh startup.
  - **Circuit breaker**: 3 consecutive recovery failures → hard-stop with a clear message.
  - **Same-CLI-session guarantee**: recovery never touches `session_id` / `total_bets` / DB rows. One CLI session spans many WolfBet server-side auto-cycles.
  - Successful bet or `api_auto_start` clears `last_stop_reason` + `consecutive_recovery_fails`.

- **Non-interactive config (C)**:
  - New `--config-show` — dumps saved config as a Rich table (Field / Value / Description). API key masked. Reports file path + mode.
  - New `--config-set KEY=VALUE [KEY=VALUE …]` — mutate one or more fields without the wizard. Uses `_TWEAK_ALIAS` (same grammar as `T` hotkey / TG `/tweak`). Prints diff-style OLD → NEW per field. Refuses `api_key=…` (env var only). Refuses unknown fields.
  - New `--version` flag.

- **Rich `--help` (B)**:
  - `RawDescriptionHelpFormatter` + multi-line description + `Examples:` epilog grouped by task (Daemon control / Config inspection / Config mutation / Data / Backtest / Concurrency).

- **Monitor stats (D)**:
  - Row 1 (BAL / PnL / WAG / WR) now also shows **ROI %** (`profit / wagered × 100`), green if positive, red if negative.
  - API row now includes **p50** + **p99** unconditionally (previously hidden unless `bet_delay > 0`).
  - **Efficiency + Cycle** rows always shown (previously delay-gated).
  - Rate-limit row's `/auto/start` quota renamed `K/20 auto-start`; turns bold-red at ≤ 3 (near the WolfBet 20-per-window cap).
  - New `_compute_api_p99()`.

- **Tests**: `tests/test_phase5c_recovery.py` (18 tests — classify matrix, recovery success/failure, circuit-breaker counter, same-session guarantee, dashboard smoke, p99); `tests/test_phase5d_config_cli.py` (21 tests — parse-kv + aliases + rejections, subprocess end-to-end for `--config-show`, `--config-set`, `--help`, `--version`, api-key refusal, unknown-key refusal, malformed-pair refusal). **260 total passing.**

## CLI v2.24.0 — pipelined betting (`--pipeline N`, Flat-only) (2026-07-17)

- **New**: `--pipeline N` CLI flag + `pipeline_depth` state field. Concurrent in-flight bets via `ThreadPoolExecutor(N)` + `threading.Semaphore(N)`.
- **Guardrails** — mirror `tg/engine.py:1373` `_pipelined_betting_loop`:
  - Non-Flat strategies (Martingale / Anti-M / D'Alembert / Paroli / WolfRider / Delay-M / Rule-Based) → forced to depth 1 (outcome ordering matters).
  - Auto-bet games (`dice-auto` / `limbo-auto`) → forced to depth 1 (server-side single-flight).
  - Depth hard-capped at 8.
- **Dispatcher**: new `_run_betting_loop()` picks between the unchanged `betting_loop()` (depth 1, default) and new `pipelined_betting_loop(depth)`. Zero regression when depth=1.
- **Locking**: new `_apply_outcome_locked()` wraps every per-bet counter mutation, DB save, and metric update in `state.lock`. `_prepare_next_bet_flat()` snapshots the payload before dispatch.
- **Rate-limit coordination**: pipeline drains all in-flight workers before honoring an exponential-probe cooldown, so no worker dispatches into the wait window.
- **Wizard step 5b**: new "Pipeline depth" prompt (default 1 = off).
- **Tweak grammar**: `T pipeline N` (canonical `pipeline_depth`) — clamps to [1, 8], `off` resets to 1.
- **Preset + config round-trip**: `pipeline_depth` persists via both `_CONFIG_KEYS` and `_PRESET_KEYS` (older presets without the field just default to 1).
- **Dashboard**: new "Pipeline" row shows `depth=N · in-flight=K · dispatched=M` when active, or the coerce-reason when the user requested pipelining but the guardrail refused.
- **Safety warnings**: new categories added to `_check_risky_config()` — pipeline+Martingale / pipeline+auto-game.
- **Tests**: `tests/test_phase5b_pipeline.py` — 25 tests covering guardrail matrix (Flat / Martingale / D'Alembert / Rule-Based / dice-auto / limbo-auto), payload snapshot, apply-outcome integrity (win / loss / streak / None result), 500-thread concurrent counter integrity, tweak grammar, clamps, config + preset key membership, risky-config warnings.
- **Backward compat**: `--pipeline` omitted or `1` → byte-equivalent to v2.23.1 behavior.

## CLI v2.23.1 — README documentation refresh (docs only) (2026-07-17)

- **Docs**: full `README.md` rewrite (`v2.11.0` → `v2.23.1` header). New sections: Live-session hotkeys, Runtime tweak grammar (22 params), Advanced session tuning, Observability metrics, Session summary + exports, Backtest mode, Lifetime analytics, Safety warnings, File locations, Development / testing, Version history. CLI Options table rebuilt (grouped by daemon control / inspection / export / backtest).
- **No code changes.** `VERSION` bumped `2.23.0` → `2.23.1` + banner literal. 196 regression tests still pass.

## CLI v2.23.0 — lifetime analytics screen (`--analytics`) (2026-07-17)

New user-visible command: **`python wolfbet.py --analytics`** — aggregates every saved session into a lifetime-performance report. Complement to `--stats` (which shows the most recent 20 sessions individually).

### Added

- **`--analytics` command flag** — mutually exclusive with `--status` / `--stop` / `--list-presets` / `--stats` / `--last-bets` / `--session-bets`. No arguments needed.
- **`cmd_analytics()`** — renders:
  - Header rule with total counts (sessions, bets, wagered).
  - **Overall** panel: total sessions, sessions in profit vs loss, total bets W/L, win rate, total wagered, total P/L, best & worst session profit.
  - **By game** table: sessions, bets, win rate, P/L, wagered per game — ordered by profit.
  - **By strategy** table: same columns per strategy.
  - **Top 5 best sessions** — profit-descending.
  - **Top 5 worst sessions** — profit-ascending.
  - Footer tip pointing at `--stats` and `--session-bets ID` for the drill-down views.
- **Pure helpers** (testable in isolation):
  - `_analytics_lifetime() -> dict` — top-line SQL aggregates with all-zero defaults for an empty DB.
  - `_analytics_by_game() -> list[dict]` — SQL `GROUP BY game`.
  - `_analytics_by_strategy() -> list[dict]` — SQL `GROUP BY strategy`.
  - `_analytics_top_sessions(order, limit) -> list[dict]` — whitelisted order key (`profit_desc` / `profit_asc` / `bets_desc`), safe against SQL injection.
- **`_print_analytics_table` / `_print_top_sessions_table`** — small render helpers, keep `cmd_analytics` readable.

### Behavioural notes

- **Read-only over the local DB.** Zero writes; no risk to session history / lifetime totals.
- **Empty DB is graceful.** `No sessions in the DB yet — run a session first.` message; clean exit code 0.
- **Complement to `--stats`, not a replacement.** `--stats` still shows the most recent 20 sessions in detail.
- **No new dependencies** — all stdlib + Rich (already imported).
- **No new secrets, no auth changes, no PII.**

### Testing

- New file `wolfbet/tests/test_phase8_analytics.py` — 18 tests seeding a temp DB with 3 sessions across 2 games and 2 strategies, then validating: lifetime totals match hand-computed values, win rate percent, `sessions_in_profit` / `_in_loss` counts, best/worst profit, `by_game` grouping + profit-desc ordering + aggregate columns, `by_strategy` correctness, `top_sessions` `profit_desc` / `profit_asc` / `bets_desc` / unknown-order fallback / limit-respected, empty-DB edge case, `cmd_analytics` no-crash on both empty and populated DBs.
- Combined suite: **196 tests** (29 Phase 2 + 22 Phase 3 + 9 Phase 4a + 34 Phase 4b + 15 Phase 5a + 14 Phase 5c + 35 Phase 6 + 20 polish + 18 analytics) — all pass in `.venv` in < 300 ms.
- Run: `cd wolfbet && WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q` → `196 passed`.

### Version

- `wolfbet.py:VERSION` `2.22.1` → `2.23.0` (minor bump — new user-visible command).
- Banner literal updated to match.

## CLI v2.22.1 — safety-check warnings + wizard polish (2026-07-17)

Small polish patch. Catches obvious foot-guns before a session starts. Non-blocking heads-up — users retain full control.

### Added

- **`_check_risky_config()`** — new module-level pure function inspecting `state.*` for combinations known to drain balance quickly. Returns a list of heads-up strings (empty = clean). Covers eight categories:
  - Aggressive Martingale (`loss_mult >= 2.5`) — flags the reserves needed for a 5-loss streak.
  - Doubling strategies (`strategy_key in ("2", "7")`) without `max_loss`.
  - Anti-Martingale without `max_profit`.
  - `base_bet > 1%` of current balance.
  - `multiplier > 10` (win chance < 10%).
  - Recurring restart with no `max_profit` and no `max_bets`.
  - Custom rules (Strategy 8) without `max_bets`.
  - Aggressive `basebet_pct >= 5%`.
- **Wizard `step_confirm` warnings panel** — before the final "Start auto-betting?" prompt, if `_check_risky_config()` returns non-empty, print a bordered yellow panel titled `⚠️  Heads-up — review before starting` with a bulleted list. One-liner underneath: "These are safety hints, not blockers. Press y to proceed anyway." Clean configs skip the panel entirely.

### Improved

- **`--export` missing-session error** now includes a hint: `Tip: run 'python wolfbet.py --stats' to see recent session IDs.`
- **`--backtest` argparse help** now shows compact examples: `--simulate 500 --seed 42`, `--replay-session 42`, `--simulate 10000 --preset aggressive`.

### Behavioural notes

- **Non-blocking.** Every warning is a heads-up — users can always proceed.
- **Zero risk to bet loop / strategy engine / HTTP surfaces.** Wizard-exit UI only.
- **No new secrets, no auth changes, no PII.**
- **Read-only state inspection** — the check function never mutates state or DB.

### Testing

- New file `wolfbet/tests/test_phase7_polish.py` — 20 tests covering: clean config → no warnings; each of the 8 warning categories triggers correctly; edge cases (`loss_mult=2.0` doesn't trigger aggressive Martingale, `multiplier=10.0` doesn't trigger high-multiplier, etc.); combined config triggers multiple simultaneous warnings.
- Combined suite: **178 tests** (29 Phase 2 + 22 Phase 3 + 9 Phase 4a + 34 Phase 4b + 15 Phase 5a + 14 Phase 5c + 35 Phase 6 + 20 polish) — all pass in `.venv` in < 300 ms.
- Run: `cd wolfbet && WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q` → `178 passed`.

### Version

- `wolfbet.py:VERSION` `2.22.0` → `2.22.1` (patch bump — additive UX, no schema change).
- Banner literal updated to match.

## TG v1.16.0 + CLI v2.22.0 — shared registry refactor: core/games.py (2026-07-17)

Phase 6 of the CLI ↔ TG parity roadmap — the last housekeeping phase. Every WolfBet endpoint, hash spec, and strategy-translation function that used to live in **both** `tg/engine.py` **and** `wolfbet.py` now lives once in `wolfbet/core/games.py`. Both engines import from one truth.

### Refactored

- **New module** `wolfbet/core/games.py` — single source of truth:
  - `GAME_ENDPOINTS`, `AUTO_ENDPOINTS`, `GAME_LABELS`, `AUTO_START_MIN_INTERVAL`
  - `wolfbet_hash(body_str)` — dynamic `md5(body) ⊕ md5(nonce)` header
  - `is_auto_game(game_key)` — game_key ∈ AUTO_ENDPOINTS
  - `fmt_multiplier(m)` — WolfBet multiplier format (no trailing zeros)
  - `request_hash_header(game_key, body_str)` — resolves the hash_spec sentinel (`None` / `"dynamic"` / verbatim string) for a game
  - `custom_rule_to_wolfbet(rule)` — client `StrategyRule` → server auto-bet rule dict
  - `build_auto_config(strategy_key, loss_mult, win_mult, base_bet, game, custom_rules, stop_conditions, delay_martin_threshold, logger, log_prefix)` — pure parameterised replacement for the old `self._build_auto_config`
  - `build_auto_start_payload(game, currency, base_bet, multiplier, rule, bet_value, config)` — pure parameterised builder
- **TG engine** (`tg/engine.py`): local `GAME_ENDPOINTS` / `AUTO_ENDPOINTS` / `GAME_LABELS` / `AUTO_START_MIN_INTERVAL` / `_wolfbet_hash` deleted; `_fmt_mult` / `_custom_rule_to_wolfbet` methods deleted; `_is_auto_game(self)` / `_build_auto_config(self)` / `_build_auto_start_payload(self)` shrunk to thin `self.* → shared function` adapters. `_request_headers` now delegates hash resolution to `request_hash_header`. **No behavioural change** on the wire.
- **CLI** (`wolfbet.py`): identical treatment. `_headers(game_key, body_str)` delegates to `request_hash_header`. The legacy hardcoded static `x-hash-api` strings for `dice-manual` / `limbo` are unified with the TG behaviour (`"dynamic"` — the sentinel that computes per-request). TG has been using dynamic hash for these endpoints successfully for months, so this brings the CLI into alignment.

### Behavioural notes

- **Zero user-visible behaviour change.** Every existing test still passes; every existing session config still runs.
- **No secrets, no auth changes, no PII.** Same endpoints, same `x-hash-api` algorithm, same auth pattern.
- **Wolfbet-repo-local**, NOT part of the pip-installed `shared/` package. Rationale: the other 5 bots (stake / cryptogames / duckdice / shuffle / goated) don't hit WolfBet endpoints; giving them a `wolfbet_games.py` dependency would be architecturally wrong.
- **`core/games.py` imports `StrategyRule` from `core.strategy`** (which is shared) — no circular dependency, no cross-repo coupling changes.
- **Adding a new game** (e.g. a `plinko-auto`) is now a **single-file edit** — both engines pick it up automatically on next start.

### Testing

- New file `wolfbet/tests/test_phase6_games.py` — 35 tests covering every branch of the shared module: registry shape, `wolfbet_hash` length/hex validity, `is_auto_game` positive/negative, `fmt_multiplier` for 6 inputs, `request_hash_header` for 3 game classes, `custom_rule_to_wolfbet` for 5 rule types, `build_auto_config` for all 8 strategy branches + unknown-default, `build_auto_start_payload` for both auto games (with/without rule + bet_value).
- Combined suite: **158 tests** (29 Phase 2 + 22 Phase 3 + 9 Phase 4a + 34 Phase 4b + 15 Phase 5a + 14 Phase 5c + 35 Phase 6) — all pass in `.venv` in < 300 ms.
- Run: `cd wolfbet && WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q` → `158 passed`.
- TG side has no test suite; TG deploy smoke on the server via `*ctl update` + `/status` → `/tweak delay 0.1` → auto-bet session start.

### Versions

- TG: `tg/__init__.py:VERSION` `1.15.10` → `1.16.0` (minor bump — internal refactor).
- CLI: `wolfbet.py:VERSION` `2.21.0` → `2.22.0`. Banner literal updated.

## CLI v2.21.0 — backtest / replay mode (no API calls, no real balance risk) (2026-07-17)

Phase 5c of the CLI ↔ TG parity roadmap. Second "beyond TG" release — run any strategy configuration against saved sessions (historical replay) or a synthetic random stream (Monte Carlo simulation) without touching the WolfBet API.

### Added

- **`--backtest` mode.** Skips the API-key wizard, all HTTP calls, all rate-limit code. Loads config from `--preset NAME` OR `~/.wolfbet_autobot.json` OR sensible defaults (Martingale / 2.01x / over / 49.5). Runs the strategy over a synthetic outcome stream; ends with the same rich session summary panel from v2.20.0.
- **`--simulate N`** — data source: N synthetic bet outcomes generated from the multiplier's win probability (`win_chance = 99 / multiplier`, matches WolfBet's 99% RTP). Default 1000.
- **`--seed N`** — RNG seed for `--simulate`. `random.Random(seed)` gives reproducible runs.
- **`--replay-session ID`** — data source: replay bets from a previously-saved session #ID. Historical rolls (win/loss + result_value) drive the outcomes; the CURRENT strategy's bet-size decisions drive the amount. Answers "what if I'd used strategy X on last night's sequence?"

### Helpers

- `_simulate_bet_outcome(multiplier, rule, bet_amount, current_balance, rng)` — pure function returning a bet-result dict shaped like `api_place_bet`'s response.
- `_replay_bets_from_db(session_id, bet_amount_fn)` — generator yielding bet-result dicts from a saved session.
- `_apply_backtest_result(result)` — mutates `state.*` from one result. Reused by `backtest_loop`; parallel to `betting_loop`'s block.
- `backtest_loop(source, progress_every=100)` — orchestrates. Honours `should_stop()` and `state.running` so stop conditions and `q` hotkey both work. Prints heartbeat progress every N bets (defaults scale with `--simulate N/20`).
- `_run_backtest(args)` — entry point wired into `main()` before the normal flow.

### Behavioural notes

- **Zero risk to real balance.** No API calls, no auth, no PII. Random uses `random.Random(seed)` (stdlib) — sufficient for statistical analysis.
- **`[BACKTEST]` prefix on `state.status`** throughout the run — impossible to confuse for a live session. Banner emits `🧪 BACKTEST MODE — no API calls, no real balance risk`.
- **Transient in v1.** Backtest doesn't write to the DB; user sees the summary panel, then exits. No pollution of real session history / lifetime stats.
- **Strategy engine is the single source of truth** for both live and simulated runs. Custom Rules (Strategy 8), streak delays, `streakbet_loss`, `basebet_pct`, milestones, `apply_rules`, `wolfrider_switch`, `compute_next_bet` — all reused verbatim via the existing hooks.
- **Fast.** No `bet_delay`, no rate-limit backoff, no HTTP. A 10k-bet simulation completes in < 1 s.

### Testing

- New file `wolfbet/tests/test_phase5c_backtest.py` — 14 tests covering: `_simulate_bet_outcome` shape / determinism / win-rate for multiple multipliers, `_replay_bets_from_db` yields correct count / bet size / raises on missing session, `_apply_backtest_result` win/loss/mixed streak tracking, `backtest_loop` runs to completion, respects `max_bets` stop condition, is deterministic with the same seed.
- Combined suite: **123 tests** (29 Phase 2 + 22 Phase 3 + 9 Phase 4a + 34 Phase 4b + 15 Phase 5a + 14 Phase 5c) — all pass in `.venv` in < 200 ms.
- Run: `cd wolfbet && WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q` → `123 passed`.

### Example usage

```
python wolfbet.py --backtest --simulate 500 --seed 42          # Monte Carlo
python wolfbet.py --backtest --replay-session 42               # Historical replay
python wolfbet.py --backtest --simulate 10000 --preset aggressive  # With a saved preset
```

### Version

- `wolfbet.py:VERSION` `2.20.0` → `2.21.0` (minor bump — new user-visible mode).
- Banner literal updated to match.

## CLI v2.20.0 — session summary on exit + CSV/JSON export (2026-07-17)

Phase 5a of the CLI ↔ TG parity roadmap — the first "beyond TG" release. Adds a rich end-of-session recap panel (native Rich, not Markdown) and full session-data export via a new CLI flag and hotkey.

### Added

- **`format_session_summary(reason)` — Rich Group of Panels.** Shown on `Q`, Ctrl+C, and SIGTERM. Green ✅ (profit ≥ 0) or red 🛑 (loss) leading emoji (matches v2.16.9 policy). Balance panel border colours from profit sign. Panels: Balance, Stats, Extremes, Config; footer with uptime + avg speed. Degrades cleanly on a zero-bet session (no `NaN`, no divide-by-zero).
- **`--export <path>` CLI flag.** Exports the latest session (or `--export-session-id N` for a specific one) to CSV or JSON — format detected from the file extension. Unknown extension defaults to CSV.
  - CSV: session metadata as leading `#` comment rows, then bet columns with a header row.
  - JSON: `{ "session": {…all session columns…}, "bets": [{…all bet columns…}] }`.
  - File permissions `0o600` (restrictive, matches other CLI-written files).
- **`E` hotkey (live-session export).** Press `E` in a live session for a modal path prompt (default `~/wolfbet-session-<id>.csv`). Follows the `show_tweak` idiom — pause / prompt / resume. `back` / blank / EOF cancels; errors surface cleanly as `✗ <error>` without crashing the loop.
- **Dashboard key legend updated** — `[P]ause [R]esume [T]weak [E]xport [H]istory [Q]uit`.

### Helpers

- `_read_session(session_id) -> dict` — sessions row as a dict via `sqlite3.Row`.
- `_read_bets(session_id) -> list[dict]` — bets ordered by id.
- `_latest_session_id() -> int` — highest session id, or 0 if the DB is empty.
- `_export_session(session_id, path) -> (bet_count, format)` — orchestrator; raises `ValueError` on missing session or write failure.
- `show_export()` — modal (mirrors `show_tweak` / `show_history`).

### Behavioural notes

- **No new external API surfaces.** No new secrets, no auth changes, no PII. Bet payloads are timestamped balances (no API keys, no auth tokens).
- **Bet loop is untouched.** All new code is display + export layer. Zero risk to the sending path.
- **Summary rendering is best-effort** — wrapped in try/except so a display bug never blocks the shutdown-save path.
- **Export uses the shared `db_connect` helper** (post-v1.15.6 `check_same_thread=False`) — no cross-thread SQLite errors when the export runs from the hotkey while the bet loop still holds its persistent connection.

### Testing

- New file `wolfbet/tests/test_phase5a_export.py` — 15 tests covering: DB seed round-trip, `_read_session` / `_read_bets` / `_latest_session_id`, format detection from `.csv` / `.CSV` / `.json` / unknown extension, CSV header + `#` metadata rows, JSON `{session, bets}` structure, file perms (`0o600`), missing-session raises `ValueError`, `format_session_summary` no-crash on both zero-bet and populated states.
- Combined suite: **109 tests** (29 Phase 2 + 22 Phase 3 + 9 Phase 4a + 34 Phase 4b + 15 Phase 5a) — all pass in `.venv` in < 200 ms.
- Run: `cd wolfbet && WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q` → `109 passed`.

### Version

- `wolfbet.py:VERSION` `2.19.0` → `2.20.0` (minor bump — new user-visible flag + hotkey + summary panel).
- Banner literal updated to match.

## CLI v2.19.0 — runtime tweak surface (parity with TG /tweak) (2026-07-17)

Phase 4b of the CLI ↔ TG parity roadmap. Full parity with `tg/handlers.cmd_tweak` — 22 tweakable params, same aliases, mid-session parameter edits without stopping the bet loop.

### Added

- **New hotkey `T` — Runtime tweak.** Press `T` during a live session to open a modal reference table + `param value` prompt (follows the same idiom as `H` for history: pause → prompt → resume). `back` / empty / `q` cancels without applying anything. Errors surface cleanly (`Invalid value: ...`, `Unknown param: ...`) and let you retry — never crash the bet loop.
- **`_apply_mutation(changes: dict) -> list[str]`** — new module-level pure mutation function. Ports the shape of `tg/engine.py:1274-1387` for CLI parity. Returns human-readable status messages ("Delay: 0.500s", "Max profit: off", …). Reuses the Phase 2 helpers: `_parse_streak_delay` decodes streak-format tweaks (`"3:0.5"` → `(3, 0.5)`); `_apply_basebet_pct` recomputes `base_bet` immediately after a `basebet_pct` tweak.
- **`_TWEAK_ALIAS` + `_TWEAK_PARAMS`** — grammar maps mirroring TG's alias set. Short forms accepted (`delay`, `sdloss`, `maxprofit`, `msbets`, `pi`, `basebet`, `lossmult`, …) and normalised to canonical state attribute names.
- **Auto-bet UUID-clear semantics.** Strategy-config changes on `dice-auto` / `limbo-auto` (base_bet, multiplier, loss_mult, win_mult) clear `state._auto_uuid`. `betting_loop`'s next iteration fires a fresh `api_auto_start()` with the new config — reuses the Phase 1 expired-session recovery path, no new engine flag.
- **`recurring` param.** Toggle Phase 2 recurring restart mid-session (`t recurring on/off`). CLI-native extension of the tweak grammar; TG doesn't have this today.
- **Dashboard key legend updated** — bottom row now reads `[P]ause [R]esume [T]weak [H]istory [Q]uit`.

### Full tweak param list

**Instant** (no restart): `delay` / `bet_delay`, `bbpct` / `basebet_pct`, `sdloss` / `sdwin` / `sdbets` (streak delays), `sbloss` (streakbet loss), `maxprofit` / `maxloss` / `maxbets` / `maxwins` / `minbalance`, `msbets` / `mswins` / `mslosses` / `msprofit` (milestones), `pi` / `pt` (profit increment / threshold), `recurring`.

**Auto-restart** (auto games clear UUID; manual games apply instantly): `basebet` / `base_bet`, `multiplier`, `lossmult` / `loss_mult`, `winmult` / `win_mult`.

Use `off` / `none` / `0` / `disable` / `false` / `no` / `n` as synonyms to clear any field.

### Behavioural notes

- **No new external API surfaces.** No new secrets, no auth changes, no PII.
- **Type validation is strict.** `float(v)` / `int(v)` errors surface as `ValueError` in the mutation function, caught by `show_tweak()` and shown as `Invalid value: ...`. The bet loop keeps running.
- **Malformed streak input** falls back to `(0, 0.0)` (disabled) via the existing `_parse_streak_delay` tolerance.
- **State mutation is lock-guarded** for the UUID-clear path; other fields are single-write assignments already safe against the bet-loop reader.

### Testing

- New file `wolfbet/tests/test_phase4b_mutation.py` — 34 pure-function tests covering every branch: instant params, streak decoding, `off` synonyms, stop conditions, milestones, recurring toggle (`on`/`off`/`true`/`false`/`yes`/`no`/`y`/`n`/`1`/`0`), strategy touches on manual vs auto games (UUID clear), type validation (`ValueError` propagation), alias resolution, `_TWEAK_ALIAS` ↔ `_TWEAK_PARAMS` consistency.
- Combined suite now: **94 tests** (29 Phase 2 + 22 Phase 3 + 9 Phase 4a + 34 Phase 4b) — all pass in `.venv` in < 200 ms.
- Run: `cd wolfbet && WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q` → `94 passed`.

### Version

- `wolfbet.py:VERSION` `2.18.0` → `2.19.0` (minor bump — new user-visible hotkey + tweak surface).
- Banner literal updated to match.

## CLI v2.18.0 — wizard prompts + preset persistence for Phase 2 fields (2026-07-17)

Phase 4a of the CLI ↔ TG parity roadmap. Clears the follow-up debt from v2.16.0 / v2.17.0.

### Added

- **New wizard step: "Step 5b / 7 — Advanced session tuning (optional)".** Placed between Stop conditions and Presets. Umbrella prompt defaults to "N" so quick-start flows are unchanged. When enabled, prompts for the ten Phase 2 config fields with the existing `_ask_optional` grammar (`Enter` keeps current, `none`/`clear`/`off`/`0` disables, `back` returns).
- **Preset schema extension.** `_PRESET_KEYS` gains six new plain fields (`recurring`, `basebet_pct`, `milestone_bets`, `milestone_wins`, `milestone_losses`, `milestone_profit`). New `_STREAK_TUPLE_KEYS` list identifies the four tuple fields (`streak_delay_loss`, `_win`, `_bets`, `streakbet_loss`) that round-trip through JSON as `"N:X"` strings — matches the format `_save_state_file` already uses for snapshots.
- **`save_preset` / `load_preset` refactored** to handle the streak-tuple encoding. Load side reuses the Phase 2 `_parse_streak_delay(value)` helper, so malformed / hand-edited JSON falls back cleanly to `(0, 0.0)` (disabled) rather than crashing.

### Behavioural notes

- **Backward-compatible.** Legacy presets (v2.17.0 era) load with defaults for the new fields — no `KeyError`, no migration script.
- **No new behaviour paths.** The fields already work as of v2.16.0; this release only adds discovery / entry surfaces.
- **Preset file permissions unchanged** — still `os.open(..., 0o600)` restrictive.
- **Malformed input safe.** Invalid streak strings (hand-edited or fat-fingered in the wizard) map to the disabled state; never crash.

### Testing

- New file `wolfbet/tests/test_phase4a_presets.py` — 9 pure-function tests covering: full round-trip of all Phase 2 fields, empty streak encoding (→ `null`), populated streak encoding (→ `"N:X"`), backward-compat with legacy presets, malformed-value fallback, JSON-native `bool`/`float` serialisation for plain fields, preset name isolation.
- Uses a `_tmp_preset_path()` context manager that swaps `wolfbet.PRESET_PATH` for a `tempfile.mkstemp` file for the duration of the test — no pollution of `~/.wolfbet_presets.json`.
- Combined suite now: **60 tests (29 Phase 2 + 22 Phase 3 + 9 Phase 4a)** — all pass in `.venv` in < 200 ms.
- Run: `cd wolfbet && WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q` → `60 passed`.

### Version

- `wolfbet.py:VERSION` `2.17.0` → `2.18.0` (minor bump — new user-visible wizard surface + preset schema extension).
- Banner literal updated to match.

## CLI v2.17.0 — observability parity (API timing, rate-limit line, Cycle, Efficiency) (2026-07-17)

Phase 3 of the CLI ↔ TG parity roadmap. Closes Section 4 "Observability" of `audits/cli-vs-tg-parity.md`.

### Added

- **API timing metrics** — every `requests.post` in `api_place_bet` + `api_auto_start` is now wall-clock-timed. Rolling counters track `last`, `avg`, `peak`, and a 500-sample deque backs the p50 median. Ports `tg/engine.py:1487-1508` math verbatim (sync-adapted).
- **`on_target %`** — fraction of bets whose API ≤ `bet_delay*1000`. Bounds the Efficiency ceiling. Renders `n/a` when `bet_delay = 0` (matches TG).
- **`cycle_ms`** — EMA-smoothed full per-bet loop time (`0.9 * old + 0.1 * new`). Derives `overhead_ms = cycle − api − delay*1000` (floored at 0).
- **`efficiency %`** — actual `bets_per_second` as a % of theoretical max (`1 / max(bet_delay, api_avg)`). Caps at 100%. Colour-coded green ≥ 90%, yellow below.
- **Dashboard rows**:
  - `API last / avg / peak` — always shown.
  - `Rate limit: N/M play [· K/20 start]` — always shown for WolfBet (the `· … start` half appears only when an auto-bet session is active).
  - `API stable: … median · … on-target (API ≤ bet delay)` — **only when `bet_delay > 0`**.
  - `Efficiency: … (max … bps @ delay=…)` — same delay-guard.
  - `Cycle: …ms/bet (overhead …ms) → … bps` — always shown.
- **Snapshot fields** — `_save_state_file()` now writes `api_ms`, `api_avg_ms`, `api_peak_ms`, `api_p50_ms`, `on_target_pct`, `cycle_ms`, `overhead_ms`, `efficiency_pct`, `theoretical_max_bps` so `--status` / `--monitor` see them.

### Behavioural notes

- **No external API surface change.** All new values are computed from data the CLI already receives; no additional HTTP calls, no new auth surface, no PII.
- **Fresh-session degradation is clean.** Before any bet lands, every metric renders as `0ms` / `0%` / `n/a` — no `NaN`, no `None` leakage, no divide-by-zero.
- **`Overhead` intentionally not a standalone row.** TG v1.15.10 dropped it as noise; the CLI shows the value parenthetically inside the Cycle row so power users still see it without a dedicated row.
- **Wizard prompts + preset persistence for the new Phase 2/3 fields remain follow-up debt.** Fields work programmatically today; the interactive `_ask()` wizard doesn't prompt for them yet.

### Testing

- New file `wolfbet/tests/test_phase3_metrics.py` — 22 pure-function tests covering `_compute_api_avg`, `_compute_api_p50`, `_compute_on_target_pct`, `_compute_theoretical_max_bps`, `_compute_efficiency_pct`, `_record_api_ms`, `_record_cycle_ms`. Test count now: **51 (29 Phase 2 + 22 Phase 3)** — all pass in `.venv` in < 200 ms.
- Run: `cd wolfbet && WOLFBET_ENV=testing .venv/bin/python -m pytest tests/ -q` → `51 passed`.

### Version

- `wolfbet.py:VERSION` `2.16.0` → `2.17.0` (minor bump — new user-visible dashboard surface).
- Banner literal updated to match.

## CLI v2.16.0 — session robustness parity (recurring, streak delays, milestones, basebet_pct) (2026-07-17)

Phase 2 of the CLI ↔ TG parity roadmap. Closes Section 3 "Session robustness" of `audits/cli-vs-tg-parity.md`.

### Added

- **Recurring restart** — set `state.recurring = True` and the session auto-restarts (counters cleared, new SQLite row, fresh `/auto/start` UUID for auto games) after a **safe** stop reason. Safe = `Profit target reached` OR `Max bets reached` OR `Max wins reached`, AND `state.profit >= 0`. **Loss-driven stops (`Max loss hit`, `Balance floor hit`) and `Insufficient balance` ALWAYS bail out** — recurring never chases losses. Positive-whitelist guard is at `_should_recurring_restart` (wolfbet.py). Mirrors TG's `_notify_stop` "is_notable" throttling logic.
- **Streak delays** — pause N seconds after every N consecutive losses / wins / total-bets. Configured via `streak_delay_loss`, `streak_delay_win`, `streak_delay_bets` in `"N:seconds"` format. Parsed by `_parse_streak_delay`, applied by `_get_streak_delay` right before each bet.
- **`streakbet_loss`** — cut current_bet by a factor after every N consecutive losses. Format `"N:multiplier"` (e.g. `"5:0.5"` halves the bet after every 5-loss streak). Applied by `_get_streakbet_loss_factor` to the computed next_bet.
- **`basebet_pct`** — dynamic base_bet as % of current balance. Recomputed every 10 bets via `_apply_basebet_pct`. Compounds as balance grows; contracts as it shrinks. 0 disables (default).
- **Milestone tracking** — dashboard signal at every N bets / N wins / N losses / X profit. Configured via `milestone_bets` (default 100) / `milestone_wins` / `milestone_losses` / `milestone_profit`. `_check_milestone` returns a reason string; the CLI logs it and updates `state.status` briefly.
- **429 `Retry-After` respected** — when the WolfBet API returns 429 with a `Retry-After` header, the CLI now uses that value for `rate_backoff` instead of the fixed 2s probe. Falls back to 2s when the header is absent.

### Behavioural notes

- **No new external API surface.** Everything is a client-side config field or bet-loop hook.
- **No new secrets, no auth changes, no PII.** New config fields are numeric / boolean / string tuples.
- **No new module.** All Phase 2 helpers live in `wolfbet.py` alongside the existing helpers, mirroring the TG file's module layout. Phase 6 will de-duplicate via a shared library.
- **`_save_state_file()` extended** with the new config fields so `--status` can display them and future `--resume` can re-hydrate them.
- **Wizard prompts + preset persistence for the new fields are still TODO** — the fields are honoured whenever a caller sets them programmatically (or a preset JSON is hand-edited), but the interactive wizard doesn't prompt for them yet. Tracked as follow-up work; simplest CLI workflow today is `save_preset` → hand-edit `~/.wolfbet_presets.json` → `--resume`.

### Testing

- Manual smoke recommended:
  1. **Recurring** — set `state.recurring=True`, `max_wins=5`; expect session #N ends at bet 5, session #N+1 auto-starts, betting continues.
  2. **Recurring safety** — set `max_loss=0.001`; force it. Expect exit, no restart.
  3. **Streak delay** — set `streak_delay_loss=(3, 0.5)`; force 3 losses; expect a 0.5s pause before bet #4.
  4. **basebet_pct** — set `basebet_pct=0.001`, `current_balance=10`; after 10 bets, expect `base_bet ≈ 0.01`.
  5. **Milestone** — set `milestone_bets=10`; expect `state.status` briefly reads "Milestone: 10 bets" every 10 bets.
- Automated tests: pure-function unit tests for `_parse_streak_delay`, `_get_streak_delay`, `_get_streakbet_loss_factor`, `_apply_basebet_pct`, `_check_milestone` land in `wolfbet/tests/test_phase2_helpers.py` (see file). Run with `WOLFBET_ENV=testing pytest wolfbet/tests/test_phase2_helpers.py -q`.

### Version

- `wolfbet.py:VERSION` `2.15.0` → `2.16.0` (minor bump — new user-visible config surface).
- Banner literal updated to match.

## CLI v2.15.0 — dice-auto / limbo-auto now supported in terminal (2026-07-17)

### Added

- **CLI now supports WolfBet auto-bet endpoints** (`/v2/{dice,limbo}/auto/start`, `/auto/play`, `/auto/stop`). Selecting `dice-auto` or `limbo-auto` from the game menu runs a server-side auto-bet session — same as the Telegram bot has offered since v1.15.x. UUID lifecycle (start → play → stop) is fully managed; 400 "Session has ended" and expired-UUID errors trigger automatic seed rotation + restart. 429 on `/auto/start` retries with exponential backoff up to 60s and respects `Retry-After`.
- **`_wolfbet_hash(body_str)`** ported from `tg/engine.py:50-54`. Auto endpoints require a per-request `x-hash-api = md5(body) ⊕ md5(nonce)`. Manual endpoints keep their legacy hardcoded hash strings — no change to existing manual behaviour.
- **Strategy → WolfBet auto-bet translator** (`_build_auto_config`, `_custom_rule_to_wolfbet`). All 8 strategy keys (Flat, Martingale, Anti-Martingale, D'Alembert, Paroli, WolfRider, Delay Martingale, Rule-Based) are serialised into the WolfBet server-side rule format. Stop conditions (`max_profit`, `max_loss`, `max_bets`, `stop_on_balance`) are also translated into server-side stop rules so the auto session enforces them without a round-trip.
- **Snapshot fields** — `auto_uuid`, `auto_start_remaining`, `game` are now written to `~/.wolfbet_autobot_live.json` so `--status` can display them.
- **Clean shutdown** — Ctrl+C / SIGTERM calls `/auto/stop` (best-effort) before writing the final session snapshot so the server-side UUID is released promptly.
- **Parity audit doc** — `audits/cli-vs-tg-parity.md` documents every Telegram-bot feature vs its CLI status (Y/N/PARTIAL), criticality (P0/P1/P2), and target phase. Phase 1 (this release) delivers all P0 auto-bet items; Phases 2–5 cover recurring / streak delays / basebet_pct / dashboard metrics / tweak commands.

### Notes

- Banner literal fixed from stale `v2.10.2` to `v2.15.0`.
- Manual games (`dice`, `dice-manual`, `limbo`) are unchanged in behaviour — the auto path lives alongside the existing manual path in `betting_loop()`, dispatched by `_is_auto_game(state.game)`.
- No new secrets, no new auth surface, no new PII paths. The auto endpoints reuse the same Bearer token the manual endpoints already use.
- Testing: manual smoke recommended (`python wolfbet.py` → pick `dice-auto` → confirm UUID handshake + ~10 bets tick + clean stop). No automated test suite yet; per the parity audit, that's a Phase 2 deliverable.

## v2.16.11 — casino-shared dependency now installs over HTTPS (2026-07-17)

### Changed

- **`requirements.txt`: `casino-shared` pinned via `git+https://` instead of `git+ssh://`.** The `snad1/casino-shared` repo is now public, so pip can fetch it anonymously over HTTPS — fresh servers no longer need a GitHub SSH key just to run `install.sh`. README "Remote Server" note updated to match. (The `wolfbet` repo itself is still private; clone it with a read-only fine-grained PAT or SSH key.)

## v2.16.10 — `/status` speed block trimmed + WolfBet rate-limit line added (2026-06-17)

### Changed

- **Removed `Overhead` line** from the `/status` speed block — rarely actionable at a glance, adds noise.
- **`API stable` and `Efficiency` now render only when `bet_delay > 0`.** With no delay set, the theoretical-max reference is meaningless (efficiency = actual / (1/delay) blows up) and median-vs-target has no target — both lines were pure clutter. When the user does set a delay, both lines render as before.

### Added

- **`Rate limit: N/M play · K/20 start` line** in the `/status` speed block, placed between `API:` and `Cycle:`. Uses the `X-RateLimit-*` headers the engine already parses in `_update_rate_limit()` (engine.py:343-349) — no new engine state; three keys plumbed through `get_status()`. Answers "how much of the per-IP budget is left?" for users running multiple sessions on the same IP; same-IP sessions naturally converge on the same number without any cross-slot coordination.

## v2.16.9 — Session-ended notification: green ✅ on profit, red 🛑 on loss (2026-06-17)

### Changed

- **`Session #X ended` notification now leads with `✅`** when the session ended with `profit >= 0`, keeping `🛑` for `profit < 0`. Previously every stop-notification opened with `🛑` regardless of outcome — hitting a `max_profit` target (a good outcome) still triggered a red-icon push, causing a moment of false-alarm on the phone before reading the body. Leading emoji is now driven by the sign of `s["profit"]`; every other line of the summary is unchanged.

## v2.16.8 — Fix: `/status` rendering as plain text — `bet_delay` opened an unclosed italic entity (2026-06-17)

### Fixed

- **`/status` rendered as PLAIN TEXT** (literal `*Session #X*`, `*Balance*`, `` `12.71343655 USDT` `` visible) because the v1.15.7 fallback was firing every invocation. Root cause was the literal token `bet_delay` on the "API stable" line — a `_` sitting OUTSIDE any backtick code span, which Markdown V1 interprets as the start of an italic entity. The parser scanned forward for the closing `_`, never found one, and rejected the whole message with `Can't parse entities`. The fallback then resent as plain text — so the user saw content but no formatting. Renamed the token to `bet delay` (space instead of underscore) — same human meaning, no Markdown bomb. Full Markdown formatting now renders normally; the v1.15.7 fallback (`safe_reply_markdown`, enhanced `_safe_send`, parse-error catches in `_monitor_loop` and button callbacks) remains in place as the safety net.

## v2.16.7 — Fix: `/status` silently failing on Markdown parse error (2026-06-17)

### Fixed

- **`/status` returns nothing in Telegram** — server logs showed `[WARNING] Telegram network: Can't parse entities: can't find end of the entity starting at byte offset 1218–1235` on every `/status`. The custom-rules section and trailing engine-status line in `format_status` wrap user/external strings inside Markdown V1 code spans (`` `…` ``); a single stray backtick in a rule description or in an engine status message (e.g. `_fmt_stop_reason` rendering a WolfBet API template verbatim) produces three backticks on the line, splits the entity, and Telegram rejects the entire message. The bot's `_error_handler` swallows the `NetworkError` so the user just sees… nothing. Fixed at the render boundary:
  - `formatter.py` — new `_safe_code(s)` helper replaces backticks with `'` in the two dynamic fields wrapped in code spans (rule descriptions, trailing status).
  - `handlers.py` — new `safe_reply_markdown(reply_target, text, ...)` wrapper for the `/status` and `/monitor` `reply_text` paths. Existing `_safe_send` (engine-driven notifications) and the monitor-refresh `edit_text` paths now retry as plain text on `BadRequest` with a parse-error message. The user always sees the content; formatting falls back to literal `*foo*`/`` `foo` `` on the rare case the fallback fires. Technical detail is logged server-side only.

## v2.16.6 — Fix: SQLite cross-thread error in _get_conn (defensive) (2026-06-17)

### Fixed

- **`DB flush_bets failed: SQLite objects created in a thread can only be used in that same thread.`** — `_get_conn` returned a connection created on the main event-loop thread; calling it from inside `asyncio.to_thread(self._flush_bets)` violated SQLite's thread-confinement. The fix WAS shipped on the shared-library side (`check_same_thread=False` in `db_connect`, commit `2c499d0`), but the pip-installed casino-shared on the server was stale (the `*ctl update` script silences pip errors with `2>/dev/null`, so a transient install failure goes unnoticed). `_get_conn` now opens the connection directly via `sqlite3.connect(..., check_same_thread=False)` — bypassing the shared library for this property so a stale install can't reintroduce the bug.

Bets that failed to flush during the broken window are unrecoverable (they were in the in-memory `_bet_queue` when the error fired); but going forward, persistence works again.

## v2.16.5 — API stable: median + on-target % (2026-06-17)

### Added

- **`API stable: Xms median / Y% on-target`** under the existing `API:` line. `Median` is the typical API time (unbiased by spikes). `On-target %` is the fraction of bets whose API ≤ `bet_delay` — the bound on Efficiency.

User question answered: "why can't I get exact bps if API is mostly below bet_delay?" Answer: bps deficit ≈ (100% − on_target%). With ~5% slow API responses (above bet_delay), bps stays ~5% below theoretical regardless of bot code. This is mathematically inherent to sequential strategies where bet N+1 needs bet N's result before firing.

For genuine bps improvement the user has two levers (both their choice):
- Raise `bet_delay` above the API p95 to absorb more variance
- Use a faster endpoint (e.g. Dice v1 instead of Dice Auto v2 — already in the benchmark table)

## v2.16.4 — Efficiency % + final audit close-out (2026-06-17)

### Added

- **`Efficiency: X%`** on the dashboard — bot's actual recent bps as a % of the theoretical max (= 1 / max(bet_delay, api_avg)). Closes the "is the bot at peak?" question with a single number. The line includes the theoretical max so the user can see the ceiling directly.

### Benchmark (user-run, recorded for the project record)

Three controlled sessions on the same account, same strategy, same WolfBet network, varying bet_delay and game type:

| Session | Game | bet_delay | API avg | Cycle (smoothed) | Bps observed | Theoretical max |
|---|---|---|---|---|---|---|
| #335 | Dice Auto v2 | none | 77ms | 68ms | 12.1 bps | 13.0 bps |
| #336 | Dice v1 | 0.125s | 80ms | 139–159ms | 6.4 bps | 8.0 bps |
| #337 | Dice v1 | 0.320s | 69ms | **329ms** | **3.0 bps** | **3.13 bps (96%)** |

Session #337 is the proof point: Cycle 329ms = bet_delay 320ms + 9ms residual = **96% of theoretical max**. The 9ms residual is `asyncio.sleep` wake-up + Python interpreter overhead and is **not removable** without leaving the asyncio model. No code-level delays remain to clean up.

Earlier high-Cycle reports (378–449ms on Dice Auto v2) were a combination of the auto/play endpoint being slower on average AND sporadic 500–862ms API spikes — both external to the bot. Visible now in `API peak` (introduced in v2.16.3).

How to read the diagnostics together:
- `Efficiency 95%+` → bot doing all it can; residual is asyncio scheduler
- `Efficiency 70–90% + Overhead 0–2ms + API peak high` → API variance is the cause
- `Efficiency < 70% + Overhead > 20ms` → real concurrency/contention worth digging into

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

"""BettingEngine: per-user betting instance running as async task."""

import asyncio
import json
import os
import time
import sqlite3
import threading
import httpx
from datetime import datetime
from collections import deque
from typing import Optional, List

from . import VERSION
from .config import API_BASE, APP_ENV, EVENTS_ENABLED, EVENTS_URL, logger
from core.events import EventPublisher, validate_events_url
from core.database import init_db, migrate_db, db_connect, cleanup_old_bets, cleanup_live_bets
from core.strategy import StrategyRule, load_rules_from_text
from core.engine import compute_next_bet, wolfrider_switch, apply_action, evaluate_rules
# v2.28.0: shared helpers previously duplicated between CLI and TG.
# classify_auto_stop_reason CLOSES TG's max-nonce blind spot: TG previously
# only matched "session/expired/not exists/betsession", never "nonce",
# so every "Max nonce achieved" was treated as a generic API error and
# never triggered seed rotation. Now identical to CLI's behaviour.
from core.net_helpers import classify_auto_stop_reason, parse_streak_delay
from core.diagnostics import percentile
from core.games import (
    GAME_ENDPOINTS, AUTO_ENDPOINTS, GAME_LABELS, AUTO_START_MIN_INTERVAL,
    is_auto_game, fmt_multiplier, request_hash_header,
    build_auto_config, build_auto_start_payload,
)

# ── Batching / periodic save constants ──────────────────
BET_BATCH_SIZE     = 50      # flush bets + session stats to DB every N bets
SESSION_SAVE_SECS  = 30      # fallback: save session stats every N seconds
REQUEST_TIMEOUT    = httpx.Timeout(15.0, connect=5.0)
STREAK_RESET_REASON = "Loss streak reset"
PROFIT_INCREMENT_SCOPES = ("session", "run")   # v1.21.0: see BettingEngine._init_run
MAX_TIMEOUT_STREAK = 3       # recreate HTTP session after this many consecutive timeouts

EVENTS_PREFIX = "wolfbet-tg"
EVENTS_STATE_EVERY_S = 2.0
_events: Optional[EventPublisher] = None


def start_events(enabled: bool = EVENTS_ENABLED, url: str = EVENTS_URL, client=None) -> bool:
    """Start the one event publisher every engine in this process shares. False when events stay off.

    Events go to wolfbet-tg:<uid>:bets|state|lifecycle; engines only enqueue, so a slow or
    missing Redis never reaches the betting loops.
    """
    global _events
    if _events is not None:
        return True
    if not enabled:
        return False
    try:
        validate_events_url(url)
    except ValueError:
        logger.warning("WOLFBET_TG_EVENTS is on but WOLFBET_EVENTS_URL is missing or invalid: "
                       "events stay off")
        return False
    pub = EventPublisher(url, stream_prefix=EVENTS_PREFIX, scoped=True, client=client)
    if not pub.start():
        return False
    _events = pub
    logger.info("EVENTS publisher started (%s:<uid>:*)", EVENTS_PREFIX)
    return True


def stop_events() -> None:
    """Flush and stop the shared event publisher if it is running."""
    global _events
    pub, _events = _events, None
    if pub is not None:
        pub.stop()


class BettingEngine:
    """Self-contained betting engine for one user. Runs in its own thread."""

    def __init__(self, user_id: int, db_path: str, api_key: str, config: dict):
        self.user_id = user_id
        self.db_path = db_path
        self.lock    = threading.Lock()

        # ── config (use `or` to handle None from presets) ──
        self.api_key        = api_key
        self.proxy          = config.get("proxy") or None
        self.pipeline       = int(config.get("pipeline") or 1)
        self.currency       = config.get("currency") or "usdt"
        self.game           = config.get("game") or "dice"
        self.rule           = config.get("rule") or "over"
        self.multiplier     = float(config.get("multiplier") or 2.01)
        self.initial_multiplier = self.multiplier
        # bet_value is fully derived from multiplier + rule (99% RTP); always recompute to
        # avoid stale configs causing WolfBet's "Incorrect win chance given" rejection
        wc = 99.0 / self.multiplier if self.multiplier > 0 else 49.5
        self.bet_value      = round(100.0 - wc - 0.01, 2) if self.rule == "over" else round(wc, 2)
        self.base_bet       = float(config.get("base_bet") or 0.0001)
        self.current_bet    = self.base_bet
        self.strategy       = config.get("strategy") or "Martingale"
        self.strategy_key   = config.get("strategy_key") or "2"
        self.win_mult       = float(config.get("win_mult") or 1.0)
        self.loss_mult      = float(config.get("loss_mult") or 2.0)
        self.bet_delay      = float(config.get("bet_delay") or 0)
        self._last_bet_start_time: float = 0.0  # monotonic ts for bet_delay pacing
        self.delay_martin_threshold = int(config.get("delay_martin_threshold") or 3)

        # ── streak delay: pause after N consecutive wins/losses ──
        # Format: "N:seconds" e.g. "5:1.0" = after every 5 losses, delay 1s
        self.streak_delay_loss = self._parse_streak_delay(config.get("streak_delay_loss"))
        self.streak_delay_win  = self._parse_streak_delay(config.get("streak_delay_win"))
        # streak_delay_bets "N:seconds": every N total bets, delay X seconds (regardless of result)
        self.streak_delay_bets = self._parse_streak_delay(config.get("streak_delay_bets"))

        # ── adaptive base bet ──
        # basebet_pct: base_bet = balance * pct, recomputed on session start + profit-increment
        self.basebet_pct       = float(config.get("basebet_pct") or 0)
        # streakbet_loss "N:multiplier": after every N consecutive losses, current_bet *= multiplier
        self.streakbet_loss    = self._parse_streak_delay(config.get("streakbet_loss"))

        # ── rules ──
        self.custom_rules: List[StrategyRule] = []
        rules_text = config.get("custom_rules_text", "")
        if rules_text:
            self.custom_rules = load_rules_from_text(rules_text)

        # ── stop conditions ──
        self.max_profit      = config.get("max_profit")
        self.max_loss        = config.get("max_loss")
        self.max_bets        = config.get("max_bets")
        self.max_wins        = config.get("max_wins")
        self.stop_on_balance = config.get("stop_on_balance")
        # Streak reset: stop on the first win after this many consecutive
        # losses (profit >= 0 only); handlers restart after streak_reset_delay.
        self.streak_reset_losses = config.get("streak_reset_losses")

        # ── session state ──
        self.running         = False
        self.paused          = False
        self.session_id      = None
        self.session_start   = 0.0
        self.session_bet_ended_at: Optional[float] = None
        self.session_pause_started_at: Optional[float] = None
        self.total_bets      = 0
        self.wins            = 0
        self.losses          = 0
        self.profit          = 0.0
        self.wagered         = 0.0
        self.start_balance   = 0.0
        self.current_balance = 0.0
        self.current_streak  = 0
        self.max_win_streak  = 0
        self.max_loss_streak = 0
        # Length of the loss streak the latest win just broke (0 if none).
        self.last_broken_loss_streak = 0
        self.highest_bet     = 0.0
        self.highest_win     = 0.0
        self.biggest_loss    = 0.0
        self.highest_balance = 0.0
        self.lowest_balance  = float("inf")
        self.bets_per_second = 0.0
        self.bets_per_minute = 0.0
        self.peak_bps        = 0.0
        self.low_bps         = float("inf")
        self.peak_bpm        = 0.0
        self.low_bpm         = float("inf")
        self._bets_this_sec  = 0
        self._bets_this_min  = 0
        self._current_sec    = 0
        self._current_min    = 0
        # Sliding 60s window: what the bot is doing RIGHT NOW (vs. session average above)
        self.recent_bps      = 0.0
        self.recent_bpm      = 0.0
        self._recent_bet_times: deque = deque(maxlen=2000)
        # Per-cycle overhead (engine + event-loop scheduler), exp-smoothed ms
        # Answers: "is something blocking the bot or is this normal Python overhead?"
        self._cycle_overhead_ms: float = 0.0
        self._iter_start_mono: float = 0.0  # set at top of loop iteration
        # Full per-bet cycle time (T_start_N - T_start_N-1), exp-smoothed ms.
        # This is the bps reciprocal: Cycle*bps ≈ 1000. Honest about scheduler jitter.
        self._cycle_ms: float = 0.0
        # get_status() cache: milestone notifier + /status are often called within
        # ms of each other; cache the dict briefly to avoid rebuilding 50 fields twice
        self._status_cache: Optional[dict] = None
        self._status_cache_ts: float = 0.0
        self.profit_history  = deque(maxlen=40)
        self.profit_history.append(0.0)
        self.recent_bets     = deque(maxlen=5)
        self.status          = "Idle"
        self.last_error      = ""
        self.stop_reason     = ""

        # ── rate limiting ──
        self.rate_remaining  = 480
        self.rate_limit      = 480
        self.rate_wait       = False
        self.rate_backoff    = 2.0
        self.consecutive_errors = 0
        self._consecutive_timeouts = 0
        self.backoff_delay   = 1.0
        self._insufficient_balance = False
        self._events_next_state    = 0.0
        self._last_api_ms          = 0.0
        self._api_ms_total         = 0.0
        self._api_ms_count         = 0
        self.peak_api_ms: float    = 0.0  # max single-bet API response time this session
        self._api_samples: deque   = deque(maxlen=200)  # ring buffer for p50/median calc
        self._on_target_count: int = 0  # bets where api_ms ≤ bet_delay (the happy path)

        # ── strategy internals ──
        self.dalembert_unit  = 0
        self.paroli_count    = 0

        # ── profit-based base bet increment ──
        self.profit_increment     = config.get("profit_increment")    # amount to add to base_bet
        self.profit_threshold     = config.get("profit_threshold")    # profit interval that triggers
        self.next_profit_milestone = float(self.profit_threshold) if self.profit_threshold else 0.0
        # v1.21.0: run-wide increment (mirrors CLI v2.37.0). scope "run": profit
        # is the user's total across every session (all slots) since the run
        # started; bumps are shared through <user dir>/run.json.
        _scope = str(config.get("profit_increment_scope") or "session").lower()
        self.profit_increment_scope = _scope if _scope in PROFIT_INCREMENT_SCOPES else "session"
        _origin = config.get("_run_base_origin")
        self._run_base_origin: Optional[float] = float(_origin) if _origin is not None else None
        self.run_first_session: Optional[int] = None
        self.run_bumps   = 0
        self._run_others = 0.0     # DB profit of the run's other sessions (cached)
        self._run_bump_pending = False   # another slot's bump applied by _run_sync

        # ── milestone config ──
        self.milestone_bets   = int(config.get("milestone_bets")) if config.get("milestone_bets") is not None else 100
        self.milestone_wins   = int(config.get("milestone_wins") or 0)
        self.milestone_losses = int(config.get("milestone_losses") or 0)
        self.milestone_profit = float(config.get("milestone_profit") or 0)
        self._last_profit_milestone = 0.0  # track last profit threshold crossed

        # ── callbacks ──
        self.on_stop         = None   # called with (reason: str)
        self.on_milestone    = None   # called every N bets/wins/losses/profit
        self.on_error        = None

        # ── batching state ──
        self._bet_queue: list = []             # pending bet rows
        self._last_session_save = 0.0          # last time session was saved
        self._last_cleanup = 0.0               # last time live bet cleanup ran
        self.purge_days = int(config.get("purge_days") or 1)  # delete bets older than N days
        self._db_connection: Optional[sqlite3.Connection] = None  # persistent connection

        # ── auto-bet state ──
        self._auto_uuid: Optional[str] = None
        self._pending_restart: bool = False  # restart auto-bet on next win
        self._chart_snapshots: list = []    # [[bet_num, profit, balance], ...]
        self._auto_start_last_time: float = 0.0  # monotonic ts of last auto/start (for pacing)
        self._auto_start_remaining: Optional[int] = None  # last seen x-ratelimit-remaining
        self._session_expired: bool = False  # server-side auto-bet UUID no longer valid
        self._last_successful_bet_time: float = 0.0  # monotonic ts of last good bet; watchdog uses this

        # ── async HTTP client (reuses TCP/TLS connections) ──
        self._http: Optional[httpx.AsyncClient] = None

        # ── async task ──
        self._task: Optional[asyncio.Task] = None

    # ── API ──────────────────────────────────────────────
    async def _recreate_http(self):
        """Close and recreate the HTTP client to recover from stale connections."""
        try:
            if self._http:
                await self._http.aclose()
        except Exception:
            pass
        self._http = httpx.AsyncClient(
            headers=self._headers(), timeout=REQUEST_TIMEOUT,
            http2=False, follow_redirects=False,
            proxy=self.proxy if self.proxy else None,
        )
        logger.info("User %d: HTTP client recreated", self.user_id)

    def _base_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
        }

    def _headers(self, game_key: str = None) -> dict:
        """Static headers for the HTTP client (no hash: added per-request)."""
        return self._base_headers()

    def _request_headers(self, body_str: str = "") -> dict:
        """Per-request headers with x-hash-api resolved via core.games.request_hash_header."""
        h = request_hash_header(self.game, body_str)
        if h is not None:
            return {"x-hash-api": h, "x-client-type": "Web-Application"}
        return {}

    # v2.28.0: delegated to core.net_helpers.parse_streak_delay (shared with CLI).
    # Kept as a static method for backward compat with `self._parse_streak_delay(...)`
    # call sites throughout this class.
    _parse_streak_delay = staticmethod(parse_streak_delay)

    @staticmethod
    def _fmt_stop_reason(sr) -> str:
        """Turn WolfBet's auto-stop payload into a human message."""
        if isinstance(sr, dict):
            tmpl = sr.get("message") or ""
            for k, v in (sr.get("values") or {}).items():
                try:
                    v_fmt = f"{float(v):.8f}" if "." in str(v) or "e" in str(v).lower() else str(v)
                except (TypeError, ValueError):
                    v_fmt = str(v)
                tmpl = tmpl.replace("{{" + str(k) + "}}", v_fmt)
            return tmpl.strip() or str(sr)
        return str(sr)

    def _apply_basebet_pct(self) -> bool:
        """If basebet_pct configured, recompute base_bet from current balance. Returns True if changed."""
        if self.basebet_pct <= 0 or self.current_balance <= 0:
            return False
        new_base = round(self.current_balance * self.basebet_pct, 8)
        if new_base <= 0:
            return False
        if abs(new_base - self.base_bet) < 1e-12:
            return False
        self.base_bet = new_base
        self.current_bet = new_base
        return True

    def _get_streakbet_loss_factor(self) -> float:
        """Return current_bet multiplier when on a qualifying loss streak. 1.0 = no change."""
        streak = self.current_streak
        count, mult = self.streakbet_loss
        if count > 0 and streak < 0 and abs(streak) > 0 and abs(streak) % count == 0:
            return mult
        return 1.0

    def _get_streak_delay(self) -> float:
        """Check if current streak/total-bets triggers a delay. Returns max applicable delay or 0."""
        streak = self.current_streak
        delay = 0.0
        # Loss streak (negative)
        if streak < 0 and self.streak_delay_loss[0] > 0:
            count, d = self.streak_delay_loss
            if abs(streak) > 0 and abs(streak) % count == 0:
                delay = max(delay, d)
        # Win streak (positive)
        if streak > 0 and self.streak_delay_win[0] > 0:
            count, d = self.streak_delay_win
            if streak % count == 0:
                delay = max(delay, d)
        # Total bets: every N bets regardless of outcome
        if self.streak_delay_bets[0] > 0 and self.total_bets > 0:
            count, d = self.streak_delay_bets
            if self.total_bets % count == 0:
                delay = max(delay, d)
        return delay

    def _set_error(self, friendly: str, technical: str = ""):
        """Set last_error: friendly message in production, technical details in dev."""
        if APP_ENV != "production" and technical:
            self.last_error = technical
        else:
            self.last_error = friendly
        if technical:
            logger.warning("User %d: %s", self.user_id, technical)

    def _update_rate_limit(self, resp):
        try:
            ep = GAME_ENDPOINTS.get(self.game, ("", 1, 480, None))
            default_limit = ep[2]
            remaining = int(resp.headers.get("x-ratelimit-remaining", self.rate_remaining))
            self.rate_remaining = remaining
            self.rate_limit = int(resp.headers.get("x-ratelimit-limit", default_limit))
            if remaining <= 2 and not self.rate_wait:
                self.rate_wait = True
                self.rate_backoff = 2.0
        except Exception:
            pass

    async def api_get_balances(self) -> list:
        if not self._http:
            await self._recreate_http()
        r = await self._http.get(f"{API_BASE}/v1/user/balances")
        self._update_rate_limit(r)
        r.raise_for_status()
        return r.json().get("balances", [])

    async def _auto_start(self) -> bool:
        """Start an auto-bet session on WolfBet, returns True on success.

        Pacing strategy (window length is undocumented: could be 20s or 60s):
        1. Enforce a 1s floor between calls.
        2. As remaining quota drops, scale up the pre-wait: at 0 we wait 6s,
           at 1 we wait 4s, at 2 we wait 2s. Spreads bursts across the window.
        3. On 429, retry with backoffs that span up to ~120s: long enough to
           outlast a 60s window, regardless of where in the window we hit it.
        """
        endpoints = AUTO_ENDPOINTS.get(self.game)
        if not endpoints:
            self._set_error("Auto-bet not available for this game", f"No auto-bet endpoint for {self.game}")
            return False
        wait = (self._auto_start_last_time + AUTO_START_MIN_INTERVAL) - time.monotonic()
        if self._auto_start_remaining is not None:
            quota_floor = {0: 6.0, 1: 4.0, 2: 2.0}.get(self._auto_start_remaining, 0.0)
            wait = max(wait, quota_floor)
        if wait > 0:
            logger.info("User %d: Pacing auto-bet start, waiting %.2fs (remaining=%s)",
                        self.user_id, wait, self._auto_start_remaining)
            await asyncio.sleep(wait)
        url = f"{API_BASE}{endpoints['start']}"
        payload = self._build_auto_start_payload()
        body_str = json.dumps(payload, separators=(",", ":"))
        extra_headers = self._request_headers(body_str)
        backoffs = [1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 25.0, 60.0]
        for attempt in range(len(backoffs) + 1):
            self._auto_start_last_time = time.monotonic()
            try:
                r = await self._http.post(url, json=payload, headers=extra_headers)
                self._update_auto_start_remaining(r.headers)
                if r.status_code == 200:
                    data = r.json()
                    uuid = data.get("autoBet", {}).get("uuid")
                    if uuid:
                        self._auto_uuid = uuid
                        logger.info("User %d: Auto-bet started, uuid=%s (remaining=%s)",
                                    self.user_id, uuid, self._auto_start_remaining)
                        return True
                    self._set_error("Auto-bet start failed", f"No UUID in response: {data}")
                    return False
                if r.status_code == 429 and attempt < len(backoffs):
                    delay = float(r.headers.get("Retry-After", backoffs[attempt]))
                    logger.warning("User %d: Auto-bet start 429, retrying in %.1fs (attempt %d/%d)",
                                   self.user_id, delay, attempt + 1, len(backoffs))
                    await asyncio.sleep(delay)
                    continue
                self._set_error("Auto-bet start failed", f"HTTP {r.status_code}: {r.text[:120]}")
                return False
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                if attempt < len(backoffs):
                    delay = backoffs[attempt]
                    logger.warning("User %d: Auto-bet start network error (%s), retrying in %.1fs (attempt %d/%d)",
                                   self.user_id, type(e).__name__, delay, attempt + 1, len(backoffs))
                    await asyncio.sleep(delay)
                    continue
                self._set_error("Auto-bet start failed", f"Network error after retries: {type(e).__name__}: {e!r}")
                return False
            except Exception as e:
                self._set_error("Auto-bet start failed", f"Auto start exception: {type(e).__name__}: {e!r}")
                return False
        return False

    def _update_auto_start_remaining(self, headers) -> None:
        """Learn live remaining quota from x-ratelimit-remaining."""
        try:
            v = headers.get("x-ratelimit-remaining")
            if v is not None:
                self._auto_start_remaining = int(v)
        except (ValueError, TypeError):
            pass

    async def _recover_expired_session(self) -> bool:
        """Recover from 'Session has ended': rotate seed, stop dead UUID, start new one."""
        logger.warning("User %d: Auto-bet session expired: rotating seed + restarting", self.user_id)
        await self._rotate_server_seed()
        await self._auto_stop()  # best-effort; UUID is already dead
        return await self._auto_start()

    async def _rotate_server_seed(self) -> bool:
        """Rotate the WolfBet server seed via GET /v1/game/seed/refresh. Returns True on 200."""
        url = f"{API_BASE}/v1/game/seed/refresh"
        try:
            r = await self._http.get(url)
            if r.status_code == 200:
                logger.info("User %d: Server seed rotated", self.user_id)
                return True
            logger.warning("User %d: seed/refresh HTTP %d: %s",
                           self.user_id, r.status_code, (r.text or "")[:120])
        except Exception as e:
            logger.warning("User %d: seed/refresh exception: %s: %r",
                           self.user_id, type(e).__name__, e)
        return False

    async def _auto_stop(self):
        """Stop the auto-bet session on WolfBet."""
        endpoints = AUTO_ENDPOINTS.get(self.game)
        if not endpoints or not self._auto_uuid:
            return
        url = f"{API_BASE}{endpoints['stop']}"
        payload = {"uuid": self._auto_uuid}
        body_str = json.dumps(payload, separators=(",", ":"))
        extra_headers = self._request_headers(body_str)
        try:
            await self._http.post(url, json=payload, headers=extra_headers)
            logger.info("User %d: Auto-bet stopped, uuid=%s", self.user_id, self._auto_uuid)
        except Exception:
            pass
        self._auto_uuid = None

    def _is_auto_game(self) -> bool:
        return is_auto_game(self.game)

    def _build_auto_start_payload(self) -> dict:
        """Adapter: self.* → shared build_auto_start_payload()."""
        return build_auto_start_payload(
            self.game, self.currency, self.base_bet, self.multiplier,
            self.rule, self.bet_value, self._build_auto_config(),
        )

    def _build_auto_config(self) -> list:
        """Adapter: self.* → shared build_auto_config()."""
        return build_auto_config(
            self.strategy_key, self.loss_mult, self.win_mult, self.base_bet,
            self.game, self.custom_rules,
            {"max_profit": self.max_profit, "max_loss": self.max_loss,
             "max_bets": self.max_bets, "stop_on_balance": self.stop_on_balance},
            self.delay_martin_threshold, logger, f"User {self.user_id}: ",
        )

    def _build_bet_payload(self, amount: float) -> dict:
        gk = self.game
        if gk == "dice":
            return {
                "currency":   self.currency,
                "game":       "dice",
                "amount":     f"{amount:.8f}",
                "rule":       self.rule,
                "multiplier": fmt_multiplier(self.multiplier),
                "bet_value":  f"{self.bet_value:.2f}",
            }
        elif gk == "dice-manual":
            return {
                "currency":   self.currency,
                "game":       "dice",
                "amount":     f"{amount:.8f}",
                "rule":       self.rule,
                "multiplier": fmt_multiplier(self.multiplier),
                "bet_value":  f"{self.bet_value:.2f}",
                "auto":       1,
            }
        elif gk == "limbo":
            return {
                "currency":   self.currency,
                "game":       "limbo",
                "amount":     f"{amount:.8f}",
                "multiplier": fmt_multiplier(self.multiplier),
                "auto":       1,
            }
        elif gk in ("limbo-auto", "dice-auto"):
            return {"uuid": self._auto_uuid}
        return {}

    async def _api_place_bet(self, amount: float) -> Optional[dict]:
        if self.current_balance > 0 and amount > self.current_balance:
            self._set_error("Insufficient balance", f"bet {amount:.8f} > balance {self.current_balance:.8f}")
            self._insufficient_balance = True
            return None
        payload = self._build_bet_payload(amount)
        ep = GAME_ENDPOINTS.get(self.game, ("/v1/bet/place", 1, 480, False))
        url = f"{API_BASE}{ep[0]}"
        body_str = json.dumps(payload, separators=(",", ":"))
        extra_headers = self._request_headers(body_str)
        try:
            t0 = time.time()
            r = await self._http.post(url, json=payload, headers=extra_headers)
            self._last_api_ms = (time.time() - t0) * 1000
            if self._last_api_ms > self.peak_api_ms:
                self.peak_api_ms = self._last_api_ms
            self._api_samples.append(self._last_api_ms)
            if self.bet_delay > 0 and self._last_api_ms <= self.bet_delay * 1000:
                self._on_target_count += 1
            self._update_rate_limit(r)
            self._consecutive_timeouts = 0
            if r.status_code == 200:
                data = r.json()
                if "error" in data:
                    err_msg = data["error"].get("message", "") if isinstance(data["error"], dict) else str(data["error"])
                    if "insufficient balance" in err_msg.lower():
                        self._set_error("Insufficient balance")
                        self._insufficient_balance = True
                        return None
                    self._set_error("Bet rejected by API", f"API error: {err_msg[:120]}")
                    return None
                self._api_ms_total += self._last_api_ms
                self._api_ms_count += 1
                return data
            elif r.status_code == 429:
                self.rate_wait = True
                self.rate_backoff = 2.0
                self._set_error("Rate limit reached: cooling down")
            else:
                body = r.text or ""
                body_lower = body.lower()
                if "insufficient" in body_lower or "balance" in body_lower:
                    self._set_error("Insufficient balance")
                    self._insufficient_balance = True
                else:
                    # v2.28.0: shared classifier catches "max nonce achieved"
                    # AS WELL AS session-expired variants. Previously TG only
                    # detected the latter, so max-nonce responses fell through
                    # to the generic "Bet request failed" branch and _recover_
                    # expired_session never fired: same dashboard-spam bug
                    # the CLI shipped a fix for at v2.25.0.
                    reason = classify_auto_stop_reason(body)
                    if reason:
                        self._session_expired = True
                        friendly = ("Max nonce achieved: rotating server seed"
                                    if reason == "max_nonce"
                                    else "Auto-bet session expired: restarting")
                        self._set_error(friendly,
                                        f"HTTP {r.status_code}: {body[:120]}")
                    else:
                        self._set_error("Bet request failed",
                                        f"HTTP {r.status_code}: {body[:120]}")
        except (httpx.TimeoutException, httpx.ConnectError):
            self._consecutive_timeouts += 1
            self._set_error("Connection timeout: retrying")
            if self._consecutive_timeouts >= MAX_TIMEOUT_STREAK:
                logger.warning("User %d: %d consecutive timeouts: recreating HTTP client",
                               self.user_id, self._consecutive_timeouts)
                await self._recreate_http()
                self._consecutive_timeouts = 0
        except Exception as e:
            err_str = str(e).lower()
            if "insufficient" in err_str or "balance" in err_str:
                self._set_error("Insufficient balance")
                self._insufficient_balance = True
            else:
                self._set_error("Unexpected error", str(e)[:120])
        return None

    # ── DB ───────────────────────────────────────────────
    def _get_conn(self) -> sqlite3.Connection:
        """Return persistent DB connection, creating if needed.

        Opens with check_same_thread=False directly (not via shared db_connect)
        so the engine survives a stale casino-shared install. The connection
        is reused across asyncio.to_thread() calls; the engine owns it.
        """
        if self._db_connection is None:
            self._db_connection = sqlite3.connect(
                self.db_path, timeout=5, check_same_thread=False)
            self._db_connection.execute("PRAGMA journal_mode=WAL")
            # v2.29.0: mirror CLI's v2.27.0 writer PRAGMAs. Same 30-40 ms
            # per-commit fsync savings on typical VPS SSD; WAL-safe.
            self._db_connection.execute("PRAGMA synchronous = NORMAL")
            self._db_connection.execute("PRAGMA busy_timeout = 5000")
            self._db_connection.execute("PRAGMA cache_size = -32000")
            self._db_connection.execute("PRAGMA temp_store = MEMORY")
        return self._db_connection

    def _close_conn(self):
        """Close the persistent connection."""
        if self._db_connection:
            try:
                self._db_connection.close()
            except Exception:
                pass
            self._db_connection = None

    def _build_config_snapshot(self) -> str:
        """Build a JSON snapshot of session config for history."""
        snap = {
            "strategy_key": self.strategy_key,
            "loss_mult": self.loss_mult,
            "win_mult": self.win_mult,
            "bet_delay": self.bet_delay,
            "rule": self.rule,
            "bet_value": self.bet_value,
            "basebet_pct": self.basebet_pct or None,
            "streak_delay_loss": f"{self.streak_delay_loss[0]}:{self.streak_delay_loss[1]}" if self.streak_delay_loss[0] > 0 else None,
            "streak_delay_win": f"{self.streak_delay_win[0]}:{self.streak_delay_win[1]}" if self.streak_delay_win[0] > 0 else None,
            "streak_delay_bets": f"{self.streak_delay_bets[0]}:{self.streak_delay_bets[1]}" if self.streak_delay_bets[0] > 0 else None,
            "streakbet_loss": f"{self.streakbet_loss[0]}:{self.streakbet_loss[1]}" if self.streakbet_loss[0] > 0 else None,
            "milestone_bets": self.milestone_bets,
            "pipeline": self.pipeline,
        }
        if self.strategy_key == "7":  # Delay Martingale
            snap["delay_threshold"] = self.delay_martin_threshold
        stops = {}
        if self.max_profit is not None: stops["max_profit"] = self.max_profit
        if self.max_loss is not None:   stops["max_loss"] = self.max_loss
        if self.max_bets is not None:   stops["max_bets"] = self.max_bets
        if self.max_wins is not None:   stops["max_wins"] = self.max_wins
        if self.stop_on_balance is not None: stops["min_balance"] = self.stop_on_balance
        if stops:
            snap["stops"] = stops
        if self.profit_threshold and self.profit_increment is not None:
            snap["profit_threshold"] = self.profit_threshold
            snap["profit_increment"] = self.profit_increment
        if self.custom_rules:
            snap["rules"] = [r.to_dict() for r in self.custom_rules]
        return json.dumps(snap)

    def _db_start_session(self) -> int:
        # Use a temporary connection: start() runs in the main thread,
        # but the persistent connection must be created in the betting thread.
        snapshot = self._build_config_snapshot()
        conn = db_connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO sessions (started_at, currency, game, strategy, base_bet, multiplier,
                                  start_balance, config_snapshot)
            VALUES (?,?,?,?,?,?,?,?)
        """, (datetime.now().isoformat(), self.currency, self.game, self.strategy,
              self.base_bet, self.multiplier, self.start_balance, snapshot))
        conn.commit()
        sid = c.lastrowid
        conn.close()
        return sid

    def _queue_bet(self, bet_data: dict, profit: float, balance: float):
        """Queue a bet row for batched insertion.

        Flush is intentionally NOT triggered here even when the queue passes
        BET_BATCH_SIZE: _periodic_save (called from the async loop) handles
        flushing via asyncio.to_thread so DB I/O never blocks the event loop.
        Queue growing past 50 between saves is harmless (~6 KB per 50 rows;
        even 1000 queued ≈ 128 KB).
        """
        self._bet_queue.append((
            self.session_id, datetime.now().isoformat(),
            float(bet_data.get("amount", 0)), float(bet_data.get("multiplier", 0)),
            self.rule, float(bet_data.get("bet_value", 0)),
            float(bet_data.get("result_value", 0)), bet_data.get("state", ""),
            profit, balance,
        ))

    def _emit_bet(self, bet_data: dict, profit: float, balance: float) -> None:
        """Enqueue the bet event (CLI field order); never waits on Redis."""
        pub = _events
        if pub is None or not pub.reserve(self.user_id):
            return
        try:
            pub.emit_bet((
                self.session_id,
                float(bet_data.get("amount", 0)),
                float(bet_data.get("multiplier", 0)),
                self.rule,
                float(bet_data.get("bet_value", 0)),
                float(bet_data.get("result_value", 0)),
                bet_data.get("state", ""),
                profit,
                balance,
                self.profit,
                self.total_bets,
                self.current_streak,
                self._last_api_ms,
            ), self.user_id)
        except Exception:
            pass

    def _emit_lifecycle(self, kind: str, data: Optional[dict] = None) -> None:
        pub = _events
        if pub is not None:
            pub.emit_lifecycle(kind, data or {}, self.user_id)

    def _emit_session_open(self) -> None:
        self._emit_lifecycle("session_open", {
            "session_id": self.session_id, "game": self.game, "currency": self.currency,
            "strategy": self.strategy, "base_bet": self.base_bet,
            "multiplier": self.multiplier, "start_balance": self.start_balance,
        })

    def _emit_start(self, resumed: bool) -> None:
        self._emit_lifecycle("start", {
            "version": VERSION, "resumed": resumed, "game": self.game,
            "currency": self.currency, "strategy": self.strategy,
            "strategy_key": self.strategy_key, "base_bet": self.base_bet,
            "multiplier": self.multiplier, "rule": self.rule, "pipeline": self.pipeline,
        })
        if self.session_id:
            self._emit_session_open()

    def _state_snapshot(self) -> dict:
        """Live session state under the CLI state event's key names."""
        return {
            "session_id": self.session_id,
            "running": self.running,
            "paused": self.paused,
            "game": self.game,
            "currency": self.currency,
            "strategy": self.strategy,
            "multiplier": self.multiplier,
            "base_bet": self.base_bet,
            "current_bet": self.current_bet,
            "total_bets": self.total_bets,
            "wins": self.wins,
            "losses": self.losses,
            "profit": self.profit,
            "wagered": self.wagered,
            "current_balance": self.current_balance,
            "start_balance": self.start_balance,
            "current_streak": self.current_streak,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "highest_bet": self.highest_bet,
            "highest_win": self.highest_win,
            "biggest_loss": self.biggest_loss,
            "bets_per_minute": self.bets_per_minute,
            "bets_per_second": self.bets_per_second,
            "peak_bps": self.peak_bps,
            "low_bps": self.low_bps if self.low_bps != float("inf") else 0,
            "peak_bpm": self.peak_bpm,
            "low_bpm": self.low_bpm if self.low_bpm != float("inf") else 0,
            "highest_balance": self.highest_balance,
            "lowest_balance": (self.lowest_balance if self.lowest_balance != float("inf")
                               else self.current_balance),
        }

    def _events_tick(self) -> None:
        """Enqueue the state snapshot at most every EVENTS_STATE_EVERY_S, from the betting loop."""
        pub = _events
        if pub is None:
            return
        now = time.monotonic()
        if now < self._events_next_state:
            return
        self._events_next_state = now + EVENTS_STATE_EVERY_S
        try:
            pub.emit_state(self._state_snapshot(), self.user_id)
        except Exception:
            pass

    def _flush_bets(self):
        """Flush all queued bets to DB and save session stats in one go."""
        if not self._bet_queue:
            return
        try:
            conn = self._get_conn()
            conn.executemany("""
                INSERT INTO bets (session_id, timestamp, amount, multiplier, rule,
                    bet_value, result_value, state, profit, balance_after)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, self._bet_queue)
            conn.commit()
            self._bet_queue.clear()
        except Exception as e:
            logger.error("User %d: DB flush_bets failed: %s", self.user_id, e)
        # always save session stats when flushing bets: keeps both in sync
        self._db_save_session()

    def _db_save_session(self, final: bool = False):
        """Save session stats. If final=True, also set ended_at."""
        try:
            conn = self._get_conn()
            lo_bal = self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance
            if final:
                conn.execute("""
                    UPDATE sessions SET
                        ended_at=?, total_bets=?, wins=?, losses=?,
                        profit=?, wagered=?, end_balance=?,
                        max_win_streak=?, max_loss_streak=?,
                        highest_balance=?, lowest_balance=?,
                        highest_win=?, biggest_loss=?,
                        bets_per_minute=?, bets_per_second=?,
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?,
                        chart_snapshots=?
                    WHERE id=?
                """, (
                    # v1.18.0: freeze ended_at at bet-stop so pause time
                    # isn't rolled into session runtime (mirrors CLI v2.28.4).
                    (datetime.fromtimestamp(self.session_bet_ended_at).isoformat()
                     if self.session_bet_ended_at else datetime.now().isoformat()),
                    self.total_bets, self.wins, self.losses,
                    self.profit, self.wagered, self.current_balance,
                    self.max_win_streak, self.max_loss_streak,
                    self.highest_balance, lo_bal,
                    self.highest_win, self.biggest_loss,
                    min(self.bets_per_second * 60, self.rate_limit) if self.bets_per_second else self.bets_per_minute,
                    self.bets_per_second,
                    self.peak_bps,
                    self.low_bps if self.low_bps != float("inf") else 0,
                    self.peak_bpm,
                    self.low_bpm if self.low_bpm != float("inf") else 0,
                    json.dumps(self._chart_snapshots[-2000:]),  # keep last 2000 points
                    self.session_id,
                ))
            else:
                conn.execute("""
                    UPDATE sessions SET
                        total_bets=?, wins=?, losses=?,
                        profit=?, wagered=?, end_balance=?,
                        max_win_streak=?, max_loss_streak=?,
                        highest_balance=?, lowest_balance=?,
                        highest_win=?, biggest_loss=?,
                        bets_per_minute=?, bets_per_second=?,
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?,
                        chart_snapshots=?
                    WHERE id=?
                """, (
                    self.total_bets, self.wins, self.losses,
                    self.profit, self.wagered, self.current_balance,
                    self.max_win_streak, self.max_loss_streak,
                    self.highest_balance, lo_bal,
                    self.highest_win, self.biggest_loss,
                    min(self.bets_per_second * 60, self.rate_limit) if self.bets_per_second else self.bets_per_minute,
                    self.bets_per_second,
                    self.peak_bps,
                    self.low_bps if self.low_bps != float("inf") else 0,
                    self.peak_bpm,
                    self.low_bpm if self.low_bpm != float("inf") else 0,
                    json.dumps(self._chart_snapshots[-2000:]),
                    self.session_id,
                ))
            conn.commit()
            self._last_session_save = time.time()
        except Exception as e:
            logger.error("User %d: DB save_session failed: %s", self.user_id, e)

    async def _periodic_save(self):
        """Flush bets and save session stats periodically.

        DB calls run in a worker thread so the event loop stays free for other
        engines to keep betting: without this, 6 concurrent sessions
        serialize on each other's flushes and bps collapses to ~1/N.
        """
        self._events_tick()
        now = time.time()
        if now - self._last_session_save >= SESSION_SAVE_SECS:
            if self._bet_queue:
                await asyncio.to_thread(self._flush_bets)
            else:
                await asyncio.to_thread(self._db_save_session)
            # v1.21.0: refresh the run total (other slots) after each save.
            if self.run_first_session is not None:
                await asyncio.to_thread(self._run_sync)
        # Live cleanup: purge old bets every hour to prevent DB bloat
        if now - self._last_cleanup >= 3600:
            try:
                deleted = await asyncio.to_thread(
                    cleanup_live_bets, self.db_path, self.purge_days)
                if deleted > 0:
                    logger.info("User %d: Live cleanup: deleted %d old bets (>%dd)",
                                self.user_id, deleted, self.purge_days)
                self._last_cleanup = now
            except Exception as e:
                logger.warning("User %d: Live cleanup failed: %s", self.user_id, e)
            self._last_cleanup = now

    # ── STRATEGY (delegates to core.engine) ────────────────
    def _compute_next_bet(self, last_result: str) -> float:
        nxt, self.dalembert_unit, self.paroli_count = compute_next_bet(
            self.strategy_key, self.base_bet, self.current_bet,
            self.loss_mult, self.win_mult, last_result,
            self.current_streak, self.delay_martin_threshold,
            self.dalembert_unit, self.paroli_count,
        )
        return nxt

    def _wolfrider_switch(self):
        new_rule, new_bv = wolfrider_switch(self.total_bets, self.rule, self.multiplier)
        if new_rule is not None:
            self.rule = new_rule
            self.bet_value = new_bv

    def _apply_rules(self, bet_state: str):
        triggered = evaluate_rules(
            self.custom_rules, bet_state, self.wins, self.losses,
            self.total_bets, self.current_streak, self.profit,
            self.current_balance, self.current_bet, self.multiplier,
        )
        for rule in triggered:
            mutations = apply_action(
                rule, self.current_bet, self.base_bet,
                self.rule, self.multiplier, self.current_streak,
                initial_multiplier=self.initial_multiplier,
            )
            for k, v in mutations.items():
                setattr(self, k, v)

    def _should_stop(self):
        p = self.profit
        b = self.current_balance
        if self.max_profit is not None and p >= self.max_profit:
            return True, f"Profit target: {p:+.8f}"
        if self.max_loss is not None and p <= -abs(self.max_loss):
            return True, f"Max loss: {p:+.8f}"
        if self.max_bets is not None and self.total_bets >= self.max_bets:
            return True, f"Max bets: {self.total_bets}"
        if self.max_wins is not None and self.wins >= self.max_wins:
            return True, f"Max wins: {self.wins}"
        if self.stop_on_balance is not None and b <= self.stop_on_balance:
            return True, f"Balance floor: {b:.8f}"
        # Streak reset: only on the win that broke an N+ loss streak, not in the red.
        n = self.streak_reset_losses
        if (n and self.current_streak == 1
                and self.last_broken_loss_streak >= n and p >= 0):
            return True, f"{STREAK_RESET_REASON}: {self.last_broken_loss_streak} losses then win"
        return False, ""

    def _process_bet_result(self, result: dict) -> str:
        """Process a bet result: update stats, queue DB write. Returns bet_state."""
        bet_data = result.get("bet", {})
        balance_data = result.get("userBalance", {})
        bet_state = bet_data.get("state", "loss")
        result_value = float(bet_data.get("result_value", 0))
        amount_used = float(bet_data.get("amount", self.current_bet))
        new_balance = float(balance_data.get("amount", self.current_balance))
        raw_profit = float(bet_data.get("profit", 0))

        self.consecutive_errors = 0
        self.backoff_delay = 1.0
        self.total_bets += 1
        self.wagered += amount_used
        self.profit += raw_profit
        self.current_balance = new_balance

        if bet_state == "win":
            self.wins += 1
            if raw_profit > self.highest_win:
                self.highest_win = raw_profit
            self.last_broken_loss_streak = max(-self.current_streak, 0)
            self.current_streak = max(self.current_streak, 0) + 1
            self.max_win_streak = max(self.max_win_streak, self.current_streak)
        else:
            self.losses += 1
            if amount_used > self.biggest_loss:
                self.biggest_loss = amount_used
            self.current_streak = min(self.current_streak, 0) - 1
            self.max_loss_streak = max(self.max_loss_streak, abs(self.current_streak))

        if amount_used > self.highest_bet:
            self.highest_bet = amount_used
        if new_balance > self.highest_balance:
            self.highest_balance = new_balance
        if new_balance < self.lowest_balance:
            self.lowest_balance = new_balance

        if self.total_bets % 5 == 0:
            self.profit_history.append(self.profit)

        # Chart snapshot every 100 bets (survives bet cleanup)
        if self.total_bets % 100 == 0:
            self._chart_snapshots.append([self.total_bets, round(self.profit, 10), round(new_balance, 10)])

        self.recent_bets.append({
            "n": self.total_bets, "time": datetime.now().isoformat(),
            "amt": amount_used, "roll": result_value,
            "state": bet_state, "pnl": raw_profit, "bal": new_balance,
        })

        self._update_speed_stats()

        sign = "+" if raw_profit >= 0 else ""
        emoji = "W" if bet_state == "win" else "L"
        self.status = f"{emoji} {'WIN' if bet_state == 'win' else 'LOSS'} | Roll: {result_value:.2f} | P/L: {sign}{raw_profit:.8f}"
        self.last_error = ""

        self._queue_bet(bet_data, raw_profit, new_balance)
        self._emit_bet(bet_data, raw_profit, new_balance)
        return bet_state

    def _emit_session_end(self) -> None:
        """Final events for a stopping engine: terminal error, session totals, stop reason."""
        if self._insufficient_balance:
            self._emit_lifecycle("error", {"reason": "Insufficient balance"})
        self._emit_lifecycle("session_close", {
            "session_id": self.session_id, "total_bets": self.total_bets,
            "wins": self.wins, "losses": self.losses, "profit": self.profit,
            "wagered": self.wagered, "end_balance": self.current_balance,
        })
        self._emit_lifecycle("stop", {"reason": self.stop_reason or "Stopped by user"})

    async def _session_cleanup(self):
        """Shared cleanup for both betting loops."""
        if self._auto_uuid:
            try:
                await self._auto_stop()
            except Exception as e:
                logger.error("User %d: auto_stop failed: %s", self.user_id, e)
        await asyncio.to_thread(self._flush_bets)
        if self.session_id:
            await asyncio.to_thread(self._db_save_session, True)
        self._emit_session_end()
        self._close_conn()
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass
        logger.info("User %d: Session #%d ended: %s (bets=%d, P/L=%+.8f)",
                    self.user_id, self.session_id, self.stop_reason or "stopped",
                    self.total_bets, self.profit)
        if self.on_stop:
            try:
                self.on_stop(self.stop_reason or "Stopped by user")
            except Exception:
                pass

    def _update_speed_stats(self):
        """Update BPS/BPM tracking: called after each bet result.

        Maintains both:
          - bets_per_second / bets_per_minute: cumulative session average
          - recent_bps / recent_bpm: sliding 60s window (what's happening NOW)
        """
        now = time.time()
        elapsed = now - self.session_start
        if elapsed > 0:
            self.bets_per_second = self.total_bets / elapsed
            self.bets_per_minute = self.bets_per_second * 60

        # Sliding 60s window: drop entries older than 60s and recompute
        mono = time.monotonic()
        self._recent_bet_times.append(mono)
        cutoff = mono - 60.0
        while self._recent_bet_times and self._recent_bet_times[0] < cutoff:
            self._recent_bet_times.popleft()
        self.recent_bps = len(self._recent_bet_times) / 60.0
        self.recent_bpm = self.recent_bps * 60

        sec_key = int(now)
        min_key = int(now) // 60
        if sec_key != self._current_sec:
            if self._current_sec > 0 and self._bets_this_sec > 0:
                if self._bets_this_sec > self.peak_bps:
                    self.peak_bps = self._bets_this_sec
                if self._bets_this_sec < self.low_bps:
                    self.low_bps = self._bets_this_sec
            self._current_sec = sec_key
            self._bets_this_sec = 1
        else:
            self._bets_this_sec += 1
        if min_key != self._current_min:
            if self._current_min > 0 and self._bets_this_min > 0:
                if self._bets_this_min > self.peak_bpm:
                    self.peak_bpm = self._bets_this_min
                if self._bets_this_min < self.low_bpm:
                    self.low_bpm = self._bets_this_min
            self._current_min = min_key
            self._bets_this_min = 1
        else:
            self._bets_this_min += 1

    def _check_milestone(self, last_state: str) -> str:
        """Check if any milestone threshold was hit. Returns reason or empty string."""
        # Every N bets
        if self.milestone_bets > 0 and self.total_bets % self.milestone_bets == 0:
            return f"{self.total_bets} bets"
        # Every N wins
        if self.milestone_wins > 0 and last_state == "win" and self.wins % self.milestone_wins == 0:
            return f"{self.wins} wins"
        # Every N losses
        if self.milestone_losses > 0 and last_state != "win" and self.losses % self.milestone_losses == 0:
            return f"{self.losses} losses"
        # Every X profit increment
        if self.milestone_profit > 0 and self.profit > 0:
            threshold = self.milestone_profit
            current_level = int(self.profit / threshold)
            last_level = int(self._last_profit_milestone / threshold)
            if current_level > last_level:
                self._last_profit_milestone = self.profit
                return f"profit +{self.profit:.8f}"
        return ""

    # ── LIFECYCLE ────────────────────────────────────────
    async def start(self) -> bool:
        """Connect, fetch balance, start betting task. Returns True on success."""
        if not self._http:
            await self._recreate_http()
        try:
            balances = await self.api_get_balances()
        except Exception as e:
            self._set_error("Connection failed", f"Connection failed: {e}")
            return False

        bal = 0.0
        for b in balances:
            if b.get("currency", "").lower() == self.currency.lower():
                bal = float(b.get("amount", 0))
                break
        if bal <= 0 and self.base_bet > 0:
            self._set_error(f"No {self.currency.upper()} balance found")
            return False

        init_db(self.db_path)
        migrate_db(self.db_path)
        cleanup_old_bets(self.db_path)
        self.start_balance   = bal
        self.current_balance = bal
        self.highest_balance = bal
        self.lowest_balance  = bal
        if self._apply_basebet_pct():
            logger.info("User %d: basebet_pct=%.6f → base_bet=%.8f (balance=%.8f)",
                        self.user_id, self.basebet_pct, self.base_bet, bal)
        self._init_run()      # v1.21.0: restore run bumps before the session row
        self.session_start   = time.time()
        self._last_session_save = time.time()
        self.session_id      = self._db_start_session()
        self._attach_run(self.session_id)
        self.running         = True
        self.status          = "Starting..."

        if self._is_auto_game():
            if not await self._auto_start():
                return False

        self._task = asyncio.create_task(self._betting_loop())
        self._emit_start(resumed=False)
        return True

    # ── v1.21.0: run-wide profit increment ───────────────
    def _increment_on(self) -> bool:
        try:
            return (self.profit_increment is not None and float(self.profit_increment) > 0
                    and bool(self.profit_threshold) and float(self.profit_threshold) > 0)
        except (TypeError, ValueError):
            return False

    def _run_path(self) -> str:
        return os.path.join(os.path.dirname(self.db_path) or ".", "run.json")

    def _load_run(self):
        try:
            with open(self._run_path()) as f:
                d = json.load(f)
            return {"first_session": int(d["first_session"]),
                    "started_at": d.get("started_at"),
                    "bumps": max(0, int(d.get("bumps") or 0))}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_run(self, started_at=None) -> None:
        if self.run_first_session is None:
            return
        cur = self._load_run()
        if started_at is None and cur and cur["first_session"] == self.run_first_session:
            started_at = cur["started_at"]
        path = self._run_path()
        tmp = path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"first_session": self.run_first_session,
                           "started_at": started_at or datetime.now().isoformat(),
                           "bumps": self.run_bumps}, f)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("User %d: run file save failed: %s", self.user_id, e)

    def _end_run(self, reason: str) -> None:
        had = self.run_first_session is not None
        try:
            os.remove(self._run_path())
            had = True
        except OSError:
            pass
        self.run_first_session = None
        self.run_bumps = 0
        self._run_others = 0.0
        if had:
            logger.warning("User %d: RUN ended (%s): the next run starts fresh",
                           self.user_id, reason)

    def _run_db_sum(self, first_sid: int) -> float:
        """Profit of the run's sessions (all slots) except this engine's own."""
        try:
            conn = db_connect(self.db_path)
            row = conn.execute(
                "SELECT COALESCE(SUM(profit),0) FROM sessions WHERE id >= ? AND id != ?",
                (first_sid, self.session_id or -1)).fetchone()
            conn.close()
            return float(row[0] or 0.0)
        except Exception as e:
            logger.warning("User %d: run profit query failed: %s", self.user_id, e)
            return 0.0

    def _db_max_session_id(self) -> int:
        try:
            conn = db_connect(self.db_path)
            row = conn.execute("SELECT COALESCE(MAX(id),0) FROM sessions").fetchone()
            conn.close()
            return int(row[0] or 0)
        except Exception:
            return 0

    def run_profit_total(self) -> float:
        return self._run_others + self.profit

    def _apply_run_bumps(self) -> None:
        """base_bet = origin + bumps × increment (basebet_pct keeps its own path)."""
        if self.basebet_pct > 0 or self._run_base_origin is None or not self.profit_increment:
            return
        self.base_bet = round(self._run_base_origin + self.run_bumps * float(self.profit_increment), 8)
        self.current_bet = max(self.current_bet, self.base_bet) if self.total_bets else self.base_bet

    def _init_run(self, resumed: bool = False) -> None:
        """Join (or prepare) the user's run before betting. Scope "run" only.

        Off (none / 0) or per-session scope removes a leftover run file so old
        bumps never come back. A file pointing past the DB's last session (the
        stats were reset) starts a new run.
        """
        self.run_first_session = None
        self.run_bumps = 0
        self._run_others = 0.0
        if self.profit_increment_scope != "run" or not self._increment_on():
            self._end_run("increment off" if self.profit_increment_scope == "run" else "scope session")
            return
        if self._run_base_origin is None:
            # Fresh /bet: config base is the origin. Resume: the snapshot base
            # already holds the bumps, so subtract them back out below.
            self._run_base_origin = self.base_bet
            pending_resume_fix = resumed
        else:
            pending_resume_fix = False
        run = self._load_run()
        if run and run["first_session"] > self._db_max_session_id() + 1:
            logger.warning("User %d: RUN file points past the stats DB: starting a new run",
                           self.user_id)
            run = None
        if run:
            self.run_first_session = run["first_session"]
            self.run_bumps = run["bumps"]
            self._run_others = self._run_db_sum(run["first_session"])
            if pending_resume_fix and self.profit_increment:
                self._run_base_origin = round(
                    self.base_bet - self.run_bumps * float(self.profit_increment), 8)
            self._apply_run_bumps()
        self.next_profit_milestone = (self.run_bumps + 1) * float(self.profit_threshold)
        logger.info("User %d: RUN %s since #%s bumps=%d run profit=%+.8f base_bet=%.8f",
                    self.user_id, "joined" if run else "new",
                    self.run_first_session or "next", self.run_bumps,
                    self._run_others, self.base_bet)

    def _attach_run(self, session_id: int) -> None:
        if (self.profit_increment_scope == "run" and self._increment_on()
                and self.run_first_session is None and session_id):
            run = self._load_run()        # another slot may have started one
            if run:
                self.run_first_session = run["first_session"]
                self.run_bumps = run["bumps"]
                return
            self.run_first_session = session_id
            self.run_bumps = 0
            self._save_run()

    def _run_sync(self) -> bool:
        """Refresh the other slots' profit + shared bumps. True if base_bet changed."""
        if self.run_first_session is None:
            return False
        run = self._load_run()
        if not run or run["first_session"] != self.run_first_session:
            # /runreset or another slot ended the run.
            self.run_first_session = None
            self.run_bumps = 0
            self._run_others = 0.0
            return False
        self._run_others = self._run_db_sum(self.run_first_session)
        if run["bumps"] != self.run_bumps:
            self.run_bumps = run["bumps"]
            self.next_profit_milestone = (self.run_bumps + 1) * float(self.profit_threshold or 0)
            before = self.base_bet
            self._apply_run_bumps()
            if self.base_bet != before:
                self._run_bump_pending = True
                return True
        return False

    def _maybe_profit_increment(self) -> bool:
        """Bump base_bet at the next profit milestone. True if base_bet changed.

        scope "session": this session's profit (existing behaviour).
        scope "run": the user's run total; the bump is shared via run.json.
        """
        if self._run_bump_pending:
            self._run_bump_pending = False      # a bump made by another slot
            return True
        if not self._increment_on():
            return False
        run = self.profit_increment_scope == "run" and self.run_first_session is not None
        if run:
            cur = self._load_run()
            if cur and cur["first_session"] == self.run_first_session and cur["bumps"] > self.run_bumps:
                self.run_bumps = cur["bumps"]
                self.next_profit_milestone = (self.run_bumps + 1) * float(self.profit_threshold)
            p = self.run_profit_total()
        else:
            p = self.profit
        if p < self.next_profit_milestone:
            return False
        if self.basebet_pct > 0 and self.current_balance > 0:
            self.base_bet = round(self.current_balance * self.basebet_pct, 8)
        elif run:
            self.run_bumps += 1
            self._apply_run_bumps()
        else:
            self.base_bet += self.profit_increment
        if run and self.basebet_pct > 0:
            self.run_bumps += 1
        if run:
            self._save_run()
            self.next_profit_milestone = (self.run_bumps + 1) * float(self.profit_threshold)
        else:
            self.next_profit_milestone += self.profit_threshold
        logger.info("User %d: %sPROFIT INCREMENT base_bet → %.8f (next at %.8f profit)",
                    self.user_id, "RUN " if run else "", self.base_bet,
                    self.next_profit_milestone)
        return True

    def snapshot_state(self) -> dict:
        """Capture full engine state for resume after restart."""
        rules_ser = json.dumps([r.to_dict() for r in self.custom_rules]) if self.custom_rules else ""
        config = {
            "api_key": self.api_key,
            "proxy": self.proxy,
            "pipeline": self.pipeline,
            "currency": self.currency,
            "game": self.game,
            "_auto_uuid": self._auto_uuid,
            "rule": self.rule,
            "multiplier": self.multiplier,
            "bet_value": self.bet_value,
            "base_bet": self.base_bet,
            "strategy": self.strategy,
            "strategy_key": self.strategy_key,
            "win_mult": self.win_mult,
            "loss_mult": self.loss_mult,
            "bet_delay": self.bet_delay,
            "delay_martin_threshold": self.delay_martin_threshold,
            "custom_rules_text": rules_ser,
            "max_profit": self.max_profit,
            "max_loss": self.max_loss,
            "max_bets": self.max_bets,
            "max_wins": self.max_wins,
            "stop_on_balance": self.stop_on_balance,
            "streak_reset_losses": self.streak_reset_losses,
            "profit_increment": self.profit_increment,
            "profit_threshold": self.profit_threshold,
            "milestone_bets": self.milestone_bets,
            "milestone_wins": self.milestone_wins,
            "milestone_losses": self.milestone_losses,
            "milestone_profit": self.milestone_profit,
            "streak_delay_loss": f"{self.streak_delay_loss[0]}:{self.streak_delay_loss[1]}" if self.streak_delay_loss[0] > 0 else None,
            "streak_delay_win":  f"{self.streak_delay_win[0]}:{self.streak_delay_win[1]}" if self.streak_delay_win[0] > 0 else None,
            "streak_delay_bets": f"{self.streak_delay_bets[0]}:{self.streak_delay_bets[1]}" if self.streak_delay_bets[0] > 0 else None,
            "streakbet_loss":    f"{self.streakbet_loss[0]}:{self.streakbet_loss[1]}" if self.streakbet_loss[0] > 0 else None,
            "basebet_pct":       self.basebet_pct or None,
            "profit_increment_scope": self.profit_increment_scope,
            "_run_base_origin":  self._run_base_origin,
        }
        return {
            "user_id": self.user_id,
            "db_path": self.db_path,
            "config": config,
            "session_id": self.session_id,
            "session_start": self.session_start,
            "initial_multiplier": self.initial_multiplier,
            "current_bet": self.current_bet,
            "total_bets": self.total_bets,
            "wins": self.wins,
            "losses": self.losses,
            "profit": self.profit,
            "wagered": self.wagered,
            "start_balance": self.start_balance,
            "current_balance": self.current_balance,
            "current_streak": self.current_streak,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "highest_bet": self.highest_bet,
            "highest_win": self.highest_win,
            "biggest_loss": self.biggest_loss,
            "highest_balance": self.highest_balance,
            "lowest_balance": self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance,
            "dalembert_unit": self.dalembert_unit,
            "paroli_count": self.paroli_count,
            "next_profit_milestone": self.next_profit_milestone,
            "_last_profit_milestone": self._last_profit_milestone,
        }

    def restore_state(self, snap: dict):
        """Restore runtime state from a snapshot dict."""
        self.session_id        = snap["session_id"]
        self.session_start     = snap["session_start"]
        self.initial_multiplier = snap.get("initial_multiplier", self.multiplier)
        self.current_bet       = snap["current_bet"]
        self.total_bets        = snap["total_bets"]
        self.wins              = snap["wins"]
        self.losses            = snap["losses"]
        self.profit            = snap["profit"]
        self.wagered           = snap["wagered"]
        self.start_balance     = snap["start_balance"]
        self.current_balance   = snap["current_balance"]
        self.current_streak    = snap["current_streak"]
        self.max_win_streak    = snap["max_win_streak"]
        self.max_loss_streak   = snap["max_loss_streak"]
        self.highest_bet       = snap["highest_bet"]
        self.highest_win       = snap["highest_win"]
        self.biggest_loss      = snap["biggest_loss"]
        self.highest_balance   = snap["highest_balance"]
        self.lowest_balance    = snap.get("lowest_balance", float("inf"))
        self.dalembert_unit    = snap.get("dalembert_unit", 0)
        self.paroli_count      = snap.get("paroli_count", 0)
        self.next_profit_milestone  = snap.get("next_profit_milestone", 0.0)
        self._last_profit_milestone = snap.get("_last_profit_milestone", 0.0)
        self._auto_uuid            = snap.get("_auto_uuid")

    async def start_resumed(self) -> bool:
        """Resume a saved session. Tests API connection but skips balance fetch / new DB session."""
        if not self._http:
            await self._recreate_http()
        init_db(self.db_path)

        try:
            await self.api_get_balances()
        except Exception as e:
            self._set_error("Resume connection failed", f"Resume connection failed: {e}")
            return False

        # v1.21.0: re-join the run (snapshot base already holds its bumps).
        self._init_run(resumed=True)
        self._attach_run(self.session_id)

        self.running = True
        self.paused = False
        self.status = "Resumed"
        self._last_session_save = time.time()
        elapsed = time.time() - self.session_start if self.session_start else 0
        if elapsed > 0 and self.total_bets > 0:
            self.bets_per_second = self.total_bets / elapsed
            self.bets_per_minute = self.bets_per_second * 60

        # Auto-bet: old UUID is dead after restart: get a new one
        if self._is_auto_game():
            self._auto_uuid = None  # clear stale UUID
            if not await self._auto_start():
                self._set_error("Auto-bet resume failed", f"Auto-bet resume failed: {self.last_error}")
                self.running = False
                return False

        self._task = asyncio.create_task(self._betting_loop())
        self._emit_start(resumed=True)
        logger.info("User %d: Resumed session #%d", self.user_id, self.session_id)
        return True

    def stop(self):
        self.running = False

    async def mutate(self, changes: dict) -> str:
        """Apply config changes to a running session. Returns status message."""
        msgs = []

        # ── Instant changes (no restart) ──
        if "pipeline" in changes:
            old = self.pipeline
            self.pipeline = int(changes["pipeline"])
            msgs.append(f"Pipeline: {old} → {self.pipeline}")

        if "bet_delay" in changes:
            self.bet_delay = float(changes["bet_delay"])
            msgs.append(f"Delay: {self.bet_delay}s")
        if "streak_delay_loss" in changes:
            self.streak_delay_loss = self._parse_streak_delay(changes["streak_delay_loss"])
            msgs.append(f"Streak delay loss: {self.streak_delay_loss[0]}x → {self.streak_delay_loss[1]:.3f}s")
        if "streak_delay_win" in changes:
            self.streak_delay_win = self._parse_streak_delay(changes["streak_delay_win"])
            msgs.append(f"Streak delay win: {self.streak_delay_win[0]}x → {self.streak_delay_win[1]:.3f}s")
        if "streak_delay_bets" in changes:
            self.streak_delay_bets = self._parse_streak_delay(changes["streak_delay_bets"])
            if self.streak_delay_bets[0] > 0:
                msgs.append(f"Streak delay bets: every {self.streak_delay_bets[0]} bets → {self.streak_delay_bets[1]:.3f}s")
            else:
                msgs.append("Streak delay bets: off")
        if "streakbet_loss" in changes:
            self.streakbet_loss = self._parse_streak_delay(changes["streakbet_loss"])
            if self.streakbet_loss[0] > 0:
                msgs.append(f"Streak bet loss: every {self.streakbet_loss[0]} losses → x{self.streakbet_loss[1]:.3f}")
            else:
                msgs.append("Streak bet loss: off")
        if "basebet_pct" in changes:
            v = changes["basebet_pct"]
            self.basebet_pct = float(v) if v is not None else 0.0
            if self.basebet_pct > 0 and self.current_balance > 0:
                self._apply_basebet_pct()
                msgs.append(f"Basebet pct: {self.basebet_pct*100:.4f}% → base={self.base_bet:.8f}")
            else:
                msgs.append("Basebet pct: off")

        # Stop conditions: instant, checked every bet
        if "max_profit" in changes:
            v = changes["max_profit"]
            self.max_profit = float(v) if v is not None else None
            msgs.append(f"Max profit: {float(v):.8f}" if v is not None else "Max profit: off")
        if "max_loss" in changes:
            v = changes["max_loss"]
            self.max_loss = float(v) if v is not None else None
            msgs.append(f"Max loss: {float(v):.8f}" if v is not None else "Max loss: off")
        if "max_bets" in changes:
            v = changes["max_bets"]
            self.max_bets = int(v) if v is not None else None
            msgs.append(f"Max bets: {v}" if v is not None else "Max bets: off")
        if "max_wins" in changes:
            v = changes["max_wins"]
            self.max_wins = int(v) if v is not None else None
            msgs.append(f"Max wins: {v}" if v is not None else "Max wins: off")
        if "stop_on_balance" in changes:
            v = changes["stop_on_balance"]
            self.stop_on_balance = float(v) if v is not None else None
            msgs.append(f"Min balance: {float(v):.8f}" if v is not None else "Min balance: off")
        if "streak_reset_losses" in changes:
            v = changes["streak_reset_losses"]
            self.streak_reset_losses = int(v) if v is not None else None
            msgs.append(f"Streak reset: after {v} losses" if v is not None else "Streak reset: off")

        # Milestone settings: instant
        if "milestone_bets" in changes:
            self.milestone_bets = int(changes["milestone_bets"])
            msgs.append(f"Milestone bets: {self.milestone_bets}")
        if "milestone_wins" in changes:
            self.milestone_wins = int(changes["milestone_wins"])
            msgs.append(f"Milestone wins: {self.milestone_wins}")

        # Profit increment: instant
        # v1.21.0: 0 / ≤ 0 means off (no phantom bumps of 0).
        if "profit_increment" in changes:
            v = changes["profit_increment"]
            self.profit_increment = None if v is None or float(v) <= 0 else float(v)
            msgs.append(f"Profit increment: {float(self.profit_increment):.8f}" if self.profit_increment is not None else "Profit increment: off")
        if "profit_threshold" in changes:
            v = changes["profit_threshold"]
            self.profit_threshold = None if v is None or float(v) <= 0 else float(v)
            msgs.append(f"Profit threshold: {float(self.profit_threshold):.8f}" if self.profit_threshold else "Profit threshold: off")
        if ("profit_increment" in changes or "profit_threshold" in changes) \
                and self.profit_increment_scope == "run":
            if not self._increment_on():
                self._end_run("increment off")
                msgs.append("Run total ended (increment off)")
            elif self.run_first_session is None and self.session_id:
                self._run_base_origin = self.base_bet
                self._attach_run(self.session_id)
                self.next_profit_milestone = float(self.profit_threshold)
                msgs.append(f"Run total started at session #{self.session_id}")

        # ── Changes that require auto-bet restart ──
        needs_restart = False
        if "base_bet" in changes:
            self.base_bet = float(changes["base_bet"])
            if self._is_auto_game():
                # Auto-bet: defer restart until next win (safe recovery)
                self._pending_restart = True
                msgs.append(f"Base bet: {self.base_bet:.8f} (restarts on next win)")
            else:
                # Local strategy: don't kill recovery: applies on next win
                msgs.append(f"Base bet: {self.base_bet:.8f} (applies on next win)")
            needs_restart = False  # don't restart immediately
        if "multiplier" in changes:
            self.multiplier = float(changes["multiplier"])
            msgs.append(f"Multiplier: {self.multiplier:.2f}")
            needs_restart = True
        if "loss_mult" in changes:
            self.loss_mult = float(changes["loss_mult"])
            msgs.append(f"Loss mult: {self.loss_mult:.2f}")
            needs_restart = True
        if "win_mult" in changes:
            self.win_mult = float(changes["win_mult"])
            msgs.append(f"Win mult: {self.win_mult:.2f}")
            needs_restart = True
        if "strategy_key" in changes:
            self.strategy_key = changes["strategy_key"]
            self.strategy = changes.get("strategy", self.strategy)
            msgs.append(f"Strategy: {self.strategy}")
            needs_restart = True

        # Auto-bet: defer restart until next win (safe recovery)
        if needs_restart and self._is_auto_game() and self._auto_uuid:
            self._pending_restart = True
            msgs.append("Changes queued: auto-bet restarts on next win")

        if msgs:
            self._emit_lifecycle("reload", {"keys": sorted(changes)})
        return "; ".join(msgs) if msgs else "No changes applied"

    def pause(self):
        # v1.18.0: freeze uptime + start pause clock (mirrors CLI v2.28.4).
        if not self.paused:
            now = time.time()
            self.session_bet_ended_at = now
            self.session_pause_started_at = now
            self._emit_lifecycle("pause")
        self.paused = True

    def resume(self):
        # v1.18.0: clear freeze so uptime resumes advancing.
        if self.paused:
            self._emit_lifecycle("resume")
        self.session_bet_ended_at = None
        self.session_pause_started_at = None
        self.paused = False

    def get_status(self) -> dict:
        # Cache for ~250ms: milestone notifier and /status often fire within ms
        # of each other; avoid rebuilding the 50-field dict every time.
        now_mono = time.monotonic()
        if self._status_cache is not None and (now_mono - self._status_cache_ts) < 0.25:
            return self._status_cache
        # v1.18.0: freeze uptime at bet-end during pauses (mirrors CLI v2.28.4).
        now = time.time()
        ref = self.session_bet_ended_at if self.session_bet_ended_at else now
        elapsed = ref - self.session_start if self.session_start else 0
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        wr = f"{self.wins/self.total_bets*100:.1f}%" if self.total_bets > 0 else "-"

        # stop conditions
        stops = {}
        if self.max_profit is not None: stops["max_profit"] = self.max_profit
        if self.max_loss is not None:   stops["max_loss"] = self.max_loss
        if self.max_bets is not None:   stops["max_bets"] = self.max_bets
        if self.max_wins is not None:   stops["max_wins"] = self.max_wins
        if self.stop_on_balance is not None: stops["min_balance"] = self.stop_on_balance

        # strategy detail
        strat_detail = ""
        if self.strategy_key in ("2", "6", "7"):
            strat_detail = f"loss_mult: {self.loss_mult:.2f}x"
        elif self.strategy_key in ("3", "5"):
            strat_detail = f"win_mult: {self.win_mult:.2f}x"

        # rule descriptions
        rule_descs = []
        for r in self.custom_rules:
            d = r.to_dict()
            rule_descs.append(f"{d.get('trigger','')} {d.get('condition','')} {d.get('threshold','')}: {d.get('action','')} {d.get('value','')}")

        status = {
            "running": self.running,
            "paused": self.paused,
            "session_id": self.session_id,
            "uptime": f"{h}h {m}m {s}s",
            "uptime_sec": int(elapsed),
            "pause_remaining_sec": 0,
            "bets": self.total_bets,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": wr,
            "profit": self.profit,
            "wagered": self.wagered,
            "balance": self.current_balance,
            "start_balance": self.start_balance,
            "current_bet": self.current_bet,
            "highest_bet": self.highest_bet,
            "streak": self.current_streak,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
            "highest_balance": self.highest_balance,
            "lowest_balance": self.lowest_balance if self.lowest_balance != float("inf") else self.current_balance,
            "highest_win": self.highest_win,
            "biggest_loss": self.biggest_loss,
            "bps": self.bets_per_second,
            "bpm": self.bets_per_minute,
            "recent_bps": self.recent_bps,
            "recent_bpm": self.recent_bpm,
            "peak_bps": self.peak_bps,
            "low_bps": self.low_bps if self.low_bps != float("inf") else 0,
            "peak_bpm": self.peak_bpm,
            "low_bpm": self.low_bpm if self.low_bpm != float("inf") else 0,
            "base_bet": self.base_bet,
            "strategy": self.strategy,
            "strategy_key": self.strategy_key,
            "strategy_detail": strat_detail,
            "multiplier": self.multiplier,
            "currency": self.currency.upper(),
            "bet_delay": self.bet_delay,
            "stop_conditions": stops,
            "profit_increment": self.profit_increment,
            "profit_threshold": self.profit_threshold,
            "next_profit_milestone": self.next_profit_milestone,
            "profit_increment_scope": self.profit_increment_scope,
            "run_profit_total": self.run_profit_total(),
            "run_first_session": self.run_first_session,
            "run_bumps": self.run_bumps,
            "milestone_bets": self.milestone_bets,
            "streak_delay_loss": self.streak_delay_loss,
            "streak_delay_win": self.streak_delay_win,
            "streak_delay_bets": self.streak_delay_bets,
            "streakbet_loss": self.streakbet_loss,
            "basebet_pct": self.basebet_pct,
            "rule": self.rule,
            "bet_value": self.bet_value,
            "win_mult": self.win_mult,
            "loss_mult": self.loss_mult,
            "rules": rule_descs,
            "game": self.game,
            "game_label": GAME_LABELS.get(self.game, self.game),
            "auto_uuid": self._auto_uuid,
            "pending_restart": self._pending_restart,
            "pipeline": self.pipeline,
            "proxy": bool(self.proxy),
            "status": self.status,
            "last_error": self.last_error,
            "api_ms": self._last_api_ms,
            "api_avg_ms": (self._api_ms_total / self._api_ms_count) if self._api_ms_count > 0 else 0,
            "api_peak_ms": self.peak_api_ms,
            "overhead_ms": self._cycle_overhead_ms,
            "cycle_ms": self._cycle_ms,
            "rate_remaining": self.rate_remaining,
            "rate_limit": self.rate_limit,
            "auto_start_remaining": self._auto_start_remaining,
        }
        # API median (p50) and on-target % (api ≤ bet_delay). Explains the
        # Efficiency ceiling: 95% on-target → ~95% efficiency is the upper bound.
        # v2.28.0: shared percentile helper (same as CLI's --benchmark).
        p50 = percentile(sorted(self._api_samples), 0.50) if self._api_samples else 0
        on_target_pct = ((self._on_target_count / self._api_ms_count) * 100
                         if self._api_ms_count > 0 and self.bet_delay > 0 else None)
        status["api_p50_ms"] = p50
        status["on_target_pct"] = on_target_pct
        # Efficiency = recent bps / theoretical max bps (capped at 100%).
        # theoretical max = 1 / max(bet_delay, api_avg): whichever bounds the cycle.
        api_avg_s = ((self._api_ms_total / self._api_ms_count) / 1000.0) if self._api_ms_count > 0 else 0
        cycle_floor = max(self.bet_delay, api_avg_s)
        theoretical_max_bps = (1.0 / cycle_floor) if cycle_floor > 0 else 0
        efficiency_pct = (self.recent_bps / theoretical_max_bps * 100) if theoretical_max_bps > 0 else 0
        status["theoretical_max_bps"] = theoretical_max_bps
        status["efficiency_pct"] = min(100.0, efficiency_pct)
        self._status_cache = status
        self._status_cache_ts = now_mono
        return status

    # ── PIPELINED BETTING LOOP (auto-bet only) ─────────
    async def _pipelined_betting_loop(self):
        """Fire multiple auto/play requests concurrently for higher throughput.
        WARNING: Only safe with Flat strategy. Sequential strategies (Martingale etc)
        break when bets overlap because each concurrent bet sees different state."""
        depth = self.pipeline
        # Force pipeline=1 for sequential strategies: they require ordered execution
        if self.strategy_key != "1":  # not Flat
            logger.warning("User %d: Pipeline=%d but strategy=%s is sequential: forcing pipeline=1",
                          self.user_id, depth, self.strategy)
            depth = 1
            self.pipeline = 1
        logger.info("User %d: Session #%d started (pipeline=%d)", self.user_id, self.session_id, depth)
        sem = asyncio.Semaphore(depth)
        should_stop = False
        last_bet_state = "none"

        async def _one_bet():
            nonlocal should_stop
            async with sem:
                if not self.running or should_stop:
                    return None
                return await self._api_place_bet(self.current_bet)

        while self.running and not should_stop:
            if self.paused:
                self.status = "PAUSED"
                self._events_tick()
                await asyncio.sleep(0.3)
                continue

            if self.rate_wait:
                backoff = self.rate_backoff
                self.status = f"Rate limit: resuming in {backoff:.0f}s..."
                for _ in range(int(backoff * 4)):
                    if not self.running:
                        break
                    await asyncio.sleep(0.25)
                self.rate_wait = False
                self.rate_backoff = 2.0
                self.rate_remaining = 50
                continue

            # Check local stop conditions
            stop, reason = self._should_stop()
            if stop:
                should_stop = True
                self.running = False
                self.stop_reason = reason
                self.status = f"STOPPED: {reason}"
                break

            # bet pacing: minimum interval since the previous batch started
            if self.bet_delay > 0:
                wait = (self._last_bet_start_time + self.bet_delay) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            prev_bet_start = self._last_bet_start_time
            self._last_bet_start_time = time.monotonic()
            self._iter_start_mono = self._last_bet_start_time
            if prev_bet_start > 0:
                self._cycle_ms = (0.9 * self._cycle_ms
                                  + 0.1 * (self._last_bet_start_time - prev_bet_start) * 1000)

            # Fire a batch of concurrent requests
            self.status = f"Betting (pipeline={depth})... #{self.total_bets + 1}"
            batch_size = min(depth, 50)  # cap batch size
            tasks = [asyncio.create_task(_one_bet()) for _ in range(batch_size)]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if not self.running or should_stop:
                    break
                if isinstance(result, Exception) or result is None:
                    if self._insufficient_balance:
                        should_stop = True
                        self.running = False
                        self.stop_reason = f"Insufficient balance"
                        break
                    if self.rate_wait:
                        break  # will handle in next loop iteration
                    if self._session_expired:
                        self._session_expired = False
                        if await self._recover_expired_session():
                            self.consecutive_errors = 0
                            break  # restart loop iteration with fresh UUID
                        # Recovery failed: keep session alive, retry on next loop
                        logger.warning("User %d: Recovery failed; will retry on next iteration",
                                       self.user_id)
                        await asyncio.sleep(5.0)
                        break
                    self.consecutive_errors += 1
                    continue

                stop_reason = result.get("stopReason")
                if stop_reason:
                    should_stop = True
                    self.stop_reason = f"WolfBet auto-stop: {self._fmt_stop_reason(stop_reason)}"
                    break

                last_bet_state = self._process_bet_result(result)
                # Per-cycle overhead = iteration time minus API. Exp-smoothed.
                iter_ms = (time.monotonic() - self._iter_start_mono) * 1000
                overhead = max(0.0, iter_ms - self._last_api_ms)
                self._cycle_overhead_ms = 0.9 * self._cycle_overhead_ms + 0.1 * overhead

                # Streak delay
                streak_pause = self._get_streak_delay()
                if streak_pause > 0:
                    self.status = f"Streak delay {streak_pause:.3f}s (streak {self.current_streak})"
                    await asyncio.sleep(streak_pause)

                # Pending restart: wait for win before restarting auto-bet
                if self._pending_restart and last_bet_state == "win" and self._auto_uuid:
                    self._pending_restart = False
                    old_uuid = self._auto_uuid
                    await self._auto_stop()
                    if await self._auto_start():
                        logger.info("User %d: Deferred auto-bet restart on win: %s → %s",
                                    self.user_id, old_uuid[:8], self._auto_uuid[:8])
                    else:
                        should_stop = True
                        self.stop_reason = "Auto-bet restart failed"
                        break

                # Check stop conditions after each result
                stop, reason = self._should_stop()
                if stop:
                    should_stop = True
                    self.stop_reason = reason
                    break

            # periodic save + profit increment (between batches)
            await self._periodic_save()
            if self._maybe_profit_increment():
                if self._auto_uuid:
                    old_uuid = self._auto_uuid
                    await self._auto_stop()
                    if not await self._auto_start():
                        self.running = False
                        self.stop_reason = "Auto-bet restart failed after profit increment"
                        break
                    logger.info("User %d: Auto-bet restarted: %s → %s",
                                self.user_id, old_uuid[:8], self._auto_uuid[:8] if self._auto_uuid else "?")

            # milestone check
            if self.on_milestone and self.total_bets > 0 and last_bet_state:
                reason = self._check_milestone(last_bet_state)
                if reason:
                    try:
                        s = self.get_status()
                        s["milestone_reason"] = reason
                        self.on_milestone(s)
                    except Exception:
                        pass

            # progress log
            if self.total_bets % 500 == 0 and self.total_bets > 0:
                game_label = GAME_LABELS.get(self.game, self.game)
                logger.info("User %d: [%s] #%d bets=%-6d bps=%.1f P/L=%+.8f",
                            self.user_id, game_label, self.session_id,
                            self.total_bets, self.bets_per_second, self.profit)

        await self._session_cleanup()

    # ── BETTING LOOP ─────────────────────────────────────
    async def _betting_loop(self):
        # Use pipelined loop for auto-bet with pipeline > 1
        if self._is_auto_game() and self.pipeline > 1:
            return await self._pipelined_betting_loop()

        last_result = "none"
        logger.info("User %d: Session #%d started", self.user_id, self.session_id)
        self._last_successful_bet_time = time.monotonic()
        watchdog_warned = False

        while self.running:
            if self.paused:
                self.status = "PAUSED"
                self._events_tick()
                await asyncio.sleep(0.3)
                continue

            # Watchdog: no successful bet for too long → force-recover or kill
            stale = time.monotonic() - self._last_successful_bet_time
            if self._is_auto_game() and stale > 600:  # 10 min
                logger.warning("User %d: Watchdog: no bet for %.0fs, forcing recovery",
                               self.user_id, stale)
                if await self._recover_expired_session():
                    self._last_successful_bet_time = time.monotonic()
                    watchdog_warned = False
                    continue
                self.running = False
                self.stop_reason = (
                    f"Session abandoned: no successful bet for {stale:.0f}s "
                    f"and recovery failed")
                self.status = f"STOPPED: {self.stop_reason}"
                await asyncio.to_thread(self._flush_bets)
                await asyncio.to_thread(self._db_save_session, True)
                break
            elif self._is_auto_game() and stale > 180 and not watchdog_warned:
                logger.warning("User %d: Watchdog: %.0fs since last bet (still trying)",
                               self.user_id, stale)
                watchdog_warned = True
            elif stale < 60 and watchdog_warned:
                watchdog_warned = False

            # rate limit recovery: wait then resume betting
            if self.rate_wait:
                backoff = self.rate_backoff
                self.status = f"Rate limit: resuming in {backoff:.0f}s..."
                # Sleep in small chunks so stop() is responsive
                for _ in range(int(backoff * 4)):
                    if not self.running:
                        break
                    await asyncio.sleep(0.25)
                self.rate_wait = False
                self.rate_backoff = 2.0
                self.rate_remaining = 50
                continue

            # stop conditions
            stop, reason = self._should_stop()
            if stop:
                self.running = False
                self.stop_reason = reason
                self.status = f"STOPPED: {reason}"
                await asyncio.to_thread(self._flush_bets)
                await asyncio.to_thread(self._db_save_session, True)
                break

            # bet pacing: minimum interval since the previous bet started
            if self.bet_delay > 0:
                wait = (self._last_bet_start_time + self.bet_delay) - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            prev_bet_start = self._last_bet_start_time
            self._last_bet_start_time = time.monotonic()
            self._iter_start_mono = self._last_bet_start_time
            # Full cycle = T_start_N - T_start_N-1 (= 1/bps). Skip first iter.
            if prev_bet_start > 0:
                self._cycle_ms = (0.9 * self._cycle_ms
                                  + 0.1 * (self._last_bet_start_time - prev_bet_start) * 1000)

            # place bet
            self.status = f"Placing bet #{self.total_bets + 1}..."
            result = await self._api_place_bet(self.current_bet)

            if result is None:
                if self._insufficient_balance:
                    if self.on_error:
                        try:
                            self.on_error(f"Insufficient balance for bet {self.current_bet:.8f}")
                        except Exception:
                            pass
                    self.running = False
                    self.stop_reason = f"Insufficient balance for bet {self.current_bet:.8f}"
                    self.status = f"STOPPED: {self.stop_reason}"
                    await asyncio.to_thread(self._flush_bets)
                    await asyncio.to_thread(self._db_save_session, True)
                    break
                if self._session_expired:
                    self._session_expired = False
                    if await self._recover_expired_session():
                        self.consecutive_errors = 0
                        self.backoff_delay = 1.0
                        continue
                    logger.warning("User %d: Recovery failed; will retry on next iteration",
                                   self.user_id)
                    await asyncio.sleep(5.0)
                    continue
                self.consecutive_errors += 1
                self.backoff_delay = min(self.backoff_delay * 2, 30.0)
                await asyncio.sleep(self.backoff_delay)
                continue

            bet_data     = result.get("bet", {})
            balance_data = result.get("userBalance", {})
            bet_state    = bet_data.get("state", "loss")

            # Auto-bet: server may signal stop
            stop_reason = result.get("stopReason")
            if stop_reason and self._is_auto_game():
                self.running = False
                self.stop_reason = f"WolfBet auto-stop: {self._fmt_stop_reason(stop_reason)}"
                self.status = f"STOPPED: {self.stop_reason}"
                break

            bet_state = self._process_bet_result(result)
            self._last_successful_bet_time = time.monotonic()
            # Per-cycle overhead = full iteration time minus the API call.
            # Excludes bet_delay sleep (we measure from start of bet, after sleep).
            # Exp-smoothed so transient spikes don't dominate the reading.
            iter_ms = (self._last_successful_bet_time - self._iter_start_mono) * 1000
            overhead = max(0.0, iter_ms - self._last_api_ms)
            self._cycle_overhead_ms = 0.9 * self._cycle_overhead_ms + 0.1 * overhead

            # Streak delay: pause after N consecutive wins/losses
            streak_pause = self._get_streak_delay()
            if streak_pause > 0:
                self.status = f"Streak delay {streak_pause:.3f}s (streak {self.current_streak})"
                await asyncio.sleep(streak_pause)

            # Pending restart: wait for win before restarting auto-bet with new config
            if self._pending_restart and bet_state == "win" and self._is_auto_game() and self._auto_uuid:
                self._pending_restart = False
                old_uuid = self._auto_uuid
                await self._auto_stop()
                if await self._auto_start():
                    logger.info("User %d: Deferred auto-bet restart on win: %s → %s (base=%.8f)",
                                self.user_id, old_uuid[:8], self._auto_uuid[:8], self.base_bet)
                else:
                    self.running = False
                    self.stop_reason = "Auto-bet restart failed"
                    break

            # periodic progress log (every 500 bets)
            if self.total_bets % 500 == 0:
                game_label = GAME_LABELS.get(self.game, self.game)
                logger.info("User %d: [%s] #%d bets=%-6d bps=%.1f P/L=%+.8f bal=%.8f",
                            self.user_id, game_label, self.session_id,
                            self.total_bets, self.bets_per_second, self.profit, self.current_balance)

            # periodic session save (every 60s)
            await self._periodic_save()

            # profit-based base bet increment
            if self._maybe_profit_increment():
                # Auto-bet: restart session with new base bet (server doesn't know about increment)
                if self._is_auto_game() and self._auto_uuid:
                    old_uuid = self._auto_uuid
                    await self._auto_stop()
                    if not await self._auto_start():
                        logger.error("User %d: Auto-bet restart after profit increment failed", self.user_id)
                        self.running = False
                        self.stop_reason = "Auto-bet restart failed after profit increment"
                        break
                    logger.info("User %d: Auto-bet restarted for profit increment: %s → %s (base_bet=%.8f)",
                                self.user_id, old_uuid[:8], self._auto_uuid[:8] if self._auto_uuid else "?", self.base_bet)

            if self.strategy_key == "6":
                self._wolfrider_switch()
            if self.strategy_key == "8" and self.custom_rules:
                self._apply_rules(bet_state)

            raw_next = self._compute_next_bet(bet_state)
            self.current_bet = max(self.base_bet, raw_next)
            sbl_factor = self._get_streakbet_loss_factor()
            if sbl_factor != 1.0:
                self.current_bet = self.current_bet * sbl_factor
            last_result = bet_state

            # milestone callbacks
            if self.on_milestone:
                reason = self._check_milestone(bet_state)
                if reason:
                    try:
                        s = self.get_status()
                        s["milestone_reason"] = reason
                        self.on_milestone(s)
                        logger.info("User %d: Milestone fired: %s", self.user_id, reason)
                    except Exception as e:
                        logger.error("User %d: Milestone callback failed: %s", self.user_id, e)

        await self._session_cleanup()

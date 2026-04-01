"""BettingEngine — per-user betting instance running as async task."""

import asyncio
import json
import time
import sqlite3
import threading
import httpx
from datetime import datetime
from collections import deque
from typing import Optional, List

from .config import API_BASE, logger
from core.database import init_db, migrate_db, db_connect, cleanup_old_bets
from core.strategy import StrategyRule, load_rules_from_text
from core.engine import compute_next_bet, wolfrider_switch, apply_action, evaluate_rules

import hashlib

# ── Game endpoint configuration ──
# game_key → (api_path, api_version, rate_limit_default, needs_hash)
GAME_ENDPOINTS = {
    "dice":           ("/v1/bet/place",         1, 480,  False),
    "dice-manual":    ("/v1/dice/manual/play",  1, 720,  True),
    "limbo":          ("/v2/limbo/manual/play",  2, 500,  True),
    "limbo-auto":     ("/v2/limbo/auto/play",    2, 4000, True),
    "dice-auto":      ("/v1/dice/auto/play",     1, 4000, True),
}
GAME_LABELS = {
    "dice":           "Dice (API v1)",
    "dice-manual":    "Dice Manual (v1)",
    "limbo":          "Limbo (v2)",
    "limbo-auto":     "Limbo Auto (v2)",
    "dice-auto":      "Dice Auto (v1)",
}

# Auto-bet start/stop endpoints
AUTO_ENDPOINTS = {
    "limbo-auto": {"start": "/v2/limbo/auto/start", "stop": "/v2/limbo/auto/stop"},
    "dice-auto":  {"start": "/v1/dice/auto/start",  "stop": "/v1/dice/auto/stop"},
}


def _wolfbet_hash(body_str: str = "") -> str:
    """Generate WolfBet x-hash-api: interleave(md5(body), md5(nonce))."""
    md5_body = hashlib.md5(body_str.encode()).hexdigest()
    md5_nonce = hashlib.md5(str(int(time.time() * 1000)).encode()).hexdigest()
    return "".join(a + b for a, b in zip(md5_body, md5_nonce))

# ── Batching / periodic save constants ──────────────────
BET_BATCH_SIZE     = 50      # flush bets + session stats to DB every N bets
SESSION_SAVE_SECS  = 30      # fallback: save session stats every N seconds
REQUEST_TIMEOUT    = httpx.Timeout(15.0, connect=5.0)
MAX_TIMEOUT_STREAK = 3       # recreate HTTP session after this many consecutive timeouts


class BettingEngine:
    """Self-contained betting engine for one user. Runs in its own thread."""

    def __init__(self, user_id: int, db_path: str, api_key: str, config: dict):
        self.user_id = user_id
        self.db_path = db_path
        self.lock    = threading.Lock()

        # ── config (use `or` to handle None from presets) ──
        self.api_key        = api_key
        self.proxy          = config.get("proxy") or None
        self.currency       = config.get("currency") or "usdt"
        self.game           = config.get("game") or "dice"
        self.rule           = config.get("rule") or "over"
        self.multiplier     = float(config.get("multiplier") or 2.01)
        self.initial_multiplier = self.multiplier
        self.bet_value      = float(config.get("bet_value") or 50.49)
        self.base_bet       = float(config.get("base_bet") or 0.0001)
        self.current_bet    = self.base_bet
        self.strategy       = config.get("strategy") or "Martingale"
        self.strategy_key   = config.get("strategy_key") or "2"
        self.win_mult       = float(config.get("win_mult") or 1.0)
        self.loss_mult      = float(config.get("loss_mult") or 2.0)
        self.bet_delay      = float(config.get("bet_delay") or 0)
        self.delay_martin_threshold = int(config.get("delay_martin_threshold") or 3)

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

        # ── session state ──
        self.running         = False
        self.paused          = False
        self.session_id      = None
        self.session_start   = 0.0
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
        self._last_api_ms          = 0.0
        self._api_ms_total         = 0.0
        self._api_ms_count         = 0

        # ── strategy internals ──
        self.dalembert_unit  = 0
        self.paroli_count    = 0

        # ── profit-based base bet increment ──
        self.profit_increment     = config.get("profit_increment")    # amount to add to base_bet
        self.profit_threshold     = config.get("profit_threshold")    # profit interval that triggers
        self.next_profit_milestone = float(self.profit_threshold) if self.profit_threshold else 0.0

        # ── milestone config ──
        self.milestone_bets   = int(config.get("milestone_bets") or 100)
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
        self._db_connection: Optional[sqlite3.Connection] = None  # persistent connection

        # ── auto-bet state ──
        self._auto_uuid: Optional[str] = None

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
        """Static headers for the HTTP client (no hash — added per-request)."""
        return self._base_headers()

    def _request_headers(self, body_str: str = "") -> dict:
        """Per-request headers including dynamic x-hash-api."""
        ep = GAME_ENDPOINTS.get(self.game)
        if ep and ep[3]:  # needs_hash
            return {
                "x-hash-api": _wolfbet_hash(body_str),
                "x-client-type": "Web-Application",
            }
        return {}

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
        """Start an auto-bet session on WolfBet, returns True on success."""
        endpoints = AUTO_ENDPOINTS.get(self.game)
        if not endpoints:
            self.last_error = f"No auto-bet endpoint for {self.game}"
            return False
        url = f"{API_BASE}{endpoints['start']}"
        payload = self._build_auto_start_payload()
        body_str = json.dumps(payload, separators=(",", ":"))
        extra_headers = self._request_headers(body_str)
        try:
            r = await self._http.post(url, json=payload, headers=extra_headers)
            if r.status_code == 200:
                data = r.json()
                uuid = data.get("autoBet", {}).get("uuid")
                if uuid:
                    self._auto_uuid = uuid
                    logger.info("User %d: Auto-bet started, uuid=%s", self.user_id, uuid)
                    return True
                self.last_error = f"No UUID in auto/start response: {data}"
            else:
                self.last_error = f"Auto start HTTP {r.status_code}: {r.text[:120]}"
        except Exception as e:
            self.last_error = f"Auto start failed: {e}"
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
        return self.game in AUTO_ENDPOINTS

    def _build_auto_start_payload(self) -> dict:
        """Build the payload for auto/start — includes strategy config."""
        base = {
            "currency":   self.currency,
            "amount":     f"{self.base_bet:.8f}",
            "multiplier": f"{self.multiplier}",
            "config":     self._build_auto_config(),
        }
        if self.game == "limbo-auto":
            base["game"] = "limbo"
        elif self.game == "dice-auto":
            base["game"] = "dice"
            base["rule"] = self.rule
            base["bet_value"] = f"{self.bet_value}"
        return base

    def _build_auto_config(self) -> list:
        """Translate our strategy into WolfBet auto-bet config format."""
        if self.strategy_key == "1":  # Flat
            return [{"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]},
                    {"command": [{"name": "resetAmount"}], "when": [{"name": "loss", "value": 1, "type": "every"}]}]
        elif self.strategy_key == "2":  # Martingale
            return [{"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]},
                    {"command": [{"name": "increaseAmount", "value": f"{self.loss_mult}"}], "when": [{"name": "loss", "value": 1, "type": "every"}]}]
        elif self.strategy_key == "3":  # Anti-Martingale
            return [{"command": [{"name": "increaseAmount", "value": f"{self.win_mult}"}], "when": [{"name": "win", "value": 1, "type": "every"}]},
                    {"command": [{"name": "resetAmount"}], "when": [{"name": "loss", "value": 1, "type": "every"}]}]
        # Default: flat (resetAmount on every result)
        return [{"command": [{"name": "resetAmount"}], "when": [{"name": "win", "value": 1, "type": "every"}]}]

    def _build_bet_payload(self, amount: float) -> dict:
        gk = self.game
        if gk == "dice":
            return {
                "currency":   self.currency,
                "game":       "dice",
                "amount":     f"{amount:.8f}",
                "rule":       self.rule,
                "multiplier": f"{self.multiplier}",
                "bet_value":  f"{self.bet_value}",
            }
        elif gk == "dice-manual":
            return {
                "currency":   self.currency,
                "game":       "dice",
                "amount":     f"{amount:.8f}",
                "rule":       self.rule,
                "multiplier": f"{self.multiplier}",
                "bet_value":  f"{self.bet_value}",
                "auto":       1,
            }
        elif gk == "limbo":
            return {
                "currency":   self.currency,
                "game":       "limbo",
                "amount":     f"{amount:.8f}",
                "multiplier": f"{self.multiplier}",
                "auto":       1,
            }
        elif gk in ("limbo-auto", "dice-auto"):
            return {"uuid": self._auto_uuid}
        return {}

    async def _api_place_bet(self, amount: float) -> Optional[dict]:
        payload = self._build_bet_payload(amount)
        ep = GAME_ENDPOINTS.get(self.game, ("/v1/bet/place", 1, 480, False))
        url = f"{API_BASE}{ep[0]}"
        body_str = json.dumps(payload, separators=(",", ":"))
        extra_headers = self._request_headers(body_str)
        try:
            t0 = time.time()
            r = await self._http.post(url, json=payload, headers=extra_headers)
            self._last_api_ms = (time.time() - t0) * 1000
            self._update_rate_limit(r)
            self._consecutive_timeouts = 0
            if r.status_code == 200:
                data = r.json()
                if "error" in data:
                    err_msg = data["error"].get("message", "") if isinstance(data["error"], dict) else str(data["error"])
                    if "insufficient balance" in err_msg.lower():
                        self.last_error = "Insufficient balance"
                        self._insufficient_balance = True
                        return None
                    self.last_error = f"API error: {err_msg[:120]}"
                    return None
                self._api_ms_total += self._last_api_ms
                self._api_ms_count += 1
                return data
            elif r.status_code == 429:
                self.rate_wait = True
                self.rate_backoff = 2.0
                self.last_error = "Rate limit 429"
            else:
                self.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
        except (httpx.TimeoutException, httpx.ConnectError):
            self._consecutive_timeouts += 1
            self.last_error = f"Request timeout (#{self._consecutive_timeouts})"
            if self._consecutive_timeouts >= MAX_TIMEOUT_STREAK:
                logger.warning("User %d: %d consecutive timeouts — recreating HTTP client",
                               self.user_id, self._consecutive_timeouts)
                await self._recreate_http()
                self._consecutive_timeouts = 0
        except Exception as e:
            err_str = str(e).lower()
            if "insufficient" in err_str or "balance" in err_str:
                self.last_error = "Insufficient balance"
                self._insufficient_balance = True
            else:
                self.last_error = str(e)[:120]
        return None

    # ── DB ───────────────────────────────────────────────
    def _get_conn(self) -> sqlite3.Connection:
        """Return persistent DB connection, creating if needed."""
        if self._db_connection is None:
            self._db_connection = db_connect(self.db_path)
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
        if self.profit_threshold and self.profit_increment:
            snap["profit_threshold"] = self.profit_threshold
            snap["profit_increment"] = self.profit_increment
        if self.custom_rules:
            snap["rules"] = [r.to_dict() for r in self.custom_rules]
        return json.dumps(snap)

    def _db_start_session(self) -> int:
        # Use a temporary connection — start() runs in the main thread,
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
        """Queue a bet row for batched insertion."""
        self._bet_queue.append((
            self.session_id, datetime.now().isoformat(),
            float(bet_data.get("amount", 0)), float(bet_data.get("multiplier", 0)),
            self.rule, float(bet_data.get("bet_value", 0)),
            float(bet_data.get("result_value", 0)), bet_data.get("state", ""),
            profit, balance,
        ))
        if len(self._bet_queue) >= BET_BATCH_SIZE:
            self._flush_bets()

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
        # always save session stats when flushing bets — keeps both in sync
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
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?
                    WHERE id=?
                """, (
                    datetime.now().isoformat(),
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
                        peak_bps=?, low_bps=?, peak_bpm=?, low_bpm=?
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
                    self.session_id,
                ))
            conn.commit()
            self._last_session_save = time.time()
        except Exception as e:
            logger.error("User %d: DB save_session failed: %s", self.user_id, e)

    def _periodic_save(self):
        """Flush bets and save session stats periodically."""
        now = time.time()
        if now - self._last_session_save >= SESSION_SAVE_SECS:
            self._flush_bets()
            self._db_save_session()

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
        return False, ""

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
            self.last_error = f"Connection failed: {e}"
            return False

        bal = 0.0
        for b in balances:
            if b.get("currency", "").lower() == self.currency.lower():
                bal = float(b.get("amount", 0))
                break
        if bal <= 0 and self.base_bet > 0:
            self.last_error = f"No {self.currency.upper()} balance found"
            return False

        init_db(self.db_path)
        migrate_db(self.db_path)
        cleanup_old_bets(self.db_path)
        self.start_balance   = bal
        self.current_balance = bal
        self.highest_balance = bal
        self.lowest_balance  = bal
        self.session_start   = time.time()
        self._last_session_save = time.time()
        self.session_id      = self._db_start_session()
        self.running         = True
        self.status          = "Starting…"

        if self._is_auto_game():
            if not await self._auto_start():
                return False

        self._task = asyncio.create_task(self._betting_loop())
        return True

    def snapshot_state(self) -> dict:
        """Capture full engine state for resume after restart."""
        rules_ser = json.dumps([r.to_dict() for r in self.custom_rules]) if self.custom_rules else ""
        config = {
            "api_key": self.api_key,
            "proxy": self.proxy,
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
            "profit_increment": self.profit_increment,
            "profit_threshold": self.profit_threshold,
            "milestone_bets": self.milestone_bets,
            "milestone_wins": self.milestone_wins,
            "milestone_losses": self.milestone_losses,
            "milestone_profit": self.milestone_profit,
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
            self.last_error = f"Resume connection failed: {e}"
            return False

        self.running = True
        self.paused = False
        self.status = "Resumed"
        self._last_session_save = time.time()
        elapsed = time.time() - self.session_start if self.session_start else 0
        if elapsed > 0 and self.total_bets > 0:
            self.bets_per_second = self.total_bets / elapsed
            self.bets_per_minute = self.bets_per_second * 60
        self._task = asyncio.create_task(self._betting_loop())
        logger.info("User %d: Resumed session #%d", self.user_id, self.session_id)
        return True

    def stop(self):
        self.running = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def get_status(self) -> dict:
        elapsed = time.time() - self.session_start if self.session_start else 0
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        wr = f"{self.wins/self.total_bets*100:.1f}%" if self.total_bets > 0 else "—"

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
            strat_detail = f"loss_mult: {self.loss_mult}x"
        elif self.strategy_key in ("3", "5"):
            strat_detail = f"win_mult: {self.win_mult}x"

        # rule descriptions
        rule_descs = []
        for r in self.custom_rules:
            d = r.to_dict()
            rule_descs.append(f"{d.get('trigger','')} {d.get('condition','')} {d.get('threshold','')}: {d.get('action','')} {d.get('value','')}")

        return {
            "running": self.running,
            "paused": self.paused,
            "session_id": self.session_id,
            "uptime": f"{h}h {m}m {s}s",
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
            "rules": rule_descs,
            "game": self.game,
            "game_label": GAME_LABELS.get(self.game, self.game),
            "proxy": bool(self.proxy),
            "status": self.status,
            "last_error": self.last_error,
            "api_ms": self._last_api_ms,
            "api_avg_ms": (self._api_ms_total / self._api_ms_count) if self._api_ms_count > 0 else 0,
        }

    # ── BETTING LOOP ─────────────────────────────────────
    async def _betting_loop(self):
        last_result = "none"
        logger.info("User %d: Session #%d started", self.user_id, self.session_id)

        while self.running:
            if self.paused:
                self.status = "PAUSED"
                await asyncio.sleep(0.3)
                continue

            # rate limit recovery — wait then resume betting
            if self.rate_wait:
                backoff = self.rate_backoff
                self.status = f"Rate limit — resuming in {backoff:.0f}s…"
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
                self._flush_bets()
                self._db_save_session(final=True)
                break

            # bet pacing
            if self.bet_delay > 0:
                await asyncio.sleep(self.bet_delay)

            # place bet
            self.status = f"Placing bet #{self.total_bets + 1}…"
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
                    self._flush_bets()
                    self._db_save_session(final=True)
                    break
                self.consecutive_errors += 1
                self.backoff_delay = min(self.backoff_delay * 2, 30.0)
                await asyncio.sleep(self.backoff_delay)
                continue

            bet_data     = result.get("bet", {})
            balance_data = result.get("userBalance", {})
            bet_state    = bet_data.get("state", "loss")
            result_value = float(bet_data.get("result_value", 0))
            amount_used  = float(bet_data.get("amount", self.current_bet))
            new_balance  = float(balance_data.get("amount", self.current_balance))
            raw_profit   = float(bet_data.get("profit", 0))

            with self.lock:
                self.consecutive_errors = 0
                self.backoff_delay = 1.0
                self.total_bets += 1
                self.wagered    += amount_used
                self.profit     += raw_profit
                self.current_balance = new_balance

                if bet_state == "win":
                    self.wins += 1
                    if raw_profit > self.highest_win: self.highest_win = raw_profit
                    self.current_streak = max(self.current_streak, 0) + 1
                    self.max_win_streak = max(self.max_win_streak, self.current_streak)
                else:
                    self.losses += 1
                    if amount_used > self.biggest_loss: self.biggest_loss = amount_used
                    self.current_streak = min(self.current_streak, 0) - 1
                    self.max_loss_streak = max(self.max_loss_streak, abs(self.current_streak))

                if amount_used > self.highest_bet: self.highest_bet = amount_used
                if new_balance > self.highest_balance: self.highest_balance = new_balance
                if new_balance < self.lowest_balance: self.lowest_balance = new_balance

                if self.total_bets % 5 == 0:
                    self.profit_history.append(self.profit)

                self.recent_bets.append({
                    "n": self.total_bets, "time": datetime.now().isoformat(),
                    "amt": amount_used, "roll": result_value,
                    "state": bet_state, "pnl": raw_profit, "bal": new_balance,
                })

                now = time.time()
                elapsed = now - self.session_start
                if elapsed > 0:
                    self.bets_per_second = self.total_bets / elapsed
                    self.bets_per_minute = self.bets_per_second * 60

                sec_key = int(now)
                min_key = int(now) // 60
                if sec_key != self._current_sec:
                    if self._current_sec > 0 and self._bets_this_sec > 0:
                        if self._bets_this_sec > self.peak_bps: self.peak_bps = self._bets_this_sec
                        if self._bets_this_sec < self.low_bps: self.low_bps = self._bets_this_sec
                    self._current_sec = sec_key
                    self._bets_this_sec = 1
                else:
                    self._bets_this_sec += 1
                if min_key != self._current_min:
                    if self._current_min > 0 and self._bets_this_min > 0:
                        if self._bets_this_min > self.peak_bpm: self.peak_bpm = self._bets_this_min
                        if self._bets_this_min < self.low_bpm: self.low_bpm = self._bets_this_min
                    self._current_min = min_key
                    self._bets_this_min = 1
                else:
                    self._bets_this_min += 1

                sign  = "+" if raw_profit >= 0 else ""
                emoji = "W" if bet_state == "win" else "L"
                self.status = f"{emoji} {'WIN' if bet_state=='win' else 'LOSS'} | Roll: {result_value:.2f} | P/L: {sign}{raw_profit:.8f}"
                self.last_error = ""

            # queue bet for batched DB write
            self._queue_bet(bet_data, raw_profit, new_balance)

            # periodic progress log (every 500 bets)
            if self.total_bets % 500 == 0:
                game_label = GAME_LABELS.get(self.game, self.game)
                logger.info("User %d: [%s] #%d bets=%-6d bps=%.1f P/L=%+.8f bal=%.8f",
                            self.user_id, game_label, self.session_id,
                            self.total_bets, self.bets_per_second, self.profit, self.current_balance)

            # periodic session save (every 60s)
            self._periodic_save()

            # profit-based base bet increment
            if (self.profit_increment and self.profit_threshold
                    and self.profit >= self.next_profit_milestone):
                self.base_bet += self.profit_increment
                self.next_profit_milestone += self.profit_threshold
                logger.info("User %d: PROFIT INCREMENT base_bet → %.8f (next at %.8f profit)",
                            self.user_id, self.base_bet, self.next_profit_milestone)

            if self.strategy_key == "6":
                self._wolfrider_switch()
            if self.strategy_key == "8" and self.custom_rules:
                self._apply_rules(bet_state)

            raw_next = self._compute_next_bet(bet_state)
            self.current_bet = max(self.base_bet, raw_next)
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

        # session ended — stop auto-bet if active, flush DB
        if self._auto_uuid:
            await self._auto_stop()
        self._flush_bets()
        if self.session_id:
            self._db_save_session(final=True)
        self._close_conn()
        if self._http:
            await self._http.aclose()
        logger.info("User %d: Session #%d ended — %s", self.user_id, self.session_id, self.stop_reason or "stopped")
        if self.on_stop:
            try:
                self.on_stop(self.stop_reason or "Stopped by user")
            except Exception:
                pass

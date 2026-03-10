#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║   🐺  WolfBet AutoBot v2.0  — VPS Terminal Dashboard    ║
║   Auto-betting engine with live TUI & session logging   ║
╚══════════════════════════════════════════════════════════╝
"""

import os, sys, json, time, sqlite3, threading, signal, random, string, argparse, logging, re
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, List
from logging.handlers import TimedRotatingFileHandler

try:
    import requests
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    # from rich.live import Live  # replaced by direct ANSI rendering
    from rich.prompt import Prompt, Confirm
    from rich.align import Align
    from rich.rule import Rule
    from rich import box
except ImportError:
    print("Missing dependencies. Run:  pip install requests rich")
    sys.exit(1)

# ══════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════
API_BASE     = "https://wolfbet.com/api/v1"
DB_PATH      = os.path.expanduser("~/.wolfbet_autobot.db")
CONFIG_PATH  = os.path.expanduser("~/.wolfbet_autobot.json")
STATE_PATH   = os.path.expanduser("~/.wolfbet_autobot_live.json")
PID_PATH     = os.path.expanduser("~/.wolfbet_autobot.pid")
PRESET_PATH  = os.path.expanduser("~/.wolfbet_presets.json")
LOG_DIR      = os.path.expanduser("~/.wolfbet_logs")
VERSION      = "2.0.0"

# ── Daily rotating logger ──
# Default: WARNING (production). Use --verbose for DEBUG.
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("wolfbet")
logger.setLevel(logging.WARNING)
_log_handler = TimedRotatingFileHandler(
    os.path.join(LOG_DIR, "wolfbet.log"),
    when="midnight", backupCount=30, utc=False,
)
_log_handler.suffix = "%Y-%m-%d"
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
logger.addHandler(_log_handler)

CURRENCIES  = ["btc", "eth", "ltc", "doge", "trx", "bch", "xrp", "usdt", "bnb", "ada", "matic"]

STRATEGIES  = {
    "1": ("Flat Bet",        "Same amount every single bet — low risk, low variance"),
    "2": ("Martingale",      "Double bet on loss, reset to base on win — classic high-risk"),
    "3": ("Anti-Martingale", "Double bet on win, reset to base on loss — ride hot streaks"),
    "4": ("D'Alembert",      "Add 1 unit on loss, remove 1 unit on win — gentler progression"),
    "5": ("Paroli (3-step)", "Double on win up to 3x then reset — safer streak play"),
    "6": ("WolfRider",      "2.01x on loss, reset on win + switch over/under on bet 2/6/9"),
    "7": ("Delay Martingale", "Flat for N losses then double — delays escalation"),
}

# ══════════════════════════════════════════════════════════
#  DATABASE SETUP
# ══════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    TEXT,
            ended_at      TEXT,
            currency      TEXT,
            game          TEXT,
            strategy      TEXT,
            base_bet      REAL,
            multiplier    REAL,
            total_bets    INTEGER DEFAULT 0,
            wins          INTEGER DEFAULT 0,
            losses        INTEGER DEFAULT 0,
            profit        REAL    DEFAULT 0,
            wagered       REAL    DEFAULT 0,
            start_balance REAL,
            end_balance   REAL,
            max_win_streak  INTEGER DEFAULT 0,
            max_loss_streak INTEGER DEFAULT 0,
            highest_balance REAL DEFAULT 0,
            lowest_balance  REAL DEFAULT 0,
            highest_win     REAL DEFAULT 0,
            biggest_loss    REAL DEFAULT 0,
            bets_per_minute REAL DEFAULT 0,
            bets_per_second REAL DEFAULT 0,
            peak_bps        REAL DEFAULT 0,
            low_bps         REAL DEFAULT 0,
            peak_bpm        REAL DEFAULT 0,
            low_bpm         REAL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   INTEGER,
            timestamp    TEXT,
            amount       REAL,
            multiplier   REAL,
            rule         TEXT,
            bet_value    REAL,
            result_value REAL,
            state        TEXT,
            profit       REAL,
            balance_after REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    # Migrate existing databases — add new columns if missing
    for col, defn in [
        ("highest_balance", "REAL DEFAULT 0"),
        ("lowest_balance",  "REAL DEFAULT 0"),
        ("highest_win",     "REAL DEFAULT 0"),
        ("biggest_loss",    "REAL DEFAULT 0"),
        ("bets_per_minute", "REAL DEFAULT 0"),
        ("bets_per_second", "REAL DEFAULT 0"),
        ("peak_bps",        "REAL DEFAULT 0"),
        ("low_bps",         "REAL DEFAULT 0"),
        ("peak_bpm",        "REAL DEFAULT 0"),
        ("low_bpm",         "REAL DEFAULT 0"),
    ]:
        try:
            c.execute(f"ALTER TABLE sessions ADD COLUMN {col} {defn}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    conn.close()

# ══════════════════════════════════════════════════════════
#  CONFIG PERSISTENCE
# ══════════════════════════════════════════════════════════
_CONFIG_KEYS = [
    "api_key", "currency", "game", "rule", "multiplier", "bet_value",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "profit_increment", "profit_threshold",
]

def save_config():
    data = {k: getattr(state, k) for k in _CONFIG_KEYS}
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)

def load_config() -> bool:
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        for k in _CONFIG_KEYS:
            if k in data:
                setattr(state, k, data[k])
        state.current_bet = state.base_bet
        # init profit milestone
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold
        # parse rules if present
        if state.custom_rules_text:
            state.custom_rules = parse_rules(state.custom_rules_text)
        return True
    except Exception:
        return False

# ══════════════════════════════════════════════════════════
#  PRESETS
# ══════════════════════════════════════════════════════════
_PRESET_KEYS = [
    "currency", "game", "rule", "multiplier", "bet_value",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "profit_increment", "profit_threshold",
]

def _load_all_presets() -> dict:
    if not os.path.exists(PRESET_PATH):
        return {}
    try:
        with open(PRESET_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_all_presets(presets: dict):
    with open(PRESET_PATH, "w") as f:
        json.dump(presets, f, indent=2)

def save_preset(name: str):
    presets = _load_all_presets()
    presets[name] = {k: getattr(state, k) for k in _PRESET_KEYS}
    _save_all_presets(presets)

def load_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    data = presets[name]
    for k in _PRESET_KEYS:
        if k in data:
            setattr(state, k, data[k])
    state.current_bet = state.base_bet
    if state.profit_threshold:
        state.next_profit_milestone = state.profit_threshold
    if state.custom_rules_text:
        state.custom_rules = parse_rules(state.custom_rules_text)
    return True

def delete_preset(name: str) -> bool:
    presets = _load_all_presets()
    if name not in presets:
        return False
    del presets[name]
    _save_all_presets(presets)
    return True

def list_presets() -> dict:
    return _load_all_presets()

# ══════════════════════════════════════════════════════════
#  RULE-BASED STRATEGY ENGINE
# ══════════════════════════════════════════════════════════
class StrategyRule:
    """A single rule: onEvery<N><trigger><Action> [params]"""
    __slots__ = ("every_n", "trigger", "action", "value", "raw")

    def __init__(self, every_n: int, trigger: str, action: str, value: float = 0.0, raw: str = ""):
        self.every_n = every_n      # trigger every N occurrences
        self.trigger = trigger      # "win", "lose", "bet"
        self.action  = action       # "reset", "stop", "increase", "switch"
        self.value   = value        # for increase: percentage (101 means *2.01)
        self.raw     = raw          # original text

    def __repr__(self):
        return f"Rule({self.raw})"

def parse_rules(text: str) -> List[StrategyRule]:
    """Parse rule text like:
    onEvery1winReset amount
    onEvery100000winStop
    onEvery1loseIncrease amount by 101%
    onEvery2betSwitch over/under
    """
    rules = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(
            r'onEvery(\d+)(win|lose|bet)(Reset|Stop|Increase|Switch)\s*(.*)',
            line, re.IGNORECASE
        )
        if not m:
            logger.warning("Could not parse rule: %s", line)
            continue
        n       = int(m.group(1))
        trigger = m.group(2).lower()
        action  = m.group(3).lower()
        params  = m.group(4).strip()
        value   = 0.0

        if action == "increase":
            pct = re.search(r'(\d+(?:\.\d+)?)%', params)
            if pct:
                value = float(pct.group(1))

        rules.append(StrategyRule(n, trigger, action, value, raw=line))

    return rules

def apply_rules(bet_state: str):
    """Apply rule-based strategy rules after a bet result.
    bet_state is 'win' or 'loss'."""
    for rule in state.custom_rules:
        # determine which counter to check
        if rule.trigger == "win" and bet_state == "win":
            count = state.wins
        elif rule.trigger == "lose" and bet_state == "loss":
            count = state.losses
        elif rule.trigger == "bet":
            count = state.total_bets
        else:
            continue

        if count > 0 and count % rule.every_n == 0:
            if rule.action == "reset":
                state.current_bet = state.base_bet
            elif rule.action == "stop":
                state.running = False
                state.status = f"Rule stop: every {rule.every_n} {rule.trigger}(s)"
                logger.warning("RULE STOP triggered: %s", rule.raw)
            elif rule.action == "increase":
                state.current_bet *= (1 + rule.value / 100)
            elif rule.action == "switch":
                state.rule = "under" if state.rule == "over" else "over"
                win_chance = 99.0 / state.multiplier
                if state.rule == "over":
                    state.bet_value = round(100.0 - win_chance - 0.01, 2)
                else:
                    state.bet_value = round(win_chance, 2)
                logger.debug("RULE SWITCH → %s  bet_value=%s", state.rule, state.bet_value)

# ══════════════════════════════════════════════════════════
#  BOT STATE — single shared object
# ══════════════════════════════════════════════════════════
class BotState:
    def __init__(self):
        self.lock = threading.Lock()

        # ── runtime flags ──
        self.running  = False
        self.paused   = False
        self.stopping = False

        # ── config ──
        self.api_key        = ""
        self.currency       = "usdt"
        self.game           = "dice"
        self.rule           = "over"
        self.multiplier     = 2.01
        self.bet_value      = 50.49
        self.base_bet       = 0.0001
        self.current_bet    = 0.0001
        self.strategy       = "Martingale"
        self.strategy_key   = "2"
        self.win_mult       = 1.0
        self.loss_mult      = 2.0

        # ── rule-based strategy ──
        self.custom_rules: List[StrategyRule] = []
        self.custom_rules_text: str = ""

        # ── stop conditions ──
        self.max_profit      : Optional[float] = None
        self.max_loss        : Optional[float] = None
        self.max_bets        : Optional[int]   = None
        self.max_wins        : Optional[int]   = None
        self.stop_on_balance : Optional[float] = None

        # ── session info ──
        self.session_id     = None
        self.session_start  = time.time()
        self.total_bets     = 0
        self.wins           = 0
        self.losses         = 0
        self.profit         = 0.0
        self.wagered        = 0.0
        self.start_balance  = 0.0
        self.current_balance= 0.0

        # ── streaks ──
        self.current_streak  = 0   # + = win streak, – = loss streak
        self.max_win_streak  = 0
        self.max_loss_streak = 0

        # ── extremes ──
        self.highest_bet     = 0.0
        self.highest_win     = 0.0
        self.biggest_loss    = 0.0
        self.highest_balance = 0.0
        self.lowest_balance  = float("inf")

        # ── bet pacing ──
        self.bet_delay       = 0       # seconds between bets (configurable)
        self.backoff_delay   = 1.0     # current backoff (grows on consecutive errors)
        self.max_backoff     = 30.0    # cap for exponential backoff
        self.consecutive_errors = 0

        # ── rate limiting ──
        self.rate_limit      = 480
        self.rate_remaining  = 480
        self.rate_wait       = False
        self.rate_reset_at   = 0.0

        # ── performance ──
        self.bets_per_minute = 0.0
        self.bets_per_second = 0.0
        self.peak_bps        = 0.0   # highest bets in a single second
        self.low_bps         = float("inf")  # lowest bets in a completed second
        self.peak_bpm        = 0.0   # highest bets in a single minute
        self.low_bpm         = float("inf")  # lowest bets in a completed minute
        self._bets_this_sec  = 0     # counter for current second
        self._bets_this_min  = 0     # counter for current minute
        self._current_sec    = 0     # which second we're in (int timestamp)
        self._current_min    = 0     # which minute we're in (int timestamp // 60)
        self.bet_timestamps  = deque(maxlen=120)

        # ── profit history for sparkline / graph ──
        self.profit_history  = deque(maxlen=80)
        self.profit_history.append(0.0)

        # ── recent bets ──
        self.recent_bets = deque(maxlen=10)

        # ── status messages ──
        self.status     = "Initializing…"
        self.last_error = ""

        # ── profit-based base bet increment ──
        self.profit_increment    : Optional[float] = None   # amount to add to base_bet each milestone
        self.profit_threshold    : Optional[float] = None   # profit interval that triggers increment
        self.next_profit_milestone: float = 0.0              # next profit level to trigger bump

        # ── strategy internal state ──
        self.dalembert_unit = 0
        self.paroli_count   = 0
        self.delay_martin_threshold = 3   # Delay Martingale: flat for this many consecutive losses


state   = BotState()
console = Console()

# ══════════════════════════════════════════════════════════
#  API HELPERS
# ══════════════════════════════════════════════════════════
def _headers() -> dict:
    return {
        "Authorization":    f"Bearer {state.api_key}",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type":     "application/json",
    }

def _update_rate_limit(resp):
    try:
        limit     = int(resp.headers.get("x-ratelimit-limit",     480))
        remaining = int(resp.headers.get("x-ratelimit-remaining", state.rate_remaining))
        with state.lock:
            state.rate_limit     = limit
            state.rate_remaining = remaining
            if remaining <= 2:
                state.rate_wait     = True
                state.rate_reset_at = time.time() + 62   # 2-sec buffer
    except Exception:
        pass

def api_get_balances():
    r = requests.get(f"{API_BASE}/user/balances", headers=_headers(), timeout=12)
    _update_rate_limit(r)
    r.raise_for_status()
    return r.json().get("balances", [])

def api_place_bet(amount: float) -> Optional[dict]:
    payload = {
        "currency":  state.currency,
        "game":      state.game,
        "amount":    f"{amount:.8f}",
        "rule":      state.rule,
        "multiplier": f"{state.multiplier}",
        "bet_value":  f"{state.bet_value}",
    }
    logger.debug("BET payload=%s", json.dumps(payload))  # only visible with --verbose
    try:
        r = requests.post(f"{API_BASE}/bet/place", headers=_headers(),
                          json=payload, timeout=15)
        _update_rate_limit(r)

        if r.status_code == 200:
            data = r.json()
            logger.debug("BET OK  id=%s state=%s",
                         data.get("bet", {}).get("id"),
                         data.get("bet", {}).get("state"))
            return data
        elif r.status_code == 429:
            logger.warning("RATE LIMIT 429 — cooling down 60s")
            with state.lock:
                state.rate_wait     = True
                state.rate_reset_at = time.time() + 62
                state.last_error    = "Rate limit hit — cooling down 60s"
        else:
            body = r.text[:500]
            logger.error("HTTP %d  response=%s  payload=%s",
                         r.status_code, body, json.dumps(payload))
            with state.lock:
                state.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
    except requests.exceptions.Timeout:
        logger.warning("REQUEST TIMEOUT on bet #%d", state.total_bets + 1)
        with state.lock:
            state.last_error = "Request timeout — retrying…"
    except Exception as e:
        logger.exception("UNEXPECTED ERROR placing bet: %s", e)
        with state.lock:
            state.last_error = str(e)[:120]
    return None

# ══════════════════════════════════════════════════════════
#  STRATEGY ENGINE
# ══════════════════════════════════════════════════════════
def compute_next_bet(last_result: str) -> float:
    key  = state.strategy_key
    base = state.base_bet
    cur  = state.current_bet

    if key == "1":   # Flat
        return base

    elif key == "2": # Martingale
        return (cur * 2.01) if last_result == "loss" else base

    elif key == "3": # Anti-Martingale
        return (cur * 2) if last_result == "win" else base

    elif key == "4": # D'Alembert
        if last_result == "loss":
            state.dalembert_unit += 1
        elif last_result == "win" and state.dalembert_unit > 0:
            state.dalembert_unit -= 1
        return base * (1 + state.dalembert_unit)

    elif key == "5": # Paroli
        if last_result == "win":
            state.paroli_count += 1
            if state.paroli_count >= 3:
                state.paroli_count = 0
                return base
            return cur * 2
        else:
            state.paroli_count = 0
            return base

    elif key == "6": # WolfRider — 2.01x on loss, reset on win (switching handled separately)
        return base if last_result == "win" else (cur * 2.01)

    elif key == "7": # Delay Martingale — flat for N consecutive losses, then double
        if last_result == "win":
            return base
        # count consecutive losses from current_streak (negative = loss streak)
        consec_losses = abs(min(state.current_streak, 0))
        if consec_losses <= state.delay_martin_threshold:
            return base  # still in delay period, flat bet
        return cur * 2.01   # past threshold, double

    return base

def wolfrider_switch():
    """WolfRider post-bet: switch over/under on every 2nd, 6th, and 9th bet."""
    n = state.total_bets
    switches = 0
    if n > 0 and n % 2 == 0:
        switches += 1
    if n > 0 and n % 6 == 0:
        switches += 1
    if n > 0 and n % 9 == 0:
        switches += 1
    # odd number of switches = actual switch; even = cancel out
    if switches % 2 == 1:
        state.rule = "under" if state.rule == "over" else "over"
        win_chance = 99.0 / state.multiplier
        if state.rule == "over":
            state.bet_value = round(100.0 - win_chance - 0.01, 2)
        else:
            state.bet_value = round(win_chance, 2)
        logger.debug("WOLFRIDER SWITCH → %s  bet_value=%s  (bet #%d)", state.rule, state.bet_value, n)

def should_stop() -> tuple:
    p = state.profit
    b = state.current_balance
    if state.max_profit is not None and p >= state.max_profit:
        return True, f"Profit target reached: {p:+.8f}"
    if state.max_loss is not None and p <= -abs(state.max_loss):
        return True, f"Max loss hit: {p:+.8f}"
    if state.max_bets is not None and state.total_bets >= state.max_bets:
        return True, f"Max bets reached: {state.total_bets}"
    if state.max_wins is not None and state.wins >= state.max_wins:
        return True, f"Max wins reached: {state.wins}"
    if state.stop_on_balance is not None and b <= state.stop_on_balance:
        return True, f"Balance floor hit: {b:.8f}"
    return False, ""

# ══════════════════════════════════════════════════════════
#  BETTING LOOP  (runs in background thread)
# ══════════════════════════════════════════════════════════
def betting_loop():
    last_result = "none"

    while state.running:
        # ── paused ──────────────────────────────────────
        if state.paused:
            state.status = "PAUSED — press [R] to resume"
            time.sleep(0.4)
            continue

        # ── rate-limit cooldown ──────────────────────────
        if state.rate_wait:
            remaining = max(0.0, state.rate_reset_at - time.time())
            state.status = f"Rate limit — cooling down {remaining:.0f}s…"
            if remaining <= 0:
                with state.lock:
                    state.rate_wait     = False
                    state.rate_remaining = state.rate_limit
                state.status = "Resuming after cooldown…"
            time.sleep(1.0)
            continue

        # ── stop-condition check ─────────────────────────
        stop, reason = should_stop()
        if stop:
            state.running = False
            state.status  = f"STOPPED: {reason}"
            _db_save_session()
            break

        # ── bet pacing delay ───────────────────────────────
        if state.bet_delay > 0:
            time.sleep(state.bet_delay)

        # ── place bet ────────────────────────────────────
        state.status = f"Placing bet #{state.total_bets + 1}…"
        result = api_place_bet(state.current_bet)

        if result is None:
            # exponential backoff on consecutive errors
            with state.lock:
                state.consecutive_errors += 1
                state.backoff_delay = min(
                    state.backoff_delay * 2,
                    state.max_backoff,
                )
            time.sleep(state.backoff_delay)
            continue

        bet_data     = result.get("bet", {})
        balance_data = result.get("user_balance", {})
        bet_state    = bet_data.get("state", "loss")
        result_value = float(bet_data.get("result_value", 0))
        amount_used  = float(bet_data.get("amount", state.current_bet))
        new_balance  = float(balance_data.get("amount", state.current_balance))

        # raw profit from API when win; negative amount when loss
        raw_profit = float(bet_data.get("profit", 0)) if bet_state == "win" else -amount_used

        with state.lock:
            # reset backoff on success
            state.consecutive_errors = 0
            state.backoff_delay      = 1.0

            state.total_bets     += 1
            state.wagered        += amount_used
            state.profit         += raw_profit
            state.current_balance = new_balance

            if bet_state == "win":
                state.wins += 1
                if raw_profit > state.highest_win:
                    state.highest_win = raw_profit
                state.current_streak = max(state.current_streak, 0) + 1
                state.max_win_streak = max(state.max_win_streak, state.current_streak)
            else:
                state.losses += 1
                if amount_used > state.biggest_loss:
                    state.biggest_loss = amount_used
                state.current_streak = min(state.current_streak, 0) - 1
                state.max_loss_streak = max(state.max_loss_streak, abs(state.current_streak))

            if amount_used > state.highest_bet:
                state.highest_bet = amount_used
            if new_balance > state.highest_balance:
                state.highest_balance = new_balance
            if new_balance < state.lowest_balance:
                state.lowest_balance = new_balance

            # ── profit-based base bet increment ──
            if (state.profit_increment and state.profit_threshold
                    and state.profit >= state.next_profit_milestone):
                state.base_bet += state.profit_increment
                state.next_profit_milestone += state.profit_threshold
                logger.info(
                    "PROFIT INCREMENT: base_bet → %.8f (next milestone at %.8f profit)",
                    state.base_bet, state.next_profit_milestone,
                )

            # profit graph snapshot (every 5 bets)
            if state.total_bets % 5 == 0:
                state.profit_history.append(state.profit)

            # recent bets log
            state.recent_bets.append({
                "n":     state.total_bets,
                "time":  datetime.now().strftime("%H:%M:%S"),
                "amt":   amount_used,
                "roll":  result_value,
                "state": bet_state,
                "pnl":   raw_profit,
                "bal":   new_balance,
            })

            # bets-per-minute / bets-per-second (session average)
            now = time.time()
            elapsed = now - state.session_start
            if elapsed > 0:
                bps = state.total_bets / elapsed
                state.bets_per_second = bps
                state.bets_per_minute = bps * 60

            # track peak/low bets per individual second and minute
            sec_key = int(now)
            min_key = int(now) // 60
            if sec_key != state._current_sec:
                # new second — record the old second's count
                if state._current_sec > 0 and state._bets_this_sec > 0:
                    if state._bets_this_sec > state.peak_bps:
                        state.peak_bps = state._bets_this_sec
                    if state._bets_this_sec < state.low_bps:
                        state.low_bps = state._bets_this_sec
                state._current_sec = sec_key
                state._bets_this_sec = 1
            else:
                state._bets_this_sec += 1

            if min_key != state._current_min:
                # new minute — record the old minute's count
                if state._current_min > 0 and state._bets_this_min > 0:
                    if state._bets_this_min > state.peak_bpm:
                        state.peak_bpm = state._bets_this_min
                    if state._bets_this_min < state.low_bpm:
                        state.low_bpm = state._bets_this_min
                state._current_min = min_key
                state._bets_this_min = 1
            else:
                state._bets_this_min += 1

            sign  = "+" if raw_profit >= 0 else ""
            emoji = "W" if bet_state == "win" else "L"
            state.status = (
                f"{emoji} {'WIN' if bet_state=='win' else 'LOSS'} | "
                f"Roll: {result_value:.2f} | "
                f"Bet: {amount_used:.8f} | "
                f"P/L: {sign}{raw_profit:.8f} | "
                f"Bal: {new_balance:.8f}"
            )
            state.last_error = ""

        # persist to DB (non-blocking)
        _db_save_bet(bet_data, raw_profit, new_balance)

        # ── WolfRider: switch over/under after bet ──
        if state.strategy_key == "6":
            wolfrider_switch()

        # calculate next bet amount with safety cap (20% of balance)
        raw_next  = compute_next_bet(bet_state)
        max_safe  = state.current_balance * 0.20
        next_bet  = max(state.base_bet, min(raw_next, max_safe))
        state.current_bet = next_bet
        last_result = bet_state

# ══════════════════════════════════════════════════════════
#  DATABASE  I/O
# ══════════════════════════════════════════════════════════
def _db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def _db_start_session() -> int:
    conn = _db_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions
            (started_at, currency, game, strategy, base_bet, multiplier, start_balance)
        VALUES (?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        state.currency, state.game, state.strategy,
        state.base_bet, state.multiplier, state.start_balance,
    ))
    conn.commit()
    sid = c.lastrowid
    conn.close()
    return sid

def _db_save_bet(bet_data: dict, profit: float, balance: float):
    try:
        conn = _db_conn()
        conn.execute("""
            INSERT INTO bets
                (session_id, timestamp, amount, multiplier, rule,
                 bet_value, result_value, state, profit, balance_after)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            state.session_id,
            datetime.now().isoformat(),
            float(bet_data.get("amount", 0)),
            float(bet_data.get("multiplier", 0)),
            state.rule,
            float(bet_data.get("bet_value", 0)),
            float(bet_data.get("result_value", 0)),
            bet_data.get("state", ""),
            profit,
            balance,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_bet failed: %s", e)

def _db_save_session():
    try:
        conn = _db_conn()
        lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
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
            state.total_bets, state.wins, state.losses,
            state.profit, state.wagered, state.current_balance,
            state.max_win_streak, state.max_loss_streak,
            state.highest_balance, lo_bal,
            state.highest_win, state.biggest_loss,
            state.bets_per_minute, state.bets_per_second,
            state.peak_bps,
            state.low_bps if state.low_bps != float("inf") else 0,
            state.peak_bpm,
            state.low_bpm if state.low_bpm != float("inf") else 0,
            state.session_id,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("DB save_session failed: %s", e)

def _db_get_history(limit: int = 12):
    conn = _db_conn()
    rows = conn.execute("""
        SELECT id, started_at, ended_at, currency, strategy,
               total_bets, profit, wagered, max_win_streak, max_loss_streak
        FROM sessions ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows

# ══════════════════════════════════════════════════════════
#  STATE FILE  (for --status / --daemon communication)
# ══════════════════════════════════════════════════════════
def _save_state_file():
    """Write live session state to JSON so --status can read it."""
    try:
        data = {
            "pid": os.getpid(),
            "session_id": state.session_id,
            "running": state.running,
            "paused": state.paused,
            "currency": state.currency,
            "strategy": state.strategy,
            "multiplier": state.multiplier,
            "base_bet": state.base_bet,
            "current_bet": state.current_bet,
            "total_bets": state.total_bets,
            "wins": state.wins,
            "losses": state.losses,
            "profit": state.profit,
            "wagered": state.wagered,
            "current_balance": state.current_balance,
            "start_balance": state.start_balance,
            "current_streak": state.current_streak,
            "max_win_streak": state.max_win_streak,
            "max_loss_streak": state.max_loss_streak,
            "highest_bet": state.highest_bet,
            "highest_win": state.highest_win,
            "biggest_loss": state.biggest_loss,
            "bets_per_minute": state.bets_per_minute,
            "bets_per_second": state.bets_per_second,
            "rate_remaining": state.rate_remaining,
            "rate_limit": state.rate_limit,
            "uptime_sec": int(time.time() - state.session_start),
            "status": state.status,
            "last_error": state.last_error,
            "updated_at": datetime.now().isoformat(),
        }
        with open(STATE_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _cleanup_state_file():
    for p in (STATE_PATH, PID_PATH):
        try:
            os.remove(p)
        except OSError:
            pass

# ══════════════════════════════════════════════════════════
#  TUI — BORDERLESS ULTRA-COMPACT DASHBOARD
# ══════════════════════════════════════════════════════════
SPARKLINE = "▁▂▃▄▅▆▇█"

def _sparkline(values) -> str:
    if not values or len(values) < 2:
        return "—"
    mn, mx = min(values), max(values)
    span   = mx - mn or 1
    return "".join(SPARKLINE[int((v - mn) / span * 7)] for v in values)

# ── ANSI color helpers (no Rich dependency for TUI) ─────
_RST  = "\033[0m"
_BOLD = "\033[1m"
_DIM  = "\033[2m"
_COLORS = {
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "white": "\033[37m",
    "bg_blue": "\033[44m",
    "bold_red": "\033[1;31m", "bold_green": "\033[1;32m",
    "bold_yellow": "\033[1;33m", "bold_cyan": "\033[1;36m",
    "bold_white": "\033[1;37m",
    "dim_blue": "\033[2;34m", "dim": "\033[2m",
}
_CLR_EOL = "\033[K"

def _a(color: str, text: str) -> str:
    """Apply ANSI color to text and reset."""
    code = _COLORS.get(color, "")
    return f"{code}{text}{_RST}" if code else text

def _fmt_speed() -> str:
    """Format peak/low BPS and BPM as a compact range string."""
    pbps = int(state.peak_bps) if state.peak_bps > 0 else 0
    lbps = int(state.low_bps) if state.low_bps != float("inf") else 0
    pbpm = int(state.peak_bpm) if state.peak_bpm > 0 else 0
    lbpm = int(state.low_bpm) if state.low_bpm != float("inf") else 0
    return f"{lbps}-{pbps}/s {lbpm}-{pbpm}/m"

def build_dashboard_screen() -> str:
    """Build full-screen dashboard using direct ANSI codes.

    Layout (always exactly term_h lines):
      Row 1:       Header bar
      Row 2:       Balance / PnL / Wagered / WR
      Row 3:       Bets / Wins / Losses / Streaks
      Row 4:       Bet / Strategy / Multiplier / BPM / Rate limit
      Row 5:       Peak / Low bal / Best win / Worst loss / Avg P/L
      Row 6:       Separator ───
      Row 7:       Bets table header
      Row 8..N-2:  Recent bets (flex — fills remaining, padded with blanks)
      Row N-1:     Separator ───
      Row N:       Status + controls
    """
    try:
        term_size = os.get_terminal_size()
        term_h = term_size.lines
        term_w = term_size.columns
    except OSError:
        term_h = 24
        term_w = 80

    lines = []  # list of raw ANSI strings, one per terminal row

    def add(s: str):
        lines.append(f"{s}{_CLR_EOL}")

    def add_blank():
        lines.append(_CLR_EOL)

    # ── Row 1: Header ─────────────────────────────────
    uptime = str(timedelta(seconds=int(time.time() - state.session_start)))
    if state.running and not state.paused:
        st_ind = _a("bold_green", "● LIVE")
    elif state.paused:
        st_ind = _a("bold_yellow", "● PAUSED")
    else:
        st_ind = _a("bold_red", "● STOPPED")

    hdr_text = f"WolfBet v{VERSION}  #{state.session_id}  {uptime}  "
    # visible chars used: 4 + len(hdr_text) + len("● STATUS") + 1
    hdr_vis = 4 + len(hdr_text) + len("● STOPPED") + 1
    fill = max(0, term_w - hdr_vis)
    add(f"{_a('dim_blue', '─── ')}{_a('bold_cyan', f'WolfBet v{VERSION}')}"
        f"{_a('white', f'  #{state.session_id}  {uptime}')}  {st_ind} "
        f"{_a('dim_blue', '─' * fill)}")

    # ── Row 2: Balance bar ────────────────────────────
    p = state.profit
    ps = "+" if p >= 0 else ""
    pc = "bold_green" if p >= 0 else "bold_red"
    wr = (state.wins / state.total_bets * 100) if state.total_bets else 0
    wr_c = "bold_green" if wr >= 50 else "bold_red"

    add(f"{_COLORS['bg_blue']}{_BOLD}{_COLORS['white']} BAL {_RST} "
        f"{_a('bold_white', f'{state.current_balance:.8f} {state.currency.upper()}')}"
        f"  {_a('dim', 'PnL')} {_a(pc, f'{ps}{p:.8f}')}"
        f"  {_a('dim', 'WAG')} {_a('yellow', f'{state.wagered:.8f}')}"
        f"  {_a('dim', 'WR')} {_a(wr_c, f'{wr:.1f}%')}")

    # ── Row 3: Stats line 1 ──────────────────────────
    cs = state.current_streak
    if cs > 0:
        sk = _a("green", f"W+{cs}")
    elif cs < 0:
        sk = _a("red", f"L{cs}")
    else:
        sk = _a("dim", "—")

    add(f" {_a('dim', 'Bets')} {_a('bold_white', str(state.total_bets))}"
        f"  {_a('green', 'W')} {_a('bold_green', str(state.wins))}"
        f"  {_a('red', 'L')} {_a('bold_red', str(state.losses))}"
        f"  {_a('dim', 'Str')} {sk}"
        f"  {_a('dim', f'Best W+{state.max_win_streak}/L-{state.max_loss_streak}')}")

    # ── Row 4: Bet / Strategy / Rate ──────────────────
    rl = state.rate_remaining
    rl_c = "green" if rl > 100 else ("yellow" if rl > 20 else "red")
    spark = _sparkline(list(state.profit_history))
    sp_c = "green" if p >= 0 else "red"

    s4 = (f" {_a('dim', 'Bet')} {_a('yellow', f'{state.current_bet:.8f}')}")
    if state.profit_increment and state.profit_threshold:
        s4 += f"  {_a('dim', 'Base')} {_a('bold_green', f'{state.base_bet:.8f}')}"
    s4 += (f"  {_a('dim', 'Hi')} {_a('white', f'{state.highest_bet:.8f}')}"
           f"  {_a('cyan', state.strategy)}"
           f"  {_a('dim', f'{state.multiplier}x')}"
           f"  {_a('dim', state.rule)}"
           f"  {_a('dim', 'BPS')} {_a('magenta', f'{state.bets_per_second:.1f}')}"
           f"  {_a('dim', 'BPM')} {_a('magenta', f'{state.bets_per_minute:.0f}')}"
           f"  {_a('dim', 'RL')} {_a(rl_c, f'{rl}/{state.rate_limit}')}"
           f"  {_a('dim', 'Speed')} {_a('magenta', _fmt_speed())}"
           f"  {_a(sp_c, spark)}")
    add(s4)

    # ── Row 5: Extremes / Performance ────────────────
    hi_bal = state.highest_balance if state.highest_balance > 0 else state.current_balance
    lo_bal = state.lowest_balance if state.lowest_balance != float("inf") else state.current_balance
    avg_pnl = state.profit / state.total_bets if state.total_bets else 0.0
    avg_c = "green" if avg_pnl >= 0 else "red"
    avg_s = "+" if avg_pnl >= 0 else ""
    bal_change = state.current_balance - state.start_balance
    bc_c = "bold_green" if bal_change >= 0 else "bold_red"
    bc_s = "+" if bal_change >= 0 else ""

    s5 = (f" {_a('dim', 'Peak')} {_a('green', f'{hi_bal:.8f}')}"
          f"  {_a('dim', 'Low')} {_a('red', f'{lo_bal:.8f}')}"
          f"  {_a('dim', 'BestW')} {_a('green', f'+{state.highest_win:.8f}')}"
          f"  {_a('dim', 'WorstL')} {_a('red', f'-{state.biggest_loss:.8f}')}"
          f"  {_a('dim', 'Avg')} {_a(avg_c, f'{avg_s}{avg_pnl:.8f}')}"
          f"  {_a('dim', 'Bal')} {_a(bc_c, f'{bc_s}{bal_change:.8f}')}")
    add(s5)

    # ── Row 6: Separator ──────────────────────────────
    add(_a("dim_blue", "─" * term_w))

    # ── Row 6: Bets table header ──────────────────────
    add(_a("dim", f"{'#':>6}  {'Time':<9} {'Amount':>14}  {'Roll':>7}  {'W/L':^4}  {'P/L':>15}  {'Balance':>15}"))

    # ── Rows 8..N-2: Recent bet rows (flex area) ─────
    # top(7) + bottom(2) = 9 pinned lines
    bets_avail = max(0, term_h - 9)
    recent = list(state.recent_bets)[-bets_avail:] if bets_avail > 0 else []

    for b in reversed(recent):
        bwl_c = "bold_green" if b["state"] == "win" else "bold_red"
        bwl_t = " W " if b["state"] == "win" else " L "
        bpnl_c = "green" if b["pnl"] >= 0 else "red"
        bsign = "+" if b["pnl"] >= 0 else ""
        bn = b["n"]
        btm = b["time"]
        bamt = b["amt"]
        broll = b["roll"]
        bpnl = b["pnl"]
        bbal = b["bal"]
        add(f"{_a('dim', f'{bn:>6}')}"
            f"  {_a('dim', f'{btm:<9}')}"
            f" {_a('white', f'{bamt:>14.8f}')}"
            f"  {_a('white', f'{broll:>7.2f}')}"
            f"  {_a(bwl_c, bwl_t)}"
            f"  {_a(bpnl_c, f'{bsign}{bpnl:>14.8f}')}"
            f"  {_a('white', f'{bbal:>14.8f}')}")

    # Pad empty lines
    for _ in range(bets_avail - len(recent)):
        add_blank()

    # ── Row N-1: Bottom separator ─────────────────────
    add(_a("dim_blue", "─" * term_w))

    # ── Row N: Status + controls ──────────────────────
    status_txt = state.status[:60]
    keys = "[P]ause [R]esume [Q]uit [H]istory"
    add(f" {_a('white', status_txt)}  {_a('dim', keys)}")

    return "\r\n".join(lines)

# ══════════════════════════════════════════════════════════
#  HISTORY SCREEN
# ══════════════════════════════════════════════════════════
def show_history():
    was_paused  = state.paused
    state.paused = True
    time.sleep(0.15)

    rows = _db_get_history(15)
    console.clear()
    console.print(Rule("[bold cyan]Session History[/]"))

    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("ID",       width=5,  justify="center")
    t.add_column("Started",  width=17)
    t.add_column("Ended",    width=17)
    t.add_column("Cur",      width=6,  justify="center")
    t.add_column("Strategy", width=18)
    t.add_column("Bets",     width=7,  justify="right")
    t.add_column("Profit",   width=16, justify="right")
    t.add_column("Wagered",  width=16, justify="right")
    t.add_column("W/L Str",  width=10, justify="center")

    for row in rows:
        sid, started, ended, cur, strat, bets, profit, wagered, mws, mls = row
        pc  = "green" if (profit or 0) >= 0 else "red"
        ps  = "+" if (profit or 0) >= 0 else ""
        t.add_row(
            str(sid),
            (started or "")[:16],
            (ended   or "—")[:16],
            (cur or "").upper(),
            strat or "—",
            str(bets or 0),
            Text(f"{ps}{profit:.8f}" if profit else "0.00000000", style=pc),
            f"{wagered:.8f}" if wagered else "—",
            f"W{mws or 0}/L{mls or 0}",
        )

    console.print(t)
    console.print("\n[dim]  Press any key to return…[/]")
    _getch()
    state.paused = was_paused

# ══════════════════════════════════════════════════════════
#  KEYBOARD INPUT  (runs in background thread)
# ══════════════════════════════════════════════════════════
def _getch() -> str:
    try:
        import termios, tty
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return ""

def input_handler():
    while state.running or state.paused:
        ch = _getch().lower()
        if not ch:
            time.sleep(0.1)
            continue
        if ch in ("q", "\x03", "\x1c"):  # Q, Ctrl+C, Ctrl+backslash
            state.running  = False
            state.paused   = False
            state.status   = "Stopping — saving session…"
            logger.warning("SESSION END (user quit)  bets=%d  profit=%+.8f  balance=%.8f",
                        state.total_bets, state.profit, state.current_balance)
            _db_save_session()
        elif ch == "p":
            state.paused = True
            state.status = "PAUSED"
        elif ch == "r":
            state.paused = False
            state.status = "Resumed"
        elif ch == "h":
            show_history()

# ══════════════════════════════════════════════════════════
#  SETUP WIZARD  (step-based, supports "back" and "none")
# ══════════════════════════════════════════════════════════
_BACK = object()   # sentinel

def _ask(prompt_text: str, default: str = "", choices: list = None) -> str:
    """input()-based prompt with Rich-colored label.
    Arrow keys / editing work normally.
    Type 'back' to go to the previous step."""
    hint = f" [bold yellow]{default}[/]" if default else ""
    console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
    while True:
        try:
            raw = input(": ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if raw.lower() == "back":
            return _BACK
        if not raw:
            return default
        if choices and raw.lower() not in [c.lower() for c in choices]:
            console.print(f"    [red]Choose from:[/] {', '.join(choices)}")
            console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
            continue
        return raw

def _ask_optional(prompt_text: str, current) -> str:
    """Like _ask but shows current value and accepts 'none'/'clear' to wipe it."""
    if current is not None:
        hint = f" [bold yellow]{current}[/]  [dim](type 'none' to clear)[/]"
    else:
        hint = " [dim]none[/]"
    console.print(f"  [bold cyan]>[/] {prompt_text}{hint}", end="")
    try:
        raw = input(": ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if raw.lower() == "back":
        return _BACK
    if raw.lower() in ("none", "clear", "off", "disable", "0"):
        return ""
    if not raw:
        return str(current) if current is not None else ""
    return raw

def setup_wizard():
    console.clear()
    console.print()
    console.print(Align.center(
        Panel(
            "[bold cyan]WolfBet AutoBot  v2.0[/]\n"
            "[dim]High-speed dice auto-bettor with live TUI dashboard[/]",
            box=box.DOUBLE, expand=False, padding=(1, 6), style="blue"
        )
    ))
    console.print()
    console.print("[dim]  Tips: arrow keys work  |  type 'back' for previous step  |  'none' to clear a value[/]")
    console.print()

    # ── Load saved config as defaults ─────────────────────
    has_saved = load_config()
    saved_key = state.api_key
    balances  = None
    available_bal = {}

    if has_saved and saved_key:
        console.print("[green]  Found saved config from last session:[/]")
        console.print(f"    [dim]Currency:[/]   [yellow]{state.currency.upper()}[/]")
        console.print(f"    [dim]Strategy:[/]   [yellow]{state.strategy}[/]")
        console.print(f"    [dim]Multiplier:[/] [yellow]{state.multiplier}x[/]")
        console.print(f"    [dim]Base bet:[/]   [yellow]{state.base_bet:.8f}[/]")
        console.print(f"    [dim]API key:[/]    [yellow]{saved_key[:8]}…{saved_key[-4:]}[/]")
        console.print()

    # ── Step functions ────────────────────────────────────
    def step_api_key():
        nonlocal balances, saved_key
        console.print(Rule("[bold cyan]Step 1 / 7 — API Authentication[/]"))
        if saved_key:
            console.print(f"  Saved key: [yellow]{saved_key[:8]}…{saved_key[-4:]}[/]")
            v = _ask("Use saved API key? (y/n)", default="y", choices=["y", "n"])
            if v is _BACK:
                return False
            if v.lower() == "y":
                state.api_key = saved_key
            else:
                console.print("  [dim]Get your token from:[/] [underline]https://wolfbet.com/user/settings?tab=api[/]")
                v = _ask("Paste your WolfBet API key")
                if v is _BACK:
                    return False
                state.api_key = v
                saved_key = v
        else:
            console.print("  [dim]Get your token from:[/] [underline]https://wolfbet.com/user/settings?tab=api[/]")
            v = _ask("Paste your WolfBet API key")
            if v is _BACK:
                return False
            state.api_key = v
            saved_key = v

        console.print("  [dim]Testing connection…[/]")
        try:
            balances = api_get_balances()
            console.print("[green]  Connected![/]\n")
        except Exception as e:
            console.print(f"[red]  Connection failed: {e}[/]")
            console.print("[yellow]  Check your API key and try again.[/]\n")
            state.api_key = ""
            saved_key = ""
            return False
        return True

    def step_currency():
        nonlocal available_bal
        console.print(Rule("[bold cyan]Step 2 / 7 — Currency[/]"))
        if balances:
            console.print("  [dim]Your balances:[/]")
            for b in balances:
                amt = float(b["amount"])
                available_bal[b["currency"]] = amt
                if amt > 0:
                    console.print(f"    [yellow]{b['currency'].upper():8s}[/] {amt:.8f}")
            console.print()

        v = _ask("Select currency", default=state.currency, choices=CURRENCIES)
        if v is _BACK:
            return False
        state.currency = v.lower()
        bal = available_bal.get(state.currency, 0.0)
        state.start_balance   = bal
        state.current_balance = bal
        state.highest_balance = bal
        state.lowest_balance  = bal
        console.print(f"  [green]Balance: {bal:.8f} {state.currency.upper()}[/]\n")
        return True

    def step_bet_config():
        console.print(Rule("[bold cyan]Step 3 / 7 — Bet Configuration[/]"))
        console.print("  [dim]Game: Dice (only game on WolfBet API v1)[/]\n")

        v = _ask("Bet direction", default=state.rule, choices=["over", "under"])
        if v is _BACK:
            return False
        state.rule = v.lower()

        console.print()
        console.print("  [dim]Multiplier examples (99% RTP, 1% house edge):[/]")
        for mult, chance in [(1.5, 66.0), (1.98, 50.0), (2.0, 49.5), (3.0, 33.0), (9.9, 10.0)]:
            console.print(f"    [yellow]{mult:5.2f}x[/]  →  ~{chance:.0f}% win")
        console.print()

        v = _ask("Multiplier", default=str(state.multiplier))
        if v is _BACK:
            return False
        state.multiplier = float(v)
        win_chance = 99.0 / state.multiplier
        if state.rule == "over":
            state.bet_value = round(100.0 - win_chance - 0.01, 2)
        else:
            state.bet_value = round(win_chance, 2)
        console.print(f"    [dim]→ Bet value:[/] [yellow]{state.bet_value}[/]  [dim]Win chance:[/] [yellow]{win_chance:.2f}%[/]\n")

        v = _ask(f"Base bet amount ({state.currency.upper()})", default=str(state.base_bet))
        if v is _BACK:
            return False
        state.base_bet    = float(v)
        state.current_bet = state.base_bet
        return True

    def step_strategy():
        console.print(Rule("[bold cyan]Step 4 / 7 — Betting Strategy[/]"))
        for k, (name, desc) in STRATEGIES.items():
            console.print(f"    [bold yellow][{k}][/] [cyan]{name:20s}[/]  [dim]{desc}[/]")
        console.print()

        v = _ask("Select strategy", default=state.strategy_key, choices=list(STRATEGIES.keys()))
        if v is _BACK:
            return False
        state.strategy_key = v
        state.strategy     = STRATEGIES[v][0]

        if v == "6":
            console.print()
            console.print("  [bold cyan]WolfRider[/] — your dedicated strategy:")
            console.print("    [green]• On win:[/]  reset to base bet")
            console.print("    [green]• On loss:[/] increase by 2.01x")
            console.print("    [green]• Switch over/under[/] on every 2nd, 6th, and 9th bet")
            console.print()

        elif v == "7":
            console.print()
            console.print("  [bold cyan]Delay Martingale[/] — flat bet through the first N")
            console.print("  consecutive losses, then start doubling.")
            console.print()
            d = _ask("  Losses before doubling starts", default=str(state.delay_martin_threshold))
            if d is _BACK:
                return False
            state.delay_martin_threshold = int(d)
            console.print(f"    [green]→ Flat for {state.delay_martin_threshold} losses, then Martingale[/]\n")

        console.print()
        return True

    def step_stop_conditions():
        console.print(Rule("[bold cyan]Step 5 / 7 — Stop Conditions[/]"))
        console.print("  [dim]Enter to keep current  |  'none' to clear  |  'back' for previous step[/]\n")

        v = _ask_optional(f"Stop when profit reaches ({state.currency.upper()})", state.max_profit)
        if v is _BACK:
            return False
        state.max_profit = float(v) if v else None

        v = _ask_optional(f"Stop when loss exceeds ({state.currency.upper()})", state.max_loss)
        if v is _BACK:
            return False
        state.max_loss = float(v) if v else None

        v = _ask_optional("Stop after N bets", state.max_bets)
        if v is _BACK:
            return False
        state.max_bets = int(v) if v else None

        v = _ask_optional("Stop after N wins", state.max_wins)
        if v is _BACK:
            return False
        state.max_wins = int(v) if v else None

        v = _ask_optional(f"Stop if balance drops below ({state.currency.upper()})", state.stop_on_balance)
        if v is _BACK:
            return False
        state.stop_on_balance = float(v) if v else None

        console.print("\n  [bold]Profit-based bet increment[/] [dim]— auto-raise base bet as profit grows[/]")
        v = _ask_optional(f"Profit threshold to trigger increment ({state.currency.upper()})", state.profit_threshold)
        if v is _BACK:
            return False
        state.profit_threshold = float(v) if v else None

        v = _ask_optional(f"Increment amount to add to base bet ({state.currency.upper()})", state.profit_increment)
        if v is _BACK:
            return False
        state.profit_increment = float(v) if v else None

        # init milestone
        if state.profit_threshold:
            state.next_profit_milestone = state.profit_threshold

        console.print()
        return True

    def step_preset():
        console.print(Rule("[bold cyan]Step 6 / 7 — Presets[/]"))
        presets = list_presets()
        if presets:
            console.print("  [dim]Saved presets:[/]")
            for name, data in presets.items():
                console.print(f"    [yellow]{name}[/]  [dim]→ {data.get('strategy', '?')} on {data.get('currency', '?').upper()}[/]")
            console.print()

        v = _ask("Save current config as preset? (name or 'skip')", default="skip")
        if v is _BACK:
            return False
        if v.lower() != "skip":
            save_preset(v)
            console.print(f"  [green]Saved preset '{v}'[/]\n")
        else:
            console.print()
        return True

    def step_confirm():
        console.print(Rule("[bold green]Step 7 / 7 — Review & Confirm[/]"))
        rows = [
            ("Currency",        f"{state.currency.upper()}"),
            ("Balance",         f"{state.start_balance:.8f}"),
            ("Rule",            state.rule),
            ("Multiplier",      f"{state.multiplier}x  (win chance {99/state.multiplier:.2f}%)"),
            ("Bet value",       str(state.bet_value)),
            ("Base bet",        f"{state.base_bet:.8f} {state.currency.upper()}"),
            ("Strategy",        state.strategy),
        ]
        if state.strategy_key == "6":
            rows.append(("Switching",   "over/under on bet 2, 6, 9 multiples"))
        if state.strategy_key == "7":
            rows.append(("Delay",       f"flat for {state.delay_martin_threshold} losses"))
        rows += [
            ("Max profit",      str(state.max_profit) if state.max_profit else "—"),
            ("Max loss",        str(state.max_loss)   if state.max_loss   else "—"),
            ("Max bets",        str(state.max_bets)   if state.max_bets   else "Unlimited"),
            ("Max wins",        str(state.max_wins)   if state.max_wins   else "Unlimited"),
            ("Min balance",     str(state.stop_on_balance) if state.stop_on_balance else "—"),
            ("Profit increment", f"+{state.profit_increment:.8f} every {state.profit_threshold} profit"
                if state.profit_increment and state.profit_threshold else "—"),
        ]
        for k, val in rows:
            console.print(f"  [dim]{k:18s}[/] [yellow]{val}[/]")

        console.print()
        console.print("[bold red]  WARNING: Real money will be wagered. Bet responsibly.[/]")
        console.print()

        v = _ask("Start auto-betting? (y/n/back)", default="n", choices=["y", "n", "back"])
        if v is _BACK or v.lower() == "back":
            return False
        if v.lower() != "y":
            console.print("[yellow]Aborted.[/]")
            sys.exit(0)

        save_config()
        console.print(f"[dim]  Config saved to {CONFIG_PATH}[/]\n")
        return True

    # ── Run steps with back navigation ────────────────────
    steps = [step_api_key, step_currency, step_bet_config, step_strategy,
             step_stop_conditions, step_preset, step_confirm]
    i = 0
    while i < len(steps):
        ok = steps[i]()
        if ok:
            i += 1
        else:
            i = max(0, i - 1)

# ══════════════════════════════════════════════════════════
#  PRINT SESSION SUMMARY
# ══════════════════════════════════════════════════════════
def _print_summary():
    console.print()
    console.print(Rule("[bold cyan]Session Complete[/]"))
    console.print()
    pairs = [
        ("Session ID",      str(state.session_id)),
        ("Total Bets",      str(state.total_bets)),
        ("Wins / Losses",   f"{state.wins} / {state.losses}"),
        ("Win Rate",        f"{state.wins/state.total_bets*100:.1f}%" if state.total_bets else "—"),
        ("Profit",          f"{state.profit:+.8f} {state.currency.upper()}"),
        ("Final Balance",   f"{state.current_balance:.8f} {state.currency.upper()}"),
        ("Streaks",         f"W+{state.max_win_streak}  L-{state.max_loss_streak}"),
        ("Best Win",        f"+{state.highest_win:.8f}"),
        ("Worst Loss",      f"-{state.biggest_loss:.8f}"),
        ("Speed",           f"{state.bets_per_second:.1f}/s  {state.bets_per_minute:.0f}/m  "
                            f"Range: {int(state.low_bps) if state.low_bps != float('inf') else 0}"
                            f"-{int(state.peak_bps)}/s  "
                            f"{int(state.low_bpm) if state.low_bpm != float('inf') else 0}"
                            f"-{int(state.peak_bpm)}/m"),
    ]
    for k, v in pairs:
        sty = "green" if ("profit" in k.lower() and state.profit >= 0) else (
              "red"   if ("profit" in k.lower() and state.profit <  0) else "white")
        console.print(f"  [dim]{k:18s}[/] [{sty}]{v}[/]")
    console.print()

# ══════════════════════════════════════════════════════════
#  --status COMMAND
# ══════════════════════════════════════════════════════════
def cmd_status():
    if not os.path.exists(STATE_PATH):
        console.print("[yellow]No running session found.[/]")
        sys.exit(0)
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
    except Exception:
        console.print("[red]Could not read state file.[/]")
        sys.exit(1)

    pid = d.get("pid")
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass

    status = "[bold green]RUNNING[/]" if alive else "[bold red]STOPPED[/]"
    p = d.get("profit", 0)
    ps = "+" if p >= 0 else ""
    pc = "green" if p >= 0 else "red"
    wr = (d["wins"] / d["total_bets"] * 100) if d.get("total_bets") else 0
    uptime = str(timedelta(seconds=d.get("uptime_sec", 0)))

    console.print(Rule(f"[bold cyan]WolfBet Session #{d.get('session_id', '?')}  {status}[/]"))
    rows = [
        ("Uptime",         uptime),
        ("Currency",       d.get("currency", "?").upper()),
        ("Strategy",       d.get("strategy", "?")),
        ("Balance",        f"{d.get('current_balance', 0):.8f}"),
        ("Profit",         f"[{pc}]{ps}{p:.8f}[/]"),
        ("Wagered",        f"{d.get('wagered', 0):.8f}"),
        ("Bets",           str(d.get("total_bets", 0))),
        ("Wins / Losses",  f"{d.get('wins', 0)} / {d.get('losses', 0)}"),
        ("Win Rate",       f"{wr:.1f}%"),
        ("Streaks",        f"W+{d.get('max_win_streak', 0)}  L-{d.get('max_loss_streak', 0)}"),
        ("Bets/min",       f"{d.get('bets_per_minute', 0):.1f}"),
        ("Rate Limit",     f"{d.get('rate_remaining', '?')}/{d.get('rate_limit', '?')}"),
        ("Last Status",    d.get("status", "")[:60]),
        ("Updated",        d.get("updated_at", "?")[:19]),
    ]
    for k, v in rows:
        console.print(f"  [dim]{k:18s}[/] {v}")
    if d.get("last_error"):
        console.print(f"  [dim]{'Last Error':18s}[/] [red]{d['last_error'][:60]}[/]")
    console.print()

# ══════════════════════════════════════════════════════════
#  --stop COMMAND
# ══════════════════════════════════════════════════════════
def cmd_stop():
    if not os.path.exists(PID_PATH):
        console.print("[yellow]No running session found (no PID file).[/]")
        sys.exit(0)
    try:
        with open(PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Sent stop signal to PID {pid}.[/]")
    except ProcessLookupError:
        console.print("[yellow]Process already stopped.[/]")
        _cleanup_state_file()
    except Exception as e:
        console.print(f"[red]Could not stop: {e}[/]")

# ══════════════════════════════════════════════════════════
#  --list-presets COMMAND
# ══════════════════════════════════════════════════════════
def cmd_list_presets():
    presets = list_presets()
    if not presets:
        console.print("[yellow]No saved presets.[/]")
        console.print(f"[dim]Presets are saved during the wizard or with --save-preset NAME[/]")
        return
    console.print(Rule("[bold cyan]Saved Presets[/]"))
    for name, data in presets.items():
        strat = data.get("strategy", "?")
        cur   = data.get("currency", "?").upper()
        mult  = data.get("multiplier", "?")
        base  = data.get("base_bet", 0)
        rules = data.get("custom_rules_text", "")
        rule_count = len(rules.strip().splitlines()) if rules else 0
        extra = f"  ({rule_count} rules)" if rule_count else ""
        console.print(f"  [bold yellow]{name:20s}[/]  [cyan]{strat}[/] on [yellow]{cur}[/]  "
                      f"{mult}x  base={base:.8f}{extra}")
    console.print()

# ══════════════════════════════════════════════════════════
#  DATABASE QUERY COMMANDS
# ══════════════════════════════════════════════════════════
def cmd_stats():
    """Show all-time session statistics."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()

    # session list — fetch all columns
    rows = conn.execute("""
        SELECT id, substr(started_at,1,16) AS started, substr(ended_at,1,16) AS ended,
               UPPER(currency) AS cur, strategy, multiplier, base_bet,
               total_bets, wins, losses, profit, wagered,
               start_balance, end_balance,
               max_win_streak, max_loss_streak,
               COALESCE(highest_balance, 0), COALESCE(lowest_balance, 0),
               COALESCE(highest_win, 0), COALESCE(biggest_loss, 0),
               COALESCE(bets_per_minute, 0), COALESCE(bets_per_second, 0),
               COALESCE(peak_bps, 0), COALESCE(low_bps, 0),
               COALESCE(peak_bpm, 0), COALESCE(low_bpm, 0)
        FROM sessions ORDER BY id DESC LIMIT 20
    """).fetchall()

    console.print(Rule("[bold cyan]Session History (last 20)[/]"))

    # Print each session as a detailed block
    for r in rows:
        sid, started, ended, cur, strat, mult, base, bets, wins, losses, \
            profit, wagered, start_bal, end_bal, mws, mls, \
            hi_bal, lo_bal, hi_win, big_loss, \
            bpm, bps, s_peak_bps, s_low_bps, s_peak_bpm, s_low_bpm = r
        bets = bets or 0
        wins = wins or 0
        losses = losses or 0
        profit = profit or 0
        wagered = wagered or 0
        start_bal = start_bal or 0
        end_bal = end_bal or 0
        mws = mws or 0
        mls = mls or 0
        mult = mult or 0
        base = base or 0
        hi_bal = hi_bal or 0
        lo_bal = lo_bal or 0
        hi_win = hi_win or 0
        big_loss = big_loss or 0
        bpm = bpm or 0
        bps = bps or 0
        s_peak_bps = int(s_peak_bps or 0)
        s_low_bps = int(s_low_bps or 0)
        s_peak_bpm = int(s_peak_bpm or 0)
        s_low_bpm = int(s_low_bpm or 0)
        pc = "green" if profit >= 0 else "red"
        ps = "+" if profit >= 0 else ""
        wr = f"{wins/bets*100:.1f}%" if bets > 0 else "—"
        avg_pnl = profit / bets if bets > 0 else 0
        avg_c = "green" if avg_pnl >= 0 else "red"
        avg_s = "+" if avg_pnl >= 0 else ""

        console.print(f"  [bold cyan]Session #{sid}[/]  [dim]{(started or '?')[:16]} → {(ended or 'running')[:16]}[/]")
        console.print(f"    [dim]Currency:[/] {cur or '?'}  [dim]Strategy:[/] {strat or '?'}  [dim]Mult:[/] {mult}x  [dim]Base:[/] {base:.8f}")
        console.print(f"    [dim]Bets:[/] {bets}  [dim]W:[/] [green]{wins}[/]  [dim]L:[/] [red]{losses}[/]  [dim]WR:[/] {wr}")
        console.print(f"    [dim]Speed:[/] {bps:.1f} avg/s  {bpm:.0f} avg/m  [dim]BPS:[/] {s_low_bps}-{s_peak_bps}/s  [dim]BPM:[/] {s_low_bpm}-{s_peak_bpm}/m")
        console.print(f"    [dim]Profit:[/] [{pc}]{ps}{profit:.8f}[/]  [dim]Wagered:[/] {wagered:.8f}  [dim]Avg P/L:[/] [{avg_c}]{avg_s}{avg_pnl:.8f}[/]")
        console.print(f"    [dim]Balance:[/] {start_bal:.8f} → {end_bal:.8f}  [dim]Peak:[/] [green]{hi_bal:.8f}[/]  [dim]Low:[/] [red]{lo_bal:.8f}[/]")
        console.print(f"    [dim]Streaks:[/] W+{mws}  L-{mls}  [dim]Best Win:[/] [green]+{hi_win:.8f}[/]  [dim]Worst Loss:[/] [red]-{big_loss:.8f}[/]")
        console.print()

    # totals
    totals = conn.execute("""
        SELECT COUNT(*), COALESCE(SUM(total_bets),0), COALESCE(SUM(wins),0),
               COALESCE(SUM(losses),0), COALESCE(SUM(profit),0), COALESCE(SUM(wagered),0),
               COALESCE(MAX(max_win_streak),0), COALESCE(MAX(max_loss_streak),0),
               COALESCE(MAX(profit),0), COALESCE(MIN(profit),0),
               COALESCE(MAX(total_bets),0), COALESCE(AVG(total_bets),0),
               COALESCE(MAX(highest_balance),0), COALESCE(MAX(highest_win),0),
               COALESCE(MAX(biggest_loss),0), COALESCE(AVG(bets_per_minute),0),
               COALESCE(MAX(peak_bps),0), COALESCE(MAX(peak_bpm),0),
               COALESCE(AVG(bets_per_second),0)
        FROM sessions
    """).fetchone()
    conn.close()

    sessions, tot_bets, tot_wins, tot_losses, tot_profit, tot_wagered, \
        best_ws, best_ls, best_profit, worst_profit, max_bets, avg_bets, \
        all_hi_bal, all_hi_win, all_big_loss, avg_bpm, \
        all_peak_bps, all_peak_bpm, avg_bps = totals
    pc = "green" if tot_profit >= 0 else "red"
    ps = "+" if tot_profit >= 0 else ""
    wr = f"{tot_wins/tot_bets*100:.1f}%" if tot_bets > 0 else "—"
    bp_c = "green" if best_profit >= 0 else "red"
    wp_c = "green" if worst_profit >= 0 else "red"
    avg_pnl = tot_profit / tot_bets if tot_bets > 0 else 0
    avg_pc = "green" if avg_pnl >= 0 else "red"
    avg_ps = "+" if avg_pnl >= 0 else ""

    console.print(Rule("[bold cyan]All-Time Totals[/]"))
    console.print(f"  [dim]Sessions:[/]      {sessions}")
    console.print(f"  [dim]Total Bets:[/]    {tot_bets}  (W {tot_wins} / L {tot_losses}  WR {wr})")
    console.print(f"  [dim]Wagered:[/]       {tot_wagered:.8f}")
    console.print(f"  [dim]Profit:[/]        [{pc}]{ps}{tot_profit:.8f}[/]")
    console.print(f"  [dim]Avg P/L:[/]       [{avg_pc}]{avg_ps}{avg_pnl:.8f}[/] per bet")
    console.print(f"  [dim]Best Session:[/]  [{bp_c}]+{best_profit:.8f}[/]")
    console.print(f"  [dim]Worst Session:[/] [{wp_c}]{worst_profit:.8f}[/]")
    console.print(f"  [dim]Peak Balance:[/]  {all_hi_bal:.8f}")
    console.print(f"  [dim]Best Win:[/]      [green]+{all_hi_win:.8f}[/]")
    console.print(f"  [dim]Worst Loss:[/]    [red]-{all_big_loss:.8f}[/]")
    console.print(f"  [dim]Best Streaks:[/]  W+{best_ws}  L-{best_ls}")
    console.print(f"  [dim]Speed:[/]         {avg_bps:.1f} avg/s  {avg_bpm:.0f} avg/m  [dim]Peak:[/] {int(all_peak_bps)}/s  {int(all_peak_bpm)}/m")
    console.print(f"  [dim]Bets/Session:[/]  Max {max_bets}  Avg {avg_bets:.0f}")
    console.print()

def cmd_last_bets(n: int):
    """Show last N bets from database."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()
    rows = conn.execute("""
        SELECT b.id, b.session_id, substr(b.timestamp,1,19) AS dt,
               b.amount, b.result_value, b.state, b.profit, b.balance_after
        FROM bets b ORDER BY b.id DESC LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]No bets found.[/]")
        return

    console.print(Rule(f"[bold cyan]Last {len(rows)} Bets[/]"))
    t = Table(box=box.SIMPLE, expand=True)
    t.add_column("Bet",      width=7,  justify="right")
    t.add_column("Sess",     width=5,  justify="center")
    t.add_column("DateTime", width=19)
    t.add_column("Amount",   width=14, justify="right")
    t.add_column("Roll",     width=7,  justify="right")
    t.add_column("W/L",      width=4,  justify="center")
    t.add_column("P/L",      width=15, justify="right")
    t.add_column("Balance",  width=15, justify="right")

    for r in rows:
        bid, sid, dt, amt, roll, st, pnl, bal = r
        rc = "bold green" if st == "win" else "bold red"
        pc = "green" if (pnl or 0) >= 0 else "red"
        ps = "+" if (pnl or 0) >= 0 else ""
        t.add_row(
            str(bid), str(sid), dt or "?",
            f"{amt:.8f}" if amt else "?",
            f"{roll:.2f}" if roll else "?",
            Text("W" if st == "win" else "L", style=rc),
            Text(f"{ps}{pnl:.8f}" if pnl is not None else "?", style=pc),
            f"{bal:.8f}" if bal else "?"
        )
    console.print(t)
    console.print()

def cmd_session_bets(session_id: int):
    """Show bets and summary for a specific session."""
    if not os.path.exists(DB_PATH):
        console.print("[yellow]No database found.[/]")
        return
    conn = _db_conn()

    # session info
    sess = conn.execute("""
        SELECT id, started_at, ended_at, UPPER(currency), strategy,
               base_bet, multiplier, total_bets, wins, losses,
               profit, wagered, start_balance, end_balance,
               max_win_streak, max_loss_streak
        FROM sessions WHERE id = ?
    """, (session_id,)).fetchone()

    if not sess:
        console.print(f"[red]Session #{session_id} not found.[/]")
        conn.close()
        return

    sid, started, ended, cur, strat, base, mult, bets, wins, losses, \
        profit, wagered, start_bal, end_bal, mws, mls = sess
    profit = profit or 0
    bets = bets or 0
    wins = wins or 0
    losses = losses or 0

    pc = "green" if profit >= 0 else "red"
    ps = "+" if profit >= 0 else ""
    wr = f"{wins/bets*100:.1f}%" if bets > 0 else "—"

    console.print(Rule(f"[bold cyan]Session #{sid}[/]"))
    console.print(f"  [dim]Started:[/]    {(started or '?')[:19]}")
    console.print(f"  [dim]Ended:[/]      {(ended or 'running')[:19]}")
    console.print(f"  [dim]Currency:[/]   {cur}   [dim]Strategy:[/] {strat}   [dim]Mult:[/] {mult}x")
    console.print(f"  [dim]Bets:[/]       {bets}  (W {wins} / L {losses}  WR {wr})")
    console.print(f"  [dim]Profit:[/]     [{pc}]{ps}{profit:.8f}[/]   [dim]Wagered:[/] {wagered:.8f}")
    console.print(f"  [dim]Balance:[/]    {start_bal:.8f} → {(end_bal or 0):.8f}")
    console.print(f"  [dim]Streaks:[/]    W+{mws or 0}  L-{mls or 0}")

    # bets for this session
    rows = conn.execute("""
        SELECT id, substr(timestamp,12,8), amount, result_value, state, profit, balance_after
        FROM bets WHERE session_id = ? ORDER BY id DESC LIMIT 50
    """, (session_id,)).fetchall()
    conn.close()

    if rows:
        console.print()
        console.print(f"[dim]  Last {len(rows)} bets:[/]")
        t = Table(box=box.SIMPLE, expand=True)
        t.add_column("Bet",     width=7, justify="right")
        t.add_column("Time",    width=9)
        t.add_column("Amount",  width=14, justify="right")
        t.add_column("Roll",    width=7, justify="right")
        t.add_column("W/L",     width=4, justify="center")
        t.add_column("P/L",     width=15, justify="right")
        t.add_column("Balance", width=15, justify="right")

        for r in rows:
            bid, tm, amt, roll, st, pnl, bal = r
            rc = "bold green" if st == "win" else "bold red"
            pcc = "green" if (pnl or 0) >= 0 else "red"
            pss = "+" if (pnl or 0) >= 0 else ""
            t.add_row(
                str(bid), tm or "?",
                f"{amt:.8f}" if amt else "?",
                f"{roll:.2f}" if roll else "?",
                Text("W" if st == "win" else "L", style=rc),
                Text(f"{pss}{pnl:.8f}" if pnl is not None else "?", style=pcc),
                f"{bal:.8f}" if bal else "?"
            )
        console.print(t)
    console.print()

# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
def _load_and_connect():
    """Load saved config and fetch fresh balance. Used by --resume and --daemon."""
    if not load_config():
        console.print("[red]No saved config found. Run without --resume first.[/]")
        sys.exit(1)
    console.print("[dim]Loading saved config…[/]")
    try:
        balances = api_get_balances()
        for b in balances:
            if b["currency"] == state.currency:
                bal = float(b["amount"])
                state.start_balance   = bal
                state.current_balance = bal
                state.highest_balance = bal
                state.lowest_balance  = bal
                break
        console.print(f"[green]Resumed — {state.strategy} on {state.currency.upper()} "
                      f"@ {state.base_bet:.8f} base bet[/]")
    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/]")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="WolfBet AutoBot v" + VERSION)
    parser.add_argument("--resume",  action="store_true", help="Skip wizard, reuse saved config")
    parser.add_argument("--daemon",  action="store_true", help="Run in background (no TUI, implies --resume)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status",  action="store_true", help="Show status of running session")
    group.add_argument("--stop",    action="store_true", help="Stop a running daemon session")
    group.add_argument("--list-presets", action="store_true", help="List saved presets")
    group.add_argument("--stats",   action="store_true", help="Show all-time session statistics")
    group.add_argument("--last-bets", type=int, metavar="N", help="Show last N bets from database")
    group.add_argument("--session-bets", type=int, metavar="ID", help="Show bets for a specific session")
    parser.add_argument("--preset", type=str, metavar="NAME",
                        help="Load a named preset and start (skips wizard)")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging (dev mode)")

    args = parser.parse_args()

    # ── Verbose mode ──────────────────────────────────────
    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # ── --daemon implies --resume ──────────────────────────
    if args.daemon:
        args.resume = True

    # ── Quick commands (no DB/state needed) ───────────────
    if args.status:
        cmd_status()
        return
    if args.stop:
        cmd_stop()
        return
    if args.list_presets:
        cmd_list_presets()
        return
    if args.stats:
        cmd_stats()
        return
    if args.last_bets:
        cmd_last_bets(args.last_bets)
        return
    if args.session_bets is not None:
        cmd_session_bets(args.session_bets)
        return

    init_db()

    # ── Load config based on mode ─────────────────────────
    if args.preset:
        if not load_preset(args.preset):
            console.print(f"[red]Preset '{args.preset}' not found.[/]")
            cmd_list_presets()
            sys.exit(1)
        # need API key from main config
        if not state.api_key:
            saved = {}
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
            if saved.get("api_key"):
                state.api_key = saved["api_key"]
            else:
                console.print("[red]No API key found. Run wizard first.[/]")
                sys.exit(1)
        console.print(f"[green]Loaded preset '{args.preset}' — {state.strategy} on "
                      f"{state.currency.upper()} @ {state.base_bet:.8f}[/]")
        # fetch fresh balance
        try:
            balances = api_get_balances()
            for b in balances:
                if b["currency"] == state.currency:
                    bal = float(b["amount"])
                    state.start_balance   = bal
                    state.current_balance = bal
                    state.highest_balance = bal
                    state.lowest_balance  = bal
                    break
        except Exception as e:
            console.print(f"[red]Connection failed: {e}[/]")
            sys.exit(1)

    elif args.resume or args.daemon:
        _load_and_connect()
    else:
        setup_wizard()

    state.session_id    = _db_start_session()
    state.session_start = time.time()
    state.running       = True

    # write PID file
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    logger.warning("SESSION START  id=%s  mode=%s  currency=%s  strategy=%s  "
                   "multiplier=%s  base_bet=%.8f  balance=%.8f",
                   state.session_id,
                   "daemon" if args.daemon else ("preset" if args.preset else "tui"),
                   state.currency, state.strategy,
                   state.multiplier, state.base_bet,
                   state.current_balance)

    # ── Signal handlers ───────────────────────────────────
    def _shutdown(sig, frame):
        state.running = False
        state.status  = "Interrupted — saving session…"
        logger.warning("SESSION END (signal)  bets=%d  profit=%+.8f  balance=%.8f",
                    state.total_bets, state.profit, state.current_balance)
        _db_save_session()
        _save_state_file()
        _cleanup_state_file()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Betting thread ────────────────────────────────────
    t_bet = threading.Thread(target=betting_loop, daemon=True, name="BettingLoop")
    t_bet.start()

    # ── Periodic save (DB + state file) every 5s ─────────
    def periodic_save():
        while state.running or state.paused:
            time.sleep(5)
            _db_save_session()
            _save_state_file()
    t_save = threading.Thread(target=periodic_save, daemon=True, name="PeriodicSave")
    t_save.start()

    # ══════════════════════════════════════════════════════
    #  DAEMON MODE — no TUI, just bet in background
    # ══════════════════════════════════════════════════════
    if args.daemon:
        console.print(f"[bold green]Daemon started[/]  PID={os.getpid()}  Session #{state.session_id}")
        console.print(f"[dim]  Check status:  python3 wolfbet.py --status[/]")
        console.print(f"[dim]  Stop:          python3 wolfbet.py --stop[/]")
        console.print(f"[dim]  Logs:          tail -f ~/.wolfbet_logs/wolfbet.log[/]")
        try:
            while state.running:
                time.sleep(2)
                _save_state_file()
        except KeyboardInterrupt:
            pass
        state.running = False
        _db_save_session()
        _save_state_file()
        _cleanup_state_file()
        _print_summary()
        return

    # ══════════════════════════════════════════════════════
    #  TUI MODE — interactive dashboard (direct ANSI rendering)
    # ══════════════════════════════════════════════════════
    t_input = threading.Thread(target=input_handler, daemon=True, name="InputHandler")
    t_input.start()

    # Save terminal state and enter alternate screen buffer
    _old_term = None
    try:
        import termios
        _old_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    sys.stdout.write("\033[?1049h")  # enter alt screen
    sys.stdout.write("\033[?25l")    # hide cursor
    sys.stdout.flush()

    try:
        while state.running or state.paused:
            sys.stdout.write("\033[H")  # cursor home
            sys.stdout.write(build_dashboard_screen())
            sys.stdout.flush()
            time.sleep(0.5)

        # Final render
        sys.stdout.write("\033[H")
        sys.stdout.write(build_dashboard_screen())
        sys.stdout.flush()
        time.sleep(1.0)
    finally:
        # Restore terminal — order matters
        sys.stdout.write("\033[?25h")    # show cursor
        sys.stdout.write("\033[?1049l")  # leave alt screen
        sys.stdout.write("\033[0m")      # reset colors/attrs
        sys.stdout.flush()
        # Restore saved terminal settings first
        if _old_term is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSAFLUSH, _old_term)
            except Exception:
                pass
        # Always run stty sane to fix any remaining issues (raw mode, echo, etc.)
        import subprocess
        try:
            subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)
        except Exception:
            pass
        sys.stdout.write("\033c")  # full terminal reset (RIS)
        sys.stdout.flush()

    _db_save_session()
    _cleanup_state_file()
    _print_summary()


if __name__ == "__main__":
    main()

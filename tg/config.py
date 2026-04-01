"""Configuration — environment variables, constants, tier definitions."""

import os
import logging

# ── Environment ──────────────────────────────────────────
APP_ENV    = os.environ.get("APP_ENV", "production")
BOT_TOKEN  = os.environ.get("WOLFBET_TG_TOKEN", "")
DATA_DIR   = os.environ.get("WOLFBET_TG_DATA", os.path.expanduser("~/.wolfbot_tg"))
API_BASE   = os.environ.get("WOLFBET_API_BASE", "https://wolfbet.com/api")
WEB_URL    = os.environ.get("WOLFBET_WEB_URL", "")          # e.g. http://IP:8002
WEB_SECRET = os.environ.get("WOLFBET_WEB_SECRET_KEY", "")   # must match web app

# ── Rate limit tiers: seconds between bets ───────────────
TIERS = {
    "free":  1.0,    # 1 bet/sec
    "trial": 0.0,    # max speed
}

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wolfbot")

# Suppress noisy httpx request logs (Telegram polling etc)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Config keys persisted per user ───────────────────────
CONFIG_KEYS = [
    "api_key", "proxy", "pipeline", "currency", "game", "rule", "multiplier", "bet_value",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold",
]


def get_user_tier(user_id: int) -> str:
    """Return rate-limit tier for a user. Extensible for paid tiers."""
    return "trial"

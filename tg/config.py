"""Configuration: environment variables, constants, tier definitions."""

import os
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

TG_KEY_ID = "telegram"


def bot_token() -> str:
    """The bot token from the host key vault, else WOLFBET_TG_TOKEN; never logged."""
    try:
        from shared.core.vault import Vault, VaultError
    except ImportError:
        return os.environ.get("WOLFBET_TG_TOKEN", "")
    try:
        vault = Vault("wolfbet")
        info = vault.info(TG_KEY_ID)
        if info is not None and info.kind == "telegram":
            return vault.secret(TG_KEY_ID) or os.environ.get("WOLFBET_TG_TOKEN", "")
    except VaultError as e:
        logging.getLogger("wolfbot").warning("Key vault unavailable for the bot token: %s", e)
    return os.environ.get("WOLFBET_TG_TOKEN", "")


# ── Environment ──────────────────────────────────────────
APP_ENV    = os.environ.get("APP_ENV", "production")
BOT_TOKEN  = bot_token()
DATA_DIR   = os.environ.get("WOLFBET_TG_DATA", os.path.expanduser("~/.wolfbot_tg"))
API_BASE   = os.environ.get("WOLFBET_API_BASE", "https://wolfbet.com/api")

# ── Dashboard sign-in for /web: no link is sent until both are set ──
DASHBOARD_URL    = os.environ.get("WOLFBET_DASHBOARD_URL", "")          # e.g. https://wolfbet.example.com
DASHBOARD_SECRET = os.environ.get("WOLFBET_TG_DASHBOARD_SECRET", "")    # the dashboard's tg_login_secret

# ── Dashboard events: off unless WOLFBET_TG_EVENTS is on ──
EVENTS_ENABLED = os.environ.get("WOLFBET_TG_EVENTS", "").strip().lower() in ("1", "on", "true", "yes")
EVENTS_URL     = os.environ.get("WOLFBET_EVENTS_URL", "")      # same Redis the CLI daemon uses

# ── Control socket for the dashboard's host agent: off unless WOLFBET_TG_CONTROL is on ──
CONTROL_ENABLED    = os.environ.get("WOLFBET_TG_CONTROL", "").strip().lower() in ("1", "on", "true", "yes")
CONTROL_SOCKET     = os.path.expanduser(os.environ.get("WOLFBET_TG_CONTROL_SOCKET")
                                        or os.path.join(DATA_DIR, "control.sock"))
CONTROL_TOKEN_FILE = os.path.expanduser(os.environ.get("WOLFBET_TG_CONTROL_TOKEN_FILE")
                                        or os.path.join(DATA_DIR, "control.token"))
CONTROL_ALLOW_UIDS = os.environ.get("WOLFBET_TG_CONTROL_ALLOW_UIDS", "")   # extra uids; own uid always

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
    "api_key", "proxy", "pipeline", "purge_days", "streak_delay_loss", "streak_delay_win", "streak_delay_bets",
    "currency", "game", "rule", "multiplier", "bet_value",
    "base_bet", "strategy", "strategy_key", "win_mult", "loss_mult",
    "bet_delay", "max_profit", "max_loss", "max_bets", "max_wins",
    "stop_on_balance", "custom_rules_text", "delay_martin_threshold",
    "milestone_bets", "milestone_wins", "milestone_losses", "milestone_profit",
    "profit_increment", "profit_threshold", "profit_increment_scope",
    "streak_reset_losses", "streak_reset_delay",
]


def get_user_tier(user_id: int) -> str:
    """Return rate-limit tier for a user. Extensible for paid tiers."""
    return "trial"

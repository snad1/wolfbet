"""Application settings loaded from environment / .env file."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings

_env = os.getenv("APP_ENV", "production")
_env_file = ".env.testing" if _env == "testing" else ".env"


class Settings(BaseSettings):
    # ── App ──
    app_name: str = "WolfBet Admin"
    app_env: str = _env
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8002

    # ── Auth ──
    secret_key: str = "change-me-to-a-random-64-char-string"
    admin_user: str = "admin"
    admin_pass: str = "changeme123"
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24
    tg_token_expire_minutes: int = 5

    # ── Paths ──
    db_path: str = os.path.expanduser("~/.wolfbet_web.db")
    bot_data_dir: str = os.path.expanduser("~/.wolfbot_tg")
    install_dir: str = os.path.expanduser("~/wolfbet-bot")
    repo_dir: str = os.path.expanduser("~/wolfbet")

    # ── Services ──
    service_cli: str = "wolfbet"
    service_tg: str = "wolfbet-tg"
    service_web: str = "wolfbet-web"

    # ── Telegram ──
    tg_bot_token: str = ""

    model_config = {"env_prefix": "WOLFBET_WEB_", "env_file": _env_file, "extra": "ignore"}


settings = Settings()

# Resolved paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

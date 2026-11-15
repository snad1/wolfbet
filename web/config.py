"""Application settings loaded from environment / .env file."""

import os
from pathlib import Path
from pydantic import model_validator
from pydantic_settings import BaseSettings

from shared.web.database import DEFAULT_ADMIN_PASS

_env = os.getenv("APP_ENV", "production")
_env_file = ".env.testing" if _env == "testing" else ".env"

DEFAULT_SECRET_KEY = "change-me-to-a-random-64-char-string"
MIN_SECRET_BYTES = 32
MIN_ADMIN_PASS_CHARS = 12


class Settings(BaseSettings):
    # ── App ──
    app_name: str = "WolfBet Admin"
    app_env: str = _env
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 8002

    # ── Auth ──
    secret_key: str = DEFAULT_SECRET_KEY
    admin_user: str = "admin"
    admin_pass: str = DEFAULT_ADMIN_PASS
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

    @model_validator(mode="after")
    def _reject_weak_auth(self):
        if self.secret_key == DEFAULT_SECRET_KEY or len(self.secret_key.encode()) < MIN_SECRET_BYTES:
            raise ValueError(
                "WOLFBET_WEB_SECRET_KEY must be set to at least "
                f"{MIN_SECRET_BYTES} bytes and must not be the shipped default. "
                'Generate one with: python3 -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if self.admin_pass == DEFAULT_ADMIN_PASS or len(self.admin_pass) < MIN_ADMIN_PASS_CHARS:
            raise ValueError(
                "WOLFBET_WEB_ADMIN_PASS must be at least "
                f"{MIN_ADMIN_PASS_CHARS} characters and must not be the shipped default"
            )
        return self


settings = Settings()

# Resolved paths
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

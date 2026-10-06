"""Настройки приложения из переменных окружения и .env."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # CRM_ENV_FILE="" отключает чтение .env (так делают тесты)
        env_file=os.environ.get("CRM_ENV_FILE", BASE_DIR / ".env") or None,
        env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'crm.db'}"
    data_dir: Path = BASE_DIR / "data"

    secret_key: str = "change-me"
    admin_username: str = "admin"
    admin_password: str = "admin"
    session_max_age: int = 60 * 60 * 24 * 7
    # Cookie только по HTTPS — включать, когда сайт открыт через туннель/HTTPS
    session_https_only: bool = False
    # Сколько неудачных входов с одного IP допускается за login_lockout_seconds
    login_max_failures: int = 5
    login_lockout_seconds: int = 300

    # Человек пишет снова, а его лид уже «Готово»/«Отказ»: новый лид или переоткрыть старый
    reopen_policy: Literal["new_lead", "append"] = "new_lead"

    # Telegram-бот (aiogram)
    bot_token: str = ""
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str = ""
    webhook_path: str = "/telegram/webhook"
    webhook_secret: str = ""

    # Личный Telegram-аккаунт (Telethon)
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_ignore_contacts: bool = False
    tg_account_enabled: bool = True

    leads_per_page: int = 20
    timezone: str = "Europe/Moscow"
    log_level: str = "INFO"

    @property
    def telethon_session_path(self) -> Path:
        return self.data_dir / "telethon" / "account"


@lru_cache
def get_settings() -> Settings:
    return Settings()

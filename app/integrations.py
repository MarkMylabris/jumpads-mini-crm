"""Состояние фоновых интеграций (бот, аккаунт) для страницы «Подключение Telegram»."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

@dataclass
class IntegrationStatus:
    # disabled | starting | running | reconnecting | need_login | awaiting_code | awaiting_password | error | stopped
    status: str = "disabled"
    detail: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_error: str = ""
    last_error_at: datetime | None = None

    def set(self, status: str, detail: str = "") -> None:
        self.status, self.detail = status, detail
        self.updated_at = datetime.now(timezone.utc)
        if status == "error" or (status == "reconnecting" and detail):
            self.last_error, self.last_error_at = detail, self.updated_at


STATUS = {"bot": IntegrationStatus(), "account": IntegrationStatus()}

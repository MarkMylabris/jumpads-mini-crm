"""Jinja2: подписи статусов и источников, фильтры для дат и цвета тегов."""

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.models import LeadSource, LeadStatus

TEMPLATES_DIR = Path(__file__).parent / "templates"

STATUS_LABELS = {
    LeadStatus.new: "Новый",
    LeadStatus.in_progress: "В работе",
    LeadStatus.done: "Готово",
    LeadStatus.rejected: "Отказ",
}
SOURCE_LABELS = {
    LeadSource.bot: "Бот",
    LeadSource.telegram_account: "Telegram",
    LeadSource.manual: "Вручную",
}

_tz = ZoneInfo(get_settings().timezone)


def fmt_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return ""
    if value.tzinfo is None:  # SQLite отдаёт даты без пояса, храним всегда UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_tz).strftime(fmt)


def text_color(bg: str) -> str:
    """Чёрный или белый текст — что лучше читается на фоне #rrggbb."""
    try:
        r, g, b = (int(bg[i : i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return "#fff"
    return "#000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#fff"


templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.filters["dt"] = fmt_dt
templates.env.filters["text_color"] = text_color
templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS,
    SOURCE_LABELS=SOURCE_LABELS,
    LeadStatus=LeadStatus,
    LeadSource=LeadSource,
)

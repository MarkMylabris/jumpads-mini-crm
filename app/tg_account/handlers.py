"""Личные сообщения подключённого аккаунта -> лиды.

Не зависит от TelegramClient, поэтому тестируется на фейковых событиях.
"""

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker
from telethon.tl.types import User

from app.config import get_settings
from app.models import LeadSource
from app.services import leads as svc

log = logging.getLogger("crm.tg_account")

TELEGRAM_SERVICE_USER_ID = 777000  # служебный аккаунт «Telegram», присылает коды входа
IGNORE_CONTACTS_KEY = "tg_ignore_contacts"


def message_text(msg) -> str:
    """Текст сообщения; для медиа — пометка вида «[фото] подпись»."""
    text = (getattr(msg, "message", None) or "").strip()
    kind = None
    if getattr(msg, "photo", None):
        kind = "фото"
    elif getattr(msg, "voice", None):
        kind = "голосовое сообщение"
    elif getattr(msg, "video_note", None):
        kind = "видеосообщение"
    elif getattr(msg, "video", None):
        kind = "видео"
    elif getattr(msg, "sticker", None):
        kind = "стикер"
    elif getattr(msg, "document", None):
        name = getattr(getattr(msg, "file", None), "name", None)
        kind = f"файл: {name}" if name else "файл"
    elif getattr(msg, "geo", None):
        kind = "геопозиция"
    elif getattr(msg, "contact", None):
        kind = "контакт"
    elif getattr(msg, "media", None):
        kind = "вложение"
    if kind:
        return f"[{kind}] {text}".strip()
    return text


def display_name(user: User) -> str:
    name = " ".join(p for p in (user.first_name, user.last_name) if p).strip()
    return name or (f"@{user.username}" if user.username else "") or (f"+{user.phone}" if user.phone else "")


def contact_of(user: User) -> str:
    if user.username:
        return f"@{user.username}"
    if user.phone:
        return f"+{user.phone.lstrip('+')}"
    return f"tg://user?id={user.id}"


async def ignore_contacts_enabled(session_factory: async_sessionmaker) -> bool:
    async with session_factory() as session:
        value = await svc.get_setting(session, IGNORE_CONTACTS_KEY)
    return get_settings().tg_ignore_contacts if value is None else value == "1"


async def handle_new_message(event, session_factory: async_sessionmaker) -> str:
    """Обрабатывает событие NewMessage. Возвращает код результата (для логов и тестов)."""
    if not event.is_private:
        return "ignored:not_private"
    msg = event.message
    if getattr(msg, "action", None) is not None:
        return "ignored:service"
    text = message_text(msg)
    if not text:
        return "ignored:empty"

    if event.out:
        # Свой исходящий ответ: только в историю существующего лида, новых лидов не создаём.
        async with session_factory() as session:
            lead = await svc.record_outgoing(
                session, source=LeadSource.telegram_account, tg_user_id=event.chat_id, text=text
            )
        return "outgoing:recorded" if lead else "ignored:outgoing"

    sender = await event.get_sender()
    if not isinstance(sender, User):
        return "ignored:not_user"
    if sender.bot:
        return "ignored:bot"
    if sender.is_self or sender.support or sender.id == TELEGRAM_SERVICE_USER_ID:
        return "ignored:service_user"

    async with session_factory() as session:
        if sender.contact and await ignore_contacts_enabled(session_factory):
            # Флаг отсекает только новых лидов: если лид уже есть (клиента добавили
            # в контакты), его история должна оставаться полной.
            if await svc.find_target_lead(session, LeadSource.telegram_account, sender.id) is None:
                return "ignored:contact"
        result = await svc.ingest_telegram_message(
            session,
            source=LeadSource.telegram_account,
            tg_user_id=sender.id,
            name=display_name(sender),
            contact=contact_of(sender),
            text=text,
        )
    return "lead:created" if result.created else "lead:appended"

"""Бизнес-логика: лиды, история, теги, фильтры.

Через этот модуль работают и веб, и бот, и Telegram-аккаунт,
поэтому правила (валидация, дедупликация, логи) одинаковы для всех источников.
"""

import logging
import math
import re
from dataclasses import dataclass, field

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models import (
    CLOSED_STATUSES,
    Lead,
    LeadSource,
    LeadStatus,
    Message,
    MessageDirection,
    MessageKind,
    Setting,
    Tag,
    lead_tags,
    utcnow,
)

log = logging.getLogger("crm.leads")

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_TAG_COLOR = "#6c757d"


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


# --- выборки


def _lead_query() -> Select:
    return select(Lead).options(selectinload(Lead.tags))


async def get_lead(session: AsyncSession, lead_id: int, *, with_messages: bool = False) -> Lead | None:
    stmt = _lead_query().where(Lead.id == lead_id)
    if with_messages:
        stmt = stmt.options(selectinload(Lead.messages))
    return (await session.execute(stmt)).scalar_one_or_none()


@dataclass
class LeadFilter:
    q: str = ""
    source: str = ""
    status: str = ""
    tags: list[str] = field(default_factory=list)  # имена тегов
    tag_mode: str = "any"  # any | all
    page: int = 1

    def query_params(self, **override) -> list[tuple[str, str]]:
        """Фильтр обратно в параметры URL — для пагинации и автообновления."""
        data = {"q": self.q, "source": self.source, "status": self.status, "tag_mode": self.tag_mode, "page": self.page}
        data.update(override)
        params = [(k, str(v)) for k, v in data.items() if v not in ("", None) and not (k == "tag_mode" and v == "any")]
        params += [("tag", t) for t in self.tags]
        if ("page", "1") in params:
            params.remove(("page", "1"))
        return params


@dataclass
class Page:
    items: list[Lead]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page))


def _apply_filter(stmt: Select, f: LeadFilter) -> Select:
    if f.q.strip():
        pattern = f"%{f.q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Lead.name).like(pattern),
                func.lower(Lead.contact).like(pattern),
                func.lower(Lead.request).like(pattern),
            )
        )
    if f.source in LeadSource._value2member_map_:
        stmt = stmt.where(Lead.source == LeadSource(f.source))
    if f.status in LeadStatus._value2member_map_:
        stmt = stmt.where(Lead.status == LeadStatus(f.status))
    tag_names = sorted({t.strip().lower() for t in f.tags if t.strip()})
    if tag_names:
        matching = (
            select(lead_tags.c.lead_id)
            .join(Tag, Tag.id == lead_tags.c.tag_id)
            .where(func.lower(Tag.name).in_(tag_names))
            .group_by(lead_tags.c.lead_id)
        )
        if f.tag_mode == "all":
            matching = matching.having(func.count(func.distinct(Tag.id)) == len(tag_names))
        stmt = stmt.where(Lead.id.in_(matching))
    return stmt


async def list_leads(session: AsyncSession, f: LeadFilter, per_page: int | None = None) -> Page:
    per_page = per_page or get_settings().leads_per_page
    total = (await session.execute(_apply_filter(select(func.count(Lead.id)), f))).scalar_one()
    pages = max(1, math.ceil(total / per_page))
    page = min(max(1, f.page), pages)
    stmt = (
        _apply_filter(_lead_query(), f)
        .order_by(Lead.created_at.desc(), Lead.id.desc())
        .limit(per_page)
        .offset((page - 1) * per_page)
    )
    items = list((await session.execute(stmt)).scalars())
    return Page(items=items, total=total, page=page, per_page=per_page)


# --- ручные лиды


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _validate_lead_fields(name: str, contact: str, status: str) -> dict[str, str]:
    errors = {}
    if not name:
        errors["name"] = "Укажите имя"
    elif len(name) > 255:
        errors["name"] = "Слишком длинное имя"
    if not contact:
        errors["contact"] = "Укажите контакт"
    elif len(contact) > 255:
        errors["contact"] = "Слишком длинный контакт"
    if status not in LeadStatus._value2member_map_:
        errors["status"] = "Неизвестный статус"
    return errors


async def _tags_by_ids(session: AsyncSession, tag_ids: list[int]) -> list[Tag]:
    if not tag_ids:
        return []
    return list((await session.execute(select(Tag).where(Tag.id.in_(tag_ids)))).scalars())


async def create_manual_lead(
    session: AsyncSession,
    *,
    name: str,
    contact: str,
    request: str = "",
    status: str = LeadStatus.new.value,
    tag_ids: list[int] | None = None,
) -> Lead:
    name, contact, request = _clean(name), _clean(contact), _clean(request)
    errors = _validate_lead_fields(name, contact, status)
    if errors:
        raise ValidationError(errors)
    lead = Lead(
        name=name,
        contact=contact,
        request=request,
        source=LeadSource.manual,
        status=LeadStatus(status),
        tags=await _tags_by_ids(session, tag_ids or []),
    )
    session.add(lead)
    await session.commit()
    log.info("New lead #%s (source=manual, name=%r)", lead.id, lead.name)
    return lead


async def update_lead(
    session: AsyncSession,
    lead: Lead,
    *,
    name: str,
    contact: str,
    request: str,
    status: str,
    tag_ids: list[int] | None = None,
) -> Lead:
    name, contact, request = _clean(name), _clean(contact), _clean(request)
    errors = _validate_lead_fields(name, contact, status)
    if errors:
        raise ValidationError(errors)
    old_status = lead.status
    lead.name, lead.contact, lead.request = name, contact, request
    lead.status = LeadStatus(status)
    if tag_ids is not None:
        lead.tags = await _tags_by_ids(session, tag_ids)
    if old_status != lead.status:
        session.add(
            Message(
                lead_id=lead.id,
                direction=MessageDirection.outbound,
                kind=MessageKind.system,
                text=f"Статус: {old_status.value} → {lead.status.value}",
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ValidationError(
            {"status": "У этого Telegram-пользователя уже есть открытый лид из того же источника"}
        ) from None
    return lead


async def delete_lead(session: AsyncSession, lead: Lead) -> None:
    await session.delete(lead)
    await session.commit()
    log.info("Lead #%s deleted", lead.id)


async def add_note(session: AsyncSession, lead: Lead, text: str) -> Message:
    text = _clean(text)
    if not text:
        raise ValidationError({"text": "Пустая заметка"})
    msg = Message(lead_id=lead.id, direction=MessageDirection.outbound, kind=MessageKind.note, text=text)
    session.add(msg)
    lead.updated_at = utcnow()
    await session.commit()
    return msg


# --- сообщения из Telegram


@dataclass
class IngestResult:
    lead: Lead
    created: bool
    reopened: bool = False


async def find_target_lead(session: AsyncSession, source: LeadSource, tg_user_id: int) -> Lead | None:
    """Открытый лид пользователя, а при REOPEN_POLICY=append — последний закрытый."""
    base = select(Lead).where(Lead.source == source, Lead.telegram_user_id == tg_user_id)
    open_lead = (
        await session.execute(base.where(Lead.status.not_in(CLOSED_STATUSES)).order_by(Lead.id.desc()).limit(1))
    ).scalar_one_or_none()
    if open_lead or get_settings().reopen_policy != "append":
        return open_lead
    return (await session.execute(base.order_by(Lead.id.desc()).limit(1))).scalar_one_or_none()


async def ingest_telegram_message(
    session: AsyncSession,
    *,
    source: LeadSource,
    tg_user_id: int,
    name: str,
    contact: str,
    text: str,
    request: str | None = None,
) -> IngestResult:
    """Входящее сообщение из Telegram -> в историю лида; лида нет — создаём.

    Дедупликация: один открытый лид на (source, telegram_user_id). Если прошлый лид
    закрыт, REOPEN_POLICY решает: новый лид или переоткрыть старый.
    """
    for attempt in range(2):
        lead = await find_target_lead(session, source, tg_user_id)
        if lead is not None:
            reopened = lead.status in CLOSED_STATUSES
            if reopened:
                old = lead.status
                lead.status = LeadStatus.new
                session.add(
                    Message(
                        lead_id=lead.id,
                        direction=MessageDirection.outbound,
                        kind=MessageKind.system,
                        text=f"Повторное обращение: статус {old.value} → new",
                    )
                )
            session.add(Message(lead_id=lead.id, direction=MessageDirection.inbound, text=text))
            lead.updated_at = utcnow()
            await session.commit()
            log.info("Message appended to lead #%s (source=%s, tg_user=%s)", lead.id, source.value, tg_user_id)
            return IngestResult(lead=lead, created=False, reopened=reopened)

        lead = Lead(
            name=_clean(name)[:255] or f"Telegram {tg_user_id}",
            contact=_clean(contact)[:255] or f"tg://user?id={tg_user_id}",
            telegram_user_id=tg_user_id,
            request=request if request is not None else text,
            source=source,
            status=LeadStatus.new,
        )
        lead.messages = [Message(direction=MessageDirection.inbound, text=text)]
        session.add(lead)
        try:
            await session.commit()
        except IntegrityError:
            # Параллельный обработчик успел создать лида — повторяем и дописываем к нему.
            await session.rollback()
            if attempt:
                raise
            continue
        log.info("New lead #%s (source=%s, tg_user=%s, name=%r)", lead.id, source.value, tg_user_id, lead.name)
        return IngestResult(lead=lead, created=True)
    raise RuntimeError("unreachable")


async def record_outgoing(
    session: AsyncSession, *, source: LeadSource, tg_user_id: int, text: str, kind: MessageKind = MessageKind.message
) -> Lead | None:
    """Исходящее сообщение -> в историю последнего лида пользователя. Лидов не создаёт."""
    lead = (
        await session.execute(
            select(Lead)
            .where(Lead.source == source, Lead.telegram_user_id == tg_user_id)
            .order_by(Lead.status.in_(CLOSED_STATUSES), Lead.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if lead is None:
        return None
    session.add(Message(lead_id=lead.id, direction=MessageDirection.outbound, kind=kind, text=text))
    await session.commit()
    return lead


# --- теги


async def list_tags(session: AsyncSession) -> list[Tag]:
    return list((await session.execute(select(Tag).order_by(func.lower(Tag.name)))).scalars())


async def tag_usage(session: AsyncSession) -> dict[int, int]:
    rows = await session.execute(select(lead_tags.c.tag_id, func.count()).group_by(lead_tags.c.tag_id))
    return dict(rows.all())


def _validate_tag(name: str, color: str) -> dict[str, str]:
    errors = {}
    if not name:
        errors["name"] = "Укажите название"
    elif len(name) > 64:
        errors["name"] = "Слишком длинное название"
    if not COLOR_RE.match(color):
        errors["color"] = "Цвет в формате #rrggbb"
    return errors


async def _tag_name_taken(session: AsyncSession, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(Tag.id).where(func.lower(Tag.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(Tag.id != exclude_id)
    return (await session.execute(stmt)).first() is not None


async def create_tag(session: AsyncSession, name: str, color: str = DEFAULT_TAG_COLOR) -> Tag:
    name, color = _clean(name), _clean(color) or DEFAULT_TAG_COLOR
    errors = _validate_tag(name, color)
    if not errors and await _tag_name_taken(session, name):
        errors["name"] = "Такой тег уже есть"
    if errors:
        raise ValidationError(errors)
    tag = Tag(name=name, color=color.lower())
    session.add(tag)
    await session.commit()
    return tag


async def update_tag(session: AsyncSession, tag: Tag, name: str, color: str) -> Tag:
    name, color = _clean(name), _clean(color) or tag.color
    errors = _validate_tag(name, color)
    if not errors and await _tag_name_taken(session, name, exclude_id=tag.id):
        errors["name"] = "Такой тег уже есть"
    if errors:
        raise ValidationError(errors)
    tag.name, tag.color = name, color.lower()
    await session.commit()
    return tag


async def delete_tag(session: AsyncSession, tag: Tag) -> None:
    await session.execute(delete(lead_tags).where(lead_tags.c.tag_id == tag.id))
    await session.delete(tag)
    await session.commit()


async def toggle_lead_tag(session: AsyncSession, lead: Lead, tag: Tag) -> bool:
    """Переключает тег на лиде. True — тег теперь назначен."""
    if any(t.id == tag.id for t in lead.tags):
        lead.tags = [t for t in lead.tags if t.id != tag.id]
        assigned = False
    else:
        lead.tags = [*lead.tags, tag]
        assigned = True
    await session.commit()
    return assigned


# --- настройки из интерфейса


async def get_setting(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = await session.get(Setting, key)
    return row.value if row else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    await session.commit()

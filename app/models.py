"""Модели БД: лиды, сообщения, теги и настройки."""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LeadSource(str, enum.Enum):
    bot = "bot"
    telegram_account = "telegram_account"
    manual = "manual"


class LeadStatus(str, enum.Enum):
    new = "new"
    in_progress = "in_progress"
    done = "done"
    rejected = "rejected"


CLOSED_STATUSES = (LeadStatus.done, LeadStatus.rejected)


class MessageDirection(str, enum.Enum):
    inbound = "in"
    outbound = "out"


class MessageKind(str, enum.Enum):
    message = "message"  # сообщение из Telegram
    note = "note"  # заметка менеджера
    system = "system"  # служебное событие: смена статуса, переоткрытие


def _enum(cls):
    return Enum(cls, values_callable=lambda e: [m.value for m in e], native_enum=False, length=32)


lead_tags = Table(
    "lead_tags",
    Base.metadata,
    Column("lead_id", ForeignKey("leads.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Lead(Base):
    __tablename__ = "leads"
    # Дедупликация на уровне БД: у Telegram-пользователя в одном источнике может быть
    # сколько угодно закрытых лидов, но только один открытый. Частичный уникальный индекс
    # (есть и в SQLite, и в PostgreSQL) не даст создать дубль даже при гонке обработчиков.
    __table_args__ = (
        Index(
            "uq_lead_open_per_tg_user",
            "source",
            "telegram_user_id",
            unique=True,
            sqlite_where=text("telegram_user_id IS NOT NULL AND status IN ('new', 'in_progress')"),
            postgresql_where=text("telegram_user_id IS NOT NULL AND status IN ('new', 'in_progress')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    contact: Mapped[str] = mapped_column(String(255))
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    request: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[LeadSource] = mapped_column(_enum(LeadSource), index=True)
    status: Mapped[LeadStatus] = mapped_column(_enum(LeadStatus), default=LeadStatus.new, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    tags: Mapped[list["Tag"]] = relationship(secondary=lead_tags, back_populates="leads", order_by="Tag.name")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="Message.created_at, Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    direction: Mapped[MessageDirection] = mapped_column(_enum(MessageDirection))
    kind: Mapped[MessageKind] = mapped_column(_enum(MessageKind), default=MessageKind.message)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="messages")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    color: Mapped[str] = mapped_column(String(7), default="#6c757d")

    leads: Mapped[list[Lead]] = relationship(secondary=lead_tags, back_populates="tags")


class Setting(Base):
    """Настройки, которые меняются из интерфейса (например, «игнорировать контакты»)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)

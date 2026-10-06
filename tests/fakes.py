"""Минимальные заглушки апдейтов aiogram и Telethon."""

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


def tg_user(user_id=1001, first_name="Пётр", last_name=None, username="petr"):
    return SimpleNamespace(id=user_id, first_name=first_name, last_name=last_name, username=username, is_bot=False)


@dataclass
class FakeMessage:
    text: str | None = None
    from_user: SimpleNamespace = field(default_factory=tg_user)
    contact: SimpleNamespace | None = None
    answer: AsyncMock = field(default_factory=AsyncMock)
    edit_reply_markup: AsyncMock = field(default_factory=AsyncMock)

    @property
    def chat(self):
        return SimpleNamespace(id=self.from_user.id, type="private")

    def last_answer(self) -> str:
        return self.answer.await_args.args[0]


@dataclass
class FakeCallback:
    data: str
    from_user: SimpleNamespace = field(default_factory=tg_user)
    message: FakeMessage = field(default_factory=FakeMessage)
    answer: AsyncMock = field(default_factory=AsyncMock)


def fsm_context(user_id=1001, storage=None) -> FSMContext:
    return FSMContext(storage=storage or MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


# --- Telethon


def tl_user(user_id=5551, first_name="Ольга", last_name=None, username="olga", phone="79990001122", **flags):
    from telethon.tl.types import User

    return User(id=user_id, first_name=first_name, last_name=last_name, username=username, phone=phone, **flags)


def tl_message(text="Здравствуйте, сколько стоит реклама?", **media):
    fields = dict(message=text, action=None, media=None, photo=None, voice=None, video_note=None, video=None,
                  sticker=None, document=None, geo=None, contact=None, file=None)
    fields.update(media)
    return SimpleNamespace(**fields)


class FakeEvent:
    """Подделка под telethon.events.NewMessage.Event."""

    def __init__(self, sender=None, message=None, *, is_private=True, out=False, chat_id=None):
        self._sender = sender if sender is not None else tl_user()
        self.message = message or tl_message()
        self.is_private = is_private
        self.out = out
        self.chat_id = chat_id if chat_id is not None else getattr(self._sender, "id", None)

    async def get_sender(self):
        return self._sender

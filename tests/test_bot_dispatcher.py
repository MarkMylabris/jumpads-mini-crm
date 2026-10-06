"""Сквозной тест через настоящий Dispatcher aiogram; HTTP-запросы к Telegram подменены."""

import itertools
from datetime import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update
from sqlalchemy import select

from app.bot.runner import build_dispatcher
from app.db import SessionLocal
from app.models import Lead, LeadSource


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=1, date=datetime.now(), chat=Chat(id=method.chat_id, type="private"), text=method.text)
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def close(self):
        pass


_ids = itertools.count(1)
USER = {"id": 777, "is_bot": False, "first_name": "Ева", "username": "eva"}


def msg_update(text=None, chat_type="private", **extra):
    message = {"message_id": next(_ids), "date": 0, "chat": {"id": 777 if chat_type == "private" else -100, "type": chat_type},
               "from": USER, **extra}
    if text is not None:
        message["text"] = text
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.model_validate({"update_id": next(_ids), "message": message})


def cb_update(data):
    return Update.model_validate({"update_id": next(_ids), "callback_query": {
        "id": str(next(_ids)), "from": USER, "chat_instance": "x", "data": data,
        "message": {"message_id": 5, "date": 0, "chat": {"id": 777, "type": "private"}, "from": USER, "text": "summary"},
    }})


async def test_full_dialog_through_dispatcher(session):
    fake = FakeSession()
    bot = Bot("42:TEST", session=fake)
    dp = build_dispatcher(SessionLocal)

    await dp.feed_update(bot, msg_update("/start"))
    await dp.feed_update(bot, msg_update("Ева"))
    await dp.feed_update(bot, msg_update(contact={"phone_number": "79001234567", "first_name": "Ева", "user_id": 777}))
    await dp.feed_update(bot, msg_update("Настроить таргет"))
    await dp.feed_update(bot, cb_update("lead:confirm"))
    await dp.feed_update(bot, cb_update("lead:confirm"))  # двойной клик

    texts = [m.text for m in fake.sent if isinstance(m, SendMessage)]
    assert texts[-1] == "Заявка принята! Мы свяжемся с вами в ближайшее время."
    leads = list((await session.execute(select(Lead).where(Lead.telegram_user_id == 777))).scalars())
    assert len(leads) == 1
    assert (leads[0].source, leads[0].contact, leads[0].request) == (LeadSource.bot, "+79001234567", "Настроить таргет")


async def test_group_messages_are_ignored(session):
    fake = FakeSession()
    bot = Bot("42:TEST", session=fake)
    dp = build_dispatcher(SessionLocal)
    await dp.feed_update(bot, msg_update("/start", chat_type="group"))
    assert fake.sent == []

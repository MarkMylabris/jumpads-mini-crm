from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from telethon import errors
from telethon.tl.types import Channel

from app.db import SessionLocal
from app.integrations import STATUS
from app.models import Lead, LeadSource, LeadStatus, MessageDirection
from app.services import leads as svc
from app.tg_account.handlers import IGNORE_CONTACTS_KEY, handle_new_message
from app.tg_account.manager import AccountManager, LoginError
from tests.fakes import FakeEvent, tl_message, tl_user


async def handle(event):
    return await handle_new_message(event, SessionLocal)


async def account_leads(session):
    stmt = select(Lead).where(Lead.source == LeadSource.telegram_account).order_by(Lead.id)
    return list((await session.execute(stmt)).scalars())


async def test_new_sender_creates_lead(session):
    assert await handle(FakeEvent()) == "lead:created"
    [lead] = await account_leads(session)
    assert (lead.name, lead.contact, lead.telegram_user_id) == ("Ольга", "@olga", 5551)
    assert lead.request == "Здравствуйте, сколько стоит реклама?"


async def test_second_message_goes_to_history_not_new_lead(session):
    await handle(FakeEvent())
    assert await handle(FakeEvent(message=tl_message("А сроки?"))) == "lead:appended"
    [lead] = await account_leads(session)
    lead = await svc.get_lead(session, lead.id, with_messages=True)
    assert [m.text for m in lead.messages] == ["Здравствуйте, сколько стоит реклама?", "А сроки?"]
    assert lead.request == "Здравствуйте, сколько стоит реклама?"


async def test_contact_falls_back_to_phone(session):
    await handle(FakeEvent(tl_user(user_id=42, username=None, phone="79001112233", last_name="Ли")))
    [lead] = await account_leads(session)
    assert (lead.name, lead.contact) == ("Ольга Ли", "+79001112233")


@pytest.mark.parametrize(
    "event, outcome",
    [
        (FakeEvent(is_private=False), "ignored:not_private"),
        (FakeEvent(tl_user(bot=True)), "ignored:bot"),
        (FakeEvent(tl_user(user_id=777000, username=None)), "ignored:service_user"),
        (FakeEvent(tl_user(is_self=True)), "ignored:service_user"),
        (FakeEvent(message=tl_message("", action=object())), "ignored:service"),
        (FakeEvent(Channel(id=1, title="c", photo=None, date=None)), "ignored:not_user"),
        (FakeEvent(out=True), "ignored:outgoing"),
    ],
)
async def test_ignored_updates(session, event, outcome):
    assert await handle(event) == outcome
    assert await account_leads(session) == []


async def test_own_outgoing_is_recorded_in_existing_lead(session):
    await handle(FakeEvent())
    assert await handle(FakeEvent(message=tl_message("Добрый день! 10 000 ₽"), out=True, chat_id=5551)) == "outgoing:recorded"
    [lead] = await account_leads(session)
    lead = await svc.get_lead(session, lead.id, with_messages=True)
    assert lead.messages[-1].direction == MessageDirection.outbound


async def test_media_placeholder(session):
    await handle(FakeEvent(message=tl_message("", photo=object(), media=object())))
    [lead] = await account_leads(session)
    assert lead.request == "[фото]"


async def test_ignore_contacts_flag(session):
    contact = tl_user(user_id=9, contact=True)
    await svc.set_setting(session, IGNORE_CONTACTS_KEY, "1")
    assert await handle(FakeEvent(contact)) == "ignored:contact"
    assert await handle(FakeEvent(tl_user(user_id=10))) == "lead:created"  # не-контакты по-прежнему становятся лидами

    await svc.set_setting(session, IGNORE_CONTACTS_KEY, "0")
    assert await handle(FakeEvent(contact)) == "lead:created"
    await svc.set_setting(session, IGNORE_CONTACTS_KEY, "1")
    assert await handle(FakeEvent(contact)) == "lead:appended"  # у существующего лида история полная


async def test_message_after_done_creates_new_lead(session):
    await handle(FakeEvent())
    [lead] = await account_leads(session)
    lead.status = LeadStatus.done
    await session.commit()
    assert await handle(FakeEvent(message=tl_message("Снова я"))) == "lead:created"
    session.expunge_all()
    assert len(await account_leads(session)) == 2


async def test_bot_and_account_leads_are_separate(session):
    """Один человек через бота и через аккаунт -> два лида (уникальность в рамках источника)."""
    await svc.ingest_telegram_message(session, source=LeadSource.bot, tg_user_id=5551, name="Ольга", contact="@olga", text="x")
    await handle(FakeEvent())
    total = (await session.execute(select(func.count(Lead.id)).where(Lead.telegram_user_id == 5551))).scalar_one()
    assert total == 2


# --- вход с подменённым клиентом


@pytest.fixture
def manager(settings, monkeypatch):
    monkeypatch.setattr(settings, "tg_api_id", 12345)
    monkeypatch.setattr(settings, "tg_api_hash", "hash")
    m = AccountManager(settings, SessionLocal)
    client = MagicMock()
    client.is_connected.return_value = True
    client.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash="HASH"))
    client.sign_in = AsyncMock()
    m.client = client
    yield m
    STATUS["account"].set("disabled")


async def test_login_with_2fa(manager):
    await manager.send_code("+7 999 123-45-67")
    manager.client.send_code_request.assert_awaited_with("+79991234567")
    assert manager.status.status == "awaiting_code"

    manager.client.sign_in.side_effect = errors.SessionPasswordNeededError(request=None)
    await manager.submit_code("12 345")
    manager.client.sign_in.assert_awaited_with("+79991234567", "12345", phone_code_hash="HASH")
    assert manager.status.status == "awaiting_password"

    manager.client.sign_in.side_effect = errors.PasswordHashInvalidError(request=None)
    with pytest.raises(LoginError, match="Неверный пароль"):
        await manager.submit_password("wrong")

    manager.client.sign_in.side_effect = None
    await manager.submit_password("right")
    manager.client.sign_in.assert_awaited_with(password="right")
    assert manager._authorized.is_set()


async def test_login_errors(manager):
    with pytest.raises(LoginError, match="международном"):
        await manager.send_code("123")
    with pytest.raises(LoginError, match="Сначала запросите код"):
        await manager.submit_code("11111")
    await manager.send_code("+79991234567")
    manager.client.sign_in.side_effect = errors.PhoneCodeInvalidError(request=None)
    with pytest.raises(LoginError, match="Неверный код"):
        await manager.submit_code("00000")


async def test_connection_page_disabled_without_credentials(auth_client):
    r = await auth_client.get("/telegram")
    assert r.status_code == 200
    assert "my.telegram.org" in r.text and "Личный аккаунт" in r.text


async def test_connection_page_login_routes(auth_client, app, manager):
    app.state.account = manager
    r = await auth_client.post("/telegram/phone", data={"phone": "+79991234567"})
    assert r.status_code == 303
    page = await auth_client.get("/telegram")
    assert "Код из Telegram" in page.text

    manager.client.sign_in.side_effect = errors.PhoneCodeExpiredError(request=None)
    r = await auth_client.post("/telegram/code", data={"code": "1"})
    assert r.status_code == 400 and "Код истёк" in r.text


async def test_settings_toggle(auth_client, session):
    await auth_client.post("/telegram/settings", data={"ignore_contacts": "1"})
    assert await svc.get_setting(session, IGNORE_CONTACTS_KEY) == "1"
    await auth_client.post("/telegram/settings", data={})
    session.expunge_all()
    assert await svc.get_setting(session, IGNORE_CONTACTS_KEY) == "0"


# --- устойчивость супервизора


async def _wait_for(predicate, timeout=3.0):
    import asyncio

    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_supervisor_survives_telegram_outage(settings, monkeypatch):
    monkeypatch.setattr(settings, "tg_api_id", 1)
    monkeypatch.setattr(settings, "tg_api_hash", "h")
    m = AccountManager(settings, SessionLocal)
    client = MagicMock()
    client.is_connected.return_value = False
    client.connect = AsyncMock(side_effect=ConnectionError("network is unreachable"))
    client.disconnect = AsyncMock()
    monkeypatch.setattr(m, "_make_client", lambda: client)

    m.start()
    try:
        await _wait_for(lambda: m.status.status == "reconnecting")
        assert "Нет соединения с Telegram" in m.status.detail
        assert m._task and not m._task.done()  # продолжает попытки, а не падает
    finally:
        await m.stop()


async def test_revoked_session_requires_new_login(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "tg_api_id", 1)
    monkeypatch.setattr(settings, "tg_api_hash", "h")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    m = AccountManager(settings, SessionLocal)
    m.session_path.parent.mkdir(parents=True)
    session_file = m.session_path.with_name("account.session")
    session_file.write_text("x")

    revoked = MagicMock()
    revoked.is_connected.return_value = True
    revoked.is_user_authorized = AsyncMock(return_value=True)
    revoked.get_me = AsyncMock(return_value=SimpleNamespace(first_name="Me", last_name=None, username="me"))
    revoked.run_until_disconnected = AsyncMock(side_effect=errors.AuthKeyUnregisteredError(request=None))
    revoked.disconnect = AsyncMock()
    fresh = MagicMock()
    fresh.is_connected.return_value = True
    fresh.is_user_authorized = AsyncMock(return_value=False)
    fresh.disconnect = AsyncMock()
    clients = iter([revoked, fresh])
    monkeypatch.setattr(m, "_make_client", lambda: next(clients))

    m.start()
    try:
        await _wait_for(lambda: m.client is fresh and m.status.status == "need_login")
        assert "Сессия завершена" in m.status.detail
        assert not session_file.exists()
    finally:
        await m.stop()

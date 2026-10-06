from sqlalchemy import func, select

from app.bot import handlers as h
from app.models import CLOSED_STATUSES, Lead, LeadSource, LeadStatus, MessageDirection
from app.db import SessionLocal
from app.services import leads as svc
from tests.fakes import FakeCallback, FakeMessage, fsm_context, tg_user


async def run_dialog(user=None, *, name="Пётр", contact_text="@petr", phone=None, request="Нужна реклама в TG"):
    """Проходит весь диалог, вызывая обработчики напрямую; возвращает callback подтверждения."""
    user = user or tg_user()
    state = fsm_context(user.id)
    await h.cmd_start(FakeMessage("/start", from_user=user), state)
    assert await state.get_state() == h.LeadForm.name.state

    await h.step_name(FakeMessage(name, from_user=user), state)
    assert await state.get_state() == h.LeadForm.contact.state

    if phone:
        from types import SimpleNamespace

        await h.step_contact_shared(
            FakeMessage(None, from_user=user, contact=SimpleNamespace(phone_number=phone, user_id=user.id)), state
        )
    else:
        await h.step_contact_text(FakeMessage(contact_text, from_user=user), state)
    assert await state.get_state() == h.LeadForm.request.state

    msg = FakeMessage(request, from_user=user)
    await h.step_request(msg, state)
    assert await state.get_state() == h.LeadForm.confirm.state
    assert "Проверьте заявку" in msg.last_answer() and request in msg.last_answer()

    cb = FakeCallback("lead:confirm", from_user=user)
    await h.cb_confirm(cb, state, session_factory=SessionLocal)
    assert await state.get_state() is None
    return cb


async def bot_leads(session, user_id):
    stmt = select(Lead).where(Lead.source == LeadSource.bot, Lead.telegram_user_id == user_id).order_by(Lead.id)
    return list((await session.execute(stmt)).scalars())


async def test_bot_dialog_creates_lead(session):
    cb = await run_dialog(phone="79991112233")
    cb.message.answer.assert_awaited_with(h.TXT_ACCEPTED)

    [lead] = await bot_leads(session, 1001)
    assert (lead.name, lead.contact, lead.request) == ("Пётр", "+79991112233", "Нужна реклама в TG")
    assert lead.status == LeadStatus.new

    lead = await svc.get_lead(session, lead.id, with_messages=True)
    inbound, outbound = lead.messages
    assert inbound.direction == MessageDirection.inbound
    assert "Имя: Пётр" in inbound.text and "Контакт: +79991112233" in inbound.text and "@petr" in inbound.text
    assert outbound.direction == MessageDirection.outbound and outbound.text == h.TXT_ACCEPTED


async def test_contact_can_be_typed_and_validation(session):
    user = tg_user(2002)
    state = fsm_context(user.id)
    await h.cmd_start(FakeMessage("/start", from_user=user), state)
    bad = FakeMessage("   ", from_user=user)
    await h.step_name(bad, state)
    assert await state.get_state() == h.LeadForm.name.state  # бот всё ещё ждёт имя

    await run_dialog(user, contact_text="@petr_manual")
    [lead] = await bot_leads(session, 2002)
    assert lead.contact == "@petr_manual"


async def test_cancel_clears_state(session):
    state = fsm_context()
    await h.cmd_start(FakeMessage("/start"), state)
    await h.step_name(FakeMessage("Пётр"), state)
    msg = FakeMessage("/cancel")
    await h.cmd_cancel(msg, state)
    assert await state.get_state() is None and await state.get_data() == {}
    assert msg.last_answer() == h.TXT_CANCELLED
    assert await bot_leads(session, 1001) == []


async def test_repeat_application_is_deduplicated(session):
    await run_dialog(request="Первая заявка")
    await run_dialog(request="Вторая заявка")
    [lead] = await bot_leads(session, 1001)
    lead = await svc.get_lead(session, lead.id, with_messages=True)
    assert lead.request == "Первая заявка"
    texts = [m.text for m in lead.messages if m.direction == MessageDirection.inbound]
    assert len(texts) == 2 and "Вторая заявка" in texts[1]


async def test_repeat_after_done_creates_new_lead_by_default(session):
    await run_dialog(request="Первая")
    [lead] = await bot_leads(session, 1001)
    lead.status = LeadStatus.done
    await session.commit()

    await run_dialog(request="Вторая")
    session.expunge_all()
    first, second = await bot_leads(session, 1001)
    assert first.status == LeadStatus.done and second.status == LeadStatus.new
    assert second.request == "Вторая"


async def test_repeat_after_done_reopens_with_append_policy(session, settings, monkeypatch):
    monkeypatch.setattr(settings, "reopen_policy", "append")
    await run_dialog(request="Первая")
    [lead] = await bot_leads(session, 1001)
    lead.status = LeadStatus.rejected
    await session.commit()

    await run_dialog(request="Вторая")
    session.expunge_all()
    [lead] = await bot_leads(session, 1001)
    assert lead.status not in CLOSED_STATUSES


async def test_double_confirm_does_not_duplicate(session):
    user = tg_user(3003)
    await run_dialog(user)
    # повторный клик по старой кнопке «Отправить»: состояние уже сброшено,
    # и dispatcher отправит его в обработчик устаревших кнопок
    cb = FakeCallback("lead:confirm", from_user=user)
    await h.cb_stale(cb)
    assert cb.answer.await_args.kwargs.get("show_alert") is True
    count = (await session.execute(select(func.count(Lead.id)))).scalar_one()
    assert count == 1


async def test_free_text_goes_to_existing_lead(session):
    user = tg_user(4004)
    msg = FakeMessage("Есть вопрос", from_user=user)
    await h.free_text(msg, session_factory=SessionLocal)
    assert "/start" in msg.last_answer()
    assert await bot_leads(session, 4004) == []

    await run_dialog(user)
    await h.free_text(FakeMessage("И ещё вопрос", from_user=user), session_factory=SessionLocal)
    [lead] = await bot_leads(session, 4004)
    lead = await svc.get_lead(session, lead.id, with_messages=True)
    assert lead.messages[-1].text == "И ещё вопрос"


async def test_bot_lead_visible_in_crm_list(auth_client):
    await run_dialog(tg_user(5005, first_name="Мария"), name="Мария")
    r = await auth_client.get("/leads?source=bot", headers={"HX-Request": "true", "HX-Target": "leads-table"})
    assert "Мария" in r.text and "Бот" in r.text

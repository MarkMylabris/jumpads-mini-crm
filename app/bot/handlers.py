"""Диалог бота: имя -> контакт -> запрос -> подтверждение -> лид в CRM.

Фабрику сессий БД обработчики получают из dispatcher (``session_factory``),
поэтому в тестах их можно вызывать напрямую с фейковыми сообщениями.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import LeadSource
from app.services import leads as svc

log = logging.getLogger("crm.bot")

MAX_LEN = 255
MAX_REQUEST_LEN = 4000

TXT_ASK_NAME = "Здравствуйте! Оставьте заявку — это займёт минуту.\n\nКак вас зовут?"
TXT_ASK_CONTACT = (
    "Как с вами связаться? Нажмите «Поделиться номером» или напишите телефон, @username или email."
)
TXT_ASK_REQUEST = "Опишите коротко, что вам нужно."
TXT_ACCEPTED = "Заявка принята! Мы свяжемся с вами в ближайшее время."
TXT_CANCELLED = "Заявка отменена. Чтобы начать заново, отправьте /start."
TXT_NEED_TEXT = "Пожалуйста, отправьте ответ текстом (или /cancel для отмены)."


class LeadForm(StatesGroup):
    name = State()
    contact = State()
    request = State()
    confirm = State()


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Телефон, @username или email",
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="lead:confirm")],
            [
                InlineKeyboardButton(text="✏️ Заново", callback_data="lead:restart"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data="lead:cancel"),
            ],
        ]
    )


def format_summary(data: dict) -> str:
    return f"Имя: {data['name']}\nКонтакт: {data['contact']}\nЗапрос: {data['request']}"


def profile_name(user: User) -> str:
    return " ".join(p for p in (user.first_name, user.last_name) if p) or (user.username or "")


# --- команды


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(LeadForm.name)
    await message.answer(TXT_ASK_NAME, reply_markup=ReplyKeyboardRemove())


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(TXT_CANCELLED, reply_markup=ReplyKeyboardRemove())


# --- шаги диалога


async def step_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or name.startswith("/") or len(name) > MAX_LEN:
        await message.answer("Введите, пожалуйста, имя (до 255 символов).")
        return
    await state.update_data(name=name)
    await state.set_state(LeadForm.contact)
    await message.answer(TXT_ASK_CONTACT, reply_markup=contact_keyboard())


async def step_contact_shared(message: Message, state: FSMContext) -> None:
    phone = message.contact.phone_number
    await _save_contact(message, state, phone if phone.startswith("+") else f"+{phone}")


async def step_contact_text(message: Message, state: FSMContext) -> None:
    contact = message.text.strip()
    if not contact or contact.startswith("/") or len(contact) > MAX_LEN:
        await message.answer("Укажите телефон, @username или email.")
        return
    await _save_contact(message, state, contact)


async def _save_contact(message: Message, state: FSMContext, contact: str) -> None:
    await state.update_data(contact=contact)
    await state.set_state(LeadForm.request)
    await message.answer(TXT_ASK_REQUEST, reply_markup=ReplyKeyboardRemove())


async def step_request(message: Message, state: FSMContext) -> None:
    request = message.text.strip()
    if not request or request.startswith("/"):
        await message.answer(TXT_ASK_REQUEST)
        return
    data = await state.update_data(request=request[:MAX_REQUEST_LEN])
    await state.set_state(LeadForm.confirm)
    await message.answer(
        f"Проверьте заявку:\n\n{format_summary(data)}\n\nВсё верно?", reply_markup=confirm_keyboard()
    )


async def step_wrong_content(message: Message) -> None:
    await message.answer(TXT_NEED_TEXT)


# --- подтверждение


async def cb_confirm(callback: CallbackQuery, state: FSMContext, session_factory: async_sessionmaker) -> None:
    data = await state.get_data()
    await state.clear()  # сначала сбрасываем состояние: двойной клик не отправит заявку дважды
    user = callback.from_user
    summary = format_summary(data)
    if user.username:
        summary += f"\nTelegram: @{user.username}"

    async with session_factory() as session:
        result = await svc.ingest_telegram_message(
            session,
            source=LeadSource.bot,
            tg_user_id=user.id,
            name=data["name"],
            contact=data["contact"],
            request=data["request"],
            text=summary,
        )
        await svc.record_outgoing(session, source=LeadSource.bot, tg_user_id=user.id, text=TXT_ACCEPTED)

    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(TXT_ACCEPTED)
    log.info("Bot application from tg_user=%s -> lead #%s (created=%s)", user.id, result.lead.id, result.created)


async def cb_restart(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(LeadForm.name)
    await callback.answer()
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Хорошо, начнём заново. Как вас зовут?")


async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(TXT_CANCELLED)


async def cb_stale(callback: CallbackQuery) -> None:
    await callback.answer("Эта заявка уже обработана. Отправьте /start, чтобы оставить новую.", show_alert=True)


# --- сообщения вне диалога


async def free_text(message: Message, session_factory: async_sessionmaker) -> None:
    """Текст вне диалога дописывается в историю лида, если он уже есть."""
    user = message.from_user
    async with session_factory() as session:
        lead = await svc.find_target_lead(session, LeadSource.bot, user.id)
        if lead is None:
            await message.answer("Чтобы оставить заявку, отправьте /start.")
            return
        await svc.ingest_telegram_message(
            session,
            source=LeadSource.bot,
            tg_user_id=user.id,
            name=profile_name(user),
            contact=lead.contact,
            text=message.text,
        )
    await message.answer("Сообщение добавлено к вашей заявке, менеджер его увидит.")



def create_router() -> Router:
    """Новый роутер на каждый Dispatcher: в aiogram роутер подключается только один раз."""
    router = Router(name="lead_form")
    router.message.filter(F.chat.type == "private")
    m, c = router.message, router.callback_query

    m.register(cmd_start, CommandStart())
    m.register(cmd_cancel, Command("cancel"))
    m.register(step_name, LeadForm.name, F.text)
    m.register(step_contact_shared, LeadForm.contact, F.contact)
    m.register(step_contact_text, LeadForm.contact, F.text)
    m.register(step_request, LeadForm.request, F.text)
    m.register(step_wrong_content, StateFilter(LeadForm))
    m.register(free_text, StateFilter(None), F.text)

    c.register(cb_confirm, LeadForm.confirm, F.data == "lead:confirm")
    c.register(cb_restart, LeadForm.confirm, F.data == "lead:restart")
    c.register(cb_cancel, F.data == "lead:cancel")
    c.register(cb_stale, F.data.startswith("lead:"))
    return router

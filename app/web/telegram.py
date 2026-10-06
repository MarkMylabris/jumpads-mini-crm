"""Страница «Подключение Telegram»: статус интеграций, вход в аккаунт, настройки."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.integrations import STATUS
from app.services import leads as svc
from app.tg_account.handlers import IGNORE_CONTACTS_KEY, ignore_contacts_enabled
from app.tg_account.manager import AccountManager, LoginError
from app.web.auth import require_user
from app.web.templating import templates

router = APIRouter(prefix="/telegram", dependencies=[Depends(require_user)])

STATUS_LABELS = {
    "disabled": ("Не настроено", "#9e9e9e"),
    "starting": ("Запуск…", "#1e88e5"),
    "running": ("Работает", "#43a047"),
    "reconnecting": ("Переподключение", "#f9a825"),
    "need_login": ("Требуется вход", "#f9a825"),
    "awaiting_code": ("Ожидается код", "#1e88e5"),
    "awaiting_password": ("Ожидается пароль 2FA", "#1e88e5"),
    "error": ("Ошибка", "#e53935"),
    "stopped": ("Остановлено", "#9e9e9e"),
}


def _manager(request: Request) -> AccountManager | None:
    return getattr(request.app.state, "account", None)


async def _render(request: Request, *, error: str | None = None, status_code: int = 200):
    from app.db import SessionLocal

    ctx = {
        "bot": STATUS["bot"],
        "account": STATUS["account"],
        "labels": STATUS_LABELS,
        "manager": _manager(request),
        "error": error,
        "ignore_contacts": await ignore_contacts_enabled(SessionLocal),
        "settings": get_settings(),
    }
    tpl = "partials/tg_status.html" if request.headers.get("HX-Target") == "tg-status" else "telegram.html"
    return templates.TemplateResponse(request, tpl, ctx, status_code=status_code)


@router.get("")
async def telegram_page(request: Request):
    return await _render(request)


async def _login_step(request: Request, step) -> RedirectResponse:
    manager = _manager(request)
    if manager is None:
        return await _render(request, error="Интеграция не запущена", status_code=503)
    try:
        await step(manager)
    except LoginError as e:
        return await _render(request, error=str(e), status_code=400)
    return RedirectResponse("/telegram", status_code=303)


@router.post("/phone")
async def submit_phone(request: Request, phone: str = Form("")):
    return await _login_step(request, lambda m: m.send_code(phone))


@router.post("/code")
async def submit_code(request: Request, code: str = Form("")):
    return await _login_step(request, lambda m: m.submit_code(code))


@router.post("/password")
async def submit_password(request: Request, password: str = Form("")):
    return await _login_step(request, lambda m: m.submit_password(password))


@router.post("/cancel")
async def cancel_login(request: Request):
    return await _login_step(request, lambda m: m.cancel_login())


@router.post("/disconnect")
async def disconnect(request: Request):
    return await _login_step(request, lambda m: m.disconnect())


@router.post("/settings")
async def save_settings(ignore_contacts: str = Form(""), session: AsyncSession = Depends(get_session)):
    await svc.set_setting(session, IGNORE_CONTACTS_KEY, "1" if ignore_contacts else "0")
    return RedirectResponse("/telegram", status_code=303)

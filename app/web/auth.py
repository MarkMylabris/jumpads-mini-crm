"""Вход единственного администратора: cookie-сессия и защита от подбора пароля."""

import logging
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.config import get_settings
from app.web.templating import templates

log = logging.getLogger("crm.auth")
router = APIRouter()


class NotAuthenticated(Exception):
    pass


def require_user(request: Request) -> str:
    """Зависимость FastAPI: пускает только залогиненного администратора."""
    user = request.session.get("user")
    if not user:
        raise NotAuthenticated()
    return user


async def not_authenticated_handler(request: Request, _exc: NotAuthenticated) -> Response:
    if request.headers.get("HX-Request"):
        # Сессия истекла во время HTMX-запроса (например, автообновления) — полный редирект.
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


class LoginThrottle:
    """Защита от подбора пароля: много неудачных входов -> HTTP 429.

    Считает ошибки по IP и глобально: IP берётся из заголовков прокси,
    и при прямом обращении к порту его можно подделать.
    """

    GLOBAL_FACTOR = 4

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, window: int) -> deque[float]:
        q, cutoff = self._failures[key], time.monotonic() - window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        s = get_settings()
        return (
            len(self._recent(ip, s.login_lockout_seconds)) >= s.login_max_failures
            or len(self._recent("*", s.login_lockout_seconds)) >= s.login_max_failures * self.GLOBAL_FACTOR
        )

    def fail(self, ip: str) -> None:
        now = time.monotonic()
        self._failures[ip].append(now)
        self._failures["*"].append(now)

    def reset(self) -> None:
        self._failures.clear()


throttle = LoginThrottle()


def client_ip(request: Request) -> str:
    # За туннелем request.client — это контейнер cloudflared, реальный IP в заголовке
    forwarded = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0]
    return forwarded.strip() or (request.client.host if request.client else "?")


def _safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else "/leads"


@router.get("/login")
async def login_page(request: Request, next: str = "/leads"):
    if request.session.get("user"):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": _safe_next(next), "error": None})


@router.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/leads")):
    s = get_settings()
    ip = client_ip(request)
    if throttle.blocked(ip):
        log.warning("Login blocked for %s: too many failed attempts", ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "error": "Слишком много попыток входа, попробуйте через несколько минут",
             "username": username},
            status_code=429,
        )
    ok_user = secrets.compare_digest(username.encode(), s.admin_username.encode())
    ok_pass = secrets.compare_digest(password.encode(), s.admin_password.encode())
    if not (ok_user and ok_pass):
        throttle.fail(ip)
        log.warning("Failed login attempt for %r from %s", username, ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": _safe_next(next), "error": "Неверный логин или пароль", "username": username},
            status_code=401,
        )
    request.session.clear()
    request.session["user"] = username
    log.info("Admin logged in")
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

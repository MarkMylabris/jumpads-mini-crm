"""Точка входа: FastAPI-приложение, бот и Telegram-аккаунт запускаются вместе."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.bot.runner import BotService
from app.config import get_settings
from app.db import SessionLocal
from app.logging_setup import setup_logging
from app.tg_account.manager import AccountManager
from app.web import auth, leads, tags, telegram
from app.web.auth import NotAuthenticated, not_authenticated_handler

log = logging.getLogger("crm")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Mini CRM starting")
    # Интеграции — фоновые задачи под супервизором: их сбои не останавливают веб.
    app.state.bot.start()
    app.state.account.start()
    try:
        yield
    finally:
        await app.state.account.stop()
        await app.state.bot.stop()
        log.info("Mini CRM stopped")


def create_app(*, start_integrations: bool = True) -> FastAPI:
    setup_logging()
    s = get_settings()
    if s.secret_key == "change-me" or s.admin_password == "admin":
        log.warning("SECRET_KEY / ADMIN_PASSWORD use insecure defaults - set them in .env")

    app = FastAPI(title="Mini CRM", lifespan=lifespan if start_integrations else None, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=s.secret_key,
        session_cookie="crm_session",
        max_age=s.session_max_age,
        same_site="lax",
        https_only=s.session_https_only,
    )
    app.add_exception_handler(NotAuthenticated, not_authenticated_handler)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(leads.router)
    app.include_router(tags.router)
    app.include_router(telegram.router)

    app.state.bot = BotService(s, SessionLocal)
    app.state.account = AccountManager(s, SessionLocal)
    if s.bot_mode == "webhook" and s.bot_token:
        app.include_router(app.state.bot.webhook_router())

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()

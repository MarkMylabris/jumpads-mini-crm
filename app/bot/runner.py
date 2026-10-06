"""Запуск бота в процессе веб-приложения (long polling или webhook).

Ошибки бота не роняют CRM: сетевые сбои — повтор с паузой,
неверный токен — остановка с понятной ошибкой в интерфейсе.
"""

import asyncio
import logging
import secrets

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers import create_router
from app.config import Settings
from app.integrations import STATUS

log = logging.getLogger("crm.bot")


def build_dispatcher(session_factory: async_sessionmaker) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), session_factory=session_factory)
    dp.include_router(create_router())
    return dp


class BotService:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker):
        self.settings = settings
        self.status = STATUS["bot"]
        self.bot: Bot | None = None
        self.dp = build_dispatcher(session_factory)
        self._task: asyncio.Task | None = None
        self._webhook_tasks: set[asyncio.Task] = set()
        self._webhook_secret = settings.webhook_secret or secrets.token_urlsafe(32)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.bot_token)

    def start(self) -> None:
        if not self.enabled:
            self.status.set("disabled", "BOT_TOKEN не задан")
            return
        self.bot = Bot(self.settings.bot_token, default=DefaultBotProperties(link_preview_is_disabled=True))
        self._task = asyncio.create_task(self._supervise(), name="bot-supervisor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.bot:
            await self.bot.session.close()
        if self.enabled:
            self.status.set("stopped")

    async def _supervise(self) -> None:
        delay = 2
        while True:
            try:
                self.status.set("starting")
                me = await self.bot.get_me()
                log.info("Bot @%s authorized, mode=%s", me.username, self.settings.bot_mode)
                if self.settings.bot_mode == "webhook":
                    url = self.settings.webhook_base_url.rstrip("/") + self.settings.webhook_path
                    await self.bot.set_webhook(url, secret_token=self._webhook_secret, allowed_updates=self.dp.resolve_used_update_types())
                    self.status.set("running", f"@{me.username}, webhook {url}")
                    return  # updates arrive via the FastAPI route
                await self.bot.delete_webhook(drop_pending_updates=False)
                self.status.set("running", f"@{me.username}, long polling")
                delay = 2
                await self.dp.start_polling(
                    self.bot,
                    handle_signals=False,
                    close_bot_session=False,
                    allowed_updates=self.dp.resolve_used_update_types(),
                )
                return
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                log.error("Bot token rejected by Telegram - bot stopped")
                self.status.set("error", "Telegram отклонил BOT_TOKEN — проверьте токен в .env")
                return
            except Exception as e:  # network errors, Telegram down, etc.
                log.warning("Bot error: %s; reconnecting in %ss", e, delay)
                self.status.set("reconnecting", f"{type(e).__name__}: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    # --- webhook

    def webhook_router(self) -> APIRouter:
        router = APIRouter()

        @router.post(self.settings.webhook_path, include_in_schema=False)
        async def telegram_webhook(
            request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)
        ):
            if self.bot is None or not secrets.compare_digest(
                x_telegram_bot_api_secret_token or "", self._webhook_secret
            ):
                raise HTTPException(403)
            update = await request.json()
            # Отвечаем Telegram сразу, апдейт обрабатываем в фоне
            task = asyncio.create_task(self._feed(update))
            self._webhook_tasks.add(task)
            task.add_done_callback(self._webhook_tasks.discard)
            return {"ok": True}

        return router

    async def _feed(self, update: dict) -> None:
        try:
            method = await self.dp.feed_webhook_update(self.bot, update)
            if method is not None:
                await self.bot(method)
        except Exception:
            log.exception("Failed to process webhook update")

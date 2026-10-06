"""Жизненный цикл личного Telegram-аккаунта (Telethon) внутри веб-процесса.

Почему в одном процессе с вебом: вход идёт в несколько HTTP-запросов
(телефон -> код -> 2FA) и требует одного и того же клиента и phone_code_hash.
Так не нужна очередь между процессами, а SQLite пишет из одного места.
Сбои изолированы в задаче-супервизоре: CRM работает, даже если Telegram недоступен.

Сессия — файл Telethon в DATA_DIR/telethon/. В отличие от StringSession он хранит
состояние обновлений, и с catch_up=True сообщения за время простоя догружаются.
"""

import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker
from telethon import TelegramClient, errors, events, functions

from app.config import Settings
from app.integrations import STATUS
from app.tg_account.handlers import handle_new_message

log = logging.getLogger("crm.tg_account")

WATCHDOG_INTERVAL = 60
SESSION_DEAD_ERRORS = (
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.AuthKeyDuplicatedError,
)


class LoginError(Exception):
    pass


def _describe(exc: Exception) -> str:
    if isinstance(exc, errors.FloodWaitError):
        return f"Слишком много попыток, Telegram просит подождать {exc.seconds} с"
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return f"Нет соединения с Telegram ({type(exc).__name__})"
    return f"{type(exc).__name__}: {exc}"


class AccountManager:
    def __init__(self, settings: Settings, session_factory: async_sessionmaker):
        self.settings = settings
        self.session_factory = session_factory
        self.status = STATUS["account"]
        self.client: TelegramClient | None = None
        self.me_label = ""
        self._task: asyncio.Task | None = None
        self._authorized = asyncio.Event()
        self._lock = asyncio.Lock()  # шаги входа/выхода не должны идти параллельно
        self._phone: str | None = None
        self._phone_code_hash: str | None = None

    # --- свойства

    @property
    def configured(self) -> bool:
        return bool(self.settings.tg_api_id and self.settings.tg_api_hash) and self.settings.tg_account_enabled

    @property
    def session_path(self) -> Path:
        return self.settings.telethon_session_path

    @property
    def pending_phone(self) -> str | None:
        return self._phone

    # --- запуск и супервизор

    def start(self) -> None:
        if not self.configured:
            self.status.set("disabled", "TG_API_ID / TG_API_HASH не заданы в .env")
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._supervise(), name="tg-account-supervisor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self.client:
            await self.client.disconnect()
        if self.configured:
            self.status.set("stopped")

    def _make_client(self) -> TelegramClient:
        client = TelegramClient(
            str(self.session_path),
            self.settings.tg_api_id,
            self.settings.tg_api_hash,
            device_model="Mini CRM",
            system_version="server",
            app_version="1.0",
            catch_up=True,
            auto_reconnect=True,
            connection_retries=5,
            retry_delay=2,
        )
        client.add_event_handler(self._on_new_message, events.NewMessage())
        return client

    async def _on_new_message(self, event) -> None:
        try:
            outcome = await handle_new_message(event, self.session_factory)
            log.debug("Telegram message from %s: %s", event.chat_id, outcome)
        except Exception:
            log.exception("Failed to store Telegram message from chat %s", getattr(event, "chat_id", "?"))

    async def _supervise(self) -> None:
        delay = 2
        while True:
            try:
                if self.client is None:
                    self.client = self._make_client()
                if not self.client.is_connected():
                    self.status.set("starting" if delay == 2 else "reconnecting", "Подключение к Telegram…")
                    await self.client.connect()
                if not await self.client.is_user_authorized():
                    self._authorized.clear()
                    if self.status.status not in ("awaiting_code", "awaiting_password", "need_login"):
                        self.status.set("need_login", "Аккаунт не подключён")
                    await self._authorized.wait()
                    continue

                me = await self.client.get_me()
                self.me_label = " ".join(p for p in (me.first_name, me.last_name) if p) + (
                    f" (@{me.username})" if me.username else ""
                )
                self.status.set("running", f"Подключён: {self.me_label}")
                log.info("Telegram account connected: %s", self.me_label)
                delay = 2
                await self._run_until_disconnected()
                raise ConnectionError("соединение с Telegram потеряно")
            except asyncio.CancelledError:
                raise
            except SESSION_DEAD_ERRORS as e:
                log.warning("Telegram session is no longer valid (%s) - login required", type(e).__name__)
                await self._drop_session()
                self.status.set("need_login", f"Сессия завершена Telegram ({type(e).__name__}) — войдите заново")
            except Exception as e:
                log.warning("Telegram account error: %s; retry in %ss", _describe(e), delay)
                self.status.set("reconnecting", _describe(e))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def _run_until_disconnected(self) -> None:
        """Ждём отключения; сторож параллельно проверяет, жива ли сессия."""
        watchdog = asyncio.create_task(self._watchdog())
        disconnected = asyncio.ensure_future(self.client.run_until_disconnected())
        try:
            done, _ = await asyncio.wait({watchdog, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raise errors from either side
        finally:
            for task in (watchdog, disconnected):
                task.cancel()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if not self.client.is_connected():
                self.status.set("reconnecting", "Переподключение к Telegram…")
                log.info("Telegram account: connection lost, Telethon is reconnecting")
                continue
            try:
                await self.client(functions.updates.GetStateRequest())
            except SESSION_DEAD_ERRORS:
                raise
            except Exception as e:
                self.status.set("reconnecting", _describe(e))
                log.warning("Telegram account health check failed: %s", _describe(e))
                continue
            if self.status.status != "running":
                log.info("Telegram account: reconnected")
                self.status.set("running", f"Подключён: {self.me_label}")

    async def _drop_session(self) -> None:
        client, self.client = self.client, None
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        for suffix in (".session", ".session-journal"):
            self.session_path.with_name(self.session_path.name + suffix).unlink(missing_ok=True)
        self._phone = self._phone_code_hash = None
        self.me_label = ""

    # --- вход (вызывается из веба)

    async def _connected_client(self) -> TelegramClient:
        if not self.configured:
            raise LoginError("TG_API_ID / TG_API_HASH не заданы в .env")
        if self.client is None:
            self.client = self._make_client()
        if not self.client.is_connected():
            try:
                await self.client.connect()
            except Exception as e:
                raise LoginError(_describe(e)) from e
        return self.client

    async def send_code(self, phone: str) -> None:
        phone = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
        if len(phone.lstrip("+")) < 7:
            raise LoginError("Введите номер в международном формате, например +79991234567")
        async with self._lock:
            client = await self._connected_client()
            try:
                sent = await client.send_code_request(phone)
            except errors.PhoneNumberInvalidError:
                raise LoginError("Неверный номер телефона") from None
            except errors.ApiIdInvalidError:
                raise LoginError("Неверные TG_API_ID / TG_API_HASH") from None
            except errors.PhoneNumberBannedError:
                raise LoginError("Этот номер заблокирован в Telegram") from None
            except Exception as e:
                log.warning("send_code failed: %s", _describe(e))
                raise LoginError(_describe(e)) from e
            self._phone, self._phone_code_hash = phone, sent.phone_code_hash
            self.status.set("awaiting_code", f"Код отправлен на {phone}")
            log.info("Telegram login: code sent to %s", phone)

    async def submit_code(self, code: str) -> None:
        code = "".join(ch for ch in code if ch.isdigit())
        if not self._phone or not self._phone_code_hash:
            raise LoginError("Сначала запросите код")
        if not code:
            raise LoginError("Введите код")
        async with self._lock:
            client = await self._connected_client()
            try:
                await client.sign_in(self._phone, code, phone_code_hash=self._phone_code_hash)
            except errors.SessionPasswordNeededError:
                self.status.set("awaiting_password", "Включена двухэтапная аутентификация — введите пароль")
                return
            except errors.PhoneCodeInvalidError:
                raise LoginError("Неверный код") from None
            except errors.PhoneCodeExpiredError:
                self._phone_code_hash = None
                self.status.set("need_login", "Код истёк")
                raise LoginError("Код истёк — запросите новый") from None
            except Exception as e:
                raise LoginError(_describe(e)) from e
            self._login_done()

    async def submit_password(self, password: str) -> None:
        if self.status.status != "awaiting_password":
            raise LoginError("Пароль сейчас не требуется")
        if not password:
            raise LoginError("Введите пароль")
        async with self._lock:
            client = await self._connected_client()
            try:
                await client.sign_in(password=password)
            except errors.PasswordHashInvalidError:
                raise LoginError("Неверный пароль") from None
            except Exception as e:
                raise LoginError(_describe(e)) from e
            self._login_done()

    def _login_done(self) -> None:
        log.info("Telegram login successful for %s", self._phone)
        self._phone = self._phone_code_hash = None
        self.status.set("starting", "Вход выполнен, запуск…")
        self._authorized.set()

    async def cancel_login(self) -> None:
        self._phone = self._phone_code_hash = None
        if self.status.status in ("awaiting_code", "awaiting_password"):
            self.status.set("need_login", "Аккаунт не подключён")

    async def disconnect(self) -> None:
        """Кнопка «Отключить»: выйти на стороне Telegram и удалить файл сессии."""
        async with self._lock:
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None
            if self.client:
                try:
                    if not self.client.is_connected():
                        await self.client.connect()
                    await self.client.log_out()
                except Exception as e:
                    log.warning("Telegram log_out failed (%s), removing local session anyway", _describe(e))
            await self._drop_session()
            log.info("Telegram account disconnected by admin")
            self.status.set("need_login", "Аккаунт отключён")
            if self.configured:
                self._task = asyncio.create_task(self._supervise(), name="tg-account-supervisor")

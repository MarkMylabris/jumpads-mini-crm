"""Подключение к БД (SQLite или PostgreSQL) и фабрика асинхронных сессий."""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


def _py_lower(value):
    return value.lower() if isinstance(value, str) else value


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> AsyncEngine:
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
            # Встроенный lower() в SQLite понимает только ASCII — подменяем,
            # чтобы поиск без учёта регистра работал и для кириллицы.
            dbapi_conn.run_async(
                lambda conn: conn.create_function("lower", 1, _py_lower, deterministic=True)
            )

    return engine


settings = get_settings()
if settings.database_url.startswith("sqlite"):
    settings.data_dir.mkdir(parents=True, exist_ok=True)

engine = make_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session

import os
import tempfile
from pathlib import Path

# Изолированное окружение — до импорта модулей приложения.
_tmp = Path(tempfile.mkdtemp(prefix="crm-tests-"))
os.environ.update(
    CRM_ENV_FILE="",  # не читать .env разработчика (там настоящие токены)
    # TEST_DATABASE_URL — прогнать тесты на PostgreSQL
    DATABASE_URL=os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{_tmp / 'test.db'}",
    DATA_DIR=str(_tmp),
    SECRET_KEY="test-secret",
    ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="secret",
    BOT_TOKEN="",
    TG_API_ID="0",
    TG_API_HASH="",
    REOPEN_POLICY="new_lead",
)

import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Base  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """Один раз применяет настоящие миграции Alembic — заодно проверяет их."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
async def clean_db():
    from app.web.auth import throttle

    throttle.reset()
    yield
    async with SessionLocal() as s:
        for table in reversed(Base.metadata.sorted_tables):
            await s.execute(delete(table))
        await s.commit()


@pytest.fixture
async def session():
    async with SessionLocal() as s:
        yield s


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def app():
    from app.main import create_app

    return create_app(start_integrations=False)


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth_client(client):
    r = await client.post("/login", data={"username": "admin", "password": "secret"})
    assert r.status_code == 303
    return client

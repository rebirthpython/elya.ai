"""Создание async-движка SQLAlchemy и фабрики сессий."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from database.models import Base


def create_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"

    if is_sqlite and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    # hide_parameters: тексты сообщений не попадут в логи при ошибках SQL.
    engine = create_async_engine(database_url, hide_parameters=True)

    if is_sqlite:

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт отсутствующие таблицы (существующие не изменяются)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

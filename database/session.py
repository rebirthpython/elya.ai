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


def describe_database(database_url: str) -> str:
    """Где лежит база — для лога при запуске (без паролей из URL)."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        return f"SQLite {Path(url.database).resolve()}"
    return url.render_as_string(hide_password=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


# Столбцы, добавленные после первых версий: в старой базе их нет, create_all их не добавит.
_ADDED_COLUMNS = {
    "messages": {"chat_id": "INTEGER"},
    "user_documents": {"chat_id": "INTEGER", "project_id": "INTEGER"},
}


def _add_missing_columns(connection) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table, columns in _ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, kind in columns.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {kind}"))
                connection.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{name} ON {table} ({name})"))


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт отсутствующие таблицы и добавляет новые столбцы в старые (данные не меняются)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)

"""Режим общения: обычный разговор или режим кода.

Хранится в БД на пользователя, поэтому включённый командой /code режим действует
и в чате с ботом, и в Mini App.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from database.models import UserMode

CHAT = "chat"
CODE = "code"
MODES = (CHAT, CODE)
DEFAULT_MODE = CHAT


class ModeStore:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def get(self, user_id: int) -> str:
        async with self._session_factory() as session:
            row = await session.get(UserMode, user_id)
        # Значение, которого больше нет в списке, заменяется на стандартное.
        return row.mode if row is not None and row.mode in MODES else DEFAULT_MODE

    async def set(self, user_id: int, mode: str) -> str:
        """Запоминает режим. ValueError — неизвестное значение."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        async with self._session_factory.begin() as session:
            row = await session.get(UserMode, user_id)
            if row is None:
                session.add(UserMode(user_id=user_id, mode=mode))
            else:
                row.mode = mode
        return mode

"""Доступ к сохранённой истории диалога в БД."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Message, User


class MessageRepository:
    def __init__(self, session_factory: async_sessionmaker, *, store_limit: int) -> None:
        self._session_factory = session_factory
        self._store_limit = store_limit

    @staticmethod
    async def _ensure_user(session: AsyncSession, user_id: int) -> None:
        if await session.get(User, user_id) is None:
            session.add(User(id=user_id))
            await session.flush()

    async def add_exchange(self, user_id: int, user_text: str, assistant_text: str) -> None:
        """Сохраняет пару «вопрос — ответ» и удаляет историю сверх лимита хранения."""
        async with self._session_factory.begin() as session:
            await self._ensure_user(session, user_id)
            session.add_all(
                [
                    Message(user_id=user_id, role="user", content=user_text),
                    Message(user_id=user_id, role="assistant", content=assistant_text),
                ]
            )
            await session.flush()

            threshold = await session.scalar(
                select(Message.id)
                .where(Message.user_id == user_id)
                .order_by(Message.id.desc())
                .offset(self._store_limit)
                .limit(1)
            )
            if threshold is not None:
                await session.execute(
                    delete(Message).where(Message.user_id == user_id, Message.id <= threshold)
                )

    async def get_recent(self, user_id: int, limit: int) -> list[Message]:
        """Последние limit сообщений в хронологическом порядке."""
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Message)
                .where(Message.user_id == user_id)
                .order_by(Message.id.desc())
                .limit(limit)
            )
            return list(reversed(rows.all()))

    async def count(self, user_id: int) -> int:
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(Message).where(Message.user_id == user_id)
            )
            return int(total or 0)

    async def clear(self, user_id: int) -> int:
        """Удаляет всю историю пользователя, возвращает число удалённых сообщений."""
        async with self._session_factory.begin() as session:
            result = await session.execute(delete(Message).where(Message.user_id == user_id))
            return int(result.rowcount or 0)

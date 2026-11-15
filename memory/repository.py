"""Доступ к сохранённой истории диалога в БД."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Message, User
from memory.chats import CHAT_BREAK  # noqa: F401 — отметка «Новый чат» из старой истории


class MessageRepository:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    @property
    def session_factory(self) -> async_sessionmaker:
        return self._session_factory

    @staticmethod
    async def _ensure_user(session: AsyncSession, user_id: int) -> None:
        if await session.get(User, user_id) is None:
            session.add(User(id=user_id))
            await session.flush()

    async def add_exchange(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        chat_id: int | None = None,
        attachments: str | None = None,
    ) -> None:
        """Сохраняет пару «вопрос — ответ» в чат; attachments — файлы к ответу (JSON)."""
        async with self._session_factory.begin() as session:
            await self._ensure_user(session, user_id)
            session.add_all(
                [
                    Message(user_id=user_id, role="user", content=user_text, chat_id=chat_id),
                    Message(
                        user_id=user_id, role="assistant", content=assistant_text, chat_id=chat_id,
                        attachments=attachments,
                    ),
                ]
            )

    async def get_recent(self, user_id: int, limit: int, chat_id: int | None = None) -> list[Message]:
        """Последние limit сообщений чата (без chat_id — всех) в хронологическом порядке."""
        query = select(Message).where(Message.user_id == user_id)
        if chat_id is not None:
            query = query.where(Message.chat_id == chat_id)
        async with self._session_factory() as session:
            rows = await session.scalars(query.order_by(Message.id.desc()).limit(limit))
            return list(reversed(rows.all()))

    async def count(self, user_id: int, chat_id: int | None = None) -> int:
        query = select(func.count()).select_from(Message).where(
            Message.user_id == user_id, Message.role != CHAT_BREAK
        )
        if chat_id is not None:
            query = query.where(Message.chat_id == chat_id)
        async with self._session_factory() as session:
            return int(await session.scalar(query) or 0)

    async def clear(self, user_id: int) -> int:
        """Удаляет всю историю пользователя, возвращает число удалённых сообщений."""
        async with self._session_factory.begin() as session:
            result = await session.execute(
                delete(Message).where(Message.user_id == user_id, Message.role != CHAT_BREAK)
            )
            await session.execute(delete(Message).where(Message.user_id == user_id))
            return int(result.rowcount or 0)

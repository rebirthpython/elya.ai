"""Менеджер памяти. Этап 1: краткосрочная память (история диалога в БД)."""

from __future__ import annotations

from ai.providers.base import ChatMessage
from database.models import Message
from memory.repository import MessageRepository


class MemoryManager:
    def __init__(self, repository: MessageRepository, *, history_limit: int) -> None:
        self._repository = repository
        self._history_limit = history_limit

    async def get_history(self, user_id: int) -> list[ChatMessage]:
        rows = await self._repository.get_recent(user_id, self._history_limit)
        return [
            ChatMessage(role="assistant" if row.role == "assistant" else "user", content=row.content)
            for row in rows
        ]

    async def get_timeline(self, user_id: int, limit: int) -> list[Message]:
        """Последние сообщения с временем отправки — для показа в интерфейсе."""
        return await self._repository.get_recent(user_id, limit)

    async def save_exchange(self, user_id: int, user_text: str, assistant_text: str) -> None:
        await self._repository.add_exchange(user_id, user_text, assistant_text)

    async def history_size(self, user_id: int) -> int:
        return await self._repository.count(user_id)

    async def clear_history(self, user_id: int) -> int:
        return await self._repository.clear(user_id)

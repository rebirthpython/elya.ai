"""Менеджер памяти. Этап 1: краткосрочная память (история диалога в БД)."""

from __future__ import annotations

from ai.providers.base import ChatMessage
from database.models import Message
from memory.repository import CHAT_BREAK, MessageRepository


class MemoryManager:
    def __init__(self, repository: MessageRepository, *, history_limit: int) -> None:
        self._repository = repository
        self._history_limit = history_limit

    async def get_history(self, user_id: int) -> list[ChatMessage]:
        rows = await self._repository.get_recent(user_id, self._history_limit)
        # Эля помнит только текущий чат: всё до последней отметки «Новый чат» остаётся в ленте.
        breaks = [index for index, row in enumerate(rows) if row.role == CHAT_BREAK]
        if breaks:
            rows = rows[breaks[-1] + 1 :]
        return [
            ChatMessage(role="assistant" if row.role == "assistant" else "user", content=row.content)
            for row in rows
        ]

    async def get_timeline(self, user_id: int, limit: int) -> list[Message]:
        """Последние сообщения с временем отправки и отметки «Новый чат» — для показа в интерфейсе."""
        return await self._repository.get_recent(user_id, limit)

    async def save_exchange(self, user_id: int, user_text: str, assistant_text: str) -> None:
        await self._repository.add_exchange(user_id, user_text, assistant_text)

    async def start_new_chat(self, user_id: int) -> Message | None:
        """Новый чат: прошлые сообщения не удаляются, Эля просто перестаёт их учитывать."""
        return await self._repository.add_break(user_id)

    async def history_size(self, user_id: int) -> int:
        return await self._repository.count(user_id)

    async def clear_history(self, user_id: int) -> int:
        return await self._repository.clear(user_id)

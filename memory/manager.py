"""Менеджер памяти: переписка открытого чата (краткосрочная память).

Каждый чат — отдельная переписка (memory.chats). Методы без chat_id работают с открытым
сейчас чатом; ChatService передаёт chat_id явно, чтобы ответ сохранился в тот чат, где
был задан вопрос, даже если за это время открыли другой.
"""

from __future__ import annotations

from ai.providers.base import ChatMessage
from database.models import Message
from memory.chats import CHAT_BREAK, ChatInfo, ChatStore
from memory.repository import MessageRepository


class MemoryManager:
    def __init__(
        self, repository: MessageRepository, *, history_limit: int, chats: ChatStore | None = None
    ) -> None:
        self._repository = repository
        self._history_limit = history_limit
        self.chats = chats or ChatStore(repository.session_factory)

    async def _chat_id(self, user_id: int, chat_id: int | None) -> int:
        return chat_id if chat_id is not None else (await self.chats.active(user_id)).id

    async def get_history(self, user_id: int, chat_id: int | None = None) -> list[ChatMessage]:
        chat_id = await self._chat_id(user_id, chat_id)
        rows = await self._repository.get_recent(user_id, self._history_limit, chat_id=chat_id)
        return [
            ChatMessage(role="assistant" if row.role == "assistant" else "user", content=row.content)
            for row in rows
            if row.role != CHAT_BREAK
        ]

    async def get_timeline(self, user_id: int, limit: int, chat_id: int | None = None) -> list[Message]:
        """Последние сообщения открытого чата с временем отправки — для показа в интерфейсе."""
        chat_id = await self._chat_id(user_id, chat_id)
        return await self._repository.get_recent(user_id, limit, chat_id=chat_id)

    async def save_exchange(
        self, user_id: int, user_text: str, assistant_text: str, chat_id: int | None = None
    ) -> None:
        chat_id = await self._chat_id(user_id, chat_id)
        await self._repository.add_exchange(user_id, user_text, assistant_text, chat_id=chat_id)
        await self.chats.touch(user_id, chat_id, user_text)

    async def start_new_chat(self, user_id: int, **kwargs) -> tuple[ChatInfo, bool]:
        """Новый чат без старой переписки (project_id= — в каком проекте). Второе значение —
        создан ли чат (False: открытый и так был пуст)."""
        return await self.chats.new_chat(user_id, **kwargs)

    async def history_size(self, user_id: int) -> int:
        """Сколько сообщений в открытом чате."""
        chat_id = await self._chat_id(user_id, None)
        return await self._repository.count(user_id, chat_id=chat_id)

    async def clear_history(self, user_id: int) -> int:
        """Очищает открытый чат (переписку и его файлы); возвращает число удалённых сообщений."""
        chat_id = await self._chat_id(user_id, None)
        return await self.chats.clear(user_id, chat_id)

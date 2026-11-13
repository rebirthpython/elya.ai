"""Один шаг диалога: история → контекст → ответ модели → сохранение.

Общий для чата с ботом и Mini App, поэтому у обоих интерфейсов одна память.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from ai.client import AIClient
from ai.context import build_context
from ai.prompts import build_system_prompt
from ai.providers.base import CompletionResult
from config.settings import Settings
from memory.manager import MemoryManager


class ChatService:
    def __init__(self, settings: Settings, ai_client: AIClient, memory: MemoryManager) -> None:
        self._settings = settings
        self._ai = ai_client
        self._memory = memory
        # Сообщения одного пользователя обрабатываются строго по очереди,
        # чтобы история диалога сохранялась в правильном порядке.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def ask(
        self,
        user_id: int,
        user_text: str,
        *,
        user_name: str | None = None,
        prefer: tuple[str, str] | None = None,
    ) -> CompletionResult:
        """Отвечает на сообщение и сохраняет обмен. Ошибки ИИ (AIError) пробрасываются,
        история при этом не меняется."""
        settings = self._settings
        async with self._locks[user_id]:
            history = await self._memory.get_history(user_id)
            context = build_context(
                build_system_prompt(settings.assistant_name, user_name=user_name),
                history,
                user_text,
                max_messages=settings.history_max_messages,
                max_chars=settings.history_max_chars,
            )
            result = await self._ai.complete(context, prefer=prefer)
            await self._memory.save_exchange(user_id, user_text, result.text)
        return result

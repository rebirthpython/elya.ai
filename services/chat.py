"""Один шаг диалога: история → контекст → ответ модели → сохранение.

Общий для чата с ботом и Mini App, поэтому у обоих интерфейсов одна память.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from ai.client import AIClient
from ai.context import build_context
from ai.prompts import build_system_prompt
from ai.providers.base import CompletionResult
from config.settings import Settings
from memory.manager import MemoryManager
from services.longterm import LongTermMemory
from services.preferences import Preferences, PreferencesStore
from services.reminders import Reminders

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(
        self,
        settings: Settings,
        ai_client: AIClient,
        memory: MemoryManager,
        preferences: PreferencesStore | None = None,
        long_memory: LongTermMemory | None = None,
        reminders: Reminders | None = None,
    ) -> None:
        self._settings = settings
        self._ai = ai_client
        self._memory = memory
        self._preferences = preferences
        self._long_memory = long_memory
        self._reminders = reminders
        self._background: set[asyncio.Task] = set()
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
            if self._reminders is not None:
                # Просьба «напомни …» обрабатывается кодом, а не моделью: так Эля не может
                # пообещать напоминание, которого нет. Ошибки ИИ (AIError) идут как обычно.
                handled = await self._reminders.handle_chat_request(user_id, user_text)
                if handled is not None:
                    await self._memory.save_exchange(user_id, user_text, handled)
                    return CompletionResult(text=handled, provider="reminders", model="")
            history = await self._memory.get_history(user_id)
            prefs = (
                await self._preferences.get(user_id) if self._preferences else Preferences()
            )
            recall = await self._long_memory.recall(user_id) if self._long_memory else None
            context = build_context(
                build_system_prompt(
                    settings.assistant_name,
                    user_name=user_name,
                    style=prefs.style,
                    verbosity=prefs.verbosity,
                    memory=recall.prompt_block() if recall else "",
                ),
                history,
                user_text,
                max_messages=settings.history_max_messages,
                max_chars=settings.history_max_chars,
            )
            result = await self._ai.complete(context, prefer=prefer)
            await self._memory.save_exchange(user_id, user_text, result.text)

        if recall and recall.enabled and self._long_memory.worth_extracting(user_text):
            self._spawn(
                self._long_memory.extract_and_store(user_id, user_text, result.text, recall.facts)
            )
        return result

    def _spawn(self, coroutine) -> None:
        """Запускает выделение фактов в фоне: ответ пользователю от него не зависит."""
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        """Ждёт завершения фоновых задач (для тестов и корректной остановки)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

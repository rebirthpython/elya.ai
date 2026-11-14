"""Один шаг диалога: история → контекст → ответ модели → сохранение.

Общий для чата с ботом и Mini App, поэтому у обоих интерфейсов одна память.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator

from ai.client import AIClient
from ai.context import build_context
from ai.prompts import build_system_prompt
from ai.providers.base import CompletionResult
from config.settings import Settings
from memory.manager import MemoryManager
from services.longterm import LongTermMemory
from services.preferences import Preferences, PreferencesStore
from services.reminders import Reminders
from services.vision import DEFAULT_QUESTION, history_text

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
        images: tuple[str, ...] = (),
    ) -> CompletionResult:
        """Отвечает на сообщение и сохраняет обмен. Ошибки ИИ (AIError) пробрасываются,
        история при этом не меняется."""
        result: CompletionResult | None = None
        async for event in self.ask_stream(
            user_id, user_text, user_name=user_name, prefer=prefer, images=images
        ):
            if isinstance(event, CompletionResult):
                result = event
        assert result is not None
        return result

    async def ask_stream(
        self,
        user_id: int,
        user_text: str,
        *,
        user_name: str | None = None,
        prefer: tuple[str, str] | None = None,
        images: tuple[str, ...] = (),
    ) -> AsyncIterator[str | CompletionResult]:
        """Как ask(), но по частям: строки — новый текст ответа по мере генерации,
        последним — CompletionResult. Обмен сохраняется в историю, только когда ответ
        пришёл целиком; если поток прервали, история не меняется."""
        settings = self._settings
        # С фото: модель получает подпись (или вопрос по умолчанию), а в историю
        # попадает пометка «📷 Фото: подпись» — сама картинка не хранится.
        question = user_text.strip() or (DEFAULT_QUESTION if images else "")
        stored_text = history_text(user_text) if images else user_text
        async with self._locks[user_id]:
            if self._reminders is not None and not images:
                # Просьба «напомни …» обрабатывается кодом, а не моделью: так Эля не может
                # пообещать напоминание, которого нет. Ошибки ИИ (AIError) идут как обычно.
                handled = await self._reminders.handle_chat_request(user_id, user_text)
                if handled is not None:
                    await self._memory.save_exchange(user_id, user_text, handled)
                    yield CompletionResult(text=handled, provider="reminders", model="")
                    return
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
                question,
                max_messages=settings.history_max_messages,
                max_chars=settings.history_max_chars,
                images=images,
            )
            result: CompletionResult | None = None
            async for event in self._ai.stream(context, prefer=prefer):
                if isinstance(event, CompletionResult):
                    result = event
                else:
                    yield event
            assert result is not None
            await self._memory.save_exchange(user_id, stored_text, result.text)

        if user_text and recall and recall.enabled and self._long_memory.worth_extracting(user_text):
            self._spawn(
                self._long_memory.extract_and_store(user_id, user_text, result.text, recall.facts)
            )
        yield result

    def _spawn(self, coroutine) -> None:
        """Запускает выделение фактов в фоне: ответ пользователю от него не зависит."""
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        """Ждёт завершения фоновых задач (для тестов и корректной остановки)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

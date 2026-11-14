"""Один шаг диалога: история → контекст → ответ модели → сохранение.

Общий для чата с ботом и Mini App, поэтому у обоих интерфейсов одна память.
Если подключён поиск, ответ может идти в два шага: модель просит поискать
(<search>запрос</search>), бот ищет в Google, и модель отвечает по результатам,
а к ответу добавляется список источников.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from ai.client import AIClient
from ai.context import build_context
from ai.prompts import (
    NO_INTERNET_RULE,
    SEARCH_AUTO_RULE,
    SEARCH_FAILED_RULE,
    SEARCH_FORCE_RULE,
    SEARCH_RESULTS_RULE,
    build_system_prompt,
)
from ai.providers.base import ChatMessage, CompletionResult
from config.settings import Settings
from memory.manager import MemoryManager
from services.instructions import InstructionsStore
from services.longterm import LongTermMemory
from services.preferences import Preferences, PreferencesStore
from services.reminders import Reminders
from services.search import (
    MarkerDetector,
    SearchError,
    SearchResult,
    SearchStarted,
    WebSearch,
    attach_sources,
    results_block,
    strip_markers,
    strip_sources,
    wants_search,
)
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
        *,
        instructions: InstructionsStore | None = None,
        search: WebSearch | None = None,
    ) -> None:
        self._settings = settings
        self._ai = ai_client
        self._memory = memory
        self._preferences = preferences
        self._long_memory = long_memory
        self._reminders = reminders
        self._instructions = instructions
        self._search = search
        self._background: set[asyncio.Task] = set()
        # Сообщения одного пользователя обрабатываются строго по очереди,
        # чтобы история диалога сохранялась в правильном порядке.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def search_enabled(self) -> bool:
        return self._search is not None

    @property
    def search_auto(self) -> bool:
        """Эля сама решает, когда искать (иначе — только по просьбе)."""
        return self._search is not None and self._search.auto

    async def ask(
        self,
        user_id: int,
        user_text: str,
        *,
        user_name: str | None = None,
        prefer: tuple[str, str] | None = None,
        images: tuple[str, ...] = (),
        search: bool | None = None,
    ) -> CompletionResult:
        """Отвечает на сообщение и сохраняет обмен. Ошибки ИИ (AIError) пробрасываются,
        история при этом не меняется."""
        result: CompletionResult | None = None
        async for event in self.ask_stream(
            user_id, user_text, user_name=user_name, prefer=prefer, images=images, search=search
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
        search: bool | None = None,
    ) -> AsyncIterator[str | SearchStarted | CompletionResult]:
        """Как ask(), но по частям: строки — новый текст ответа по мере генерации,
        SearchStarted — Эля ищет в интернете, последним — CompletionResult. Обмен
        сохраняется в историю, только когда ответ пришёл целиком; если поток прервали,
        история не меняется.

        search: True — обязательно поискать (кнопка в Mini App, /search), False — не искать,
        None — по просьбе в тексте или по решению модели (если включён SEARCH_AUTO).
        """
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
            history = [
                ChatMessage(m.role, strip_sources(m.content)) if m.role == "assistant" else m
                for m in await self._memory.get_history(user_id)
            ]
            prefs = (
                await self._preferences.get(user_id) if self._preferences else Preferences()
            )
            recall = await self._long_memory.recall(user_id) if self._long_memory else None
            custom = await self._instructions.get(user_id) if self._instructions else None

            def context(rule: str = NO_INTERNET_RULE, results: str = "") -> list[ChatMessage]:
                return build_context(
                    build_system_prompt(
                        settings.assistant_name,
                        user_name=user_name,
                        style=prefs.style,
                        verbosity=prefs.verbosity,
                        memory=recall.prompt_block() if recall else "",
                        instructions=custom.prompt_block() if custom else "",
                        can_draw=settings.images_enabled,
                        internet_rule=rule,
                        search_results=results,
                    ),
                    history,
                    question,
                    max_messages=settings.history_max_messages,
                    max_chars=settings.history_max_chars,
                    images=images,
                )

            mode = self._search_mode(user_text, images, search)
            result: CompletionResult | None = None
            if mode is None:
                async for event in self._ai.stream(context(), prefer=prefer):
                    if isinstance(event, CompletionResult):
                        result = event
                    else:
                        yield event
            else:
                # Шаг 1: модель либо сразу отвечает (текст идёт пользователю как обычно),
                # либо просит поискать — тогда служебная строка не показывается.
                detector = MarkerDetector()
                first = self._ai.stream(
                    context(SEARCH_FORCE_RULE if mode == "force" else SEARCH_AUTO_RULE),
                    prefer=prefer,
                )
                try:
                    async for event in first:
                        if isinstance(event, CompletionResult):
                            result = event
                            break
                        shown = detector.feed(event)
                        if detector.complete or (mode == "force" and detector.mode == "text"):
                            break  # запрос дописан или модель не стала искать, хотя просили
                        if shown:
                            yield shown
                finally:
                    await first.aclose()
                if detector.mode is None and result is not None:
                    if tail := detector.flush():
                        yield tail
                if detector.mode == "search" or (mode == "force" and detector.mode == "text"):
                    # Шаг 2: поиск и ответ по результатам.
                    query = detector.query or " ".join(question.split())
                    yield SearchStarted(query)
                    results = await self._find(query)
                    second = (
                        context(
                            SEARCH_RESULTS_RULE,
                            results_block(query, results, datetime.now(timezone.utc).strftime("%Y-%m-%d")),
                        )
                        if results
                        else context(SEARCH_FAILED_RULE)
                    )
                    result = None
                    async for event in self._ai.stream(second, prefer=prefer):
                        if isinstance(event, CompletionResult):
                            result = event
                        else:
                            yield event
                    assert result is not None
                    result = dataclasses.replace(result, text=attach_sources(result.text, results))
                else:
                    assert result is not None
                    result = dataclasses.replace(result, text=strip_markers(result.text) or result.text)
            assert result is not None
            await self._memory.save_exchange(user_id, stored_text, result.text)

        if user_text and recall and recall.enabled and self._long_memory.worth_extracting(user_text):
            self._spawn(
                self._long_memory.extract_and_store(
                    user_id, user_text, strip_sources(result.text), recall.facts
                )
            )
        yield result

    def _search_mode(self, text: str, images: tuple[str, ...], search: bool | None) -> str | None:
        """None — без поиска, "auto" — модель решает сама, "force" — искать обязательно."""
        if self._search is None or images or search is False:
            return None
        if search or wants_search(text):
            return "force"
        return "auto" if self._search.auto else None

    async def _find(self, query: str) -> list[SearchResult]:
        """Результаты поиска; при ошибке — пустой список (Эля ответит без них и предупредит)."""
        try:
            return await self._search.search(query)
        except SearchError as exc:
            logger.warning("Web search unavailable: %s", exc)
            return []

    def _spawn(self, coroutine) -> None:
        """Запускает выделение фактов в фоне: ответ пользователю от него не зависит."""
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        """Ждёт завершения фоновых задач (для тестов и корректной остановки)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

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
from services.artifacts import (
    DOCUMENT_RULE,
    PROJECT_RULE,
    FileStore,
    OutputFile,
    attachments_json,
    build_outputs,
    named_files,
    wants_document,
    wants_project,
)
from services.code import CodeFile, file_message
from services.documents import (
    DEFAULT_DOCUMENT_QUESTION,
    DEFAULT_DOCUMENTS_QUESTION,
    DocumentContext,
    DocumentStore,
)
from services.documents import history_text as document_history_text
from services.instructions import InstructionsStore
from services.longterm import LongTermMemory
from services.modes import CODE, DEFAULT_MODE, ModeStore
from services.preferences import Preferences, PreferencesStore
from services.reminders import Reminders
from services.search import (
    MarkerDetector,
    SearchError,
    SearchResult,
    SearchStarted,
    WebSearch,
    attach_sources,
    needs_fresh_data,
    results_block,
    strip_markers,
    strip_sources,
    wants_search,
)
from services.vision import DEFAULT_QUESTION, history_text

logger = logging.getLogger(__name__)

# В режиме кода «напомни» может встретиться во вставленном коде, поэтому просьбой
# о напоминании считается только короткое сообщение без блоков кода.
_CODE_REMINDER_MAX_CHARS = 300


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
        modes: ModeStore | None = None,
        search: WebSearch | None = None,
        documents: DocumentStore | None = None,
        files: FileStore | None = None,
        github=None,
    ) -> None:
        self._settings = settings
        self._ai = ai_client
        self._memory = memory
        self._preferences = preferences
        self._long_memory = long_memory
        self._reminders = reminders
        self._instructions = instructions
        self._modes = modes
        self._search = search
        self._documents = documents
        self._files = files
        self._github = github  # services.github.GitHubService
        self._background: set[asyncio.Task] = set()
        # Сообщения одного пользователя обрабатываются строго по очереди,
        # чтобы история диалога сохранялась в правильном порядке.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def search_enabled(self) -> bool:
        return self._search is not None

    @property
    def files(self) -> FileStore | None:
        """Готовые файлы к ответам (DOCX, PPTX, ZIP, HTML); None — не сохраняются."""
        return self._files

    @property
    def github(self):
        """GitHub (services.github.GitHubService); None — выключен."""
        return self._github

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def documents(self) -> DocumentStore | None:
        """Документы разговора (PDF, Word, таблицы); None — функция выключена."""
        return self._documents

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
        file: CodeFile | None = None,
        search: bool | None = None,
        documents: tuple[int, ...] = (),
    ) -> CompletionResult:
        """Отвечает на сообщение и сохраняет обмен. Ошибки ИИ (AIError) пробрасываются,
        история при этом не меняется."""
        result: CompletionResult | None = None
        async for event in self.ask_stream(
            user_id, user_text, user_name=user_name, prefer=prefer, images=images, file=file,
            search=search, documents=documents,
        ):
            if isinstance(event, CompletionResult):
                result = event
        assert result is not None
        return result  # готовые файлы — в result.files

    async def ask_stream(
        self,
        user_id: int,
        user_text: str,
        *,
        user_name: str | None = None,
        prefer: tuple[str, str] | None = None,
        images: tuple[str, ...] = (),
        file: CodeFile | None = None,
        search: bool | None = None,
        documents: tuple[int, ...] = (),
    ) -> AsyncIterator[str | SearchStarted | CompletionResult]:
        """Как ask(), но по частям: строки — новый текст ответа по мере генерации,
        SearchStarted — Эля ищет в интернете, последним — CompletionResult. Обмен
        сохраняется в историю, только когда ответ пришёл целиком; если поток прервали,
        история не меняется.

        file — присланный файл с кодом: его текст вместе с подписью уходит модели
        и сохраняется в истории, чтобы о файле можно было спрашивать дальше.

        search: True — обязательно поискать (кнопка в Mini App, /search), False — не искать,
        None — по просьбе в тексте или по решению модели (если включён SEARCH_AUTO).

        documents — id документов, присланных вместе с этим сообщением. Документы разговора
        учитываются и без этого: к каждому сообщению подбираются подходящие фрагменты.
        """
        settings = self._settings
        if file is not None:
            question = stored_text = file_message(file, user_text)
        elif documents:
            # Файл без вопроса — Эля коротко рассказывает, о чём он.
            question = user_text.strip() or (
                DEFAULT_DOCUMENT_QUESTION if len(documents) == 1 else DEFAULT_DOCUMENTS_QUESTION
            )
            stored_text = ""  # имена файлов станут известны ниже, из хранилища
        else:
            # С фото: модель получает подпись (или вопрос по умолчанию), а в историю
            # попадает пометка «📷 Фото: подпись» — сама картинка не хранится.
            question = user_text.strip() or (DEFAULT_QUESTION if images else "")
            stored_text = history_text(user_text) if images else user_text
        async with self._locks[user_id]:
            # Чат, в котором задан вопрос: ответ сохранится в него, даже если тем временем
            # откроют другой. У проекта чата — свои инструкции, файлы и режим.
            chat = await self._memory.chats.active(user_id)
            project = await self._memory.chats.project(user_id, chat.project_id)
            chat_mode = await self._modes.get(user_id) if self._modes else DEFAULT_MODE
            if project is not None and project.mode == CODE:
                chat_mode = CODE
            coding = chat_mode == CODE
            looks_like_code = coding and (
                len(user_text) > _CODE_REMINDER_MAX_CHARS or "```" in user_text
            )
            if (
                self._reminders is not None
                and not images
                and file is None
                and not documents
                and not looks_like_code
            ):
                # Просьба «напомни …» обрабатывается кодом, а не моделью: так Эля не может
                # пообещать напоминание, которого нет. Ошибки ИИ (AIError) идут как обычно.
                handled = await self._reminders.handle_chat_request(user_id, user_text)
                if handled is not None:
                    await self._memory.save_exchange(user_id, user_text, handled, chat_id=chat.id)
                    yield CompletionResult(text=handled, provider="reminders", model="")
                    return
            history = [
                ChatMessage(m.role, strip_sources(m.content)) if m.role == "assistant" else m
                for m in await self._memory.get_history(user_id, chat_id=chat.id)
            ]
            prefs = (
                await self._preferences.get(user_id) if self._preferences else Preferences()
            )
            recall = await self._long_memory.recall(user_id) if self._long_memory else None
            custom = await self._instructions.get(user_id) if self._instructions else None
            docs = DocumentContext()
            if self._documents is not None and not images and file is None:
                docs = await self._documents.context(user_id, question, fresh=documents, chat_id=chat.id)
            if documents:
                names = [info.name for info in docs.documents if info.id in documents]
                stored_text = document_history_text(names or ["документ"], user_text)

            # Просят реферат или проект — модель пишет его целиком, а бот прикладывает файлы.
            wants_doc = file is None and not images and wants_document(user_text)
            wants_zip = file is None and not images and wants_project(user_text)
            output_rules = (DOCUMENT_RULE if wants_doc else "") + (PROJECT_RULE if wants_zip else "")
            if self._github is not None and not images:
                # Подключённый репозиторий: модель знает его файлы и пишет код с путями.
                try:
                    output_rules += await asyncio.wait_for(self._github.prompt_block(user_id), 6)
                except Exception:  # noqa: BLE001 — без GitHub ответ всё равно нужен
                    logger.warning("GitHub context unavailable", exc_info=True)

            def context(rule: str = NO_INTERNET_RULE, results: str = "") -> list[ChatMessage]:
                return build_context(
                    build_system_prompt(
                        settings.assistant_name,
                        user_name=user_name,
                        style=prefs.style,
                        verbosity=prefs.verbosity,
                        memory=recall.prompt_block() if recall else "",
                        instructions=(custom.prompt_block() if custom else "")
                        + (project.prompt_block() if project else "")
                        + output_rules,
                        mode=chat_mode,
                        can_draw=settings.images_enabled,
                        internet_rule=rule,
                        search_results=results,
                        documents=docs.block,
                    ),
                    history,
                    question,
                    max_messages=settings.history_max_messages,
                    # Код и присланные файлы длиннее обычных реплик — им нужно больше места.
                    max_chars=settings.code_history_max_chars if coding else settings.history_max_chars,
                    images=images,
                )

            # Файл с кодом — это не поисковый вопрос: по нему не ищем.
            search_mode = self._search_mode(user_text, images, search) if file is None else None
            if docs.relevant and search is not True and not wants_search(user_text):
                # Вопрос по присланному документу: ответ в нём, а не в интернете.
                search_mode = None
            # В режиме кода ответ длиннее и точнее: больше токенов, ниже температура.
            options = (
                {
                    "temperature": settings.code_temperature,
                    "max_tokens": max(settings.code_max_output_tokens, settings.max_output_tokens),
                }
                if coding
                else {}
            )
            if wants_doc or wants_zip:
                # Реферат или проект длиннее обычного ответа.
                options["max_tokens"] = max(
                    options.get("max_tokens", 0), settings.study_max_output_tokens, settings.max_output_tokens
                )
            result: CompletionResult | None = None
            if search_mode is None:
                async for event in self._ai.stream(context(), prefer=prefer, **options):
                    if isinstance(event, CompletionResult):
                        result = event
                    else:
                        yield event
            else:
                # Шаг 1: модель либо сразу отвечает (текст идёт пользователю как обычно),
                # либо просит поискать — тогда служебная строка не показывается.
                detector = MarkerDetector()
                first = self._ai.stream(
                    context(SEARCH_FORCE_RULE if search_mode == "force" else SEARCH_AUTO_RULE),
                    prefer=prefer,
                    **options,
                )
                try:
                    async for event in first:
                        if isinstance(event, CompletionResult):
                            result = event
                            break
                        shown = detector.feed(event)
                        if detector.complete or (search_mode == "force" and detector.mode == "text"):
                            break  # запрос дописан или модель не стала искать, хотя просили
                        if shown:
                            yield shown
                finally:
                    await first.aclose()
                if detector.mode is None and result is not None:
                    if tail := detector.flush():
                        yield tail
                if detector.mode == "search" or (search_mode == "force" and detector.mode == "text"):
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
                    async for event in self._ai.stream(second, prefer=prefer, **options):
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
            stored_files = await self._attach_files(user_id, user_text, result.text, coding)
            if stored_files:
                result = dataclasses.replace(result, files=tuple(stored_files))
            push = await self._remember_push(user_id, user_text, result.text)
            if push is not None:
                result = dataclasses.replace(result, push=push)
            await self._memory.save_exchange(
                user_id, stored_text, result.text, chat_id=chat.id,
                attachments=attachments_json([stored for _, stored in stored_files if stored is not None]),
            )

        if user_text and recall and recall.enabled and self._long_memory.worth_extracting(user_text):
            self._spawn(
                self._long_memory.extract_and_store(
                    user_id, user_text, strip_sources(result.text), recall.facts
                )
            )
        yield result

    async def _attach_files(
        self, user_id: int, request: str, answer: str, coding: bool
    ) -> list[tuple[OutputFile, object]]:
        """Файлы из ответа (DOCX, ZIP, HTML) и их записи в хранилище (None — не сохранён)."""
        try:
            outputs = await asyncio.to_thread(build_outputs, request, answer, code_mode=coding)
        except Exception:  # noqa: BLE001 — файл — дополнение, ответ важнее
            logger.exception("Could not build files for the answer")
            return []
        result = []
        for output in outputs:
            stored = await self._files.save(user_id, output) if self._files is not None else None
            result.append((output, stored))
        return result

    async def _remember_push(self, user_id: int, request: str, answer: str) -> int | None:
        """Файлы с путями из ответа — для кнопки «Отправить в GitHub» (если репозиторий выбран)."""
        if self._github is None:
            return None
        files = named_files(answer)
        if not files:
            return None
        try:
            status = await self._github.status(user_id)
        except Exception:  # noqa: BLE001
            logger.warning("GitHub status unavailable", exc_info=True)
            return None
        if not status.connected or not status.repo:
            return None
        return self._github.remember(user_id, files, request)

    def _search_mode(self, text: str, images: tuple[str, ...], search: bool | None) -> str | None:
        """None — без поиска, "auto" — модель решает сама, "force" — искать обязательно."""
        if self._search is None or images or search is False:
            return None
        if search or wants_search(text):
            return "force"
        if not self._search.auto:
            return None
        return "force" if needs_fresh_data(text) else "auto"

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

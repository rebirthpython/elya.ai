"""Конвейер учебных задач: запросы к модели по шагам, проверка ответов, исправления.

Каждый шаг — это запрос, разбор JSON и проверка кодом. Если ответ не прошёл проверку,
модель получает конкретный список замечаний и исправляет его (не больше MAX_REPAIRS раз,
чтобы не тратить бесплатную квоту). Итого на конспект и на презентацию уходит 2–4 запроса.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from ai.client import AIClient
from ai.providers.base import ChatMessage
from study import prompts
from study.jsonutil import ModelOutputError, extract_json_object
from study.notes import NOTE_MODES, Extraction, Notes, parse_extraction, parse_notes, validate_notes
from study.slides import (
    Outline,
    Presentation,
    normalize_presentation,
    parse_outline,
    parse_presentation,
    requested_slide_count,
    validate_outline,
    validate_presentation,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Текст короче этого считается темой или просьбой, а не материалом для сокращения.
TOPIC_MAX_CHARS = 400
MAX_REPAIRS = 1


class StudyError(Exception):
    """Модель так и не выдала пригодный результат."""


@dataclass(frozen=True)
class Material:
    text: str

    @property
    def is_topic(self) -> bool:
        return len(self.text) < TOPIC_MAX_CHARS

    @property
    def requested_slides(self) -> int | None:
        return requested_slide_count(self.text) if self.is_topic else None


class StudyService:
    def __init__(
        self,
        ai_client: AIClient,
        *,
        assistant_name: str,
        max_tokens: int,
        temperature: float = 0.4,
    ) -> None:
        self._ai = ai_client
        self._name = assistant_name
        self._max_tokens = max_tokens
        self._temperature = temperature

    async def _ask(
        self,
        messages: list[ChatMessage],
        build: Callable[[dict[str, Any]], T],
        check: Callable[[T], list[str]] | None = None,
        *,
        what: str,
    ) -> T:
        """Запрос -> JSON -> объект -> проверка; при замечаниях — просьба исправить."""
        conversation = list(messages)
        best: T | None = None
        for attempt in range(MAX_REPAIRS + 1):
            result = await self._ai.complete(
                conversation, max_tokens=self._max_tokens, temperature=self._temperature
            )
            try:
                built = build(extract_json_object(result.text))
            except ModelOutputError as exc:
                issues = [f"Ответ не удалось разобрать: {exc}. Нужен JSON строго по схеме."]
                if result.truncated:
                    issues.append("Ответ оборвался на середине: пиши короче, чтобы JSON поместился целиком.")
            else:
                best = built
                issues = check(built) if check else []
                if not issues:
                    return built

            logger.info("Study step %r: attempt %d has %d issue(s)", what, attempt + 1, len(issues))
            if attempt < MAX_REPAIRS:
                conversation += [
                    ChatMessage(role="assistant", content=result.text),
                    ChatMessage(role="user", content=prompts.repair_prompt(issues)),
                ]

        if best is None:
            raise StudyError(f"model returned no usable result for {what}")
        logger.warning("Study step %r accepted with unresolved issues", what)
        return best

    # ---------- Конспекты ----------

    async def extract(self, material: Material) -> Extraction:
        return await self._ask(
            [
                ChatMessage(role="system", content=prompts.EXTRACT_PROMPT),
                ChatMessage(role="user", content=prompts.material_block(material.text, from_topic=False)),
            ],
            parse_extraction,
            what="extract",
        )

    async def make_notes(self, material: Material, mode: str) -> Notes:
        if mode not in NOTE_MODES:
            raise ValueError(f"unknown notes mode: {mode}")
        from_topic = material.is_topic
        extraction = None if from_topic else await self.extract(material)

        user_content = prompts.material_block(material.text, from_topic=from_topic)
        if extraction is not None:
            user_content += f"\n\nКлючевые сведения из материала (JSON):\n{extraction.to_json()}"
        source_chars = 0 if from_topic else len(material.text)

        return await self._ask(
            [
                ChatMessage(
                    role="system",
                    content=prompts.notes_system_prompt(
                        self._name, mode, NOTE_MODES[mode], from_topic=from_topic
                    ),
                ),
                ChatMessage(role="user", content=user_content),
            ],
            lambda data: parse_notes(data, mode, from_topic=from_topic),
            lambda notes: validate_notes(notes, extraction, source_chars),
            what=f"notes:{mode}",
        )

    # ---------- Презентации ----------

    async def make_outline(
        self,
        material: Material,
        *,
        previous: Outline | None = None,
        feedback: str | None = None,
    ) -> Outline:
        # Правка плана («убери слайд», «добавь про…») отменяет прежнее число слайдов,
        # если в ней не названо новое.
        requested = requested_slide_count(feedback) if feedback else material.requested_slides
        messages = [
            ChatMessage(
                role="system",
                content=prompts.outline_system_prompt(
                    self._name, requested, from_topic=material.is_topic
                ),
            ),
            ChatMessage(role="user", content=prompts.material_block(material.text, from_topic=material.is_topic)),
        ]
        if previous is not None:
            messages += [
                ChatMessage(role="assistant", content=previous.to_json()),
                ChatMessage(
                    role="user",
                    content=(
                        f"Переделай план с учётом пожеланий: {feedback}"
                        if feedback
                        else "Предложи другой вариант плана: другую логику рассказа и другие заголовки."
                    )
                    + f"\n{prompts.JSON_ONLY}",
                ),
            ]
        return await self._ask(
            messages,
            parse_outline,
            lambda outline: validate_outline(outline, requested),
            what="outline",
        )

    async def make_presentation(self, material: Material, outline: Outline) -> Presentation:
        presentation = await self._ask(
            [
                ChatMessage(
                    role="system",
                    content=prompts.content_system_prompt(self._name, from_topic=material.is_topic),
                ),
                ChatMessage(
                    role="user",
                    content=prompts.material_block(material.text, from_topic=material.is_topic)
                    + f"\n\nУтверждённый план (JSON):\n{outline.to_json()}",
                ),
            ],
            parse_presentation,
            lambda p: validate_presentation(p, outline),
            what="slides",
        )
        try:
            return normalize_presentation(presentation)
        except ModelOutputError as exc:
            raise StudyError(str(exc)) from exc

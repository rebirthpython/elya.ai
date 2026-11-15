"""Режим глубокого размышления.

Ответ идёт в два шага: сначала модель пишет черновик размышлений (разбирает задачу по шагам,
ищет ошибки, проверяет себя), затем по нему — итоговый ответ. Черновик собеседник видит
свёрнутым над ответом, в историю диалога он не попадает. Режим включается на пользователя
(общий для бота и Mini App) или разово: /think вопрос.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from database.models import ThinkingSettings

# Сколько черновика передавать модели на шаге ответа (остальное вырезается из середины).
PROMPT_REASONING_CHARS = 12000


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """Новый кусок размышлений. Первое событие — пустое: размышление началось."""

    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDone:
    """Размышление закончено, дальше идёт ответ."""

    reasoning: str
    seconds: float


def clip_middle(text: str, limit: int, marker: str = "\n[…]\n") -> str:
    """Не длиннее limit символов: начало и конец сохраняются, середина вырезается.

    В размышлениях важны и постановка задачи (начало), и вывод (конец)."""
    if len(text) <= limit:
        return text
    budget = max(0, limit - len(marker))
    head = budget * 3 // 5
    tail = budget - head
    return text[:head].rstrip() + marker + (text[-tail:].lstrip() if tail else "")


def format_seconds(seconds: float) -> str:
    """23 → «23 с», 75 → «1 мин 15 с»."""
    total = max(1, round(seconds))
    minutes, rest = divmod(total, 60)
    if not minutes:
        return f"{rest} с"
    return f"{minutes} мин {rest} с" if rest else f"{minutes} мин"


class ThinkingStore:
    """Включён ли режим глубокого размышления (по умолчанию выключен)."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def is_enabled(self, user_id: int) -> bool:
        async with self._session_factory() as session:
            row = await session.get(ThinkingSettings, user_id)
        return bool(row and row.enabled)

    async def set_enabled(self, user_id: int, enabled: bool) -> bool:
        async with self._session_factory.begin() as session:
            row = await session.get(ThinkingSettings, user_id)
            if row is None:
                session.add(ThinkingSettings(user_id=user_id, enabled=enabled))
            else:
                row.enabled = enabled
        return enabled

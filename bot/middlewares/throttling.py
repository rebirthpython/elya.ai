"""Ограничение частоты сообщений: не больше N за скользящую минуту."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, User


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        exempt_states: frozenset[str] = frozenset(),
    ) -> None:
        """exempt_states — FSM-состояния, в которых сообщения не ограничиваются
        (например, приём длинной лекции, которую Telegram режет на много сообщений)."""
        self._limit = limit
        self._exempt_states = exempt_states
        self._window = window
        self._clock = clock
        self._hits: defaultdict[int, deque[float]] = defaultdict(deque)
        self._warned_until: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or data.get("raw_state") in self._exempt_states:
            return await handler(event, data)

        now = self._clock()
        hits = self._hits[user.id]
        while hits and now - hits[0] >= self._window:
            hits.popleft()

        if len(hits) >= self._limit:
            # Предупреждаем один раз за окно, остальные сообщения молча пропускаем.
            if self._warned_until.get(user.id, 0.0) <= now:
                self._warned_until[user.id] = hits[0] + self._window
                if isinstance(event, Message):
                    seconds = max(1, int(hits[0] + self._window - now))
                    await event.answer(
                        f"Слишком много сообщений подряд. Подожди примерно {seconds} сек. 🙂"
                    )
            return None

        hits.append(now)
        return await handler(event, data)

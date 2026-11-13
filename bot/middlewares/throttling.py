"""Ограничение частоты сообщений: не больше N за скользящую минуту."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, User

from services.rate_limit import SlidingWindowLimiter


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(
        self,
        limiter: SlidingWindowLimiter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limiter = limiter
        self._clock = clock
        self._warned_until: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        retry_after = self._limiter.hit(user.id)
        if retry_after is None:
            return await handler(event, data)

        # Предупреждаем один раз за окно, остальные сообщения молча пропускаем.
        now = self._clock()
        if self._warned_until.get(user.id, 0.0) <= now:
            self._warned_until[user.id] = now + retry_after
            if isinstance(event, Message):
                await event.answer(
                    f"Слишком много сообщений подряд. Подожди примерно "
                    f"{max(1, int(retry_after))} сек. 🙂"
                )
        return None

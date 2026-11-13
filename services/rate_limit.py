"""Скользящее окно: не больше N событий за window секунд на один ключ (пользователя)."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """Один экземпляр общий для бота и Mini App, чтобы лимит был единым."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: defaultdict[int, deque[float]] = defaultdict(deque)

    def hit(self, key: int) -> float | None:
        """Учитывает событие. None — разрешено, иначе через сколько секунд освободится место."""
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return hits[0] + self._window - now
        hits.append(now)
        return None

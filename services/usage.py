"""Дневной лимит запросов к AI-провайдерам (сохраняется в БД, переживает перезапуск)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker

from database.models import DailySearchUsage, DailyUsage


def _today() -> date:
    return datetime.now(timezone.utc).date()


class UsageTracker:
    """Считает каждую HTTP-попытку к провайдеру. daily_limit=0 — без ограничения.

    model — таблица счётчика: DailyUsage для ИИ, DailySearchUsage для поиска.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        daily_limit: int,
        *,
        model: type[DailyUsage] | type[DailySearchUsage] = DailyUsage,
    ) -> None:
        self._session_factory = session_factory
        self.daily_limit = daily_limit
        self._model = model

    async def used_today(self) -> int:
        async with self._session_factory() as session:
            row = await session.get(self._model, _today())
            return row.requests if row else 0

    async def try_consume(self) -> bool:
        """Резервирует один запрос. False — дневной лимит исчерпан."""
        today = _today()
        async with self._session_factory.begin() as session:
            row = await session.get(self._model, today)
            if row is None:
                row = self._model(day=today, requests=0)
                session.add(row)
            if self.daily_limit and row.requests >= self.daily_limit:
                return False
            row.requests += 1
            return True

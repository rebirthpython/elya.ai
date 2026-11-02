"""Доступ к боту только для разрешённых Telegram ID и только в личных чатах."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Chat, TelegramObject, User

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        self._allowed = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")

        if user is None or user.id not in self._allowed:
            if user is not None:
                # Логируем только ID — по нему владелец может узнать свой ID.
                logger.info("Access denied for user_id=%s", user.id)
            return None
        if chat is not None and chat.type != "private":
            return None
        return await handler(event, data)

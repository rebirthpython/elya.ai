"""Глобальный обработчик непредвиденных ошибок."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent, Message

logger = logging.getLogger(__name__)


async def on_error(event: ErrorEvent) -> bool:
    logger.error(
        "Unhandled error in update %s: %s",
        event.update.update_id,
        type(event.exception).__name__,
        exc_info=event.exception,
    )

    target: Message | None = event.update.message
    if target is None and event.update.callback_query is not None:
        msg = event.update.callback_query.message
        target = msg if isinstance(msg, Message) else None

    if target is not None:
        try:
            await target.answer("Ой, что-то пошло не так 😕 Попробуй ещё раз.")
        except TelegramAPIError:
            logger.warning("Failed to notify user about an error")
    return True


def create_router() -> Router:
    router = Router(name="errors")
    router.errors.register(on_error)
    return router

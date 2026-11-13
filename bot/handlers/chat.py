"""Диалог с ИИ: обычные текстовые сообщения."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from ai.client import AllProvidersFailedError, DailyLimitExceededError
from bot.utils.sender import send_long_text
from services.chat import ChatService

logger = logging.getLogger(__name__)

_FAILURE_TEXTS = {
    "rate_limit": (
        "⏳ Сейчас исчерпан лимит запросов к ИИ-модели (бесплатные тарифы ограничены). "
        "Попробуй ещё раз через минуту-другую."
    ),
    "config": (
        "⚠️ Не получается подключиться к ИИ-сервису: похоже, проблема с API-ключом, "
        "балансом или регионом сервера. Подробности — в логах бота."
    ),
    "unavailable": (
        "😔 ИИ-сервис сейчас не отвечает, поэтому сообщение не обработано. "
        "Отправь его ещё раз чуть позже."
    ),
}


async def handle_text(message: Message, bot: Bot, chat_service: ChatService) -> None:
    if message.from_user is None or not message.text or not message.text.strip():
        return

    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            result = await chat_service.ask(
                message.from_user.id,
                message.text.strip(),
                user_name=message.from_user.first_name,
            )
    except DailyLimitExceededError as exc:
        await message.answer(
            f"📵 Достигнут дневной лимит запросов к ИИ ({exc.limit}). "
            "Он обновится в 00:00 по UTC. Это защита от перерасхода бесплатной квоты."
        )
        return
    except AllProvidersFailedError as exc:
        logger.error("All AI providers failed (%s)", exc.kind)
        await message.answer(_FAILURE_TEXTS[exc.kind])
        return

    reply = result.text
    if result.truncated:
        reply += "\n\n_(ответ обрезан из-за ограничения длины — напиши «продолжи»)_"
    await send_long_text(message, reply)


async def handle_unsupported(message: Message) -> None:
    await message.answer(
        "Пока я понимаю только текстовые сообщения. Голосовые и фото научусь разбирать позже 🙂"
    )


def create_router() -> Router:
    router = Router(name="chat")
    router.message.register(handle_text, F.text)
    router.message.register(handle_unsupported)
    return router

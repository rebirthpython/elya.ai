"""Диалог с ИИ: обычные текстовые сообщения."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from ai.client import AIClient, AllProvidersFailedError, DailyLimitExceededError
from ai.context import build_context
from ai.prompts import build_system_prompt
from bot.utils.sender import send_long_text
from config.settings import Settings
from memory.manager import MemoryManager

logger = logging.getLogger(__name__)

# Сообщения одного пользователя обрабатываются строго по очереди,
# чтобы история диалога сохранялась в правильном порядке.
_user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

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


async def handle_text(
    message: Message,
    bot: Bot,
    settings: Settings,
    ai_client: AIClient,
    memory: MemoryManager,
) -> None:
    if message.from_user is None or not message.text or not message.text.strip():
        return
    user_id = message.from_user.id
    user_text = message.text.strip()

    async with _user_locks[user_id]:
        history = await memory.get_history(user_id)
        context = build_context(
            build_system_prompt(settings.assistant_name),
            history,
            user_text,
            max_messages=settings.history_max_messages,
            max_chars=settings.history_max_chars,
        )

        try:
            async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
                result = await ai_client.complete(context)
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

        await memory.save_exchange(user_id, user_text, result.text)

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

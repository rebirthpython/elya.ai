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
from bot.utils.ai_errors import ai_error_text
from bot.utils.sender import send_long_text
from config.settings import Settings
from memory.manager import MemoryManager

logger = logging.getLogger(__name__)

# Сообщения одного пользователя обрабатываются строго по очереди,
# чтобы история диалога сохранялась в правильном порядке.
_user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

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
            await message.answer(ai_error_text(exc))
            return
        except AllProvidersFailedError as exc:
            logger.error("All AI providers failed (%s)", exc.kind)
            await message.answer(ai_error_text(exc))
            return

        await memory.save_exchange(user_id, user_text, result.text)

    reply = result.text
    if result.truncated:
        reply += "\n\n_(ответ обрезан из-за ограничения длины — напиши «продолжи»)_"
    await send_long_text(message, reply)


async def handle_unsupported(message: Message) -> None:
    await message.answer(
        "Пока я понимаю только текстовые сообщения. Голосовые и фото научусь разбирать позже 🙂\n"
        "Файл с лекцией (.txt, .docx, .pdf) можно прислать после команды /notes или /slides."
    )


def create_router() -> Router:
    router = Router(name="chat")
    router.message.register(handle_text, F.text)
    router.message.register(handle_unsupported)
    return router

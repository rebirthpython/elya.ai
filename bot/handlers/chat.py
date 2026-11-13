"""Диалог с ИИ: обычные текстовые сообщения."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from ai.client import AllProvidersFailedError, DailyLimitExceededError
from bot.utils.sender import send_long_text
from services.chat import ChatService
from services.speech import SpeechError, SpeechToText

MAX_VOICE_SECONDS = 300

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
    await _reply(message, bot, chat_service, message.text.strip())


async def handle_voice(
    message: Message, bot: Bot, chat_service: ChatService, speech: SpeechToText | None = None
) -> None:
    """Голосовое → текст (Groq Whisper) → обычный ответ Эли."""
    if message.from_user is None or message.voice is None:
        return
    if speech is None:
        await message.answer(
            "Голосовые пока не настроены: владельцу бота нужно добавить ключ GROQ_API_KEY "
            "(бесплатно на console.groq.com). А текстом я всё понимаю 🙂"
        )
        return
    if (message.voice.duration or 0) > MAX_VOICE_SECONDS:
        await message.answer("Голосовое слишком длинное: я разбираю до 5 минут.")
        return
    try:
        async with ChatActionSender.record_voice(bot=bot, chat_id=message.chat.id):
            audio = await bot.download(message.voice)
            text = await speech.transcribe(audio.getvalue() if audio else b"")
    except SpeechError as exc:
        logger.warning("Speech recognition failed: %s", exc)
        await message.answer("Не получилось разобрать голосовое 😔 Попробуй ещё раз или напиши текстом.")
        return
    if not text:
        await message.answer("Не расслышала ни слова. Попробуй записать ещё раз 🙂")
        return
    await message.answer(f"🎤 <i>{escape(text)}</i>", parse_mode=ParseMode.HTML)
    await _reply(message, bot, chat_service, text)


async def _reply(message: Message, bot: Bot, chat_service: ChatService, text: str) -> None:
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            result = await chat_service.ask(
                message.from_user.id,
                text,
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
        "Я понимаю текст и голосовые сообщения. Фото научусь разбирать позже 🙂"
    )


def create_router() -> Router:
    router = Router(name="chat")
    router.message.register(handle_text, F.text)
    router.message.register(handle_voice, F.voice)
    router.message.register(handle_unsupported)
    return router

"""Диалог с ИИ: обычные текстовые сообщения."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

from ai.client import AllProvidersFailedError, DailyLimitExceededError, NoVisionModelError
from ai.providers.base import CompletionResult
from bot.utils.live import LiveMessage
from services.chat import ChatService
from services.speech import SpeechError, SpeechToText
from services.vision import MAX_IMAGE_BYTES, NO_VISION_TEXT, ImageError, to_data_url

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


async def handle_photo(message: Message, bot: Bot, chat_service: ChatService) -> None:
    """Фото (или картинка, отправленная файлом) + подпись → ответ Эли."""
    if message.from_user is None:
        return
    if message.photo:
        # Самый крупный размер не больше ~1600 px: модели этого хватает, а запрос легче.
        sizes = sorted(message.photo, key=lambda s: s.width * s.height)
        fitting = [s for s in sizes if max(s.width, s.height) <= 1600]
        source = fitting[-1] if fitting else sizes[0]
    else:
        source = message.document
        if (source.file_size or 0) > MAX_IMAGE_BYTES:
            await message.answer("Картинка слишком большая: я принимаю до 5 МБ.")
            return
    try:
        async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
            file = await bot.download(source)
        image = to_data_url(file.getvalue() if file else b"")
    except ImageError:
        await message.answer("Не получилось открыть картинку. Пришли её как фото (JPEG, PNG или WebP).")
        return
    await _reply(message, bot, chat_service, (message.caption or "").strip(), images=(image,))


async def _reply(
    message: Message, bot: Bot, chat_service: ChatService, text: str, images: tuple[str, ...] = ()
) -> None:
    """Ответ Эли, который появляется в чате по мере генерации."""
    live = LiveMessage(bot, message)
    answer = ""
    result = None
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            async for event in chat_service.ask_stream(
                message.from_user.id,
                text,
                user_name=message.from_user.first_name,
                images=images,
            ):
                if isinstance(event, CompletionResult):
                    result = event
                else:
                    answer += event
                    await live.update(answer)
    except NoVisionModelError:
        await message.answer(NO_VISION_TEXT)
        return
    except DailyLimitExceededError as exc:
        await live.fail(
            f"📵 Достигнут дневной лимит запросов к ИИ ({exc.limit}). "
            "Он обновится в 00:00 по UTC. Это защита от перерасхода бесплатной квоты."
        )
        return
    except AllProvidersFailedError as exc:
        logger.error("All AI providers failed (%s)", exc.kind)
        await live.fail(
            "⚠️ Ответ оборвался: ИИ-сервис перестал отвечать. Отправь сообщение ещё раз."
            if live.started
            else _FAILURE_TEXTS[exc.kind]
        )
        return

    reply = result.text
    if result.truncated:
        reply += "\n\n_(ответ обрезан из-за ограничения длины — напиши «продолжи»)_"
    await live.finish(reply)


async def handle_unsupported(message: Message) -> None:
    await message.answer(
        "Я понимаю текст, голосовые и фото. Стикеры, видео и другие файлы пока не умею разбирать 🙂\n"
        "Файл с лекцией (.txt, .docx, .pdf) можно прислать после команды /notes или /slides."
    )


def create_router() -> Router:
    router = Router(name="chat")
    router.message.register(handle_text, F.text)
    router.message.register(handle_voice, F.voice)
    router.message.register(handle_photo, F.photo)
    router.message.register(handle_photo, F.document.mime_type.startswith("image/"))
    router.message.register(handle_unsupported)
    return router

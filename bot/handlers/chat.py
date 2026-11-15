"""Диалог с ИИ: обычные текстовые сообщения."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.chat_action import ChatActionSender

from ai.client import AllProvidersFailedError, DailyLimitExceededError, NoVisionModelError
from ai.providers.base import CompletionResult
from bot.utils.live import LiveMessage
from bot.utils.text import close_open_fence
from config.settings import Settings
from services.chat import ChatService
from services.code import (
    MAX_CODE_FILE_BYTES,
    CodeFile,
    CodeFileError,
    attachments,
    looks_like_code,
    read_code_file,
)
from services.modes import CODE, ModeStore
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
    await send_reply(message, bot, chat_service, message.text.strip())


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
    await send_reply(message, bot, chat_service, text)


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
    await send_reply(message, bot, chat_service, (message.caption or "").strip(), images=(image,))


_CODE_FILE_ERRORS = {
    "too_large": "Файл слишком большой: я читаю файлы с кодом до 1 МБ.",
    "empty": "Файл пустой — в нём нечего разбирать.",
    "binary": (
        "Это не похоже на текстовый файл. В режиме кода я читаю исходники и конфиги "
        "(.py, .js, .json, .txt и другие текстовые файлы), а лекцию в .docx или .pdf "
        "можно прислать после команды /notes."
    ),
}


async def handle_document(
    message: Message,
    bot: Bot,
    chat_service: ChatService,
    settings: Settings,
    modes: ModeStore | None = None,
) -> None:
    """Файл с кодом: в режиме кода Эля читает его и отвечает на подпись."""
    if message.from_user is None or message.document is None:
        return
    document = message.document
    if modes is None or await modes.get(message.from_user.id) != CODE:
        if looks_like_code(document.file_name):
            await message.answer(
                "Похоже, это файл с кодом. Включи режим кода командой /code и пришли файл "
                "ещё раз — я его прочитаю и разберу 👩‍💻"
            )
        else:
            await handle_unsupported(message)
        return
    if (document.file_size or 0) > MAX_CODE_FILE_BYTES:
        await message.answer(_CODE_FILE_ERRORS["too_large"])
        return
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            content = await bot.download(document)
        file = read_code_file(
            content.getvalue() if content else b"",
            document.file_name,
            max_chars=settings.code_max_file_chars,
        )
    except CodeFileError as exc:
        await message.answer(_CODE_FILE_ERRORS[str(exc)])
        return
    if file.truncated:
        await message.answer(
            f"Файл длинный: я прочитала первые {len(file.text)} из {file.total_chars} символов. "
            "Если нужна другая часть — пришли её отдельным файлом."
        )
    await send_reply(message, bot, chat_service, (message.caption or "").strip(), file=file)


async def _send_code_files(message: Message, text: str) -> None:
    """Длинные блоки кода из ответа — ещё и файлами: так их удобнее сохранить."""
    for index, (name, data) in enumerate(attachments(text)):
        try:
            await message.answer_document(
                BufferedInputFile(data, filename=name),
                caption="📎 Код из ответа — файлом" if index == 0 else None,
            )
        except TelegramAPIError as exc:
            # Сам ответ уже в чате; файл — дополнение.
            logger.warning("Could not send the code file: %s", exc)
            return


async def send_reply(
    message: Message,
    bot: Bot,
    chat_service: ChatService,
    text: str,
    images: tuple[str, ...] = (),
    file: CodeFile | None = None,
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
                file=file,
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
        # Оборванный блок кода закрывается, иначе пометка попала бы внутрь кода.
        reply = close_open_fence(reply)
        reply += "\n\n_(ответ обрезан из-за ограничения длины — напиши «продолжи»)_"
    await live.finish(reply)
    await _send_code_files(message, result.text)


async def handle_unsupported(message: Message) -> None:
    await message.answer(
        "Я понимаю текст, голосовые и фото. Стикеры, видео и другие файлы пока не умею разбирать 🙂\n"
        "Файл с лекцией (.txt, .docx, .pdf) можно прислать после команды /notes или /slides, "
        "а файл с кодом — в режиме кода: /code."
    )


def create_router() -> Router:
    router = Router(name="chat")
    router.message.register(handle_text, F.text)
    router.message.register(handle_voice, F.voice)
    router.message.register(handle_photo, F.photo)
    router.message.register(handle_photo, F.document.mime_type.startswith("image/"))
    # GIF приходит и как animation, и как document — это не файл с кодом.
    router.message.register(handle_document, F.document, ~F.animation)
    router.message.register(handle_unsupported)
    return router

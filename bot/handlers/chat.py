"""Диалог с ИИ: текст, голосовые, фото, документы и поиск в интернете (/search)."""

from __future__ import annotations

import logging
from html import escape
from pathlib import PurePath

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiogram.utils.chat_action import ChatActionSender

from ai.client import AllProvidersFailedError, DailyLimitExceededError, NoVisionModelError
from ai.providers.base import CompletionResult
from bot.handlers.documents import receive_document
from bot.handlers.github import offer_push
from bot.handlers.draw import DrawStates, draw_and_send, edit_and_send, maybe_draw, maybe_edit
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
    wants_files,
    read_code_file,
)
from services.artifacts import OutputFile, wants_presentation
from services.documents import PLAIN_TEXT_EXTENSIONS, is_document, unsupported_hint
from services.modes import CODE, ModeStore
from services.drawing import DrawingService
from services.links import LinkStarted
from services.search import SearchStarted
from services.speech import SpeechError, SpeechToText
from services.vision import MAX_IMAGE_BYTES, NO_VISION_TEXT, ImageError, history_text, to_data_url

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


async def maybe_presentation(
    message: Message, bot: Bot, text: str, state: FSMContext | None, settings: Settings | None
) -> bool:
    """«Сделай презентацию про космос» — тот же путь, что /slides: план, оформление, PPTX."""
    if state is None or settings is None or not wants_presentation(text):
        return False
    from bot.handlers.study import start_slides  # study импортирует этот модуль

    await start_slides(message, state, bot, settings, text)
    return True


async def handle_text(
    message: Message,
    bot: Bot,
    chat_service: ChatService,
    drawing: DrawingService | None = None,
    state: FSMContext | None = None,
    settings: Settings | None = None,
) -> None:
    if message.from_user is None or not message.text or not message.text.strip():
        return
    text = message.text.strip()
    if await maybe_presentation(message, bot, text, state, settings):
        return
    if await maybe_draw(message, bot, drawing, text):
        return
    if await maybe_edit(message, bot, drawing, text):
        return
    await send_reply(message, bot, chat_service, text)


async def handle_voice(
    message: Message,
    bot: Bot,
    chat_service: ChatService,
    speech: SpeechToText | None = None,
    drawing: DrawingService | None = None,
    state: FSMContext | None = None,
    settings: Settings | None = None,
) -> None:
    """Голосовое → текст (Groq Whisper) → обычный ответ Эли (или картинка, если просят нарисовать)."""
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
    if drawing is not None and drawing.enabled and state is not None \
            and await state.get_state() == DrawStates.waiting.state:
        # После /draw описание картинки можно надиктовать.
        await state.clear()
        await draw_and_send(message, bot, drawing, text, explicit=True)
        return
    if await maybe_presentation(message, bot, text, state, settings):
        return
    if await maybe_draw(message, bot, drawing, text):
        return
    if await maybe_edit(message, bot, drawing, text):
        return
    await send_reply(message, bot, chat_service, text)


async def handle_photo(
    message: Message, bot: Bot, chat_service: ChatService, drawing: DrawingService | None = None
) -> None:
    """Фото (или картинка, отправленная файлом) + подпись → ответ Эли, а если в подписи
    просьба изменить картинку («убери фон») — изменённая картинка."""
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
    caption = (message.caption or "").strip()
    try:
        async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
            file = await bot.download(source)
        data = file.getvalue() if file else b""
        image = to_data_url(data)
    except ImageError:
        await message.answer("Не получилось открыть картинку. Пришли её как фото (JPEG, PNG или WebP).")
        return
    if drawing is not None and drawing.can_edit:
        # «Убери фон» следующим сообщением — тоже об этом фото.
        drawing.remember_image(message.from_user.id, data)
        if caption and drawing.looks_like_edit(caption):
            if await edit_and_send(
                message, bot, drawing, caption, data, explicit=False, request=history_text(caption)
            ):
                return
    await send_reply(message, bot, chat_service, caption, images=(image,))


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
    """Документ (PDF, Word, таблица) — в разговор, чтобы искать в нём ответы;
    файл с кодом — в режиме кода Эля читает его и отвечает на подпись."""
    if message.from_user is None or message.document is None:
        return
    document = message.document
    coding = modes is not None and await modes.get(message.from_user.id) == CODE
    extension = PurePath((document.file_name or "").lower()).suffix
    as_code = coding and (looks_like_code(document.file_name) or extension in PLAIN_TEXT_EXTENSIONS)
    if chat_service.documents is not None and is_document(document.file_name) and not as_code:
        await receive_document(message, bot, chat_service)
        return
    if not coding:
        if hint := unsupported_hint(document.file_name):
            await message.answer(f"📄 {hint}")
        elif looks_like_code(document.file_name):
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


async def _send_code_files(message: Message, text: str, request: str = "", skip: frozenset = frozenset()) -> None:
    """Код из ответа — ещё и файлами: длинный, подписанный именем файла или, если просили
    «пришли файлом», весь. Так его удобнее сохранить и запустить. skip — имена файлов,
    которые и так придут (например, страница .html)."""
    files = [item for item in attachments(text, all_blocks=wants_files(request)) if item[0] not in skip]
    for index, (name, data) in enumerate(files):
        try:
            await message.answer_document(
                BufferedInputFile(data, filename=name),
                caption="📎 Код из ответа — файлом" if index == 0 else None,
            )
        except TelegramAPIError as exc:
            # Сам ответ уже в чате; файл — дополнение.
            logger.warning("Could not send the code file: %s", exc)
            return


_FILE_CAPTIONS = {
    "docx": "📄 Документ Word — можно сразу открыть, распечатать или сдать",
    "zip": "🗂 Проект целиком — распакуй архив, там все файлы по папкам",
    "html": "🌐 Веб-страница — открой файл в браузере",
}


async def send_output_files(message: Message, files: tuple, settings: Settings | None) -> None:
    """Готовые файлы к ответу: DOCX, ZIP, HTML (для HTML — кнопка предпросмотра в приложении)."""
    for output, stored in files:
        if not isinstance(output, OutputFile):
            continue
        caption = _FILE_CAPTIONS.get(output.kind, "📎 Файл")
        markup = None
        if output.preview and stored is not None and settings is not None and settings.webapp_enabled:
            caption += " или посмотри в приложении 👇"
            separator = "&" if "?" in settings.webapp_url else "?"
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="👁 Посмотреть в приложении",
                    web_app=WebAppInfo(url=f"{settings.webapp_url}{separator}preview={stored.token}"),
                )
            ]])
        try:
            await message.answer_document(
                BufferedInputFile(output.data, filename=output.name), caption=caption, reply_markup=markup
            )
        except TelegramAPIError as exc:
            logger.warning("Could not send the file %s: %s", output.name, exc)


async def cmd_search(
    message: Message, command: CommandObject, bot: Bot, chat_service: ChatService
) -> None:
    """/search запрос — ответ по свежим результатам поиска со ссылками на источники."""
    if message.from_user is None:
        return
    if not chat_service.search_enabled:
        await message.answer(
            "🔎 Поиск в интернете пока не настроен: владельцу бота нужно добавить ключ "
            "SERPER_API_KEY или SERPAPI_API_KEY (бесплатно, подробности — в README)."
        )
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "Напиши, что найти, например: <code>/search курс евро сегодня</code>\n\n"
            "Можно и без команды: «погугли, когда выходит …» или «найди в интернете …»."
            + (" А если для ответа нужны свежие сведения, я поищу сама." if chat_service.search_auto else ""),
            parse_mode=ParseMode.HTML,
        )
        return
    await send_reply(message, bot, chat_service, query, search=True)


async def send_reply(
    message: Message,
    bot: Bot,
    chat_service: ChatService,
    text: str,
    images: tuple[str, ...] = (),
    file: CodeFile | None = None,
    *,
    search: bool | None = None,
    documents: tuple[int, ...] = (),
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
                search=search,
                documents=documents,
            ):
                if isinstance(event, CompletionResult):
                    result = event
                elif isinstance(event, SearchStarted):
                    await live.status(f"🔎 Ищу в интернете: «{event.query}»…")
                elif isinstance(event, LinkStarted):
                    await live.status(event.status_text())
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
    files = result.files
    # Код — файлами (небольшой проект — и файлами, и архивом; большой — только архивом).
    code_files = attachments(result.text, all_blocks=wants_files(text))
    if not any(output.kind == "zip" for output, _ in files) or len(code_files) <= 5:
        await _send_code_files(message, result.text, text, frozenset(output.name for output, _ in files))
    await send_output_files(message, files, chat_service.settings)
    await offer_push(message, chat_service.github, result.push)


async def handle_unsupported(message: Message) -> None:
    await message.answer(
        "Я понимаю текст, голосовые, фото, ссылки на статьи и видео и документы (PDF, Word, Excel, CSV, .txt), "
        "а видео умею монтировать (/video). Стикеры и другие файлы пока не разбираю 🙂\n"
        "Конспект лекции — команда /notes или /slides, файл с кодом — в режиме кода: /code."
    )


def create_search_router() -> Router:
    """Отдельно от общего чата: команда должна сработать раньше «неизвестной команды»."""
    router = Router(name="search")
    router.message.register(cmd_search, Command("search", "google"))
    return router


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

"""Конспекты (/notes) и презентации (/slides): пошаговый диалог с кнопками.

/notes:  материал (текст частями или файл) -> режим (подробно, кратко, детали) -> DOCX + суть в чате
/slides: тема или материал -> план (можно поправить текстом) -> тема оформления -> PPTX
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from ai.client import AIError
from bot.keyboards.study import (
    StudyCallback,
    notes_modes_keyboard,
    outline_keyboard,
    slides_start_keyboard,
)
from bot.utils.ai_errors import ai_error_text
from bot.utils.sender import send_long_text
from config.settings import Settings
from study.notes import NOTE_MODES
from study.pipeline import Material, StudyError, StudyService
from study.render_docx import notes_to_markdown, render_notes
from study.render_pptx import render_presentation
from study.slides import Outline
from study.sources import MAX_FILE_BYTES, SUPPORTED_EXTENSIONS, SourceError, extract_text, safe_filename
from study.themes import THEMES

logger = logging.getLogger(__name__)

KIND_NOTES, KIND_SLIDES = "notes", "slides"
READING_SPEED = 1000  # символов в минуту — для оценки «минут чтения»

# Части материала одного пользователя добавляются строго по очереди.
_user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# Telegram режет длинный вставленный текст на сообщения по 4096 символов, и они приходят
# пачкой. Статус с кнопками отправляется, когда части перестали приходить STATUS_DELAY секунд.
STATUS_DELAY = 1.5
_collect_seq: defaultdict[int, int] = defaultdict(int)


class StudyStates(StatesGroup):
    collecting = State()  # собираем материал
    outline = State()  # план презентации готов, ждём тему или правки
    working = State()  # модель работает


NOTES_INTRO = (
    "📚 <b>Конспект</b>\n\n"
    "Пришли материал: текст лекции или расшифровку (можно несколькими сообщениями), "
    f"файл {', '.join(SUPPORTED_EXTENSIONS)} или просто тему.\n"
    "Я выпишу все термины, даты, числа и имена, уберу воду и повторы и проверю, "
    "что ничего важного не потерялось."
)
SLIDES_INTRO = (
    "🎞 <b>Презентация</b>\n\n"
    "Пришли тему или материал (текст или файл). Число слайдов можно указать прямо в теме: "
    "<i>«Фотосинтез, 8 слайдов»</i>.\n"
    "Сначала покажу план — его можно поправить, потом выберешь оформление."
)
WORKING_TEXT = "⏳ Я ещё работаю над предыдущим заданием — подожди немного."
OUTDATED_TEXT = "Эта кнопка уже неактуальна."


def _material(data: dict[str, Any]) -> Material:
    return Material("\n\n".join(data.get("parts", [])).strip())


def _status_text(kind: str, material: Material) -> str:
    chars = len(material.text)
    if material.is_topic:
        what = "конспект" if kind == KIND_NOTES else "презентацию"
        return (
            f"📝 Тема: «{material.text}»\n\n"
            f"Сделаю {what} по своим знаниям. Если есть текст лекции или учебника — пришли его, "
            "так результат будет точнее."
        )
    minutes = max(1, round(chars / READING_SPEED))
    head = f"📥 Материал: {chars:,} символов (~{minutes} мин чтения).".replace(",", " ")
    tail = "Можно прислать продолжение — я соберу всё вместе."
    action = "Что сделать?" if kind == KIND_NOTES else "Когда всё отправишь — жми «Составить план»."
    return f"{head}\n{tail}\n\n{action}"


async def _drop_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramBadRequest:
        pass  # сообщение уже изменено или удалено


async def _start(message: Message, state: FSMContext, bot: Bot, settings: Settings,
                 kind: str, args: str | None) -> None:
    await state.clear()
    await state.set_state(StudyStates.collecting)
    await state.update_data(kind=kind, parts=[], status_id=None)
    await message.answer(NOTES_INTRO if kind == KIND_NOTES else SLIDES_INTRO, parse_mode="HTML")
    if args and args.strip():
        await _add_material(message, state, bot, settings, args.strip())


async def cmd_notes(message: Message, command: CommandObject, state: FSMContext, bot: Bot,
                    settings: Settings) -> None:
    await _start(message, state, bot, settings, KIND_NOTES, command.args)


async def cmd_slides(message: Message, command: CommandObject, state: FSMContext, bot: Bot,
                     settings: Settings) -> None:
    await _start(message, state, bot, settings, KIND_SLIDES, command.args)


async def _add_material(message: Message, state: FSMContext, bot: Bot, settings: Settings,
                        text: str) -> None:
    user_id = message.from_user.id if message.from_user else 0
    limit = settings.study_max_input_chars
    async with _user_locks[user_id]:
        data = await state.get_data()
        parts = list(data.get("parts", []))
        room = limit - sum(len(p) + 2 for p in parts)
        if room <= 0 or len(text) > room:
            if not data.get("limit_notified"):
                await message.answer(
                    f"✂️ Материал длиннее лимита ({limit} символов) — беру только начало. "
                    "Остальное можно обработать отдельно."
                )
                await state.update_data(limit_notified=True)
            if room <= 0:
                return
            text = text[:room]
        parts.append(text)
        await state.update_data(parts=parts)
        _collect_seq[user_id] += 1
        seq = _collect_seq[user_id]

    # Ждём, не придёт ли следующая часть: статус с кнопками нужен один на всю пачку.
    await asyncio.sleep(STATUS_DELAY)
    async with _user_locks[user_id]:
        if _collect_seq[user_id] != seq or await state.get_state() != StudyStates.collecting.state:
            return
        data = await state.get_data()
        await _drop_keyboard(bot, message.chat.id, data.get("status_id"))
        kind = data.get("kind", KIND_NOTES)
        keyboard = notes_modes_keyboard() if kind == KIND_NOTES else slides_start_keyboard()
        status = await message.answer(_status_text(kind, _material(data)), reply_markup=keyboard)
        await state.update_data(status_id=status.message_id)


async def on_material_text(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
    if message.text and message.text.strip():
        await _add_material(message, state, bot, settings, message.text.strip())


async def on_material_file(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
    document = message.document
    if document is None:
        return
    if document.file_size and document.file_size > MAX_FILE_BYTES:
        await message.answer("Файл слишком большой (больше 10 МБ).")
        return
    filename = document.file_name or "file.txt"
    try:
        buffer = io.BytesIO()
        await bot.download(document, destination=buffer)
        text = await asyncio.to_thread(extract_text, filename, buffer.getvalue())
    except SourceError as exc:
        await message.answer(f"📄 {exc}")
        return
    await _add_material(message, state, bot, settings, text)


async def on_unsupported_material(message: Message) -> None:
    await message.answer(
        "Для конспекта и презентации принимаю текст и файлы " + ", ".join(SUPPORTED_EXTENSIONS) + ". "
        "Фото и голосовые я понимаю в обычном чате — выйти туда можно через /cancel."
    )


async def on_busy(message: Message) -> None:
    await message.answer(WORKING_TEXT)


async def _run(
    message: Message,
    state: FSMContext,
    bot: Bot,
    *,
    progress: str,
    job: Callable[[], Awaitable[None]],
    on_error_state: State,
    retry_keyboard: Any,
) -> None:
    """Общая обвязка: статус «работаю», индикатор, ошибки с возможностью повторить."""
    await state.set_state(StudyStates.working)
    try:
        await bot.edit_message_text(progress, chat_id=message.chat.id, message_id=message.message_id)
    except TelegramBadRequest:
        await bot.send_message(message.chat.id, progress)

    error_text: str | None = None
    try:
        async with ChatActionSender.upload_document(bot=bot, chat_id=message.chat.id):
            await job()
        return
    except AIError as exc:
        error_text = ai_error_text(exc)
    except StudyError:
        logger.warning("Study job failed: model output unusable", exc_info=True)
        error_text = "😕 Модель выдала неподходящий ответ даже после исправлений. Попробуй ещё раз."
    except BaseException:
        await state.set_state(on_error_state)
        raise
    await state.set_state(on_error_state)
    await message.answer(error_text, reply_markup=retry_keyboard)


async def _make_notes(message: Message, state: FSMContext, bot: Bot, study: StudyService,
                      mode: str) -> None:
    material = _material(await state.get_data())

    async def job() -> None:
        notes = await study.make_notes(material, mode)
        document = await asyncio.to_thread(render_notes, notes)
        await send_long_text(message, notes_to_markdown(notes, full=mode == "short"))
        await message.answer_document(
            BufferedInputFile(document, safe_filename(notes.title, ".docx")),
            caption=f"📄 {NOTE_MODES[mode].capitalize()}: «{notes.title}»",
        )
        await state.clear()

    await _run(
        message, state, bot,
        progress=f"⏳ Делаю {NOTE_MODES[mode]}: выписываю термины, даты и имена, потом пишу и "
                 "проверяю, что ничего не потерялось. Обычно это до минуты.",
        job=job, on_error_state=StudyStates.collecting, retry_keyboard=notes_modes_keyboard(),
    )


async def _make_outline(message: Message, state: FSMContext, bot: Bot, study: StudyService,
                        *, feedback: str | None = None, replan: bool = False) -> None:
    data = await state.get_data()
    material = _material(data)
    previous = Outline.from_dict(data["outline"]) if (feedback or replan) and data.get("outline") else None
    back_state = StudyStates.outline if previous else StudyStates.collecting

    async def job() -> None:
        outline = await study.make_outline(material, previous=previous, feedback=feedback)
        await state.update_data(outline=asdict(outline))
        await state.set_state(StudyStates.outline)
        await send_long_text(
            message,
            "📋 **План презентации**\n\n" + outline.as_text()
            + "\n\nЕсли что-то поменять — просто напиши, например: _«убери 3-й слайд, добавь про "
            "последствия»_. Если всё нравится — выбери оформление:",
        )
        await message.answer("Оформление:", reply_markup=outline_keyboard())

    await _run(
        message, state, bot,
        progress="⏳ Составляю план презентации…",
        job=job, on_error_state=back_state,
        retry_keyboard=outline_keyboard() if previous else slides_start_keyboard(),
    )


async def _make_slides(message: Message, state: FSMContext, bot: Bot, study: StudyService,
                       theme_key: str) -> None:
    data = await state.get_data()
    material = _material(data)
    outline = Outline.from_dict(data["outline"])
    theme = THEMES[theme_key]

    async def job() -> None:
        presentation = await study.make_presentation(material, outline)
        document = await asyncio.to_thread(render_presentation, presentation, theme)
        await message.answer_document(
            BufferedInputFile(document, safe_filename(presentation.title, ".pptx")),
            caption=(
                f"🎞 «{presentation.title}» — {len(presentation.slides) + 2} слайдов, "
                f"оформление «{theme.title}».\n"
                "Пункты появляются по щелчку, между слайдами — плавные переходы. "
                "Что говорить на каждом слайде — в заметках докладчика."
            ),
        )
        await state.clear()

    await _run(
        message, state, bot,
        progress=f"⏳ Заполняю слайды и собираю презентацию («{theme.title}»)… Обычно это до минуты.",
        job=job, on_error_state=StudyStates.outline, retry_keyboard=outline_keyboard(),
    )


async def on_outline_feedback(message: Message, state: FSMContext, bot: Bot, study: StudyService) -> None:
    if not message.text or not message.text.strip():
        return
    status = await message.answer("⏳ Переделываю план…")
    await _make_outline(status, state, bot, study, feedback=message.text.strip())


async def on_study_action(callback: CallbackQuery, callback_data: StudyCallback, state: FSMContext,
                          bot: Bot, study: StudyService) -> None:
    message = callback.message if isinstance(callback.message, Message) else None
    current = await state.get_state()
    data = await state.get_data()
    action, value = callback_data.action, callback_data.value

    if action == "cancel":
        if current is not None:
            await state.clear()
        if message is not None:
            try:
                await bot.edit_message_text("Отменено.", chat_id=message.chat.id, message_id=message.message_id)
            except TelegramBadRequest:
                pass
        await callback.answer()
        return
    if current == StudyStates.working.state:
        await callback.answer(WORKING_TEXT, show_alert=True)
        return

    kind = data.get("kind")
    in_collecting = current == StudyStates.collecting.state
    in_outline = current == StudyStates.outline.state
    valid = message is not None and (
        (action == "notes" and in_collecting and kind == KIND_NOTES and value in NOTE_MODES)
        or (action == "outline" and in_collecting and kind == KIND_SLIDES)
        or (action == "replan" and in_outline)
        or (action == "theme" and in_outline and value in THEMES)
    )
    if not valid:
        await callback.answer(OUTDATED_TEXT)
        return
    await callback.answer()

    if action == "notes":
        await _make_notes(message, state, bot, study, value)
    elif action == "outline":
        await _make_outline(message, state, bot, study)
    elif action == "replan":
        await _make_outline(message, state, bot, study, replan=True)
    else:
        await _make_slides(message, state, bot, study, value)


def create_router() -> Router:
    router = Router(name="study")
    router.message.register(cmd_notes, Command("notes"))
    router.message.register(cmd_slides, Command("slides"))
    router.callback_query.register(on_study_action, StudyCallback.filter())

    not_command = ~F.text.startswith("/")
    router.message.register(on_busy, StudyStates.working, not_command)
    router.message.register(on_material_text, StudyStates.collecting, F.text, not_command)
    router.message.register(on_material_file, StudyStates.collecting, F.document)
    router.message.register(on_unsupported_material, StudyStates.collecting, not_command)
    router.message.register(on_outline_feedback, StudyStates.outline, F.text, not_command)
    return router

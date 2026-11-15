"""Монтаж видео: /video или просто видео в чат.

Клипы (можно альбомом) и музыка -> просьба словами, голосом или кнопкой -> готовое видео ->
правки следующими сообщениями («а теперь ускорь»), «Отменить» возвращает прошлый вариант,
«Готово» заканчивает монтаж и стирает файлы.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections import defaultdict
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from ai.client import AIError
from bot.keyboards.video import VideoCallback, panel_keyboard
from bot.utils.ai_errors import ai_error_text
from services.speech import SpeechError, SpeechToText
from video.ffmpeg import ClipInfo, FFmpegError
from video.plan import EditPlan, apply_limits, default_plan, fmt_time
from video.service import NO_FFMPEG_TEXT, PlanError, RenderResult, VideoError, VideoService

logger = logging.getLogger(__name__)

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # больше бот скачать не может (ограничение Bot API)
STATUS_DELAY = 1.5  # ждём, не придут ли ещё клипы альбома, прежде чем показать кнопки
PROGRESS_INTERVAL = 2.0
MAX_UNDO = 10
MAX_SENT = 12
MAX_HISTORY = 10
CAPTION_LIMIT = 1024

_user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_collect_seq: defaultdict[int, int] = defaultdict(int)

_VIDEO_EXT = {"video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv", "video/3gpp": ".3gp"}
_AUDIO_EXT = {"audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/ogg": ".ogg",
              "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac", "audio/aac": ".aac"}


class VideoStates(StatesGroup):
    session = State()  # ждём клипы, просьбы и правки
    working = State()  # модель думает или ffmpeg монтирует


INTRO = (
    "🎬 <b>Монтаж видео</b>\n\n"
    "Пришли одно или несколько видео (можно альбомом), а потом нажми кнопку или напиши, что сделать:\n"
    "• <i>обрежь первые 5 секунд и ускорь в 1,5 раза</i>\n"
    "• <i>убери паузы и добавь субтитры</i>\n"
    "• <i>сделай вертикально для Reels и подпиши «Лето 2026»</i>\n"
    "• <i>склей, наложи музыку потише, в конце затемнение</i>\n\n"
    "Музыку присылай аудиофайлом. Каждое видео — до 20 МБ: файлы больше Telegram не даёт скачать боту."
)
EXAMPLES = ("Нажми кнопку или напиши своими словами — например, <i>«обрежь первые 5 секунд»</i> "
            "или <i>«сделай вертикально и добавь субтитры»</i>.")
EDIT_HINT = ("Что поправить? Напиши, например, <i>«ускорь в 1,5 раза»</i> или <i>«добавь в начало "
             "надпись „Привет“»</i>. Можно голосовым.")
EXIT_HINT = "Закончить монтаж — «Готово» или /cancel."
WORKING_TEXT = "⏳ Я ещё монтирую — подожди немного."
OUTDATED_TEXT = "Эта кнопка уже неактуальна."
DOWNLOAD_FAILED = "Не получилось скачать файл из Telegram. Попробуй прислать его ещё раз."
TOO_BIG = ("Это видео больше 20 МБ — Telegram не даёт ботам скачивать такие файлы. Пришли его как "
           "обычное видео, а не файлом (Telegram сам его сожмёт), или раздели на части.")


# ---------- Данные сессии ----------


def _clips(data: dict[str, Any]) -> list[ClipInfo]:
    return [ClipInfo.from_dict(c) for c in data.get("clips", [])]


def _music(data: dict[str, Any]) -> ClipInfo | None:
    return ClipInfo.from_dict(data["music"]) if data.get("music") else None


def _plan(data: dict[str, Any]) -> EditPlan | None:
    return EditPlan.from_dict(data["plan"]) if data.get("plan") else None


def _key(plan: EditPlan) -> str:
    return hashlib.sha1(plan.key.encode()).hexdigest()[:16]


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else message.chat.id


async def _alive(state: FSMContext, session_id: str) -> bool:
    """Монтаж не отменили (/cancel, «Готово», /start), пока шла долгая работа."""
    return (await state.get_data()).get("session_id") == session_id


async def _begin(state: FSMContext, video: VideoService, user_id: int, *, intro: bool) -> None:
    video.workspace(user_id).clear()
    await state.clear()
    await state.set_state(VideoStates.session)
    await state.update_data(
        session_id=uuid.uuid4().hex, clips=[], music=None, plan=None, undo=[], rev=0,
        panel_id=None, sent={}, history=[], pending=[], intro=intro,
    )


async def _finish(state: FSMContext, video: VideoService | None, user_id: int) -> None:
    await state.clear()
    if video is not None:
        video.workspace(user_id).remove()


async def _drop_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramBadRequest:
        pass  # сообщение уже изменено или удалено


def _keyboard(video: VideoService, data: dict[str, Any], plan: EditPlan | None, rev: int,
              *, can_undo: bool) -> InlineKeyboardMarkup:
    return panel_keyboard(
        plan, rev=rev, clips=len(data.get("clips", [])), has_music=bool(data.get("music")),
        can_subtitles=video.can_subtitles, can_undo=can_undo,
    )


def _progress(bot: Bot, status: Message):
    last = {"time": 0.0, "text": ""}

    async def update(text: str) -> None:
        now = time.monotonic()
        same_stage = text[:2] == last["text"][:2]
        if text == last["text"] or (same_stage and now - last["time"] < PROGRESS_INTERVAL):
            return
        last.update(time=now, text=text)
        try:
            await bot.edit_message_text(text, chat_id=status.chat.id, message_id=status.message_id)
        except TelegramBadRequest:
            pass

    return update


async def _set_status(bot: Bot, status: Message, text: str, **kwargs: Any) -> None:
    try:
        await bot.edit_message_text(text, chat_id=status.chat.id, message_id=status.message_id, **kwargs)
    except TelegramBadRequest:
        await bot.send_message(status.chat.id, text, **kwargs)


# ---------- Начало и приём клипов ----------


async def cmd_video(message: Message, state: FSMContext, video: VideoService | None = None) -> None:
    if video is None or not video.ready:
        await message.answer(
            video.unavailable_text if video else NO_FFMPEG_TEXT, parse_mode=ParseMode.HTML,
        )
        return
    await _begin(state, video, _user_id(message), intro=True)
    await message.answer(INTRO, parse_mode=ParseMode.HTML)


def _media(message: Message) -> tuple[str, Any, str] | None:
    """(что это: clip | music, объект для скачивания, расширение файла)."""
    if message.video is not None:
        return "clip", message.video, _VIDEO_EXT.get(message.video.mime_type or "", ".mp4")
    if message.video_note is not None:
        return "clip", message.video_note, ".mp4"
    if message.animation is not None:
        return "clip", message.animation, ".mp4"
    if message.audio is not None:
        return "music", message.audio, _AUDIO_EXT.get(message.audio.mime_type or "", ".mp3")
    document = message.document
    if document is not None and document.mime_type:
        suffix = Path(document.file_name or "").suffix.lower()
        suffix = suffix if suffix[1:].isalnum() and len(suffix) <= 6 else ""
        if document.mime_type.startswith("video/"):
            return "clip", document, suffix or _VIDEO_EXT.get(document.mime_type, ".mp4")
        if document.mime_type.startswith("audio/"):
            return "music", document, suffix or _AUDIO_EXT.get(document.mime_type, ".mp3")
    return None


async def on_media(message: Message, state: FSMContext, bot: Bot, video: VideoService | None = None) -> None:
    found = _media(message)
    if found is None:
        return
    if video is None or not video.ready:
        await message.answer(
            video.unavailable_text if video else NO_FFMPEG_TEXT, parse_mode=ParseMode.HTML,
        )
        return
    kind, media, ext = found
    user_id = _user_id(message)
    if (getattr(media, "file_size", None) or 0) > TELEGRAM_DOWNLOAD_LIMIT:
        await message.answer(TOO_BIG)
        return

    async with _user_locks[user_id]:
        # Клипы альбома, присланного прямо в чат, приходят разом: сессию начинает только первый.
        began = await state.get_state() is None
        if began:
            await _begin(state, video, user_id, intro=False)
        elif await state.get_state() != VideoStates.session.state:
            await message.answer(WORKING_TEXT)
            return
        data = await state.get_data()
        clips = list(data.get("clips", []))
        ws = video.workspace(user_id).ensure()
        name = f"{kind}-{message.message_id}{ext}"

        async def fail(text: str) -> None:
            ws.path(name).unlink(missing_ok=True)
            if began:  # видео в чат не открылось — не держим пустой монтаж, дальше обычный чат
                await _finish(state, video, user_id)
            await message.answer(text)

        if kind == "clip" and len(clips) >= video.max_clips:
            if not data.get("limit_notified"):
                await state.update_data(limit_notified=True)
                await message.answer(f"Можно до {video.max_clips} клипов за один монтаж — остальные пропускаю.")
            return
        _collect_seq[user_id] += 1
        seq = _collect_seq[user_id]
        try:
            await bot.download(media, destination=ws.path(name), timeout=180)
        except TelegramBadRequest as exc:
            logger.info("Video download refused: %s", exc)
            await fail(TOO_BIG if "too big" in str(exc).lower() else DOWNLOAD_FAILED)
            return
        except (TelegramAPIError, OSError, asyncio.TimeoutError) as exc:
            logger.warning("Video download failed: %s", exc)
            await fail(DOWNLOAD_FAILED)
            return
        try:
            info = await video.probe(ws.path(name))
        except VideoError as exc:
            await fail(str(exc))
            return
        if kind == "clip" and not info.has_video:
            kind = "music"  # «видео» без картинки — это звук
        if kind == "music" and not info.has_audio:
            await fail("В этом файле нет звука — для музыки он не подойдёт.")
            return
        if kind == "clip" and info.duration < 0.3:
            await fail("Это видео слишком короткое для монтажа.")
            return

        updates: dict[str, Any] = {}
        if kind == "clip":
            clips.append({**info.to_dict(), "msg": message.message_id})
            clips.sort(key=lambda c: c["msg"])  # клипы альбома — в том порядке, в каком их отправили
            updates["clips"] = clips
        else:
            if old := data.get("music"):
                ws.path(old["file"]).unlink(missing_ok=True)
            title = getattr(media, "title", None) or getattr(media, "file_name", None) or "трек"
            updates["music"] = {**info.to_dict(), "title": str(title)[:60]}
        if (caption := (message.caption or "").strip()):
            updates["pending"] = [*data.get("pending", []), caption]
        await state.update_data(**updates)

    # Альбом приходит пачкой сообщений: кнопки показываем один раз, когда клипы перестали приходить.
    await asyncio.sleep(STATUS_DELAY)
    async with _user_locks[user_id]:
        if _collect_seq[user_id] != seq or await state.get_state() != VideoStates.session.state:
            return
        pending = (await state.get_data()).get("pending", [])
        await state.update_data(pending=[])
    if pending:
        await _instruct(message, state, bot, video, " ".join(pending))
    else:
        await _show_panel(message, state, bot, video)


def _panel_text(data: dict[str, Any]) -> str:
    clips = _clips(data)
    lines = []
    if not data.get("intro") and data.get("plan") is None:
        lines.append("🎬 Давай смонтирую!\n")
    if clips:
        lines.append("📥 <b>Клипы</b>")
        for number, clip in enumerate(clips, start=1):
            sound = "" if clip.has_audio else ", без звука"
            lines.append(f"{number}) {fmt_time(clip.duration)}, {clip.orientation}{sound}")
    else:
        lines.append("Пока нет ни одного видео — пришли его сюда 🎬")
    if music := data.get("music"):
        lines.append(f"🎵 Музыка: «{escape(music['title'])}» ({fmt_time(music['duration'])})")
    lines.append("")
    if data.get("plan") is None:
        lines.append(EXAMPLES)
    else:
        lines.append("Напиши, что сделать с новым — например, <i>«добавь клип "
                     f"{max(1, len(clips))} в конец»</i> или <i>«наложи музыку потише»</i>.")
    return "\n".join(lines)


async def _show_panel(message: Message, state: FSMContext, bot: Bot, video: VideoService) -> None:
    data = await state.get_data()
    plan = _plan(data)
    rev = data.get("rev", 0)
    await _drop_keyboard(bot, message.chat.id, data.get("panel_id"))
    panel = await message.answer(
        _panel_text(data), parse_mode=ParseMode.HTML,
        reply_markup=_keyboard(video, data, plan, rev, can_undo=bool(data.get("undo"))),
    )
    await state.update_data(panel_id=panel.message_id)


# ---------- Просьбы словами ----------


async def on_text(message: Message, state: FSMContext, bot: Bot, video: VideoService | None = None) -> None:
    if message.text and message.text.strip():
        await _instruct(message, state, bot, video, message.text.strip())


async def on_voice(message: Message, state: FSMContext, bot: Bot, video: VideoService | None = None,
                   speech: SpeechToText | None = None) -> None:
    if message.voice is None:
        return
    if speech is None:
        await message.answer("Голосовые пока не настроены (нужен ключ GROQ_API_KEY) — напиши, пожалуйста, текстом.")
        return
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            audio = await bot.download(message.voice)
            text = await speech.transcribe(audio.getvalue() if audio else b"")
    except (SpeechError, TelegramAPIError) as exc:
        logger.warning("Speech recognition failed: %s", exc)
        await message.answer("Не получилось разобрать голосовое 😔 Попробуй ещё раз или напиши текстом.")
        return
    if not text:
        await message.answer("Не расслышала ни слова. Попробуй записать ещё раз 🙂")
        return
    await message.answer(f"🎤 <i>{escape(text)}</i>", parse_mode=ParseMode.HTML)
    await _instruct(message, state, bot, video, text)


async def _instruct(message: Message, state: FSMContext, bot: Bot, video: VideoService | None, text: str) -> None:
    if video is None or not video.ready:
        return
    data = await state.get_data()
    clips = _clips(data)
    if not clips:
        await message.answer("Сначала пришли видео 🎬 — потом скажешь, что с ним сделать. Выйти — /cancel.")
        return
    session_id = data["session_id"]
    current = _plan(data)
    await state.set_state(VideoStates.working)
    status = await message.answer("🤔 Думаю, как смонтировать…")
    try:
        outcome = await video.plan(
            text, clips, current, ws=video.workspace(_user_id(message)), music=_music(data),
            history=list(data.get("history", [])), progress=_progress(bot, status),
        )
    except AIError as exc:
        error: str | None = ai_error_text(exc)
    except PlanError:
        logger.warning("Video plan failed: model output unusable", exc_info=True)
        error = "😕 Не поняла, что сделать. Скажи иначе — например, «обрежь с 0:05 до 0:30» или «ускорь в 2 раза»."
    except BaseException:
        if await _alive(state, session_id):
            await state.set_state(VideoStates.session)
        raise
    else:
        error = None
    if not await _alive(state, session_id):
        return
    if error is not None:
        await state.set_state(VideoStates.session)
        await _set_status(bot, status, error)
        return

    await state.update_data(history=[*data.get("history", []), text][-MAX_HISTORY:])
    if not outcome.changed and (current is not None or outcome.notes):
        await state.set_state(VideoStates.session)
        reply = "\n".join(outcome.notes) or "Кажется, тут нечего менять 🙂 Скажи, что поправить."
        await _set_status(bot, status, f"{reply}\n\n{EXIT_HINT}")
        return
    await _render_and_send(message, state, bot, video, outcome.plan, status, outcome.notes,
                           user_id=_user_id(message))


# ---------- Монтаж и отправка ----------


def _caption(result_lines: list[str], header: str, notes: list[str], *, hint: bool) -> str:
    parts = [header]
    if result_lines:
        parts.append("\n".join(escape(line) for line in result_lines))
    if notes:
        parts.append("\n".join(escape(note) for note in notes))
    if hint:
        parts.append(EDIT_HINT)
    text = "\n\n".join(parts)
    if len(text) > CAPTION_LIMIT:
        text = "\n\n".join(parts[:2])[: CAPTION_LIMIT - 1] + "…"
    return text


async def _send(message: Message, kind: str, media: FSInputFile | str, caption: str,
                keyboard: InlineKeyboardMarkup, *, duration: float, width: int, height: int) -> tuple[Message, str]:
    """Отправляет результат; возвращает сообщение с кнопками и file_id для быстрого повтора."""
    seconds = max(1, round(duration))
    if kind == "video_note":  # у кружка нет подписи — кнопки идут отдельным сообщением
        sent = await message.answer_video_note(media, duration=seconds, length=width)
        panel = await message.answer(caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return panel, sent.video_note.file_id if sent.video_note else ""
    if kind == "animation":
        sent = await message.answer_animation(media, duration=seconds, width=width, height=height,
                                              caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return sent, sent.animation.file_id if sent.animation else ""
    if kind == "audio":
        sent = await message.answer_audio(media, duration=seconds, title="Звук из видео",
                                          caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return sent, sent.audio.file_id if sent.audio else ""
    sent = await message.answer_video(media, duration=seconds, width=width, height=height, supports_streaming=True,
                                      caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return sent, sent.video.file_id if sent.video else ""


async def _render_and_send(
    message: Message,
    state: FSMContext,
    bot: Bot,
    video: VideoService,
    plan: EditPlan,
    status: Message,
    notes: list[str],
    *,
    user_id: int,
    from_undo: bool = False,
) -> None:
    """message — любое сообщение в чате (ответ уходит туда же); user_id — чей монтаж."""
    data = await state.get_data()
    session_id = data["session_id"]
    previous = data.get("plan")
    rev = data.get("rev", 0) + 1
    undo = list(data.get("undo", []))
    if previous is not None and not from_undo:
        undo = (undo + [previous])[-MAX_UNDO:]
    keyboard = _keyboard(video, {**data, "plan": plan.to_dict()}, plan, rev, can_undo=bool(undo))
    sent_cache: dict[str, Any] = dict(data.get("sent", {}))
    key = _key(plan)
    await state.set_state(VideoStates.working)
    started = time.monotonic()

    result: RenderResult | None = None
    try:
        if (cached := sent_cache.get(key)) is not None:
            # Такой вариант уже отправляли (например, «Отменить»): повторяем мгновенно, без монтажа.
            panel, file_id = await _send(
                message, cached["kind"], cached["file_id"], cached["caption"], keyboard,
                duration=cached["duration"], width=cached["width"], height=cached["height"],
            )
            entry = cached
        else:
            async with ChatActionSender.upload_video(bot=bot, chat_id=message.chat.id):
                result = await video.render(
                    video.workspace(user_id), plan, _clips(data), music=_music(data),
                    progress=_progress(bot, status),
                )
            if not await _alive(state, session_id):
                return
            header = f"🎬 <b>Готово</b> · {fmt_time(result.duration)}"
            if result.kind != "audio":
                header += f" · {result.width}×{result.height}"
            caption = _caption(result.lines, header, notes + result.notes, hint=previous is None)
            panel, file_id = await _send(
                message, result.kind, FSInputFile(result.path, filename=f"elya{result.path.suffix}"),
                caption, keyboard, duration=result.duration, width=result.width, height=result.height,
            )
            entry = {"kind": result.kind, "file_id": file_id, "duration": result.duration,
                     "width": result.width, "height": result.height,
                     "caption": _caption(result.lines, header, result.notes, hint=False)}
    except VideoError as exc:
        error: str | None = str(exc)
    except FFmpegError:
        logger.warning("Video render failed", exc_info=True)
        error = "😕 Не получилось смонтировать: ffmpeg споткнулся на этом видео. Попробуй другие настройки."
    except TelegramAPIError:
        logger.warning("Cannot send the video result", exc_info=True)
        error = "😕 Смонтировала, но Telegram не принял файл. Попробуй ещё раз."
    except BaseException:
        if await _alive(state, session_id):
            await state.set_state(VideoStates.session)
        raise
    else:
        error = None
    finally:
        if result is not None:
            result.path.unlink(missing_ok=True)

    if not await _alive(state, session_id):
        return
    await state.set_state(VideoStates.session)
    if error is not None:
        await _set_status(bot, status, error)
        return

    if file_id:
        sent_cache[key] = entry
        sent_cache = dict(list(sent_cache.items())[-MAX_SENT:])
    old_panel = data.get("panel_id")
    await state.update_data(plan=plan.to_dict(), undo=undo, rev=rev, panel_id=panel.message_id, sent=sent_cache)
    await _drop_keyboard(bot, message.chat.id, old_panel)
    done = ("✅ Готово — этот вариант уже был, прислала его снова." if result is None
            else f"✅ Смонтировала за {max(1, round(time.monotonic() - started))} с.")
    await _set_status(bot, status, done)


# ---------- Кнопки ----------


def _quick(value: str, base: EditPlan, clips: list[ClipInfo]) -> EditPlan | None:
    if value == "join":
        return default_plan(clips)
    if value == "pauses":
        return base.but(remove_pauses=not base.remove_pauses)
    if value == "subs":
        return base.but(subtitles=not base.subtitles)
    if value == "vertical":
        vertical = base.format != "9:16"
        return base.but(format="9:16" if vertical else "original",
                        output="video" if base.output == "circle" else base.output)
    if value == "circle":
        circle = base.output != "circle"
        return base.but(output="circle" if circle else "video", format="1:1" if circle else "original")
    if value == "music":
        return base.but(music=not base.music)
    return None


async def on_video_action(callback: CallbackQuery, callback_data: VideoCallback, state: FSMContext,
                          bot: Bot, video: VideoService | None = None) -> None:
    message = callback.message if isinstance(callback.message, Message) else None
    current = await state.get_state()
    data = await state.get_data()
    if current == VideoStates.working.state:
        await callback.answer(WORKING_TEXT, show_alert=True)
        return
    if current != VideoStates.session.state or message is None or callback_data.rev != data.get("rev"):
        await callback.answer(OUTDATED_TEXT)
        return

    if callback_data.action == "done":
        await _finish(state, video, callback.from_user.id)
        await _drop_keyboard(bot, message.chat.id, message.message_id)
        await callback.answer()
        await message.answer("✅ Монтаж закончен, файлы удалила. Захочешь ещё — просто пришли видео 🎬")
        return
    if video is None or not video.ready:
        await callback.answer("Монтаж сейчас недоступен.", show_alert=True)
        return

    clips = _clips(data)
    if not clips:
        await callback.answer("Сначала пришли видео 🎬", show_alert=True)
        return
    if callback_data.action == "undo":
        undo = list(data.get("undo", []))
        if not undo:
            await callback.answer("Отменять нечего.")
            return
        previous = EditPlan.from_dict(undo.pop())
        await state.update_data(undo=undo)
        await callback.answer()
        status = await message.answer("↩️ Возвращаю предыдущий вариант…")
        await _render_and_send(message, state, bot, video, previous, status, [],
                               user_id=callback.from_user.id, from_undo=True)
        return

    if callback_data.value == "subs" and not video.can_subtitles:
        await callback.answer("Субтитры сейчас недоступны.", show_alert=True)
        return
    plan = _quick(callback_data.value, _plan(data) or default_plan(clips), clips)
    if plan is None:
        await callback.answer(OUTDATED_TEXT)
        return
    notes: list[str] = []
    plan = apply_limits(plan, video.context(clips, has_music=bool(data.get("music"))), notes)
    await callback.answer()
    status = await message.answer("⏳ Монтирую…")
    await _render_and_send(message, state, bot, video, plan, status, notes, user_id=callback.from_user.id)


# ---------- Прочее ----------


async def cmd_cancel(message: Message, state: FSMContext, bot: Bot, video: VideoService | None = None) -> None:
    data = await state.get_data()
    await _finish(state, video, _user_id(message))
    await _drop_keyboard(bot, message.chat.id, data.get("panel_id"))
    await message.answer("Монтаж отменён, файлы удалила.")


async def on_busy(message: Message) -> None:
    await message.answer(WORKING_TEXT)


async def on_unsupported(message: Message) -> None:
    await message.answer(
        "В монтаже я принимаю видео, музыку (аудиофайлом) и просьбы — текстом или голосовым. "
        + EXIT_HINT
    )


def create_router() -> Router:
    router = Router(name="video")
    router.message.register(cmd_video, Command("video"))
    router.message.register(cmd_cancel, Command("cancel"), StateFilter(VideoStates.session, VideoStates.working))
    router.callback_query.register(on_video_action, VideoCallback.filter())

    not_command = ~F.text.startswith("/")
    clip = F.video | F.video_note | F.document.mime_type.startswith("video/")
    media = clip | F.animation | F.audio | F.document.mime_type.startswith("audio/")
    router.message.register(on_busy, VideoStates.working, not_command)
    router.message.register(on_media, StateFilter(None), clip)  # видео прямо в чат — сразу монтаж
    router.message.register(on_media, VideoStates.session, media)
    router.message.register(on_voice, VideoStates.session, F.voice)
    router.message.register(on_text, VideoStates.session, F.text, not_command)
    router.message.register(on_unsupported, VideoStates.session, not_command)
    return router

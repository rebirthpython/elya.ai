"""Монтаж видео: /video или просто видео в чат — и то же самое в Mini App.

Клипы (можно альбомом) и музыка -> просьба словами, голосом или кнопкой -> готовое видео ->
правки следующими сообщениями («а теперь ускорь»), «Отменить» возвращает прошлый вариант,
«Готово» заканчивает монтаж и стирает файлы.

Сессия монтажа хранится в FSM aiogram и общая для чата с ботом и приложения: клип можно
прислать в чат, а просьбу написать в приложении. Ядро (add_media, run_request, run_action,
render_and_send) не зависит от того, откуда пришла просьба; результат всегда уходит в чат
с ботом — там он и сохраняется.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
from video.service import NO_FFMPEG_TEXT, PlanError, RenderResult, VideoError, VideoService, Workspace

logger = logging.getLogger(__name__)

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # больше бот скачать не может (ограничение Bot API)
STATUS_DELAY = 1.5  # ждём, не придут ли ещё клипы альбома, прежде чем показать кнопки
PROGRESS_INTERVAL = 2.0
MAX_UNDO = 10
MAX_SENT = 12
MAX_HISTORY = 10
CAPTION_LIMIT = 1024

Progress = Callable[[str], Awaitable[None]]

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
NO_CLIPS_TEXT = "Сначала пришли видео 🎬 — потом скажешь, что с ним сделать. Выйти — /cancel."
DONE_TEXT = "✅ Монтаж закончен, файлы удалила. Захочешь ещё — просто пришли видео 🎬"
DOWNLOAD_FAILED = "Не получилось скачать файл из Telegram. Попробуй прислать его ещё раз."
TOO_BIG = ("Это видео больше 20 МБ — Telegram не даёт ботам скачивать такие файлы. Пришли его как "
           "обычное видео, а не файлом (Telegram сам его сожмёт), или раздели на части.")


@dataclass
class Outcome:
    """Итог просьбы или кнопки — одинаковый для чата с ботом и приложения."""

    text: str = ""  # ответ без монтажа (пояснение) или текст ошибки; без HTML
    error: bool = False
    rendered: bool = False  # результат отправлен в чат с ботом
    reused: bool = False  # тот же вариант уже был — отправлен повторно без монтажа
    cancelled: bool = False  # монтаж закончили, пока шла работа
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    kind: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    result_file: str | None = None  # файл результата в рабочей папке (для просмотра в приложении)
    seconds: int = 0


# ---------- Данные сессии ----------


def clips_of(data: dict[str, Any]) -> list[ClipInfo]:
    return [ClipInfo.from_dict(c) for c in data.get("clips", [])]


def music_of(data: dict[str, Any]) -> ClipInfo | None:
    return ClipInfo.from_dict(data["music"]) if data.get("music") else None


def plan_of(data: dict[str, Any]) -> EditPlan | None:
    return EditPlan.from_dict(data["plan"]) if data.get("plan") else None


def _key(plan: EditPlan) -> str:
    return hashlib.sha1(plan.key.encode()).hexdigest()[:16]


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else message.chat.id


def user_lock(user_id: int) -> asyncio.Lock:
    return _user_locks[user_id]


async def _alive(state: FSMContext, session_id: str) -> bool:
    """Монтаж не отменили (/cancel, «Готово», /start), пока шла долгая работа."""
    return (await state.get_data()).get("session_id") == session_id


async def begin_session(state: FSMContext, video: VideoService, user_id: int, *, intro: bool) -> None:
    video.workspace(user_id).clear()
    await state.clear()
    await state.set_state(VideoStates.session)
    await state.update_data(
        session_id=uuid.uuid4().hex, clips=[], music=None, plan=None, undo=[], rev=0,
        panel_id=None, sent={}, history=[], pending=[], intro=intro,
    )


async def finish_session(bot: Bot, state: FSMContext, video: VideoService | None, user_id: int,
                         chat_id: int) -> None:
    data = await state.get_data()
    await state.clear()
    if video is not None:
        video.workspace(user_id).remove()
    await _drop_keyboard(bot, chat_id, data.get("panel_id"))


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


def actions_for(video: VideoService, data: dict[str, Any]) -> list[dict[str, str]]:
    """Те же кнопки, что под результатом в чате, — данными для приложения (без «Готово»)."""
    keyboard = _keyboard(video, data, plan_of(data), data.get("rev", 0), can_undo=bool(data.get("undo")))
    actions = []
    for row in keyboard.inline_keyboard:
        for button in row:
            callback = VideoCallback.unpack(button.callback_data)
            if callback.action != "done":
                actions.append({"action": callback.action, "value": callback.value, "label": button.text})
    return actions


def panel_text(data: dict[str, Any], *, html: bool = True) -> str:
    """Что уже прислано и что можно сделать. html=False — Markdown для приложения."""
    def em(text: str) -> str:
        return f"<i>{text}</i>" if html else f"_{text}_"

    def esc(text: str) -> str:
        return escape(text) if html else text

    clips = clips_of(data)
    lines = []
    if not data.get("intro") and data.get("plan") is None:
        lines.append("🎬 Давай смонтирую!\n")
    if clips:
        lines.append("📥 <b>Клипы</b>" if html else "📥 **Клипы**")
        for number, clip in enumerate(clips, start=1):
            sound = "" if clip.has_audio else ", без звука"
            lines.append(f"{number}) {fmt_time(clip.duration)}, {clip.orientation}{sound}")
    else:
        lines.append("Пока нет ни одного видео — пришли его сюда 🎬")
    if music := data.get("music"):
        lines.append(f"🎵 Музыка: «{esc(music['title'])}» ({fmt_time(music['duration'])})")
    lines.append("")
    if data.get("plan") is None:
        lines.append("Нажми кнопку или напиши своими словами — например, " + em("«обрежь первые 5 секунд»")
                     + " или " + em("«сделай вертикально и добавь субтитры»") + ".")
    else:
        lines.append("Напиши, что сделать с новым — например, " + em(f"«добавь клип {max(1, len(clips))} в конец»")
                     + " или " + em("«наложи музыку потише»") + ".")
    return "\n".join(lines)


def _progress(bot: Bot, status: Message, also: Progress | None = None) -> Progress:
    last = {"time": 0.0, "text": ""}

    async def update(text: str) -> None:
        if also is not None:
            await also(text)
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


# ---------- Ядро: клипы, просьбы, кнопки, монтаж ----------


async def add_media(state: FSMContext, video: VideoService, ws: Workspace, name: str, kind: str, *,
                    title: str | None = None, order: int | None = None, caption: str = "") -> str | None:
    """Файл уже лежит в рабочей папке: проверяем и добавляем в сессию. Возвращает текст ошибки.

    Вызывать под user_lock: части альбома приходят одновременно.
    """
    data = await state.get_data()
    clips = list(data.get("clips", []))
    try:
        info = await video.probe(ws.path(name))
    except VideoError as exc:
        ws.path(name).unlink(missing_ok=True)
        return str(exc)
    if kind == "clip" and not info.has_video:
        kind = "music"  # «видео» без картинки — это звук
    if kind == "music" and not info.has_audio:
        ws.path(name).unlink(missing_ok=True)
        return "В этом файле нет звука — для музыки он не подойдёт."
    if kind == "clip" and info.duration < 0.3:
        ws.path(name).unlink(missing_ok=True)
        return "Это видео слишком короткое для монтажа."
    if kind == "clip" and len(clips) >= video.max_clips:
        ws.path(name).unlink(missing_ok=True)
        return f"Можно до {video.max_clips} клипов за один монтаж."

    updates: dict[str, Any] = {}
    if kind == "clip":
        order = order if order is not None else max((c.get("msg", 0) for c in clips), default=0) + 1
        clips.append({**info.to_dict(), "msg": order})
        clips.sort(key=lambda c: c["msg"])  # клипы альбома — в том порядке, в каком их отправили
        updates["clips"] = clips
    else:
        if (old := data.get("music")) and old["file"] != name:
            ws.path(old["file"]).unlink(missing_ok=True)
        updates["music"] = {**info.to_dict(), "title": str(title or "трек")[:60]}
    if caption:
        updates["pending"] = [*data.get("pending", []), caption]
    await state.update_data(**updates)
    return None


async def run_request(bot: Bot, state: FSMContext, video: VideoService, text: str, *, user_id: int,
                      chat_id: int, progress: Progress | None = None) -> Outcome:
    """Просьба словами: план от модели -> монтаж -> результат в чат с ботом."""
    data = await state.get_data()
    clips = clips_of(data)
    if not clips:
        return Outcome(text=NO_CLIPS_TEXT, error=True)
    session_id = data["session_id"]
    current = plan_of(data)
    await state.set_state(VideoStates.working)
    try:
        outcome = await video.plan(
            text, clips, current, ws=video.workspace(user_id), music=music_of(data),
            history=list(data.get("history", [])), progress=progress,
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
        return Outcome(cancelled=True)
    if error is not None:
        await state.set_state(VideoStates.session)
        return Outcome(text=error, error=True)

    await state.update_data(history=[*data.get("history", []), text][-MAX_HISTORY:])
    if not outcome.changed and (current is not None or outcome.notes):
        await state.set_state(VideoStates.session)
        reply = "\n".join(outcome.notes) or "Кажется, тут нечего менять 🙂 Скажи, что поправить."
        return Outcome(text=f"{reply}\n\n{EXIT_HINT}")
    return await render_and_send(bot, state, video, outcome.plan, outcome.notes, user_id=user_id,
                                 chat_id=chat_id, progress=progress)


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


def check_action(video: VideoService, data: dict[str, Any], action: str, value: str) -> str | None:
    """Можно ли выполнить кнопку сейчас; иначе — короткое пояснение."""
    if not clips_of(data):
        return "Сначала пришли видео 🎬"
    if action == "undo":
        return None if data.get("undo") else "Отменять нечего."
    if action != "quick" or _quick(value, default_plan(clips_of(data)), clips_of(data)) is None:
        return OUTDATED_TEXT
    if value == "subs" and not video.can_subtitles:
        return "Субтитры сейчас недоступны."
    return None


async def run_action(bot: Bot, state: FSMContext, video: VideoService, action: str, value: str, *,
                     user_id: int, chat_id: int, progress: Progress | None = None) -> Outcome:
    """Кнопка: частое действие без модели (quick) или возврат прошлого варианта (undo)."""
    data = await state.get_data()
    if (problem := check_action(video, data, action, value)) is not None:
        return Outcome(text=problem, error=True)
    clips = clips_of(data)
    if action == "undo":
        undo = list(data.get("undo", []))
        previous = EditPlan.from_dict(undo.pop())
        await state.update_data(undo=undo)
        return await render_and_send(bot, state, video, previous, [], user_id=user_id, chat_id=chat_id,
                                     progress=progress, from_undo=True)
    plan = _quick(value, plan_of(data) or default_plan(clips), clips)
    notes: list[str] = []
    plan = apply_limits(plan, video.context(clips, has_music=bool(data.get("music"))), notes)
    return await render_and_send(bot, state, video, plan, notes, user_id=user_id, chat_id=chat_id,
                                 progress=progress)


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


async def _send(bot: Bot, chat_id: int, kind: str, media: FSInputFile | str, caption: str,
                keyboard: InlineKeyboardMarkup, *, duration: float, width: int, height: int) -> tuple[Message, str]:
    """Отправляет результат; возвращает сообщение с кнопками и file_id для быстрого повтора."""
    seconds = max(1, round(duration))
    html = ParseMode.HTML
    if kind == "video_note":  # у кружка нет подписи — кнопки идут отдельным сообщением
        sent = await bot.send_video_note(chat_id, media, duration=seconds, length=width)
        panel = await bot.send_message(chat_id, caption, parse_mode=html, reply_markup=keyboard)
        return panel, sent.video_note.file_id if sent.video_note else ""
    if kind == "animation":
        sent = await bot.send_animation(chat_id, media, duration=seconds, width=width, height=height,
                                        caption=caption, parse_mode=html, reply_markup=keyboard)
        return sent, sent.animation.file_id if sent.animation else ""
    if kind == "audio":
        sent = await bot.send_audio(chat_id, media, duration=seconds, title="Звук из видео",
                                    caption=caption, parse_mode=html, reply_markup=keyboard)
        return sent, sent.audio.file_id if sent.audio else ""
    sent = await bot.send_video(chat_id, media, duration=seconds, width=width, height=height,
                                supports_streaming=True, caption=caption, parse_mode=html, reply_markup=keyboard)
    return sent, sent.video.file_id if sent.video else ""


async def render_and_send(
    bot: Bot,
    state: FSMContext,
    video: VideoService,
    plan: EditPlan,
    notes: list[str],
    *,
    user_id: int,
    chat_id: int,
    progress: Progress | None = None,
    from_undo: bool = False,
) -> Outcome:
    """Монтирует по плану и отправляет результат в чат с ботом (с кнопками правок)."""
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
    outcome = Outcome(rendered=True, notes=list(notes))
    try:
        if (cached := sent_cache.get(key)) is not None:
            # Такой вариант уже отправляли (например, «Отменить»): повторяем мгновенно, без монтажа.
            panel, file_id = await _send(
                bot, chat_id, cached["kind"], cached["file_id"], cached["caption"], keyboard,
                duration=cached["duration"], width=cached["width"], height=cached["height"],
            )
            entry = cached
            outcome.reused = True
            outcome.lines = list(cached.get("lines", []))
            outcome.kind, outcome.duration = cached["kind"], cached["duration"]
            outcome.width, outcome.height = cached["width"], cached["height"]
        else:
            async with ChatActionSender.upload_video(bot=bot, chat_id=chat_id):
                result = await video.render(
                    video.workspace(user_id), plan, clips_of(data), music=music_of(data), progress=progress,
                )
            if not await _alive(state, session_id):
                return Outcome(cancelled=True)
            header = f"🎬 <b>Готово</b> · {fmt_time(result.duration)}"
            if result.kind != "audio":
                header += f" · {result.width}×{result.height}"
            caption = _caption(result.lines, header, notes + result.notes, hint=previous is None)
            panel, file_id = await _send(
                bot, chat_id, result.kind, FSInputFile(result.path, filename=f"elya{result.path.suffix}"),
                caption, keyboard, duration=result.duration, width=result.width, height=result.height,
            )
            entry = {"kind": result.kind, "file_id": file_id, "duration": result.duration,
                     "width": result.width, "height": result.height, "lines": result.lines,
                     "caption": _caption(result.lines, header, result.notes, hint=False)}
            outcome.lines, outcome.kind, outcome.duration = result.lines, result.kind, result.duration
            outcome.width, outcome.height = result.width, result.height
            outcome.notes += result.notes
            outcome.result_file = result.path.name  # остаётся до следующего монтажа — для приложения
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

    if not await _alive(state, session_id):
        return Outcome(cancelled=True)
    await state.set_state(VideoStates.session)
    if error is not None:
        return Outcome(text=error, error=True)

    if file_id:
        sent_cache[key] = entry
        sent_cache = dict(list(sent_cache.items())[-MAX_SENT:])
    old_panel = data.get("panel_id")
    await state.update_data(plan=plan.to_dict(), undo=undo, rev=rev, panel_id=panel.message_id, sent=sent_cache)
    await _drop_keyboard(bot, chat_id, old_panel)
    outcome.seconds = max(1, round(time.monotonic() - started))
    return outcome


def outcome_status(outcome: Outcome) -> str:
    """Чем закончилось — одной строкой для статуса в чате."""
    if not outcome.rendered:
        return outcome.text
    if outcome.reused:
        return "✅ Готово — этот вариант уже был, прислала его снова."
    return f"✅ Смонтировала за {outcome.seconds} с."


# ---------- Чат с ботом: начало и приём клипов ----------


async def cmd_video(message: Message, state: FSMContext, video: VideoService | None = None) -> None:
    if video is None or not video.ready:
        await message.answer(video.unavailable_text if video else NO_FFMPEG_TEXT, parse_mode=ParseMode.HTML)
        return
    await begin_session(state, video, _user_id(message), intro=True)
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
        await message.answer(video.unavailable_text if video else NO_FFMPEG_TEXT, parse_mode=ParseMode.HTML)
        return
    kind, media, ext = found
    user_id = _user_id(message)
    if (getattr(media, "file_size", None) or 0) > TELEGRAM_DOWNLOAD_LIMIT:
        await message.answer(TOO_BIG)
        return

    async with user_lock(user_id):
        # Клипы альбома, присланного прямо в чат, приходят разом: сессию начинает только первый.
        began = await state.get_state() is None
        if began:
            await begin_session(state, video, user_id, intro=False)
        elif await state.get_state() != VideoStates.session.state:
            await message.answer(WORKING_TEXT)
            return
        data = await state.get_data()
        if kind == "clip" and len(data.get("clips", [])) >= video.max_clips:
            if not data.get("limit_notified"):
                await state.update_data(limit_notified=True)
                await message.answer(f"Можно до {video.max_clips} клипов за один монтаж — остальные пропускаю.")
            return
        ws = video.workspace(user_id).ensure()
        name = f"{kind}-{message.message_id}{ext}"

        async def fail(text: str) -> None:
            ws.path(name).unlink(missing_ok=True)
            if began:  # видео в чат не открылось — не держим пустой монтаж, дальше обычный чат
                await finish_session(bot, state, video, user_id, message.chat.id)
            await message.answer(text)

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
        title = getattr(media, "title", None) or getattr(media, "file_name", None)
        error = await add_media(state, video, ws, name, kind, title=title, order=message.message_id,
                                caption=(message.caption or "").strip())
        if error is not None:
            await fail(error)
            return

    # Альбом приходит пачкой сообщений: кнопки показываем один раз, когда клипы перестали приходить.
    await asyncio.sleep(STATUS_DELAY)
    async with user_lock(user_id):
        if _collect_seq[user_id] != seq or await state.get_state() != VideoStates.session.state:
            return
        pending = (await state.get_data()).get("pending", [])
        await state.update_data(pending=[])
    if pending:
        await _instruct(message, state, bot, video, " ".join(pending))
    else:
        await _show_panel(message, state, bot, video)


async def _show_panel(message: Message, state: FSMContext, bot: Bot, video: VideoService) -> None:
    data = await state.get_data()
    rev = data.get("rev", 0)
    await _drop_keyboard(bot, message.chat.id, data.get("panel_id"))
    panel = await message.answer(
        panel_text(data), parse_mode=ParseMode.HTML,
        reply_markup=_keyboard(video, data, plan_of(data), rev, can_undo=bool(data.get("undo"))),
    )
    await state.update_data(panel_id=panel.message_id)


# ---------- Чат с ботом: просьбы словами и кнопки ----------


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
    if not clips_of(await state.get_data()):
        await message.answer(NO_CLIPS_TEXT)
        return
    status = await message.answer("🤔 Думаю, как смонтировать…")
    outcome = await run_request(bot, state, video, text, user_id=_user_id(message), chat_id=message.chat.id,
                                progress=_progress(bot, status))
    if not outcome.cancelled:
        await _set_status(bot, status, outcome_status(outcome))


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

    user_id = callback.from_user.id
    if callback_data.action == "done":
        await finish_session(bot, state, video, user_id, message.chat.id)
        await _drop_keyboard(bot, message.chat.id, message.message_id)
        await callback.answer()
        await message.answer(DONE_TEXT)
        return
    if video is None or not video.ready:
        await callback.answer("Монтаж сейчас недоступен.", show_alert=True)
        return
    if (problem := check_action(video, data, callback_data.action, callback_data.value)) is not None:
        await callback.answer(problem, show_alert=problem != OUTDATED_TEXT)
        return
    await callback.answer()
    first = "↩️ Возвращаю предыдущий вариант…" if callback_data.action == "undo" else "⏳ Монтирую…"
    status = await message.answer(first)
    outcome = await run_action(bot, state, video, callback_data.action, callback_data.value, user_id=user_id,
                               chat_id=message.chat.id, progress=_progress(bot, status))
    if not outcome.cancelled:
        await _set_status(bot, status, outcome_status(outcome))


# ---------- Прочее ----------


async def cmd_cancel(message: Message, state: FSMContext, bot: Bot, video: VideoService | None = None) -> None:
    await finish_session(bot, state, video, _user_id(message), message.chat.id)
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

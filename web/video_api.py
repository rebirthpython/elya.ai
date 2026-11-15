"""Монтаж видео в Mini App: та же сессия, что в чате с ботом (FSM aiogram), и то же ядро.

Видео загружается из приложения напрямую на сервер, поэтому лимит Telegram в 20 МБ на
скачивание ботом здесь не действует. Ход монтажа приходит в приложение потоком (NDJSON),
а готовое видео бот отправляет в чат — там оно сохраняется, а в приложении его можно
посмотреть сразу.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.utils.web_app import WebAppUser
from aiohttp import web

from bot.handlers.video import (
    DONE_TEXT,
    NO_CLIPS_TEXT,
    WORKING_TEXT,
    Outcome,
    VideoStates,
    actions_for,
    add_media,
    begin_session,
    check_action,
    clips_of,
    finish_session,
    panel_text,
    plan_of,
    run_action,
    run_request,
    user_lock,
)
from video.plan import fmt_time
from video.service import VideoService
from web.common import BOT, SETTINGS, _USER_KEY, _apply_headers, _error, _rate_limited, _read_json

logger = logging.getLogger(__name__)

VIDEO = web.AppKey("video", object)
FSM_STORAGE = web.AppKey("fsm_storage", object)

PING_INTERVAL = 15.0  # прокси (Cloudflare) рвут ответ, если он долго молчит
MAX_REQUEST_CHARS = 2000
_RESULT_RE = re.compile(r"^result-\d+\.(mp4|m4a)$")
_EXTENSIONS = {
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "video/3gpp": ".3gp", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/aac": ".aac", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac",
}

APP_INTRO = (
    "🎬 **Монтаж видео**\n\n"
    "Прикрепи видео скрепкой 📎 (можно по одному несколько) и напиши, что сделать:\n"
    "- _обрежь первые 5 секунд и ускорь в 1,5 раза_\n"
    "- _убери паузы и добавь субтитры_\n"
    "- _сделай вертикально для Reels и подпиши «Лето 2026»_\n"
    "- _склей, наложи музыку потише, в конце затемнение_\n\n"
    "Музыку прикрепляй аудиофайлом. Готовое видео пришлю сюда и в чат с ботом — там оно сохранится."
)


def available(app: web.Application) -> bool:
    video: VideoService | None = app.get(VIDEO)
    return video is not None and video.ready and app.get(BOT) is not None and app.get(FSM_STORAGE) is not None


def _context(app: web.Application, user_id: int) -> FSMContext:
    bot: Bot = app[BOT]
    storage: BaseStorage = app[FSM_STORAGE]
    # Тот же ключ, что у диспетчера для личного чата: чат с ботом и приложение делят сессию.
    return FSMContext(storage=storage, key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id))


def _upload_limit(app: web.Application) -> int:
    return app[SETTINGS].video_upload_max_mb * 1024 * 1024


async def _session(app: web.Application, state: FSMContext) -> dict[str, Any] | None:
    current = await state.get_state()
    if current not in (VideoStates.session.state, VideoStates.working.state):
        return None
    data = await state.get_data()
    music = data.get("music")
    return {
        "clips": [
            {"duration": round(c.duration, 1), "label": f"{fmt_time(c.duration)}, {c.orientation}"
                                                        + ("" if c.has_audio else ", без звука")}
            for c in clips_of(data)
        ],
        "music": {"title": music["title"], "duration": round(music["duration"], 1)} if music else None,
        "has_result": plan_of(data) is not None,
        "busy": current == VideoStates.working.state,
        "actions": actions_for(app[VIDEO], data),
    }


async def state_payload(app: web.Application, user_id: int) -> dict[str, Any]:
    """Часть /api/state: доступен ли монтаж и идёт ли он сейчас."""
    if not available(app):
        return {"available": False}
    return {
        "available": True,
        "upload_limit_mb": app[SETTINGS].video_upload_max_mb,
        "session": await _session(app, _context(app, user_id)),
    }


def _unavailable() -> web.Response:
    return _error(503, "video_off", "Монтаж видео сейчас недоступен: на сервере не установлен ffmpeg.")


async def post_start(request: web.Request) -> web.Response:
    """/video в приложении: начинает монтаж (или показывает уже начатый)."""
    app = request.app
    if not available(app):
        return _unavailable()
    user: WebAppUser = request[_USER_KEY]
    state = _context(app, user.id)
    async with user_lock(user.id):
        if await state.get_state() is None:
            await begin_session(state, app[VIDEO], user.id, intro=True)
            message = APP_INTRO
        else:
            message = panel_text(await state.get_data(), html=False)
    return web.json_response({"message": message, "session": await _session(app, state)})


async def post_upload(request: web.Request) -> web.Response:
    """multipart: file — видео или музыка. Добавляет его в монтаж."""
    app = request.app
    if not available(app):
        return _unavailable()
    if not request.content_type.startswith("multipart/"):
        return _error(400, "bad_request", "Некорректный запрос.")
    user: WebAppUser = request[_USER_KEY]
    video: VideoService = app[VIDEO]
    state = _context(app, user.id)
    limit = _upload_limit(app)
    try:
        reader = await request.multipart()
        part = await reader.next()
    except (ValueError, AssertionError):
        return _error(400, "bad_request", "Некорректный запрос.")
    if part is None or part.name != "file":
        return _error(400, "bad_request", "Нет файла.")
    content_type = (part.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type.startswith("video/"):
        kind = "clip"
    elif content_type.startswith("audio/"):
        kind = "music"
    else:
        return _error(415, "bad_media", "Это не видео и не музыка.")
    filename = unquote(part.filename or "")  # имя с кириллицей иногда приходит в %-кодировке
    suffix = Path(filename).suffix.lower()
    suffix = suffix if suffix[1:].isalnum() and len(suffix) <= 6 else _EXTENSIONS.get(content_type, "")
    title = Path(filename).name[:60] or None

    async with user_lock(user.id):
        began = await state.get_state() is None
        if began:
            await begin_session(state, video, user.id, intro=True)
        elif await state.get_state() != VideoStates.session.state:
            return _error(409, "busy", WORKING_TEXT)
        ws = video.workspace(user.id).ensure()
        name = f"{kind}-app-{uuid.uuid4().hex[:8]}{suffix or '.bin'}"

        async def fail(status: int, code: str, message: str) -> web.Response:
            ws.path(name).unlink(missing_ok=True)
            if began and not clips_of(await state.get_data()):  # не держим пустой монтаж
                await finish_session(app[BOT], state, video, user.id, user.id)
            return _error(status, code, message)

        size = 0
        try:
            with ws.path(name).open("wb") as target:
                while chunk := await part.read_chunk(256 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise OverflowError
                    target.write(chunk)
        except OverflowError:
            return await fail(413, "too_large", f"Файл больше {app[SETTINGS].video_upload_max_mb} МБ.")
        except (OSError, ValueError, asyncio.CancelledError):
            await fail(500, "internal", "")
            raise
        if size == 0:
            return await fail(400, "bad_request", "Пустой файл.")
        if (error := await add_media(state, video, ws, name, kind, title=title)) is not None:
            return await fail(400, "bad_media", error)
        message = panel_text(await state.get_data(), html=False)
    return web.json_response({"message": message, "session": await _session(app, state)})


def _outcome_text(outcome: Outcome) -> str:
    if not outcome.rendered:
        return outcome.text
    header = "🎬 **Готово**"
    if outcome.reused:
        header += " — этот вариант уже был"
    if outcome.duration:
        header += f" · {fmt_time(outcome.duration)}"
    if outcome.kind != "audio" and outcome.width:
        header += f" · {outcome.width}×{outcome.height}"
    parts = [header]
    if outcome.lines:
        parts.append("\n".join(outcome.lines))
    if outcome.notes:
        parts.append("\n".join(outcome.notes))
    parts.append("Файл — в чате с ботом. Что поправить? Напиши, например, «ускорь в 1,5 раза».")
    return "\n\n".join(parts)


async def _stream(request: web.Request, job: Callable[[Callable[[str], Awaitable[None]]], Awaitable[Outcome]],
                  user_id: int) -> web.StreamResponse:
    """Выполняет монтаж, присылая ход работы построчно: progress … ping … done."""
    app = request.app
    response = web.StreamResponse(
        headers={"Content-Type": "application/x-ndjson; charset=utf-8", "X-Accel-Buffering": "no"}
    )
    _apply_headers(request, response.headers)
    await response.prepare(request)
    gone = False
    write_lock = asyncio.Lock()

    async def emit(item: dict[str, Any]) -> None:
        nonlocal gone
        if gone:
            return
        async with write_lock:
            try:
                await response.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
            except (ConnectionError, RuntimeError):
                gone = True  # приложение закрыли: монтаж доделается, видео придёт в чат

    async def progress(text: str) -> None:
        await emit({"type": "progress", "text": text})

    async def pinger() -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await emit({"type": "ping"})

    ping_task = asyncio.create_task(pinger())
    try:
        outcome = await job(progress)
    finally:
        ping_task.cancel()
    state = _context(app, user_id)
    poster = None
    if outcome.result_file and outcome.kind != "audio":
        image = await app[VIDEO].poster(app[VIDEO].workspace(user_id), outcome.result_file)
        poster = "data:image/jpeg;base64," + base64.b64encode(image).decode() if image else None
    await emit({
        "type": "done",
        "outcome": {
            "text": _outcome_text(outcome) if not outcome.cancelled else "Монтаж закончен.",
            "error": outcome.error,
            "rendered": outcome.rendered,
            "reused": outcome.reused,
            "kind": outcome.kind,
            "duration": outcome.duration,
            "width": outcome.width,
            "height": outcome.height,
            "result": outcome.result_file,
            "poster": poster,
        },
        "session": await _session(app, state),
    })
    if not gone:
        await response.write_eof()
    return response


async def _ready_session(request: web.Request) -> tuple[FSMContext, WebAppUser] | web.Response:
    app = request.app
    if not available(app):
        return _unavailable()
    user: WebAppUser = request[_USER_KEY]
    state = _context(app, user.id)
    current = await state.get_state()
    if current == VideoStates.working.state:
        return _error(409, "busy", WORKING_TEXT)
    if current != VideoStates.session.state:
        return _error(409, "no_session", "Монтаж не начат: прикрепи видео скрепкой 📎 или напиши /video.")
    return state, user


async def post_edit(request: web.Request) -> web.StreamResponse:
    """{"text": "обрежь первые 5 секунд"} — просьба словами, ответ потоком (NDJSON)."""
    ready = await _ready_session(request)
    if isinstance(ready, web.Response):
        return ready
    state, user = ready
    payload = await _read_json(request)
    text = payload.get("text") if payload else None
    if not isinstance(text, str) or not text.strip():
        return _error(400, "bad_request", "Пустое сообщение.")
    if len(text) > MAX_REQUEST_CHARS:
        return _error(400, "too_long", f"Просьба длиннее {MAX_REQUEST_CHARS} символов — сократи её.")
    if not clips_of(await state.get_data()):
        return _error(400, "no_clips", NO_CLIPS_TEXT)
    if (limited := _rate_limited(request.app, user)) is not None:
        return limited
    app = request.app
    return await _stream(
        request,
        lambda progress: run_request(app[BOT], state, app[VIDEO], text.strip(), user_id=user.id,
                                     chat_id=user.id, progress=progress),
        user.id,
    )


async def post_action(request: web.Request) -> web.StreamResponse:
    """{"action": "quick", "value": "pauses"} | {"action": "undo"} | {"action": "done"}."""
    ready = await _ready_session(request)
    if isinstance(ready, web.Response):
        return ready
    state, user = ready
    app = request.app
    payload = await _read_json(request) or {}
    action, value = payload.get("action"), payload.get("value") or ""
    if not isinstance(action, str) or not isinstance(value, str):
        return _error(400, "bad_request", "Некорректный запрос.")
    if action == "done":
        await finish_session(app[BOT], state, app[VIDEO], user.id, user.id)
        return web.json_response({"message": DONE_TEXT, "session": None})
    if (problem := check_action(app[VIDEO], await state.get_data(), action, value)) is not None:
        return _error(400, "bad_action", problem)
    return await _stream(
        request,
        lambda progress: run_action(app[BOT], state, app[VIDEO], action, value, user_id=user.id,
                                    chat_id=user.id, progress=progress),
        user.id,
    )


async def get_result(request: web.Request) -> web.StreamResponse:
    """Последний результат монтажа — для просмотра в приложении."""
    app = request.app
    if not available(app):
        return _unavailable()
    user: WebAppUser = request[_USER_KEY]
    name = request.match_info.get("name", "")
    if not _RESULT_RE.match(name):
        return _error(404, "not_found", "Такого файла нет.")
    path = app[VIDEO].workspace(user.id).path(name)
    if not path.is_file():
        return _error(404, "not_found", "Этого варианта уже нет — он в чате с ботом.")
    content_type = "audio/mp4" if name.endswith(".m4a") else "video/mp4"
    return web.FileResponse(path, headers={"Content-Type": content_type})


def add_routes(app: web.Application) -> None:
    app.router.add_post("/api/video/start", post_start)
    app.router.add_post("/api/video/upload", post_upload)
    app.router.add_post("/api/video/edit", post_edit)
    app.router.add_post("/api/video/action", post_action)
    app.router.add_get("/api/video/result/{name}", get_result)


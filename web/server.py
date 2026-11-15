"""HTTP-сервер Mini App: статика интерфейса и JSON API поверх тех же сервисов, что у бота.

Все запросы к /api/ обязаны нести подписанную Telegram строку initData, а доступ,
как и в боте, есть только у пользователей из ALLOWED_USER_IDS.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.web_app import WebAppUser
from aiohttp import web

from ai.client import (
    AIClient,
    AIError,
    AllProvidersFailedError,
    DailyLimitExceededError,
    NoVisionModelError,
)
from ai.providers.base import CompletionResult
from bot.handlers.draw import deliver_drawing
from config.settings import Settings
from database.models import Message
from memory.manager import MemoryManager
from memory.repository import CHAT_BREAK
from services.chat import ChatService
from services.drawing import Drawing, DrawingService, history_text
from services.imagegen import ImageGenerationError, ImageLimitError
from services.instructions import MAX_INSTRUCTION_LENGTH, InstructionsStore
from services.longterm import MAX_FACT_LENGTH, MAX_FACTS, LongTermMemory, MemoryFullError
from services.modes import CODE, DEFAULT_MODE, MODES, ModeStore
from services.preferences import PreferencesStore
from services.speech import SpeechError, SpeechToText
from services.vision import MAX_IMAGE_BYTES, NO_VISION_TEXT, ImageError, to_data_url
from services.reminders import (
    MAX_AHEAD,
    MAX_PENDING,
    MAX_TEXT,
    REPEATS,
    Reminders,
    TooManyRemindersError,
)
from services.preferences import options as preference_options
from services.rate_limit import SlidingWindowLimiter
from services.search import SearchStarted, WebSearch
from services.usage import UsageTracker
from web import video_api
from web.auth import InitDataError, extract_init_data, verify_init_data
from web.common import (
    BOT,
    LIMITER,
    SETTINGS,
    _USER_KEY,
    _apply_headers,
    _error,
    _rate_limited,
    _read_json,
)

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
MAX_TEXT_LENGTH = 4000
# В режиме кода в сообщение вставляют целые файлы, поэтому лимит выше.
MAX_CODE_TEXT_LENGTH = 16000
MAX_BODY_BYTES = 160 * 1024
TIMELINE_LIMIT = 100

CHAT = web.AppKey("chat", ChatService)
MEMORY = web.AppKey("memory", MemoryManager)
USAGE = web.AppKey("usage", UsageTracker)
AI_CLIENT = web.AppKey("ai_client", AIClient)
PREFERENCES = web.AppKey("preferences", PreferencesStore)
LONG_MEMORY = web.AppKey("long_memory", LongTermMemory)
REMINDERS = web.AppKey("reminders", Reminders)
INSTRUCTIONS = web.AppKey("instructions", object)
SPEECH = web.AppKey("speech", object)
MODES_STORE = web.AppKey("modes", object)
DRAWING = web.AppKey("drawing", object)
SEARCH = web.AppKey("search", object)

MAX_VOICE_BYTES = 5 * 1024 * 1024
# Что умеет записывать браузер телефона → расширение файла для Whisper
AUDIO_TYPES = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/aac": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}

_IMAGE_FAILURE_TEXTS = {
    "rejected": "Сервис картинок отказался это рисовать — возможно, сработал его фильтр. Попробуй описать по-другому.",
    "rate_limit": "Сервис картинок упёрся в лимит бесплатного тарифа. Попробуй чуть позже.",
    "config": (
        "Не получается подключиться к сервису картинок: похоже, проблема с ключом или аккаунтом. "
        "Подробности — в логах бота."
    ),
    "unavailable": "Сервис картинок сейчас не отвечает. Попробуй ещё раз чуть позже.",
}
IMAGES_OFF_TEXT = (
    "Рисовать я пока не умею: владельцу бота нужно подключить сервис картинок "
    "(Cloudflare Workers AI или Pollinations — см. README)."
)

_AI_FAILURE_TEXTS = {
    "rate_limit": (
        "Сейчас исчерпан лимит запросов к ИИ-модели: бесплатные тарифы ограничены. "
        "Попробуй ещё раз через минуту-другую."
    ),
    "config": (
        "Не получается подключиться к ИИ-сервису: похоже, проблема с API-ключом, "
        "балансом или регионом сервера. Подробности — в логах бота."
    ),
    "unavailable": "ИИ-сервис сейчас не отвечает. Отправь сообщение ещё раз чуть позже.",
}


def _timestamp(value: datetime) -> int:
    # SQLite возвращает время без часового пояса; в базе оно хранится в UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _serialize(message: Message) -> dict[str, Any]:
    if message.role == CHAT_BREAK:
        return {"role": "break", "content": "", "ts": _timestamp(message.created_at)}
    return {
        "role": "assistant" if message.role == "assistant" else "user",
        "content": message.content,
        "ts": _timestamp(message.created_at),
    }


@web.middleware
async def security_headers(request: web.Request, handler) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _apply_headers(request, exc.headers)
        raise
    _apply_headers(request, response.headers)
    return response


@web.middleware
async def api_guard(request: web.Request, handler) -> web.StreamResponse:
    """Аутентификация по initData и JSON-ответы на ошибки для всех путей /api/."""
    if not request.path.startswith("/api/"):
        return await handler(request)

    settings = request.app[SETTINGS]
    try:
        user = verify_init_data(
            extract_init_data(request.headers.get("Authorization")),
            settings.bot_token,
            max_age=settings.webapp_auth_max_age,
        )
    except InitDataError as exc:
        logger.info("Mini App auth failed: %s", exc)
        return _error(
            401,
            "unauthorized",
            "Не удалось подтвердить вход через Telegram. Закрой приложение и открой его заново.",
        )
    if user.id not in settings.allowed_user_ids:
        logger.info("Access denied for user_id=%s", user.id)
        return _error(403, "forbidden", "Это личный ассистент: доступ есть только у владельца.")

    request[_USER_KEY] = user
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return _error(exc.status, "http_error", exc.reason or "Ошибка запроса")
    except Exception:
        logger.exception("Unhandled error in %s %s", request.method, request.path)
        return _error(500, "internal", "Что-то пошло не так. Попробуй ещё раз.")


def _resolve_model(ai_client: AIClient, model_id: Any) -> tuple[str, str] | None:
    """«провайдер:модель» из запроса → пара для AIClient. Незнакомое значение — авто."""
    if not isinstance(model_id, str):
        return None
    for provider, model in ai_client.available_models:
        if model_id == f"{provider.name}:{model}":
            return provider.name, model
    return None


async def _memory_payload(memory: LongTermMemory, user_id: int) -> dict[str, Any]:
    return {
        "enabled": await memory.is_enabled(user_id),
        "facts": [{"id": f.id, "text": f.text} for f in await memory.list(user_id)],
        "max_facts": MAX_FACTS,
        "max_fact_length": MAX_FACT_LENGTH,
    }


async def _reminders_payload(reminders: Reminders, user_id: int) -> dict[str, Any]:
    return {
        "timezone": await reminders.get_timezone(user_id),
        "items": [
            {"id": i.id, "text": i.text, "due": int(i.due_at.timestamp()), "repeat": i.repeat}
            for i in await reminders.list_pending(user_id)
        ],
        "max": MAX_PENDING,
        "max_text": MAX_TEXT,
    }


async def _instructions_payload(store: InstructionsStore | None, user_id: int) -> dict[str, Any] | None:
    if store is None:
        return None
    return {**(await store.get(user_id)).as_dict(), "max_length": MAX_INSTRUCTION_LENGTH}


async def post_instructions(request: web.Request) -> web.Response:
    """{"about": "...", "style": "...", "enabled": true} — любые поля по отдельности."""
    user: WebAppUser = request[_USER_KEY]
    store: InstructionsStore | None = request.app[INSTRUCTIONS]
    if store is None:
        return _error(503, "unavailable", "Инструкции сейчас недоступны.")
    payload = await _read_json(request)
    if payload is None:
        return _error(400, "bad_request", "Некорректный запрос.")
    about, style, enabled = payload.get("about"), payload.get("style"), payload.get("enabled")
    if (
        (about is not None and not isinstance(about, str))
        or (style is not None and not isinstance(style, str))
        or (enabled is not None and not isinstance(enabled, bool))
        or (about is None and style is None and enabled is None)
    ):
        return _error(400, "bad_request", "Некорректный запрос.")
    try:
        await store.update(user.id, about=about, style=style, enabled=enabled)
    except ValueError:
        return _error(
            400, "too_long", f"Слишком длинно: в каждом поле до {MAX_INSTRUCTION_LENGTH} символов."
        )
    return web.json_response(await _instructions_payload(store, user.id))


async def _mode(app: web.Application, user_id: int) -> str:
    store: ModeStore | None = app[MODES_STORE]
    return await store.get(user_id) if store is not None else DEFAULT_MODE


def _text_limit(mode: str) -> int:
    return MAX_CODE_TEXT_LENGTH if mode == CODE else MAX_TEXT_LENGTH


async def _check_text(request: web.Request, user: WebAppUser, text: Any) -> str | web.Response:
    """Текст сообщения или готовый ответ с ошибкой."""
    if not isinstance(text, str) or not text.strip():
        return _error(400, "bad_request", "Пустое сообщение.")
    text = text.strip()
    limit = _text_limit(await _mode(request.app, user.id))
    if len(text) > limit:
        return _error(400, "too_long", f"Сообщение длиннее {limit} символов — сократи его.")
    return text


async def post_mode(request: web.Request) -> web.Response:
    """{"mode": "code" | "chat"} — включает и выключает режим кода (как /code в боте)."""
    user: WebAppUser = request[_USER_KEY]
    store: ModeStore | None = request.app[MODES_STORE]
    if store is None:
        return _error(503, "unavailable", "Режим кода сейчас недоступен.")
    payload = await _read_json(request)
    mode = payload.get("mode") if payload else None
    if not isinstance(mode, str) or mode not in MODES:
        return _error(400, "bad_request", "Такого режима нет.")
    await store.set(user.id, mode)
    return web.json_response({"mode": mode, "limits": {"max_text_length": _text_limit(mode)}})


async def get_state(request: web.Request) -> web.Response:
    """Всё, что нужно интерфейсу при открытии: имя, история, модели, счётчики."""
    app = request.app
    user: WebAppUser = request[_USER_KEY]
    usage = app[USAGE]
    timeline = await app[MEMORY].get_timeline(user.id, TIMELINE_LIMIT)
    mode = await _mode(app, user.id)
    return web.json_response(
        {
            "assistant": {"name": app[SETTINGS].assistant_name},
            "user": {"first_name": user.first_name},
            "history": [_serialize(message) for message in timeline],
            "models": [
                {"id": f"{provider.name}:{model}", "provider": provider.title, "model": model}
                for provider, model in app[AI_CLIENT].available_models
            ],
            "providers": app[AI_CLIENT].provider_titles,
            "usage": {"used": await usage.used_today(), "limit": usage.daily_limit},
            "limits": {"max_text_length": _text_limit(mode)},
            # None — режим кода на сервере не подключён, переключатель не показывается.
            "mode": mode if app[MODES_STORE] is not None else None,
            "voice": app[SPEECH] is not None,
            "vision": app[AI_CLIENT].has_vision,
            "images": await _images_payload(app[DRAWING]),
            "search": app[SEARCH] is not None,
            "preferences": (await app[PREFERENCES].get(user.id)).as_dict(),
            "instructions": await _instructions_payload(app[INSTRUCTIONS], user.id),
            "memory": await _memory_payload(app[LONG_MEMORY], user.id),
            "reminders": await _reminders_payload(app[REMINDERS], user.id),
            "video": await video_api.state_payload(app, user.id),
            "preference_options": preference_options(),
        }
    )


async def _images_payload(drawing: DrawingService | None) -> dict[str, Any]:
    if drawing is None or not drawing.enabled:
        return {"enabled": False}
    usage = drawing.generator.usage
    return {
        "enabled": True,
        "used": await usage.used_today() if usage else 0,
        "limit": usage.daily_limit if usage else 0,
    }


def _image_error(exc: ImageGenerationError | ImageLimitError) -> tuple[int, str, str]:
    if isinstance(exc, ImageLimitError):
        return (
            429,
            "image_limit",
            f"На сегодня лимит картинок исчерпан ({exc.limit}). Он обновится в 00:00 по UTC.",
        )
    logger.error("Image generation failed (%s): %s", exc.kind, exc)
    status = 422 if exc.kind == "rejected" else 503
    return status, f"image_{exc.kind}", _IMAGE_FAILURE_TEXTS[exc.kind]


async def _deliver_to_chat(app: web.Application, user_id: int, result: Drawing) -> bool:
    """Копия картинки — в чат с ботом: приложение хранит только текст истории, а из чата
    картинку можно сохранить и переслать."""
    bot: Bot | None = app[BOT]
    if bot is None:
        return False
    try:
        await deliver_drawing(bot, user_id, result, caption=f"{result.caption} (из приложения)")
    except TelegramAPIError as exc:
        logger.warning("Could not send the picture to the chat: %s", exc)
        return False
    return True


async def _drawing_payload(app: web.Application, result: Drawing, *, text: str, user_id: int) -> dict[str, Any]:
    usage = app[USAGE]
    sent = await _deliver_to_chat(app, user_id, result)
    return {
        "reply": {
            "role": "assistant",
            "content": text,
            "ts": int(datetime.now(timezone.utc).timestamp()),
            "provider": "images",
            "model": result.image.model,
            "truncated": False,
            "image": result.image.data_url(),
            "drawing_id": result.id,
            "sent_to_chat": sent,
        },
        "usage": {"used": await usage.used_today(), "limit": usage.daily_limit},
        "images": await _images_payload(app[DRAWING]),
    }


async def _try_draw(request: web.Request, user: WebAppUser, text: str) -> dict[str, Any] | web.Response | None:
    """Если сообщение — просьба нарисовать (или /draw …), рисует: возвращает ответ для
    клиента или ошибку. None — это обычное сообщение для диалога."""
    drawing: DrawingService | None = request.app[DRAWING]
    command, _, rest = text.partition(" ")
    explicit = command.lower().partition("@")[0] == "/draw"
    if explicit:
        text = rest.strip()
        if drawing is None or not drawing.enabled:
            return _error(503, "images_off", IMAGES_OFF_TEXT)
        if not text:
            return _error(400, "bad_request", "Напиши после /draw, что нарисовать.")
    elif drawing is None or not drawing.enabled or not drawing.looks_like_request(text):
        return None
    plan = await drawing.plan(user.id, text, explicit=explicit)
    if plan is None:
        return None
    try:
        result = await drawing.draw(user.id, plan)
    except (ImageGenerationError, ImageLimitError) as exc:
        return _error(*_image_error(exc))
    return await _drawing_payload(request.app, result, text=history_text(plan), user_id=user.id)


async def post_draw_again(request: web.Request) -> web.Response:
    """{"id": N} — ещё один вариант картинки; в истории — «🔄 Ещё вариант» и подпись."""
    app = request.app
    user: WebAppUser = request[_USER_KEY]
    drawing: DrawingService | None = app[DRAWING]
    if drawing is None or not drawing.enabled:
        return _error(503, "images_off", IMAGES_OFF_TEXT)
    payload = await _read_json(request)
    drawing_id = payload.get("id") if payload else None
    if not isinstance(drawing_id, int) or isinstance(drawing_id, bool):
        return _error(400, "bad_request", "Некорректный запрос.")
    if (limited := _rate_limited(app, user)) is not None:
        return limited
    try:
        result = await drawing.again(user.id, drawing_id)
    except (ImageGenerationError, ImageLimitError) as exc:
        return _error(*_image_error(exc))
    if result is None:
        return _error(410, "forgotten", "Эту картинку я уже не помню. Напиши просьбу ещё раз — нарисую заново.")
    return web.json_response(
        await _drawing_payload(app, result, text=history_text(result.plan), user_id=user.id)
    )


async def post_chat(request: web.Request) -> web.Response:
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    text = await _check_text(request, user, payload.get("text") if payload else None)
    if isinstance(text, web.Response):
        return text
    return await _answer(request, user, text, payload.get("model"), search=_search_flag(payload))


def _search_flag(payload: dict[str, Any]) -> bool | None:
    """Поле "search": true — пользователь включил поиск для этого сообщения (кнопка 🌐)."""
    return True if payload.get("search") is True else None


async def _read_part(part, limit: int) -> bytes | None:
    """Читает часть multipart-запроса; None — превышен лимит."""
    data = bytearray()
    while chunk := await part.read_chunk(64 * 1024):
        data += chunk
        if len(data) > limit:
            return None
    return bytes(data)


async def post_photo(request: web.Request) -> web.Response:
    """multipart: image (файл), text (подпись, необязательно), model (необязательно)."""
    user: WebAppUser = request[_USER_KEY]
    if not request.content_type.startswith("multipart/"):
        return _error(400, "bad_request", "Некорректный запрос.")
    image: bytes | None = None
    fields: dict[str, str] = {}
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "image":
                image = await _read_part(part, MAX_IMAGE_BYTES)
                if image is None:
                    return _error(413, "too_large", "Картинка слишком большая: до 5 МБ.")
            elif part.name in ("text", "model"):
                raw = await _read_part(part, MAX_TEXT_LENGTH * 4)
                if raw is None:
                    return _error(400, "too_long", f"Подпись длиннее {MAX_TEXT_LENGTH} символов.")
                fields[part.name] = raw.decode("utf-8", errors="replace")
    except (ValueError, AssertionError):  # битый multipart
        return _error(400, "bad_request", "Некорректный запрос.")
    if image is None:
        return _error(400, "bad_request", "Нет картинки.")
    text = fields.get("text", "").strip()
    if len(text) > MAX_TEXT_LENGTH:
        return _error(400, "too_long", f"Подпись длиннее {MAX_TEXT_LENGTH} символов.")
    try:
        data_url = to_data_url(image)
    except ImageError:
        return _error(415, "bad_image", "Не получилось открыть картинку: нужен JPEG, PNG или WebP.")
    return await _answer(request, user, text, fields.get("model"), images=(data_url,))


def _ai_error(exc: AIError) -> tuple[int, str, str]:
    """(HTTP-статус, код, текст для пользователя) для ошибки ИИ."""
    if isinstance(exc, NoVisionModelError):
        return 503, "no_vision", NO_VISION_TEXT
    if isinstance(exc, DailyLimitExceededError):
        return (
            429,
            "daily_limit",
            f"Достигнут дневной лимит запросов к ИИ ({exc.limit}). Он обновится в 00:00 по UTC.",
        )
    if isinstance(exc, AllProvidersFailedError):
        logger.error("All AI providers failed (%s)", exc.kind)
        return 503, f"ai_{exc.kind}", _AI_FAILURE_TEXTS[exc.kind]
    logger.error("AI error: %s", exc)
    return 503, "ai_unavailable", _AI_FAILURE_TEXTS["unavailable"]


async def _reply_payload(app: web.Application, result: CompletionResult) -> dict[str, Any]:
    usage = app[USAGE]
    return {
        "reply": {
            "role": "assistant",
            "content": result.text,
            "ts": int(datetime.now(timezone.utc).timestamp()),
            "provider": result.provider,
            "model": result.model,
            "truncated": result.truncated,
        },
        "usage": {"used": await usage.used_today(), "limit": usage.daily_limit},
    }


async def _answer(
    request: web.Request,
    user: WebAppUser,
    text: str,
    model: Any,
    images: tuple[str, ...] = (),
    *,
    search: bool | None = None,
) -> web.Response:
    """Общая часть /api/chat и /api/photo: лимит частоты, ответ модели, ошибки ИИ."""
    app = request.app
    if (limited := _rate_limited(app, user)) is not None:
        return limited
    if not images:
        drawn = await _try_draw(request, user, text)
        if isinstance(drawn, web.Response):
            return drawn
        if drawn is not None:
            return web.json_response(drawn)
    try:
        result = await app[CHAT].ask(
            user.id,
            text,
            user_name=user.first_name,
            prefer=_resolve_model(app[AI_CLIENT], model),
            images=images,
            search=search,
        )
    except AIError as exc:
        return _error(*_ai_error(exc))
    return web.json_response(await _reply_payload(app, result))


async def post_chat_stream(request: web.Request) -> web.StreamResponse:
    """Как /api/chat, но ответ приходит по мере генерации — построчно (NDJSON):
    {"type": "search", "query": "..."} — Эля ищет в интернете (если понадобилось),
    {"type": "delta", "text": "..."} … и в конце {"type": "done", "reply": …, "usage": …}
    или {"type": "error", "error": {...}}. Ошибки до начала ответа — обычный JSON со статусом."""
    app = request.app
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    text = await _check_text(request, user, payload.get("text") if payload else None)
    if isinstance(text, web.Response):
        return text
    if (limited := _rate_limited(app, user)) is not None:
        return limited
    drawn = await _try_draw(request, user, text)
    if isinstance(drawn, web.Response):
        return drawn
    if drawn is not None:
        # Картинка приходит целиком — одним событием того же потока.
        return web.Response(
            text=json.dumps({"type": "done", **drawn}, ensure_ascii=False) + "\n",
            content_type="application/x-ndjson",
            charset="utf-8",
        )

    events = app[CHAT].ask_stream(
        user.id,
        text,
        user_name=user.first_name,
        prefer=_resolve_model(app[AI_CLIENT], payload.get("model")),
        search=_search_flag(payload),
    )
    try:
        # Ждём начала ответа: если модель недоступна, честнее вернуть обычную ошибку.
        try:
            event = await anext(events)
        except AIError as exc:
            return _error(*_ai_error(exc))

        response = web.StreamResponse(
            headers={
                "Content-Type": "application/x-ndjson; charset=utf-8",
                "X-Accel-Buffering": "no",  # прокси не должны копить ответ целиком
            }
        )
        _apply_headers(request, response.headers)
        await response.prepare(request)

        async def emit(item: dict[str, Any]) -> None:
            await response.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))

        try:
            while True:
                if isinstance(event, CompletionResult):
                    await emit({"type": "done", **await _reply_payload(app, event)})
                    break
                if isinstance(event, SearchStarted):
                    await emit({"type": "search", "query": event.query})
                else:
                    await emit({"type": "delta", "text": event})
                event = await anext(events)
        except AIError as exc:
            status, code, message = _ai_error(exc)
            await emit({"type": "error", "error": {"code": code, "message": message}})
        await response.write_eof()
        return response
    finally:
        # Клиент ушёл посреди ответа — поток закрывается, история не меняется.
        await events.aclose()


async def post_preferences(request: web.Request) -> web.Response:
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    if payload is None:
        return _error(400, "bad_request", "Некорректный запрос.")
    style = payload.get("style")
    verbosity = payload.get("verbosity")
    if (style is not None and not isinstance(style, str)) or (
        verbosity is not None and not isinstance(verbosity, str)
    ):
        return _error(400, "bad_request", "Некорректный запрос.")
    try:
        prefs = await request.app[PREFERENCES].update(user.id, style=style, verbosity=verbosity)
    except ValueError:
        return _error(400, "bad_request", "Такой настройки нет.")
    return web.json_response({"preferences": prefs.as_dict()})


_FACT_ERRORS = {
    "too_short": "Слишком коротко: напиши факт хотя бы в паре слов.",
    "too_long": f"Слишком длинно: уложись в {MAX_FACT_LENGTH} символов.",
    "sensitive": "Пароли, номера карт и ключи я не запоминаю.",
}


async def post_memory(request: web.Request) -> web.Response:
    """Включает и выключает долговременную память."""
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    enabled = payload.get("enabled") if payload else None
    if not isinstance(enabled, bool):
        return _error(400, "bad_request", "Некорректный запрос.")
    memory = request.app[LONG_MEMORY]
    await memory.set_enabled(user.id, enabled)
    return web.json_response(await _memory_payload(memory, user.id))


async def post_memory_add(request: web.Request) -> web.Response:
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    text = payload.get("text") if payload else None
    if not isinstance(text, str):
        return _error(400, "bad_request", "Некорректный запрос.")
    memory = request.app[LONG_MEMORY]
    try:
        fact = await memory.add(user.id, text, source="manual")
    except ValueError as exc:
        return _error(400, str(exc), _FACT_ERRORS.get(str(exc), "Не получилось сохранить."))
    except MemoryFullError:
        return _error(409, "memory_full", f"Память заполнена ({MAX_FACTS}): удали что-нибудь лишнее.")
    data = await _memory_payload(memory, user.id)
    data["added"] = {"id": fact.id, "text": fact.text} if fact else None
    return web.json_response(data)


async def post_memory_delete(request: web.Request) -> web.Response:
    """{"id": N} удаляет один факт, {"all": true} — все."""
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    memory = request.app[LONG_MEMORY]
    if payload and payload.get("all") is True:
        await memory.clear(user.id)
    elif payload and isinstance(payload.get("id"), int) and not isinstance(payload.get("id"), bool):
        await memory.delete(user.id, payload["id"])
    else:
        return _error(400, "bad_request", "Некорректный запрос.")
    return web.json_response(await _memory_payload(memory, user.id))


async def post_timezone(request: web.Request) -> web.Response:
    """Приложение присылает часовой пояс телефона (IANA-имя)."""
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    name = payload.get("tz") if payload else None
    if not isinstance(name, str) or len(name) > 64:
        return _error(400, "bad_request", "Некорректный запрос.")
    reminders = request.app[REMINDERS]
    if await reminders.set_timezone(user.id, name) is None:
        return _error(400, "bad_timezone", "Не знаю такого часового пояса.")
    return web.json_response(await _reminders_payload(reminders, user.id))


async def post_reminder_add(request: web.Request) -> web.Response:
    """{"text": "...", "when": "ГГГГ-ММ-ДДTЧЧ:ММ" (местное время), "repeat": "none|daily|..."}"""
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    if payload is None:
        return _error(400, "bad_request", "Некорректный запрос.")
    text, when = payload.get("text"), payload.get("when")
    repeat = payload.get("repeat", "none")
    if not isinstance(text, str) or not isinstance(when, str) or repeat not in REPEATS:
        return _error(400, "bad_request", "Некорректный запрос.")
    text = " ".join(text.split())
    if not text:
        return _error(400, "bad_request", "Напиши, что напомнить.")
    if len(text) > MAX_TEXT:
        return _error(400, "too_long", f"Слишком длинно: уложись в {MAX_TEXT} символов.")

    reminders = request.app[REMINDERS]
    tz = await reminders.tzinfo(user.id)
    if tz is None:
        return _error(400, "no_timezone", "Не знаю твой часовой пояс: перезапусти приложение.")
    try:
        local = datetime.fromisoformat(when)
    except ValueError:
        return _error(400, "bad_request", "Не получилось разобрать дату и время.")
    if local.tzinfo is not None:
        return _error(400, "bad_request", "Не получилось разобрать дату и время.")
    due = local.replace(tzinfo=tz).astimezone(timezone.utc)
    now = reminders.now()
    if due <= now:
        return _error(400, "past", "Это время уже прошло. Выбери время в будущем.")
    if due - now > MAX_AHEAD:
        return _error(400, "far", "Я напоминаю не дальше чем на год вперёд.")
    try:
        await reminders.create(user.id, text, due, repeat, tz=tz)
    except TooManyRemindersError:
        return _error(409, "too_many", f"Уже {MAX_PENDING} напоминаний: удали что-нибудь лишнее.")
    return web.json_response(await _reminders_payload(reminders, user.id))


async def post_reminder_delete(request: web.Request) -> web.Response:
    user: WebAppUser = request[_USER_KEY]
    payload = await _read_json(request)
    reminder_id = payload.get("id") if payload else None
    if not isinstance(reminder_id, int) or isinstance(reminder_id, bool):
        return _error(400, "bad_request", "Некорректный запрос.")
    reminders = request.app[REMINDERS]
    await reminders.delete(user.id, reminder_id)
    return web.json_response(await _reminders_payload(reminders, user.id))


async def post_voice(request: web.Request) -> web.Response:
    """Тело запроса — запись с микрофона; ответ — распознанный текст."""
    speech: SpeechToText | None = request.app[SPEECH]
    if speech is None:
        return _error(
            503, "speech_off",
            "Голосовые не настроены: владельцу бота нужно добавить ключ GROQ_API_KEY.",
        )
    content_type = request.content_type
    extension = AUDIO_TYPES.get(content_type)
    if extension is None:
        return _error(415, "bad_audio", "Не получилось прочитать запись.")
    # Тело читается вручную: общий лимит запроса (64 КБ) для аудио слишком мал.
    data = bytearray()
    async for chunk in request.content.iter_chunked(64 * 1024):
        data += chunk
        if len(data) > MAX_VOICE_BYTES:
            return _error(413, "too_large", "Запись слишком длинная: до 5 минут.")
    if not data:
        return _error(400, "bad_request", "Пустая запись.")
    try:
        text = await speech.transcribe(bytes(data), f"voice.{extension}", content_type)
    except SpeechError as exc:
        logger.warning("Speech recognition failed: %s", exc)
        return _error(502, "speech_failed", "Не получилось разобрать голосовое. Попробуй ещё раз.")
    return web.json_response({"text": text})


async def post_clear(request: web.Request) -> web.Response:
    user: WebAppUser = request[_USER_KEY]
    deleted = await request.app[MEMORY].clear_history(user.id)
    return web.json_response({"deleted": deleted})


async def post_new_chat(request: web.Request) -> web.Response:
    """«Новый чат»: Эля начинает с чистого листа, прошлая переписка остаётся в ленте."""
    user: WebAppUser = request[_USER_KEY]
    memory = request.app[MEMORY]
    started = await memory.start_new_chat(user.id) is not None
    timeline = await memory.get_timeline(user.id, TIMELINE_LIMIT)
    return web.json_response({"started": started, "history": [_serialize(m) for m in timeline]})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def create_web_app(
    settings: Settings,
    chat_service: ChatService,
    memory: MemoryManager,
    usage: UsageTracker,
    ai_client: AIClient,
    limiter: SlidingWindowLimiter,
    preferences: PreferencesStore,
    long_memory: LongTermMemory,
    reminders: Reminders,
    speech: SpeechToText | None = None,
    instructions: InstructionsStore | None = None,
    modes: ModeStore | None = None,
    *,
    drawing: DrawingService | None = None,
    bot: Bot | None = None,
    search: WebSearch | None = None,
    video: object | None = None,
    fsm_storage: object | None = None,
) -> web.Application:
    """video, bot и fsm_storage — для монтажа видео: сессия общая с чатом, результат уходит в чат."""
    app = web.Application(
        middlewares=[security_headers, api_guard], client_max_size=MAX_BODY_BYTES
    )
    app[SETTINGS] = settings
    app[CHAT] = chat_service
    app[MEMORY] = memory
    app[USAGE] = usage
    app[AI_CLIENT] = ai_client
    app[LIMITER] = limiter
    app[PREFERENCES] = preferences
    app[LONG_MEMORY] = long_memory
    app[REMINDERS] = reminders
    app[SPEECH] = speech
    app[INSTRUCTIONS] = instructions
    app[MODES_STORE] = modes
    app[DRAWING] = drawing
    app[BOT] = bot
    app[SEARCH] = search
    app[video_api.VIDEO] = video
    app[video_api.FSM_STORAGE] = fsm_storage

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/state", get_state)
    app.router.add_post("/api/chat", post_chat)
    app.router.add_post("/api/chat/stream", post_chat_stream)
    app.router.add_post("/api/clear", post_clear)
    app.router.add_post("/api/chat/new", post_new_chat)
    app.router.add_post("/api/preferences", post_preferences)
    app.router.add_post("/api/instructions", post_instructions)
    app.router.add_post("/api/mode", post_mode)
    app.router.add_post("/api/memory", post_memory)
    app.router.add_post("/api/memory/add", post_memory_add)
    app.router.add_post("/api/memory/delete", post_memory_delete)
    app.router.add_post("/api/timezone", post_timezone)
    app.router.add_post("/api/reminders/add", post_reminder_add)
    app.router.add_post("/api/reminders/delete", post_reminder_delete)
    app.router.add_post("/api/voice", post_voice)
    app.router.add_post("/api/photo", post_photo)
    app.router.add_post("/api/draw/again", post_draw_again)
    video_api.add_routes(app)
    app.router.add_static("/assets/", WEBAPP_DIR / "assets")
    return app


async def start_web_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Запускает сервер в текущем цикле событий; останавливается через runner.cleanup().

    OSError (например, порт занят) пробрасывается вызывающему.
    """
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, host=host, port=port).start()
    except OSError:
        await runner.cleanup()
        raise
    logger.info("Mini App server is listening on %s:%s", host, port)
    return runner

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

from aiogram.utils.web_app import WebAppUser
from aiohttp import web

from ai.client import AIClient, AllProvidersFailedError, DailyLimitExceededError
from config.settings import Settings
from database.models import Message
from memory.manager import MemoryManager
from services.chat import ChatService
from services.longterm import MAX_FACT_LENGTH, MAX_FACTS, LongTermMemory, MemoryFullError
from services.preferences import PreferencesStore
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
from services.usage import UsageTracker
from web.auth import InitDataError, extract_init_data, verify_init_data

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
MAX_TEXT_LENGTH = 4000
MAX_BODY_BYTES = 64 * 1024
TIMELINE_LIMIT = 100

SETTINGS = web.AppKey("settings", Settings)
CHAT = web.AppKey("chat", ChatService)
MEMORY = web.AppKey("memory", MemoryManager)
USAGE = web.AppKey("usage", UsageTracker)
AI_CLIENT = web.AppKey("ai_client", AIClient)
LIMITER = web.AppKey("limiter", SlidingWindowLimiter)
PREFERENCES = web.AppKey("preferences", PreferencesStore)
LONG_MEMORY = web.AppKey("long_memory", LongTermMemory)
REMINDERS = web.AppKey("reminders", Reminders)

_USER_KEY = web.RequestKey("tg_user", WebAppUser)

# Скрипты — только свои и SDK Telegram; ответ модели выводится как текст, но CSP
# подстраховывает на случай ошибки в разметке.
_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'"
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


def _error(status: int, code: str, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message, **extra}}, status=status)


def _timestamp(value: datetime) -> int:
    # SQLite возвращает время без часового пояса; в базе оно хранится в UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _serialize(message: Message) -> dict[str, Any]:
    return {
        "role": "assistant" if message.role == "assistant" else "user",
        "content": message.content,
        "ts": _timestamp(message.created_at),
    }


def _apply_headers(request: web.Request, headers) -> None:
    headers.setdefault("Content-Security-Policy", _CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    # Интерфейс всегда перепроверяется (WebView Telegram кеширует агрессивно),
    # ответы API не кешируются вовсе.
    is_api = request.path.startswith("/api/")
    headers.setdefault("Cache-Control", "no-store" if is_api else "no-cache")


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


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


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


async def get_state(request: web.Request) -> web.Response:
    """Всё, что нужно интерфейсу при открытии: имя, история, модели, счётчики."""
    app = request.app
    user: WebAppUser = request[_USER_KEY]
    usage = app[USAGE]
    timeline = await app[MEMORY].get_timeline(user.id, TIMELINE_LIMIT)
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
            "limits": {"max_text_length": MAX_TEXT_LENGTH},
            "preferences": (await app[PREFERENCES].get(user.id)).as_dict(),
            "memory": await _memory_payload(app[LONG_MEMORY], user.id),
            "reminders": await _reminders_payload(app[REMINDERS], user.id),
            "preference_options": preference_options(),
        }
    )


async def post_chat(request: web.Request) -> web.Response:
    app = request.app
    user: WebAppUser = request[_USER_KEY]

    payload = await _read_json(request)
    text = payload.get("text") if payload else None
    if not isinstance(text, str) or not text.strip():
        return _error(400, "bad_request", "Пустое сообщение.")
    text = text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        return _error(
            400, "too_long", f"Сообщение длиннее {MAX_TEXT_LENGTH} символов — сократи его."
        )

    retry_after = app[LIMITER].hit(user.id)
    if retry_after is not None:
        seconds = max(1, int(retry_after))
        return _error(
            429,
            "rate_limited",
            f"Слишком много сообщений подряд. Подожди примерно {seconds} сек.",
            retry_after=seconds,
        )

    try:
        result = await app[CHAT].ask(
            user.id,
            text,
            user_name=user.first_name,
            prefer=_resolve_model(app[AI_CLIENT], payload.get("model")),
        )
    except DailyLimitExceededError as exc:
        return _error(
            429,
            "daily_limit",
            f"Достигнут дневной лимит запросов к ИИ ({exc.limit}). Он обновится в 00:00 по UTC.",
        )
    except AllProvidersFailedError as exc:
        logger.error("All AI providers failed (%s)", exc.kind)
        return _error(503, f"ai_{exc.kind}", _AI_FAILURE_TEXTS[exc.kind])

    usage = app[USAGE]
    return web.json_response(
        {
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
    )


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


async def post_clear(request: web.Request) -> web.Response:
    user: WebAppUser = request[_USER_KEY]
    deleted = await request.app[MEMORY].clear_history(user.id)
    return web.json_response({"deleted": deleted})


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
) -> web.Application:
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

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/state", get_state)
    app.router.add_post("/api/chat", post_chat)
    app.router.add_post("/api/clear", post_clear)
    app.router.add_post("/api/preferences", post_preferences)
    app.router.add_post("/api/memory", post_memory)
    app.router.add_post("/api/memory/add", post_memory_add)
    app.router.add_post("/api/memory/delete", post_memory_delete)
    app.router.add_post("/api/timezone", post_timezone)
    app.router.add_post("/api/reminders/add", post_reminder_add)
    app.router.add_post("/api/reminders/delete", post_reminder_delete)
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

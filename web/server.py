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
from services.preferences import PreferencesStore
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

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/state", get_state)
    app.router.add_post("/api/chat", post_chat)
    app.router.add_post("/api/clear", post_clear)
    app.router.add_post("/api/preferences", post_preferences)
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

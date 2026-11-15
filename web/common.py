"""Общее для HTTP API Mini App: ответы с ошибкой, заголовки безопасности, лимит частоты."""

from __future__ import annotations

import json
from typing import Any

from aiogram.utils.web_app import WebAppUser
from aiohttp import web

from config.settings import Settings
from services.rate_limit import SlidingWindowLimiter

SETTINGS = web.AppKey("settings", Settings)
LIMITER = web.AppKey("limiter", SlidingWindowLimiter)

_USER_KEY = web.RequestKey("tg_user", WebAppUser)

# Скрипты — только свои и SDK Telegram; ответ модели выводится как текст, но CSP
# подстраховывает на случай ошибки в разметке.
_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def _error(status: int, code: str, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message, **extra}}, status=status)


def _apply_headers(request: web.Request, headers) -> None:
    headers.setdefault("Content-Security-Policy", _CSP)
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    # Интерфейс всегда перепроверяется (WebView Telegram кеширует агрессивно),
    # ответы API не кешируются вовсе.
    is_api = request.path.startswith("/api/")
    headers.setdefault("Cache-Control", "no-store" if is_api else "no-cache")


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _rate_limited(app: web.Application, user: WebAppUser) -> web.Response | None:
    retry_after = app[LIMITER].hit(user.id)
    if retry_after is None:
        return None
    seconds = max(1, int(retry_after))
    return _error(
        429,
        "rate_limited",
        f"Слишком много сообщений подряд. Подожди примерно {seconds} сек.",
        retry_after=seconds,
    )

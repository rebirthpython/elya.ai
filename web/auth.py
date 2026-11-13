"""Проверка подлинности пользователя Mini App.

Telegram передаёт в Mini App строку initData, подписанную токеном бота. Клиент
присылает её в каждом запросе, сервер проверяет подпись и срок давности:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram.utils.web_app import WebAppUser, safe_parse_webapp_init_data

AUTH_SCHEME = "tma"


class InitDataError(Exception):
    """initData отсутствует, подделана или устарела."""


def extract_init_data(authorization: str | None) -> str:
    """Достаёт initData из заголовка `Authorization: tma <initData>`."""
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != AUTH_SCHEME or not value.strip():
        raise InitDataError("missing init data")
    return value.strip()


def verify_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int,
    now: datetime | None = None,
) -> WebAppUser:
    """Возвращает пользователя из initData с верной подписью, иначе InitDataError."""
    try:
        data = safe_parse_webapp_init_data(bot_token, init_data)
    except ValueError as exc:  # неверная подпись или неразборчивые данные
        raise InitDataError("invalid signature") from exc

    now = now or datetime.now(timezone.utc)
    auth_date = data.auth_date
    if auth_date.tzinfo is None:
        auth_date = auth_date.replace(tzinfo=timezone.utc)
    age = (now - auth_date).total_seconds()
    # Небольшой допуск на расхождение часов сервера и Telegram.
    if age > max_age or age < -300:
        raise InitDataError("init data expired")

    if data.user is None or data.user.is_bot:
        raise InitDataError("no user in init data")
    return data.user

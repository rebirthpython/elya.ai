"""Понятные пользователю сообщения об ошибках AI-клиента."""

from __future__ import annotations

from ai.client import AIError, AllProvidersFailedError, DailyLimitExceededError

FAILURE_TEXTS = {
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


def ai_error_text(exc: AIError) -> str:
    if isinstance(exc, DailyLimitExceededError):
        return (
            f"📵 Достигнут дневной лимит запросов к ИИ ({exc.limit}). "
            "Он обновится в 00:00 по UTC. Это защита от перерасхода бесплатной квоты."
        )
    if isinstance(exc, AllProvidersFailedError):
        return FAILURE_TEXTS[exc.kind]
    return FAILURE_TEXTS["unavailable"]

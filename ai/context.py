"""Сборка контекста для модели с ограничением размера окна."""

from __future__ import annotations

from ai.providers.base import ChatMessage


def build_context(
    system_prompt: str,
    history: list[ChatMessage],
    user_text: str,
    *,
    max_messages: int,
    max_chars: int,
) -> list[ChatMessage]:
    """Возвращает [system, ...последние сообщения истории..., текущее сообщение].

    Из истории (в хронологическом порядке) берутся самые свежие сообщения, пока
    суммарная длина не превысит max_chars (грубая, но дешёвая оценка токенов).
    Текущее сообщение пользователя включается всегда. Контекст после system
    всегда начинается с реплики пользователя.
    """
    budget = max_chars - len(user_text)
    selected: list[ChatMessage] = []
    for message in reversed(history[-max_messages:] if max_messages > 0 else []):
        if len(message.content) > budget:
            break
        selected.append(message)
        budget -= len(message.content)
    selected.reverse()

    while selected and selected[0].role != "user":
        selected.pop(0)

    return [
        ChatMessage(role="system", content=system_prompt),
        *selected,
        ChatMessage(role="user", content=user_text),
    ]

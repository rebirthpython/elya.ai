"""Строка со скоростью ответа под сообщением: токены в секунду и задержка до первого слова."""

from __future__ import annotations

from ai.providers.base import CompletionResult

# Короче этого генерация слишком быстрая, чтобы скорость была осмысленной (ответ пришёл одним куском).
_MIN_GENERATION_S = 0.4


def _short_model(model: str) -> str:
    # «_» и «*» в имени сломали бы курсив вокруг строки.
    return model.split("/")[-1].removesuffix(":free").replace("_", "-").replace("*", "")


def speed_line(result: CompletionResult, *, with_model: bool = True) -> str:
    """«⚡ ~85 ток/с · 1.2 с до ответа · gpt-oss-120b»; пустая строка — замеров нет (напоминания,
    ответ целиком одним куском)."""
    if result.tokens <= 0 or result.first_token_s <= 0:
        return ""
    parts = []
    if result.generation_s >= _MIN_GENERATION_S:
        rate = result.tokens / result.generation_s
        parts.append(f"{'~' if result.tokens_estimated else ''}{rate:.0f} ток/с")
    parts.append(f"{result.first_token_s:.1f} с до ответа")
    if with_model and result.model:
        parts.append(_short_model(result.model))
    return "⚡ " + " · ".join(parts)

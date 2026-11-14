"""Разбор JSON из ответа модели и приведение полей к нужным типам."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


class ModelOutputError(Exception):
    """Ответ модели не удалось разобрать или он не подходит по структуре."""


def extract_json_object(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа: модели часто оборачивают его в ``` или пишут текст вокруг."""
    candidates: list[str] = []
    if fence := _FENCE_RE.search(text):
        candidates.append(fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        for variant in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
            try:
                data = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    raise ModelOutputError("ответ не является JSON-объектом")


def as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def str_list(value: Any) -> list[str]:
    return [text for item in as_list(value) if (text := as_str(item))]


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def shorten(text: str, limit: int) -> str:
    """Обрезает по границе слова с многоточием (последняя страховка, если модель не уложилась)."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or text[: limit - 1]) + "…"

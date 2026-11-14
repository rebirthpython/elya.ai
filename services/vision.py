"""Фото в диалоге: проверка картинки, data:-URL для модели и текст для истории."""

from __future__ import annotations

import base64

MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_QUESTION = "Что на этом фото? Опиши коротко и по делу."
NO_VISION_TEXT = (
    "Сейчас ни одна подключённая модель не умеет смотреть фото. Владельцу бота нужно "
    "подключить Gemini (GEMINI_API_KEY) — его модели понимают картинки."
)


class ImageError(ValueError):
    """Файл не похож на картинку или слишком большой."""


def sniff_image(data: bytes) -> str | None:
    """MIME-тип по первым байтам: JPEG, PNG, WebP, GIF; None — не картинка."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def to_data_url(data: bytes) -> str:
    """Проверяет картинку и превращает её в data:-URL для OpenAI-совместимого API."""
    if not data:
        raise ImageError("empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("too_large")
    mime = sniff_image(data)
    if mime is None:
        raise ImageError("not_image")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def history_text(caption: str) -> str:
    """Что сохраняется в истории вместо картинки (сама картинка не хранится)."""
    caption = caption.strip()
    return f"📷 Фото: {caption}" if caption else "📷 Фото"

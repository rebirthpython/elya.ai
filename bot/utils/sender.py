"""Отправка длинных ответов модели с разбиением и безопасным форматированием."""

from __future__ import annotations

import logging

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions, Message

from bot.utils.formatting import markdown_to_html
from bot.utils.text import split_text

logger = logging.getLogger(__name__)
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_long_text(message: Message, text: str) -> None:
    """Отправляет текст частями; если Telegram не принял HTML — отправляет как есть."""
    for chunk in split_text(text):
        try:
            await message.answer(
                markdown_to_html(chunk),
                parse_mode=ParseMode.HTML,
                link_preview_options=_NO_PREVIEW,
            )
        except TelegramBadRequest as exc:
            logger.debug("HTML formatting rejected, sending plain text: %s", exc)
            await message.answer(chunk, parse_mode=None, link_preview_options=_NO_PREVIEW)

"""Сообщение, которое дописывается по мере того, как модель генерирует ответ.

Пока ответ идёт, в чате виден обычный текст с курсором ▍ (без разметки: недописанный
Markdown может сломать HTML). Telegram ограничивает частоту правок, поэтому сообщение
обновляется не чаще раза в interval секунд. В конце — оформленный ответ, а если он
длиннее одного сообщения, продолжение уходит следующими сообщениями.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions, Message

from bot.utils.formatting import markdown_to_html
from bot.utils.sender import send_long_text
from bot.utils.text import DEFAULT_CHUNK_LIMIT, split_text

logger = logging.getLogger(__name__)
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
CURSOR = " ▍"


class LiveMessage:
    def __init__(
        self,
        bot: Bot,
        message: Message,
        *,
        first_delay: float = 0.8,
        interval: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._message = message
        self._first_delay = first_delay
        self._interval = interval
        self._clock = clock
        self._started = clock()
        self._last_edit = 0.0
        self._shown = ""
        self._sent: Message | None = None

    @property
    def started(self) -> bool:
        return self._sent is not None

    def _preview(self, text: str) -> str:
        text = text.strip()
        if len(text) > DEFAULT_CHUNK_LIMIT:  # остальное придёт отдельными сообщениями в конце
            text = text[:DEFAULT_CHUNK_LIMIT] + "…"
        return text + CURSOR

    async def update(self, text: str) -> None:
        """Новый текст ответа целиком (не прирост)."""
        now = self._clock()
        if not text.strip():
            return
        if self._sent is None:
            if now - self._started < self._first_delay:
                return  # короткий ответ успеет прийти целиком — не мигаем лишним сообщением
            preview = self._preview(text)
            try:
                self._sent = await self._message.answer(preview, parse_mode=None, link_preview_options=_NO_PREVIEW)
            except TelegramAPIError as exc:
                logger.debug("Live message not sent: %s", exc)
                return
            self._shown, self._last_edit = preview, now
            return
        if now - self._last_edit < self._interval:
            return
        preview = self._preview(text)
        if preview != self._shown:
            await self._edit(preview, None)
            self._shown, self._last_edit = preview, now

    async def finish(self, text: str) -> None:
        """Окончательный ответ с оформлением."""
        if self._sent is None:
            await send_long_text(self._message, text)
            return
        chunks = split_text(text)
        first, rest = (chunks[0], chunks[1:]) if chunks else (text, [])
        if not await self._edit(markdown_to_html(first), ParseMode.HTML):
            await self._edit(first, None)  # Telegram не принял разметку — оставляем текст
        for chunk in rest:
            await send_long_text(self._message, chunk)

    async def fail(self, notice: str) -> None:
        """Ответ оборвался: дописываем пояснение к уже показанному тексту или шлём его отдельно."""
        if self._sent is None:
            await self._message.answer(notice)
            return
        partial = self._shown.removesuffix(CURSOR)
        await self._edit(f"{partial}\n\n{notice}", None)

    async def _edit(self, text: str, parse_mode: str | None) -> bool:
        try:
            await self._bot.edit_message_text(
                text=text,
                chat_id=self._sent.chat.id,
                message_id=self._sent.message_id,
                parse_mode=parse_mode,
                link_preview_options=_NO_PREVIEW,
            )
            return True
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            logger.debug("Live edit rejected: %s", exc)
            return False
        except TelegramAPIError as exc:
            logger.debug("Live edit failed: %s", exc)
            return False

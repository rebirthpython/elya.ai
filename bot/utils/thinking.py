"""Сообщение о размышлении в режиме глубокого размышления.

Пока модель думает, в чате видно «💭 Размышляю… 12 с» и последние строки её черновика
(обновляется не чаще раза в interval секунд — Telegram ограничивает частоту правок).
Когда размышление закончено, сообщение превращается в «💭 Размышляла 23 с» со свёрнутой
цитатой: нажатие разворачивает весь ход мыслей. Ответ приходит следующим сообщением.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from html import escape

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions, Message

from services.thinking import clip_middle, format_seconds

logger = logging.getLogger(__name__)
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Сообщение Telegram — до 4096 символов; остаток — под заголовок.
REASONING_DISPLAY_CHARS = 3800
PREVIEW_CHARS = 350

_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[*-][ \t]+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"\*\*|__")


def plain_reasoning(text: str) -> str:
    """Черновик без Markdown-разметки: в цитате Telegram звёздочки и решётки лишь мешают."""
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub(r"\1• ", text)
    return _EMPHASIS_RE.sub("", text).strip()


def _tail(text: str, limit: int = PREVIEW_CHARS) -> str:
    """Последние строки черновика, начиная с целого слова."""
    text = plain_reasoning(text)
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    breaks = [i for i in (tail.find("\n"), tail.find(" ")) if 0 <= i < limit // 2]
    return "…" + (tail[min(breaks) + 1:] if breaks else tail)


class ThinkingMessage:
    def __init__(
        self,
        bot: Bot,
        message: Message,
        *,
        interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._message = message
        self._interval = interval
        self._clock = clock
        self._started = 0.0
        self._last_edit = 0.0
        self._text = ""
        self._shown = ""
        self._sent: Message | None = None
        self._active = False
        self._done = False

    @property
    def active(self) -> bool:
        """Размышление идёт и ещё не закончено."""
        return self._active and not self._done

    async def update(self, delta: str) -> None:
        """Новый кусок черновика; первый вызов показывает «Размышляю…»."""
        if self._done:
            return
        now = self._clock()
        self._text += delta
        if not self._active:
            self._active = True
            self._started = self._last_edit = now
            self._shown = self._progress(now)
            try:
                self._sent = await self._message.answer(
                    self._shown, parse_mode=ParseMode.HTML, link_preview_options=_NO_PREVIEW
                )
            except TelegramAPIError as exc:
                logger.debug("Thinking message not sent: %s", exc)
            return
        if self._sent is None or now - self._last_edit < self._interval:
            return
        text = self._progress(now)
        if text != self._shown and await self._edit(text, ParseMode.HTML):
            self._shown, self._last_edit = text, now

    async def finish(self, reasoning: str, seconds: float) -> None:
        """Размышление закончено: заголовок со временем и свёрнутый ход мыслей."""
        if self._done:
            return
        self._done = True
        title = f"💭 Размышляла {format_seconds(seconds)}"
        body = clip_middle(plain_reasoning(reasoning), REASONING_DISPLAY_CHARS)
        if not body:
            await self._put(title, None)
            return
        html = (
            f"💭 <b>Размышляла {format_seconds(seconds)}</b>\n"
            f"<blockquote expandable>{escape(body, quote=False)}</blockquote>"
        )
        if not await self._put(html, ParseMode.HTML):
            await self._put(f"{title}\n\n{body}", None)

    async def abort(self) -> None:
        """Ответ не получился: убираем «Размышляю…», чтобы статус не висел вечно."""
        if not self.active:
            return
        self._done = True
        if self._sent is not None:
            await self._edit("💭 Размышление прервано.", None)

    def _progress(self, now: float) -> str:
        header = "💭 <b>Размышляю…</b>"
        elapsed = now - self._started
        if elapsed >= 1:
            header += f" {format_seconds(elapsed)}"
        tail = _tail(self._text)
        return f"{header}\n<blockquote>{escape(tail, quote=False)}</blockquote>" if tail else header

    async def _put(self, text: str, parse_mode: str | None) -> bool:
        """Правит сообщение о размышлении, а если его нет — отправляет новое."""
        if self._sent is not None:
            return await self._edit(text, parse_mode)
        try:
            self._sent = await self._message.answer(
                text, parse_mode=parse_mode, link_preview_options=_NO_PREVIEW
            )
            return True
        except TelegramAPIError as exc:
            logger.debug("Thinking message not sent: %s", exc)
            return False

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
            logger.debug("Thinking edit rejected: %s", exc)
            return False
        except TelegramAPIError as exc:
            logger.debug("Thinking edit failed: %s", exc)
            return False

"""Преобразование базового Markdown от модели в HTML-разметку Telegram."""

from __future__ import annotations

import html
import re

_FENCE_RE = re.compile(r"```(?:[\w+#.-]*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)\"<>]+)\)")
_HEADER_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[*-][ \t]+", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOLD_UNDERSCORE_RE = re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def markdown_to_html(text: str) -> str:
    """Поддерживает ```код```, `код`, **жирный**, *курсив*, ~~зачёркнутый~~,
    заголовки, списки и ссылки. Всё остальное экранируется."""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = text.replace("\x00", "")
    text = _FENCE_RE.sub(lambda m: keep(f"<pre>{_escape(m.group(1).strip(chr(10)))}</pre>"), text)
    text = _INLINE_CODE_RE.sub(lambda m: keep(f"<code>{_escape(m.group(1))}</code>"), text)
    text = _escape(text)
    text = _LINK_RE.sub(lambda m: keep(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text)

    text = _HEADER_RE.sub(r"<b>\1</b>", text)
    text = _BULLET_RE.sub(r"\1• ", text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _BOLD_UNDERSCORE_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _ITALIC_STAR_RE.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDERSCORE_RE.sub(r"<i>\1</i>", text)

    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)

"""Разбиение длинного текста на части под лимит Telegram (4096 символов)."""

from __future__ import annotations

TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_CHUNK_LIMIT = 4000  # запас под служебные символы
_FENCE = "```"
_SEPARATORS = ("\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ")


def _find_cut(text: str, budget: int) -> int:
    """Позиция разреза <= budget: по абзацу, строке, предложению, слову или жёстко."""
    window = text[:budget]
    min_pos = budget // 3  # не создавать слишком мелкие куски
    for sep in _SEPARATORS:
        idx = window.rfind(sep)
        if idx >= min_pos:
            # Знак препинания остаётся в текущем куске.
            return idx if sep.startswith("\n") or sep == " " else idx + 1
    return budget


def split_text(text: str, limit: int = DEFAULT_CHUNK_LIMIT) -> list[str]:
    """Делит текст на части длиной <= limit, сохраняя парность блоков кода ```."""
    if limit < 100:
        raise ValueError("limit is too small")

    rest = text.strip()
    chunks: list[str] = []
    reopen = ""
    while rest:
        rest = reopen + rest
        if len(rest) <= limit:
            chunks.append(rest)
            break

        cut = _find_cut(rest, limit - len(_FENCE) - 1)
        chunk = rest[:cut].rstrip()
        rest = rest[cut:]
        rest = rest.lstrip("\n") if rest.startswith("\n") else rest.removeprefix(" ")

        if chunk.count(_FENCE) % 2 == 1:  # разрез внутри блока кода
            chunk += "\n" + _FENCE
            reopen = _FENCE + "\n"
        else:
            reopen = ""
        if chunk.strip():
            chunks.append(chunk)
    return chunks

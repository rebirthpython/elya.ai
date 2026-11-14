"""Инструкции пользователя: что Эле знать о нём и как отвечать.

Пользователь пишет их один раз в настройках, и они добавляются к каждому разговору —
в приложении и в чате с ботом, чтобы не повторять одно и то же для каждой задачи.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from database.models import UserInstructions

MAX_INSTRUCTION_LENGTH = 1500


@dataclass(frozen=True, slots=True)
class Instructions:
    about: str = ""
    style: str = ""
    enabled: bool = True

    @property
    def is_empty(self) -> bool:
        return not self.about and not self.style

    @property
    def active(self) -> bool:
        return self.enabled and not self.is_empty

    def as_dict(self) -> dict:
        return {"about": self.about, "style": self.style, "enabled": self.enabled}

    def prompt_block(self) -> str:
        """Раздел системного промпта; пустой, если инструкции выключены или не заданы."""
        if not self.active:
            return ""
        parts = [
            "\nИнструкции, которые собеседник сам задал для всех разговоров. Следуй им: они "
            "важнее выбранного стиля и полноты ответа, но не важнее правил выше. Не пересказывай "
            "их и не упоминай без повода."
        ]
        if self.about:
            parts.append(f"Что знать о собеседнике:\n{self.about}")
        if self.style:
            parts.append(f"Как отвечать:\n{self.style}")
        return "\n".join(parts) + "\n"


def clean_instruction(text: str) -> str:
    """Убирает лишние пробелы и пустые строки, сохраняя абзацы."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class InstructionsStore:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def get(self, user_id: int) -> Instructions:
        async with self._session_factory() as session:
            row = await session.get(UserInstructions, user_id)
        if row is None:
            return Instructions()
        return Instructions(about=row.about or "", style=row.style or "", enabled=bool(row.enabled))

    async def update(
        self,
        user_id: int,
        *,
        about: str | None = None,
        style: str | None = None,
        enabled: bool | None = None,
    ) -> Instructions:
        """Меняет переданные поля. ValueError("too_long") — текст длиннее лимита."""
        values = {}
        for name, value in (("about", about), ("style", style)):
            if value is not None:
                cleaned = clean_instruction(value)
                if len(cleaned) > MAX_INSTRUCTION_LENGTH:
                    raise ValueError("too_long")
                values[name] = cleaned
        async with self._session_factory.begin() as session:
            row = await session.get(UserInstructions, user_id)
            if row is None:
                row = UserInstructions(user_id=user_id, about="", style="", enabled=True)
                session.add(row)
            for name, value in values.items():
                setattr(row, name, value)
            if enabled is not None:
                row.enabled = enabled
            return Instructions(about=row.about, style=row.style, enabled=row.enabled)

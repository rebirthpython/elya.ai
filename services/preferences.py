"""Настройки общения: стиль и подробность ответов. Хранятся в БД на пользователя,
поэтому действуют и в Mini App, и в чате с ботом."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from database.models import UserPreferences

# id → (название, пояснение). Тексты для модели — в ai/prompts.py.
STYLES: dict[str, tuple[str, str]] = {
    "friendly": ("Дружелюбный", "Тепло, с лёгким юмором"),
    "caring": ("Заботливый", "Мягко, с поддержкой и вниманием"),
    "playful": ("Весёлый", "Шутки, игра слов и эмодзи"),
    "business": ("Деловой", "Сдержанно и по существу, на «вы»"),
}

VERBOSITY: dict[str, tuple[str, str]] = {
    "short": ("Кратко", "1–3 предложения"),
    "normal": ("Обычно", "По ситуации"),
    "detailed": ("Подробно", "С объяснениями и примерами"),
}

DEFAULT_STYLE = "friendly"
DEFAULT_VERBOSITY = "normal"


@dataclass(frozen=True, slots=True)
class Preferences:
    style: str = DEFAULT_STYLE
    verbosity: str = DEFAULT_VERBOSITY

    def as_dict(self) -> dict[str, str]:
        return {"style": self.style, "verbosity": self.verbosity}


def options() -> dict[str, list[dict[str, str]]]:
    """Варианты для интерфейса."""
    return {
        "style": [{"id": k, "title": t, "description": d} for k, (t, d) in STYLES.items()],
        "verbosity": [{"id": k, "title": t, "description": d} for k, (t, d) in VERBOSITY.items()],
    }


class PreferencesStore:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory

    async def get(self, user_id: int) -> Preferences:
        async with self._session_factory() as session:
            row = await session.get(UserPreferences, user_id)
        if row is None:
            return Preferences()
        # Значение, которого больше нет в списке, заменяется на стандартное.
        return Preferences(
            style=row.style if row.style in STYLES else DEFAULT_STYLE,
            verbosity=row.verbosity if row.verbosity in VERBOSITY else DEFAULT_VERBOSITY,
        )

    async def update(
        self, user_id: int, *, style: str | None = None, verbosity: str | None = None
    ) -> Preferences:
        """Меняет переданные поля. ValueError — неизвестное значение."""
        if style is not None and style not in STYLES:
            raise ValueError(f"unknown style {style!r}")
        if verbosity is not None and verbosity not in VERBOSITY:
            raise ValueError(f"unknown verbosity {verbosity!r}")

        async with self._session_factory.begin() as session:
            row = await session.get(UserPreferences, user_id)
            if row is None:
                row = UserPreferences(
                    user_id=user_id, style=DEFAULT_STYLE, verbosity=DEFAULT_VERBOSITY
                )
                session.add(row)
            if style is not None:
                row.style = style
            if verbosity is not None:
                row.verbosity = verbosity
            return Preferences(style=row.style, verbosity=row.verbosity)

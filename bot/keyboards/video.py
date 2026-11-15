"""Кнопки монтажа: частые действия без запроса к модели, отмена правки и «Готово»."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from video.plan import EditPlan


class VideoCallback(CallbackData, prefix="video"):
    action: str  # quick | undo | done
    value: str = ""  # для quick: pauses | subs | vertical | circle | music | join
    rev: int = 0  # номер версии: кнопки под старыми результатами не срабатывают


def panel_keyboard(
    plan: EditPlan | None,
    *,
    rev: int,
    clips: int,
    has_music: bool,
    can_subtitles: bool,
    can_undo: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    def quick(text: str, value: str) -> None:
        builder.button(text=text, callback_data=VideoCallback(action="quick", value=value, rev=rev))

    quick("🗣 Вернуть паузы" if plan and plan.remove_pauses else "🤐 Убрать паузы", "pauses")
    if can_subtitles:
        quick("💬 Без субтитров" if plan and plan.subtitles else "💬 Субтитры", "subs")
    quick("🖥 Исходный формат" if plan and plan.format == "9:16" else "📱 Вертикально 9:16", "vertical")
    quick("🎬 Обычное видео" if plan and plan.output == "circle" else "⭕ Кружочек", "circle")
    if has_music:
        quick("🔇 Без музыки" if plan and plan.music else "🎵 Наложить музыку", "music")
    if plan is None and clips > 1:
        quick("🔗 Просто склеить", "join")
    if can_undo:
        builder.button(text="↩️ Отменить", callback_data=VideoCallback(action="undo", rev=rev))
    builder.button(
        text="✅ Готово" if plan else "✖️ Выйти", callback_data=VideoCallback(action="done", rev=rev)
    )
    builder.adjust(2)
    return builder.as_markup()

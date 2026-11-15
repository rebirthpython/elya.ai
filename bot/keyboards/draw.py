"""Кнопки под нарисованной картинкой."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class DrawCallback(CallbackData, prefix="draw"):
    action: str  # again | edit
    id: int


def drawing_keyboard(drawing_id: int, *, editable: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Ещё вариант", callback_data=DrawCallback(action="again", id=drawing_id))
    if editable:
        builder.button(text="✏️ Изменить", callback_data=DrawCallback(action="edit", id=drawing_id))
    return builder.as_markup()

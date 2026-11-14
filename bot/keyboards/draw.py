"""Кнопки под нарисованной картинкой."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class DrawCallback(CallbackData, prefix="draw"):
    action: str  # again
    id: int


def drawing_keyboard(drawing_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Ещё вариант", callback_data=DrawCallback(action="again", id=drawing_id))
    return builder.as_markup()

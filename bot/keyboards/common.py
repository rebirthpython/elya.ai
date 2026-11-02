"""Общие inline-клавиатуры."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ClearHistoryCallback(CallbackData, prefix="clear"):
    confirm: bool


def clear_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, очистить", callback_data=ClearHistoryCallback(confirm=True))
    builder.button(text="Отмена", callback_data=ClearHistoryCallback(confirm=False))
    builder.adjust(2)
    return builder.as_markup()

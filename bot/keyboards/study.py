"""Кнопки для конспектов и презентаций."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from study.themes import THEMES


class StudyCallback(CallbackData, prefix="study"):
    action: str  # notes | outline | theme | replan | cancel
    value: str = ""


def _cancel(builder: InlineKeyboardBuilder) -> None:
    builder.button(text="✖️ Отмена", callback_data=StudyCallback(action="cancel"))


def notes_modes_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Подробный конспект", callback_data=StudyCallback(action="notes", value="full"))
    builder.button(text="✂️ Кратко", callback_data=StudyCallback(action="notes", value="short"))
    builder.button(text="🔍 Главные детали", callback_data=StudyCallback(action="notes", value="details"))
    _cancel(builder)
    builder.adjust(1, 2, 1)
    return builder.as_markup()


def slides_start_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Составить план", callback_data=StudyCallback(action="outline"))
    _cancel(builder)
    builder.adjust(1)
    return builder.as_markup()


def outline_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for theme in THEMES.values():
        builder.button(
            text=f"🎨 {theme.title}", callback_data=StudyCallback(action="theme", value=theme.key)
        )
    builder.button(text="🔄 Другой план", callback_data=StudyCallback(action="replan"))
    _cancel(builder)
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()

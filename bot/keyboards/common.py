"""Общие inline-клавиатуры."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ClearHistoryCallback(CallbackData, prefix="clear"):
    confirm: bool


def clear_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, очистить", callback_data=ClearHistoryCallback(confirm=True))
    builder.button(text="Отмена", callback_data=ClearHistoryCallback(confirm=False))
    builder.adjust(2)
    return builder.as_markup()


def open_app_keyboard(webapp_url: str, assistant_name: str) -> InlineKeyboardMarkup:
    """Кнопка, открывающая Mini App (работает только в личном чате с ботом)."""
    button = InlineKeyboardButton(
        text=f"✨ Открыть приложение «{assistant_name}»", web_app=WebAppInfo(url=webapp_url)
    )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])

"""Кнопки напоминаний: отложить, готово, удалить."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ReminderCallback(CallbackData, prefix="rem"):
    action: str  # snooze | done | del
    id: int
    minutes: int = 0


def fired_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Под сработавшим напоминанием: отложить или отметить выполненным."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⏰ +10 мин", callback_data=ReminderCallback(action="snooze", id=reminder_id, minutes=10))
    builder.button(text="⏰ +1 час", callback_data=ReminderCallback(action="snooze", id=reminder_id, minutes=60))
    builder.button(text="✅ Готово", callback_data=ReminderCallback(action="done", id=reminder_id))
    builder.adjust(2, 1)
    return builder.as_markup()


def list_keyboard(items: list[tuple[int, int]]) -> InlineKeyboardMarkup | None:
    """items — пары (номер в списке, id напоминания): по кнопке «🗑 N» на каждое."""
    if not items:
        return None
    builder = InlineKeyboardBuilder()
    for number, reminder_id in items:
        builder.button(text=f"🗑 {number}", callback_data=ReminderCallback(action="del", id=reminder_id))
    builder.adjust(5)
    return builder.as_markup()

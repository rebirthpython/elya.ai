"""Напоминания в боте: /remind, /reminders, /timezone, кнопки и доставка."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ai.client import AIError
from bot.keyboards.reminders import ReminderCallback, fired_keyboard, list_keyboard
from services.reminders import (
    HELP_EXAMPLE,
    NEED_TIMEZONE,
    REPEAT_TITLES,
    DeliveryError,
    ReminderItem,
    Reminders,
    describe_due,
)

logger = logging.getLogger(__name__)

_UNAVAILABLE = "Напоминания сейчас недоступны."
_AI_DOWN = (
    "Сейчас не получается разобрать просьбу: ИИ-сервис не отвечает или исчерпан лимит. "
    "Попробуй чуть позже или добавь напоминание в приложении (кнопка 🔔)."
)


def make_sender(bot: Bot):
    """Функция доставки для планировщика: отправляет напоминание в личный чат."""

    async def send(item: ReminderItem, late_note: str | None) -> None:
        text = f"⏰ <b>Напоминание</b>\n{escape(item.text)}"
        if late_note:
            text += f"\n\n<i>{escape(late_note)}</i>"
        try:
            await bot.send_message(
                item.user_id, text, parse_mode=ParseMode.HTML, reply_markup=fired_keyboard(item.id)
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            # Бот заблокирован или чат недоступен — повторять бессмысленно.
            raise DeliveryError(str(exc), permanent=True) from exc
        except TelegramAPIError as exc:  # сеть, лимиты Telegram и т. п. — повторим позже
            raise DeliveryError(str(exc)) from exc

    return send


async def _list_text(reminders: Reminders, user_id: int) -> tuple[str, list[tuple[int, int]]]:
    items = await reminders.list_pending(user_id)
    if not items:
        return (
            "Напоминаний нет. Напиши, например: " + HELP_EXAMPLE + ".",
            [],
        )
    tz = await reminders.tzinfo(user_id)
    now = reminders.now()
    lines = []
    for number, item in enumerate(items[:20], start=1):
        when = describe_due(item.due_at, tz, now) if tz else item.due_at.strftime("%d.%m %H:%M UTC")
        repeat = f" 🔁 {REPEAT_TITLES[item.repeat]}" if item.repeat in REPEAT_TITLES else ""
        lines.append(f"{number}. <b>{escape(when)}</b>{repeat}\n    {escape(item.text)}")
    text = "⏰ <b>Твои напоминания</b>\n\n" + "\n".join(lines)
    if len(items) > 20:
        text += f"\n\n<i>…и ещё {len(items) - 20}. Остальные — в приложении.</i>"
    if not tz:
        text += "\n\n<i>Часовой пояс не задан: /timezone Europe/Moscow</i>"
    return text, [(n, i.id) for n, i in enumerate(items[:10], start=1)]


async def cmd_reminders(message: Message, reminders: Reminders | None) -> None:
    if reminders is None or message.from_user is None:
        await message.answer(_UNAVAILABLE)
        return
    text, buttons = await _list_text(reminders, message.from_user.id)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=list_keyboard(buttons))


async def cmd_remind(message: Message, command: CommandObject, reminders: Reminders | None) -> None:
    if reminders is None or message.from_user is None:
        await message.answer(_UNAVAILABLE)
        return
    request = (command.args or "").strip()
    if not request:
        await message.answer(
            f"Просто напиши мне обычным сообщением, например: {HELP_EXAMPLE}. "
            "Можно и с повтором: «напоминай каждый день в 8 пить воду». "
            "Список — /reminders, часовой пояс — /timezone."
        )
        return
    try:
        reply = await reminders.handle_chat_request(message.from_user.id, "напомни " + request)
    except AIError:
        await message.answer(_AI_DOWN)
        return
    await message.answer(reply or f"Не поняла, что напомнить. Пример: {HELP_EXAMPLE}.")


async def cmd_timezone(message: Message, command: CommandObject, reminders: Reminders | None) -> None:
    if reminders is None or message.from_user is None:
        await message.answer(_UNAVAILABLE)
        return
    user_id = message.from_user.id
    argument = (command.args or "").strip()
    if not argument:
        current = await reminders.get_timezone(user_id)
        head = f"Твой часовой пояс: {current}." if current else "Часовой пояс пока не задан."
        await message.answer(
            head + "\nЧтобы поменять, отправь, например: /timezone Europe/Moscow, "
            "/timezone Asia/Almaty или /timezone UTC+3. В приложении он определяется сам."
        )
        return
    saved = await reminders.set_timezone(user_id, argument)
    if saved is None:
        await message.answer(
            "Не знаю такого часового пояса. Примеры: Europe/Moscow, Asia/Yekaterinburg, "
            "Europe/Minsk, Asia/Almaty или смещение вроде UTC+3."
        )
        return
    await message.answer(f"Готово, часовой пояс: {saved}.")


async def on_reminder_action(
    callback: CallbackQuery, callback_data: ReminderCallback, reminders: Reminders | None
) -> None:
    if reminders is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user_id = callback.from_user.id
    message = callback.message
    action = callback_data.action

    if action == "del":
        await reminders.delete(user_id, callback_data.id)
        text, buttons = await _list_text(reminders, user_id)
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=list_keyboard(buttons))
        await callback.answer("Удалила")
        return

    if action == "done":
        await message.edit_text(message.html_text + "\n\n✅ Готово", parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    if action == "snooze":
        minutes = callback_data.minutes if callback_data.minutes in (10, 60) else 10
        item = await reminders.snooze(user_id, callback_data.id, minutes)
        if item is None:
            await message.edit_text(message.html_text, parse_mode=ParseMode.HTML)
            await callback.answer("Не нашла это напоминание")
            return
        label = "10 минут" if minutes == 10 else "час"
        await message.edit_text(
            message.html_text + f"\n\n💤 Напомню ещё раз через {label}", parse_mode=ParseMode.HTML
        )
        await callback.answer("Отложила")
        return
    await callback.answer()


def create_router() -> Router:
    router = Router(name="reminders")
    router.message.register(cmd_reminders, Command("reminders"))
    router.message.register(cmd_remind, Command("remind"))
    router.message.register(cmd_timezone, Command("timezone"))
    router.callback_query.register(on_reminder_action, ReminderCallback.filter())
    return router

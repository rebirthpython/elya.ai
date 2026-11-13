"""Команды: /start, /help, /clear, /cancel и неизвестные команды."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ai.client import AIClient
from bot.keyboards.common import (
    ClearHistoryCallback,
    MemoryCallback,
    clear_confirm_keyboard,
    forget_confirm_keyboard,
    memory_keyboard,
    open_app_keyboard,
)
from config.settings import Settings
from memory.manager import MemoryManager
from services.longterm import LongTermMemory
from services.usage import UsageTracker


def _privacy_note(ai_client: AIClient) -> str:
    providers = escape(", ".join(ai_client.provider_titles))
    return (
        "🔒 <b>О приватности.</b> Твои сообщения и недавняя история диалога отправляются "
        f"для ответа во внешний ИИ-сервис: <i>{providers}</i>. На бесплатных тарифах "
        "провайдеры могут использовать данные для улучшения своих моделей, поэтому "
        "не присылай пароли, номера карт и другие секреты. История хранится только в базе "
        "этого бота, её можно удалить командой /clear."
    )


async def cmd_start(
    message: Message, state: FSMContext, settings: Settings, ai_client: AIClient
) -> None:
    await state.clear()
    first_name = message.from_user.first_name if message.from_user else ""
    greeting = f"Привет, {escape(first_name)}!" if first_name else "Привет!"
    app_note = (
        "У меня есть удобное приложение прямо в Telegram — открой его кнопкой ниже "
        "или пиши здесь, память у нас общая.\n\n"
        if settings.webapp_enabled
        else ""
    )
    await message.answer(
        f"{greeting} Я — {escape(settings.assistant_name)}, твой персональный ИИ-ассистент. 👋\n\n"
        "Просто пиши мне обычными сообщениями: могу ответить на вопрос, помочь с учёбой, "
        "спланировать день или просто поболтать. Я помню контекст нашего разговора.\n\n"
        f"{app_note}"
        f"{_privacy_note(ai_client)}\n\n"
        "Все команды — в /help.",
        parse_mode=ParseMode.HTML,
        reply_markup=(
            open_app_keyboard(settings.webapp_url, settings.assistant_name)
            if settings.webapp_enabled
            else None
        ),
    )


async def cmd_help(
    message: Message, settings: Settings, memory: MemoryManager, usage: UsageTracker
) -> None:
    used = await usage.used_today()
    limit = str(usage.daily_limit) if usage.daily_limit else "∞"
    history = await memory.history_size(message.from_user.id) if message.from_user else 0
    await message.answer(
        f"<b>Что умеет {escape(settings.assistant_name)}</b>\n\n"
        "💬 Отвечать на вопросы и поддерживать диалог с учётом контекста\n"
        "📚 Помогать с учёбой: объяснить тему, проверить текст, составить план\n"
        "🗓 Помогать с планированием и организацией дел\n\n"
        "<b>Команды</b>\n"
        "/start — приветствие\n"
        "/help — эта справка\n"
        "/memory — что я о тебе помню (и включить или выключить память)\n"
        "/forget — забыть всё, что я о тебе помню\n"
        "/clear — очистить историю диалога\n"
        "/cancel — отменить текущее действие\n\n"
        f"📊 Сообщений в истории: {history}\n"
        f"📈 Запросов к ИИ сегодня (UTC): {used} из {limit}\n\n"
        "<i>Скоро: долговременная память, настройки стиля, напоминания, голосовые и фото.</i>",
        parse_mode=ParseMode.HTML,
    )


_MEMORY_OFF = (
    "🧠 <b>Долговременная память выключена.</b>\n\n"
    "Если включить, я буду запоминать важное о тебе (имя, близких, вкусы, планы) и помнить "
    "это между разговорами. Я не сохраняю пароли и номера карт, а всё запомненное можно "
    "посмотреть и удалить в любой момент. Данные хранятся только в базе этого бота, но при "
    "ответах они, как и история диалога, отправляются во внешний ИИ-сервис."
)


async def _memory_text(memory: LongTermMemory, user_id: int) -> tuple[str, bool, bool]:
    enabled = await memory.is_enabled(user_id)
    facts = await memory.list(user_id)
    if not enabled and not facts:
        return _MEMORY_OFF, False, False
    status = "включена" if enabled else "выключена"
    if facts:
        body = "\n".join(f"• {escape(fact.text)}" for fact in facts)
    else:
        body = "<i>Пока ничего не запомнила. Расскажи что-нибудь о себе.</i>"
    text = f"🧠 <b>Память {status}.</b> Вот что я о тебе помню:\n\n{body}"
    if not enabled:
        text += "\n\n<i>Пока память выключена, я этим не пользуюсь и ничего нового не запоминаю.</i>"
    return text, enabled, bool(facts)


async def cmd_memory(message: Message, long_memory: LongTermMemory | None) -> None:
    if long_memory is None or message.from_user is None:
        await message.answer("Долговременная память сейчас недоступна.")
        return
    text, enabled, has_facts = await _memory_text(long_memory, message.from_user.id)
    await message.answer(
        text, parse_mode=ParseMode.HTML, reply_markup=memory_keyboard(enabled, has_facts)
    )


async def cmd_forget(message: Message, long_memory: LongTermMemory | None) -> None:
    if long_memory is None or message.from_user is None:
        await message.answer("Долговременная память сейчас недоступна.")
        return
    if not await long_memory.count(message.from_user.id):
        await message.answer("Мне пока нечего забывать: я ничего о тебе не храню.")
        return
    await message.answer(
        "Забыть всё, что я о тебе помню? Это нельзя отменить.",
        reply_markup=forget_confirm_keyboard(),
    )


async def on_memory_action(
    callback: CallbackQuery, callback_data: MemoryCallback, long_memory: LongTermMemory | None
) -> None:
    if long_memory is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    user_id = callback.from_user.id
    if callback_data.action == "toggle":
        await long_memory.set_enabled(user_id, not await long_memory.is_enabled(user_id))
    elif callback_data.action == "forget_ask":
        await callback.message.edit_text(
            "Забыть всё, что я о тебе помню? Это нельзя отменить.",
            reply_markup=forget_confirm_keyboard(),
        )
        await callback.answer()
        return
    elif callback_data.action == "forget_yes":
        deleted = await long_memory.clear(user_id)
        await callback.answer(f"Забыла: {deleted}")
    text, enabled, has_facts = await _memory_text(long_memory, user_id)
    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML, reply_markup=memory_keyboard(enabled, has_facts)
    )
    await callback.answer()


async def cmd_clear(message: Message) -> None:
    await message.answer(
        "Удалить всю историю нашего диалога? Это действие нельзя отменить.",
        reply_markup=clear_confirm_keyboard(),
    )


async def on_clear_confirm(
    callback: CallbackQuery, callback_data: ClearHistoryCallback, memory: MemoryManager
) -> None:
    if callback_data.confirm:
        deleted = await memory.clear_history(callback.from_user.id)
        text = f"🗑 История очищена (удалено сообщений: {deleted}). Начнём с чистого листа!"
    else:
        text = "Хорошо, история сохранена."
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text)
    await callback.answer()


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Сейчас нечего отменять.")
        return
    await state.clear()
    await message.answer("Действие отменено.")


async def cmd_unknown(message: Message) -> None:
    await message.answer("Такой команды я пока не знаю. Список команд — /help")


def create_router() -> Router:
    router = Router(name="commands")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_memory, Command("memory"))
    router.message.register(cmd_forget, Command("forget"))
    router.message.register(cmd_clear, Command("clear"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.callback_query.register(on_clear_confirm, ClearHistoryCallback.filter())
    router.callback_query.register(on_memory_action, MemoryCallback.filter())
    router.message.register(cmd_unknown, F.text.startswith("/"))
    return router

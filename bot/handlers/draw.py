"""Рисование: /draw и просьбы вроде «нарисуй кота в шляпе» в обычном чате."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from bot.keyboards.draw import DrawCallback, drawing_keyboard
from services.drawing import Drawing, DrawingService
from services.imagegen import ImageGenerationError, ImageLimitError

logger = logging.getLogger(__name__)


class DrawStates(StatesGroup):
    waiting = State()  # /draw без описания: ждём, что нарисовать


NOT_CONFIGURED_TEXT = (
    "🎨 Рисовать я пока не умею: владельцу бота нужно подключить сервис картинок — "
    "бесплатный Cloudflare Workers AI (CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN) "
    "или Pollinations (POLLINATIONS_API_KEY). Как это сделать — в README, раздел «Картинки»."
)
ASK_TEXT = (
    "🎨 Что нарисовать? Опиши картинку одним сообщением, например: "
    "«кот-астронавт в стиле акварели» или «уютный домик в горах на закате».\n"
    "Можно и без команды — просто напиши «нарисуй …». /cancel — отмена."
)
FORGOTTEN_TEXT = "Эту картинку я уже не помню 🙈 Напиши просьбу ещё раз — нарисую заново."
FAILURE_TEXTS = {
    "rejected": (
        "🙈 Сервис картинок отказался это рисовать — возможно, сработал его фильтр. "
        "Попробуй описать по-другому."
    ),
    "rate_limit": (
        "⏳ Сервис картинок упёрся в лимит бесплатного тарифа. Попробуй чуть позже."
    ),
    "config": (
        "⚠️ Не получается подключиться к сервису картинок: похоже, проблема с ключом "
        "или аккаунтом. Подробности — в логах бота."
    ),
    "unavailable": "😔 Сервис картинок сейчас не отвечает. Попробуй ещё раз чуть позже.",
}


def limit_text(limit: int) -> str:
    return (
        f"🎨 На сегодня лимит картинок исчерпан ({limit}). Он обновится в 00:00 по UTC — "
        "это защита бесплатной квоты сервиса."
    )


def failure_text(exc: ImageGenerationError | ImageLimitError) -> str:
    if isinstance(exc, ImageLimitError):
        return limit_text(exc.limit)
    logger.error("Image generation failed (%s): %s", exc.kind, exc)
    return FAILURE_TEXTS[exc.kind]


async def deliver_drawing(bot: Bot, chat_id: int, drawing: Drawing, *, caption: str | None = None) -> None:
    """Картинка с подписью и кнопкой «Ещё вариант». Если Telegram не принял её как фото
    (например, из-за пропорций), она уходит файлом. Ошибки Telegram пробрасываются."""
    image = drawing.image
    name = f"elya-{drawing.id}.{image.extension}"
    caption = caption or drawing.caption
    markup = drawing_keyboard(drawing.id)
    try:
        await bot.send_photo(
            chat_id, BufferedInputFile(image.data, filename=name), caption=caption, reply_markup=markup
        )
    except TelegramBadRequest as exc:
        logger.warning("Telegram rejected the picture as a photo (%s), sending it as a file", exc)
        await bot.send_document(
            chat_id, BufferedInputFile(image.data, filename=name), caption=caption, reply_markup=markup
        )


async def draw_and_send(
    message: Message, bot: Bot, drawing: DrawingService, text: str, *, explicit: bool
) -> bool:
    """True — просьба обработана (картинка или понятная ошибка); False — это оказалась
    не просьба нарисовать, и сообщение надо отдать обычному диалогу."""
    user_id = message.from_user.id
    try:
        async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
            plan = await drawing.plan(user_id, text, explicit=explicit)
            if plan is None:
                return False
            result = await drawing.draw(user_id, plan)
    except (ImageGenerationError, ImageLimitError) as exc:
        await message.answer(failure_text(exc))
        return True
    await deliver_drawing(bot, message.chat.id, result)
    return True


async def maybe_draw(message: Message, bot: Bot, drawing: DrawingService | None, text: str) -> bool:
    """Обычное сообщение: если это просьба нарисовать — рисуем и возвращаем True."""
    if drawing is None or not drawing.enabled or not drawing.looks_like_request(text):
        return False
    return await draw_and_send(message, bot, drawing, text, explicit=False)


async def cmd_draw(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    bot: Bot,
    drawing: DrawingService | None = None,
) -> None:
    await state.clear()
    if drawing is None or not drawing.enabled:
        await message.answer(NOT_CONFIGURED_TEXT)
        return
    text = (command.args or "").strip()
    if not text:
        await state.set_state(DrawStates.waiting)
        await message.answer(ASK_TEXT)
        return
    await draw_and_send(message, bot, drawing, text, explicit=True)


async def on_description(
    message: Message, state: FSMContext, bot: Bot, drawing: DrawingService | None = None
) -> None:
    await state.clear()
    if drawing is None or not drawing.enabled:
        await message.answer(NOT_CONFIGURED_TEXT)
        return
    await draw_and_send(message, bot, drawing, message.text.strip(), explicit=True)


async def on_draw_action(
    callback: CallbackQuery,
    callback_data: DrawCallback,
    bot: Bot,
    drawing: DrawingService | None = None,
) -> None:
    if drawing is None or not drawing.enabled or not isinstance(callback.message, Message):
        await callback.answer()
        return
    await callback.answer("Рисую ещё вариант…")
    message = callback.message
    try:
        async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
            result = await drawing.again(callback.from_user.id, callback_data.id)
    except (ImageGenerationError, ImageLimitError) as exc:
        await message.answer(failure_text(exc))
        return
    if result is None:
        await message.answer(FORGOTTEN_TEXT)
        return
    await deliver_drawing(bot, message.chat.id, result)


def create_router() -> Router:
    router = Router(name="draw")
    router.message.register(cmd_draw, Command("draw"))
    router.message.register(on_description, DrawStates.waiting, F.text, ~F.text.startswith("/"))
    router.callback_query.register(on_draw_action, DrawCallback.filter(F.action == "again"))
    return router

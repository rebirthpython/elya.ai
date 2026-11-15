"""Документы в чате с ботом: PDF, Word, таблицы. Поиск ответа со ссылками на страницы
и сравнение файлов. Сам поиск — в services.documents, ответ — общий для всего диалога."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from services.chat import ChatService
from services.documents import (
    MAX_DOCUMENT_BYTES,
    DocumentError,
    DocumentInfo,
    DocumentStore,
    parse_document,
)

logger = logging.getLogger(__name__)

# Несколько файлов одним сообщением приходят по одному; подпись есть только у одного из них.
# Ждём, пока придут все, и отвечаем один раз.
ALBUM_DELAY = 1.5

NO_DOCUMENTS_TEXT = (
    "📚 В разговоре пока нет документов.\n\n"
    "Пришли PDF, Word (.docx), Excel (.xlsx), CSV или .txt — найду в нём ответ и укажу "
    "страницы. Можно сразу с вопросом в подписи. Два файла могу сравнить."
)


class DocsCallback(CallbackData, prefix="docs"):
    action: str  # del | clear
    id: int = 0


@dataclass
class _Album:
    message: Message
    caption: str = ""
    infos: list[DocumentInfo] = field(default_factory=list)
    task: asyncio.Task | None = None


_albums: dict[tuple[int, str], _Album] = {}


def _short(name: str, limit: int = 28) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def documents_keyboard(infos: list[DocumentInfo]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"✕ {_short(info.name)}", callback_data=DocsCallback(action="del", id=info.id).pack())]
        for info in infos
    ]
    if len(infos) > 1:
        rows.append([InlineKeyboardButton(text="🗑 Убрать все", callback_data=DocsCallback(action="clear").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def documents_text(infos: list[DocumentInfo]) -> str:
    lines = ["📚 Документы в разговоре:"]
    lines += [f"{n}. {info.title()}" for n, info in enumerate(infos, start=1)]
    lines.append(
        "\nСпрашивай — найду ответ и укажу страницы"
        + (", а «сравни их» — сравню файлы." if len(infos) > 1 else ".")
        + " Новый чат или /clear убирает документы, а лишний можно убрать кнопкой ниже."
    )
    return "\n".join(lines)


def _received_text(new: list[DocumentInfo], total: int) -> str:
    if len(new) == 1:
        lines = [f"📄 Прочитала {new[0].title()}."]
    else:
        lines = ["📄 Прочитала документы:"] + [f"• {info.title()}" for info in new]
    lines.append(
        "\nСпрашивай, что найти, — отвечу со ссылками на страницы. Можно и про конкретное "
        "место: «что на стр. 40?»"
    )
    if total > 1:
        lines.append("Чтобы сравнить файлы, напиши «сравни их».")
    else:
        lines.append("Пришли ещё один файл — смогу их сравнить.")
    lines.append("Все документы разговора — /docs")
    return "\n".join(lines)


async def _respond(message: Message, bot: Bot, chat_service: ChatService, infos: list[DocumentInfo], caption: str) -> None:
    from bot.handlers.chat import send_reply  # chat импортирует этот модуль

    if caption:
        await send_reply(message, bot, chat_service, caption, documents=tuple(info.id for info in infos))
        return
    total = await chat_service.documents.count(message.from_user.id)
    await message.answer(_received_text(infos, total))


async def _finish_album(key: tuple[int, str], bot: Bot, chat_service: ChatService) -> None:
    await asyncio.sleep(ALBUM_DELAY)
    album = _albums.pop(key, None)
    if album is None or not album.infos:
        return
    try:
        await _respond(album.message, bot, chat_service, album.infos, album.caption)
    except Exception:  # noqa: BLE001 — задача в фоне: ошибка не должна теряться молча
        logger.exception("Could not answer about the documents")


async def receive_document(message: Message, bot: Bot, chat_service: ChatService) -> None:
    """Файл-документ: читаем, кладём в разговор и отвечаем на подпись (или говорим, что готово)."""
    store = chat_service.documents
    document = message.document
    if store is None or document is None or message.from_user is None:
        return
    if (document.file_size or 0) > MAX_DOCUMENT_BYTES:
        await message.answer(
            "Файл слишком большой: Telegram даёт ботам скачивать файлы до 20 МБ. "
            "Пришли часть документа или сожми PDF."
        )
        return
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            content = await bot.download(document)
            parsed = await asyncio.to_thread(
                parse_document, document.file_name, content.getvalue() if content else b""
            )
            info = await store.add(message.from_user.id, parsed)
    except DocumentError as exc:
        await message.answer(f"📄 {exc}")
        return
    except TelegramAPIError as exc:
        logger.warning("Could not download the document: %s", exc)
        await message.answer("Не получилось скачать файл из Telegram. Попробуй прислать его ещё раз.")
        return
    if parsed.truncated:
        await message.answer(
            f"Документ «{info.name}» очень большой: я прочитала только его начало. "
            "Если нужное дальше — пришли эту часть отдельным файлом."
        )

    caption = (message.caption or "").strip()
    if message.media_group_id:
        key = (message.chat.id, message.media_group_id)
        album = _albums.get(key)
        if album is None:
            album = _albums[key] = _Album(message=message)
        album.infos.append(info)
        if caption:
            album.caption, album.message = caption, message
        if album.task is not None:
            album.task.cancel()
        album.task = asyncio.create_task(_finish_album(key, bot, chat_service))
        return
    await _respond(message, bot, chat_service, [info], caption)


async def cmd_docs(message: Message, chat_service: ChatService) -> None:
    store = chat_service.documents
    if message.from_user is None or store is None:
        return
    infos = await store.list(message.from_user.id)
    if not infos:
        await message.answer(NO_DOCUMENTS_TEXT)
        return
    await message.answer(documents_text(infos), reply_markup=documents_keyboard(infos))


async def on_docs_action(callback: CallbackQuery, callback_data: DocsCallback, chat_service: ChatService) -> None:
    store: DocumentStore | None = chat_service.documents
    if store is None:
        await callback.answer()
        return
    user_id = callback.from_user.id
    if callback_data.action == "clear":
        await store.clear(user_id)
        note = "Документы убраны"
    else:
        await store.delete(user_id, callback_data.id)
        note = "Документ убран"
    infos = await store.list(user_id)
    if isinstance(callback.message, Message):
        try:
            if infos:
                await callback.message.edit_text(documents_text(infos), reply_markup=documents_keyboard(infos))
            else:
                await callback.message.edit_text("📚 Документов в разговоре больше нет. Пришли новый файл, когда понадобится.")
        except TelegramAPIError:
            pass  # сообщение не изменилось или слишком старое
    await callback.answer(note)


def create_router() -> Router:
    router = Router(name="documents")
    router.message.register(cmd_docs, Command("docs"))
    router.callback_query.register(on_docs_action, DocsCallback.filter(F.action.in_({"del", "clear"})))
    return router

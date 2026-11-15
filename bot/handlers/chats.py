"""Чаты и проекты в чате с ботом: /new, /chats, /projects, /project.

Открытый чат общий с приложением: переписка идёт в него, а инструкции, файлы и режим
берутся из его проекта. Создавать и настраивать проекты удобнее в приложении,
но основное доступно и здесь."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from memory.chats import MAX_PROJECT_NAME, ChatInfo, ChatStore, ProjectError, ProjectInfo
from memory.manager import MemoryManager

CHATS_SHOWN = 10


class ChatsCallback(CallbackData, prefix="chats"):
    action: str  # open | new
    id: int = 0


class ProjectCallback(CallbackData, prefix="proj"):
    action: str  # open
    id: int = 0  # 0 — без проекта


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _where(chats: ChatStore, user_id: int, chat: ChatInfo) -> str:
    project = await chats.project(user_id, chat.project_id)
    return f" в проекте «{escape(project.label)}»" if project else ""


async def _last_reply(memory: MemoryManager, user_id: int, chat: ChatInfo) -> str:
    history = await memory.get_history(user_id, chat_id=chat.id)
    last = next((m.content for m in reversed(history) if m.role == "assistant"), "")
    return f"\n\nПоследний ответ: <i>{escape(_short(last, 200))}</i>" if last else ""


async def open_new_chat(message: Message, memory: MemoryManager, user_id: int, project_id=None, *, keep=True) -> None:
    chats = memory.chats
    if keep:
        chat, _ = await chats.new_chat(user_id)
    else:
        chat, _ = await chats.new_chat(user_id, project_id=project_id)
    await message.answer(
        f"🆕 Новый чат{await _where(chats, user_id, chat)}. Пиши — старая переписка не помешает.\n"
        "Вернуться к прошлым чатам — /chats",
        parse_mode=ParseMode.HTML,
    )


async def cmd_new(message: Message, memory: MemoryManager) -> None:
    if message.from_user is not None:
        await open_new_chat(message, memory, message.from_user.id)


def _chats_keyboard(chats: list[ChatInfo], active_id: int, labels: dict[int, str]) -> InlineKeyboardMarkup:
    rows = []
    for chat in chats[:CHATS_SHOWN]:
        mark = "✓ " if chat.id == active_id else ""
        emoji = labels.get(chat.project_id or 0, "")
        title = chat.title or "Новый чат"
        rows.append([
            InlineKeyboardButton(
                text=_short(f"{mark}{emoji}{title}", 48),
                callback_data=ChatsCallback(action="open", id=chat.id).pack(),
            )
        ])
    rows.append([InlineKeyboardButton(text="🆕 Новый чат", callback_data=ChatsCallback(action="new").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cmd_chats(message: Message, memory: MemoryManager) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    chats = memory.chats
    active = await chats.active(user_id)
    items = await chats.list(user_id)
    labels = {p.id: f"{p.emoji} " for p in await chats.projects(user_id)}
    text = (
        f"💬 <b>Чаты</b> — сейчас открыт «{escape(active.title or 'Новый чат')}»"
        f"{await _where(chats, user_id, active)}.\n"
        "Нажми на чат, чтобы вернуться к нему. У каждого чата своя переписка."
    )
    if len(items) > CHATS_SHOWN:
        text += f"\nЗдесь последние {CHATS_SHOWN}; все чаты — в приложении."
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=_chats_keyboard(items, active.id, labels))


async def on_chats_action(callback: CallbackQuery, callback_data: ChatsCallback, memory: MemoryManager) -> None:
    user_id = callback.from_user.id
    message = callback.message if isinstance(callback.message, Message) else None
    if callback_data.action == "new":
        await callback.answer("Новый чат")
        if message is not None:
            await open_new_chat(message, memory, user_id)
        return
    chat = await memory.chats.open(user_id, callback_data.id)
    if chat is None:
        await callback.answer("Этого чата больше нет", show_alert=True)
        return
    await callback.answer("Чат открыт")
    if message is not None:
        await message.answer(
            f"💬 Открыт чат «{escape(chat.title or 'Новый чат')}»{await _where(memory.chats, user_id, chat)}."
            f"{await _last_reply(memory, user_id, chat)}",
            parse_mode=ParseMode.HTML,
        )


def _projects_keyboard(projects: list[ProjectInfo]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=project.label, callback_data=ProjectCallback(action="open", id=project.id).pack())]
        for project in projects
    ]
    rows.append([InlineKeyboardButton(text="💬 Без проекта", callback_data=ProjectCallback(action="open", id=0).pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _project_text(projects: list[ProjectInfo], current: ProjectInfo | None) -> str:
    now = f"Сейчас: <b>{escape(current.label)}</b>." if current else "Сейчас чат без проекта."
    return (
        "🗂 <b>Проекты</b> — папки для чатов. У каждого свои инструкции и файлы, а в проекте "
        "«Код» Эля сразу отвечает как программист.\n"
        f"{now}\n\n"
        "Нажми на проект, чтобы продолжить его последний чат.\n"
        "Новый проект: <code>/project Название</code>. Инструкции и файлы проекта настраиваются "
        "в приложении, а файл, присланный сюда, попадает в открытый чат."
    )


async def cmd_projects(message: Message, memory: MemoryManager) -> None:
    if message.from_user is None:
        return
    user_id = message.from_user.id
    chats = memory.chats
    active = await chats.active(user_id)
    projects = await chats.projects(user_id)
    current = await chats.project(user_id, active.project_id)
    await message.answer(
        _project_text(projects, current), parse_mode=ParseMode.HTML, reply_markup=_projects_keyboard(projects)
    )


async def _enter_project(message: Message, memory: MemoryManager, user_id: int, project: ProjectInfo | None) -> None:
    """Продолжить последний чат проекта, а если его нет — начать новый."""
    chats = memory.chats
    project_id = project.id if project else None
    recent = await chats.list(user_id, project_id=project_id, limit=1)
    if recent:
        chat = await chats.open(user_id, recent[0].id)
        title = escape(chat.title or "Новый чат")
        where = f"Проект «{escape(project.label)}»" if project else "Чаты без проекта"
        await message.answer(
            f"{where}: открыт чат «{title}».{await _last_reply(memory, user_id, chat)}\n\n"
            "Начать новый — /new",
            parse_mode=ParseMode.HTML,
        )
        return
    await open_new_chat(message, memory, user_id, project_id, keep=False)


async def on_project_action(callback: CallbackQuery, callback_data: ProjectCallback, memory: MemoryManager) -> None:
    user_id = callback.from_user.id
    project = None
    if callback_data.id:
        project = await memory.chats.project(user_id, callback_data.id)
        if project is None:
            await callback.answer("Этого проекта больше нет", show_alert=True)
            return
    await callback.answer(project.label if project else "Без проекта")
    if isinstance(callback.message, Message):
        await _enter_project(callback.message, memory, user_id, project)


async def cmd_project(message: Message, command: CommandObject, memory: MemoryManager) -> None:
    """/project Название — перейти в проект, а если его нет — создать."""
    if message.from_user is None:
        return
    user_id = message.from_user.id
    name = (command.args or "").strip()
    if not name:
        await cmd_projects(message, memory)
        return
    chats = memory.chats
    project = await chats.find_project(user_id, name)
    if project is None:
        try:
            project = await chats.create_project(user_id, name)
        except ProjectError as exc:
            reason = {
                "too_many": "Проектов уже слишком много — удали лишние в приложении.",
                "empty_name": "Напиши название: <code>/project Английский</code>",
            }.get(str(exc), f"Название — до {MAX_PROJECT_NAME} символов.")
            await message.answer(reason, parse_mode=ParseMode.HTML)
            return
        await message.answer(
            f"🗂 Создала проект «{escape(project.label)}». Инструкции и файлы для него можно задать "
            "в приложении.",
            parse_mode=ParseMode.HTML,
        )
        await open_new_chat(message, memory, user_id, project.id, keep=False)
        return
    await _enter_project(message, memory, user_id, project)


def create_router() -> Router:
    router = Router(name="chats")
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_chats, Command("chats"))
    router.message.register(cmd_projects, Command("projects"))
    router.message.register(cmd_project, Command("project"))
    router.callback_query.register(on_chats_action, ChatsCallback.filter(F.action.in_({"open", "new"})))
    router.callback_query.register(on_project_action, ProjectCallback.filter(F.action == "open"))
    return router


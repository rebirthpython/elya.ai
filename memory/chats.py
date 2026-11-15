"""Отдельные чаты и проекты (папки чатов).

У каждого чата своя переписка: Эля видит только его сообщения. Чат может лежать в проекте —
«Учёба», «Код», «Личное» или любом своём. У проекта свои инструкции, файлы и, если нужно,
режим кода для всех его чатов.

Открытый чат один на пользователя и общий для бота и приложения: что написано в боте,
появляется в том же чате в приложении.

Старая история (до появления чатов) при первом обращении делится на чаты по отметкам
«Новый чат»; присланные тогда документы попадают в последний из них.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import Chat, ChatState, Message, Project, User, UserDocument

CHAT_BREAK = "break"  # отметка «Новый чат» в старой истории
MAX_PROJECTS = 20
MAX_PROJECT_NAME = 40
MAX_PROJECT_INSTRUCTIONS = 3000
MAX_CHAT_TITLE = 60
PROJECT_MODES = ("", "code")

# Проекты, которые появляются сами при первом знакомстве (их можно переименовать и удалить).
DEFAULT_PROJECTS = (
    (
        "Учёба",
        "📚",
        "Помогай с учёбой: объясняй понятно и по шагам, с примерами. В задачах сначала "
        "подскажи ход решения, а готовый ответ давай, если прошу. Проверяй, что я понял.",
        "",
    ),
    ("Код", "💻", "", "code"),
    ("Личное", "🌿", "", ""),
)

_KEEP = object()  # «оставить как есть» для необязательных аргументов


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime:
    """SQLite отдаёт время без пояса — считаем его UTC."""
    if value is None:
        return utcnow()
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    id: int
    name: str
    emoji: str
    instructions: str = ""
    mode: str = ""

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}".strip()

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "emoji": self.emoji,
            "instructions": self.instructions, "mode": self.mode,
        }

    def prompt_block(self) -> str:
        """Раздел системного промпта: в каком проекте разговор и его инструкции."""
        lines = [f"\nРазговор идёт в проекте «{self.name}»."]
        if self.instructions:
            lines.append(
                "Инструкции этого проекта — следуй им во всех его чатах (они важнее общего "
                f"стиля и полноты ответа, но не важнее правил выше):\n{self.instructions}"
            )
        return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class ChatInfo:
    id: int
    project_id: int | None
    title: str
    updated_at: datetime

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "updated": int(self.updated_at.timestamp()),
        }


def _chat(row: Chat) -> ChatInfo:
    return ChatInfo(id=row.id, project_id=row.project_id, title=row.title or "", updated_at=_aware(row.updated_at))


def _project(row: Project) -> ProjectInfo:
    return ProjectInfo(
        id=row.id, name=row.name, emoji=row.emoji or "📁",
        instructions=row.instructions or "", mode=row.mode or "",
    )


def make_title(text: str) -> str:
    """Название чата из первого сообщения: первая строка без пометок, коротко."""
    lines = [line.strip() for line in str(text).split("\n") if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    # «📄 «Учебник.pdf»» — если есть вопрос, название по вопросу, иначе по файлу.
    if first.startswith("📄") and len(lines) > 1:
        first = lines[1]
    first = re.sub(r"^📷 Фото:?\s*", "", first) or "Фото"
    first = " ".join(first.split())
    if len(first) > MAX_CHAT_TITLE:
        cut = first.rfind(" ", 0, MAX_CHAT_TITLE)
        first = first[: cut if cut > 20 else MAX_CHAT_TITLE].rstrip(" ,.:;—-") + "…"
    return first


def clean_project_name(name: str) -> str:
    return " ".join(str(name).split())[:MAX_PROJECT_NAME]


def clean_instructions(text: str) -> str:
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class ProjectError(ValueError):
    """empty_name | too_long | too_many | bad_mode | not_found"""


class ChatStore:
    # Общие для всех экземпляров: бот и приложение могут одновременно спросить открытый чат,
    # и без очереди у нового пользователя появилось бы два «первых» чата.
    _locks: dict[tuple[int, int], asyncio.Lock] = {}

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._session_factory = session_factory
        self._migrated: set[int] = set()

    @classmethod
    def _lock(cls, user_id: int) -> asyncio.Lock:
        key = (id(asyncio.get_running_loop()), user_id)
        lock = cls._locks.get(key)
        if lock is None:
            lock = cls._locks[key] = asyncio.Lock()
        return lock

    # ---------- открытый чат ----------

    async def active(self, user_id: int) -> ChatInfo:
        """Открытый сейчас чат; если чатов ещё нет — создаёт первый."""
        async with self._lock(user_id):
            return await self._active(user_id)

    async def _active(self, user_id: int) -> ChatInfo:
        await self._migrate(user_id)
        async with self._session_factory.begin() as session:
            state = await session.get(ChatState, user_id)
            row = await session.get(Chat, state.chat_id) if state and state.chat_id else None
            if row is None or row.user_id != user_id:
                row = await session.scalar(
                    select(Chat).where(Chat.user_id == user_id).order_by(Chat.updated_at.desc(), Chat.id.desc()).limit(1)
                )
                if row is None:
                    row = Chat(user_id=user_id, title="")
                    session.add(row)
                    await session.flush()
                await self._set_active(session, user_id, row.id)
            return _chat(row)

    @staticmethod
    async def _set_active(session: AsyncSession, user_id: int, chat_id: int) -> None:
        state = await session.get(ChatState, user_id)
        if state is None:
            session.add(ChatState(user_id=user_id, chat_id=chat_id, projects_seeded=False))
        else:
            state.chat_id = chat_id

    async def get(self, user_id: int, chat_id: int) -> ChatInfo | None:
        async with self._session_factory() as session:
            row = await session.get(Chat, chat_id)
            return _chat(row) if row is not None and row.user_id == user_id else None

    async def open(self, user_id: int, chat_id: int) -> ChatInfo | None:
        """Делает чат открытым. None — такого чата нет."""
        async with self._lock(user_id):
            await self._migrate(user_id)
            async with self._session_factory.begin() as session:
                row = await session.get(Chat, chat_id)
                if row is None or row.user_id != user_id:
                    return None
                await self._set_active(session, user_id, row.id)
            return _chat(row)

    async def is_empty(self, chat_id: int) -> bool:
        async with self._session_factory() as session:
            return not await session.scalar(select(exists().where(Message.chat_id == chat_id)))

    async def new_chat(self, user_id: int, project_id=_KEEP) -> tuple[ChatInfo, bool]:
        """Новый пустой чат (в том же проекте, если project_id не указан) и делает его открытым.
        Если открытый чат и так пуст, он и становится «новым». Второе значение — создан ли чат."""
        target = None if project_id is _KEEP else project_id
        if target is not None and await self.project(user_id, target) is None:
            target = None
        async with self._lock(user_id):
            current = await self._active(user_id)
            if project_id is _KEEP:
                target = current.project_id
            return await self._new_chat(user_id, current, target)

    async def _new_chat(self, user_id: int, current: ChatInfo, target: int | None) -> tuple[ChatInfo, bool]:
        async with self._session_factory.begin() as session:
            if await self._is_empty(session, current.id):
                row = await session.get(Chat, current.id)
                row.project_id = target
                row.title = ""
                row.updated_at = utcnow()
                return _chat(row), False
            row = Chat(user_id=user_id, project_id=target, title="")
            session.add(row)
            await session.flush()
            await self._set_active(session, user_id, row.id)
            return _chat(row), True

    @staticmethod
    async def _is_empty(session: AsyncSession, chat_id: int) -> bool:
        has_messages = await session.scalar(select(exists().where(Message.chat_id == chat_id)))
        has_files = await session.scalar(select(exists().where(UserDocument.chat_id == chat_id)))
        return not has_messages and not has_files

    async def touch(self, user_id: int, chat_id: int, first_text: str = "") -> None:
        """После нового сообщения: чат поднимается в списке и получает название."""
        async with self._session_factory.begin() as session:
            row = await session.get(Chat, chat_id)
            if row is None or row.user_id != user_id:
                return
            row.updated_at = utcnow()
            if not row.title and first_text:
                row.title = make_title(first_text) or "Новый чат"

    # ---------- список чатов ----------

    async def list(self, user_id: int, *, project_id=_KEEP, limit: int = 60) -> list[ChatInfo]:
        """Чаты с перепиской (и открытый, даже если пуст), новые сверху.
        project_id: не указан — все; None — без проекта; число — чаты проекта."""
        active = await self.active(user_id)
        async with self._session_factory() as session:
            query = select(Chat).where(
                Chat.user_id == user_id,
                (Chat.id == active.id)
                | exists().where(Message.chat_id == Chat.id)
                | exists().where(UserDocument.chat_id == Chat.id),
            )
            if project_id is not _KEEP:
                query = query.where(Chat.project_id.is_(None) if project_id is None else Chat.project_id == project_id)
            rows = await session.scalars(query.order_by(Chat.updated_at.desc(), Chat.id.desc()).limit(limit))
            return [_chat(row) for row in rows]

    async def rename(self, user_id: int, chat_id: int, title: str) -> ChatInfo | None:
        title = " ".join(str(title).split())[:MAX_CHAT_TITLE]
        async with self._session_factory.begin() as session:
            row = await session.get(Chat, chat_id)
            if row is None or row.user_id != user_id:
                return None
            row.title = title or row.title
            return _chat(row)

    async def move(self, user_id: int, chat_id: int, project_id: int | None) -> ChatInfo | None:
        """Перенести чат в проект (или убрать из проекта: None)."""
        if project_id is not None and await self.project(user_id, project_id) is None:
            return None
        async with self._session_factory.begin() as session:
            row = await session.get(Chat, chat_id)
            if row is None or row.user_id != user_id:
                return None
            row.project_id = project_id
            return _chat(row)

    async def delete(self, user_id: int, chat_id: int) -> bool:
        """Удаляет чат с перепиской и его файлами. Если он был открыт — открывается
        последний из оставшихся (или новый пустой)."""
        async with self._session_factory.begin() as session:
            row = await session.get(Chat, chat_id)
            if row is None or row.user_id != user_id:
                return False
            await session.execute(delete(Message).where(Message.chat_id == chat_id))
            await session.execute(delete(UserDocument).where(UserDocument.chat_id == chat_id))
            await session.delete(row)
            state = await session.get(ChatState, user_id)
            if state is not None and state.chat_id == chat_id:
                state.chat_id = None
        return True

    async def clear(self, user_id: int, chat_id: int) -> int:
        """Очищает переписку и файлы чата; сам чат остаётся. Возвращает число сообщений."""
        async with self._session_factory.begin() as session:
            row = await session.get(Chat, chat_id)
            if row is None or row.user_id != user_id:
                return 0
            result = await session.execute(delete(Message).where(Message.chat_id == chat_id))
            await session.execute(delete(UserDocument).where(UserDocument.chat_id == chat_id))
            row.title = ""
            return int(result.rowcount or 0)

    # ---------- проекты ----------

    async def projects(self, user_id: int) -> list[ProjectInfo]:
        async with self._lock(user_id):
            await self._seed(user_id)
        async with self._session_factory() as session:
            rows = await session.scalars(select(Project).where(Project.user_id == user_id).order_by(Project.id))
            return [_project(row) for row in rows]

    async def project(self, user_id: int, project_id: int | None) -> ProjectInfo | None:
        if project_id is None:
            return None
        async with self._session_factory() as session:
            row = await session.get(Project, project_id)
            return _project(row) if row is not None and row.user_id == user_id else None

    async def find_project(self, user_id: int, name: str) -> ProjectInfo | None:
        wanted = clean_project_name(name).lower()
        for project in await self.projects(user_id):
            if project.name.lower() == wanted:
                return project
        return None

    @staticmethod
    def _check(name: str | None, instructions: str | None, mode: str | None) -> dict:
        values: dict = {}
        if name is not None:
            cleaned = clean_project_name(name)
            if not cleaned:
                raise ProjectError("empty_name")
            values["name"] = cleaned
        if instructions is not None:
            cleaned = clean_instructions(instructions)
            if len(cleaned) > MAX_PROJECT_INSTRUCTIONS:
                raise ProjectError("too_long")
            values["instructions"] = cleaned
        if mode is not None:
            if mode not in PROJECT_MODES:
                raise ProjectError("bad_mode")
            values["mode"] = mode
        return values

    async def create_project(
        self, user_id: int, name: str, *, emoji: str = "📁", instructions: str = "", mode: str = ""
    ) -> ProjectInfo:
        values = self._check(name, instructions, mode)
        async with self._lock(user_id):
            await self._seed(user_id)
        async with self._session_factory.begin() as session:
            count = await session.scalar(select(func.count()).select_from(Project).where(Project.user_id == user_id))
            if (count or 0) >= MAX_PROJECTS:
                raise ProjectError("too_many")
            row = Project(user_id=user_id, emoji=_clean_emoji(emoji), **values)
            session.add(row)
            await session.flush()
            return _project(row)

    async def update_project(
        self, user_id: int, project_id: int, *, name: str | None = None, emoji: str | None = None,
        instructions: str | None = None, mode: str | None = None,
    ) -> ProjectInfo:
        values = self._check(name, instructions, mode)
        if emoji is not None:
            values["emoji"] = _clean_emoji(emoji)
        async with self._session_factory.begin() as session:
            row = await session.get(Project, project_id)
            if row is None or row.user_id != user_id:
                raise ProjectError("not_found")
            for key, value in values.items():
                setattr(row, key, value)
            return _project(row)

    async def delete_project(self, user_id: int, project_id: int) -> bool:
        """Удаляет проект и его файлы. Чаты не пропадают — переходят в «Без проекта»."""
        async with self._session_factory.begin() as session:
            row = await session.get(Project, project_id)
            if row is None or row.user_id != user_id:
                return False
            await session.execute(
                update(Chat).where(Chat.user_id == user_id, Chat.project_id == project_id).values(project_id=None)
            )
            await session.execute(delete(UserDocument).where(UserDocument.project_id == project_id))
            await session.delete(row)
            return True

    async def _seed(self, user_id: int) -> None:
        async with self._session_factory.begin() as session:
            state = await session.get(ChatState, user_id)
            if state is not None and state.projects_seeded:
                return
            if state is None:
                state = ChatState(user_id=user_id, chat_id=None, projects_seeded=False)
                session.add(state)
            has_projects = await session.scalar(select(exists().where(Project.user_id == user_id)))
            if not has_projects:
                for name, emoji, instructions, mode in DEFAULT_PROJECTS:
                    session.add(Project(user_id=user_id, name=name, emoji=emoji, instructions=instructions, mode=mode))
            state.projects_seeded = True

    # ---------- старая история ----------

    async def _migrate(self, user_id: int) -> None:
        """Старая история без чатов → чаты по отметкам «Новый чат». Один раз на пользователя."""
        if user_id in self._migrated:
            return
        async with self._session_factory.begin() as session:
            rows = (
                await session.scalars(
                    select(Message).where(Message.user_id == user_id, Message.chat_id.is_(None)).order_by(Message.id)
                )
            ).all()
            last: Chat | None = None
            if rows:
                if await session.get(User, user_id) is None:
                    session.add(User(id=user_id))
                groups: list[list[Message]] = [[]]
                for row in rows:
                    if row.role == CHAT_BREAK:
                        groups.append([])
                        await session.delete(row)
                    else:
                        groups[-1].append(row)
                for group in groups:
                    if not group:
                        continue
                    first_user = next((m.content for m in group if m.role == "user"), "")
                    chat = Chat(
                        user_id=user_id,
                        title=make_title(first_user) or "Чат",
                        created_at=_aware(group[0].created_at),
                        updated_at=_aware(group[-1].created_at),
                    )
                    session.add(chat)
                    await session.flush()
                    for message in group:
                        message.chat_id = chat.id
                    last = chat
            orphans = await session.scalar(
                select(exists().where(
                    UserDocument.user_id == user_id, UserDocument.chat_id.is_(None), UserDocument.project_id.is_(None)
                ))
            )
            if orphans:
                if last is None:
                    state = await session.get(ChatState, user_id)
                    last = await session.get(Chat, state.chat_id) if state and state.chat_id else None
                if last is None:
                    last = Chat(user_id=user_id, title="Документы")
                    session.add(last)
                    await session.flush()
                await session.execute(
                    update(UserDocument)
                    .where(UserDocument.user_id == user_id, UserDocument.chat_id.is_(None), UserDocument.project_id.is_(None))
                    .values(chat_id=last.id)
                )
            if last is not None:
                await self._set_active(session, user_id, last.id)
        self._migrated.add(user_id)


def _clean_emoji(emoji: str | None) -> str:
    value = "".join(str(emoji or "").split())[:8]
    return value or "📁"

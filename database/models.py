"""ORM-модели. Новые сущности следующих этапов добавляются отдельными таблицами."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # Telegram user ID используется как первичный ключ.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    """Сообщение диалога (краткосрочная память)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    # Чат, к которому относится сообщение (None — старая история до появления чатов).
    chat_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Готовые файлы к ответу (JSON: [{token, name, kind, size, preview}]) — для приложения.
    attachments: Mapped[str | None] = mapped_column(Text, nullable=True)


class DailyUsage(Base):
    """Счётчик запросов к AI-провайдерам за сутки (UTC) — защита квот."""

    __tablename__ = "daily_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)


class DailyImageUsage(Base):
    """Сколько картинок нарисовано за сутки (UTC) — свой лимит, отдельный от текстовых запросов."""

    __tablename__ = "daily_image_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)


class DailySearchUsage(Base):
    """Счётчик поисков в интернете за сутки (UTC) — защита квоты поискового API."""

    __tablename__ = "daily_search_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)


class UserPreferences(Base):
    """Как Эля общается с пользователем: стиль и подробность ответов."""

    __tablename__ = "user_preferences"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    style: Mapped[str] = mapped_column(String(16))
    verbosity: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MemoryFact(Base):
    """Факт о пользователе, который Эля помнит между диалогами."""

    __tablename__ = "memory_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(8))  # "auto" — выделила Эля, "manual" — добавлен вручную
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemorySettings(Base):
    """Согласие пользователя на долговременную память (по умолчанию выключена)."""

    __tablename__ = "memory_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class UserTimezone(Base):
    """Часовой пояс пользователя (IANA-имя или смещение UTC±HH:MM) — для напоминаний."""

    __tablename__ = "user_timezones"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    tz: Mapped[str] = mapped_column(String(64))


class Reminder(Base):
    """Напоминание. Время хранится в UTC; повторяющееся переносится после отправки."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(String(400))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    repeat: Mapped[str] = mapped_column(String(8), default="none")
    anchor_day: Mapped[int] = mapped_column(Integer, default=1)  # число месяца для «каждый месяц»
    status: Mapped[str] = mapped_column(String(8), default="pending")  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserInstructions(Base):
    """Постоянные инструкции пользователя для Эли (как «Custom instructions» в ChatGPT)."""

    __tablename__ = "user_instructions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    about: Mapped[str] = mapped_column(Text, default="")
    style: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UserMode(Base):
    """Режим общения: обычный разговор или режим кода (команда /code)."""

    __tablename__ = "user_modes"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    mode: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UserDocument(Base):
    """Документ, присланный в разговор (PDF, Word, таблица): фрагменты с пометками места."""

    __tablename__ = "user_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))  # pdf | docx | xlsx | csv | text
    info: Mapped[str] = mapped_column(String(200))  # «PDF, 230 стр.»
    chars: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)  # JSON: [[пометка, текст], …]
    # Где документ лежит: в одном чате или в проекте (тогда он виден во всех его чатах).
    chat_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    """Проект (папка чатов): «Учёба», «Код», «Личное». У каждого свои инструкции и файлы."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(60))
    emoji: Mapped[str] = mapped_column(String(16), default="📁")
    instructions: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="")  # "code" — чаты проекта в режиме кода
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Chat(Base):
    """Отдельный чат со своей перепиской; может лежать в проекте."""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatState(Base):
    """Открытый сейчас чат пользователя — общий для бота и приложения."""

    __tablename__ = "chat_state"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Проекты по умолчанию уже созданы (если пользователь их удалит, они не вернутся).
    projects_seeded: Mapped[bool] = mapped_column(Boolean, default=False)


class UserFile(Base):
    """Готовый файл к ответу (DOCX, PPTX, ZIP, HTML): приложение скачивает его по токену."""

    __tablename__ = "user_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))
    mime: Mapped[str] = mapped_column(String(120))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

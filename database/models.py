"""ORM-модели. Новые сущности следующих этапов добавляются отдельными таблицами."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyUsage(Base):
    """Счётчик запросов к AI-провайдерам за сутки (UTC) — защита квот."""

    __tablename__ = "daily_usage"

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

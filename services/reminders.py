"""Напоминания: разбор просьб из чата, хранение, расчёт времени и доставка.

Просьба на естественном языке («напомни завтра в 9 позвонить маме») разбирается моделью
в структуру, а саму дату считает код: модели плохо считают относительное время и дни недели.
Подтверждение пользователю формирует тоже код, чтобы Эля не могла пообещать напоминание,
которого на самом деле нет.
"""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from ai.client import AIClient
from ai.providers.base import ChatMessage
from database.models import Reminder, UserTimezone

logger = logging.getLogger(__name__)

MAX_PENDING = 50
MAX_TEXT = 300
MAX_AHEAD = timedelta(days=366)
LATE_AFTER = timedelta(minutes=5)
DEFAULT_TIME = time(9, 0)
REPEATS = ("none", "daily", "weekdays", "weekly", "monthly")

# Явная просьба поставить напоминание; обычные вопросы про «напоминания» сюда не попадают.
INTENT = re.compile(
    r"\b(?:напомни(?:те|ть)?|напоминай(?:те)?|"
    r"(?:поставь|создай|добавь)\s+(?:мне\s+)?напоминани\w*)\b",
    re.IGNORECASE,
)

WEEKDAYS_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
REPEAT_TITLES = {
    "daily": "каждый день",
    "weekdays": "по будням",
    "weekly": "каждую неделю",
    "monthly": "каждый месяц",
}

HELP_EXAMPLE = "«напомни завтра в 9 позвонить маме»"
NEED_TIMEZONE = (
    "Сначала мне нужно знать твой часовой пояс, иначе я промахнусь со временем. "
    "Открой приложение (там он определится сам) или отправь, например, "
    "/timezone Europe/Moscow. Потом повтори просьбу."
)

PARSE_PROMPT = """\
Ты разбираешь просьбы поставить напоминание. Сейчас у пользователя: {now:%Y-%m-%d %H:%M}, \
{weekday}.

Если сообщение — просьба поставить напоминание, ответь ТОЛЬКО JSON-объектом:
{{"is_reminder": true, "text": "...", "date": "ГГГГ-ММ-ДД" или null, "weekday": 0-6 или null, \
"time": "ЧЧ:ММ" или null, "in_minutes": целое или null, \
"repeat": "none" | "daily" | "weekdays" | "weekly" | "monthly"}}

Правила:
- text — что напомнить, коротко, без слов «напомни» и без указания времени: \
«позвонить маме», «выпить воду».
- «через 2 часа» → in_minutes=120, остальные поля времени null.
- «завтра», «послезавтра», «31 декабря» → date (по календарю выше), time — если названо.
- «в понедельник», «каждую пятницу» → weekday (0 — понедельник), для «каждую» repeat=weekly.
- «каждый день» → repeat=daily; «по будням» → repeat=weekdays; «каждый месяц» → repeat=monthly.
- время суток: утром 09:00, днём 13:00, вечером 19:00, ночью 23:00; «в 9» без уточнения → 09:00, \
«в 5 вечера» → 17:00. Время указывай в 24-часовом формате.
- Если сообщение не просьба поставить напоминание, ответь {{"is_reminder": false}}.
Никакого текста вне JSON.\
"""


# ---------------------------------------------------------------- время ----

_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def resolve_timezone(name: str) -> tzinfo | None:
    """IANA-имя («Europe/Moscow») или смещение («UTC+3», «+03:30»); None — не распознано."""
    name = name.strip()
    if not name:
        return None
    match = _OFFSET.match(name)
    if match:
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            return None
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if match.group(1) == "-" else delta)
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, KeyError):  # ZoneInfoNotFoundError — подкласс KeyError
        return None


def canonical_timezone(name: str) -> str | None:
    """Имя для хранения: ключ IANA или «UTC+03:30»."""
    tz = resolve_timezone(name)
    if tz is None:
        return None
    if isinstance(tz, ZoneInfo):
        return tz.key
    offset = tz.utcoffset(None) or timedelta()
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """SQLite отдаёт время без пояса; в базе оно всегда UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _local_to_utc(day: date, at: time, tz: tzinfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz).astimezone(timezone.utc)


def describe_due(due: datetime, tz: tzinfo, now: datetime) -> str:
    """«сегодня в 09:00», «завтра в 09:00», «в пятницу, 14 ноября, в 09:00»."""
    local = due.astimezone(tz)
    today = now.astimezone(tz).date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        return f"сегодня в {clock}"
    if local.date() == today + timedelta(days=1):
        return f"завтра в {clock}"
    year = f" {local.year}" if local.year != today.year else ""
    return f"в {WEEKDAYS_ACC[local.weekday()]}, {local.day} {MONTHS_GEN[local.month - 1]}{year}, в {clock}"


def _minutes_text(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч" + (f" {rest} мин" if rest else "")
    days, hours = divmod(hours, 24)
    return f"{days} дн" + (f" {hours} ч" if hours else "")


def next_occurrence(
    due: datetime, repeat: str, tz: tzinfo, anchor_day: int, now: datetime
) -> datetime | None:
    """Ближайшее повторение строго после now; None — напоминание не повторяется."""
    if repeat not in REPEATS or repeat == "none":
        return None
    local = due.astimezone(tz)
    at, day = local.time().replace(tzinfo=None), local.date()
    for _ in range(5000):
        if repeat == "daily":
            day += timedelta(days=1)
        elif repeat == "weekdays":
            day += timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
        elif repeat == "weekly":
            day += timedelta(days=7)
        else:  # monthly: число месяца не «сползает» после коротких месяцев
            year, month = day.year + day.month // 12, day.month % 12 + 1
            day = date(year, month, min(anchor_day, calendar.monthrange(year, month)[1]))
        candidate = _local_to_utc(day, at, tz)
        if candidate > now:
            return candidate
    return None


# ------------------------------------------------------------ разбор просьбы ----


@dataclass(frozen=True, slots=True)
class Spec:
    """Что понял разбор: когда и что напомнить."""

    text: str
    date: date | None = None
    weekday: int | None = None
    time: time | None = None
    in_minutes: int | None = None
    repeat: str = "none"


def _parse_json_object(raw: str) -> dict | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _int_or_none(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def spec_from_model(raw: str) -> Spec | None:
    """None — модель решила, что это не просьба о напоминании (или ответила невнятно)."""
    data = _parse_json_object(raw)
    if not data or data.get("is_reminder") is not True:
        return None
    text = " ".join(str(data.get("text") or "").split()).strip(" .,;:—-\"'«»")
    day = None
    if isinstance(data.get("date"), str):
        try:
            day = date.fromisoformat(data["date"].strip()[:10])
        except ValueError:
            day = None
    weekday = _int_or_none(data.get("weekday"))
    at = None
    if isinstance(data.get("time"), str):
        match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", data["time"])
        if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
            at = time(int(match.group(1)), int(match.group(2)))
    repeat = data.get("repeat") if data.get("repeat") in REPEATS else "none"
    return Spec(
        text=text,
        date=day,
        weekday=weekday if weekday is not None and 0 <= weekday <= 6 else None,
        time=at,
        in_minutes=_int_or_none(data.get("in_minutes")),
        repeat=repeat,
    )


@dataclass(frozen=True, slots=True)
class Due:
    at: datetime | None  # UTC
    problem: str | None = None  # unclear | past | far
    default_time: bool = False  # время не было названо, поставлено 09:00


def due_from_spec(spec: Spec, tz: tzinfo, now: datetime) -> Due:
    local_now = now.astimezone(tz)
    if spec.in_minutes is not None:
        if spec.in_minutes < 1:
            return Due(None, "unclear")
        at = now + timedelta(minutes=spec.in_minutes)
        return Due(at, "far" if at - now > MAX_AHEAD else None)

    default_time = False
    at = spec.time
    has_day = spec.date is not None or spec.weekday is not None
    if at is None and (has_day or spec.repeat != "none"):
        at, default_time = DEFAULT_TIME, True
    if at is None:
        return Due(None, "unclear")

    if spec.date is not None:
        due = _local_to_utc(spec.date, at, tz)
        if due <= now:
            return Due(None, "past")
    else:
        day = local_now.date()
        if spec.weekday is not None:
            day += timedelta(days=(spec.weekday - local_now.weekday()) % 7)
        due = _local_to_utc(day, at, tz)
        if due <= now:
            day += timedelta(days=7 if spec.weekday is not None else 1)
            due = _local_to_utc(day, at, tz)

    if spec.repeat == "weekdays" and due.astimezone(tz).weekday() >= 5:
        due = next_occurrence(due, "weekdays", tz, 1, due) or due
    return Due(due, "far" if due - now > MAX_AHEAD else None, default_time)


# ------------------------------------------------------------------ данные ----


@dataclass(frozen=True, slots=True)
class ReminderItem:
    id: int
    user_id: int
    text: str
    due_at: datetime  # UTC
    repeat: str
    anchor_day: int
    status: str
    attempts: int


class TooManyRemindersError(Exception):
    pass


def _item(row: Reminder) -> ReminderItem:
    return ReminderItem(
        row.id, row.user_id, row.text, as_utc(row.due_at), row.repeat,
        row.anchor_day, row.status, row.attempts,
    )


class DeliveryError(Exception):
    """Сообщение не доставлено. permanent=True — повторять бессмысленно (бот заблокирован)."""

    def __init__(self, message: str = "", *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class Reminders:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        ai_client: AIClient | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        max_pending: int = MAX_PENDING,
    ) -> None:
        self._session_factory = session_factory
        self._ai = ai_client
        self._clock = clock
        self._max_pending = max_pending

    def now(self) -> datetime:
        return self._clock()

    # ---------- часовой пояс ----------

    async def get_timezone(self, user_id: int) -> str | None:
        async with self._session_factory() as session:
            row = await session.get(UserTimezone, user_id)
            return row.tz if row else None

    async def tzinfo(self, user_id: int) -> tzinfo | None:
        name = await self.get_timezone(user_id)
        return resolve_timezone(name) if name else None

    async def set_timezone(self, user_id: int, name: str) -> str | None:
        """Сохраняет пояс; возвращает каноническое имя или None, если имя не распознано."""
        canonical = canonical_timezone(name)
        if canonical is None:
            return None
        async with self._session_factory.begin() as session:
            row = await session.get(UserTimezone, user_id)
            if row is None:
                session.add(UserTimezone(user_id=user_id, tz=canonical))
            else:
                row.tz = canonical
        return canonical

    # ---------- напоминания ----------

    async def create(
        self, user_id: int, text: str, due_at: datetime, repeat: str = "none", *, tz: tzinfo | None = None
    ) -> ReminderItem:
        tz = tz or await self.tzinfo(user_id) or timezone.utc
        async with self._session_factory.begin() as session:
            pending = await session.scalar(
                select(func.count()).select_from(Reminder)
                .where(Reminder.user_id == user_id, Reminder.status == "pending")
            )
            if int(pending or 0) >= self._max_pending:
                raise TooManyRemindersError
            row = Reminder(
                user_id=user_id, text=text, due_at=as_utc(due_at),
                repeat=repeat if repeat in REPEATS else "none",
                anchor_day=as_utc(due_at).astimezone(tz).day,
            )
            session.add(row)
            await session.flush()
            return _item(row)

    async def list_pending(self, user_id: int) -> list[ReminderItem]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Reminder)
                .where(Reminder.user_id == user_id, Reminder.status == "pending")
                .order_by(Reminder.due_at, Reminder.id)
            )
            return [_item(r) for r in rows]

    async def get(self, user_id: int, reminder_id: int) -> ReminderItem | None:
        async with self._session_factory() as session:
            row = await session.get(Reminder, reminder_id)
            return _item(row) if row and row.user_id == user_id else None

    async def delete(self, user_id: int, reminder_id: int) -> bool:
        async with self._session_factory.begin() as session:
            result = await session.execute(
                delete(Reminder).where(Reminder.user_id == user_id, Reminder.id == reminder_id)
            )
            return bool(result.rowcount)

    async def snooze(self, user_id: int, reminder_id: int, minutes: int) -> ReminderItem | None:
        """Новое разовое напоминание с тем же текстом; None — исходное уже не найти."""
        original = await self.get(user_id, reminder_id)
        if original is None:
            return None
        return await self.create(user_id, original.text, self.now() + timedelta(minutes=minutes))

    async def due(self, now: datetime, limit: int = 50) -> list[ReminderItem]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Reminder)
                .where(Reminder.status == "pending", Reminder.due_at <= as_utc(now))
                .order_by(Reminder.due_at, Reminder.id)
                .limit(limit)
            )
            return [_item(r) for r in rows]

    async def mark_sent(self, reminder_id: int, now: datetime) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                update(Reminder).where(Reminder.id == reminder_id)
                .values(status="sent", sent_at=as_utc(now))
            )

    async def mark_failed(self, reminder_id: int) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                update(Reminder).where(Reminder.id == reminder_id).values(status="failed")
            )

    async def bump_attempts(self, reminder_id: int) -> int:
        async with self._session_factory.begin() as session:
            row = await session.get(Reminder, reminder_id)
            if row is None:
                return 0
            row.attempts += 1
            return row.attempts

    async def reschedule(self, reminder_id: int, due_at: datetime) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                update(Reminder).where(Reminder.id == reminder_id)
                .values(due_at=as_utc(due_at), attempts=0)
            )

    async def purge_old(self, now: datetime, days: int = 30) -> int:
        async with self._session_factory.begin() as session:
            result = await session.execute(
                delete(Reminder).where(
                    Reminder.status != "pending", Reminder.due_at < as_utc(now) - timedelta(days=days)
                )
            )
            return int(result.rowcount or 0)

    # ---------- просьба из чата ----------

    async def _parse(self, text: str, tz: tzinfo, now: datetime) -> Spec | None:
        if self._ai is None:
            return None
        local = now.astimezone(tz)
        weekday = WEEKDAYS_ACC[local.weekday()]
        messages = [
            ChatMessage(
                role="system",
                content=PARSE_PROMPT.format(now=local.replace(tzinfo=None), weekday=f"{weekday} (0–6: {local.weekday()})"),
            ),
            ChatMessage(role="user", content=text[:1000]),
        ]
        result = await self._ai.complete(messages, temperature=0.0, max_tokens=250)
        return spec_from_model(result.text)

    async def handle_chat_request(self, user_id: int, text: str) -> str | None:
        """Готовый ответ, если сообщение — просьба о напоминании; None — это обычный разговор.

        Ошибки ИИ (AIError) пробрасываются: диалог обработает их как обычно.
        """
        if not INTENT.search(text):
            return None
        tz = await self.tzinfo(user_id)
        if tz is None:
            return NEED_TIMEZONE
        now = self.now()
        spec = await self._parse(text, tz, now)
        if spec is None:
            return None
        if not spec.text:
            return f"Что именно напомнить? Напиши, например: {HELP_EXAMPLE}."
        if len(spec.text) > MAX_TEXT:
            return f"Слишком длинно: уложись в {MAX_TEXT} символов."

        due = due_from_spec(spec, tz, now)
        if due.at is None or due.problem:
            if due.problem == "past":
                return "Это время уже прошло. Назови время в будущем."
            if due.problem == "far":
                return "Я напоминаю не дальше чем на год вперёд."
            return f"Не поняла, когда напомнить. Напиши, например: {HELP_EXAMPLE}."
        try:
            item = await self.create(user_id, spec.text, due.at, spec.repeat, tz=tz)
        except TooManyRemindersError:
            return f"У тебя уже {self._max_pending} напоминаний. Удали что-нибудь лишнее: /reminders."
        return self.confirmation(item, spec, tz, now, due.default_time)

    @staticmethod
    def confirmation(item: ReminderItem, spec: Spec, tz: tzinfo, now: datetime, default_time: bool) -> str:
        if spec.in_minutes is not None:
            when = f"через {_minutes_text(spec.in_minutes)}, {describe_due(item.due_at, tz, now)}"
        else:
            when = describe_due(item.due_at, tz, now)
        lines = [f"Хорошо, напомню {when}: {item.text} ⏰"]
        if spec.repeat != "none":
            lines.append(f"Повторять: {REPEAT_TITLES[spec.repeat]}.")
        if default_time:
            lines.append("Время не указано, поэтому поставила 09:00.")
        lines.append("Все напоминания — в /reminders или в приложении (кнопка 🔔).")
        return "\n".join(lines)


# ------------------------------------------------------------ планировщик ----

Sender = Callable[[ReminderItem, str | None], Awaitable[None]]


class ReminderScheduler:
    """Раз в interval секунд отправляет созданные напоминания, срок которых наступил.

    Если бот был выключен, просроченные напоминания придут после запуска с пометкой;
    повторяющееся после этого переносится на ближайшее будущее время (пропущенные не копятся).
    """

    def __init__(
        self,
        reminders: Reminders,
        sender: Sender,
        *,
        interval: float = 20.0,
        max_attempts: int = 5,
    ) -> None:
        self._reminders = reminders
        self._sender = sender
        self._interval = interval
        self._max_attempts = max_attempts

    async def tick(self) -> int:
        """Один проход: возвращает число доставленных напоминаний."""
        reminders = self._reminders
        now = reminders.now()
        delivered = 0
        for item in await reminders.due(now):
            tz = await reminders.tzinfo(item.user_id) or timezone.utc
            late = now - item.due_at
            note = (
                f"Должно было прийти {describe_due(item.due_at, tz, now)}, но бот был недоступен."
                if late > LATE_AFTER else None
            )
            try:
                await self._sender(item, note)
            except DeliveryError as exc:
                if exc.permanent:
                    logger.warning("Reminder %s: cannot deliver (%s), giving up", item.id, exc)
                    await reminders.mark_failed(item.id)
                elif await reminders.bump_attempts(item.id) >= self._max_attempts:
                    logger.warning("Reminder %s: giving up after %d attempts", item.id, self._max_attempts)
                    await reminders.mark_failed(item.id)
                continue
            delivered += 1
            following = next_occurrence(item.due_at, item.repeat, tz, item.anchor_day, now)
            if following is None:
                await reminders.mark_sent(item.id, now)
            else:
                await reminders.reschedule(item.id, following)
        return delivered

    async def run(self) -> None:
        await self._reminders.purge_old(self._reminders.now())
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — планировщик не должен падать
                logger.exception("Reminder tick failed")
            await asyncio.sleep(self._interval)

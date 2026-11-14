from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ai.client import AIClient, AllProvidersFailedError
from ai.providers.base import BaseProvider, CompletionResult, ProviderUnavailableError
from config.settings import ProviderConfig, Settings
from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.reminders import (
    INTENT,
    NEED_TIMEZONE,
    DeliveryError,
    ReminderScheduler,
    Reminders,
    Spec,
    TooManyRemindersError,
    canonical_timezone,
    describe_due,
    due_from_spec,
    next_occurrence,
    resolve_timezone,
    spec_from_model,
)

UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")  # UTC+3 круглый год
USER = 5
# Пятница, 13 ноября 2026, 15:00 по Москве
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def msk(y, mo, d, h=9, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=MSK).astimezone(UTC)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


# ---------------------------------------------------------------- часовые пояса ----


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Europe/Moscow", "Europe/Moscow"),
        ("  Asia/Almaty ", "Asia/Almaty"),
        ("UTC+3", "UTC+03:00"),
        ("utc-3:30", "UTC-03:30"),
        ("+5", "UTC+05:00"),
        ("GMT+0530", "UTC+05:30"),
        ("Mars/Base", None),
        ("", None),
        ("UTC+15", None),
        ("UTC+3:99", None),
        ("../etc/passwd", None),
    ],
)
def test_canonical_timezone(name, expected):
    assert canonical_timezone(name) == expected


def test_resolve_timezone_gives_working_offsets():
    tz = resolve_timezone("UTC+03:30")
    assert datetime(2026, 1, 1, 12, tzinfo=UTC).astimezone(tz).strftime("%H:%M") == "15:30"


# ------------------------------------------------------------------- «когда» ----


def test_describe_due():
    assert describe_due(msk(2026, 11, 13, 18), MSK, NOW) == "сегодня в 18:00"
    assert describe_due(msk(2026, 11, 14, 9), MSK, NOW) == "завтра в 09:00"
    assert describe_due(msk(2026, 11, 20, 9), MSK, NOW) == "в пятницу, 20 ноября, в 09:00"
    assert describe_due(msk(2027, 1, 3, 9), MSK, NOW) == "в воскресенье, 3 января 2027, в 09:00"
    # день определяется по поясу пользователя, а не по UTC
    late = datetime(2026, 11, 13, 21, 30, tzinfo=UTC)  # в Москве уже суббота 00:30
    assert describe_due(late, MSK, NOW) == "завтра в 00:30"


# ------------------------------------------------------------- разбор ответа ----


def test_spec_from_model_parses_fields_and_cleans_text():
    raw = '```json\n{"is_reminder": true, "text": " «Позвонить маме». ", "date": "2026-11-14", "time": "9:05", "repeat": "weekly", "weekday": "4"}\n```'
    assert spec_from_model(raw) == Spec(
        "Позвонить маме", date(2026, 11, 14), 4, time(9, 5), None, "weekly"
    )


@pytest.mark.parametrize(
    "raw",
    ["", "не json", '{"is_reminder": false}', '{"is_reminder": "true"}', '["a"]', "{нет}"],
)
def test_spec_from_model_rejects_non_reminders(raw):
    assert spec_from_model(raw) is None


def test_spec_from_model_ignores_garbage_fields():
    spec = spec_from_model(
        '{"is_reminder": true, "text": "выпить воду", "date": "завтра", "weekday": 9, '
        '"time": "25:00", "in_minutes": "abc", "repeat": "yearly"}'
    )
    assert spec == Spec("выпить воду")


# ------------------------------------------------------------- расчёт времени ----


def due(spec, now=NOW, tz=MSK):
    return due_from_spec(spec, tz, now)


def test_relative_minutes():
    assert due(Spec("x", in_minutes=120)).at == NOW + timedelta(hours=2)
    assert due(Spec("x", in_minutes=0)).problem == "unclear"
    assert due(Spec("x", in_minutes=10**7)).problem == "far"


def test_date_and_time():
    result = due(Spec("x", date=date(2026, 11, 14), time=time(9, 0)))
    assert result.at == msk(2026, 11, 14, 9) and not result.default_time
    assert due(Spec("x", date=date(2026, 11, 13), time=time(9, 0))).problem == "past"
    assert due(Spec("x", date=date(2028, 1, 1), time=time(9, 0))).problem == "far"


def test_date_without_time_uses_nine():
    result = due(Spec("x", date=date(2026, 11, 14)))
    assert result.at == msk(2026, 11, 14, 9) and result.default_time


def test_time_only_means_today_or_tomorrow():
    assert due(Spec("x", time=time(18, 0))).at == msk(2026, 11, 13, 18)
    assert due(Spec("x", time=time(9, 0))).at == msk(2026, 11, 14, 9)  # 9:00 уже прошло


def test_weekday_is_next_occurrence():
    # сегодня пятница (4): понедельник — через 3 дня, пятница позже времени — через неделю
    assert due(Spec("x", weekday=0, time=time(10, 0))).at == msk(2026, 11, 16, 10)
    assert due(Spec("x", weekday=4, time=time(18, 0))).at == msk(2026, 11, 13, 18)
    assert due(Spec("x", weekday=4, time=time(9, 0))).at == msk(2026, 11, 20, 9)


def test_repeat_without_any_time_starts_tomorrow_at_nine():
    result = due(Spec("пить воду", repeat="daily"))
    assert result.at == msk(2026, 11, 14, 9) and result.default_time


def test_nothing_to_go_on_is_unclear():
    assert due(Spec("x")).problem == "unclear"


def test_weekdays_repeat_skips_the_weekend_on_first_run():
    saturday = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)  # суббота 11:00 по Москве
    assert due(Spec("x", time=time(18, 0), repeat="weekdays"), now=saturday).at == msk(2026, 11, 16, 18)


# ------------------------------------------------------------------- повторы ----


def test_next_occurrence_daily_weekly_weekdays():
    first = msk(2026, 11, 13, 9)
    assert next_occurrence(first, "daily", MSK, 13, NOW) == msk(2026, 11, 14, 9)
    assert next_occurrence(first, "weekly", MSK, 13, NOW) == msk(2026, 11, 20, 9)
    # с пятницы по будням — сразу на понедельник
    assert next_occurrence(first, "weekdays", MSK, 13, NOW) == msk(2026, 11, 16, 9)
    assert next_occurrence(first, "none", MSK, 13, NOW) is None


def test_missed_occurrences_are_skipped_not_replayed():
    first = msk(2026, 11, 1, 9)  # бот был выключен две недели
    assert next_occurrence(first, "daily", MSK, 1, NOW) == msk(2026, 11, 14, 9)
    assert next_occurrence(first, "weekly", MSK, 1, NOW) == msk(2026, 11, 15, 9)


def test_monthly_keeps_the_anchor_day_after_short_months():
    now = datetime(2027, 1, 31, 12, tzinfo=UTC)
    jan31 = msk(2027, 1, 31, 9)
    feb = next_occurrence(jan31, "monthly", MSK, 31, now)
    assert feb == msk(2027, 2, 28, 9)
    assert next_occurrence(feb, "monthly", MSK, 31, now) == msk(2027, 3, 31, 9)  # не 28 марта
    dec = next_occurrence(msk(2026, 12, 31, 9), "monthly", MSK, 31, datetime(2026, 12, 31, 12, tzinfo=UTC))
    assert dec == msk(2027, 1, 31, 9)  # переход через год


def test_daily_keeps_the_wall_clock_across_dst():
    berlin = ZoneInfo("Europe/Berlin")  # 28 марта 2027 — переход на летнее время
    first = datetime(2027, 3, 27, 9, 0, tzinfo=berlin).astimezone(UTC)
    now = datetime(2027, 3, 27, 10, 0, tzinfo=UTC)
    following = next_occurrence(first, "daily", berlin, 27, now)
    assert following.astimezone(berlin).strftime("%H:%M") == "09:00"
    assert following - first == timedelta(hours=23)


# ------------------------------------------------------------------ хранилище ----


class ScriptedProvider(BaseProvider):
    """Отвечает на разбор просьбы заданным JSON; обычные запросы — эхом."""

    def __init__(self, parse_reply='{"is_reminder": false}', fail=False):
        super().__init__("fake", "Fake AI", ("m",))
        self.parse_reply = parse_reply
        self.fail = fail
        self.parse_calls = []
        self.chat_calls = 0

    async def complete(self, messages, *, model, max_tokens, temperature):
        if messages[0].content.startswith("Ты разбираешь просьбы"):
            self.parse_calls.append(messages)
            assert temperature == 0.0
            if self.fail:
                raise ProviderUnavailableError("down", provider="fake", model=model)
            return CompletionResult(text=self.parse_reply, provider="fake", model=model)
        self.chat_calls += 1
        return CompletionResult(text="обычный ответ", provider="fake", model=model)


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'rem.db'}")
    await init_db(engine)
    sf = create_session_factory(engine)

    def build(provider=None, clock=None, **kwargs):
        provider = provider or ScriptedProvider()
        clock = clock or Clock()
        ai = AIClient([provider], max_tokens=256, temperature=0.7)
        reminders = Reminders(sf, ai, clock=clock, **kwargs)
        return provider, clock, reminders

    yield build
    await engine.dispose()


async def test_store_create_list_delete_and_isolation(env):
    _, _, rem = env()
    a = await rem.create(USER, "Позвонить маме", msk(2026, 11, 14, 9), tz=MSK)
    b = await rem.create(USER, "Выпить воду", msk(2026, 11, 13, 18), "daily", tz=MSK)
    await rem.create(99, "Чужое", msk(2026, 11, 14, 9), tz=MSK)

    listed = await rem.list_pending(USER)
    assert [r.text for r in listed] == ["Выпить воду", "Позвонить маме"]  # по времени
    assert listed[0].repeat == "daily" and listed[0].due_at == msk(2026, 11, 13, 18)
    assert listed[0].due_at.tzinfo is not None

    assert await rem.delete(99, a.id) is False  # чужое не удалить
    assert await rem.delete(USER, a.id) is True
    assert [r.id for r in await rem.list_pending(USER)] == [b.id]
    assert len(await rem.list_pending(99)) == 1


async def test_store_limit_and_invalid_repeat(env):
    _, _, rem = env(max_pending=2)
    await rem.create(USER, "a", msk(2026, 11, 14), tz=MSK)
    item = await rem.create(USER, "b", msk(2026, 11, 15), "bogus", tz=MSK)
    assert item.repeat == "none"
    with pytest.raises(TooManyRemindersError):
        await rem.create(USER, "c", msk(2026, 11, 16), tz=MSK)


async def test_timezone_roundtrip(env):
    _, _, rem = env()
    assert await rem.get_timezone(USER) is None and await rem.tzinfo(USER) is None
    assert await rem.set_timezone(USER, "utc+3") == "UTC+03:00"
    assert await rem.set_timezone(USER, "Europe/Moscow") == "Europe/Moscow"
    assert await rem.set_timezone(USER, "Nowhere/City") is None
    assert await rem.get_timezone(USER) == "Europe/Moscow"


async def test_snooze_creates_a_one_off_copy(env):
    _, clock, rem = env()
    original = await rem.create(USER, "Позвонить маме", msk(2026, 11, 13, 9), "daily", tz=MSK)
    snoozed = await rem.snooze(USER, original.id, 10)
    assert snoozed.text == "Позвонить маме" and snoozed.repeat == "none"
    assert snoozed.due_at == clock.now + timedelta(minutes=10)
    assert await rem.snooze(99, original.id, 10) is None
    assert await rem.snooze(USER, 12345, 10) is None


async def test_purge_old_removes_only_finished(env):
    _, clock, rem = env()
    old = await rem.create(USER, "старое", NOW - timedelta(days=40))
    await rem.mark_sent(old.id, NOW - timedelta(days=40))
    await rem.create(USER, "будущее", NOW + timedelta(days=1))
    pending_old = await rem.create(USER, "просроченное", NOW - timedelta(days=40))
    assert await rem.purge_old(NOW) == 1
    assert {r.id for r in await rem.list_pending(USER)} >= {pending_old.id}


# ------------------------------------------------------------- просьба из чата ----


def intent_reply(**fields):
    import json

    return json.dumps({"is_reminder": True, **fields}, ensure_ascii=False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Напомни завтра в 9 позвонить маме", True),
        ("напомните мне про встречу", True),
        ("напоминай каждый день пить воду", True),
        ("Поставь напоминание на пятницу", True),
        ("создай мне напоминание", True),
        ("Что такое напоминание в Android?", False),
        ("Я вспомнил про тебя", False),
        ("Привет", False),
    ],
)
def test_intent_detection(text, expected):
    assert bool(INTENT.search(text)) is expected


async def test_plain_messages_never_reach_the_parser(env):
    provider, _, rem = env()
    assert await rem.handle_chat_request(USER, "Расскажи про Рим") is None
    assert provider.parse_calls == []


async def test_without_timezone_asks_for_it_and_does_not_call_the_model(env):
    provider, _, rem = env()
    assert await rem.handle_chat_request(USER, "напомни завтра в 9 позвонить") == NEED_TIMEZONE
    assert provider.parse_calls == []


async def test_creates_the_reminder_and_confirms_in_code(env):
    provider, _, rem = env(ScriptedProvider(intent_reply(text="позвонить маме", date="2026-11-14", time="09:00")))
    await rem.set_timezone(USER, "Europe/Moscow")
    reply = await rem.handle_chat_request(USER, "напомни завтра в 9 позвонить маме")
    assert reply.startswith("Хорошо, напомню завтра в 09:00: позвонить маме ⏰")
    assert "/reminders" in reply and "Повторять" not in reply
    [item] = await rem.list_pending(USER)
    assert item.text == "позвонить маме" and item.due_at == msk(2026, 11, 14, 9)
    # модели передали текущее местное время и день недели пользователя
    system = provider.parse_calls[0][0].content
    assert "2026-11-13 15:00" in system and "пятницу" in system


async def test_relative_time_is_computed_by_code(env):
    _, _, rem = env(ScriptedProvider(intent_reply(text="проверить пирог", in_minutes=90)))
    await rem.set_timezone(USER, "Europe/Moscow")
    reply = await rem.handle_chat_request(USER, "напомни через полтора часа проверить пирог")
    assert "через 1 ч 30 мин, сегодня в 16:30" in reply
    assert (await rem.list_pending(USER))[0].due_at == NOW + timedelta(minutes=90)


async def test_repeat_and_default_time_are_mentioned(env):
    _, _, rem = env(ScriptedProvider(intent_reply(text="выпить воду", repeat="daily")))
    await rem.set_timezone(USER, "Europe/Moscow")
    reply = await rem.handle_chat_request(USER, "напоминай каждый день выпить воду")
    assert "Повторять: каждый день." in reply and "поставила 09:00" in reply
    assert (await rem.list_pending(USER))[0].repeat == "daily"


async def test_not_a_reminder_falls_through_to_normal_chat(env):
    _, _, rem = env(ScriptedProvider('{"is_reminder": false}'))
    await rem.set_timezone(USER, "Europe/Moscow")
    assert await rem.handle_chat_request(USER, "напомни, как называется столица Перу?") is None
    assert await rem.list_pending(USER) == []


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"text": ""}, "Что именно напомнить"),
        ({"text": "позвонить"}, "Не поняла, когда"),
        ({"text": "позвонить", "date": "2026-11-12", "time": "09:00"}, "уже прошло"),
        ({"text": "позвонить", "date": "2029-01-01", "time": "09:00"}, "не дальше чем на год"),
        ({"text": "а" * 400, "date": "2026-11-14"}, "Слишком длинно"),
    ],
)
async def test_unusable_requests_get_a_clear_answer_and_create_nothing(env, fields, fragment):
    _, _, rem = env(ScriptedProvider(intent_reply(**fields)))
    await rem.set_timezone(USER, "Europe/Moscow")
    reply = await rem.handle_chat_request(USER, "напомни что-нибудь")
    assert fragment in reply
    assert await rem.list_pending(USER) == []


async def test_too_many_reminders_message(env):
    _, _, rem = env(ScriptedProvider(intent_reply(text="ещё", date="2026-11-20", time="09:00")), max_pending=1)
    await rem.set_timezone(USER, "Europe/Moscow")
    await rem.create(USER, "первое", msk(2026, 11, 14))
    assert "уже 1 напоминаний" in await rem.handle_chat_request(USER, "напомни ещё")


async def test_model_failure_propagates_like_normal_chat(env):
    _, _, rem = env(ScriptedProvider(fail=True))
    await rem.set_timezone(USER, "Europe/Moscow")
    with pytest.raises(AllProvidersFailedError):
        await rem.handle_chat_request(USER, "напомни завтра в 9 позвонить")
    assert await rem.list_pending(USER) == []


# --------------------------------------------------- интеграция с диалогом ----


async def test_chat_service_handles_reminders_without_the_llm(env, tmp_path):
    provider, clock, rem = env(ScriptedProvider(intent_reply(text="позвонить маме", date="2026-11-14", time="09:00")))
    await rem.set_timezone(USER, "Europe/Moscow")
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
    await init_db(engine)
    sf = create_session_factory(engine)
    memory = MemoryManager(MessageRepository(sf, store_limit=100), history_limit=30)
    settings = Settings(
        bot_token="1:x", allowed_user_ids=frozenset({USER}),
        providers=(ProviderConfig("fake", "Fake", "http://x", "", ("m",)),),
        database_url="", assistant_name="Эля", history_max_messages=30, history_max_chars=12000,
        history_store_limit=500, max_output_tokens=256, temperature=0.5, request_timeout=30,
        rate_limit_per_minute=10, daily_request_limit=100, log_level="INFO",
    )
    chat = ChatService(settings, AIClient([provider], max_tokens=1, temperature=0), memory, None, None, rem)

    result = await chat.ask(USER, "напомни завтра в 9 позвонить маме")
    assert result.provider == "reminders" and result.model == "" and not result.truncated
    assert "Хорошо, напомню завтра в 09:00" in result.text
    assert provider.chat_calls == 0
    history = await memory.get_history(USER)
    assert [m.content for m in history] == ["напомни завтра в 9 позвонить маме", result.text]
    assert len(await rem.list_pending(USER)) == 1

    ordinary = await chat.ask(USER, "Как дела?")
    assert ordinary.text == "обычный ответ" and provider.chat_calls == 1
    await engine.dispose()


# ----------------------------------------------------------------- планировщик ----


class Outbox:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def __call__(self, item, note):
        if self.error:
            raise self.error
        self.sent.append((item.text, note))


async def test_scheduler_sends_due_reminders_once(env):
    _, clock, rem = env()
    await rem.set_timezone(USER, "Europe/Moscow")
    await rem.create(USER, "сейчас", NOW - timedelta(seconds=30))
    await rem.create(USER, "позже", NOW + timedelta(hours=1))
    outbox = Outbox()
    scheduler = ReminderScheduler(rem, outbox)

    assert await scheduler.tick() == 1
    assert outbox.sent == [("сейчас", None)]
    assert await scheduler.tick() == 0  # повторно не отправляется
    assert [r.text for r in await rem.list_pending(USER)] == ["позже"]

    clock.now += timedelta(hours=2)
    assert await scheduler.tick() == 1
    assert [t for t, _ in outbox.sent] == ["сейчас", "позже"]


async def test_scheduler_adds_a_note_when_the_bot_was_offline(env):
    _, _, rem = env()
    await rem.set_timezone(USER, "Europe/Moscow")
    await rem.create(USER, "пропущенное", msk(2026, 11, 13, 9))  # было в 09:00, сейчас 15:00
    outbox = Outbox()
    await ReminderScheduler(rem, outbox).tick()
    text, note = outbox.sent[0]
    assert "сегодня в 09:00" in note and "недоступен" in note


async def test_recurring_reminder_moves_to_the_next_future_time(env):
    _, clock, rem = env()
    await rem.set_timezone(USER, "Europe/Moscow")
    daily = await rem.create(USER, "вода", msk(2026, 11, 1, 9), "daily", tz=MSK)  # две недели назад
    outbox = Outbox()
    await ReminderScheduler(rem, outbox).tick()
    assert len(outbox.sent) == 1  # одно напоминание, а не 12 пропущенных
    [item] = await rem.list_pending(USER)
    assert item.id == daily.id and item.due_at == msk(2026, 11, 14, 9) and item.attempts == 0
    assert await ReminderScheduler(rem, outbox).tick() == 0


async def test_transient_failures_are_retried_then_given_up(env):
    _, _, rem = env()
    await rem.create(USER, "не доходит", NOW - timedelta(seconds=5))
    scheduler = ReminderScheduler(rem, Outbox(DeliveryError("сеть")), max_attempts=3)
    for _ in range(2):
        assert await scheduler.tick() == 0
        assert len(await rem.list_pending(USER)) == 1  # ещё пробуем
    await scheduler.tick()
    assert await rem.list_pending(USER) == []  # сдались, но не зациклились


async def test_failed_delivery_succeeds_on_a_later_tick(env):
    _, _, rem = env()
    await rem.create(USER, "дойдёт", NOW - timedelta(seconds=5))
    outbox = Outbox(DeliveryError("сеть"))
    scheduler = ReminderScheduler(rem, outbox)
    await scheduler.tick()
    outbox.error = None
    assert await scheduler.tick() == 1
    assert outbox.sent[0][0] == "дойдёт"


async def test_permanent_failure_is_not_retried(env):
    _, _, rem = env()
    await rem.create(USER, "заблокировано", NOW - timedelta(seconds=5))
    scheduler = ReminderScheduler(rem, Outbox(DeliveryError("blocked", permanent=True)))
    await scheduler.tick()
    assert await rem.list_pending(USER) == []


async def test_one_bad_reminder_does_not_stop_the_others(env):
    _, _, rem = env()
    await rem.create(USER, "первое", NOW - timedelta(minutes=2))
    await rem.create(USER, "второе", NOW - timedelta(minutes=1))
    sent = []

    async def sender(item, note):
        if item.text == "первое":
            raise DeliveryError("сбой")
        sent.append(item.text)

    await ReminderScheduler(rem, sender).tick()
    assert sent == ["второе"]

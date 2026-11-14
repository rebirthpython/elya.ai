"""Интеграционные тесты: апдейты проходят через настоящий Dispatcher,
а запросы к Telegram перехватываются фейковой сессией."""

from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendChatAction, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult
from bot.keyboards.common import ClearHistoryCallback
from config.settings import ProviderConfig, Settings
from database.session import create_engine, create_session_factory, init_db
from main import create_dispatcher
from services.longterm import LongTermMemory
from services.reminders import Reminders
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.usage import UsageTracker

OWNER_ID = 111
STRANGER_ID = 999


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id or OWNER_ID, type="private"),
                text=method.text,
            )
        if isinstance(method, (SendChatAction, AnswerCallbackQuery)):
            return True
        raise AssertionError(f"unexpected Telegram method {type(method).__name__}")

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, (SendMessage, EditMessageText))]


class EchoProvider(BaseProvider):
    def __init__(self) -> None:
        super().__init__("echo", "Echo AI", ("echo-1",))
        self.received: list = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.received.append(messages)
        return CompletionResult(text=f"**Ответ** на: {messages[-1].content}", provider="echo", model=model)


def _settings(**overrides) -> Settings:
    values = dict(
        bot_token="42:TEST", allowed_user_ids=frozenset({OWNER_ID}),
        providers=(ProviderConfig("echo", "Echo AI", "http://x", "", ("echo-1",)),),
        database_url="", assistant_name="Эля", history_max_messages=30, history_max_chars=12000,
        history_store_limit=500, max_output_tokens=256, temperature=0.5, request_timeout=30,
        rate_limit_per_minute=10, daily_request_limit=100, log_level="INFO",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await init_db(engine)
    session_factory = create_session_factory(engine)
    provider = EchoProvider()
    usage = UsageTracker(session_factory, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    memory = MemoryManager(MessageRepository(session_factory, store_limit=500), history_limit=30)
    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)

    long_memory = LongTermMemory(session_factory)
    reminders = Reminders(
        session_factory, ai_client, clock=lambda: datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    )

    def make(settings=None):
        return create_dispatcher(
            settings or _settings(), ai_client, memory, usage,
            long_memory=long_memory, reminders=reminders,
        )

    yield {
        "bot": bot, "session": session, "provider": provider, "memory": memory,
        "usage": usage, "make": make, "long_memory": long_memory, "reminders": reminders,
    }
    await engine.dispose()


_update_id = 0


def _message_update(text: str, user_id: int = OWNER_ID) -> Update:
    global _update_id
    _update_id += 1
    user = User(id=user_id, is_bot=False, first_name="Аня")
    return Update(
        update_id=_update_id,
        message=Message(
            message_id=_update_id, date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"), from_user=user, text=text,
        ),
    )


def _callback_update(data: str, user_id: int = OWNER_ID) -> Update:
    global _update_id
    _update_id += 1
    user = User(id=user_id, is_bot=False, first_name="Аня")
    return Update(
        update_id=_update_id,
        callback_query=CallbackQuery(
            id=str(_update_id), from_user=user, chat_instance="ci", data=data,
            message=Message(
                message_id=1, date=datetime.now(timezone.utc),
                chat=Chat(id=user_id, type="private"), from_user=user, text="confirm?",
            ),
        ),
    )


async def test_stranger_is_ignored(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("привет", user_id=STRANGER_ID))
    assert env["session"].requests == []
    assert env["provider"].received == []


async def test_start_shows_greeting_and_privacy(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("/start"))
    text = env["session"].sent_texts()[0]
    assert "Привет, Аня!" in text and "Эля" in text and "Echo AI" in text


async def test_start_has_no_app_button_when_mini_app_is_off(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("/start"))
    assert env["session"].requests[0].reply_markup is None


async def test_start_offers_mini_app_when_enabled(env):
    dp = env["make"](_settings(webapp_url="https://elya.example/"))
    await dp.feed_update(env["bot"], _message_update("/start"))
    message = env["session"].requests[0]
    button = message.reply_markup.inline_keyboard[0][0]
    assert button.web_app.url == "https://elya.example/"
    assert "приложение" in message.text


async def test_chat_uses_history_and_formats_reply(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("первый вопрос"))
    await dp.feed_update(env["bot"], _message_update("второй вопрос"))

    replies = [m for m in env["session"].requests if isinstance(m, SendMessage)]
    assert replies[-1].text == "<b>Ответ</b> на: второй вопрос"
    assert replies[-1].parse_mode == "HTML"

    second_context = env["provider"].received[1]
    assert second_context[0].role == "system"
    assert [m.content for m in second_context[1:]] == [
        "первый вопрос", "**Ответ** на: первый вопрос", "второй вопрос",
    ]
    assert len(await env["memory"].get_history(OWNER_ID)) == 4


async def test_clear_with_confirmation(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("запомни это"))
    await dp.feed_update(env["bot"], _message_update("/clear"))
    confirm = [m for m in env["session"].requests if isinstance(m, SendMessage)][-1]
    assert confirm.reply_markup is not None

    await dp.feed_update(env["bot"], _callback_update(ClearHistoryCallback(confirm=True).pack()))
    assert "История очищена" in env["session"].sent_texts()[-1]
    assert await env["memory"].get_history(OWNER_ID) == []


async def test_unknown_command_and_unsupported_content(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("/nosuchcommand"))
    assert "не знаю" in env["session"].sent_texts()[-1]
    assert env["provider"].received == []


async def test_throttling_blocks_flood(env):
    dp = env["make"](_settings(rate_limit_per_minute=2))
    for i in range(4):
        await dp.feed_update(env["bot"], _message_update(f"сообщение {i}"))
    assert len(env["provider"].received) == 2
    assert sum("Слишком много" in t for t in env["session"].sent_texts()) == 1


async def test_ai_failure_gives_friendly_message_and_keeps_history_clean(env):
    from ai.providers.base import ProviderUnavailableError

    class DownProvider(BaseProvider):
        def __init__(self):
            super().__init__("down", "Down AI", ("m",))

        async def complete(self, messages, *, model, max_tokens, temperature):
            raise ProviderUnavailableError("timeout", provider="down", model=model)

    dp = create_dispatcher(
        _settings(), AIClient([DownProvider()], max_tokens=10, temperature=0.1),
        env["memory"], env["usage"],
    )
    await dp.feed_update(env["bot"], _message_update("ты тут?"))
    assert "не отвечает" in env["session"].sent_texts()[-1]
    assert await env["memory"].get_history(OWNER_ID) == []


async def test_memory_command_toggle_and_forget(env):
    from bot.keyboards.common import MemoryCallback

    dp = env["make"]()
    lt = env["long_memory"]

    await dp.feed_update(env["bot"], _message_update("/memory"))
    first = [m for m in env["session"].requests if isinstance(m, SendMessage)][-1]
    assert "выключена" in first.text and "Если включить" in first.text
    assert first.reply_markup.inline_keyboard[0][0].text == "🧠 Включить память"
    assert len(first.reply_markup.inline_keyboard) == 1  # «забыть всё» не нужно, фактов нет

    await dp.feed_update(env["bot"], _callback_update(MemoryCallback(action="toggle").pack()))
    assert await lt.is_enabled(OWNER_ID) is True
    assert "Память включена" in env["session"].sent_texts()[-1]
    assert "Пока ничего не запомнила" in env["session"].sent_texts()[-1]

    await lt.add(OWNER_ID, "Любит <чай> & кофе")
    await dp.feed_update(env["bot"], _message_update("/memory"))
    listing = env["session"].sent_texts()[-1]
    assert "• Любит &lt;чай&gt; &amp; кофе" in listing  # разметка экранируется

    await dp.feed_update(env["bot"], _message_update("/forget"))
    confirm = [m for m in env["session"].requests if isinstance(m, SendMessage)][-1]
    assert "Забыть всё" in confirm.text and confirm.reply_markup is not None
    assert len(await lt.list(OWNER_ID)) == 1  # без подтверждения ничего не удалено

    await dp.feed_update(env["bot"], _callback_update(MemoryCallback(action="forget_yes").pack()))
    assert await lt.list(OWNER_ID) == []
    assert "Пока ничего не запомнила" in env["session"].sent_texts()[-1]

    await dp.feed_update(env["bot"], _message_update("/forget"))
    assert "нечего забывать" in env["session"].sent_texts()[-1]


async def test_memory_commands_are_blocked_for_strangers(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("/memory", user_id=STRANGER_ID))
    assert env["session"].requests == []


async def test_timezone_and_reminders_commands(env):
    dp = env["make"]()
    rem = env["reminders"]

    await dp.feed_update(env["bot"], _message_update("/timezone"))
    assert "пока не задан" in env["session"].sent_texts()[-1]
    await dp.feed_update(env["bot"], _message_update("/timezone Mars/Base"))
    assert "Не знаю такого" in env["session"].sent_texts()[-1]
    await dp.feed_update(env["bot"], _message_update("/timezone utc+3"))
    assert "UTC+03:00" in env["session"].sent_texts()[-1]
    await dp.feed_update(env["bot"], _message_update("/timezone"))
    assert "UTC+03:00" in env["session"].sent_texts()[-1] and "/timezone Europe/Moscow" in env["session"].sent_texts()[-1]

    await dp.feed_update(env["bot"], _message_update("/reminders"))
    assert "Напоминаний нет" in env["session"].sent_texts()[-1]

    from datetime import timedelta

    tz = await rem.tzinfo(OWNER_ID)
    first = await rem.create(OWNER_ID, "Позвонить <маме>", rem.now() + timedelta(hours=3), "daily", tz=tz)
    await rem.create(OWNER_ID, "Второе", rem.now() + timedelta(days=1), tz=tz)
    await dp.feed_update(env["bot"], _message_update("/reminders"))
    listing = [m for m in env["session"].requests if isinstance(m, SendMessage)][-1]
    assert "сегодня в 18:00" in listing.text and "Позвонить &lt;маме&gt;" in listing.text
    assert "🔁 каждый день" in listing.text and "завтра в 15:00" in listing.text
    assert [b.text for b in listing.reply_markup.inline_keyboard[0]] == ["🗑 1", "🗑 2"]

    from bot.keyboards.reminders import ReminderCallback

    await dp.feed_update(env["bot"], _callback_update(ReminderCallback(action="del", id=first.id).pack()))
    assert [r.text for r in await rem.list_pending(OWNER_ID)] == ["Второе"]
    assert "Позвонить" not in env["session"].sent_texts()[-1]


async def test_remind_command_without_arguments_explains(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("/remind"))
    assert "напомни завтра в 9" in env["session"].sent_texts()[-1]
    await dp.feed_update(env["bot"], _message_update("/remind завтра в 9 позвонить"))
    assert "часовой пояс" in env["session"].sent_texts()[-1]  # пока пояс не задан


async def test_reminder_buttons_snooze_and_done(env):
    from datetime import timedelta

    from bot.keyboards.reminders import ReminderCallback, fired_keyboard

    dp = env["make"]()
    rem = env["reminders"]
    item = await rem.create(OWNER_ID, "Выпить воду", rem.now() - timedelta(minutes=1))
    assert [b.text for row in fired_keyboard(item.id).inline_keyboard for b in row] == [
        "⏰ +10 мин", "⏰ +1 час", "✅ Готово",
    ]

    await dp.feed_update(env["bot"], _callback_update(ReminderCallback(action="snooze", id=item.id, minutes=10).pack()))
    pending = [r for r in await rem.list_pending(OWNER_ID) if r.id != item.id]
    assert len(pending) == 1 and pending[0].text == "Выпить воду"
    assert pending[0].due_at == rem.now() + timedelta(minutes=10)
    assert "через 10 минут" in env["session"].sent_texts()[-1]

    await dp.feed_update(env["bot"], _callback_update(ReminderCallback(action="snooze", id=987654, minutes=10).pack()))
    assert len(await rem.list_pending(OWNER_ID)) == 2  # несуществующее — ничего не создаёт
    await dp.feed_update(env["bot"], _callback_update(ReminderCallback(action="done", id=item.id).pack()))
    assert "✅ Готово" in env["session"].sent_texts()[-1]


async def test_sender_delivers_with_buttons_and_classifies_errors(env):
    from datetime import timedelta

    from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError

    from bot.handlers.reminders import make_sender
    from services.reminders import DeliveryError, ReminderItem

    rem = env["reminders"]
    item = ReminderItem(7, OWNER_ID, "Позвонить <маме>", rem.now(), "none", 1, "pending", 0)
    await make_sender(env["bot"])(item, "Должно было прийти вчера.")
    sent = [m for m in env["session"].requests if isinstance(m, SendMessage)][-1]
    assert sent.chat_id == OWNER_ID and sent.parse_mode == "HTML"
    assert "<b>Напоминание</b>" in sent.text and "Позвонить &lt;маме&gt;" in sent.text
    assert "Должно было прийти вчера" in sent.text
    assert sent.reply_markup.inline_keyboard[0][0].callback_data.startswith("rem:snooze:7:10")

    class Failing:
        def __init__(self, error):
            self.error = error

        async def send_message(self, *args, **kwargs):
            raise self.error

    blocked = TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
    with pytest.raises(DeliveryError) as info:
        await make_sender(Failing(blocked))(item, None)
    assert info.value.permanent is True
    with pytest.raises(DeliveryError) as info:
        await make_sender(Failing(TelegramNetworkError(method=None, message="timeout")))(item, None)
    assert info.value.permanent is False


async def test_reminder_commands_are_blocked_for_strangers(env):
    dp = env["make"]()
    for command in ("/reminders", "/timezone UTC+3", "/remind завтра в 9 позвонить"):
        await dp.feed_update(env["bot"], _message_update(command, user_id=STRANGER_ID))
    assert env["session"].requests == []


def _voice_update(duration: int = 3, user_id: int = OWNER_ID) -> Update:
    from aiogram.types import Voice

    global _update_id
    _update_id += 1
    user = User(id=user_id, is_bot=False, first_name="Аня")
    return Update(
        update_id=_update_id,
        message=Message(
            message_id=_update_id, date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"), from_user=user,
            voice=Voice(file_id="f", file_unique_id="u", duration=duration),
        ),
    )


class FakeSpeech:
    def __init__(self, text="", error=None):
        self.text, self.error, self.calls = text, error, []

    async def transcribe(self, audio, filename="voice.ogg"):
        self.calls.append(audio)
        if self.error:
            raise self.error
        return self.text


def _voice_dispatcher(env, speech):
    import io

    from main import create_dispatcher as make_dp

    async def download(file, *args, **kwargs):
        return io.BytesIO(b"OGG")

    env["bot"].download = download
    return make_dp(_settings(), AIClient([env["provider"]], max_tokens=10, temperature=0.1),
                   env["memory"], env["usage"], speech=speech)


async def test_voice_is_transcribed_and_answered(env):
    speech = FakeSpeech("запомни что я люблю чай")
    dp = _voice_dispatcher(env, speech)
    await dp.feed_update(env["bot"], _voice_update())
    texts = env["session"].sent_texts()
    assert speech.calls == [b"OGG"]
    assert texts[0] == "🎤 <i>запомни что я люблю чай</i>"
    assert texts[-1] == "<b>Ответ</b> на: запомни что я люблю чай"
    assert [m.content for m in await env["memory"].get_history(OWNER_ID)][0] == "запомни что я люблю чай"


async def test_voice_without_key_explains(env):
    dp = _voice_dispatcher(env, None)
    await dp.feed_update(env["bot"], _voice_update())
    assert "GROQ_API_KEY" in env["session"].sent_texts()[-1]
    assert env["provider"].received == []


async def test_voice_errors_and_limits(env):
    from services.speech import SpeechError

    dp = _voice_dispatcher(env, FakeSpeech(error=SpeechError("down")))
    await dp.feed_update(env["bot"], _voice_update())
    assert "Не получилось разобрать" in env["session"].sent_texts()[-1]

    dp = _voice_dispatcher(env, FakeSpeech(""))
    await dp.feed_update(env["bot"], _voice_update())
    assert "Не расслышала" in env["session"].sent_texts()[-1]

    speech = FakeSpeech("длинное")
    dp = _voice_dispatcher(env, speech)
    await dp.feed_update(env["bot"], _voice_update(duration=600))
    assert "слишком длинное" in env["session"].sent_texts()[-1] and speech.calls == []
    assert env["provider"].received == []


async def test_voice_from_stranger_is_ignored(env):
    speech = FakeSpeech("привет")
    dp = _voice_dispatcher(env, speech)
    await dp.feed_update(env["bot"], _voice_update(user_id=STRANGER_ID))
    assert env["session"].requests == [] and speech.calls == []


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _photo_update(caption=None, document=None, user_id: int = OWNER_ID) -> Update:
    from aiogram.types import Document, PhotoSize

    global _update_id
    _update_id += 1
    user = User(id=user_id, is_bot=False, first_name="Аня")
    extra = {}
    if document is None:
        extra["photo"] = [
            PhotoSize(file_id="small", file_unique_id="s", width=90, height=60),
            PhotoSize(file_id="mid", file_unique_id="m", width=1280, height=853),
            PhotoSize(file_id="huge", file_unique_id="h", width=2560, height=1706),
        ]
    else:
        extra["document"] = Document(file_id="doc", file_unique_id="d", **document)
    return Update(
        update_id=_update_id,
        message=Message(
            message_id=_update_id, date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"), from_user=user, caption=caption, **extra,
        ),
    )


def _photo_dispatcher(env, seeing=True, content=PNG):
    import io

    downloaded = []

    async def download(file, *args, **kwargs):
        downloaded.append(getattr(file, "file_id", None))
        return io.BytesIO(content)

    env["bot"].download = download
    provider = env["provider"]
    provider.vision_models = provider.models if seeing else ()
    dp = create_dispatcher(_settings(), AIClient([provider], max_tokens=10, temperature=0.1), env["memory"], env["usage"])
    return dp, downloaded


async def test_photo_with_caption_is_answered(env):
    dp, downloaded = _photo_dispatcher(env)
    await dp.feed_update(env["bot"], _photo_update("Что это за цветок?"))
    assert downloaded == ["mid"]  # крупнейший размер не больше 1600 px
    context = env["provider"].received[-1]
    assert context[-1].content == "Что это за цветок?" and context[-1].images[0].startswith("data:image/png")
    assert env["session"].sent_texts()[-1] == "<b>Ответ</b> на: Что это за цветок?"
    assert (await env["memory"].get_history(OWNER_ID))[0].content == "📷 Фото: Что это за цветок?"


async def test_photo_without_caption_and_image_document(env):
    dp, downloaded = _photo_dispatcher(env)
    await dp.feed_update(env["bot"], _photo_update())
    assert env["provider"].received[-1][-1].content == "Что на этом фото? Опиши коротко и по делу."
    await dp.feed_update(env["bot"], _photo_update("скрин", document={"mime_type": "image/png", "file_size": 1000}))
    assert downloaded[-1] == "doc" and env["provider"].received[-1][-1].content == "скрин"


async def test_photo_errors(env):
    dp, downloaded = _photo_dispatcher(env)
    await dp.feed_update(env["bot"], _photo_update(document={"mime_type": "image/png", "file_size": 6 * 1024 * 1024}))
    assert "слишком большая" in env["session"].sent_texts()[-1] and downloaded == []

    dp, _ = _photo_dispatcher(env, content=b"not an image")
    await dp.feed_update(env["bot"], _photo_update())
    assert "Не получилось открыть картинку" in env["session"].sent_texts()[-1]

    dp, _ = _photo_dispatcher(env, seeing=False)
    await dp.feed_update(env["bot"], _photo_update("что это?"))
    assert "GEMINI_API_KEY" in env["session"].sent_texts()[-1]
    assert env["provider"].received == []
    assert await env["memory"].get_history(OWNER_ID) == []


async def test_other_documents_are_still_unsupported(env):
    dp, downloaded = _photo_dispatcher(env)
    await dp.feed_update(env["bot"], _photo_update(document={"mime_type": "application/pdf", "file_size": 1000}))
    assert "Стикеры, видео и другие файлы" in env["session"].sent_texts()[-1] and downloaded == []


async def test_photo_from_stranger_is_ignored(env):
    dp, downloaded = _photo_dispatcher(env)
    await dp.feed_update(env["bot"], _photo_update("что это?", user_id=STRANGER_ID))
    assert env["session"].requests == [] and downloaded == []


async def test_slow_answer_appears_early_and_is_edited_into_the_final_reply(env):
    import asyncio

    from aiogram.methods import EditMessageText

    class Slow(BaseProvider):
        def __init__(self):
            super().__init__("slow", "Slow AI", ("s1",))

        async def complete(self, messages, *, model, max_tokens, temperature):
            raise AssertionError("stream expected")

        async def stream(self, messages, *, model, max_tokens, temperature):
            for piece in ("**При", "вет**, ", "Аня! ", "Как дела?"):
                await asyncio.sleep(0.45)
                yield piece
            yield CompletionResult("**Привет**, Аня! Как дела?", "slow", model)

    dp = create_dispatcher(_settings(), AIClient([Slow()], max_tokens=10, temperature=0.1), env["memory"], env["usage"])
    await dp.feed_update(env["bot"], _message_update("привет"))
    sent = [m for m in env["session"].requests if isinstance(m, SendMessage)]
    edits = [m for m in env["session"].requests if isinstance(m, EditMessageText)]
    assert len(sent) == 1 and sent[0].text.endswith(" ▍") and sent[0].parse_mode is None  # показан заранее
    assert edits[-1].text == "<b>Привет</b>, Аня! Как дела?" and edits[-1].parse_mode == "HTML"
    assert [m.content for m in await env["memory"].get_history(OWNER_ID)][-1] == "**Привет**, Аня! Как дела?"

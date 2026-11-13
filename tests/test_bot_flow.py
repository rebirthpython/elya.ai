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

    def make(settings=None):
        return create_dispatcher(
            settings or _settings(), ai_client, memory, usage, long_memory=long_memory
        )

    yield {
        "bot": bot, "session": session, "provider": provider, "memory": memory,
        "usage": usage, "make": make, "long_memory": long_memory,
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
    await dp.feed_update(env["bot"], _message_update("/remind"))
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

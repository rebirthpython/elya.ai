"""Режим глубокого размышления: настройка, два шага ответа, показ в боте и в Mini App."""

import json

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient, AllProvidersFailedError, NoVisionModelError
from ai.providers.base import BaseProvider, CompletionResult, ProviderUnavailableError
from config.settings import load_settings
from database.session import create_engine, create_session_factory, init_db
from main import create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.preferences import PreferencesStore
from services.longterm import LongTermMemory
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.thinking import (
    ThinkingDelta,
    ThinkingDone,
    ThinkingStore,
    clip_middle,
    format_seconds,
)
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, RecordingSession, _callback_update, _message_update
from tests.test_bot_flow import _settings as bot_settings
from tests.test_webapp import _settings as web_settings
from tests.test_webapp import auth
from web.server import create_web_app

THINKING_MARK = "Собеседнику пока не отвечай"


def is_thinking(messages) -> bool:
    return THINKING_MARK in messages[0].content


class Thinker(BaseProvider):
    """На шаге размышления отдаёт thought, на шаге ответа — answer.

    fail_thought_after=N — размышление обрывается после N кусков (0 — до первого)."""

    def __init__(
        self,
        name="a",
        models=("a-m",),
        *,
        thinking_models=(),
        vision_models=(),
        thought=("Шаг 1: 6 × 7. ", "Проверка: 42 / 7 = 6. ", "Вывод: 42."),
        answer=("Ответ: ", "**42**"),
        fail_thought_after=None,
    ):
        super().__init__(name, name.title(), models, vision_models, thinking_models)
        self.thought, self.answer, self.fail_thought_after = thought, answer, fail_thought_after
        self.requests: list[tuple[str, int, list]] = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        raise AssertionError("stream() expected")

    async def stream(self, messages, *, model, max_tokens, temperature):
        self.requests.append((model, max_tokens, messages))
        thinking = is_thinking(messages)
        pieces = self.thought if thinking else self.answer
        for index, piece in enumerate(pieces):
            if thinking and index == self.fail_thought_after:
                raise ProviderUnavailableError("down", provider=self.name, model=model)
            yield piece
        yield CompletionResult("".join(pieces), self.name, model)


async def collect(agen):
    return [event async for event in agen]


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'think.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def memory(db):
    return MemoryManager(MessageRepository(db, store_limit=100), history_limit=30)


def make_service(provider, memory, store=None, **settings_overrides):
    settings = bot_settings(thinking_max_output_tokens=4000, **settings_overrides)
    ticks = iter(range(100, 10_000, 7))  # каждое обращение к часам — +7 секунд
    return ChatService(
        settings,
        AIClient([provider], max_tokens=256, temperature=0.5),
        memory,
        thinking=store,
        clock=lambda: next(ticks),
    )


# ---------------------------------------------------------------- утилиты ----


def test_clip_middle_keeps_start_and_end():
    assert clip_middle("коротко", 100) == "коротко"
    text = "начало " + "середина " * 200 + "вывод"
    clipped = clip_middle(text, 120)
    assert len(clipped) <= 120 and clipped.startswith("начало") and clipped.endswith("вывод")
    assert "[…]" in clipped


def test_format_seconds():
    assert format_seconds(0.2) == "1 с"
    assert format_seconds(23.4) == "23 с"
    assert format_seconds(60) == "1 мин"
    assert format_seconds(75) == "1 мин 15 с"


def test_thinking_models_are_read_per_provider(monkeypatch):
    for key in ("AI_PROVIDERS", "OPENROUTER_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY", "CUSTOM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GEMINI_THINKING_MODELS", "gemini-pro-latest, gemini-flash-latest")
    monkeypatch.setenv("THINKING_MAX_OUTPUT_TOKENS", "6000")
    settings = load_settings(env_file=None)
    assert settings.providers[0].thinking_models == ("gemini-pro-latest", "gemini-flash-latest")
    assert settings.thinking_max_output_tokens == 6000

    monkeypatch.delenv("GEMINI_THINKING_MODELS")
    monkeypatch.delenv("THINKING_MAX_OUTPUT_TOKENS")
    settings = load_settings(env_file=None)
    assert settings.providers[0].thinking_models == () and settings.thinking_max_output_tokens == 8192


def test_thinking_models_go_first_and_regular_ones_stay_as_fallback():
    a = Thinker("a", ("a1", "a2"), thinking_models=("a-pro",))
    b = Thinker("b", ("b1",), thinking_models=("b1",), vision_models=("b1",))
    client = AIClient([a, b], max_tokens=10, temperature=0.1)
    names = lambda pairs: [(p.name, m) for p, m in pairs]  # noqa: E731
    assert names(client._candidates(None)) == [("a", "a1"), ("a", "a2"), ("b", "b1")]
    assert names(client._candidates(None, thinking=True)) == [
        ("a", "a-pro"), ("b", "b1"), ("a", "a1"), ("a", "a2"),
    ]
    # с фото — только модели, которые видят картинки
    assert names(client._candidates(None, vision=True, thinking=True)) == [("b", "b1")]
    # выбранная вручную модель — всё равно первая
    assert names(client._candidates(("a", "a2"), thinking=True))[0] == ("a", "a2")


# ---------------------------------------------------------------- настройка ----


async def test_store_is_off_by_default_and_toggles(db):
    store = ThinkingStore(db)
    assert await store.is_enabled(1) is False
    assert await store.set_enabled(1, True) is True
    assert await store.is_enabled(1) is True and await store.is_enabled(2) is False
    await store.set_enabled(1, False)
    assert await store.is_enabled(1) is False


# ---------------------------------------------------------------- диалог ----


async def test_deep_answer_thinks_first_then_answers(memory):
    provider = Thinker()
    events = await collect(make_service(provider, memory).ask_stream(1, "сколько будет 6×7?", deep=True))

    assert events[0] == ThinkingDelta("")  # «Размышляю…» — сразу
    assert [e.text for e in events[1:4]] == list(provider.thought)
    done = events[4]
    assert isinstance(done, ThinkingDone) and done.reasoning == "".join(provider.thought).strip()
    assert done.seconds > 0
    assert events[5:7] == list(provider.answer)
    result = events[-1]
    assert result.text == "Ответ: **42**" and result.reasoning == done.reasoning

    (_, thinking_tokens, thinking_ctx), (_, answer_tokens, answer_ctx) = provider.requests
    assert is_thinking(thinking_ctx) and thinking_tokens == 4000
    assert thinking_ctx[-1].content == "сколько будет 6×7?"
    assert not is_thinking(answer_ctx) and answer_tokens == 256
    assert "<размышления>\n" + done.reasoning in answer_ctx[0].content
    assert "режим глубокого размышления: вопрос ты уже обдумала" in answer_ctx[0].content

    # в историю — только вопрос и ответ, черновик не копится в контексте
    assert [m.content for m in await memory.get_history(1)] == ["сколько будет 6×7?", "Ответ: **42**"]


async def test_mode_follows_the_user_setting_unless_overridden(db, memory):
    store = ThinkingStore(db)
    provider = Thinker()
    service = make_service(provider, memory, store)

    result = await service.ask(1, "привет")
    assert len(provider.requests) == 1 and result.reasoning == ""
    assert "/think" in provider.requests[0][2][0].content  # обычный ответ подсказывает про режим

    await store.set_enabled(1, True)
    result = await service.ask(1, "задача")
    assert len(provider.requests) == 3 and result.reasoning

    await service.ask(1, "просто так", deep=False)  # разово без размышления
    assert len(provider.requests) == 4


async def test_broken_thinking_still_gives_an_answer_from_the_partial_draft(memory):
    provider = Thinker(fail_thought_after=2)
    events = await collect(make_service(provider, memory).ask_stream(1, "задача", deep=True))
    done = next(e for e in events if isinstance(e, ThinkingDone))
    assert done.reasoning == "Шаг 1: 6 × 7. Проверка: 42 / 7 = 6."
    answer_ctx = provider.requests[-1][2]
    assert "Черновик оборвался" in answer_ctx[0].content
    assert events[-1].text == "Ответ: **42**"


async def test_thinking_that_never_started_is_an_error(memory):
    provider = Thinker(fail_thought_after=0)
    with pytest.raises(AllProvidersFailedError):
        await collect(make_service(provider, memory).ask_stream(1, "задача", deep=True))
    assert await memory.get_history(1) == []


async def test_photo_without_vision_fails_before_thinking_starts(memory):
    events = []
    with pytest.raises(NoVisionModelError):
        async for event in make_service(Thinker(), memory).ask_stream(
            1, "что тут?", images=("data:image/png;base64,AA==",), deep=True
        ):
            events.append(event)
    assert events == []


async def test_reminder_requests_skip_thinking(memory):
    class Handled:
        async def handle_chat_request(self, user_id, text):
            return "Хорошо, напомню ⏰"

    provider = Thinker()
    service = make_service(provider, memory)
    service._reminders = Handled()
    events = await collect(service.ask_stream(1, "напомни завтра в 9 позвонить", deep=True))
    assert len(events) == 1 and events[0].provider == "reminders" and provider.requests == []


# ---------------------------------------------------------- сообщение в боте ----


class FakeBot:
    def __init__(self, reject_html=False):
        self.edits, self.reject_html = [], reject_html

    async def edit_message_text(self, *, text, chat_id, message_id, parse_mode, link_preview_options):
        if self.reject_html and parse_mode == "HTML" and "expandable" in text:
            raise TelegramBadRequest(method=None, message="can't parse entities")
        self.edits.append((text, parse_mode))


class FakeSent:
    class chat:  # noqa: N801
        id = 5

    message_id = 77


class FakeMessage:
    def __init__(self):
        self.answers = []

    async def answer(self, text, parse_mode=None, link_preview_options=None, **kwargs):
        self.answers.append((text, parse_mode))
        return FakeSent()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


async def test_thinking_message_shows_progress_then_collapses():
    from bot.utils.thinking import ThinkingMessage

    clock, bot, message = Clock(), FakeBot(), FakeMessage()
    thinking = ThinkingMessage(bot, message, interval=2.0, clock=clock)
    await thinking.update("")
    assert message.answers == [("💭 <b>Размышляю…</b>", "HTML")] and thinking.active

    clock.now += 1
    await thinking.update("**Шаг 1**: a < b")
    assert bot.edits == []  # слишком часто
    clock.now += 2
    await thinking.update(", значит…")
    text, mode = bot.edits[-1]
    assert mode == "HTML" and text.startswith("💭 <b>Размышляю…</b> 3 с")
    assert "<blockquote>Шаг 1: a &lt; b, значит…</blockquote>" in text  # без Markdown, экранировано

    await thinking.finish("## План\n- **Шаг 1**: a < b\n- Вывод", 23.4)
    text, mode = bot.edits[-1]
    assert mode == "HTML" and text.startswith("💭 <b>Размышляла 23 с</b>")
    assert "<blockquote expandable>План\n• Шаг 1: a &lt; b\n• Вывод</blockquote>" in text
    assert not thinking.active
    await thinking.abort()  # после окончания — ничего не меняет
    assert bot.edits[-1] == (text, mode)


async def test_thinking_message_long_reasoning_fits_and_falls_back_to_plain_text():
    from bot.utils.thinking import ThinkingMessage

    clock, bot, message = Clock(), FakeBot(reject_html=True), FakeMessage()
    thinking = ThinkingMessage(bot, message, clock=clock)
    await thinking.update("")
    await thinking.finish("рассуждение " * 1000, 75)
    text, mode = bot.edits[-1]
    assert mode is None and text.startswith("💭 Размышляла 1 мин 15 с\n\n") and len(text) < 4096


async def test_thinking_message_abort_replaces_the_status():
    from bot.utils.thinking import ThinkingMessage

    bot, message = FakeBot(), FakeMessage()
    thinking = ThinkingMessage(bot, message, clock=Clock())
    await thinking.abort()  # ещё не начиналось — ничего не отправляет
    assert message.answers == [] and bot.edits == []
    await thinking.update("")
    await thinking.abort()
    assert bot.edits == [("💭 Размышление прервано.", None)]


# ------------------------------------------------------------------- бот ----


class EchoThinker(Thinker):
    def __init__(self):
        super().__init__("echo", ("echo-1",), thought=("Думаю о <важном>",), answer=("**Готово**",))


@pytest.fixture
def bot_env(db, memory):
    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    provider = EchoThinker()
    usage = UsageTracker(db, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    store = ThinkingStore(db)

    def make(thinking=store):
        return create_dispatcher(bot_settings(), ai_client, memory, usage, thinking=thinking)

    return {"bot": bot, "session": session, "provider": provider, "make": make, "store": store, "memory": memory}


async def test_think_command_toggles_the_mode(bot_env):
    from bot.keyboards.common import ThinkingCallback

    dp = bot_env["make"]()
    await dp.feed_update(bot_env["bot"], _message_update("/think"))
    shown = [m for m in bot_env["session"].requests if isinstance(m, SendMessage)][-1]
    assert "Глубокое размышление выключено" in shown.text
    assert shown.reply_markup.inline_keyboard[0][0].text == "💭 Включить"

    await dp.feed_update(bot_env["bot"], _callback_update(ThinkingCallback(action="toggle").pack()))
    assert await bot_env["store"].is_enabled(OWNER_ID) is True
    assert "Глубокое размышление включено" in bot_env["session"].sent_texts()[-1]

    # теперь обычные сообщения идут через размышление
    await dp.feed_update(bot_env["bot"], _message_update("сложный вопрос"))
    assert [is_thinking(r[2]) for r in bot_env["provider"].requests] == [True, False]


async def test_think_with_a_question_answers_deeply_once(bot_env):
    dp = bot_env["make"]()
    await dp.feed_update(bot_env["bot"], _message_update("/think почему небо голубое?"))
    requests = bot_env["provider"].requests
    assert [is_thinking(r[2]) for r in requests] == [True, False]
    assert requests[0][2][-1].content == "почему небо голубое?"  # без команды

    sent = [m for m in bot_env["session"].requests if isinstance(m, SendMessage)]
    edits = [m for m in bot_env["session"].requests if isinstance(m, EditMessageText)]
    assert sent[0].text == "💭 <b>Размышляю…</b>"
    assert edits[-1].text.startswith("💭 <b>Размышляла")
    assert "<blockquote expandable>Думаю о &lt;важном&gt;</blockquote>" in edits[-1].text
    assert sent[-1].text == "<b>Готово</b>" and sent[-1].parse_mode == "HTML"
    assert await bot_env["store"].is_enabled(OWNER_ID) is False  # режим не включился

    history = await bot_env["memory"].get_history(OWNER_ID)
    assert [m.content for m in history] == ["почему небо голубое?", "**Готово**"]

    # следующий вопрос — снова обычный
    await dp.feed_update(bot_env["bot"], _message_update("а сейчас?"))
    assert [is_thinking(r[2]) for r in requests] == [True, False, False]


async def test_think_without_store_still_answers_once(bot_env):
    dp = bot_env["make"](thinking=None)
    await dp.feed_update(bot_env["bot"], _message_update("/think"))
    assert "разово" in bot_env["session"].sent_texts()[-1]
    await dp.feed_update(bot_env["bot"], _message_update("/think вопрос"))
    assert len(bot_env["provider"].requests) == 2


async def test_failed_thinking_clears_the_status_in_the_bot(bot_env, db, memory):
    provider = Thinker("echo", ("echo-1",), fail_thought_after=0)
    dp = create_dispatcher(
        bot_settings(), AIClient([provider], max_tokens=10, temperature=0.1), memory, UsageTracker(db, 100)
    )
    await dp.feed_update(bot_env["bot"], _message_update("/think вопрос"))
    texts = bot_env["session"].sent_texts()
    assert texts[0] == "💭 <b>Размышляю…</b>"
    assert "💭 Размышление прервано." in texts and "ИИ-сервис сейчас не отвечает" in texts[-1]
    assert await memory.get_history(OWNER_ID) == []


# -------------------------------------------------------------- Mini App ----


@pytest.fixture
async def web(db, memory):
    clients = []
    store = ThinkingStore(db)

    async def make(provider):
        settings = web_settings()
        usage = UsageTracker(db, 100)
        ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
        preferences = PreferencesStore(db)
        long_memory = LongTermMemory(db)
        reminders = Reminders(db)
        app = create_web_app(
            settings,
            ChatService(settings, ai_client, memory, preferences, long_memory, reminders, thinking=store),
            memory, usage, ai_client, SlidingWindowLimiter(10), preferences, long_memory, reminders,
            thinking=store,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield make, store
    for client in clients:
        await client.close()


async def _ndjson(response):
    return [json.loads(line) for line in (await response.text()).splitlines() if line.strip()]


async def test_web_state_and_toggle(web):
    make, store = web
    client = await make(Thinker())
    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["thinking"] == {"enabled": False}

    response = await client.post("/api/thinking", json={"enabled": True}, headers=auth())
    assert response.status == 200 and await response.json() == {"enabled": True}
    assert await store.is_enabled(OWNER_ID) is True
    for body in ({}, {"enabled": "yes"}):
        assert (await client.post("/api/thinking", json=body, headers=auth())).status == 400
    assert (await client.post("/api/thinking", json={"enabled": True})).status == 401


async def test_web_stream_sends_thinking_before_the_answer(web):
    make, store = web
    provider = Thinker()
    client = await make(provider)
    response = await client.post("/api/chat/stream", json={"text": "6×7?", "deep": True}, headers=auth())
    events = await _ndjson(response)
    kinds = [e["type"] for e in events]
    assert kinds == ["thinking"] * 4 + ["thinking_done", "delta", "delta", "done"]
    assert events[0]["text"] == "" and "".join(e["text"] for e in events[:4]) == "".join(provider.thought)
    assert events[4]["seconds"] >= 0
    reply = events[-1]["reply"]
    assert reply["content"] == "Ответ: **42**" and reply["reasoning"].startswith("Шаг 1")
    assert events[-1]["usage"]["used"] == 2  # два запроса к ИИ

    # без флага — как настроено (выключено): обычный ответ
    events = await _ndjson(await client.post("/api/chat/stream", json={"text": "ещё"}, headers=auth()))
    assert [e["type"] for e in events] == ["delta", "delta", "done"] and events[-1]["reply"]["reasoning"] == ""


async def test_web_plain_chat_uses_the_stored_setting(web):
    make, store = web
    provider = Thinker()
    client = await make(provider)
    await store.set_enabled(OWNER_ID, True)
    data = await (await client.post("/api/chat", json={"text": "задача"}, headers=auth())).json()
    assert data["reply"]["content"] == "Ответ: **42**" and data["reply"]["reasoning"]
    assert [is_thinking(r[2]) for r in provider.requests] == [True, False]

    data = await (await client.post("/api/chat", json={"text": "ок", "deep": False}, headers=auth())).json()
    assert data["reply"]["reasoning"] == "" and len(provider.requests) == 3


async def test_web_photo_can_ask_for_thinking(web):
    from aiohttp import FormData

    from tests.test_webapp import PNG

    make, store = web
    provider = Thinker(vision_models=("a-m",))
    client = await make(provider)
    form = FormData()
    form.add_field("text", "что не так на схеме?")
    form.add_field("deep", "1")
    form.add_field("image", PNG, filename="photo.jpg", content_type="image/jpeg")
    data = await (await client.post("/api/photo", data=form, headers=auth())).json()
    assert data["reply"]["reasoning"] and data["reply"]["content"] == "Ответ: **42**"
    # картинку видят оба шага: и размышление, и ответ
    assert [is_thinking(r[2]) for r in provider.requests] == [True, False]
    assert all(r[2][-1].images for r in provider.requests)

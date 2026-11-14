import asyncio
import json

import httpx
import pytest

from ai.client import AIClient, AllProvidersFailedError
from ai.providers.base import (
    BadResponseError,
    BaseProvider,
    ChatMessage,
    CompletionResult,
    ProviderUnavailableError,
    RateLimitError,
)
from ai.providers.openai_compatible import OpenAICompatibleProvider, _visible_text

MESSAGES = [ChatMessage("system", "sys"), ChatMessage("user", "привет")]


def sse(*chunks, done=True) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def delta(text, finish=None, model=None):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}
    if model:
        chunk["model"] = model
    return chunk


def provider(handler):
    return OpenAICompatibleProvider(
        name="t", title="T", base_url="https://api.test/v1", api_key="k", models=("m",),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def collect(agen):
    return [event async for event in agen]


async def run_stream(handler):
    return await collect(provider(handler).stream(MESSAGES, model="m", max_tokens=50, temperature=0.3))


# ---------------------------------------------------------------- провайдер ----


async def test_sse_stream_yields_pieces_then_result():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=sse(delta("При", model="gemini-x"), delta("вет, "), {"choices": []}, delta("Аня!", "stop")),
        )

    events = await run_stream(handler)
    assert seen["body"]["stream"] is True
    assert events[:-1] == ["При", "вет, ", "Аня!"]
    assert events[-1] == CompletionResult("Привет, Аня!", "t", "gemini-x", False)


async def test_think_blocks_are_hidden_even_when_split_across_chunks():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
            delta("<thi"), delta("nk>секрет"), delta(" мысли</th"), delta("ink>\n\nОтвет"), delta(" готов", "length"),
        ))

    events = await run_stream(handler)
    assert "".join(events[:-1]) == "Ответ готов"
    assert all("think" not in e and "секрет" not in e for e in events[:-1])
    assert events[-1].text == "Ответ готов" and events[-1].truncated is True


@pytest.mark.parametrize(
    ("raw", "visible"),
    [("Привет", "Привет"), ("<think>a</think> Да", "Да"), ("Да <thi", "Да "), ("<think>долго", ""), ("<", "")],
)
def test_visible_text(raw, visible):
    assert _visible_text(raw) == visible


async def test_http_error_before_stream_is_classified():
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "quota"}}, headers={"retry-after": "7"})

    with pytest.raises(RateLimitError) as info:
        await run_stream(handler)
    assert info.value.retry_after == 7.0


async def test_error_inside_the_stream_is_classified():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(delta("Нач"), {"error": {"code": 503, "message": "overloaded"}}, done=False))

    with pytest.raises(ProviderUnavailableError):
        await run_stream(handler)


async def test_server_ignoring_stream_returns_plain_json():
    def handler(request):
        return httpx.Response(200, json={"model": "m2", "choices": [{"message": {"content": "Целиком"}, "finish_reason": "stop"}]})

    assert await run_stream(handler) == ["Целиком", CompletionResult("Целиком", "t", "m2", False)]


async def test_empty_stream_and_network_errors():
    def empty(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(delta("", "content_filter")))

    with pytest.raises(BadResponseError):
        await run_stream(empty)

    def broken(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderUnavailableError):
        await run_stream(broken)


# --------------------------------------------------------------- клиент ИИ ----


class Scripted(BaseProvider):
    """pieces — что отдать; fail_after=N — оборваться после N кусков (0 — до первого)."""

    def __init__(self, name, pieces, fail_after=None):
        super().__init__(name, name, (f"{name}-m",))
        self.pieces, self.fail_after, self.calls = pieces, fail_after, 0

    async def complete(self, messages, *, model, max_tokens, temperature):
        raise AssertionError("stream() expected")

    async def stream(self, messages, *, model, max_tokens, temperature):
        self.calls += 1
        for index, piece in enumerate(self.pieces):
            if self.fail_after == index:
                raise ProviderUnavailableError("down", provider=self.name, model=model)
            yield piece
        yield CompletionResult("".join(self.pieces), self.name, model)


async def test_client_falls_back_until_the_first_piece():
    a = Scripted("a", ["x"], fail_after=0)
    b = Scripted("b", ["Пр", "ивет"])
    events = await collect(AIClient([a, b], max_tokens=10, temperature=0.1).stream(MESSAGES))
    assert events[:-1] == ["Пр", "ивет"] and events[-1].provider == "b"


async def test_client_does_not_switch_models_mid_answer():
    a = Scripted("a", ["Пр", "ивет"], fail_after=1)
    b = Scripted("b", ["другое"])
    shown = []
    with pytest.raises(AllProvidersFailedError) as info:
        async for event in AIClient([a, b], max_tokens=10, temperature=0.1).stream(MESSAGES):
            shown.append(event)
    assert shown == ["Пр"] and b.calls == 0
    assert info.value.kind == "unavailable"


async def test_complete_only_providers_stream_in_one_piece():
    class Plain(BaseProvider):
        async def complete(self, messages, *, model, max_tokens, temperature):
            return CompletionResult("целиком", "p", model)

    events = await collect(AIClient([Plain("p", "P", ("m",))], max_tokens=10, temperature=0.1).stream(MESSAGES))
    assert events == ["целиком", CompletionResult("целиком", "p", "m")]


# ---------------------------------------------------------------- диалог ----


@pytest.fixture
async def chat(tmp_path):
    from config.settings import ProviderConfig, Settings
    from database.session import create_engine, create_session_factory, init_db
    from memory.manager import MemoryManager
    from memory.repository import MessageRepository
    from services.chat import ChatService

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    await init_db(engine)
    memory = MemoryManager(MessageRepository(create_session_factory(engine), store_limit=100), history_limit=30)
    settings = Settings(
        bot_token="1:x", allowed_user_ids=frozenset({1}),
        providers=(ProviderConfig("a", "A", "http://x", "", ("a-m",)),),
        database_url="", assistant_name="Эля", history_max_messages=30, history_max_chars=12000,
        history_store_limit=500, max_output_tokens=256, temperature=0.5, request_timeout=30,
        rate_limit_per_minute=10, daily_request_limit=100, log_level="INFO",
    )

    def build(provider):
        return ChatService(settings, AIClient([provider], max_tokens=10, temperature=0.1), memory)

    yield build, memory
    await engine.dispose()


async def test_ask_stream_saves_only_the_complete_answer(chat):
    build, memory = chat
    service = build(Scripted("a", ["При", "вет!"]))
    events = await collect(service.ask_stream(1, "здравствуй"))
    assert events[:-1] == ["При", "вет!"] and events[-1].text == "Привет!"
    assert [m.content for m in await memory.get_history(1)] == ["здравствуй", "Привет!"]


async def test_abandoned_stream_leaves_history_untouched_and_unlocks(chat):
    build, memory = chat
    service = build(Scripted("a", ["При", "вет!"]))
    agen = service.ask_stream(1, "здравствуй")
    assert await anext(agen) == "При"
    await agen.aclose()  # клиент ушёл
    assert await memory.get_history(1) == []
    result = await asyncio.wait_for(service.ask(1, "ещё раз"), timeout=2)  # блокировка снята
    assert result.text == "Привет!"


# ------------------------------------------------------------- живое сообщение ----


class FakeBot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, *, text, chat_id, message_id, parse_mode, link_preview_options):
        self.edits.append((text, parse_mode))


class FakeChat:
    id = 5


class FakeSent:
    chat = FakeChat()
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


async def test_live_message_appears_after_a_delay_and_is_throttled():
    from bot.utils.live import LiveMessage

    clock, bot, message = Clock(), FakeBot(), FakeMessage()
    live = LiveMessage(bot, message, first_delay=0.8, interval=1.5, clock=clock)
    await live.update("При")
    assert message.answers == [] and not live.started  # ещё рано

    clock.now += 1
    await live.update("Привет")
    assert message.answers == [("Привет ▍", None)] and live.started

    clock.now += 0.5
    await live.update("Привет, как")
    assert bot.edits == []  # слишком часто
    clock.now += 1.1
    await live.update("Привет, как дела")
    assert bot.edits == [("Привет, как дела ▍", None)]

    await live.finish("**Привет**, как дела?")
    assert bot.edits[-1] == ("<b>Привет</b>, как дела?", "HTML")


async def test_short_answers_are_sent_once_without_live_edits():
    from bot.utils.live import LiveMessage

    clock, bot, message = Clock(), FakeBot(), FakeMessage()
    live = LiveMessage(bot, message, clock=clock)
    await live.update("Да")
    await live.finish("**Да**")
    assert bot.edits == [] and message.answers == [("<b>Да</b>", "HTML")]


async def test_long_answers_continue_in_new_messages_and_failures_are_appended():
    from bot.utils.live import LiveMessage

    clock, bot, message = Clock(), FakeBot(), FakeMessage()
    live = LiveMessage(bot, message, clock=clock)
    clock.now += 1
    await live.update("а" * 5000)
    assert message.answers[0][0].endswith("… ▍") and len(message.answers[0][0]) < 4096
    await live.finish("слово " * 1500)
    assert len(bot.edits) == 1 and len(message.answers) >= 2  # хвост — отдельными сообщениями

    clock2, bot2, message2 = Clock(), FakeBot(), FakeMessage()
    live2 = LiveMessage(bot2, message2, clock=clock2)
    clock2.now += 1
    await live2.update("Начало ответа")
    await live2.fail("⚠️ Ответ оборвался")
    assert bot2.edits == [("Начало ответа\n\n⚠️ Ответ оборвался", None)]

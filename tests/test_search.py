"""Поиск в интернете: провайдеры выдачи Google, служебная строка <search>, сноски
и список источников, ответ в два шага в ChatService, команда /search и Mini App."""

import json

import httpx
import pytest
from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient, AllProvidersFailedError
from ai.prompts import (
    NO_INTERNET_RULE,
    SEARCH_AUTO_RULE,
    SEARCH_FAILED_RULE,
    SEARCH_FORCE_RULE,
    SEARCH_RESULTS_RULE,
)
from ai.providers.base import BaseProvider, CompletionResult, ProviderUnavailableError
from config.settings import ConfigError, load_settings
from database.models import DailySearchUsage
from database.session import create_engine, create_session_factory, init_db
from main import create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.longterm import LongTermMemory
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.search import (
    MarkerDetector,
    SearchError,
    SearchLimitError,
    SearchProvider,
    SearchResult,
    SearchStarted,
    SerpApiProvider,
    SerperProvider,
    WebSearch,
    attach_sources,
    parse_query,
    results_block,
    strip_markers,
    strip_sources,
    wants_search,
)
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, RecordingSession, _message_update
from tests.test_bot_flow import _settings as bot_settings
from tests.test_webapp import auth
from web.server import create_web_app

RESULTS = [
    SearchResult("Курсы валют на сегодня", "https://www.bank.example/rates", "Евро — 100 ₽", "14 нояб. 2026"),
    SearchResult("Прогноз курса [евро]", "https://news.example/euro_(prognoz)", "Аналитики ждут роста"),
    SearchResult("Евро — Википедия", "https://ru.wikipedia.org/wiki/Евро", "Валюта 20 стран ЕС"),
]


class ScriptedAI(BaseProvider):
    """Модель, которая по очереди отдаёт заготовленные ответы кусками потока."""

    def __init__(self, *answers: str, fail_on: int | None = None) -> None:
        super().__init__("fake", "Fake AI", ("m1",), vision_models=("m1",))
        self.answers = list(answers)
        self.fail_on = fail_on
        self.calls: list[list] = []
        self.pieces_sent: list[int] = []  # сколько кусков модель успела отдать в каждом вызове
        self.log: list[str] = []  # открытие и закрытие потоков по порядку

    @property
    def systems(self) -> list[str]:
        return [messages[0].content for messages in self.calls]

    async def complete(self, messages, *, model, max_tokens, temperature):
        raise AssertionError("stream expected")

    async def stream(self, messages, *, model, max_tokens, temperature):
        self.calls.append(messages)
        self.pieces_sent.append(0)
        if self.fail_on == len(self.calls):
            raise ProviderUnavailableError("timeout", provider=self.name, model=model)
        answer = self.answers.pop(0)
        self.log.append(f"open{len(self.calls)}")
        try:
            for start in range(0, len(answer), 4):
                self.pieces_sent[-1] += 1
                yield answer[start : start + 4]
            yield CompletionResult(answer, self.name, model)
        finally:
            self.log.append(f"close{len(self.calls)}")


class FakeSearch(SearchProvider):
    def __init__(self, results=RESULTS, *, error: str | None = None, name: str = "fake") -> None:
        super().__init__(name, f"{name.title()} (Google)", http=None)
        self.results = list(results)
        self.error = error
        self.queries: list[str] = []

    async def search(self, query, *, language, region):
        self.queries.append(query)
        if self.error:
            raise SearchError(self.error)
        return list(self.results)


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'search.db'}")
    await init_db(engine)
    session_factory = create_session_factory(engine)
    yield {
        "session_factory": session_factory,
        "memory": MemoryManager(MessageRepository(session_factory, store_limit=500), history_limit=30),
    }
    await engine.dispose()


def make_chat(db, ai: ScriptedAI, provider: SearchProvider | None = None, *, auto: bool = True):
    search = WebSearch([provider], auto=auto) if provider is not None else None
    client = AIClient([ai], max_tokens=100, temperature=0.3)
    return ChatService(bot_settings(), client, db["memory"], search=search)


async def collect(chat: ChatService, text: str, **kwargs) -> list:
    return [event async for event in chat.ask_stream(OWNER_ID, text, **kwargs)]


def text_of(events: list) -> str:
    return "".join(e for e in events if isinstance(e, str))


# ------------------------------------------------------------- провайдеры ----


def mock_http(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_serper_sends_query_and_parses_google_results():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={
            "answerBox": {"title": "Курс евро", "answer": "100,5 ₽", "link": "https://cbr.example/"},
            "knowledgeGraph": {"title": "Евро", "description": "Валюта  ЕС", "descriptionLink": "https://wiki.example/euro"},
            "organic": [
                {"title": "Курсы", "link": "https://bank.example/rates", "snippet": "Евро — 100 ₽", "date": "1 ч назад"},
                {"title": "Плохая ссылка", "link": "javascript:alert(1)", "snippet": "x"},
                {"title": "Без описания", "link": "https://nosnippet.example/"},
            ],
        })

    provider = SerperProvider("serper-key", mock_http(handler))
    results = await provider.search("курс евро", language="ru", region="ru")
    request = seen["request"]
    assert request.method == "POST" and str(request.url) == "https://google.serper.dev/search"
    assert request.headers["X-API-KEY"] == "serper-key"
    assert json.loads(request.content) == {"q": "курс евро", "hl": "ru", "gl": "ru"}
    assert results == [
        SearchResult("Курс евро", "https://cbr.example/", "100,5 ₽"),
        SearchResult("Евро", "https://wiki.example/euro", "Валюта ЕС"),
        SearchResult("Курсы", "https://bank.example/rates", "Евро — 100 ₽", "1 ч назад"),
        SearchResult("Без описания", "https://nosnippet.example/", ""),
    ]
    assert results[2].domain == "bank.example"


async def test_serpapi_sends_query_and_parses_google_results():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={
            "answer_box": {"title": "Погода", "snippet": "+3 °C", "link": "https://weather.example/"},
            "knowledge_graph": {"title": "Казань", "description": "Город", "source": {"link": "https://wiki.example/kazan"}},
            "organic_results": [{"title": "Новости", "link": "https://www.news.example/a", "snippet": "Текст"}],
        })

    provider = SerpApiProvider("serpapi-key", mock_http(handler))
    results = await provider.search("погода казань", language="ru", region="")
    params = dict(seen["request"].url.params)
    assert seen["request"].method == "GET" and seen["request"].url.host == "serpapi.com"
    assert params == {"engine": "google", "q": "погода казань", "api_key": "serpapi-key", "hl": "ru"}
    assert [r.url for r in results] == ["https://weather.example/", "https://wiki.example/kazan", "https://www.news.example/a"]
    assert results[2].domain == "news.example"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"message": "Unauthorized"}),
        httpx.Response(200, text="<html>не JSON</html>"),
        httpx.Response(200, json={"error": "Invalid API key"}),
        httpx.Response(200, json=["не объект"]),
    ],
)
async def test_provider_errors_become_search_errors(response):
    provider = SerperProvider("k", mock_http(lambda request: response))
    with pytest.raises(SearchError):
        await provider.search("запрос", language="ru", region="")


async def test_network_error_becomes_search_error():
    def handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(SearchError, match="network"):
        await SerpApiProvider("k", mock_http(handler)).search("запрос", language="ru", region="")


# ---------------------------------------------------------------- WebSearch ----


async def test_falls_back_to_next_provider_and_pauses_the_failed_one():
    now = [0.0]
    broken, working = FakeSearch(error="HTTP 500", name="first"), FakeSearch(name="second")
    search = WebSearch([broken, working], clock=lambda: now[0], cooldown=300)

    assert await search.search("  курс   евро ") == RESULTS
    assert broken.queries == ["курс евро"] and working.queries == ["курс евро"]
    await search.search("ещё")
    assert broken.queries == ["курс евро"]  # на паузе — не спрашиваем
    now[0] = 301
    await search.search("снова")
    assert broken.queries == ["курс евро", "снова"]


async def test_all_providers_failing_raises():
    search = WebSearch([FakeSearch(error="boom")])
    with pytest.raises(SearchError, match="boom"):
        await search.search("x")
    with pytest.raises(SearchError):
        await search.search("   ")


async def test_results_are_deduplicated_and_limited():
    duplicates = [*RESULTS, SearchResult("Дубль", "https://www.bank.example/rates/", "x")]
    search = WebSearch([FakeSearch(duplicates)], max_results=2)
    assert await search.search("x") == RESULTS[:2]


async def test_daily_search_limit_is_kept_in_its_own_table(db):
    session_factory = db["session_factory"]
    searches = UsageTracker(session_factory, 1, model=DailySearchUsage)
    ai_usage = UsageTracker(session_factory, 10)
    search = WebSearch([FakeSearch()], usage=searches)
    await search.search("раз")
    with pytest.raises(SearchLimitError):
        await search.search("два")
    assert await search.used_today() == 1 and search.daily_limit == 1
    assert await ai_usage.used_today() == 0


# --------------------------------------------- просьба поискать и <search> ----


@pytest.mark.parametrize(
    "text",
    [
        "погугли, когда выйдет новый айфон",
        "Загугли курс евро",
        "найди в интернете рецепт борща",
        "Посмотри в гугле расписание электричек",
        "что пишут в интернете про это?",
        "в интернете поищи отзывы",
        "дай ссылки на источники",
        "ответь с источниками",
        "google it",
    ],
)
def test_explicit_search_requests_are_recognized(text):
    assert wants_search(text)


@pytest.mark.parametrize(
    "text",
    ["как дела?", "найди ошибку в коде", "я работаю в гугле", "объясни теорему Пифагора", "поищи синоним к слову"],
)
def test_ordinary_messages_are_not_search_requests(text):
    assert not wants_search(text)


@pytest.mark.parametrize(
    ("pieces", "mode", "shown", "query"),
    [
        (["<se", "arch>курс ", "евро</search>"], "search", "", "курс евро"),
        (["\n`", "<SEARCH>«погода»", "</search>"], "search", "", "погода"),
        (["При", "вет"], "text", "Привет", ""),
        (["<", "b>жирный</b>"], "text", "<b>жирный</b>", ""),
        (["  ", "Да"], "text", "  Да", ""),
    ],
)
def test_marker_detector(pieces, mode, shown, query):
    detector = MarkerDetector()
    out = "".join(detector.feed(piece) for piece in pieces)
    assert detector.mode == mode and out == shown and detector.query == query


def test_marker_detector_flushes_undecided_tail():
    detector = MarkerDetector()
    assert detector.feed("<se") == "" and detector.mode is None
    assert detector.flush() == "<se" and detector.mode == "text"


def test_query_parsing_and_marker_stripping():
    assert parse_query("<search>  курс\nевро  </search>") == "курс евро"
    assert parse_query("<search>без закрывающего") == "без закрывающего"
    assert parse_query("обычный текст") == ""
    assert parse_query("<search>" + "я" * 500 + "</search>") == "я" * 200
    assert strip_markers("Ответ <search>лишнее</search> готов") == "Ответ готов"


# -------------------------------------------------------------- источники ----


def test_sources_are_renumbered_in_order_of_mention():
    text = attach_sources("Евро стоит 100 ₽ [3]. Прогноз: рост [2, 3][9]. Ещё [7].", RESULTS)
    body, sources = text.split("\n\n**Источники:**\n")
    assert body == "Евро стоит 100 ₽ [1]. Прогноз: рост [1, 2]. Ещё."
    assert sources.splitlines() == [
        "1. [Евро — Википедия](https://ru.wikipedia.org/wiki/%D0%95%D0%B2%D1%80%D0%BE) — ru.wikipedia.org",
        "2. [Прогноз курса (евро)](https://news.example/euro_%28prognoz%29) — news.example",
    ]


def test_without_citations_the_top_results_are_listed():
    text = attach_sources("Курс около 100 ₽.", RESULTS, fallback=2)
    assert text.endswith("1. [Курсы валют на сегодня](https://www.bank.example/rates) — bank.example\n"
                         "2. [Прогноз курса (евро)](https://news.example/euro_%28prognoz%29) — news.example")
    assert attach_sources("Ничего не нашлось.", []) == "Ничего не нашлось."


def test_code_is_not_mistaken_for_citations():
    text = attach_sources("Код `a[0]`:\n```\nx = arr[9]\n```\nФакт [2].", RESULTS)
    assert "`a[0]`" in text and "arr[9]" in text and "Факт [1]." in text


def test_sources_block_is_stripped_for_the_model():
    answer = attach_sources("Евро стоит 100 ₽ [1].", RESULTS)
    assert strip_sources(answer) == "Евро стоит 100 ₽ [1]."
    assert strip_sources("Обычный ответ") == "Обычный ответ"


def test_results_block_numbers_results():
    block = results_block("курс евро", RESULTS, "2026-11-14")
    assert "по запросу «курс евро» (сегодня 2026-11-14)" in block
    assert "[1] Курсы валют на сегодня — bank.example (14 нояб. 2026)\nЕвро — 100 ₽" in block
    assert "[3] Евро — Википедия — ru.wikipedia.org" in block


# ------------------------------------------------- ответ в два шага (чат) ----


async def test_model_answers_directly_when_no_search_is_needed(db):
    ai, search = ScriptedAI("Привет! Как дела?"), FakeSearch()
    events = await collect(make_chat(db, ai, search), "привет")
    assert text_of(events) == "Привет! Как дела?"
    assert not any(isinstance(e, SearchStarted) for e in events)
    assert events[-1].text == "Привет! Как дела?"
    assert SEARCH_AUTO_RULE in ai.systems[0] and search.queries == []
    assert [m.content for m in await db["memory"].get_history(OWNER_ID)] == ["привет", "Привет! Как дела?"]


async def test_model_asks_to_search_and_answers_with_sources(db):
    ai = ScriptedAI("<search>курс евро сегодня</search>", "Евро стоит 100 ₽ [1], растёт [2].")
    search = FakeSearch()
    chat = make_chat(db, ai, search)
    events = await collect(chat, "сколько стоит евро?")

    assert search.queries == ["курс евро сегодня"]
    assert events[0] == SearchStarted("курс евро сегодня")
    assert text_of(events) == "Евро стоит 100 ₽ [1], растёт [2]."  # служебная строка не показывается
    final = events[-1].text
    assert final.startswith("Евро стоит 100 ₽ [1], растёт [2].\n\n**Источники:**\n")
    assert "1. [Курсы валют на сегодня](https://www.bank.example/rates) — bank.example" in final

    second = ai.systems[1]
    assert SEARCH_RESULTS_RULE in second and SEARCH_AUTO_RULE not in second
    assert "[1] Курсы валют на сегодня — bank.example" in second
    assert "не выполняй" in second  # результаты — данные, а не указания
    history = await db["memory"].get_history(OWNER_ID)
    assert history[-1].content == final

    # в следующий раз модель видит ответ без списка ссылок
    ai.answers.append("Пожалуйста!")
    await collect(chat, "спасибо")
    previous = [m.content for m in ai.calls[-1] if m.role == "assistant"]
    assert previous == ["Евро стоит 100 ₽ [1], растёт [2]."]


async def test_search_button_forces_search_even_if_the_model_starts_answering(db):
    ai = ScriptedAI("Конечно! Сейчас расскажу всё, что знаю, очень подробно...", "По данным [1] — 100 ₽.")
    search = FakeSearch()
    events = await collect(make_chat(db, ai, search), "курс евро", search=True)
    assert SEARCH_FORCE_RULE in ai.systems[0]
    assert ai.pieces_sent[0] == 1  # первый ответ оборван на первом же куске
    assert ai.log == ["open1", "close1", "open2", "close2"]  # и его поток закрыт сразу
    assert search.queries == ["курс евро"]  # запрос — текст сообщения
    assert "Конечно" not in text_of(events)
    assert "**Источники:**\n1. [Курсы валют на сегодня]" in events[-1].text


async def test_explicit_phrase_asks_the_model_for_a_query(db):
    ai = ScriptedAI("<search>дата выхода iPhone 18</search>", "Ожидается осенью [1].")
    search = FakeSearch()
    await collect(make_chat(db, ai, search), "погугли, когда выйдет новый айфон")
    assert SEARCH_FORCE_RULE in ai.systems[0]
    assert search.queries == ["дата выхода iPhone 18"]


async def test_failed_search_is_answered_from_memory_with_a_warning(db):
    ai = ScriptedAI("<search>погода в Казани</search>", "Не смогла проверить в интернете, но обычно там холодно.")
    events = await collect(make_chat(db, ai, FakeSearch(error="HTTP 403")), "какая погода в Казани?")
    assert SearchStarted("погода в Казани") in events
    assert SEARCH_FAILED_RULE in ai.systems[1]
    assert "Источники" not in events[-1].text


async def test_without_search_the_prompt_says_there_is_no_internet(db):
    ai = ScriptedAI("Не знаю свежих новостей.", "Тоже не знаю.")
    chat = make_chat(db, ai)
    assert not chat.search_enabled
    await collect(chat, "какие новости?")
    await collect(chat, "погугли новости", search=True)
    assert all(NO_INTERNET_RULE in system for system in ai.systems) and len(ai.calls) == 2


async def test_photos_are_answered_without_search(db):
    ai = ScriptedAI("На фото кот.")
    await collect(make_chat(db, ai, FakeSearch()), "погугли, что это", images=("data:image/png;base64,AA==",))
    assert NO_INTERNET_RULE in ai.systems[0]


async def test_manual_mode_searches_only_on_request(db):
    ai = ScriptedAI("Ответ по памяти.", "<search>новости</search>", "Свежее [1].")
    search = FakeSearch()
    chat = make_chat(db, ai, search, auto=False)
    await collect(chat, "какие новости?")
    assert NO_INTERNET_RULE in ai.systems[0] and search.queries == []
    await collect(chat, "найди в интернете новости")
    assert SEARCH_FORCE_RULE in ai.systems[1] and search.queries == ["новости"]


async def test_ai_failure_after_search_keeps_history_clean(db):
    ai = ScriptedAI("<search>курс</search>", fail_on=2)
    with pytest.raises(AllProvidersFailedError):
        await collect(make_chat(db, ai, FakeSearch()), "курс евро?")
    assert await db["memory"].get_history(OWNER_ID) == []


async def test_ask_returns_the_answer_with_sources(db):
    ai = ScriptedAI("<search>курс евро</search>", "100 ₽ [1].")
    result = await make_chat(db, ai, FakeSearch()).ask(OWNER_ID, "курс евро?")
    assert result.text.startswith("100 ₽ [1].\n\n**Источники:**")


# ---------------------------------------------------------------- настройки ----

SEARCH_ENV = [
    "SEARCH_PROVIDERS", "SERPER_API_KEY", "SERPAPI_API_KEY", "SEARCH_AUTO", "SEARCH_LANGUAGE",
    "SEARCH_REGION", "SEARCH_MAX_RESULTS", "SEARCH_DAILY_LIMIT", "AI_PROVIDERS",
]


@pytest.fixture
def search_env(monkeypatch):
    for key in SEARCH_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("AI_PROVIDERS", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    return monkeypatch


def test_search_is_off_without_keys(search_env):
    config = load_settings(env_file=None).search
    assert not config.enabled and config.auto and config.daily_limit == 100 and config.max_results == 6


def test_search_settings(search_env):
    search_env.setenv("SEARCH_PROVIDERS", "serpapi, serper")
    search_env.setenv("SERPER_API_KEY", "s1")
    search_env.setenv("SERPAPI_API_KEY", "s2")
    search_env.setenv("SEARCH_AUTO", "false")
    search_env.setenv("SEARCH_REGION", "RU")
    search_env.setenv("SEARCH_MAX_RESULTS", "8")
    search_env.setenv("SEARCH_DAILY_LIMIT", "0")
    config = load_settings(env_file=None).search
    assert config.providers == (("serpapi", "s2"), ("serper", "s1"))
    assert (config.auto, config.region, config.language, config.max_results, config.daily_limit) == (
        False, "ru", "ru", 8, 0,
    )


def test_provider_without_key_is_skipped(search_env):
    search_env.setenv("SERPAPI_API_KEY", "s2")
    assert load_settings(env_file=None).search.providers == (("serpapi", "s2"),)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("SEARCH_PROVIDERS", "bing", "неизвестный провайдер поиска"),
        ("SEARCH_AUTO", "maybe", "SEARCH_AUTO"),
        ("SEARCH_MAX_RESULTS", "11", "SEARCH_MAX_RESULTS"),
        ("SEARCH_MAX_RESULTS", "0", "SEARCH_MAX_RESULTS"),
    ],
)
def test_bad_search_settings(search_env, key, value, message):
    search_env.setenv(key, value)
    with pytest.raises(ConfigError, match=message):
        load_settings(env_file=None)


# -------------------------------------------------------------- бот: /search ----


@pytest.fixture
def telegram():
    session = RecordingSession()
    return session, Bot(token="42:TEST", session=session)


def bot_dispatcher(db, ai: ScriptedAI, provider: SearchProvider | None):
    usage = UsageTracker(db["session_factory"], 100)
    searches = UsageTracker(db["session_factory"], 100, model=DailySearchUsage)
    search = WebSearch([provider], usage=searches) if provider else None
    return create_dispatcher(
        bot_settings(), AIClient([ai], max_tokens=100, temperature=0.3, usage=usage),
        db["memory"], usage, search=search,
    )


async def test_search_command_shows_progress_and_links(db, telegram):
    session, bot = telegram
    ai, search = ScriptedAI("<search>курс евро</search>", "Евро стоит **100 ₽** [1]."), FakeSearch()
    await bot_dispatcher(db, ai, search).feed_update(bot, _message_update("/search курс евро"))

    sent = [m for m in session.requests if isinstance(m, SendMessage)]
    edits = [m for m in session.requests if isinstance(m, EditMessageText)]
    assert sent[0].text == "🔎 Ищу в интернете: «курс евро»…" and sent[0].parse_mode is None
    final = edits[-1]
    assert final.parse_mode == "HTML"
    assert final.text.startswith("Евро стоит <b>100 ₽</b> [1].\n\n<b>Источники:</b>\n")
    assert '1. <a href="https://www.bank.example/rates">Курсы валют на сегодня</a> — bank.example' in final.text
    assert SEARCH_FORCE_RULE in ai.systems[0]
    assert (await db["memory"].get_history(OWNER_ID))[0].content == "курс евро"


async def test_search_command_help_and_missing_config(db, telegram):
    session, bot = telegram
    await bot_dispatcher(db, ScriptedAI(), FakeSearch()).feed_update(bot, _message_update("/search"))
    assert "/search курс евро сегодня" in session.sent_texts()[-1] and "поищу сама" in session.sent_texts()[-1]
    await bot_dispatcher(db, ScriptedAI(), None).feed_update(bot, _message_update("/search курс"))
    assert "SERPER_API_KEY" in session.sent_texts()[-1]


async def test_failed_answer_after_search_replaces_the_status(db, telegram):
    session, bot = telegram
    ai = ScriptedAI("<search>курс</search>", fail_on=2)
    await bot_dispatcher(db, ai, FakeSearch()).feed_update(bot, _message_update("курс евро?"))
    last = session.sent_texts()[-1]
    assert "Ищу" not in last and "ИИ-сервис" in last


async def test_help_and_start_mention_search(db, telegram):
    session, bot = telegram
    dp = bot_dispatcher(db, ScriptedAI(), FakeSearch())
    await dp.feed_update(bot, _message_update("/help"))
    assert "/search" in session.sent_texts()[-1] and "Поисков в интернете сегодня: 0 из 100" in session.sent_texts()[-1]
    await dp.feed_update(bot, _message_update("/start"))
    assert "Fake (Google)" in session.sent_texts()[-1]  # приватность: куда уходят запросы
    assert "когда нужны свежие сведения" in session.sent_texts()[-1]


# ------------------------------------------------------------------ Mini App ----


async def web_client(db, ai: ScriptedAI, provider: SearchProvider | None) -> TestClient:
    from tests.test_webapp import _settings as web_settings

    session_factory = db["session_factory"]
    settings = web_settings()
    usage = UsageTracker(session_factory, 100)
    ai_client = AIClient([ai], max_tokens=100, temperature=0.3, usage=usage)
    search = WebSearch([provider]) if provider else None
    preferences, long_memory = PreferencesStore(session_factory), LongTermMemory(session_factory)
    reminders = Reminders(session_factory)
    app = create_web_app(
        settings,
        ChatService(settings, ai_client, db["memory"], preferences, long_memory, reminders, search=search),
        db["memory"], usage, ai_client, SlidingWindowLimiter(10), preferences, long_memory, reminders,
        None, None, search,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_mini_app_stream_reports_search_and_returns_sources(db):
    ai = ScriptedAI("<search>курс евро</search>", "100 ₽ [1].")
    client = await web_client(db, ai, FakeSearch())
    try:
        state = await (await client.get("/api/state", headers=auth())).json()
        assert state["search"] is True
        response = await client.post("/api/chat/stream", json={"text": "курс евро", "search": True}, headers=auth())
        events = [json.loads(line) for line in (await response.text()).splitlines() if line.strip()]
        assert events[0] == {"type": "search", "query": "курс евро"}
        assert "".join(e["text"] for e in events if e["type"] == "delta") == "100 ₽ [1]."
        assert events[-1]["type"] == "done"
        assert events[-1]["reply"]["content"].startswith("100 ₽ [1].\n\n**Источники:**\n1. [Курсы валют")
        assert SEARCH_FORCE_RULE in ai.systems[0]
    finally:
        await client.close()


async def test_mini_app_without_search(db):
    ai = ScriptedAI("Ответ.")
    client = await web_client(db, ai, None)
    try:
        assert (await (await client.get("/api/state", headers=auth())).json())["search"] is False
        response = await client.post("/api/chat", json={"text": "новости", "search": True}, headers=auth())
        assert (await response.json())["reply"]["content"] == "Ответ."
        assert NO_INTERNET_RULE in ai.systems[0]
    finally:
        await client.close()


# ------------------------------------------- сквозной сценарий по HTTP-мокам ----


async def test_end_to_end_with_real_provider_and_serper(db):
    """OpenAI-совместимый поток (SSE) + Serper: служебная строка приходит кусками."""
    from ai.providers.openai_compatible import OpenAICompatibleProvider

    def sse(*pieces: str) -> bytes:
        chunks = [{"choices": [{"delta": {"content": p}, "finish_reason": None}]} for p in pieces]
        return "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"

    prompts: list[str] = []

    def llm(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        pieces = ("<sea", "rch>погода ", "Казань</sea", "rch>") if len(prompts) == 1 else ("Сейчас +3 °C ", "[1].")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(*pieces))

    def serper(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["q"] == "погода Казань"
        return httpx.Response(200, json={"organic": [
            {"title": "Погода в Казани", "link": "https://weather.example/kazan", "snippet": "+3 °C, облачно"},
        ]})

    provider = OpenAICompatibleProvider(
        name="llm", title="LLM", base_url="https://llm.example/v1", api_key="k", models=("m",),
        http_client=mock_http(llm),
    )
    search = WebSearch([SerperProvider("key", mock_http(serper))])
    chat = ChatService(bot_settings(), AIClient([provider], max_tokens=100, temperature=0.3), db["memory"], search=search)
    events = await collect(chat, "какая погода в Казани?")

    assert SearchStarted("погода Казань") in events
    assert text_of(events) == "Сейчас +3 °C [1]."
    assert events[-1].text == (
        "Сейчас +3 °C [1].\n\n**Источники:**\n1. [Погода в Казани](https://weather.example/kazan) — weather.example"
    )
    assert "[1] Погода в Казани — weather.example\n+3 °C, облачно" in prompts[1]

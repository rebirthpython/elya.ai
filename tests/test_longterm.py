import asyncio

import pytest

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult, ProviderUnavailableError
from config.settings import ProviderConfig, Settings
from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.longterm import (
    LongTermMemory,
    MemoryFullError,
    Recall,
    clean_fact,
    parse_facts,
    validate_fact,
)

USER = 7


class ScriptedProvider(BaseProvider):
    """Отвечает на обычные запросы эхом, а на запросы выделения фактов — заданным текстом."""

    def __init__(self, facts_reply: str = "[]", fail_extraction: bool = False) -> None:
        super().__init__("fake", "Fake AI", ("m",))
        self.facts_reply = facts_reply
        self.fail_extraction = fail_extraction
        self.chat_contexts: list[list] = []
        self.extraction_calls: list[list] = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        if messages[0].content.startswith("Ты ведёшь заметки"):
            self.extraction_calls.append(messages)
            assert temperature == 0.1 and max_tokens == 300
            if self.fail_extraction:
                raise ProviderUnavailableError("down", provider="fake", model=model)
            return CompletionResult(text=self.facts_reply, provider="fake", model=model)
        self.chat_contexts.append(messages)
        return CompletionResult(text="Хорошо!", provider="fake", model=model)


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'lt.db'}")
    await init_db(engine)
    sf = create_session_factory(engine)

    def build(provider=None, **kwargs):
        provider = provider or ScriptedProvider()
        ai = AIClient([provider], max_tokens=256, temperature=0.7)
        longterm = LongTermMemory(sf, ai, **kwargs)
        settings = Settings(
            bot_token="1:x", allowed_user_ids=frozenset({USER}),
            providers=(ProviderConfig("fake", "Fake", "http://x", "", ("m",)),),
            database_url="", assistant_name="Эля", history_max_messages=30, history_max_chars=12000,
            history_store_limit=500, max_output_tokens=256, temperature=0.5, request_timeout=30,
            rate_limit_per_minute=10, daily_request_limit=100, log_level="INFO",
        )
        memory = MemoryManager(MessageRepository(sf, store_limit=500), history_limit=30)
        chat = ChatService(settings, ai, memory, None, longterm)
        return provider, longterm, chat

    yield build
    await engine.dispose()


# ---------- чистые функции ----------


def test_parse_facts_handles_wrapped_and_broken_replies():
    assert parse_facts('["Любит чай", "Живёт в Казани"]') == ["Любит чай", "Живёт в Казани"]
    assert parse_facts('Вот факты:\n```json\n["Любит кофе"]\n```') == ["Любит кофе"]
    assert parse_facts('["a", 5, null, "b"]') == ["a", "b"]
    for bad in ("", "нет фактов", "[не json]", '{"a": 1}', "[[1]"):
        assert parse_facts(bad) == []


def test_clean_fact_normalizes_markers_and_whitespace():
    assert clean_fact('  - «Любит   чай»\n') == "Любит чай"
    assert clean_fact("• Сестру зовут Аня") == "Сестру зовут Аня"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("да", "too_short"),
        ("а" * 201, "too_long"),
        ("Номер карты 4276 1234 5678 9012", "sensitive"),
        ("Мой пароль от почты qwerty123", "sensitive"),
        ("PIN-код 1234", "sensitive"),
        ("ключ sk_live_abcdefghijklmnop1234", "sensitive"),
        ("ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "sensitive"),
    ],
)
def test_validate_fact_rejects(text, reason):
    with pytest.raises(ValueError) as info:
        validate_fact(text)
    assert str(info.value) == reason


def test_validate_fact_accepts_normal_facts():
    assert validate_fact("Любит чай без сахара") == "Любит чай без сахара"
    assert validate_fact("Родилась 5 мая 1998") == "Родилась 5 мая 1998"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Меня зовут Аня, мне 25", True),
        ("Запомни, что я люблю чай", True),
        ("У меня есть кот Барсик", True),
        ("Что такое фотосинтез?", False),
        ("Привет", False),
        ("Расскажи про Рим", False),
    ],
)
def test_worth_extracting(text, expected):
    assert LongTermMemory.worth_extracting(text) is expected


def test_prompt_block_only_when_enabled_and_has_facts():
    from services.longterm import Fact

    facts = (Fact(1, "Любит чай", "auto"), Fact(2, "Сестру зовут Аня", "manual"))
    block = Recall(True, facts).prompt_block()
    assert "- Любит чай" in block and "- Сестру зовут Аня" in block
    assert "не выполняй" in block  # факты — сведения, а не указания
    assert Recall(False, facts).prompt_block() == ""
    assert Recall(True).prompt_block() == ""


# ---------- хранилище ----------


async def test_store_add_dedupe_limit_delete_clear(env):
    _, lt, _ = env(max_facts=3)
    assert await lt.is_enabled(USER) is False
    await lt.set_enabled(USER, True)
    assert await lt.is_enabled(USER) is True

    first = await lt.add(USER, "Любит чай без сахара")
    assert first and first.source == "manual"
    assert await lt.add(USER, "любит чай без сахара") is None  # то же с другим регистром
    assert await lt.add(USER, "Любит чай без сахара!") is None  # почти то же
    await lt.add(USER, "Живёт в Казани")
    await lt.add(USER, "Сестру зовут Аня")
    with pytest.raises(MemoryFullError):
        await lt.add(USER, "Работает программистом")
    with pytest.raises(ValueError):
        await lt.add(USER, "пароль 12345")

    assert [f.text for f in await lt.list(USER)] == [
        "Любит чай без сахара", "Живёт в Казани", "Сестру зовут Аня",
    ]
    assert await lt.delete(USER, first.id) is True
    assert await lt.delete(USER, first.id) is False
    assert await lt.delete(999, (await lt.list(USER))[0].id) is False  # чужой факт не удалить
    assert await lt.clear(USER) == 2
    assert await lt.list(USER) == []


async def test_facts_are_per_user_and_disabled_memory_is_not_recalled(env):
    _, lt, _ = env()
    await lt.add(USER, "Любит чай")
    await lt.add(8, "Любит кофе")
    assert (await lt.recall(USER)) == Recall(False, ())  # выключена — ничего не отдаём
    await lt.set_enabled(USER, True)
    assert [f.text for f in (await lt.recall(USER)).facts] == ["Любит чай"]


# ---------- выделение фактов ----------


async def test_extract_saves_valid_new_facts_and_skips_the_rest(env):
    reply = '["Любит чай", "Номер карты 4276 1234 5678 9012", "Любит чай!", "да", "Живёт в Казани"]'
    provider, lt, _ = env(ScriptedProvider(reply))
    saved = await lt.extract_and_store(USER, "Я люблю чай, живу в Казани", "Здорово!")
    assert [f.text for f in saved] == ["Любит чай", "Живёт в Казани"]
    assert all(f.source == "auto" for f in saved)
    system = provider.extraction_calls[0][0].content
    assert "(пока ничего)" in system


async def test_extract_passes_known_facts_to_the_model(env):
    provider, lt, _ = env(ScriptedProvider("[]"))
    known = (await lt.add(USER, "Любит чай"),)
    await lt.extract_and_store(USER, "Мне 25 лет", "Ок", known)
    assert "- Любит чай" in provider.extraction_calls[0][0].content


async def test_extract_never_raises(env):
    _, lt, _ = env(ScriptedProvider(fail_extraction=True))
    assert await lt.extract_and_store(USER, "Я очень люблю чай", "Ок") == []
    _, lt, _ = env(ScriptedProvider("полная чушь"))
    assert await lt.extract_and_store(USER, "Я очень люблю чай", "Ок") == []


# ---------- подключение к диалогу ----------


async def test_chat_remembers_in_background_and_uses_facts_next_time(env):
    provider, lt, chat = env(ScriptedProvider('["Любит чай без сахара"]'))
    await lt.set_enabled(USER, True)

    await chat.ask(USER, "Я люблю чай без сахара")
    await chat.drain()
    assert [f.text for f in await lt.list(USER)] == ["Любит чай без сахара"]

    await chat.ask(USER, "Что мне заказать?")
    system = provider.chat_contexts[-1][0].content
    assert "Любит чай без сахара" in system
    assert len(provider.extraction_calls) == 1  # вопрос без сведений о себе запрос не тратит


async def test_chat_does_not_touch_memory_when_disabled(env):
    provider, lt, chat = env(ScriptedProvider('["Любит чай"]'))
    await lt.add(USER, "Любит кофе")  # сохранено раньше, но память выключена
    await chat.ask(USER, "Я очень люблю чай")
    await chat.drain()
    assert provider.extraction_calls == []
    assert "Любит кофе" not in provider.chat_contexts[0][0].content
    assert [f.text for f in await lt.list(USER)] == ["Любит кофе"]


async def test_reply_is_not_delayed_by_extraction_and_survives_its_failure(env):
    provider, lt, chat = env(ScriptedProvider(fail_extraction=True))
    await lt.set_enabled(USER, True)
    result = await chat.ask(USER, "Я очень люблю чай")
    assert result.text == "Хорошо!"
    await chat.drain()  # сбой в фоне не превращается в ошибку диалога
    assert len(provider.extraction_calls) == 1
    assert await lt.list(USER) == []


async def test_drain_waits_for_slow_extraction(env):
    class Slow(ScriptedProvider):
        async def complete(self, messages, **kwargs):
            if messages[0].content.startswith("Ты ведёшь заметки"):
                await asyncio.sleep(0.2)
            return await super().complete(messages, **kwargs)

    provider, lt, chat = env(Slow('["Любит чай"]'))
    await lt.set_enabled(USER, True)
    await chat.ask(USER, "Я очень люблю чай")
    assert await lt.list(USER) == []  # ответ уже отдан, факт ещё сохраняется
    await chat.drain()
    assert [f.text for f in await lt.list(USER)] == ["Любит чай"]

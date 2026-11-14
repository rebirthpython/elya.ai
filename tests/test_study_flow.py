"""Сценарии /notes и /slides через настоящий Dispatcher с фейковым Telegram и моделью."""

import io
import json

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageReplyMarkup, SendDocument, SendMessage
from docx import Document
from pptx import Presentation as PptxPresentation

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult
from bot.keyboards.study import StudyCallback
from database.session import create_engine, create_session_factory, init_db
from main import create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, RecordingSession, _callback_update, _message_update, _settings

# Две части по ~1200 символов: это материал (не тема), но короче порога проверки объёма.
LECTURE_PART = "Лекция про фотосинтез. Ну вот, значит, хлорофилл поглощает свет. Запомните уравнение! " * 13


class StudySession(RecordingSession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendDocument):
            self.requests.append(method)
            return await super().make_request(bot, SendMessage(chat_id=method.chat_id, text="file"))
        if isinstance(method, EditMessageReplyMarkup):
            self.requests.append(method)
            return True
        result = await super().make_request(bot, method, timeout)
        # Как и настоящий клиент aiogram, привязываем ответы к боту, чтобы у них работал .answer().
        return result.as_(bot) if hasattr(result, "as_") else result

    def documents(self) -> list[SendDocument]:
        return [m for m in self.requests if isinstance(m, SendDocument)]


class TaskProvider(BaseProvider):
    """Отвечает в зависимости от шага конвейера (по системному промпту)."""

    def __init__(self) -> None:
        super().__init__("fake", "Fake AI", ("m",))
        self.steps: list[str] = []
        self.requests: list[str] = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        system = messages[0].content
        self.requests.append(messages[-1].content)
        if "персональный ИИ-ассистент" in system:
            self.steps.append("chat")
            return CompletionResult(text="Ответ в чате", provider="fake", model=model)
        if "внимательный редактор" in system:
            step, reply = "extract", {"topic": "Фотосинтез", "terms": [{"term": "Хлорофилл"}]}
        elif "Как писать хороший конспект" in system:
            step, reply = "notes", {
                "title": "Фотосинтез", "summary": "Свет даёт энергию.",
                "sections": [{"heading": "Свет", "points": ["**Хлорофилл** поглощает свет"]},
                             {"heading": "Итог", "points": ["Образуется глюкоза"]}],
                "self_check": ["Зачем хлорофилл?"],
            }
        elif "Составь план презентации" in system:
            feedback = "уберите" in messages[-1].content
            count = 3 if feedback else 4
            step = "outline+feedback" if feedback else "outline"
            reply = {"title": "Фотосинтез", "subtitle": "", "slides": [
                {"type": "bullets", "title": f"Слайд {i}"} for i in range(count - 2)
            ] + [{"type": "stats", "title": "Цифры"}, {"type": "summary", "title": "Выводы"}]}
        else:
            step = "slides"
            outline = json.loads(messages[1].content.split("(JSON):\n", 1)[1])
            notes = "Пояснение для докладчика: что рассказать на этом слайде."
            reply = {"title": "Фотосинтез", "subtitle": "Как растения едят свет", "slides": [
                {"type": s["type"], "title": s["title"], "bullets": ["Первый тезис", "Второй тезис"],
                 "items": [{"value": "70%", "label": "кислорода"}], "notes": notes}
                for s in outline["slides"]
            ]}
        self.steps.append(step)
        return CompletionResult(text=json.dumps(reply, ensure_ascii=False), provider="fake", model=model)


@pytest.fixture
async def study_env(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.handlers.study.STATUS_DELAY", 0)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await init_db(engine)
    session_factory = create_session_factory(engine)
    usage = UsageTracker(session_factory, 100)
    memory = MemoryManager(MessageRepository(session_factory, store_limit=500), history_limit=30)
    provider = TaskProvider()
    session = StudySession()

    def make(ai_provider: BaseProvider):
        client = AIClient([ai_provider], max_tokens=100, temperature=0.5, usage=usage)
        return create_dispatcher(_settings(), client, memory, usage)

    yield {
        "bot": Bot(token="42:TEST", session=session), "session": session, "provider": provider,
        "dp": make(provider), "make": make,
    }
    await engine.dispose()


async def _feed(env, update):
    await env["dp"].feed_update(env["bot"], update)


async def test_notes_from_multipart_lecture(study_env):
    env = study_env
    await _feed(env, _message_update("/notes"))
    await _feed(env, _message_update(LECTURE_PART))
    await _feed(env, _message_update(LECTURE_PART))
    statuses = [m for m in env["session"].requests if isinstance(m, SendMessage) and m.reply_markup]
    assert len(statuses) == 2
    assert "Материал: " in statuses[-1].text and "Что сделать?" in statuses[-1].text
    # кнопки у предыдущего статуса убираются, чтобы не путаться
    assert any(isinstance(m, EditMessageReplyMarkup) for m in env["session"].requests)

    await _feed(env, _callback_update(StudyCallback(action="notes", value="full").pack()))
    assert env["provider"].steps == ["extract", "notes"]
    assert any("Делаю подробный конспект" in t for t in env["session"].sent_texts())

    document = env["session"].documents()[-1].document
    assert document.filename == "Фотосинтез.docx"
    text = "\n".join(p.text for p in Document(io.BytesIO(document.data)).paragraphs)
    assert "Хлорофилл поглощает свет" in text
    assert any("Свет даёт энергию." in t for t in env["session"].sent_texts())

    # после готового результата бот снова в обычном чате, а старые кнопки неактуальны
    await _feed(env, _callback_update(StudyCallback(action="notes", value="short").pack()))
    assert env["provider"].steps == ["extract", "notes"]


async def test_long_paste_is_not_throttled_and_gets_one_status(study_env, monkeypatch):
    import asyncio

    monkeypatch.setattr("bot.handlers.study.STATUS_DELAY", 0.05)
    env = study_env
    await _feed(env, _message_update("/notes"))
    # 13 частей подряд, как Telegram режет вставленную лекцию; лимит частоты в _settings — 10/мин
    await asyncio.gather(*(_feed(env, _message_update(f"Часть {i}. " + "текст " * 600)) for i in range(13)))
    texts = env["session"].sent_texts()
    assert not any("Слишком много сообщений" in t for t in texts)
    statuses = [m for m in env["session"].requests if isinstance(m, SendMessage) and m.reply_markup]
    assert len(statuses) == 1 and "Материал: " in statuses[0].text
    await _feed(env, _callback_update(StudyCallback(action="notes", value="short").pack()))
    extract_request = env["provider"].requests[0]
    assert all(f"Часть {i}." in extract_request for i in range(13))


async def test_slides_with_plan_feedback_and_theme(study_env):
    env = study_env
    await _feed(env, _message_update("/slides Фотосинтез, 4 слайда"))
    status = [m for m in env["session"].requests if isinstance(m, SendMessage)][-1]
    assert "Тема: «Фотосинтез, 4 слайда»" in status.text and status.reply_markup is not None

    await _feed(env, _callback_update(StudyCallback(action="outline").pack()))
    assert "1. Слайд 0" in "\n".join(env["session"].sent_texts())

    await _feed(env, _message_update("уберите один слайд"))
    assert env["provider"].steps == ["outline", "outline+feedback"]

    await _feed(env, _callback_update(StudyCallback(action="theme", value="dark").pack()))
    document = env["session"].documents()[-1]
    pptx = PptxPresentation(io.BytesIO(document.document.data))
    assert len(pptx.slides) == 3 + 2
    assert "оформление «Тёмная»" in document.caption


async def test_material_is_not_sent_to_chat_and_commands_still_work(study_env):
    env = study_env
    await _feed(env, _message_update("/notes"))
    await _feed(env, _message_update("/help"))
    assert "Что умеет" in env["session"].sent_texts()[-1]
    await _feed(env, _message_update("/cancel"))
    assert env["session"].sent_texts()[-1] == "Действие отменено."
    await _feed(env, _message_update("обычный вопрос"))
    assert env["provider"].steps == ["chat"]  # обычное сообщение ушло в чат, а не в конспект


async def test_ai_failure_keeps_material_for_retry(study_env):
    from ai.providers.base import ProviderUnavailableError

    class DownProvider(BaseProvider):
        def __init__(self):
            super().__init__("down", "Down", ("m",))

        async def complete(self, messages, *, model, max_tokens, temperature):
            raise ProviderUnavailableError("timeout", provider="down", model=model)

    dp = study_env["make"](DownProvider())
    bot = study_env["bot"]
    await dp.feed_update(bot, _message_update("/notes"))
    await dp.feed_update(bot, _message_update(LECTURE_PART))
    await dp.feed_update(bot, _callback_update(StudyCallback(action="notes", value="short").pack()))
    last = [m for m in study_env["session"].requests if isinstance(m, SendMessage)][-1]
    assert "не отвечает" in last.text and last.reply_markup is not None  # можно нажать ещё раз
    state = await dp.fsm.get_context(bot, chat_id=OWNER_ID, user_id=OWNER_ID).get_state()
    assert state == "StudyStates:collecting"

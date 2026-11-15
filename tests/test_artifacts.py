"""Готовые файлы на выходе: распознавание просьб, DOCX из Markdown, ZIP проекта, HTML,
хранилище файлов, бот (документы, кнопка предпросмотра, презентация из чата) и приложение
(файлы в ответе, скачивание, предпросмотр, презентация)."""

import io
import json
import zipfile
from datetime import timedelta

import pytest
from aiogram import Bot
from aiogram.methods import SendDocument
from aiohttp.test_utils import TestClient, TestServer
from docx import Document
from sqlalchemy import update

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult
from database.models import UserFile
from database.session import create_engine, create_session_factory, init_db
from main import create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.artifacts import (
    DOCUMENT_RULE,
    FileStore,
    OutputFile,
    build_outputs,
    markdown_to_docx,
    safe_path,
    wants_document,
    wants_presentation,
    wants_project,
)
from services.chat import ChatService
from services.instructions import InstructionsStore
from services.longterm import LongTermMemory
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, _message_update
from tests.test_bot_flow import _settings as bot_settings
from tests.test_study_flow import StudySession, TaskProvider
from tests.test_webapp import _settings as web_settings
from tests.test_webapp import auth
from web.server import create_web_app

ESSAY = (
    "# Пётр I и его реформы\n\n## Введение\n\nПётр I — **первый** император России. "
    + "Он менял страну. " * 60
    + "\n\n## Реформы\n\n- армия\n- флот\n\n1. первая\n2. вторая\n\n"
    "| Год | Событие |\n|---|---|\n| 1703 | Основан Петербург |\n\n"
    "> Цитата\n\n```python\nprint(1)\n```\n\n## Заключение\n\nИтог.\n"
)
PROJECT = (
    "Вот проект.\n\nФайл `src/main.py`:\n```python\nprint('hi')\n```\n\n"
    "### README.md\n```markdown\n# Бот\n```\n\n"
    "```js\n// static/app.js\nconsole.log(1)\n```\n\n"
    "Файл `../../etc/evil.sh`:\n```bash\nrm -rf /\n```\n"
)
PAGE = "Готово:\n\nФайл `index.html`:\n```html\n<!doctype html>\n<html><body><h1>Привет</h1><script>alert(1)</script></body></html>\n```\n"


# ---------- распознавание ----------


@pytest.mark.parametrize(
    "text, doc, slides, project",
    [
        ("Напиши реферат про Петра I", True, False, False),
        ("доклад о фотосинтезе в ворде", True, False, False),
        ("сделай мне отчёт по практике", True, False, False),
        ("как написать реферат?", False, False, False),
        ("что такое эссе", False, False, False),
        ("сделай презентацию про космос на 8 слайдов", False, True, False),
        ("как сделать презентацию интересной", False, False, False),
        ("напиши сайт-визитку для кофейни", False, False, True),
        ("скинь всё zip-архивом", False, False, True),
        ("привет, как дела?", False, False, False),
    ],
)
def test_requests_are_recognized(text, doc, slides, project):
    assert (wants_document(text), wants_presentation(text), wants_project(text)) == (doc, slides, project)


# ---------- файлы ----------


def test_markdown_becomes_a_formatted_word_document():
    document = Document(io.BytesIO(markdown_to_docx(ESSAY)))
    texts = [p.text for p in document.paragraphs]
    assert texts[0] == "ПЁТР I И ЕГО РЕФОРМЫ"
    headings = [p.text for p in document.paragraphs if p.style.name.startswith("Heading")]
    assert headings == ["Введение", "Реформы", "Заключение"]
    assert any(p.style.name == "List Bullet" and p.text == "армия" for p in document.paragraphs)
    assert any(p.style.name == "List Number" and p.text == "вторая" for p in document.paragraphs)
    bold = [r.text for p in document.paragraphs for r in p.runs if r.bold]
    assert "первый" in bold and "**" not in "".join(texts)
    assert document.tables[0].cell(1, 1).text == "Основан Петербург"
    assert document.styles["Normal"].font.name == "Times New Roman"
    assert "PAGE" in document.sections[0].footer._element.xml  # номера страниц


def test_essay_request_gets_a_docx_named_by_its_title():
    [output] = build_outputs("Напиши реферат про Петра I", ESSAY)
    assert output.kind == "docx" and output.name == "Пётр I и его реформы.docx"
    assert build_outputs("Напиши реферат", "Короткий ответ.") == []  # не документ
    assert build_outputs("Расскажи про Петра I", ESSAY) == []  # не просили файл
    assert build_outputs("Напиши реферат про Петра I", ESSAY, code_mode=True) == []


def test_named_code_files_become_a_zip_with_folders():
    outputs = build_outputs("Сделай проект бота на aiogram", PROJECT)
    [archive] = [o for o in outputs if o.kind == "zip"]
    assert archive.name == "бота-aiogram.zip"
    names = zipfile.ZipFile(io.BytesIO(archive.data)).namelist()
    assert names == ["src/main.py", "README.md", "static/app.js", "etc/evil.sh"]
    assert safe_path("/abs/../x.py") == "abs/x.py" and safe_path("..") is None


def test_web_page_is_offered_for_preview():
    [page] = build_outputs("Сделай страницу", PAGE)
    assert (page.kind, page.name, page.preview) == ("html", "index.html", True)
    assert b"<h1>" in page.data
    assert build_outputs("x", "```html\n<div>кусок</div>\n```") == []  # не страница целиком


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'files.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_file_store_keeps_recent_files(db):
    store = FileStore(db, keep=2)
    first = await store.save(1, OutputFile("a.docx", b"1", "docx"))
    second = await store.save(1, OutputFile("b.zip", b"22", "zip"))
    third = await store.save(1, OutputFile("c.html", b"333", "html", preview=True))
    assert await store.get(first.token) is None and (await store.get(second.token)).data == b"22"
    assert third.as_dict() == {"token": third.token, "name": "c.html", "kind": "html", "size": 3, "preview": True}
    assert len(third.token) >= 30 and await store.get("nope") is None

    async with db.begin() as session:  # старые файлы удаляются при следующем сохранении
        await session.execute(update(UserFile).values(created_at=UserFile.created_at - timedelta(days=40)))
    await store.save(2, OutputFile("d.zip", b"4", "zip"))
    assert await store.get(third.token) is None


# ---------- сервис диалога ----------


class ScriptedProvider(BaseProvider):
    def __init__(self, reply):
        super().__init__("s", "Scripted", ("s-1",))
        self.reply = reply
        self.calls = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        return CompletionResult(text=self.reply, provider="s", model=model)


async def test_service_attaches_files_and_keeps_them_in_history(db):
    provider = ScriptedProvider(ESSAY)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    settings = bot_settings()
    service = ChatService(settings, AIClient([provider], max_tokens=256, temperature=0.5), memory, files=FileStore(db))
    result = await service.ask(OWNER_ID, "Напиши реферат про Петра I")
    assert DOCUMENT_RULE.strip() in provider.calls[-1]["messages"][0].content
    assert provider.calls[-1]["max_tokens"] == settings.study_max_output_tokens
    [(output, stored)] = result.files
    assert output.kind == "docx" and stored.name == output.name
    timeline = await memory.get_timeline(OWNER_ID, 10)
    assert json.loads(timeline[-1].attachments)[0]["token"] == stored.token

    await service.ask(OWNER_ID, "спасибо")
    assert DOCUMENT_RULE.strip() not in provider.calls[-1]["messages"][0].content


# ---------- бот ----------


@pytest.fixture
async def bot_env(db, monkeypatch):
    monkeypatch.setattr("bot.handlers.study.STATUS_DELAY", 0)
    provider = ScriptedProvider(PAGE)
    usage = UsageTracker(db, 100)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    settings = bot_settings(webapp_url="https://elya.example/app")
    client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    service = ChatService(settings, client, memory, files=FileStore(db))
    session = StudySession()
    bot = Bot(token="42:TEST", session=session)
    dp = create_dispatcher(settings, client, memory, usage, chat_service=service)
    return {"bot": bot, "dp": dp, "session": session, "provider": provider}


async def _feed(env, update):
    await env["dp"].feed_update(env["bot"], update)


async def test_bot_sends_files_and_a_preview_button(bot_env):
    env = bot_env
    await _feed(env, _message_update("Сделай страницу"))
    [document] = env["session"].documents()
    assert document.document.filename == "index.html"
    button = document.reply_markup.inline_keyboard[0][0]
    assert button.web_app.url.startswith("https://elya.example/app?preview=")

    env["provider"].reply = ESSAY
    await _feed(env, _message_update("Напиши реферат про Петра I"))
    document = env["session"].documents()[-1]
    assert isinstance(document, SendDocument) and document.document.filename.endswith(".docx")
    assert "Документ Word" in document.caption


async def test_presentation_request_starts_the_slides_flow(bot_env):
    env = bot_env
    await _feed(env, _message_update("Сделай презентацию про фотосинтез"))
    assert any("Презентация" in text for text in env["session"].sent_texts())
    assert env["provider"].calls == [] or "Составь план" in env["provider"].calls[-1]["messages"][0].content


# ---------- приложение ----------


async def _web(db, provider):
    settings = web_settings()
    usage = UsageTracker(db, 100)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    preferences, long_memory, reminders = PreferencesStore(db), LongTermMemory(db), Reminders(db)
    instructions = InstructionsStore(db)
    app = create_web_app(
        settings,
        ChatService(settings, client, memory, preferences, long_memory, reminders,
                    instructions=instructions, files=FileStore(db)),
        memory, usage, client, SlidingWindowLimiter(30), preferences, long_memory, reminders, None, instructions,
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    return test_client


async def test_app_reply_has_files_download_and_preview(db):
    client = await _web(db, ScriptedProvider(PAGE))
    response = await client.post("/api/chat/stream", json={"text": "Сделай страницу"}, headers=auth())
    done = [json.loads(line) for line in (await response.text()).splitlines() if '"done"' in line][0]
    [file] = done["reply"]["files"]
    assert (file["name"], file["kind"], file["preview"]) == ("index.html", "html", True)

    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["history"][-1]["files"] == [file]  # карточка файла переживает перезагрузку

    download = await client.get(f"/files/{file['token']}/index.html")
    assert download.status == 200 and "attachment" in download.headers["Content-Disposition"]
    assert "sandbox" in download.headers["Content-Security-Policy"]

    preview = await client.get(f"/preview/{file['token']}")
    assert preview.status == 200 and "<h1>Привет</h1>" in await preview.text()
    assert preview.headers["Content-Security-Policy"].startswith("sandbox allow-scripts")

    assert (await client.get("/files/nope/x.html")).status == 404
    info = await client.post("/api/files/info", json={"token": file["token"]}, headers=auth())
    assert (await info.json())["file"]["preview"] is True
    assert (await client.post("/api/files/info", json={"token": "nope"}, headers=auth())).status == 404
    await client.close()


async def test_app_presentation_from_chat(db):
    provider = TaskProvider()
    client = await _web(db, provider)
    response = await client.post("/api/chat/stream", json={"text": "Сделай презентацию про фотосинтез"}, headers=auth())
    events = [json.loads(line) for line in (await response.text()).splitlines()]
    assert [e["type"] for e in events] == ["status", "status", "done"]
    [file] = events[-1]["reply"]["files"]
    assert file["kind"] == "pptx" and file["name"].endswith(".pptx")
    assert provider.steps == ["outline", "slides"]
    download = await client.get(f"/files/{file['token']}/{file['name']}")
    assert (await download.read())[:2] == b"PK"
    await client.close()

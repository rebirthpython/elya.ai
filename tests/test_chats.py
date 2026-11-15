"""Отдельные чаты и проекты: хранилище, перенос старой истории, контекст проекта,
команды бота (/new, /chats, /projects, /project) и API приложения."""

import io

import pytest
from aiogram import Bot
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from ai.client import AIClient
from bot.handlers.chats import ChatsCallback, ProjectCallback
from database.models import Message
from database.session import create_engine, create_session_factory, init_db
from main import BOT_COMMANDS, create_dispatcher
from memory.chats import DEFAULT_PROJECTS, ChatStore, ProjectError, make_title
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.documents import DocumentStore, parse_document
from services.instructions import InstructionsStore
from services.longterm import LongTermMemory
from services.modes import CHAT, ModeStore
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, _callback_update, _message_update
from tests.test_bot_flow import _settings as bot_settings
from tests.test_code_mode import CodeProvider, DocumentSession
from tests.test_documents import BIOLOGY, make_pdf
from tests.test_webapp import _settings as web_settings
from tests.test_webapp import auth
from web.server import create_web_app


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'chats.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


# ---------- хранилище ----------


def test_chat_title_from_the_first_message():
    assert make_title("Как решить уравнение?\nподробно") == "Как решить уравнение?"
    assert make_title("📄 «bio.pdf»\nЧто такое клетка?") == "Что такое клетка?"
    assert make_title("📄 «bio.pdf»") == "📄 «bio.pdf»"
    assert make_title("📷 Фото: что это") == "что это"
    long = make_title("слово " * 30)
    assert len(long) <= 61 and long.endswith("…")


async def test_default_projects_appear_once(db):
    chats = ChatStore(db)
    projects = await chats.projects(1)
    assert [(p.name, p.emoji, p.mode) for p in projects] == [(n, e, m) for n, e, _, m in DEFAULT_PROJECTS]
    for project in projects:
        await chats.delete_project(1, project.id)
    assert await chats.projects(1) == []  # удалённые не возвращаются
    assert len(await ChatStore(db).projects(2)) == 3  # у другого пользователя свои


async def test_projects_are_created_checked_and_deleted(db):
    chats = ChatStore(db)
    project = await chats.create_project(1, "  Английский  ", emoji="🌍", instructions="По-английски")
    assert (project.name, project.emoji, project.label) == ("Английский", "🌍", "🌍 Английский")
    assert (await chats.find_project(1, "английский")).id == project.id
    updated = await chats.update_project(1, project.id, instructions="Коротко", mode="code")
    assert (updated.instructions, updated.mode) == ("Коротко", "code")
    with pytest.raises(ProjectError, match="empty_name"):
        await chats.create_project(1, "   ")
    with pytest.raises(ProjectError, match="bad_mode"):
        await chats.update_project(1, project.id, mode="hacker")
    with pytest.raises(ProjectError, match="not_found"):
        await chats.update_project(2, project.id, name="чужой")

    chat, _ = await chats.new_chat(1, project_id=project.id)
    assert await chats.delete_project(1, project.id)
    assert (await chats.get(1, chat.id)).project_id is None  # чат остался, но без проекта
    assert not await chats.delete_project(1, project.id)


async def test_new_chat_list_open_rename_delete(db):
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    chats = memory.chats
    first = await chats.active(1)
    await memory.save_exchange(1, "Первый вопрос", "ответ")
    assert (await chats.get(1, first.id)).title == "Первый вопрос"

    study = (await chats.projects(1))[0]
    second, created = await chats.new_chat(1, project_id=study.id)
    assert created and second.project_id == study.id
    await memory.save_exchange(1, "Про клетки", "ответ")
    assert [c.title for c in await chats.list(1)] == ["Про клетки", "Первый вопрос"]
    assert [c.title for c in await chats.list(1, project_id=study.id)] == ["Про клетки"]
    assert [c.title for c in await chats.list(1, project_id=None)] == ["Первый вопрос"]

    assert (await chats.open(1, first.id)).id == first.id
    assert [m.content for m in await memory.get_history(1)] == ["Первый вопрос", "ответ"]
    assert await chats.open(2, first.id) is None  # чужой чат не открыть

    assert (await chats.rename(1, first.id, "  Мой  чат ")).title == "Мой чат"
    assert await chats.delete(1, first.id)
    assert (await chats.active(1)).id == second.id  # открывается оставшийся
    assert not await chats.delete(1, first.id)


async def test_old_history_is_split_into_chats(db):
    async with db.begin() as session:
        await session.execute(text("INSERT INTO users (id, created_at) VALUES (1, CURRENT_TIMESTAMP)"))
        for role, content in [
            ("user", "старый вопрос"), ("assistant", "старый ответ"), ("break", ""),
            ("user", "новый вопрос"), ("assistant", "новый ответ"),
        ]:
            session.add(Message(user_id=1, role=role, content=content))
    store = DocumentStore(db)
    async with db.begin() as session:
        await session.execute(
            text("INSERT INTO user_documents (user_id, name, kind, info, chars, content, created_at) "
                 "VALUES (1, 'a.txt', 'text', 'текст', 5, '[[\"строка 1\", \"hello\"]]', CURRENT_TIMESTAMP)")
        )

    memory = MemoryManager(MessageRepository(db), history_limit=30)
    chats = await memory.chats.list(1)
    assert [c.title for c in chats] == ["новый вопрос", "старый вопрос"]
    assert [m.content for m in await memory.get_history(1)] == ["новый вопрос", "новый ответ"]
    assert [m.content for m in await memory.get_history(1, chat_id=chats[1].id)] == ["старый вопрос", "старый ответ"]
    assert [d.name for d in await store.list(1)] == ["a.txt"]  # документы — в последнем чате
    async with db() as session:
        assert (await session.execute(text("SELECT count(*) FROM messages WHERE role = 'break'"))).scalar() == 0


async def test_old_database_gets_the_new_columns(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id BIGINT, role VARCHAR(16), "
            "content TEXT, created_at DATETIME)"
        ))
        await conn.execute(text("INSERT INTO messages (user_id, role, content) VALUES (1, 'user', 'привет')"))
    await init_db(engine)
    await init_db(engine)  # второй запуск ничего не ломает
    async with engine.connect() as conn:
        columns = [row[1] for row in (await conn.execute(text("PRAGMA table_info(messages)"))).all()]
        assert "chat_id" in columns
        assert (await conn.execute(text("SELECT content FROM messages"))).scalar() == "привет"
    await engine.dispose()


# ---------- контекст проекта ----------


def _service(db, provider):
    settings = bot_settings()
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([provider], max_tokens=256, temperature=0.5)
    modes = ModeStore(db)
    documents = DocumentStore(db, chats=memory.chats)
    service = ChatService(settings, client, memory, modes=modes, documents=documents)
    return service, memory, documents, modes


async def test_project_instructions_mode_and_files_reach_the_model(db):
    provider = CodeProvider()
    service, memory, documents, modes = _service(db, provider)
    chats = memory.chats
    study, code, _ = await chats.projects(OWNER_ID)
    await documents.add(OWNER_ID, parse_document("bio.pdf", make_pdf(BIOLOGY)), project_id=study.id)

    await chats.new_chat(OWNER_ID, project_id=study.id)
    await service.ask(OWNER_ID, "what do mitochondria produce")
    assert "Разговор идёт в проекте «Учёба»" in provider.system
    assert "объясняй понятно и по шагам" in provider.system
    assert "Mitochondria" in provider.system  # файл проекта виден в его чате

    await chats.new_chat(OWNER_ID, project_id=None)
    await service.ask(OWNER_ID, "what do mitochondria produce")
    assert "проекте" not in provider.system and "Mitochondria" not in provider.system

    await chats.new_chat(OWNER_ID, project_id=code.id)
    await service.ask(OWNER_ID, "напиши сортировку")
    assert "включён режим кода" in provider.system
    assert await modes.get(OWNER_ID) == CHAT  # общий режим не меняется


async def test_answer_is_saved_to_the_chat_where_it_was_asked(db):
    provider = CodeProvider()
    service, memory, _, _ = _service(db, provider)
    first = await memory.chats.active(OWNER_ID)
    events = service.ask_stream(OWNER_ID, "вопрос в первом чате")
    async for _ in events:
        # Пока Эля отвечает, открывают другой чат.
        await memory.chats.new_chat(OWNER_ID, project_id=None)
    assert [m.content for m in await memory.get_history(OWNER_ID, chat_id=first.id)][0] == "вопрос в первом чате"
    assert await memory.get_history(OWNER_ID) == []


# ---------- бот ----------


@pytest.fixture
async def bot_env(db):
    provider = CodeProvider(reply="Ответ")
    usage = UsageTracker(db, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    service = ChatService(bot_settings(), ai_client, memory, documents=DocumentStore(db, chats=memory.chats))
    session = DocumentSession()
    bot = Bot(token="42:TEST", session=session)
    bot.download = lambda *args, **kwargs: io.BytesIO(b"")
    dp = create_dispatcher(bot_settings(), ai_client, memory, usage, chat_service=service)
    return {"bot": bot, "dp": dp, "session": session, "provider": provider, "memory": memory}


async def _feed(env, update):
    await env["dp"].feed_update(env["bot"], update)


def test_chat_commands_are_in_the_menu():
    names = [command.command for command in BOT_COMMANDS]
    assert {"new", "chats", "projects"} <= set(names)


async def test_new_chat_in_the_bot_forgets_the_old_one(bot_env):
    env = bot_env
    await _feed(env, _message_update("Меня зовут Аня"))
    await _feed(env, _message_update("/new"))
    assert "Новый чат" in env["session"].sent_texts()[-1]
    await _feed(env, _message_update("Как меня зовут?"))
    sent = [m.content for m in env["provider"].calls[-1]["messages"][1:]]
    assert sent == ["Как меня зовут?"]


async def test_chats_list_and_open(bot_env):
    env = bot_env
    await _feed(env, _message_update("Первый чат"))
    first = await env["memory"].chats.active(OWNER_ID)
    await _feed(env, _message_update("/new"))
    await _feed(env, _message_update("Второй чат"))

    await _feed(env, _message_update("/chats"))
    request = env["session"].requests[-1]
    buttons = [b.text for row in request.reply_markup.inline_keyboard for b in row]
    assert buttons == ["✓ Второй чат", "Первый чат", "🆕 Новый чат"]

    await _feed(env, _callback_update(ChatsCallback(action="open", id=first.id).pack()))
    assert "Открыт чат «Первый чат»" in env["session"].sent_texts()[-1]
    assert "Ответ" in env["session"].sent_texts()[-1]  # последний ответ — для контекста
    assert (await env["memory"].chats.active(OWNER_ID)).id == first.id


async def test_projects_in_the_bot(bot_env):
    env = bot_env
    await _feed(env, _message_update("/projects"))
    request = env["session"].requests[-1]
    buttons = [b.text for row in request.reply_markup.inline_keyboard for b in row]
    assert buttons == ["📚 Учёба", "💻 Код", "🌿 Личное", "💬 Без проекта"]

    study = (await env["memory"].chats.projects(OWNER_ID))[0]
    await _feed(env, _callback_update(ProjectCallback(action="open", id=study.id).pack()))
    assert "Новый чат в проекте «📚 Учёба»" in env["session"].sent_texts()[-1]
    await _feed(env, _message_update("Помоги с биологией"))
    assert "проекте «Учёба»" in env["provider"].system

    await _feed(env, _message_update("/project Английский"))
    assert "Создала проект «📁 Английский»" in env["session"].sent_texts()[-2]
    active = await env["memory"].chats.active(OWNER_ID)
    english = await env["memory"].chats.find_project(OWNER_ID, "английский")
    assert active.project_id == english.id

    await _feed(env, _message_update("/project учёба"))  # существующий — продолжить его чат
    assert "Проект «📚 Учёба»: открыт чат «Помоги с биологией»" in env["session"].sent_texts()[-1]


# ---------- приложение ----------


@pytest.fixture
async def web_env(db):
    provider = CodeProvider(reply="Ответ")
    settings = web_settings()
    usage = UsageTracker(db, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    preferences = PreferencesStore(db)
    long_memory = LongTermMemory(db)
    reminders = Reminders(db)
    instructions = InstructionsStore(db)
    modes = ModeStore(db)
    documents = DocumentStore(db, chats=memory.chats)
    app = create_web_app(
        settings,
        ChatService(settings, ai_client, memory, preferences, long_memory, reminders,
                    instructions=instructions, modes=modes, documents=documents),
        memory, usage, ai_client, SlidingWindowLimiter(30), preferences, long_memory,
        reminders, None, instructions, modes,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    yield {"client": client, "provider": provider, "memory": memory}
    await client.close()


async def _post(client, path, body):
    response = await client.post(path, json=body, headers=auth())
    return response.status, await response.json()


async def test_app_state_has_chats_and_projects(web_env):
    client = web_env["client"]
    state = await (await client.get("/api/state", headers=auth())).json()
    assert [p["name"] for p in state["projects"]] == ["Учёба", "Код", "Личное"]
    assert state["chats"] == [state["chat"]] and state["chat"]["title"] == ""


async def test_app_chats_new_open_rename_move_delete(web_env):
    client = web_env["client"]
    await _post(client, "/api/chat", {"text": "первый"})
    first = (await (await client.get("/api/state", headers=auth())).json())["chat"]

    status, data = await _post(client, "/api/chat/new", {"project_id": None})
    assert status == 200 and data["started"] and data["history"] == []
    await _post(client, "/api/chat", {"text": "второй"})

    status, data = await _post(client, "/api/chat/open", {"id": first["id"]})
    assert [m["content"] for m in data["history"]] == ["первый", "Ответ"]
    assert (await _post(client, "/api/chat/open", {"id": 999}))[0] == 404

    status, data = await _post(client, "/api/chat/rename", {"id": first["id"], "title": "Мой"})
    assert data["chats"][0]["title"] in ("Мой", "второй")
    assert (await _post(client, "/api/chat/rename", {"id": first["id"], "title": " "}))[0] == 400

    study = data["projects"][0]
    status, data = await _post(client, "/api/chat/move", {"id": first["id"], "project_id": study["id"]})
    assert data["chat"]["project_id"] == study["id"]

    status, data = await _post(client, "/api/chat/delete", {"id": first["id"]})
    assert status == 200 and [c["title"] for c in data["chats"]] == ["второй"]
    assert [m["content"] for m in data["history"]] == ["второй", "Ответ"]


async def test_app_projects_save_files_and_delete(web_env):
    client = web_env["client"]
    status, data = await _post(
        client, "/api/projects/save",
        {"name": "Английский", "emoji": "🌍", "instructions": "Отвечай с примерами", "open": True},
    )
    assert status == 200 and data["project"]["name"] == "Английский"
    assert data["chat"]["project_id"] == data["project"]["id"] and data["history"] == []
    project_id = data["project"]["id"]

    form = FormData()
    form.add_field("project_id", str(project_id))
    form.add_field("file", make_pdf(BIOLOGY), filename="bio.pdf", content_type="application/pdf")
    response = await client.post("/api/document", data=form, headers=auth())
    uploaded = await response.json()
    assert response.status == 200 and uploaded["document"]["project_id"] == project_id
    assert [f["name"] for f in uploaded["files"]] == ["bio.pdf"]
    assert [d["name"] for d in uploaded["documents"]] == ["bio.pdf"]  # открытый чат — в проекте

    status, data = await _post(client, "/api/projects/files", {"id": project_id})
    assert [f["name"] for f in data["files"]] == ["bio.pdf"]

    await _post(client, "/api/chat", {"text": "what do mitochondria produce"})
    system = web_env["provider"].system
    assert "Отвечай с примерами" in system and "Mitochondria" in system

    status, data = await _post(client, "/api/projects/save", {"id": project_id, "name": ""})
    assert status == 400 and data["error"]["code"] == "empty_name"
    status, data = await _post(client, "/api/projects/save", {"id": project_id, "mode": "code"})
    assert data["mode"] == "code" and data["limits"]["max_text_length"] > 4000

    status, data = await _post(client, "/api/mode", {"mode": "chat"})
    assert data["mode"] == "code" and data["locked"] is True  # проект держит режим кода

    status, data = await _post(client, "/api/projects/delete", {"id": project_id})
    assert status == 200 and project_id not in [p["id"] for p in data["projects"]]
    assert data["chat"]["project_id"] is None and data["documents"] == []
    assert (await _post(client, "/api/projects/delete", {"id": project_id}))[0] == 404

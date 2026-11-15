"""GitHub и файлы с кодом: шифрование токена, подключение, репозитории, коммит из нескольких
файлов (в том числе в пустой репозиторий), чтение файла, бот (/github, /repo, /push, /pull,
кнопка под ответом) и приложение (/api/github, кнопка под ответом)."""

import base64
import json

import httpx
import pytest
from aiogram import Bot
from aiogram.methods import DeleteMessage, SendDocument
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult
from bot.handlers.github import GitHubCallback
from database.models import GitHubAccount
from database.session import create_engine, create_session_factory, init_db
from main import BOT_COMMANDS, create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.code import attachments, wants_files
from services.github import GitHubError, GitHubService, TokenCipher, commit_message, find_token, parse_repo
from services.instructions import InstructionsStore
from services.longterm import LongTermMemory
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, _callback_update, _message_update
from tests.test_bot_flow import _settings as bot_settings
from tests.test_study_flow import StudySession
from tests.test_webapp import _settings as web_settings
from tests.test_webapp import auth
from web.server import create_web_app

TOKEN = "ghp_" + "a" * 36
BOT_ANSWER = (
    "Готово!\n\nФайл `bot.py`:\n```python\nimport os\nprint('bot')\n```\n\n"
    "Файл `requirements.txt`:\n```text\naiogram==3.31.0\n```\n"
)


class FakeGitHub:
    """Маленький GitHub в памяти: репозитории, ветки, файлы, коммиты."""

    def __init__(self, *, empty: bool = False) -> None:
        self.files: dict[str, str] = {} if empty else {"README.md": "# Hi"}
        self.head: str | None = None if empty else "c0"
        self.commits = 0
        self.requests: list[tuple[str, str]] = []
        self.created: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path))
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        body = json.loads(request.content) if request.content else {}
        if path == "/user":
            return httpx.Response(200, json={"login": "anna"})
        if path == "/user/repos" and method == "GET":
            return httpx.Response(200, json=[{"full_name": "anna/my-bot"}, {"full_name": "anna/notes"}])
        if path == "/user/repos" and method == "POST":
            self.created.append(body)
            return httpx.Response(201, json={"full_name": f"anna/{body['name']}", "default_branch": "main"})
        if path == "/repos/anna/my-bot":
            return httpx.Response(200, json={"full_name": "anna/my-bot", "default_branch": "main", "permissions": {"push": True}})
        if path == "/repos/anna/my-bot/git/ref/heads/main":
            if self.head is None:
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            return httpx.Response(200, json={"object": {"sha": self.head}})
        if path.startswith("/repos/anna/my-bot/contents/") and method == "PUT":
            name = path.split("/contents/", 1)[1]
            self.files[name] = base64.b64decode(body["content"]).decode()
            self.commits += 1
            self.head = f"c{self.commits}"
            return httpx.Response(201, json={})
        if path.startswith("/repos/anna/my-bot/contents/"):
            name = path.split("/contents/", 1)[1]
            if name == "src":
                return httpx.Response(200, json=[{"name": "a.py"}])
            if name not in self.files:
                return httpx.Response(404, json={"message": "Not Found"})
            content = base64.b64encode(self.files[name].encode()).decode()
            return httpx.Response(200, json={"type": "file", "path": name, "size": len(self.files[name]), "content": content})
        if path.startswith("/repos/anna/my-bot/git/commits/") and method == "GET":
            return httpx.Response(200, json={"tree": {"sha": "t-" + path.rsplit("/", 1)[1]}})
        if path == "/repos/anna/my-bot/git/trees" and method == "POST":
            for item in body["tree"]:
                self.files[item["path"]] = item["content"]
            return httpx.Response(201, json={"sha": "t-new"})
        if path.startswith("/repos/anna/my-bot/git/trees/"):
            if self.head is None:
                return httpx.Response(409, json={"message": "empty"})
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob"} for p in self.files]})
        if path == "/repos/anna/my-bot/git/commits" and method == "POST":
            self.commits += 1
            return httpx.Response(201, json={"sha": f"c{self.commits}"})
        if path == "/repos/anna/my-bot/git/refs/heads/main" and method == "PATCH":
            self.head = body["sha"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'gh.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


def _service(db, fake=None, env_token=""):
    fake = fake or FakeGitHub()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return GitHubService(db, http, secret="42:TEST", env_token=env_token), fake


# ---------- мелочи ----------


def test_token_is_encrypted_and_protected():
    cipher = TokenCipher("secret")
    sealed = cipher.encrypt(TOKEN)
    assert TOKEN not in sealed and cipher.decrypt(sealed) == TOKEN
    assert cipher.encrypt(TOKEN) != sealed  # случайный nonce
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    assert cipher.decrypt(tampered) is None
    assert TokenCipher("other").decrypt(sealed) is None


def test_parsing_helpers():
    assert find_token(f"вот токен {TOKEN} держи") == TOKEN
    assert find_token("github_pat_" + "b" * 30) and find_token("просто текст") is None
    assert parse_repo("anna/my-bot") == ("anna", "my-bot")
    assert parse_repo("https://github.com/anna/my-bot.git") == ("anna", "my-bot")
    assert parse_repo("не репозиторий") is None
    assert commit_message("Эля, добавь команду /help") == "добавь команду /help"


def test_code_files_are_sent_when_named_or_asked():
    assert [name for name, _ in attachments(BOT_ANSWER)] == ["bot.py", "requirements.txt"]
    short = "```python\nprint(1)\n```"
    assert attachments(short) == []
    assert wants_files("пришли файлом") and wants_files("скинь код файлами")
    assert [name for name, _ in attachments(short, all_blocks=True)] == ["code.py"]
    assert attachments("Используй python3.11:\n```bash\npip install x\n```") == []  # не имя файла


# ---------- сервис ----------


async def test_connect_choose_and_push_to_github(db):
    github, fake = _service(db)
    assert not (await github.status(1)).connected
    with pytest.raises(GitHubError) as error:
        await github.connect(1, "ghp_" + "x" * 36)
    assert error.value.kind == "auth"

    status = await github.connect(1, TOKEN)
    assert status.connected and status.login == "anna" and not status.repo
    async with db() as session:
        assert TOKEN not in (await session.get(GitHubAccount, 1)).token

    assert await github.repos(1) == ["anna/my-bot", "anna/notes"]
    status = await github.use_repo(1, "https://github.com/anna/my-bot")
    assert (status.repo, status.branch) == ("anna/my-bot", "main")
    assert await github.tree(1) == ["README.md"]

    result = await github.push(1, [("bot.py", "print(1)\n"), ("src/util.py", "x = 1\n")], "Add bot")
    assert result.files == 2 and result.commit_url == "https://github.com/anna/my-bot/commit/c1"
    assert fake.files["bot.py"] == "print(1)\n" and fake.head == "c1"
    assert await github.read_file(1, "bot.py") == ("bot.py", "print(1)\n")
    with pytest.raises(GitHubError, match="папка"):
        await github.read_file(1, "src")

    await github.disconnect(1)
    assert not (await github.status(1)).connected


async def test_push_to_an_empty_repository(db):
    github, fake = _service(db, FakeGitHub(empty=True))
    await github.connect(1, TOKEN)
    await github.use_repo(1, "anna/my-bot")
    assert await github.tree(1) == []
    result = await github.push(1, [("bot.py", "a\n"), ("README.md", "b\n")], "Init")
    assert result.files == 1 and fake.files == {"bot.py": "a\n", "README.md": "b\n"}
    assert ("PUT", "/repos/anna/my-bot/contents/bot.py") in fake.requests


async def test_env_token_and_create_repo(db):
    github, fake = _service(db, env_token=TOKEN)
    status = await github.status(1)
    assert status.connected and status.from_env
    with pytest.raises(GitHubError, match="латиница"):
        await github.create_repo(1, "мой бот")
    status = await github.create_repo(1, "my telegram bot")
    assert status.repo == "anna/my-telegram-bot" and fake.created[0]["private"] is True
    with pytest.raises(GitHubError, match="Репозиторий не выбран"):
        github2, _ = _service(db, env_token=TOKEN)
        await github2.push(2, [("a.py", "x")], "m")


async def test_prompt_block_lists_repository_files(db):
    github, _ = _service(db)
    assert await github.prompt_block(1) == ""
    await github.connect(1, TOKEN)
    await github.use_repo(1, "anna/my-bot")
    block = await github.prompt_block(1)
    assert "anna/my-bot" in block and "README.md" in block and "Файл `bot.py`:" in block


# ---------- бот ----------


class ScriptedProvider(BaseProvider):
    def __init__(self, reply):
        super().__init__("s", "Scripted", ("s-1",))
        self.reply = reply
        self.calls = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append(messages)
        return CompletionResult(text=self.reply, provider="s", model=model)


class GitHubSession(StudySession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, DeleteMessage):
            self.requests.append(method)
            return True
        return await super().make_request(bot, method, timeout)


@pytest.fixture
async def bot_env(db):
    github, fake = _service(db)
    provider = ScriptedProvider(BOT_ANSWER)
    usage = UsageTracker(db, 100)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    service = ChatService(bot_settings(), client, memory, github=github)
    session = GitHubSession()
    bot = Bot(token="42:TEST", session=session)
    dp = create_dispatcher(bot_settings(), client, memory, usage, chat_service=service)
    return {"bot": bot, "dp": dp, "session": session, "provider": provider, "fake": fake, "github": github}


async def _feed(env, update):
    await env["dp"].feed_update(env["bot"], update)


def test_github_commands_are_in_the_menu():
    names = [command.command for command in BOT_COMMANDS]
    assert "github" in names and "push" in names


async def test_bot_connects_pushes_and_pulls(bot_env):
    env = bot_env
    await _feed(env, _message_update("/github"))
    assert "Создай токен" in env["session"].sent_texts()[-1]

    await _feed(env, _message_update(f"/github {TOKEN}"))
    assert any(isinstance(r, DeleteMessage) for r in env["session"].requests)  # сообщение с токеном удалено
    assert "GitHub подключён как <b>anna</b>" in env["session"].sent_texts()[-1]

    await _feed(env, _message_update("/repo anna/my-bot"))
    assert "anna/my-bot" in env["session"].sent_texts()[-1]

    await _feed(env, _message_update("Напиши телеграм-бота"))
    system = env["provider"].calls[-1][0].content
    assert "Подключён репозиторий GitHub anna/my-bot" in system
    documents = [r.document.filename for r in env["session"].requests if isinstance(r, SendDocument)]
    assert documents[:2] == ["bot.py", "requirements.txt"]  # код — файлами (и архивом)
    offer = env["session"].requests[-1]
    assert "Отправить в GitHub" in offer.text
    button = offer.reply_markup.inline_keyboard[0][0]
    await _feed(env, _callback_update(button.callback_data))
    assert "Отправила в <b>anna/my-bot</b> (2 файла)" in env["session"].sent_texts()[-1]
    assert env["fake"].files["requirements.txt"] == "aiogram==3.31.0\n"

    await _feed(env, _message_update("/pull bot.py добавь /help"))
    question = env["provider"].calls[-1][-1].content
    assert "Файл `bot.py`" in question and "import os" in question and question.endswith("добавь /help")

    await _feed(env, _message_update("/push"))  # последние файлы — ещё раз
    assert "Отправила" in env["session"].sent_texts()[-1]


async def test_token_without_a_command_never_reaches_the_model(bot_env):
    env = bot_env
    await _feed(env, _message_update(f"мой токен {TOKEN}"))
    assert env["provider"].calls == []
    assert "GitHub подключён" in env["session"].sent_texts()[-1]


async def test_repo_buttons(bot_env):
    env = bot_env
    await _feed(env, _message_update(f"/github {TOKEN}"))
    await _feed(env, _message_update("/repo"))
    rows = env["session"].requests[-1].reply_markup.inline_keyboard
    assert [row[0].text for row in rows] == ["anna/my-bot", "anna/notes", "➕ Новый репозиторий"]
    await _feed(env, _callback_update(GitHubCallback(action="repo", id=0).pack()))
    assert (await env["github"].status(OWNER_ID)).repo == "anna/my-bot"


# ---------- приложение ----------


async def test_app_github_flow(db):
    github, fake = _service(db)
    settings = web_settings()
    usage = UsageTracker(db, 100)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([ScriptedProvider(BOT_ANSWER)], max_tokens=256, temperature=0.5, usage=usage)
    preferences, long_memory, reminders = PreferencesStore(db), LongTermMemory(db), Reminders(db)
    instructions = InstructionsStore(db)
    app = create_web_app(
        settings,
        ChatService(settings, client, memory, preferences, long_memory, reminders, instructions=instructions, github=github),
        memory, usage, client, SlidingWindowLimiter(30), preferences, long_memory, reminders, None, instructions,
    )
    test = TestClient(TestServer(app))
    await test.start_server()

    async def post(body):
        response = await test.post("/api/github", json=body, headers=auth())
        return response.status, await response.json()

    state = await (await test.get("/api/state", headers=auth())).json()
    assert state["github"]["connected"] is False
    status, data = await post({"action": "connect", "token": "ghp_" + "z" * 36})
    assert status == 400 and data["error"]["code"] == "github_auth"
    status, data = await post({"action": "connect", "token": TOKEN})
    assert data["github"]["login"] == "anna"
    status, data = await post({"action": "repos"})
    assert data["repos"] == ["anna/my-bot", "anna/notes"]
    status, data = await post({"action": "use", "repo": "anna/my-bot"})
    assert data["github"]["repo"] == "anna/my-bot"

    reply = (await (await test.post("/api/chat", json={"text": "сделай бота"}, headers=auth())).json())["reply"]
    assert reply["push"]["files"] == 2 and reply["push"]["repo"] == "anna/my-bot"
    status, data = await post({"action": "push", "id": reply["push"]["id"]})
    assert status == 200 and data["pushed"]["files"] == 2
    assert (await post({"action": "push", "id": 999}))[0] == 410
    assert (await post({"action": "nope"}))[0] == 400
    await test.close()

"""Режим кода: команда /code, промпт, файлы с кодом, длинный код файлом, Mini App."""

import io
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from aiogram.types import Chat, Message
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient
from ai.prompts import build_system_prompt
from ai.providers.base import BaseProvider, CompletionResult
from bot.keyboards.common import CodeModeCallback
from bot.utils.formatting import markdown_to_html
from bot.utils.text import close_open_fence, split_text
from database.session import create_engine, create_session_factory, init_db
from main import BOT_COMMANDS, create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.code import (
    DEFAULT_FILE_QUESTION,
    MAX_CODE_FILE_BYTES,
    CodeFileError,
    attachments,
    clean_filename,
    code_blocks,
    file_message,
    language_for,
    looks_like_code,
    read_code_file,
)
from services.instructions import InstructionsStore
from services.longterm import LongTermMemory
from services.modes import CHAT, CODE, ModeStore
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.usage import UsageTracker
from tests.test_bot_flow import (
    OWNER_ID,
    STRANGER_ID,
    RecordingSession,
    _callback_update,
    _message_update,
    _photo_update,
)
from tests.test_bot_flow import _settings as bot_settings
from tests.test_webapp import _settings as web_settings
from tests.test_webapp import auth, sign_init_data
from web.server import MAX_CODE_TEXT_LENGTH, MAX_TEXT_LENGTH, create_web_app

LONG_CODE = "\n".join(f"value_{i} = {i}" for i in range(60))


class CodeProvider(BaseProvider):
    """Запоминает запросы вместе с параметрами и отвечает заготовкой."""

    def __init__(self, reply: str = "Готово: `print(1)`", truncated: bool = False) -> None:
        super().__init__("code", "Code AI", ("code-1",), vision_models=("code-1",))
        self.reply = reply
        self.truncated = truncated
        self.calls: list[dict] = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append(
            {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        )
        return CompletionResult(
            text=self.reply, provider="code", model=model, truncated=self.truncated
        )

    @property
    def system(self) -> str:
        return self.calls[-1]["messages"][0].content

    @property
    def question(self) -> str:
        return self.calls[-1]["messages"][-1].content


class DocumentSession(RecordingSession):
    """Как RecordingSession, но принимает и отправку файлов."""

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendDocument):
            self.requests.append(method)
            return Message(
                message_id=len(self.requests),
                date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
            )
        return await super().make_request(bot, method, timeout)

    def documents(self) -> list[SendDocument]:
        return [m for m in self.requests if isinstance(m, SendDocument)]


# ---------- файлы с кодом ----------


def test_code_file_is_read_and_wrapped_for_the_model():
    file = read_code_file(b"def f():\r\n    return 1  \r\n", "src/main.py", max_chars=1000)
    assert (file.name, file.language, file.text) == ("main.py", "python", "def f():\n    return 1")
    assert not file.truncated
    assert file_message(file, "Что делает f?") == (
        "Файл `main.py`:\n```python\ndef f():\n    return 1\n```\n\nЧто делает f?"
    )
    assert file_message(file).endswith(DEFAULT_FILE_QUESTION)


def test_code_file_encodings_and_binary_detection():
    assert read_code_file("# привет".encode("utf-8-sig"), "a.py", max_chars=100).text == "# привет"
    assert read_code_file("# привет".encode("cp1251"), "a.py", max_chars=100).text == "# привет"
    assert read_code_file("x = 1".encode("utf-16"), "a.py", max_chars=100).text == "x = 1"
    for data, reason in [
        (b"PK\x03\x04\x00\x00binary", "binary"),
        (b"", "empty"),
        (b"  \n\n ", "empty"),
        (b"x" * (MAX_CODE_FILE_BYTES + 1), "too_large"),
    ]:
        with pytest.raises(CodeFileError) as error:
            read_code_file(data, "a.py", max_chars=100)
        assert str(error.value) == reason


def test_long_code_file_is_cut_at_a_line_and_says_so():
    source = "\n".join(f"line_{i} = {i}" for i in range(500))
    file = read_code_file(source.encode(), "big.py", max_chars=1000)
    assert file.truncated and len(file.text) <= 1000 and file.total_chars == len(source)
    assert source.startswith(file.text) and file.text.endswith(" = " + file.text.rsplit(" = ", 1)[1])
    assert f"первые {len(file.text)} из {len(source)} символов" in file_message(file, "что тут?")


def test_file_with_its_own_fences_gets_a_longer_fence():
    file = read_code_file(b"# Readme\n```python\nprint(1)\n```\n", "README.md", max_chars=1000)
    message = file_message(file, "ок?")
    assert message.startswith("Файл `README.md`:\n````markdown\n")
    assert "\n````\n\nок?" in message


@pytest.mark.parametrize(
    "name, language, is_code",
    [
        ("bot.py", "python", True),
        ("App.TSX", "tsx", True),
        ("Dockerfile", "dockerfile", True),
        ("config.yml", "yaml", True),
        ("лекция.txt", "", False),
        ("notes.md", "markdown", False),
        ("report.pdf", "", False),
        (None, "", False),
    ],
)
def test_language_is_guessed_from_the_file_name(name, language, is_code):
    assert language_for(name or "") == language
    assert looks_like_code(name) is is_code


def test_file_names_are_made_safe():
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("C:\\work\\my script`.py") == "my_script_.py"
    assert clean_filename("") == "file.txt" and clean_filename(None) == "file.txt"


# ---------- длинный код из ответа — файлом ----------


def test_long_code_block_becomes_a_file():
    text = f"Вот скрипт:\n```python\n{LONG_CODE}\n```\nЗапуск:\n```bash\npython main.py\n```"
    assert [(b.language, b.code.count("\n") + 1) for b in code_blocks(text)] == [
        ("python", 60),
        ("bash", 1),
    ]
    assert attachments(text) == [("code.py", (LONG_CODE + "\n").encode())]
    assert attachments("Короткий ответ с `кодом` и\n```python\nprint(1)\n```") == []


def test_attachment_takes_the_file_name_written_before_the_block():
    text = (
        f"Файл `app/handlers/code.py`:\n```python\n{LONG_CODE}\n```\n\n"
        f"**index.js**\n```javascript\n{LONG_CODE}\n```\n\n"
        f"И ещё один:\n```python\n{LONG_CODE}\n```\n"
        f"```python\n{LONG_CODE}\n```\n```go\n{LONG_CODE}\n```"
    )
    names = [name for name, _ in attachments(text, limit=5)]
    assert names == ["code.py", "index.js", "code_2.py", "code_3.py", "code.go"]
    assert len(attachments(text)) == 3  # не больше трёх файлов на ответ


def test_unfinished_block_is_not_attached():
    assert attachments(f"Начало:\n```python\n{LONG_CODE}") == []


# ---------- оформление кода в Telegram ----------


def test_code_block_keeps_its_language_for_highlighting():
    html = markdown_to_html("Смотри:\n```python\nif a < b:\n    print('**x**')\n```")
    assert html == (
        'Смотри:\n<pre><code class="language-python">if a &lt; b:\n    print(\'**x**\')</code></pre>'
    )
    assert markdown_to_html("```\nplain\n```") == "<pre>plain</pre>"
    assert 'class="language-c++"' in markdown_to_html("```C++\nint x;\n```")


def test_split_code_block_reopens_with_the_same_language():
    code = "\n".join(f"line_{i} = {i}" for i in range(300))
    chunks = split_text(f"Пример:\n```python\n{code}\n```\nГотово.", limit=1000)
    assert len(chunks) > 2
    assert all(chunk.startswith("```python\n") for chunk in chunks[1:])
    assert all(chunk.count("```") % 2 == 0 and len(chunk) <= 1000 for chunk in chunks)
    restored = "\n".join(
        line for chunk in chunks for line in chunk.split("\n") if not line.startswith("```")
    )
    assert code in restored


def test_cut_off_code_block_is_closed():
    assert close_open_fence("Код:\n```python\nx = 1") == "Код:\n```python\nx = 1\n```"
    assert close_open_fence("```python\nx = 1\n```") == "```python\nx = 1\n```"
    assert close_open_fence("без кода") == "без кода"


# ---------- промпт ----------


def test_code_prompt_describes_a_programmer_and_keeps_the_identity():
    prompt = build_system_prompt(
        "Эля", user_name="Аня", style="business", verbosity="short", mode="code",
        memory="\nПАМЯТЬ\n", instructions="\nИНСТРУКЦИИ\n",
    )
    assert "включён режим кода" in prompt and "тебя зовут Эля" in prompt
    assert "```python" in prompt and "не запускаешь код" in prompt and "/code off" in prompt
    assert "деловой" in prompt and "Отвечай коротко" in prompt
    assert "«Аня»" in prompt and "ПАМЯТЬ" in prompt and "ИНСТРУКЦИИ" in prompt
    assert "{" not in prompt and "}" not in prompt


def test_ordinary_prompt_is_not_the_code_prompt_and_mentions_the_command():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
    prompt = build_system_prompt("Эля", now)
    assert "включён режим кода" not in prompt and "/code" in prompt
    assert build_system_prompt("Эля", now, mode="unknown") == prompt  # незнакомый режим — обычный


# ---------- хранилище режима и сервис диалога ----------


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'code.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_mode_is_stored_per_user(db):
    modes = ModeStore(db)
    assert await modes.get(1) == CHAT
    assert await modes.set(1, CODE) == CODE
    assert await modes.get(1) == CODE and await modes.get(2) == CHAT
    await modes.set(1, CHAT)
    assert await modes.get(1) == CHAT
    with pytest.raises(ValueError):
        await modes.set(1, "hacker")
    assert await ModeStore(db).get(1) == CHAT


def _service(db, provider, **settings_overrides):
    settings = bot_settings(**settings_overrides)
    memory = MemoryManager(MessageRepository(db, store_limit=500), history_limit=30)
    modes = ModeStore(db)
    client = AIClient([provider], max_tokens=settings.max_output_tokens, temperature=settings.temperature)
    reminders = Reminders(db, client, clock=lambda: datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc))
    service = ChatService(settings, client, memory, reminders=reminders, modes=modes)
    return service, memory, modes, reminders


async def test_code_mode_changes_prompt_and_generation_settings(db):
    provider = CodeProvider()
    service, _, modes, _ = _service(db, provider)

    await service.ask(OWNER_ID, "привет")
    assert "включён режим кода" not in provider.system
    assert (provider.calls[-1]["max_tokens"], provider.calls[-1]["temperature"]) == (256, 0.5)

    await modes.set(OWNER_ID, CODE)
    await service.ask(OWNER_ID, "напиши сортировку")
    assert "включён режим кода" in provider.system
    assert (provider.calls[-1]["max_tokens"], provider.calls[-1]["temperature"]) == (4096, 0.2)

    await modes.set(OWNER_ID, CHAT)
    await service.ask(OWNER_ID, "спасибо")
    assert "включён режим кода" not in provider.system
    assert provider.calls[-1]["max_tokens"] == 256


async def test_code_mode_never_shortens_answers(db):
    provider = CodeProvider()
    service, _, modes, _ = _service(db, provider, max_output_tokens=8000, code_max_output_tokens=1024)
    await modes.set(OWNER_ID, CODE)
    await service.ask(OWNER_ID, "код")
    assert provider.calls[-1]["max_tokens"] == 8000


async def test_code_mode_keeps_long_code_in_the_context(db):
    provider = CodeProvider()
    service, _, modes, _ = _service(db, provider)
    pasted = "```python\n" + "x = 1\n" * 3000 + "```"  # ~18 000 символов: больше обычного окна
    await modes.set(OWNER_ID, CODE)
    await service.ask(OWNER_ID, pasted)
    await service.ask(OWNER_ID, "а что в третьей строке?")
    assert [m.content for m in provider.calls[-1]["messages"][1:]] == [
        pasted, provider.reply, "а что в третьей строке?",
    ]

    await modes.set(OWNER_ID, CHAT)
    await service.ask(OWNER_ID, "ещё вопрос")
    assert pasted not in [m.content for m in provider.calls[-1]["messages"]]


async def test_file_reaches_the_model_and_stays_in_history(db):
    provider = CodeProvider()
    service, memory, modes, _ = _service(db, provider)
    await modes.set(OWNER_ID, CODE)
    file = read_code_file(b"print('hi')\n", "hello.py", max_chars=1000)

    result = await service.ask(OWNER_ID, "почему не работает?", file=file)
    expected = "Файл `hello.py`:\n```python\nprint('hi')\n```\n\nпочему не работает?"
    assert result.text == provider.reply and provider.question == expected
    assert [m.content for m in await memory.get_history(OWNER_ID)] == [expected, provider.reply]


async def test_pasted_code_is_not_mistaken_for_a_reminder(db):
    provider = CodeProvider()
    service, _, modes, reminders = _service(db, provider)
    await reminders.set_timezone(OWNER_ID, "Europe/Moscow")
    await modes.set(OWNER_ID, CODE)

    pasted = "Что не так?\n```python\nTEXT = 'напомни завтра в 9 позвонить маме'\n```"
    await service.ask(OWNER_ID, pasted)
    assert len(provider.calls) == 1 and provider.question == pasted  # разбора напоминания не было

    file = read_code_file("INTENT = 'напомни'".encode(), "reminders.py", max_chars=1000)
    await service.ask(OWNER_ID, "напомни, что делает этот файл", file=file)
    assert len(provider.calls) == 2 and provider.question.startswith("Файл `reminders.py`")
    assert await reminders.list_pending(OWNER_ID) == []


# ---------- бот ----------


@pytest.fixture
async def bot_env(db):
    provider = CodeProvider()
    usage = UsageTracker(db, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    memory = MemoryManager(MessageRepository(db, store_limit=500), history_limit=30)
    modes = ModeStore(db)
    session = DocumentSession()
    bot = Bot(token="42:TEST", session=session)
    downloaded: list[str] = []
    files = {"content": b"def f():\n    return 1\n"}

    async def download(file, *args, **kwargs):
        downloaded.append(file.file_id)
        return io.BytesIO(files["content"])

    bot.download = download
    dp = create_dispatcher(bot_settings(), ai_client, memory, usage, modes=modes)
    return {
        "bot": bot, "dp": dp, "session": session, "provider": provider, "memory": memory,
        "modes": modes, "downloaded": downloaded, "files": files, "ai_client": ai_client,
        "usage": usage,
    }


def _document(name="main.py", size=100, caption=None, user_id=OWNER_ID):
    return _photo_update(
        caption,
        document={"file_name": name, "mime_type": "text/x-python", "file_size": size},
        user_id=user_id,
    )


async def _feed(env, update):
    await env["dp"].feed_update(env["bot"], update)


def test_code_command_is_in_the_menu():
    assert "code" in [command.command for command in BOT_COMMANDS]


async def test_code_command_turns_the_mode_on_and_off(bot_env):
    env = bot_env
    await _feed(env, _message_update("/code"))
    sent = env["session"].requests[-1]
    assert "Режим кода включён." in sent.text and "Скриншот ошибки" in sent.text
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == CodeModeCallback(action="off").pack()
    assert await env["modes"].get(OWNER_ID) == CODE
    assert env["provider"].calls == []  # сама команда к модели не обращается

    await _feed(env, _message_update("/code"))
    assert "Режим кода уже включён." in env["session"].sent_texts()[-1]

    await _feed(env, _message_update("напиши hello world"))
    assert "включён режим кода" in env["provider"].system

    await _feed(env, _message_update("/code off"))
    sent = env["session"].requests[-1]
    assert "Режим кода выключен" in sent.text
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == CodeModeCallback(action="on").pack()
    assert await env["modes"].get(OWNER_ID) == CHAT

    await _feed(env, _message_update("/code выкл"))
    assert "и так выключен" in env["session"].sent_texts()[-1]

    await _feed(env, _message_update("привет"))
    assert "включён режим кода" not in env["provider"].system


async def test_code_command_with_a_task_answers_right_away(bot_env):
    env = bot_env
    await _feed(env, _message_update("/code напиши функцию факториала"))
    texts = env["session"].sent_texts()
    assert "Режим кода включён" in texts[0] and texts[-1] == "Готово: <code>print(1)</code>"
    assert env["provider"].question == "напиши функцию факториала"
    assert "включён режим кода" in env["provider"].system
    assert await env["modes"].get(OWNER_ID) == CODE

    await _feed(env, _message_update("/code а теперь на Go"))
    assert len(env["session"].sent_texts()) == len(texts) + 1  # повторного «включён» нет
    assert env["provider"].question == "а теперь на Go"

    await _feed(env, _message_update("/code on"))
    assert "уже включён" in env["session"].sent_texts()[-1] and len(env["provider"].calls) == 2


async def test_code_mode_buttons(bot_env):
    env = bot_env
    await _feed(env, _callback_update(CodeModeCallback(action="on").pack()))
    edit = [m for m in env["session"].requests if isinstance(m, EditMessageText)][-1]
    assert "Режим кода включён." in edit.text and await env["modes"].get(OWNER_ID) == CODE

    await _feed(env, _callback_update(CodeModeCallback(action="off").pack()))
    edit = [m for m in env["session"].requests if isinstance(m, EditMessageText)][-1]
    assert "Режим кода выключен" in edit.text and await env["modes"].get(OWNER_ID) == CHAT


async def test_code_file_is_read_in_code_mode(bot_env):
    env = bot_env
    await env["modes"].set(OWNER_ID, CODE)
    await _feed(env, _document(caption="Найди ошибку"))
    assert env["downloaded"] == ["doc"]
    expected = "Файл `main.py`:\n```python\ndef f():\n    return 1\n```\n\nНайди ошибку"
    assert env["provider"].question == expected
    assert env["session"].sent_texts() == ["Готово: <code>print(1)</code>"]
    assert (await env["memory"].get_history(OWNER_ID))[0].content == expected

    await _feed(env, _document(name="notes.txt"))  # без подписи — вопрос по умолчанию
    assert env["provider"].question.endswith(DEFAULT_FILE_QUESTION)


async def test_code_file_outside_code_mode_suggests_the_command(bot_env):
    env = bot_env
    await _feed(env, _document())
    assert "/code" in env["session"].sent_texts()[-1] and "файл с кодом" in env["session"].sent_texts()[-1]
    await _feed(env, _document(name="лекция.txt"))
    assert "Стикеры, видео и другие файлы" in env["session"].sent_texts()[-1]
    assert env["downloaded"] == [] and env["provider"].calls == []


async def test_code_file_problems_are_explained(bot_env):
    env = bot_env
    await env["modes"].set(OWNER_ID, CODE)

    await _feed(env, _document(size=MAX_CODE_FILE_BYTES + 1))
    assert "до 1 МБ" in env["session"].sent_texts()[-1] and env["downloaded"] == []

    env["files"]["content"] = b"%PDF-1.7\x00\x01\x02"
    await _feed(env, _document(name="report.pdf"))
    assert "не похоже на текстовый файл" in env["session"].sent_texts()[-1]

    env["files"]["content"] = b"\n\n"
    await _feed(env, _document())
    assert "Файл пустой" in env["session"].sent_texts()[-1]
    assert env["provider"].calls == [] and await env["memory"].get_history(OWNER_ID) == []


async def test_long_code_file_is_cut_with_a_notice(db, bot_env):
    env = bot_env
    await env["modes"].set(OWNER_ID, CODE)
    env["files"]["content"] = ("x = 1\n" * 2000).encode()
    dp = create_dispatcher(
        bot_settings(code_max_file_chars=1000), env["ai_client"], env["memory"], env["usage"],
        modes=env["modes"],
    )
    await dp.feed_update(env["bot"], _document())
    assert "прочитала первые" in env["session"].sent_texts()[0]
    assert len(env["provider"].question) < 1300 and "Файл длинный" in env["provider"].question


async def test_long_code_from_the_answer_is_also_sent_as_a_file(bot_env):
    env = bot_env
    env["provider"].reply = f"`bot.py`:\n```python\n{LONG_CODE}\n```\nЗапусти и проверь."
    await env["modes"].set(OWNER_ID, CODE)
    await _feed(env, _message_update("напиши бота"))
    [document] = env["session"].documents()
    assert document.document.filename == "bot.py"
    assert document.document.data == (LONG_CODE + "\n").encode()
    assert "файлом" in document.caption
    assert 'class="language-python"' in env["session"].sent_texts()[-1]

    env["provider"].reply = "```python\nprint(1)\n```"
    await _feed(env, _message_update("короче"))
    assert len(env["session"].documents()) == 1  # короткий код файлом не дублируется


async def test_cut_off_answer_closes_the_code_block_before_the_notice(bot_env):
    env = bot_env
    env["provider"].reply = "```python\nimport os\nx = os.getcwd()"
    env["provider"].truncated = True
    await _feed(env, _message_update("/code длинная программа"))
    final = env["session"].sent_texts()[-1]
    assert final.startswith('<pre><code class="language-python">import os\nx = os.getcwd()</code></pre>')
    assert final.endswith("<i>(ответ обрезан из-за ограничения длины — напиши «продолжи»)</i>")
    assert env["session"].documents() == []


async def test_code_mode_is_closed_for_strangers_and_optional(bot_env):
    env = bot_env
    await _feed(env, _message_update("/code", user_id=STRANGER_ID))
    await _feed(env, _document(user_id=STRANGER_ID))
    await _feed(env, _callback_update(CodeModeCallback(action="on").pack(), user_id=STRANGER_ID))
    assert env["session"].requests == [] and await env["modes"].get(STRANGER_ID) == CHAT

    dp = create_dispatcher(bot_settings(), env["ai_client"], env["memory"], env["usage"])
    await dp.feed_update(env["bot"], _message_update("/code"))
    assert "недоступен" in env["session"].sent_texts()[-1]


async def test_help_and_start_mention_code_mode(bot_env):
    env = bot_env
    await _feed(env, _message_update("/start"))
    assert "/code" in env["session"].sent_texts()[-1]
    await _feed(env, _message_update("/help"))
    assert "/code — режим кода" in env["session"].sent_texts()[-1]


# ---------- Mini App ----------


@pytest.fixture
async def web_env(db):
    provider = CodeProvider()
    settings = web_settings(allowed_user_ids=frozenset({OWNER_ID, 222}))
    usage = UsageTracker(db, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    memory = MemoryManager(MessageRepository(db, store_limit=500), history_limit=30)
    preferences = PreferencesStore(db)
    long_memory = LongTermMemory(db)
    reminders = Reminders(db)
    instructions = InstructionsStore(db)
    modes = ModeStore(db)
    clients: list[TestClient] = []

    async def make(with_modes: bool = True):
        store = modes if with_modes else None
        app = create_web_app(
            settings,
            ChatService(
                settings, ai_client, memory, preferences, long_memory, reminders,
                instructions=instructions, modes=store,
            ),
            memory, usage, ai_client, SlidingWindowLimiter(10), preferences, long_memory,
            reminders, None, instructions, store,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    yield {"make": make, "provider": provider, "modes": modes}
    for client in clients:
        await client.close()


async def test_app_shows_and_switches_the_mode(web_env):
    client = await web_env["make"]()
    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["mode"] == "chat" and state["limits"] == {"max_text_length": MAX_TEXT_LENGTH}

    response = await client.post("/api/mode", json={"mode": "code"}, headers=auth())
    assert response.status == 200
    assert await response.json() == {"mode": "code", "limits": {"max_text_length": MAX_CODE_TEXT_LENGTH}}
    assert await web_env["modes"].get(OWNER_ID) == CODE and await web_env["modes"].get(222) == CHAT

    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["mode"] == "code" and state["limits"] == {"max_text_length": MAX_CODE_TEXT_LENGTH}
    other = await (await client.get("/api/state", headers=auth(sign_init_data(222)))).json()
    assert other["mode"] == "chat"

    await client.post("/api/chat", json={"text": "напиши цикл"}, headers=auth())
    assert "включён режим кода" in web_env["provider"].system
    assert web_env["provider"].calls[-1]["temperature"] == 0.2

    response = await client.post("/api/mode", json={"mode": "chat"}, headers=auth())
    assert (await response.json())["limits"] == {"max_text_length": MAX_TEXT_LENGTH}
    await client.post("/api/chat/stream", json={"text": "привет"}, headers=auth())
    assert "включён режим кода" not in web_env["provider"].system


async def test_app_accepts_long_messages_only_in_code_mode(web_env):
    client = await web_env["make"]()
    pasted = "x = 1\n" * 2000  # 12 000 символов

    for path in ("/api/chat", "/api/chat/stream"):
        response = await client.post(path, json={"text": pasted}, headers=auth())
        assert response.status == 400 and (await response.json())["error"]["code"] == "too_long"
    assert web_env["provider"].calls == []

    await web_env["modes"].set(OWNER_ID, CODE)  # как после /code в чате с ботом
    response = await client.post("/api/chat", json={"text": pasted}, headers=auth())
    assert response.status == 200 and web_env["provider"].question == pasted.strip()

    response = await client.post(
        "/api/chat", json={"text": "я" * (MAX_CODE_TEXT_LENGTH + 1)}, headers=auth()
    )
    assert response.status == 400
    assert str(MAX_CODE_TEXT_LENGTH) in (await response.json())["error"]["message"]
    response = await client.post(
        "/api/chat", json={"text": "я" * MAX_CODE_TEXT_LENGTH}, headers=auth()
    )
    assert response.status == 200


@pytest.mark.parametrize("body", [{"mode": "hacker"}, {"mode": 1}, {}, [1], "не json"])
async def test_app_rejects_unknown_modes(web_env, body):
    client = await web_env["make"]()
    kwargs = {"data": body} if isinstance(body, str) else {"json": body}
    response = await client.post("/api/mode", headers=auth(), **kwargs)
    assert response.status == 400
    assert await web_env["modes"].get(OWNER_ID) == CHAT


async def test_app_mode_requires_access_and_is_optional(web_env):
    client = await web_env["make"]()
    assert (await client.post("/api/mode", json={"mode": "code"})).status == 401
    stranger = auth(sign_init_data(STRANGER_ID))
    assert (await client.post("/api/mode", json={"mode": "code"}, headers=stranger)).status == 403

    client = await web_env["make"](with_modes=False)
    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["mode"] is None
    response = await client.post("/api/mode", json={"mode": "code"}, headers=auth())
    assert response.status == 503

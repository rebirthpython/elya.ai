"""Запуск кода из ответа Эли: разметка блока run, цикл «запуск → исправление», бот и Mini App."""

import json
import random

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage, SendPhoto
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient
from ai.prompts import RUN_HONESTY
from ai.providers.base import BaseProvider, CompletionResult
from database.session import create_engine, create_session_factory, init_db
from main import BOT_COMMANDS, create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.artifacts import FileStore
from services.chat import ChatService
from services.coderun import (
    CodeRunStarted,
    RunCutter,
    display_block,
    model_report,
    output_files,
    strip_run_marks,
)
from services.longterm import LongTermMemory
from services.modes import CODE, ModeStore
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.sandbox import RunResult, SandboxFile, SandboxLimits
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, _message_update
from tests.test_bot_flow import _settings as bot_settings
from tests.test_study_flow import StudySession
from tests.test_webapp import _settings as web_settings
from tests.test_webapp import auth
from web.server import create_web_app

PNG = b"\x89PNG\r\n\x1a\nfake"


# ---------- разметка блока run ----------


def feed_randomly(cutter: RunCutter, text: str, seed: int) -> str:
    rng = random.Random(seed)
    shown, i = "", 0
    while i < len(text) and cutter.block is None:
        size = rng.randint(1, 7)
        shown += cutter.feed(text[i:i + size])
        i += size
    if cutter.block is None:
        shown += cutter.finish()
    return shown


@pytest.mark.parametrize("seed", range(40))
def test_run_block_is_cut_right_after_it_closes(seed):
    text = "Проверю:\n\n```python run\nimport math\nprint(math.pi)\n```\nВывод: 3.14 (это модель выдумала)"
    cutter = RunCutter()
    shown = feed_randomly(cutter, text, seed)
    assert cutter.block == "import math\nprint(math.pi)"
    assert shown == cutter.text == "Проверю:\n\n```python\nimport math\nprint(math.pi)\n```"


@pytest.mark.parametrize("opening", ["```python run", "```py run", "```Python3-run", "```python exec", "  ```python run  "])
def test_run_mark_variants(opening):
    cutter = RunCutter()
    shown = cutter.feed(f"{opening}\nprint(1)\n```\nдальше")
    assert cutter.block == "print(1)" and "run" not in shown.lower() and "exec" not in shown


def test_ordinary_blocks_and_run_marks_inside_them_are_not_run():
    cutter = RunCutter()
    text = "Пример:\n```python\nprint(1)\n```\nКак просить запуск:\n````markdown\n```python run\nx\n```\n````\nконец"
    assert cutter.feed(text) + cutter.finish() == text
    assert cutter.block is None


def test_run_mark_in_the_middle_of_a_line_is_not_a_block():
    cutter = RunCutter()
    shown = cutter.feed("Пиши так: ") + cutter.feed("```python run\nprint(1)\n```") + cutter.finish()
    assert cutter.block is None and "```python run" in shown


def test_unclosed_block_is_run_but_a_truncated_one_is_not():
    cutter = RunCutter()
    assert cutter.feed("```python run\nprint(2)") + cutter.finish() == "```python\nprint(2)\n```"
    assert cutter.block == "print(2)"
    cut = RunCutter()
    cut.feed("```python run\nprint(2")
    cut.finish(truncated=True)
    assert cut.block is None


def test_inactive_cutter_only_hides_the_mark():
    cutter = RunCutter(active=False)
    text = "```python run\nprint(1)\n```\nпояснение"
    assert cutter.feed(text) + cutter.finish() == "```python\nprint(1)\n```\nпояснение"
    assert cutter.block is None
    assert strip_run_marks(text) == "```python\nprint(1)\n```\nпояснение"


def test_display_and_report_texts():
    ok = RunResult(exit_code=0, stdout="42\n", duration=0.2,
                   files=(SandboxFile("figure_1.png", PNG), SandboxFile("data.csv", b"a\n1\n")))
    shown = display_block(ok)
    assert "▶️ **Результат:**" in shown and "```text\n42\n```" in shown
    assert "📈 Графики и файлы `figure_1.png`, `data.csv` — ниже." in shown
    report = model_report(ok, 1, runs_left=2)
    assert "Код выхода: 0" in report and "stdout:\n42" in report
    assert "figure_1.png (картинка — собеседник её увидит)" in report and "data.csv (4 байт)" in report
    assert "```python run" in report  # можно исправить и запустить снова

    failed = RunResult(exit_code=1, stderr="Traceback ...\nNameError: name 'x' is not defined\n")
    shown = display_block(failed)
    assert "⚠️ **Ошибка при запуске:**" in shown and "NameError" in shown
    assert "код выхода 3" in display_block(RunResult(exit_code=3))
    assert "Запусков больше не осталось" in model_report(failed, 3, runs_left=0)

    slow = RunResult(exit_code=-9, stdout="старт\n", timed_out=True, duration=15.0)
    assert "не уложился в 15 с" in display_block(slow) and "Остановлено" in model_report(slow, 1, 1)

    long = RunResult(exit_code=0, stdout="\n".join(str(i) for i in range(500)))
    assert display_block(long).count("\n") < 60  # длинный вывод не превращается в файл


def test_output_files_keep_the_latest_of_each_name():
    files = output_files([
        SandboxFile("figure_1.png", b"old"), SandboxFile("out/a.csv", b"x"), SandboxFile("figure_1.png", PNG),
    ])
    assert [(f.name, f.kind, f.mime) for f in files] == [
        ("out_a.csv", "data", "text/csv"), ("figure_1.png", "image", "image/png"),
    ]
    assert files[-1].data == PNG


# ---------- цикл в ChatService ----------


class FakeSandbox:
    """Отвечает на код заготовками: print(1/0) — ошибка, plt — график, иначе вывод."""

    def __init__(self, max_runs: int = 3) -> None:
        self.max_runs = max_runs
        self.limits = SandboxLimits(timeout=15, memory_mb=512)
        self.packages = ["numpy", "matplotlib"]
        self.python_version = "3.12.0"
        self.codes: list[str] = []

    def describe(self) -> str:
        return "тестовая песочница"

    async def run(self, code: str) -> RunResult:
        self.codes.append(code)
        if "1/0" in code:
            return RunResult(exit_code=1, stderr='File "main.py", line 1\nZeroDivisionError: division by zero\n')
        files = (SandboxFile("figure_1.png", PNG),) if "plt" in code else ()
        return RunResult(exit_code=0, stdout="42\n", files=files, duration=0.1)


class StepsProvider(BaseProvider):
    """Отвечает по очереди заготовками и отдаёт их мелкими кусками, как настоящий поток."""

    def __init__(self, *replies: str) -> None:
        super().__init__("steps", "Steps AI", ("s1",))
        self.replies = list(replies)
        self.calls: list = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append(messages)
        text = self.replies.pop(0) if self.replies else "Готово."
        return CompletionResult(text=text, provider="steps", model=model)

    async def stream(self, messages, *, model, max_tokens, temperature):
        result = await self.complete(messages, model=model, max_tokens=max_tokens, temperature=temperature)
        for i in range(0, len(result.text), 5):
            yield result.text[i:i + 5]
        yield result


FIRST = "Посчитаю:\n\n```python run\nprint(1/0)\n```\nВывод: 42 (выдумано)"
SECOND = "Делить на ноль нельзя — исправлю:\n```python run\nimport matplotlib.pyplot as plt\nplt.plot([1, 2])\nprint(42)\n```"
THIRD = "Ответ: **42**, а график — ниже."


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'run.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


def _service(db, provider, sandbox, **kwargs):
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([provider], max_tokens=256, temperature=0.5)
    return ChatService(bot_settings(), client, memory, files=FileStore(db), sandbox=sandbox, **kwargs), memory


async def test_model_runs_code_sees_the_error_and_fixes_it(db):
    provider, sandbox = StepsProvider(FIRST, SECOND, THIRD), FakeSandbox()
    service, memory = _service(db, provider, sandbox)
    events = [e async for e in service.ask_stream(OWNER_ID, "посчитай и построй график")]
    result = events[-1]
    pieces = [e for e in events if isinstance(e, str)]
    assert [e.number for e in events if isinstance(e, CodeRunStarted)] == [1, 2]
    assert sandbox.codes == ["print(1/0)", "import matplotlib.pyplot as plt\nplt.plot([1, 2])\nprint(42)"]

    text = result.text
    assert "".join(pieces).strip() == text  # показано ровно то, что сохранится
    assert "выдумано" not in text and "python run" not in text
    assert "ZeroDivisionError" in text and "```text\n42\n```" in text and text.endswith(THIRD)
    assert text.index("print(1/0)") < text.index("ZeroDivisionError") < text.index("исправлю") < text.index("42")

    # Модель получила результаты как сообщения собеседника, после своего кода.
    second_call, third_call = provider.calls[1], provider.calls[2]
    assert second_call[-2].role == "assistant" and second_call[-2].content.endswith("print(1/0)\n```")
    assert second_call[-1].role == "user" and "ZeroDivisionError" in second_call[-1].content
    assert "Результат запуска №2" in third_call[-1].content and "figure_1.png (картинка" in third_call[-1].content
    assert "Запуск кода: ты умеешь" in provider.calls[0][0].content

    # График — файлом к ответу, ответ целиком — в истории.
    assert [(output.name, output.kind) for output, _ in result.files] == [("figure_1.png", "image")]
    assert result.files[0][1] is not None and result.files[0][1].kind == "image"
    history = await memory.get_history(OWNER_ID)
    assert history[-1].content == text


async def test_runs_per_answer_are_limited(db):
    loop = "Ещё раз:\n```python run\nprint(1/0)\n```"
    provider, sandbox = StepsProvider(loop, loop, loop, loop), FakeSandbox(max_runs=2)
    service, _ = _service(db, provider, sandbox)
    result = await service.ask(OWNER_ID, "запусти")
    assert len(sandbox.codes) == 2
    assert "Запусков больше не осталось" in provider.calls[2][-1].content
    assert result.text.endswith("Ещё раз:\n```python\nprint(1/0)\n```")  # третий блок не запущен


async def test_answer_without_a_run_is_unchanged(db):
    provider, sandbox = StepsProvider("Просто ответ без кода."), FakeSandbox()
    service, _ = _service(db, provider, sandbox)
    result = await service.ask(OWNER_ID, "привет")
    assert result.text == "Просто ответ без кода." and sandbox.codes == [] and result.files == ()


async def test_code_mode_prompt_with_and_without_sandbox(db):
    modes = ModeStore(db)
    await modes.set(OWNER_ID, CODE)
    provider = StepsProvider("ок", "ок")
    service, _ = _service(db, provider, FakeSandbox(), modes=modes)
    await service.ask(OWNER_ID, "напиши функцию")
    system = provider.calls[-1][0].content
    assert RUN_HONESTY[True] in system and "Python 3.12.0, пакеты: numpy, matplotlib" in system

    plain, _ = _service(db, provider, None, modes=modes)
    await plain.ask(OWNER_ID, "напиши функцию")
    system = provider.calls[-1][0].content
    assert RUN_HONESTY[False] in system and "python run" not in system


# ---------- бот ----------


class PhotoSession(StudySession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendPhoto):
            self.requests.append(method)
            return await super().make_request(bot, SendMessage(chat_id=method.chat_id, text="photo"))
        return await super().make_request(bot, method, timeout)

    def photos(self) -> list[SendPhoto]:
        return [m for m in self.requests if isinstance(m, SendPhoto)]


async def _bot(db, provider, sandbox):
    usage = UsageTracker(db, 100)
    service, memory = _service(db, provider, sandbox)
    client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    session = PhotoSession()
    bot = Bot(token="42:TEST", session=session)
    dp = create_dispatcher(bot_settings(), client, memory, usage, chat_service=service)
    return bot, dp, session


def test_run_is_in_the_menu():
    assert "run" in [command.command for command in BOT_COMMANDS]


async def test_bot_shows_the_run_result_and_sends_the_plot(db):
    provider, sandbox = StepsProvider(FIRST, SECOND, THIRD), FakeSandbox()
    bot, dp, session = await _bot(db, provider, sandbox)
    await dp.feed_update(bot, _message_update("построй график"))
    final = session.sent_texts()[-2] if session.photos() else session.sent_texts()[-1]
    assert "ZeroDivisionError" in final and "Ответ: <b>42</b>" in final and "выдумано" not in final
    assert "language-python" in final and "python run" not in final
    photo = session.photos()[-1]
    assert photo.caption == "📈 figure_1.png" and photo.photo.filename == "figure_1.png"


async def test_run_command(db):
    provider, sandbox = StepsProvider(), FakeSandbox()
    bot, dp, session = await _bot(db, provider, sandbox)
    await dp.feed_update(bot, _message_update("/run"))
    assert "Песочница для кода" in session.sent_texts()[-1] and "тестовая песочница" in session.sent_texts()[-1]

    await dp.feed_update(bot, _message_update("/run ```python\nimport matplotlib.pyplot as plt\nprint(42)\n```"))
    assert sandbox.codes[-1] == "import matplotlib.pyplot as plt\nprint(42)"
    assert any("Результат" in text and "42" in text for text in session.sent_texts())
    assert session.photos()[-1].caption == "📈 figure_1.png"
    assert provider.calls == []  # модель не участвует

    bot2, dp2, session2 = await _bot(db, provider, None)
    await dp2.feed_update(bot2, _message_update("/run print(1)"))
    assert "Запуск кода выключен" in session2.sent_texts()[-1]


# ---------- Mini App ----------


async def test_app_stream_reports_runs_and_plots(db):
    settings = web_settings()
    provider, sandbox = StepsProvider(FIRST, SECOND, THIRD), FakeSandbox()
    usage = UsageTracker(db, 100)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    service = ChatService(settings, client, memory, files=FileStore(db), sandbox=sandbox)
    app = create_web_app(
        settings, service, memory, usage, client, SlidingWindowLimiter(30),
        PreferencesStore(db), LongTermMemory(db), Reminders(db),
    )
    test = TestClient(TestServer(app))
    await test.start_server()
    try:
        response = await test.post("/api/chat/stream", json={"text": "построй график"}, headers=auth())
        events = [json.loads(line) for line in (await response.text()).splitlines() if line.strip()]
        assert [e["number"] for e in events if e["type"] == "run"] == [1, 2]
        done = events[-1]
        assert done["type"] == "done"
        reply = done["reply"]
        assert "".join(e["text"] for e in events if e["type"] == "delta").strip() == reply["content"]
        assert [(f["name"], f["kind"]) for f in reply["files"]] == [("figure_1.png", "image")]
        image = await test.get(f"/files/{reply['files'][0]['token']}/figure_1.png")
        assert image.status == 200 and image.headers["Content-Type"] == "image/png"
        assert await image.read() == PNG
    finally:
        await test.close()


async def test_runs_work_when_the_model_could_also_search(db):
    from services.search import WebSearch
    from tests.test_search import FakeSearch

    search_provider = FakeSearch()
    provider, sandbox = StepsProvider(FIRST, "Причина — деление на ноль. Готово."), FakeSandbox()
    service, _ = _service(db, provider, sandbox, search=WebSearch([search_provider], auto=True))
    result = await service.ask(OWNER_ID, "сколько будет 1/0 в питоне")
    assert sandbox.codes == ["print(1/0)"] and search_provider.queries == []
    assert "ZeroDivisionError" in result.text and result.text.endswith("Готово.")
    assert "выдумано" not in result.text

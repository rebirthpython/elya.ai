"""Рисование в Mini App: /api/chat, /api/chat/stream и /api/draw/again."""

import base64
import json

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendPhoto
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient
from database.models import DailyImageUsage
from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.drawing import AGAIN_TEXT, DrawingService
from services.longterm import LongTermMemory
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.usage import UsageTracker
from services.imagegen import ImageGenerator
from tests.test_draw_flow import PNG, FakeBackend, WriterProvider
from tests.test_webapp import OWNER_ID, STRANGER_ID, _settings, auth, sign_init_data
from web.server import create_web_app


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list = []

    async def send_photo(self, chat_id, photo, *, caption=None, reply_markup=None):
        if self.fail:
            raise TelegramForbiddenError(method=SendPhoto(chat_id=chat_id, photo="x"), message="bot was blocked")
        self.sent.append((chat_id, photo, caption, reply_markup))


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'web-draw.db'}")
    await init_db(engine)
    session_factory = create_session_factory(engine)
    memory = MemoryManager(MessageRepository(session_factory), history_limit=30)
    clients: list[TestClient] = []

    async def make(*, images=True, bot=None, image_limit=0, rate_limit=10):
        provider = WriterProvider()
        backend = FakeBackend()
        settings = _settings()
        usage = UsageTracker(session_factory, 100)
        ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
        drawing = DrawingService(
            ImageGenerator(
                [backend] if images else [],
                usage=UsageTracker(session_factory, image_limit, table=DailyImageUsage),
            ),
            ai_client,
            memory,
        )
        app = create_web_app(
            settings,
            ChatService(settings, ai_client, memory),
            memory,
            usage,
            ai_client,
            SlidingWindowLimiter(rate_limit),
            PreferencesStore(session_factory),
            LongTermMemory(session_factory),
            Reminders(session_factory),
            drawing=drawing,
            bot=bot,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client, backend, provider

    yield {"make": make, "memory": memory}
    for client in clients:
        await client.close()
    await engine.dispose()


def _image_of(reply) -> bytes:
    assert reply["image"].startswith("data:image/png;base64,")
    return base64.b64decode(reply["image"].split(",", 1)[1])


async def test_state_reports_images(env):
    client, _, _ = await env["make"](image_limit=20)
    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["images"] == {"enabled": True, "used": 0, "limit": 20}

    client, _, _ = await env["make"](images=False)
    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["images"] == {"enabled": False}


async def test_stream_endpoint_draws_and_sends_a_copy_to_the_chat(env):
    bot = FakeBot()
    client, backend, _ = await env["make"](bot=bot)
    response = await client.post("/api/chat/stream", json={"text": "нарисуй кота"}, headers=auth())
    assert response.status == 200 and response.content_type == "application/x-ndjson"
    lines = [json.loads(line) for line in (await response.text()).splitlines() if line]
    assert len(lines) == 1 and lines[0]["type"] == "done"
    reply = lines[0]["reply"]
    assert _image_of(reply) == PNG
    assert reply["provider"] == "images" and reply["model"] == "fake-1"
    assert reply["content"].startswith("🎨 Нарисовала картинку: Кот в шляпе")
    assert reply["sent_to_chat"] is True and isinstance(reply["drawing_id"], int)
    assert lines[0]["images"]["used"] == 1
    assert backend.prompts == ["PROMPT(нарисуй кота)"]

    [(chat_id, photo, caption, markup)] = bot.sent
    assert chat_id == OWNER_ID and photo.data == PNG and caption == "🎨 Кот в шляпе (из приложения)"
    assert markup.inline_keyboard[0][0].text == "🔄 Ещё вариант"

    history = await env["memory"].get_history(OWNER_ID)
    assert [m.content for m in history] == ["нарисуй кота", reply["content"]]


async def test_plain_chat_endpoint_and_draw_command(env):
    client, backend, _ = await env["make"]()
    response = await client.post("/api/chat", json={"text": "/draw закат"}, headers=auth())
    data = await response.json()
    assert response.status == 200 and _image_of(data["reply"]) == PNG
    assert data["reply"]["sent_to_chat"] is False  # бота нет — копия не отправляется
    assert backend.prompts == ["PROMPT(закат)"]

    response = await client.post("/api/chat", json={"text": "/draw"}, headers=auth())
    assert response.status == 400

    response = await client.post("/api/chat", json={"text": "привет"}, headers=auth())
    assert (await response.json())["reply"]["content"].startswith("Ответ на")


async def test_again_endpoint(env):
    client, backend, _ = await env["make"]()
    first = await (await client.post("/api/chat", json={"text": "нарисуй кота"}, headers=auth())).json()
    drawing_id = first["reply"]["drawing_id"]

    response = await client.post("/api/draw/again", json={"id": drawing_id}, headers=auth())
    data = await response.json()
    assert response.status == 200 and _image_of(data["reply"]) == PNG
    assert data["reply"]["drawing_id"] != drawing_id
    assert len(backend.prompts) == 2
    history = await env["memory"].get_history(OWNER_ID)
    assert history[-2].content == AGAIN_TEXT and len(history) == 4

    assert (await client.post("/api/draw/again", json={"id": 999}, headers=auth())).status == 410
    assert (await client.post("/api/draw/again", json={"id": "1"}, headers=auth())).status == 400
    # чужую картинку перерисовать нельзя, а посторонних не пускает проверка доступа
    response = await client.post("/api/draw/again", json={"id": drawing_id}, headers=auth(sign_init_data(STRANGER_ID)))
    assert response.status == 403


async def test_not_a_drawing_request_is_answered_as_usual(env):
    client, backend, provider = await env["make"]()
    provider.decision = False
    response = await client.post("/api/chat/stream", json={"text": "нарисуй мне план"}, headers=auth())
    text = await response.text()
    assert '"type": "delta"' in text and backend.prompts == []


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("rejected", 422, "image_rejected"),
        ("rate_limit", 503, "image_rate_limit"),
        ("config", 503, "image_config"),
        ("unavailable", 503, "image_unavailable"),
    ],
)
async def test_drawing_errors(env, kind, status, code):
    client, backend, _ = await env["make"]()
    backend.errors = [kind]
    response = await client.post("/api/chat/stream", json={"text": "нарисуй кота"}, headers=auth())
    assert response.status == status and (await response.json())["error"]["code"] == code
    assert await env["memory"].get_history(OWNER_ID) == []


async def test_daily_image_limit_and_failed_delivery(env):
    client, _, _ = await env["make"](image_limit=1, bot=FakeBot(fail=True))
    first = await (await client.post("/api/chat", json={"text": "нарисуй кота"}, headers=auth())).json()
    assert first["reply"]["sent_to_chat"] is False  # бот заблокирован — картинка всё равно в приложении
    response = await client.post("/api/chat", json={"text": "нарисуй пса"}, headers=auth())
    assert response.status == 429 and (await response.json())["error"]["code"] == "image_limit"


async def test_images_off(env):
    client, backend, _ = await env["make"](images=False)
    response = await client.post("/api/chat", json={"text": "/draw кот"}, headers=auth())
    assert response.status == 503 and (await response.json())["error"]["code"] == "images_off"
    response = await client.post("/api/chat", json={"text": "нарисуй кота"}, headers=auth())
    assert (await response.json())["reply"]["content"].startswith("Ответ на")
    response = await client.post("/api/draw/again", json={"id": 1}, headers=auth())
    assert response.status == 503


async def test_drawing_respects_rate_limit(env):
    client, backend, _ = await env["make"](rate_limit=1)
    assert (await client.post("/api/chat", json={"text": "нарисуй кота"}, headers=auth())).status == 200
    assert (await client.post("/api/chat", json={"text": "нарисуй пса"}, headers=auth())).status == 429
    assert len(backend.prompts) == 1

"""Монтаж из Mini App: загрузка, просьбы с ходом работы, кнопки, общая с ботом сессия."""

import json
from collections import defaultdict

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendVideo
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient
from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.longterm import LongTermMemory
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.usage import UsageTracker
from tests.conftest import needs_ffmpeg
from tests.test_video_flow import Planner, VideoSession
from tests.test_webapp import OWNER_ID, _settings, auth
from video.ffmpeg import FFmpeg
from video.service import VideoService
from web.server import create_web_app

pytestmark = needs_ffmpeg


@pytest.fixture
async def app_env(tmp_path, media_dir, monkeypatch):
    import asyncio

    monkeypatch.setattr("bot.handlers.video._user_locks", defaultdict(asyncio.Lock))
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'web.db'}")
    await init_db(engine)
    factory = create_session_factory(engine)
    memory = MemoryManager(MessageRepository(factory), history_limit=30)
    usage = UsageTracker(factory, 100)
    planner = Planner()
    ai = AIClient([planner], max_tokens=100, temperature=0.5, usage=usage)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    session = VideoSession(inbox)
    bot = Bot(token="42:TEST", session=session)
    storage = MemoryStorage()
    clients = []

    async def make(*, with_video=True, **overrides):
        settings = _settings(**overrides)
        video = None
        if with_video:
            video = VideoService(ai, FFmpeg(), workdir=tmp_path / "work")
            await video.start()
        app = create_web_app(
            settings, ChatService(settings, ai, memory), memory, usage, ai, SlidingWindowLimiter(30),
            PreferencesStore(factory), LongTermMemory(factory), Reminders(factory, ai),
            video=video, bot=bot, fsm_storage=storage,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client, video

    yield {"make": make, "session": session, "planner": planner, "storage": storage, "media": media_dir}
    for client in clients:
        await client.close()
    await engine.dispose()


def _upload(path, content_type="video/mp4", name=None):
    form = FormData()
    form.add_field("file", path.read_bytes(), filename=name or path.name, content_type=content_type)
    return form


def _events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def _state(client):
    return (await (await client.get("/api/state", headers=auth())).json())["video"]


async def test_full_editing_session_from_the_app(app_env):
    client, video = await app_env["make"]()
    assert await _state(client) == {"available": True, "upload_limit_mb": 100, "session": None}

    start = await (await client.post("/api/video/start", headers=auth(), json={})).json()
    assert "Монтаж видео" in start["message"] and start["session"]["clips"] == []

    resp = await client.post("/api/video/upload", headers=auth(), data=_upload(app_env["media"] / "talk.mp4"))
    data = await resp.json()
    assert resp.status == 200, data
    assert data["session"]["clips"] == [{"duration": 3.0, "label": "0:03, горизонтальное"}]
    assert "1) 0:03, горизонтальное" in data["message"]

    # Сессия общая с ботом: в его FSM — тот же монтаж.
    fsm = FSMContext(app_env["storage"], StorageKey(bot_id=42, chat_id=OWNER_ID, user_id=OWNER_ID))
    assert await fsm.get_state() == "VideoStates:session"

    resp = await client.post("/api/video/edit", headers=auth(), json={"text": "ускорь в 2 раза"})
    assert resp.headers["Content-Type"].startswith("application/x-ndjson")
    events = _events(await resp.text())
    assert {e["type"] for e in events[:-1]} <= {"progress", "ping"}
    assert any(e.get("text") == "🎬 Собираю видео…" for e in events)
    done = events[-1]
    outcome = done["outcome"]
    assert outcome["rendered"] and outcome["kind"] == "video" and outcome["duration"] == pytest.approx(1.5, abs=0.05)
    assert "×2" in outcome["text"] and "в чате с ботом" in outcome["text"]
    assert outcome["poster"].startswith("data:image/jpeg;base64,")
    sent = [m for m in app_env["session"].requests if isinstance(m, SendVideo)]
    assert len(sent) == 1 and sent[0].chat_id == OWNER_ID  # видео ушло и в чат с ботом
    assert done["session"]["has_result"] and app_env["planner"].requests[-1] == ("plan", "ускорь в 2 раза")

    result = await client.get(f"/api/video/result/{outcome['result']}", headers=auth())
    assert result.status == 200 and result.headers["Content-Type"] == "video/mp4"
    assert len(await result.read()) > 1000
    assert (await client.get("/api/video/result/..%2Fsecret", headers=auth())).status == 404
    assert (await client.get("/api/video/result/result-1.mp4")).status == 401

    resp = await client.post("/api/video/action", headers=auth(), json={"action": "quick", "value": "pauses"})
    second = _events(await resp.text())[-1]
    assert second["outcome"]["rendered"] and "Паузы вырезаны" in second["outcome"]["text"]
    labels = [a["label"] for a in second["session"]["actions"]]
    assert "🗣 Вернуть паузы" in labels and "↩️ Отменить" in labels

    resp = await client.post("/api/video/action", headers=auth(), json={"action": "undo"})
    undone = _events(await resp.text())[-1]["outcome"]
    assert undone["reused"] and "уже был" in undone["text"]
    assert undone["result"] is None  # прошлый вариант прислан в чат повторно, без монтажа

    resp = await client.post("/api/video/action", headers=auth(), json={"action": "done"})
    assert (await resp.json())["session"] is None
    assert not video.workspace(OWNER_ID).root.exists()
    assert await fsm.get_state() is None
    resp = await client.post("/api/video/edit", headers=auth(), json={"text": "ещё"})
    assert resp.status == 409 and (await resp.json())["error"]["code"] == "no_session"


async def test_upload_size_limit(app_env, tmp_path):
    client, video = await app_env["make"](video_upload_max_mb=1)
    big = tmp_path / "big.mp4"
    big.write_bytes(b"\0" * (1024 * 1024 + 10))
    resp = await client.post("/api/video/upload", headers=auth(), data=_upload(big))
    assert resp.status == 413 and "1 МБ" in (await resp.json())["error"]["message"]
    assert not video.workspace(OWNER_ID).root.exists()  # недогруженный файл и пустой монтаж удалены
    assert (await _state(client))["session"] is None


async def test_bad_files_and_requests(app_env, tmp_path):
    client, _ = await app_env["make"]()
    resp = await client.post("/api/video/upload", headers=auth(),
                             data=_upload(app_env["media"] / "talk.mp4", content_type="image/png"))
    assert resp.status == 415
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    resp = await client.post("/api/video/upload", headers=auth(), data=_upload(broken))
    assert resp.status == 400 and "повреждён" in (await resp.json())["error"]["message"]
    assert (await _state(client))["session"] is None  # битый файл не оставляет пустой монтаж

    await client.post("/api/video/start", headers=auth(), json={})
    resp = await client.post("/api/video/edit", headers=auth(), json={"text": "обрежь"})
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "no_clips"
    resp = await client.post("/api/video/action", headers=auth(), json={"action": "quick", "value": "fly"})
    assert resp.status == 400

    music = await client.post("/api/video/upload", headers=auth(),
                              data=_upload(app_env["media"] / "music.m4a", content_type="audio/mp4", name="песня.m4a"))
    assert (await music.json())["session"]["music"] == {"title": "песня.m4a", "duration": 3.0}


async def test_without_ffmpeg_video_is_unavailable(app_env):
    client, _ = await app_env["make"](with_video=False)
    assert await _state(client) == {"available": False}
    resp = await client.post("/api/video/start", headers=auth(), json={})
    assert resp.status == 503 and "ffmpeg" in (await resp.json())["error"]["message"]

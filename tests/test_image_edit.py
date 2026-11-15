"""Правка картинок: подготовка исходника, запросы к Cloudflare и OpenAI-совместимым сервисам,
решение «правка или вопрос о фото», бот (фото с подписью, ответ на картинку, /edit, кнопка)
и приложение (/api/photo, «сделай фон синим» после картинки, /api/draw/select)."""

import io
import json
from datetime import datetime, timezone

import httpx
import pytest
from aiogram import Bot
from aiogram.methods import SendPhoto
from aiogram.types import Chat, Message, PhotoSize, Update, User
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult
from bot.keyboards.draw import DrawCallback
from config.settings import ImageProviderConfig, load_settings
from database.session import create_engine, create_session_factory, init_db
from main import BOT_COMMANDS, create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.drawing import AGAIN_TEXT, DrawingService
from services.imagegen import (
    CloudflareBackend,
    GeneratedImage,
    ImageGenerationError,
    ImageGenerator,
    ImageProviderError,
    OpenAIImagesBackend,
    SourceImageError,
    prepare_source,
)
from services.instructions import InstructionsStore
from services.longterm import LongTermMemory
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, _callback_update, _message_update
from tests.test_bot_flow import _settings as bot_settings
from tests.test_draw_flow import PhotoSession
from tests.test_webapp import _settings as web_settings
from tests.test_webapp import auth
from web.server import create_web_app


def make_image(width=800, height=600, mode="RGB", fmt="PNG") -> bytes:
    color = (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)
    out = io.BytesIO()
    Image.new(mode, (width, height), color).save(out, format=fmt)
    return out.getvalue()


EDITED = make_image(64, 64)


# ---------- исходник ----------


def test_source_is_jpeg_with_kept_proportions():
    source = prepare_source(make_image(3000, 1500, "RGBA"))
    assert source.data[:3] == b"\xff\xd8\xff" and (source.width, source.height) == (1024, 512)
    small = Image.open(io.BytesIO(source.scaled(511)))
    assert max(small.size) == 511
    width, height = source.output_size()
    assert (width, height) == (1024, 512) and width % 16 == 0 and height % 16 == 0
    portrait = prepare_source(make_image(300, 900))
    assert portrait.output_size() == (336, 1024)


def test_not_an_image_is_rejected():
    with pytest.raises(SourceImageError):
        prepare_source(b"definitely not a picture")
    with pytest.raises(SourceImageError):
        prepare_source(b"")


# ---------- сервисы ----------


def _config(kind="cloudflare", edit_model="@cf/black-forest-labs/flux-2-klein-4b", name="cloudflare"):
    base = "https://api.cloudflare.com/client/v4/accounts/abc/ai/run" if kind == "cloudflare" else "https://gen.example/v1"
    return ImageProviderConfig(
        name=name, title=name.title(), kind=kind, base_url=base, api_key="key",
        model="@cf/black-forest-labs/flux-1-schnell" if kind == "cloudflare" else "flux", edit_model=edit_model,
    )


def _capture(response):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["type"] = request.headers["content-type"]
        seen["body"] = request.content
        return response(request)

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_cloudflare_edit_sends_multipart_with_a_small_source():
    import base64

    seen, client = _capture(lambda r: httpx.Response(200, json={"result": {"image": base64.b64encode(EDITED).decode()}}))
    backend = CloudflareBackend(_config(), client)
    image = await backend.edit("Remove the background", prepare_source(make_image(2000, 1000)), seed=7)
    assert image.data == EDITED and image.model == "@cf/black-forest-labs/flux-2-klein-4b"
    assert seen["url"].endswith("/@cf/black-forest-labs/flux-2-klein-4b")
    assert seen["type"].startswith("multipart/form-data")
    body = seen["body"]
    assert b'name="prompt"' in body and b"Remove the background" in body
    assert b'name="input_image_0"' in body and b'name="width"\r\n\r\n1024' in body
    assert b'name="height"\r\n\r\n512' in body
    await client.aclose()


async def test_openai_compatible_edit_posts_to_images_edits():
    import base64

    seen, client = _capture(lambda r: httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(EDITED).decode()}]}))
    backend = OpenAIImagesBackend(_config("openai", "kontext", "pollinations"), client, send_seed=True)
    image = await backend.edit("Make it winter", prepare_source(make_image()), seed=3)
    assert image.data == EDITED and image.model == "kontext"
    assert seen["url"] == "https://gen.example/v1/images/edits"
    assert b'name="model"\r\n\r\nkontext' in seen["body"] and b'name="image"; filename="source.jpg"' in seen["body"]
    await client.aclose()


async def test_backend_without_edit_model_refuses():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    backend = CloudflareBackend(_config(edit_model=""), client)
    with pytest.raises(ImageProviderError) as error:
        await backend.edit("x", prepare_source(make_image()), seed=1)
    assert error.value.kind == "config"
    await client.aclose()


class EditBackend:
    def __init__(self, name="fake", fail_with=None, edit_model="edit-1"):
        self.name, self.title, self.model, self.edit_model = name, name.title(), "draw-1", edit_model
        self.fail_with = fail_with
        self.edits: list[str] = []
        self.generated: list[str] = []

    async def generate(self, prompt, *, width, height, seed):
        self.generated.append(prompt)
        return GeneratedImage(EDITED, "image/png", self.title, self.model)

    async def edit(self, prompt, source, *, seed):
        self.edits.append(prompt)
        if self.fail_with:
            raise ImageProviderError("fail", provider=self.name, kind=self.fail_with)
        return GeneratedImage(EDITED, "image/png", self.title, self.edit_model)


async def test_generator_falls_back_and_pauses_only_editing():
    first, second = EditBackend("first", fail_with="rate_limit"), EditBackend("second")
    generator = ImageGenerator([first, second])
    source = prepare_source(make_image())
    assert (await generator.edit("x", source)).provider == "Second"
    await generator.edit("y", source)
    assert first.edits == ["x"]  # упёрся в лимит — пропускается
    await generator.generate("draw")  # рисование у того же сервиса не на паузе
    assert first.generated == ["draw"]

    only_drawing = ImageGenerator([EditBackend(edit_model="")])
    assert not only_drawing.can_edit
    with pytest.raises(ImageGenerationError):
        await only_drawing.edit("x", source)


def test_edit_models_from_settings(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "abc123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("POLLINATIONS_API_KEY", "p")
    settings = load_settings()
    edit_models = {p.name: p.edit_model for p in settings.image_providers}
    assert edit_models == {"cloudflare": "@cf/black-forest-labs/flux-2-klein-4b", "pollinations": "kontext"}
    assert settings.image_editing_enabled
    monkeypatch.setenv("CLOUDFLARE_EDIT_MODEL", "off")
    monkeypatch.setenv("POLLINATIONS_EDIT_MODEL", "off")
    assert not load_settings().image_editing_enabled


# ---------- сервис рисования ----------


class EditWriter(BaseProvider):
    """Пишет инструкции для правки (и промпты рисунков); остальное — эхом."""

    def __init__(self):
        super().__init__("echo", "Echo AI", ("echo-1",), vision_models=("echo-1",))

    async def complete(self, messages, *, model, max_tokens, temperature):
        system = messages[0].content
        if system.startswith("Ты помогаешь редактировать картинки"):
            request = messages[-1].content.rsplit("Сообщение: ", 1)[-1]
            if "конспект" in request and "edit всегда true" not in system:
                return CompletionResult(text='{"edit": false}', provider="echo", model=model)
            text = json.dumps({"edit": True, "prompt": f"EDIT({request})", "title": "Фон убран"}, ensure_ascii=False)
            return CompletionResult(text=text, provider="echo", model=model)
        if system.startswith("Ты помогаешь рисовать картинки"):
            text = json.dumps({"draw": True, "prompt": "PROMPT", "title": "Кот", "orientation": "square"})
            return CompletionResult(text=text, provider="echo", model=model)
        return CompletionResult(text=f"Ответ на: {messages[-1].content}", provider="echo", model=model)


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'edit.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


def _drawing(db, backend=None, clock=None):
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    ai_client = AIClient([EditWriter()], max_tokens=256, temperature=0.5)
    backend = backend or EditBackend()
    kwargs = {"clock": clock} if clock else {}
    return DrawingService(ImageGenerator([backend]), ai_client, memory, **kwargs), memory, backend, ai_client


async def test_edit_plan_saves_history_and_again_reuses_the_source(db):
    drawing, memory, backend, _ = _drawing(db)
    assert await drawing.plan_edit(1, "сделай конспект лекции") is None
    plan = await drawing.plan_edit(1, "убери фон")
    assert plan.edit and plan.prompt == "EDIT(убери фон)"

    result = await drawing.edit(1, plan, make_image(), request="📷 Фото: убери фон")
    assert result.caption == "✏️ Фон убран" and drawing.last_image(1) == EDITED
    history = await memory.get_history(1)
    assert history[0].content == "📷 Фото: убери фон"
    assert history[1].content.startswith("✏️ Изменила картинку: Фон убран")

    again = await drawing.again(1, result.id)
    assert backend.edits == ["EDIT(убери фон)", "EDIT(убери фон)"] and again.plan.edit
    assert (await memory.get_history(1))[2].content == AGAIN_TEXT


async def test_last_image_expires_and_select_picks_an_older_one(db):
    now = [0.0]
    drawing, _, _, _ = _drawing(db, clock=lambda: now[0])
    drawn = await drawing.draw(1, (await drawing.plan(1, "нарисуй кота")))
    drawing.remember_image(1, b"photo")
    assert drawing.last_image(1) == b"photo"
    assert drawing.select(1, drawn.id) and drawing.last_image(1) == EDITED
    assert not drawing.select(2, drawn.id)  # чужая картинка
    now[0] = 4000
    assert drawing.last_image(1) is None


# ---------- бот ----------

_update_id = 10_000


def _photo_message(caption=None, reply_to=None, *, text=None, photo=True):
    global _update_id
    _update_id += 1
    user = User(id=OWNER_ID, is_bot=False, first_name="Аня")
    extra = {}
    if photo:
        extra["photo"] = [PhotoSize(file_id=f"ph{_update_id}", file_unique_id="u", width=800, height=600)]
    return Message(
        message_id=_update_id, date=datetime.now(timezone.utc), chat=Chat(id=OWNER_ID, type="private"),
        from_user=user, caption=caption, text=text, reply_to_message=reply_to, **extra,
    )


def _update(message):
    return Update(update_id=message.message_id, message=message)


@pytest.fixture
async def bot_env(db):
    drawing, memory, backend, ai_client = _drawing(db)
    usage = UsageTracker(db, 100)
    session = PhotoSession()
    bot = Bot(token="42:TEST", session=session)
    downloaded: list[str] = []

    async def download(file, *args, **kwargs):
        downloaded.append(file.file_id)
        return io.BytesIO(make_image())

    bot.download = download
    dp = create_dispatcher(bot_settings(), ai_client, memory, usage, drawing=drawing)
    return {"bot": bot, "dp": dp, "session": session, "backend": backend, "drawing": drawing,
            "downloaded": downloaded, "memory": memory}


async def _feed(env, update):
    await env["dp"].feed_update(env["bot"], update)


def test_edit_command_is_in_the_menu():
    assert "edit" in [command.command for command in BOT_COMMANDS]


async def test_photo_with_an_edit_caption_is_edited(bot_env):
    env = bot_env
    await _feed(env, _update(_photo_message("Убери фон")))
    photo = env["session"].photos()[-1]
    assert isinstance(photo, SendPhoto) and photo.caption == "✏️ Фон убран"
    buttons = [b.callback_data for row in photo.reply_markup.inline_keyboard for b in row]
    assert DrawCallback(action="edit", id=1).pack() in buttons
    assert env["backend"].edits == ["EDIT(Убери фон)"]


async def test_photo_with_a_question_is_answered_as_usual(bot_env):
    env = bot_env
    await _feed(env, _update(_photo_message("Что на фото?")))
    assert env["session"].photos() == [] and env["backend"].edits == []
    assert env["session"].sent_texts()[-1].startswith("Ответ на:")


async def test_follow_up_and_reply_edit_the_right_picture(bot_env):
    env = bot_env
    await _feed(env, _update(_photo_message()))  # фото без подписи — Эля его описывает
    await _feed(env, _message_update("сделай её зимней"))
    assert env["backend"].edits == ["EDIT(сделай её зимней)"]

    await _feed(env, _message_update("сделай конспект"))  # не правка — обычный ответ
    assert len(env["backend"].edits) == 1

    original = _photo_message(photo=True)
    reply = _photo_message(reply_to=original, text="добавь шляпу", photo=False)
    await _feed(env, _update(reply))
    assert env["backend"].edits[-1] == "EDIT(добавь шляпу)"
    assert env["downloaded"][-1] == original.photo[0].file_id


async def test_edit_command_and_button(bot_env):
    env = bot_env
    await _feed(env, _message_update("/edit убери фон"))
    assert "Пришли фото с подписью" in env["session"].sent_texts()[-1]  # картинки ещё нет

    await _feed(env, _message_update("нарисуй кота"))
    drawn_id = 1
    await _feed(env, _message_update("/edit добавь шляпу"))
    assert env["backend"].edits == ["EDIT(добавь шляпу)"]

    await _feed(env, _callback_update(DrawCallback(action="edit", id=drawn_id).pack()))
    assert "Напиши, что изменить" in env["session"].sent_texts()[-1]
    assert env["drawing"].last_image(OWNER_ID) == EDITED


# ---------- приложение ----------


@pytest.fixture
async def web_env(db):
    settings = web_settings()
    drawing, memory, backend, ai_client = _drawing(db)
    usage = UsageTracker(db, 100)
    preferences, long_memory, reminders = PreferencesStore(db), LongTermMemory(db), Reminders(db)
    instructions = InstructionsStore(db)
    app = create_web_app(
        settings,
        ChatService(settings, ai_client, memory, preferences, long_memory, reminders, instructions=instructions),
        memory, usage, ai_client, SlidingWindowLimiter(30), preferences, long_memory,
        reminders, None, instructions, drawing=drawing,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    yield {"client": client, "backend": backend, "memory": memory}
    await client.close()


def _photo_form(text):
    form = FormData()
    form.add_field("text", text)
    form.add_field("image", make_image(), filename="photo.png", content_type="image/png")
    return form


async def test_app_photo_edit_and_follow_up(web_env):
    client = web_env["client"]
    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["images"]["edit"] is True

    response = await client.post("/api/photo", data=_photo_form("убери фон"), headers=auth())
    data = await response.json()
    assert response.status == 200 and data["reply"]["image"].startswith("data:image/png")
    assert data["reply"]["content"].startswith("✏️ Изменила картинку")

    response = await client.post("/api/chat", json={"text": "сделай фон синим"}, headers=auth())
    assert (await response.json())["reply"]["drawing_id"] == 2
    assert web_env["backend"].edits == ["EDIT(убери фон)", "EDIT(сделай фон синим)"]

    response = await client.post("/api/draw/select", json={"id": 1}, headers=auth())
    assert response.status == 200
    assert (await client.post("/api/draw/select", json={"id": 99}, headers=auth())).status == 410

    response = await client.post("/api/photo", data=_photo_form("что на фото?"), headers=auth())
    assert (await response.json())["reply"]["content"].startswith("Ответ на:")


async def test_app_edit_command_without_a_picture(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
    await init_db(engine)
    db = create_session_factory(engine)
    settings = web_settings()
    drawing, memory, _, ai_client = _drawing(db)
    usage = UsageTracker(db, 100)
    preferences, long_memory, reminders = PreferencesStore(db), LongTermMemory(db), Reminders(db)
    app = create_web_app(
        settings, ChatService(settings, ai_client, memory), memory, usage, ai_client,
        SlidingWindowLimiter(30), preferences, long_memory, reminders, drawing=drawing,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    response = await client.post("/api/chat", json={"text": "/edit убери фон"}, headers=auth())
    assert response.status == 400 and (await response.json())["error"]["code"] == "no_image"
    await client.close()
    await engine.dispose()

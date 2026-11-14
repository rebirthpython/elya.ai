"""Рисование в чате с ботом: апдейты идут через настоящий Dispatcher, Telegram и сервис
картинок подменены."""

import json
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendDocument, SendPhoto
from aiogram.types import Chat, Message, PhotoSize, Update, User, Voice

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult
from bot.keyboards.draw import DrawCallback
from database.session import create_engine, create_session_factory, init_db
from main import create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.drawing import AGAIN_TEXT, DrawingService
from services.imagegen import GeneratedImage, ImageGenerator, ImageProviderError
from services.usage import UsageTracker
from tests.test_bot_flow import (
    OWNER_ID,
    STRANGER_ID,
    RecordingSession,
    _callback_update,
    _message_update,
    _settings,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class PhotoSession(RecordingSession):
    """Как RecordingSession, но умеет отправлять фото и файлы."""

    def __init__(self) -> None:
        super().__init__()
        self.reject_photo = False

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendPhoto):
            self.requests.append(method)
            if self.reject_photo:
                raise TelegramBadRequest(method=method, message="PHOTO_INVALID_DIMENSIONS")
            return Message(
                message_id=len(self.requests), date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"), caption=method.caption,
                photo=[PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)],
            )
        if isinstance(method, SendDocument):
            self.requests.append(method)
            return Message(
                message_id=len(self.requests), date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"), caption=method.caption,
            )
        return await super().make_request(bot, method, timeout)

    def photos(self) -> list:
        return [m for m in self.requests if isinstance(m, (SendPhoto, SendDocument))]


class WriterProvider(BaseProvider):
    """Пишет промпты для картинок, на остальное отвечает эхом."""

    def __init__(self) -> None:
        super().__init__("echo", "Echo AI", ("echo-1",))
        self.decision = True

    async def complete(self, messages, *, model, max_tokens, temperature):
        if messages[0].content.startswith("Ты помогаешь рисовать картинки"):
            request = messages[-1].content.rsplit("Просьба: ", 1)[-1]
            if not self.decision and "draw всегда true" not in messages[0].content:
                text = '{"draw": false}'
            else:
                text = json.dumps({
                    "draw": True, "prompt": f"PROMPT({request})", "title": "Кот в шляпе",
                    "orientation": "square",
                }, ensure_ascii=False)
            return CompletionResult(text=text, provider="echo", model=model)
        return CompletionResult(text=f"Ответ на: {messages[-1].content}", provider="echo", model=model)


class FakeBackend:
    def __init__(self) -> None:
        self.name, self.title, self.model = "fake", "Fake Images", "fake-1"
        self.errors: list[str] = []
        self.prompts: list[str] = []

    async def generate(self, prompt, *, width, height, seed):
        self.prompts.append(prompt)
        if self.errors:
            raise ImageProviderError("fail", provider=self.name, kind=self.errors.pop(0))
        return GeneratedImage(PNG, "image/png", self.title, self.model)


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'draw.db'}")
    await init_db(engine)
    session_factory = create_session_factory(engine)
    provider = WriterProvider()
    usage = UsageTracker(session_factory, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    memory = MemoryManager(MessageRepository(session_factory), history_limit=30)
    backend = FakeBackend()
    drawing = DrawingService(ImageGenerator([backend]), ai_client, memory)
    session = PhotoSession()
    bot = Bot(token="42:TEST", session=session)

    def make(drawing_service=drawing, speech=None):
        return create_dispatcher(
            _settings(), ai_client, memory, usage, drawing=drawing_service, speech=speech
        )

    yield {
        "bot": bot, "session": session, "provider": provider, "memory": memory,
        "backend": backend, "drawing": drawing, "make": make,
    }
    await engine.dispose()


async def test_draw_request_in_chat_sends_a_picture(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("нарисуй кота в шляпе"))

    [photo] = env["session"].photos()
    assert isinstance(photo, SendPhoto) and photo.chat_id == OWNER_ID
    assert photo.caption == "🎨 Кот в шляпе"
    assert photo.photo.data == PNG and photo.photo.filename.endswith(".png")
    button = photo.reply_markup.inline_keyboard[0][0]
    assert button.text == "🔄 Ещё вариант"
    assert DrawCallback.unpack(button.callback_data).action == "again"
    assert env["backend"].prompts == ["PROMPT(нарисуй кота в шляпе)"]

    history = await env["memory"].get_history(OWNER_ID)
    assert history[0].content == "нарисуй кота в шляпе"
    assert history[1].content.startswith("🎨 Нарисовала картинку: Кот в шляпе")


async def test_again_button_draws_another_variant(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("нарисуй кота"))
    data = env["session"].photos()[0].reply_markup.inline_keyboard[0][0].callback_data
    await dp.feed_update(env["bot"], _callback_update(data))
    assert len(env["session"].photos()) == 2
    assert len(env["backend"].prompts) == 2 and env["backend"].prompts[0] == env["backend"].prompts[1]
    assert (await env["memory"].get_history(OWNER_ID))[-2].content == AGAIN_TEXT

    await dp.feed_update(env["bot"], _callback_update(DrawCallback(action="again", id=999).pack()))
    assert "уже не помню" in env["session"].sent_texts()[-1]

    await dp.feed_update(env["bot"], _callback_update(data, user_id=STRANGER_ID))
    assert len(env["session"].photos()) == 2


async def test_draw_command_with_and_without_description(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("/draw закат над морем"))
    assert env["backend"].prompts == ["PROMPT(закат над морем)"]

    await dp.feed_update(env["bot"], _message_update("/draw"))
    assert "Что нарисовать?" in env["session"].sent_texts()[-1]
    await dp.feed_update(env["bot"], _message_update("уютный домик в горах"))
    assert env["backend"].prompts[-1] == "PROMPT(уютный домик в горах)"
    assert len(env["session"].photos()) == 2

    # после рисунка обычные сообщения снова идут в диалог
    await dp.feed_update(env["bot"], _message_update("спасибо!"))
    assert env["session"].sent_texts()[-1].startswith("Ответ на")


async def test_draw_command_can_be_cancelled(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("/draw"))
    await dp.feed_update(env["bot"], _message_update("/cancel"))
    await dp.feed_update(env["bot"], _message_update("привет"))
    assert env["backend"].prompts == []
    assert env["session"].sent_texts()[-1].startswith("Ответ на")


async def test_not_a_drawing_request_goes_to_the_dialog(env):
    env["provider"].decision = False
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("нарисуй мне план на неделю"))
    assert env["session"].photos() == [] and env["backend"].prompts == []
    assert env["session"].sent_texts()[-1].startswith("Ответ на")


@pytest.mark.parametrize(
    ("kind", "phrase"),
    [
        ("rejected", "отказался"),
        ("rate_limit", "лимит"),
        ("config", "ключом"),
        ("unavailable", "не отвечает"),
    ],
)
async def test_drawing_failures_are_explained(env, kind, phrase):
    env["backend"].errors = [kind]
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("нарисуй кота"))
    assert env["session"].photos() == []
    assert phrase in env["session"].sent_texts()[-1]
    assert await env["memory"].get_history(OWNER_ID) == []


async def test_daily_image_limit_is_explained(env, tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'limit.db'}")
    await init_db(engine)
    from database.models import DailyImageUsage

    usage = UsageTracker(create_session_factory(engine), 1, table=DailyImageUsage)
    drawing = DrawingService(ImageGenerator([env["backend"]], usage=usage), None, env["memory"])
    dp = env["make"](drawing)
    await dp.feed_update(env["bot"], _message_update("/draw кот"))
    await dp.feed_update(env["bot"], _message_update("/draw пёс"))
    assert len(env["session"].photos()) == 1
    assert "лимит картинок исчерпан (1)" in env["session"].sent_texts()[-1]
    await engine.dispose()


async def test_picture_rejected_as_photo_is_sent_as_file(env):
    env["session"].reject_photo = True
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("нарисуй кота"))
    photo, document = env["session"].photos()
    assert isinstance(photo, SendPhoto) and isinstance(document, SendDocument)
    assert document.caption == "🎨 Кот в шляпе"


async def test_without_image_service(env):
    off = DrawingService(ImageGenerator([]), None, env["memory"])
    dp = env["make"](off)
    await dp.feed_update(env["bot"], _message_update("/draw кот"))
    assert "Рисовать я пока не умею" in env["session"].sent_texts()[-1]
    # без сервиса картинок просьба уходит в обычный диалог (Эля честно скажет, что не умеет)
    await dp.feed_update(env["bot"], _message_update("нарисуй кота"))
    assert env["session"].sent_texts()[-1].startswith("Ответ на")
    assert env["session"].photos() == []


async def test_stranger_cannot_draw(env):
    dp = env["make"]()
    await dp.feed_update(env["bot"], _message_update("нарисуй кота", user_id=STRANGER_ID))
    await dp.feed_update(env["bot"], _message_update("/draw кот", user_id=STRANGER_ID))
    assert env["session"].requests == [] and env["backend"].prompts == []


def _voice_update(user_id: int = OWNER_ID) -> Update:
    user = User(id=user_id, is_bot=False, first_name="Аня")
    return Update(
        update_id=10_000,
        message=Message(
            message_id=10_000, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type="private"),
            from_user=user, voice=Voice(file_id="v", file_unique_id="v", duration=3),
        ),
    )


class FakeSpeech:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio, *args, **kwargs):
        return self.text


async def test_voice_request_draws_and_voice_can_answer_draw_command(env):
    async def download(file, *args, **kwargs):
        import io

        return io.BytesIO(b"OGG")

    env["bot"].download = download
    dp = env["make"](speech=FakeSpeech("нарисуй кота"))
    await dp.feed_update(env["bot"], _voice_update())
    assert env["backend"].prompts == ["PROMPT(нарисуй кота)"]

    dp = env["make"](speech=FakeSpeech("лиса в лесу"))
    await dp.feed_update(env["bot"], _message_update("/draw"))
    await dp.feed_update(env["bot"], _voice_update())
    assert env["backend"].prompts[-1] == "PROMPT(лиса в лесу)"
    assert len(env["session"].photos()) == 2

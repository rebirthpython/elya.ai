"""Монтаж в боте: настоящий Dispatcher, фейковый Telegram и модель, настоящий ffmpeg."""

import asyncio
import io
import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    SendAnimation,
    SendAudio,
    SendMessage,
    SendVideo,
    SendVideoNote,
)
from aiogram.types import (
    Animation, Audio, CallbackQuery, Chat, Document, Message, Update, User, Video, VideoNote, Voice,
)

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult, ProviderUnavailableError
from bot.keyboards.video import VideoCallback
from database.session import create_engine, create_session_factory, init_db
from main import create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.usage import UsageTracker
from tests.conftest import needs_ffmpeg
from tests.test_bot_flow import OWNER_ID, RecordingSession, _message_update, _settings
from video.ffmpeg import FFmpeg
from video.service import VideoService

MEDIA_METHODS = (SendVideo, SendAnimation, SendVideoNote, SendAudio)


class VideoSession(RecordingSession):
    def __init__(self, inbox):
        super().__init__()
        self.inbox = inbox  # сюда копируем отправленные файлы: бот удаляет их сразу после отправки

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, MEDIA_METHODS):
            self.requests.append(method)
            n = len(self.requests)
            field = {SendVideo: "video", SendAnimation: "animation", SendVideoNote: "video_note", SendAudio: "audio"}[type(method)]
            media = getattr(method, field)
            if not isinstance(media, str):
                shutil.copy(media.path, self.inbox / f"{n}{media.path.suffix}")
            ids = {"file_id": f"sent-{n}", "file_unique_id": f"u{n}"}
            payload = {
                "video": lambda: Video(**ids, width=method.width or 1, height=method.height or 1, duration=method.duration or 1),
                "animation": lambda: Animation(**ids, width=1, height=1, duration=1),
                "video_note": lambda: VideoNote(**ids, length=method.length or 1, duration=1),
                "audio": lambda: Audio(**ids, duration=1),
            }[field]()
            return Message(message_id=n, date=datetime.now(timezone.utc), chat=Chat(id=OWNER_ID, type="private"),
                           caption=getattr(method, "caption", None), **{field: payload}).as_(bot)
        if isinstance(method, EditMessageReplyMarkup):
            self.requests.append(method)
            return True
        result = await super().make_request(bot, method, timeout)
        return result.as_(bot) if hasattr(result, "as_") else result

    def media(self):
        return [m for m in self.requests if isinstance(m, MEDIA_METHODS)]

    def panels(self):
        return [m for m in self.requests if isinstance(m, SendMessage) and m.reply_markup is not None]

    def alerts(self):
        return [m.text for m in self.requests if isinstance(m, AnswerCallbackQuery) and m.text]


class Planner(BaseProvider):
    """Монтажёр по ключевым словам просьбы; на обычный чат отвечает текстом."""

    def __init__(self):
        super().__init__("fake", "Fake AI", ("m",))
        self.requests = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        system, last = messages[0].content, messages[-1].content
        if "монтажёр" not in system:
            self.requests.append(("chat", last))
            return CompletionResult(text="Ответ в чате", provider="fake", model=model)
        request = last.split("Просьба:\n<<<\n", 1)[1].split("\n>>>", 1)[0]
        self.requests.append(("plan", request))
        current = json.loads(last.split("Текущий план (JSON):\n", 1)[1].split("\n", 1)[0])
        if "ускорь" in request:
            reply = {**current, "timeline": [{**s, "speed": 2} for s in current["timeline"]]}
        elif "обрежь" in request:
            reply = {**current, "timeline": [{"clip": 1, "from": 1, "to": None}]}
        else:
            reply = {**current, "note": "Это не про монтаж 🙂"}
        return CompletionResult(text=json.dumps(reply, ensure_ascii=False), provider="fake", model=model)


_update_id = 10_000


def _media_update(kind="video", file_id="talk", caption=None, size=200_000, group=None):
    global _update_id
    _update_id += 1
    user = User(id=OWNER_ID, is_bot=False, first_name="Аня")
    media = {
        "video": lambda: {"video": Video(file_id=file_id, file_unique_id=file_id, width=320, height=240,
                                         duration=3, file_size=size, mime_type="video/mp4")},
        "video_note": lambda: {"video_note": VideoNote(file_id=file_id, file_unique_id=file_id, length=240,
                                                       duration=2, file_size=size)},
        "music": lambda: {"document": Document(file_id=file_id, file_unique_id=file_id, file_name="song.m4a",
                                               mime_type="audio/mp4", file_size=size)},
        "voice": lambda: {"voice": Voice(file_id=file_id, file_unique_id=file_id, duration=2)},
    }[kind]()
    return Update(update_id=_update_id, message=Message(
        message_id=_update_id, date=datetime.now(timezone.utc), chat=Chat(id=OWNER_ID, type="private"),
        from_user=user, caption=caption, media_group_id=group, **media,
    ))


def _button(data):
    """Нажатие кнопки. Как в настоящем Telegram, сообщение с кнопками — от бота, а не от человека."""
    global _update_id
    _update_id += 1
    user = User(id=OWNER_ID, is_bot=False, first_name="Аня")
    bot_user = User(id=42, is_bot=True, first_name="Эля")
    return Update(update_id=_update_id, callback_query=CallbackQuery(
        id=str(_update_id), from_user=user, chat_instance="ci", data=data,
        message=Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=OWNER_ID, type="private"),
                        from_user=bot_user, text="panel"),
    ))


class FakeSpeech:
    def __init__(self, text):
        self.text = text

    async def transcribe(self, audio, filename="voice.ogg", content_type="audio/ogg"):
        return self.text


@pytest.fixture
async def env(tmp_path, media_dir, monkeypatch):
    monkeypatch.setattr("bot.handlers.video.STATUS_DELAY", 0)
    # Блокировки привязываются к циклу событий, а у каждого теста он свой.
    monkeypatch.setattr("bot.handlers.video._user_locks", defaultdict(asyncio.Lock))
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await init_db(engine)
    session_factory = create_session_factory(engine)
    usage = UsageTracker(session_factory, 100)
    memory = MemoryManager(MessageRepository(session_factory), history_limit=30)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    session = VideoSession(inbox)
    bot = Bot(token="42:TEST", session=session)
    sources = {"talk": media_dir / "talk.mp4", "silent": media_dir / "silent.mp4", "song": media_dir / "music.m4a",
               "broken": None}

    async def download(file, destination=None, timeout=30, **kwargs):
        if destination is None:  # голосовое скачивается в память
            return io.BytesIO(b"OGG")
        source = sources[file.file_id]
        if source is None:
            destination.write_bytes(b"garbage")
        else:
            shutil.copy(source, destination)

    bot.download = download

    async def make(provider=None, *, with_video=True, speech=None):
        provider = provider or Planner()
        client = AIClient([provider], max_tokens=100, temperature=0.5, usage=usage)
        service = None
        if with_video:
            service = VideoService(client, FFmpeg(), workdir=tmp_path / "work", speech=speech)
            await service.start()
        dp = create_dispatcher(_settings(), client, memory, usage, video=service, speech=speech)
        return dp, provider, service

    yield {"bot": bot, "session": session, "make": make, "inbox": inbox}
    await engine.dispose()


async def _state(dp, bot):
    return await dp.fsm.get_context(bot, chat_id=OWNER_ID, user_id=OWNER_ID).get_state()


async def _rev(dp, bot):
    return (await dp.fsm.get_context(bot, chat_id=OWNER_ID, user_id=OWNER_ID).get_data())["rev"]


@needs_ffmpeg
async def test_video_in_chat_quick_button_edit_undo_and_done(env):
    dp, provider, service = await env["make"]()
    bot, session = env["bot"], env["session"]

    await dp.feed_update(bot, _media_update())
    panel = session.panels()[-1]
    assert "Давай смонтирую" in panel.text and "1) 0:03, горизонтальное" in panel.text
    buttons = [b.text for row in panel.reply_markup.inline_keyboard for b in row]
    assert "🤐 Убрать паузы" in buttons and "✖️ Выйти" in buttons and "💬 Субтитры" not in buttons
    assert await _state(dp, bot) == "VideoStates:session"

    # Кнопка — без запроса к модели.
    await dp.feed_update(bot, _button(VideoCallback(action="quick", value="pauses", rev=0).pack()))
    first = session.media()[-1]
    assert isinstance(first, SendVideo) and first.duration == 2  # 3 с минус секунда тишины
    assert "Паузы вырезаны" in first.caption and "Что поправить?" in first.caption
    assert provider.requests == []
    assert any("✅ Смонтировала" in t for t in session.sent_texts())

    # Правка словами — к текущему результату.
    await dp.feed_update(bot, _message_update("а теперь ускорь в 2 раза"))
    second = session.media()[-1]
    assert provider.requests == [("plan", "а теперь ускорь в 2 раза")]
    assert "×2" in second.caption and "Паузы вырезаны" in second.caption and "Что поправить?" not in second.caption
    dropped = [m.message_id for m in session.requests if isinstance(m, EditMessageReplyMarkup)]
    assert session.requests.index(first) + 1 in dropped  # кнопки под прошлым результатом убраны

    # Старая кнопка не срабатывает, «Отменить» возвращает прошлый вариант без нового монтажа.
    await dp.feed_update(bot, _button(VideoCallback(action="quick", value="circle", rev=1).pack()))
    assert session.alerts()[-1] == "Эта кнопка уже неактуальна."
    rendered = len(list(env["inbox"].iterdir()))
    await dp.feed_update(bot, _button(VideoCallback(action="undo", rev=2).pack()))
    again = session.media()[-1]
    assert again.video == "sent-" + str(session.requests.index(first) + 1)
    assert len(list(env["inbox"].iterdir())) == rendered

    workdir = service.workspace(OWNER_ID).root
    assert workdir.exists()
    await dp.feed_update(bot, _button(VideoCallback(action="done", rev=await _rev(dp, bot)).pack()))
    assert await _state(dp, bot) is None and not workdir.exists()
    assert "Монтаж закончен" in session.sent_texts()[-1]


@needs_ffmpeg
async def test_album_with_caption_is_edited_right_away(env, monkeypatch):
    monkeypatch.setattr("bot.handlers.video.STATUS_DELAY", 0.05)
    dp, provider, _ = await env["make"]()
    bot, session = env["bot"], env["session"]
    await dp.feed_update(bot, _message_update("/video"))
    assert "Монтаж видео" in session.sent_texts()[-1]
    await asyncio.gather(
        dp.feed_update(bot, _media_update(caption="обрежь первую секунду", group="g")),
        dp.feed_update(bot, _media_update(file_id="silent", group="g")),
    )
    assert session.panels() == []  # подпись альбома — это просьба: сразу монтаж
    assert provider.requests == [("plan", "обрежь первую секунду")]
    result = session.media()[-1]
    assert result.duration == 2 and "0:01–0:03" in result.caption


@needs_ffmpeg
async def test_circle_music_and_voice_request(env):
    dp, provider, _ = await env["make"](speech=FakeSpeech("ускорь вдвое"))
    bot, session = env["bot"], env["session"]
    await dp.feed_update(bot, _media_update(kind="video_note"))
    await dp.feed_update(bot, _media_update(kind="music", file_id="song"))
    panel = session.panels()[-1]
    assert "🎵 Музыка: «song.m4a» (0:03)" in panel.text
    buttons = [b.text for row in panel.reply_markup.inline_keyboard for b in row]
    assert "🎵 Наложить музыку" in buttons and "💬 Субтитры" in buttons  # распознавание речи настроено

    await dp.feed_update(bot, _button(VideoCallback(action="quick", value="circle", rev=0).pack()))
    note = session.media()[-1]
    assert isinstance(note, SendVideoNote) and note.length == 240
    after = session.requests[session.requests.index(note) + 1]
    assert isinstance(after, SendMessage) and "⭕ Кружок" in after.text and after.reply_markup is not None

    await dp.feed_update(bot, _media_update(kind="voice", file_id="voice"))
    assert "🎤 <i>ускорь вдвое</i>" in session.sent_texts()
    assert provider.requests[-1] == ("plan", "ускорь вдвое")


@needs_ffmpeg
async def test_not_about_editing_gets_a_note_and_cancel_cleans_up(env):
    dp, provider, service = await env["make"]()
    bot, session = env["bot"], env["session"]
    await dp.feed_update(bot, _media_update())
    await dp.feed_update(bot, _message_update("как дела?"))
    assert "Это не про монтаж" in session.sent_texts()[-1] and session.media() == []

    await dp.feed_update(bot, _message_update("/cancel"))
    assert session.sent_texts()[-1] == "Монтаж отменён, файлы удалила."
    assert not service.workspace(OWNER_ID).root.exists()
    await dp.feed_update(bot, _message_update("как дела?"))
    assert provider.requests[-1] == ("chat", "как дела?")  # снова обычный чат


@needs_ffmpeg
async def test_errors_big_broken_and_ai_down(env):
    class Down(BaseProvider):
        def __init__(self):
            super().__init__("down", "Down", ("m",))

        async def complete(self, messages, *, model, max_tokens, temperature):
            raise ProviderUnavailableError("timeout", provider="down", model=model)

    dp, _, _ = await env["make"](Down())
    bot, session = env["bot"], env["session"]
    await dp.feed_update(bot, _media_update(size=30 * 1024 * 1024))
    assert "больше 20 МБ" in session.sent_texts()[-1]
    await dp.feed_update(bot, _media_update(file_id="broken"))
    assert "повреждён" in session.sent_texts()[-1]
    assert await _state(dp, bot) is None  # битое видео в чат не начинает пустой монтаж

    await dp.feed_update(bot, _message_update("/video"))
    await dp.feed_update(bot, _message_update("обрежь"))
    assert "Сначала пришли видео" in session.sent_texts()[-1]

    await dp.feed_update(bot, _media_update())
    await dp.feed_update(bot, _message_update("обрежь первую секунду"))
    assert "не отвечает" in session.sent_texts()[-1]
    assert await _state(dp, bot) == "VideoStates:session"  # можно попросить ещё раз


async def test_without_ffmpeg_the_bot_explains(env):
    dp, _, _ = await env["make"](with_video=False)
    bot, session = env["bot"], env["session"]
    await dp.feed_update(bot, _message_update("/video"))
    assert "нет ffmpeg" in session.sent_texts()[-1]
    await dp.feed_update(bot, _media_update())
    assert "pkg install ffmpeg" in session.sent_texts()[-1]
    assert await _state(dp, bot) is None


@needs_ffmpeg
async def test_album_sent_straight_to_chat_makes_one_session(env, monkeypatch):
    monkeypatch.setattr("bot.handlers.video.STATUS_DELAY", 0.05)
    dp, _, service = await env["make"]()
    bot, session = env["bot"], env["session"]
    await asyncio.gather(
        dp.feed_update(bot, _media_update(group="a")),
        dp.feed_update(bot, _media_update(file_id="silent", group="a")),
    )
    assert len(session.panels()) == 1
    panel = session.panels()[0]
    assert "1) 0:03" in panel.text and "2) 0:02, вертикальное, без звука" in panel.text
    buttons = [b.text for row in panel.reply_markup.inline_keyboard for b in row]
    assert "🔗 Просто склеить" in buttons
    assert len(list(service.workspace(OWNER_ID).root.glob("clip-*"))) == 2

    await dp.feed_update(bot, _button(VideoCallback(action="quick", value="join", rev=0).pack()))
    result = session.media()[-1]
    assert result.duration == 5 and "Склеены клипы 1–2" in result.caption

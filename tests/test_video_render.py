"""Монтаж настоящим ffmpeg на маленьких сгенерированных клипах и план от фейковой модели."""

import json
import shutil

import pytest

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult
from services.speech import SpeechError, Timed, TimedTranscript
from tests.conftest import needs_ffmpeg
from video.ffmpeg import FFmpeg
from video.plan import Segment, TextOverlay, default_plan
from video.service import PlanError, VideoError, VideoService

pytestmark = needs_ffmpeg


class FakeSpeech:
    def __init__(self, transcript=None, error=None):
        self.transcript = transcript or TimedTranscript(
            "Привет, это тест", words=[Timed(0.1, 0.5, "Привет,"), Timed(0.55, 0.9, "это"), Timed(2.0, 2.4, "тест")]
        )
        self.error = error
        self.calls = []

    async def transcribe_timed(self, audio, filename="audio.m4a", content_type="audio/mp4"):
        self.calls.append((len(audio), filename))
        if self.error:
            raise self.error
        return self.transcript


class PlannerAI(BaseProvider):
    def __init__(self, replies):
        super().__init__("fake", "Fake AI", ("m",))
        self.replies = list(replies)
        self.requests = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.requests.append([m.content for m in messages])
        reply = self.replies.pop(0)
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return CompletionResult(text=text, provider="fake", model=model)


@pytest.fixture
async def setup(tmp_path, media_dir):
    async def make(replies=(), speech=None):
        ai = AIClient([PlannerAI(replies)], max_tokens=100, temperature=0.2) if replies else None
        service = VideoService(ai, FFmpeg(), workdir=tmp_path / "work", speech=speech, max_seconds=600)
        assert (await service.start()) is not None and service.ready
        ws = service.workspace(1).ensure()
        for name in ("talk.mp4", "silent.mp4", "music.m4a"):
            shutil.copy(media_dir / name, ws.path(name))
        clips = [await service.probe(ws.path("talk.mp4")), await service.probe(ws.path("silent.mp4"))]
        music = await service.probe(ws.path("music.m4a"))
        return service, ws, clips, music

    return make


async def _render(service, ws, plan, clips, music=None):
    stages = []

    async def progress(text):
        stages.append(text)

    result = await service.render(ws, plan, clips, music=music, progress=progress)
    info = await service.probe(result.path)
    return result, info, stages


async def test_join_and_remove_pauses(setup):
    service, ws, clips, _ = await setup()
    result, info, stages = await _render(service, ws, default_plan(clips), clips)
    assert result.kind == "video" and info.duration == pytest.approx(5.0, abs=0.1)
    assert (info.width, info.height) == (320, 240) and info.has_audio
    assert result.lines == ["🔗 Склеены клипы 1–2"]
    assert any("Готовлю фрагменты" in s for s in stages)

    result, info, stages = await _render(service, ws, default_plan(clips[:1]).but(remove_pauses=True), clips)
    # Тишина 1–2 с вырезана, по краям речи оставлен запас 0,15 с.
    assert info.duration == pytest.approx(2.3, abs=0.1)
    assert any("Паузы вырезаны" in line for line in result.lines)
    assert "🤐 Ищу паузы…" in stages
    assert not list(ws.root.glob("piece-*"))  # промежуточные куски убраны


async def test_vertical_text_filters_music(setup):
    service, ws, clips, music = await setup()
    plan = default_plan(clips).but(
        format="9:16", texts=(TextOverlay("Лето 2026", 0, 2, "top"),), filter="vivid", fade=True,
        transitions=True, music=True, music_volume=0.5, clean_audio=True,
        timeline=(Segment(1, 0.5, 3.0, 2.0), Segment(2, 0, 2, 1)),
    )
    result, info, _ = await _render(service, ws, plan, clips, music)
    assert (info.width, info.height) == (240, 426) and info.has_audio
    assert info.duration == pytest.approx(1.25 + 2.0, abs=0.1)
    assert "Лето 2026" in ws.path("captions.ass").read_text(encoding="utf-8")
    assert any("Музыка" in line for line in result.lines)


async def test_gif_circle_and_audio_outputs(setup):
    service, ws, clips, _ = await setup()
    result, info, _ = await _render(service, ws, default_plan(clips).but(output="gif"), clips)
    assert result.kind == "animation" and not info.has_audio
    result, info, _ = await _render(service, ws, default_plan(clips).but(output="circle", format="1:1"), clips)
    assert result.kind == "video_note" and info.width == info.height == 240
    result, info, _ = await _render(service, ws, default_plan(clips).but(output="audio"), clips)
    assert result.kind == "audio" and result.path.suffix == ".m4a" and not info.has_video
    with pytest.raises(VideoError, match="нет звука"):
        await service.render(ws, default_plan(clips[1:2]).but(timeline=(Segment(2, 0, 2),), output="audio"), clips)


async def test_subtitles_are_recognised_from_the_edited_sound_once(setup):
    speech = FakeSpeech()
    service, ws, clips, _ = await setup(speech=speech)
    plan = default_plan(clips).but(subtitles=True)
    result, _, stages = await _render(service, ws, plan, clips)
    ass = ws.path("captions.ass").read_text(encoding="utf-8")
    assert "Привет, это" in ass and "тест" in ass
    assert "💬 Субтитры из речи" in result.lines and "💬 Распознаю речь для субтитров…" in stages
    await _render(service, ws, plan.but(filter="bw"), clips)
    assert len(speech.calls) == 1  # те же куски — та же расшифровка из кэша


async def test_subtitles_failure_is_explained(setup):
    service, ws, clips, _ = await setup(speech=FakeSpeech(error=SpeechError("HTTP 429")))
    result, _, _ = await _render(service, ws, default_plan(clips).but(subtitles=True), clips)
    assert any("Не получилось распознать речь" in n for n in result.notes)


async def test_planner_repairs_mistakes(setup):
    service, ws, clips, _ = await setup(replies=[
        {"timeline": [{"clip": 7}]},
        {"timeline": [{"clip": 1, "from": "0:01", "to": None}], "format": "9:16"},
    ])
    outcome = await service.plan("обрежь первую секунду и сделай вертикально", clips, None, ws=ws)
    assert outcome.changed and outcome.plan.timeline == (Segment(1, 1.0, 3.0),) and outcome.plan.format == "9:16"
    provider = service._ai._providers[0]
    assert "клипа 7 нет" in provider.requests[1][-1]
    assert "Просьба:\n<<<\nобрежь первую секунду" in provider.requests[0][1]


async def test_planner_listens_when_it_needs_the_words(setup):
    speech = FakeSpeech(TimedTranscript("Привет", segments=[Timed(0.0, 0.9, "Привет")]))
    service, ws, clips, _ = await setup(speech=speech, replies=[
        {"need_transcript": True},
        {"timeline": [{"clip": 1, "from": 0, "to": 0.9}]},
    ])
    stages = []

    async def progress(text):
        stages.append(text)

    outcome = await service.plan("оставь только приветствие", clips, None, ws=ws, progress=progress)
    assert outcome.plan.timeline == (Segment(1, 0.0, 0.9),)
    assert len(speech.calls) == 1  # второй клип без звука — его не слушаем
    second_request = service._ai._providers[0].requests[1][1]
    assert "[0:00–0:00.9] Привет" in second_request and "Клип 2:\n  (речи нет)" in second_request
    assert stages == ["👂 Слушаю, что говорят в видео…"]


async def test_planner_without_speech_explains_and_keeps_plan(setup):
    service, ws, clips, _ = await setup(replies=[{"need_transcript": True}])
    current = default_plan(clips).but(fade=True)
    outcome = await service.plan("вырежи момент, где я чихаю", clips, current, ws=ws)
    assert not outcome.changed and outcome.plan == current and "GROQ_API_KEY" in outcome.notes[0]


async def test_planner_gives_up_on_garbage(setup):
    service, ws, clips, _ = await setup(replies=["не JSON", "опять не JSON"])
    with pytest.raises(PlanError):
        await service.plan("сделай красиво", clips, None, ws=ws)


async def test_note_and_unchanged_plan(setup):
    service, ws, clips, _ = await setup(replies=[{"note": "Убрать человека из кадра я не умею."}])
    current = default_plan(clips).but(remove_pauses=True)
    outcome = await service.plan("убери человека на фоне", clips, current, ws=ws)
    assert not outcome.changed and outcome.notes == ["💭 Убрать человека из кадра я не умею."]


async def test_broken_file_and_missing_ffmpeg(tmp_path, setup):
    service, ws, _, _ = await setup()
    ws.path("broken.mp4").write_bytes(b"not a video at all")
    with pytest.raises(VideoError, match="повреждён"):
        await service.probe(ws.path("broken.mp4"))

    stale = tmp_path / "work2" / "123"
    stale.mkdir(parents=True)
    (tmp_path / "work2" / "keep-me").mkdir()
    missing = VideoService(None, FFmpeg("no-such-ffmpeg", "no-such-ffprobe"), workdir=tmp_path / "work2")
    assert await missing.start() is None and not missing.ready
    assert "ffmpeg" in missing.unavailable_text and missing.status.startswith("off")
    assert not stale.exists() and (tmp_path / "work2" / "keep-me").exists()  # стираем только свои папки

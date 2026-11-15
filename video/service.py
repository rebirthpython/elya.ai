"""Монтаж целиком: рабочая папка, план от модели (с исправлением ошибок), сборка через ffmpeg."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai.client import AIClient
from ai.providers.base import ChatMessage
from services.speech import SpeechError, SpeechToText, TimedTranscript
from study.jsonutil import ModelOutputError, extract_json_object
from study.prompts import repair_prompt
from video import prompts
from video.captions import build_ass, build_cues, caption_chars
from video.ffmpeg import Capabilities, ClipInfo, FFmpeg, FFmpegError
from video.filters import TELEGRAM_UPLOAD_LIMIT, concat_list, final_args, piece_args, speech_audio_args
from video.plan import EditPlan, ParsedPlan, PlanContext, default_plan, describe, fmt_time, parse_plan
from video.timeline import MAX_PIECES, Piece, build_pieces, canvas_size, output_fps, speech_intervals

logger = logging.getLogger(__name__)

MAX_REPAIRS = 1
PLAN_MAX_TOKENS = 2048
SPEECH_MAX_BYTES = 24 * 1024 * 1024  # бесплатный Whisper в Groq принимает файлы до 25 МБ
Progress = Callable[[str], Awaitable[None]]

NO_FFMPEG_TEXT = (
    "🎬 Монтаж пока не настроен: на сервере нет ffmpeg.\n"
    "Владельцу бота: <code>pkg install ffmpeg</code> в Termux или "
    "<code>sudo apt install ffmpeg fonts-dejavu-core</code> на сервере, потом перезапусти бота."
)


class VideoError(Exception):
    """Монтаж не удался по понятной человеку причине: текст можно показать в чате."""


class PlanError(Exception):
    """Модель так и не выдала пригодный план."""


@dataclass
class PlanOutcome:
    plan: EditPlan
    notes: list[str] = field(default_factory=list)
    changed: bool = True


@dataclass
class RenderResult:
    path: Path
    kind: str  # video | animation | video_note | audio
    duration: float
    width: int
    height: int
    lines: list[str]  # что сделано — для подписи
    notes: list[str]  # пояснения: что-то обрезано, речь не распознана и т. п.


class Workspace:
    """Папка с клипами одного человека. Всё в ней временное: она стирается в конце монтажа."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, name: str) -> Path:
        return self.root / name

    def ensure(self) -> Workspace:
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    def clear(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def load_json(self, name: str) -> Any:
        try:
            return json.loads(self.path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save_json(self, name: str, data: Any) -> None:
        self.path(name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class VideoService:
    def __init__(
        self,
        ai_client: AIClient | None,
        ffmpeg: FFmpeg,
        *,
        workdir: Path,
        speech: SpeechToText | None = None,
        assistant_name: str = "Эля",
        max_clips: int = 10,
        max_seconds: float = 600.0,
        max_side: int = 1920,
    ) -> None:
        self._ai = ai_client
        self._ffmpeg = ffmpeg
        self._workdir = workdir
        self._speech = speech
        self._name = assistant_name
        self.max_clips = max_clips
        self.max_seconds = max_seconds
        self.max_side = max_side
        self.caps: Capabilities | None = None
        # Один монтаж за раз: на телефоне или маленьком сервере два ffmpeg сразу — слишком тяжело.
        self._render_lock = asyncio.Lock()

    # ---------- Запуск ----------

    async def start(self) -> Capabilities | None:
        """Проверяет ffmpeg и стирает папки прошлых запусков (сессии монтажа живут в памяти бота)."""
        self._cleanup_stale()
        if not self._ffmpeg.available:
            return None
        try:
            self.caps = await self._ffmpeg.capabilities()
        except (FFmpegError, OSError) as exc:
            logger.warning("ffmpeg check failed: %s", exc)
            self.caps = None
        return self.caps

    def _cleanup_stale(self) -> None:
        if not self._workdir.is_dir():
            return
        for child in self._workdir.iterdir():
            if child.is_dir() and child.name.isdigit():  # чужого в папке не трогаем
                shutil.rmtree(child, ignore_errors=True)

    @property
    def ready(self) -> bool:
        return self.caps is not None and self.caps.can_render

    @property
    def status(self) -> str:
        if self.caps is None:
            return "off (ffmpeg not found)"
        if not self.caps.can_render:
            return f"off ({self.caps.version}: no libx264/aac)"
        extras = [name for name, ok in (("text", self.caps.ass), ("subtitles", self.caps.ass and self.can_transcribe)) if ok]
        return f"on ({self.caps.version}; {', '.join(extras) or 'basic'})"

    @property
    def unavailable_text(self) -> str:
        if self.caps is not None and not self.caps.can_render:
            return ("🎬 Монтаж недоступен: ffmpeg на сервере собран без кодеков H.264 и AAC. "
                    "Поставь полную сборку ffmpeg и перезапусти бота.")
        return NO_FFMPEG_TEXT

    @property
    def can_transcribe(self) -> bool:
        return self._speech is not None

    @property
    def can_subtitles(self) -> bool:
        return self.can_transcribe and bool(self.caps and self.caps.ass)

    def workspace(self, user_id: int) -> Workspace:
        return Workspace(self._workdir / str(user_id))

    def context(self, clips: list[ClipInfo], *, has_music: bool) -> PlanContext:
        caps = self.caps or Capabilities("ffmpeg")
        if not self.can_transcribe:
            reason = "владельцу бота нужно добавить ключ GROQ_API_KEY (бесплатно на console.groq.com)"
        else:
            reason = "ffmpeg на сервере собран без libass"
        return PlanContext(
            clips=clips, has_music=has_music, can_text=caps.ass, can_subtitles=self.can_subtitles,
            subtitles_reason=reason, can_stabilize=caps.deshake, can_clean_audio=caps.afftdn,
        )

    async def probe(self, path: Path) -> ClipInfo:
        try:
            return await self._ffmpeg.probe(path)
        except FFmpegError as exc:
            logger.info("Cannot probe %s: %s", path.name, exc)
            raise VideoError("Не получилось открыть файл: похоже, он повреждён или это не видео.") from exc

    # ---------- План от модели ----------

    async def plan(
        self,
        request: str,
        clips: list[ClipInfo],
        current: EditPlan | None,
        *,
        ws: Workspace,
        music: ClipInfo | None = None,
        history: list[str] = (),
        progress: Progress | None = None,
    ) -> PlanOutcome:
        if self._ai is None:
            raise PlanError("no AI client")
        base = current or default_plan(clips)
        ctx = self.context(clips, has_music=music is not None)
        transcripts: dict[int, TimedTranscript] | None = None
        while True:
            parsed = await self._ask_plan(request, clips, base, ctx, music, history, transcripts)
            if not parsed.need_transcript or transcripts is not None:
                break
            if not self.can_transcribe:
                return PlanOutcome(base, [
                    "👂 Чтобы искать моменты по тому, что говорят в видео, мне нужно распознавание речи: "
                    "владельцу бота нужно добавить ключ GROQ_API_KEY. А по времени («с 0:10 до 0:25») "
                    "смонтирую и так."
                ], changed=False)
            if progress:
                await progress("👂 Слушаю, что говорят в видео…")
            transcripts = await self._clip_transcripts(ws, clips)

        notes = list(parsed.notes)
        if parsed.note:
            notes.insert(0, f"💭 {parsed.note}")
        return PlanOutcome(parsed.plan, notes, changed=parsed.plan.key != base.key)

    async def _ask_plan(
        self,
        request: str,
        clips: list[ClipInfo],
        base: EditPlan,
        ctx: PlanContext,
        music: ClipInfo | None,
        history: list[str],
        transcripts: dict[int, TimedTranscript] | None,
    ) -> ParsedPlan:
        conversation = [
            ChatMessage(role="system", content=prompts.planner_prompt(self._name)),
            ChatMessage(role="user", content=prompts.planner_request(
                request, clips, base, has_music=music is not None,
                music_duration=music.duration if music else None, can_subtitles=self.can_subtitles,
                history=history, transcripts=transcripts,
            )),
        ]
        best: ParsedPlan | None = None
        for attempt in range(MAX_REPAIRS + 1):
            result = await self._ai.complete(conversation, max_tokens=PLAN_MAX_TOKENS, temperature=0.2)
            try:
                parsed = parse_plan(extract_json_object(result.text), ctx, base)
            except ModelOutputError as exc:
                issues = [f"Ответ не удалось разобрать: {exc}. Нужен JSON строго по схеме."]
            else:
                best = parsed
                if not parsed.issues:
                    return parsed
                issues = parsed.issues
            logger.info("Video plan: attempt %d has %d issue(s)", attempt + 1, len(issues))
            if attempt < MAX_REPAIRS:
                conversation += [
                    ChatMessage(role="assistant", content=result.text),
                    ChatMessage(role="user", content=repair_prompt(issues)),
                ]
        if best is None:
            raise PlanError("model returned no usable plan")
        return best  # с замечаниями: неверное просто не применилось

    # ---------- Распознавание речи ----------

    async def _transcribe_file(self, ws: Workspace, source: str, *, concat: bool, cache: str) -> TimedTranscript:
        if (cached := ws.load_json(cache)) is not None:
            return TimedTranscript.from_dict(cached)
        if self._speech is None:
            raise SpeechError("speech recognition is not configured")
        audio = ws.path(cache + ".m4a")
        try:
            await self._ffmpeg.run(speech_audio_args(source, audio.name, concat=concat), cwd=ws.root)
            data = audio.read_bytes()
        finally:
            audio.unlink(missing_ok=True)
        if len(data) > SPEECH_MAX_BYTES:
            raise SpeechError("audio is too long for speech recognition")
        transcript = await self._speech.transcribe_timed(data, "audio.m4a", "audio/mp4")
        ws.save_json(cache, transcript.to_dict())
        return transcript

    async def _clip_transcripts(self, ws: Workspace, clips: list[ClipInfo]) -> dict[int, TimedTranscript]:
        result: dict[int, TimedTranscript] = {}
        for number, clip in enumerate(clips, start=1):
            if not clip.has_audio:
                result[number] = TimedTranscript(text="")
                continue
            try:
                result[number] = await self._transcribe_file(
                    ws, clip.file, concat=False, cache=f"transcript-{clip.file}.json"
                )
            except (SpeechError, FFmpegError) as exc:
                logger.warning("Clip transcription failed: %s", exc)
                result[number] = TimedTranscript(text="")
        return result

    # ---------- Монтаж ----------

    async def _speech_map(self, ws: Workspace, plan: EditPlan, clips: list[ClipInfo]) -> dict[int, list]:
        """Тишина ищется один раз на клип и запоминается в рабочей папке."""
        silences: dict[int, list[tuple[float, float]]] = {}
        for number in sorted({s.clip for s in plan.timeline}):
            clip = clips[number - 1]
            if not clip.has_audio:
                continue
            cache = f"silence-{clip.file}.json"
            cached = ws.load_json(cache)
            if cached is None:
                cached = await self._ffmpeg.detect_silence(ws.path(clip.file), clip.duration)
                ws.save_json(cache, cached)
            silences[number] = [(float(a), float(b)) for a, b in cached]
        return silences

    def _pieces(self, plan: EditPlan, clips: list[ClipInfo], fps: int,
                silences: dict[int, list] | None) -> tuple[list[Piece], bool]:
        if not plan.remove_pauses or not silences:
            return build_pieces(plan, clips, fps, max_seconds=self.max_seconds)
        pieces, trimmed = [], False
        # Слишком много коротких кусков — склеиваем соседние фразы, разделённые короткими паузами.
        for join_gap in (0.3, 0.6, 1.0, 2.0, 4.0):
            speech = {n: speech_intervals(s, clips[n - 1].duration, join_gap=join_gap) for n, s in silences.items()}
            pieces, trimmed = build_pieces(plan, clips, fps, speech=speech, max_seconds=self.max_seconds)
            if len(pieces) <= MAX_PIECES:
                break
        return pieces, trimmed

    async def render(
        self,
        ws: Workspace,
        plan: EditPlan,
        clips: list[ClipInfo],
        *,
        music: ClipInfo | None = None,
        progress: Progress | None = None,
    ) -> RenderResult:
        if not self.ready:
            raise VideoError(self.unavailable_text)

        async def report(text: str) -> None:
            if progress is not None:
                await progress(text)

        if self._render_lock.locked():
            await report("⏳ Жду очереди: сейчас монтируется другое видео…")
        async with self._render_lock:
            started = time.monotonic()
            try:
                return await self._render(ws, plan, clips, music, report)
            finally:
                for leftover in ws.root.glob("piece-*.mkv"):
                    leftover.unlink(missing_ok=True)
                logger.info("Video render took %.1f s", time.monotonic() - started)

    async def _render(self, ws: Workspace, plan: EditPlan, clips: list[ClipInfo],
                      music: ClipInfo | None, report: Progress) -> RenderResult:
        notes: list[str] = []
        if plan.output == "audio" and not (plan.volume > 0 and any(clips[s.clip - 1].has_audio for s in plan.timeline)) \
                and not (plan.music and music):
            raise VideoError("🔇 В этом видео нет звука — сохранить только звук не получится.")
        for old in ws.root.glob("result-*"):
            old.unlink(missing_ok=True)

        fps = output_fps(plan, clips)
        canvas = canvas_size(plan, clips, self.max_side)
        silences = None
        if plan.remove_pauses:
            await report("🤐 Ищу паузы…")
            silences = await self._speech_map(ws, plan, clips)
        pieces, trimmed = self._pieces(plan, clips, fps, silences)
        if not pieces:
            raise VideoError("Получилось пустое видео: выбранные фрагменты слишком короткие.")
        total = sum(p.duration for p in pieces)
        if trimmed:
            limit = 60 if plan.output == "circle" else self.max_seconds
            notes.append(f"⏱ Видео длиннее {fmt_time(limit)} — оставила первые {fmt_time(limit)}.")
        full = sum(s.duration for s in plan.timeline)
        removed = max(0.0, full - total) if plan.remove_pauses and not trimmed else 0.0

        files = []
        for index, piece in enumerate(pieces, start=1):
            if len(pieces) > 1:
                await report(f"🎞 Готовлю фрагменты: {index} из {len(pieces)}")
            name = f"piece-{index:03d}.mkv"
            await self._ffmpeg.run(piece_args(piece, clips[piece.clip - 1], plan, canvas, fps, name), cwd=ws.root)
            files.append(name)
        pieces_list = "pieces.txt"
        ws.path(pieces_list).write_text(concat_list(files), encoding="utf-8")

        captions = None
        if plan.output != "audio" and (plan.subtitles or plan.texts):
            cues = []
            if plan.subtitles:
                await report("💬 Распознаю речь для субтитров…")
                cues = await self._subtitles(ws, plan, pieces, canvas, total, pieces_list, notes)
            if cues or plan.texts:
                captions = "captions.ass"
                ws.path(captions).write_text(
                    build_ass(*canvas, cues=cues, texts=list(plan.texts), total=total), encoding="utf-8"
                )

        await report("🎬 Собираю видео…" if plan.output != "audio" else "🎧 Сохраняю звук…")
        ext = ".m4a" if plan.output == "audio" else ".mp4"
        output = f"result-{int(time.time() * 1000)}{ext}"
        await self._ffmpeg.run(
            final_args(plan, total, pieces_list=pieces_list, output=output,
                       music=music.file if (music and plan.music) else None, captions=captions),
            cwd=ws.root,
        )
        path = ws.path(output)
        if not path.is_file() or path.stat().st_size == 0:
            raise VideoError("😕 ffmpeg не выдал результат. Попробуй ещё раз.")
        if path.stat().st_size > TELEGRAM_UPLOAD_LIMIT:
            path.unlink(missing_ok=True)
            raise VideoError("Результат больше 50 МБ — Telegram не даст его отправить. "
                             "Сделай видео короче или выбери формат поменьше.")
        kind = {"gif": "animation", "circle": "video_note", "audio": "audio"}.get(plan.output, "video")
        return RenderResult(
            path=path, kind=kind, duration=total, width=canvas[0], height=canvas[1],
            lines=describe(plan, clips, removed_pauses=removed), notes=notes,
        )

    async def poster(self, ws: Workspace, name: str, *, width: int = 480) -> bytes | None:
        """Кадр из середины результата (JPEG) — обложка для просмотра в приложении."""
        path = ws.path(name)
        if not path.is_file() or path.suffix != ".mp4":
            return None
        try:
            info = await self._ffmpeg.probe(path)
            out = ws.path("poster.jpg")
            await self._ffmpeg.run([
                "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                "-ss", f"{min(1.0, info.duration / 2):.2f}", "-i", name, "-frames:v", "1",
                "-vf", f"scale='min({width},iw)':-2", "-q:v", "5", out.name,
            ], cwd=ws.root, timeout=60)
            return out.read_bytes()
        except (FFmpegError, OSError) as exc:
            logger.info("Poster failed: %s", exc)
            return None
        finally:
            ws.path("poster.jpg").unlink(missing_ok=True)

    async def _subtitles(self, ws: Workspace, plan: EditPlan, pieces: list[Piece], canvas: tuple[int, int],
                         total: float, pieces_list: str, notes: list[str]) -> list:
        # Распознаём уже смонтированный звук: так время субтитров точно совпадает с видео.
        key = hashlib.sha1(json.dumps([p.__dict__ for p in pieces], sort_keys=True).encode()).hexdigest()[:16]
        try:
            transcript = await self._transcribe_file(ws, pieces_list, concat=True, cache=f"subs-{key}.json")
        except (SpeechError, FFmpegError) as exc:
            logger.warning("Subtitles transcription failed: %s", exc)
            notes.append("💬 Не получилось распознать речь для субтитров — смонтировала без них.")
            return []
        cues = build_cues(transcript, max_chars=caption_chars(*canvas), total=total)
        if not cues:
            notes.append("💬 Не расслышала в видео речи — субтитров не будет.")
        return cues

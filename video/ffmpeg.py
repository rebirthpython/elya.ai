"""Запуск ffmpeg и ffprobe: сведения о клипе, поиск тишины, проверка возможностей сборки."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


class FFmpegError(Exception):
    """ffmpeg не справился: битый файл, нехватка места, таймаут. Текст — для логов, не для людей."""


@dataclass(frozen=True)
class ClipInfo:
    """Клип в рабочей папке. Ширина и высота — как видно на экране (с учётом поворота телефона)."""

    file: str
    duration: float
    width: int
    height: int
    fps: float = 30.0
    has_audio: bool = True
    has_video: bool = True
    vstream: int = 0  # номер видеодорожки среди видеодорожек файла

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClipInfo:
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def orientation(self) -> str:
        if self.width == self.height:
            return "квадратное"
        return "вертикальное" if self.height > self.width else "горизонтальное"


@dataclass(frozen=True)
class Capabilities:
    """Что умеет установленный ffmpeg. Сборки бывают разные (Termux, Debian, Homebrew)."""

    version: str
    x264: bool = False
    aac: bool = False
    ass: bool = False  # надписи и субтитры (libass)
    deshake: bool = False
    afftdn: bool = False  # шумоподавление

    @property
    def can_render(self) -> bool:
        return self.x264 and self.aac


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and number not in (float("inf"), float("-inf")) else default


def _frame_rate(value: Any) -> float:
    """«30000/1001» -> 29.97; «0/0» и мусор -> 0."""
    if not isinstance(value, str) or not value:
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        denominator = _number(den)
        return _number(num) / denominator if denominator else 0.0
    return _number(value)


def _rotation(stream: dict[str, Any]) -> int:
    for item in stream.get("side_data_list") or []:
        if isinstance(item, dict) and "rotation" in item:
            return int(_number(item["rotation"]))
    return int(_number((stream.get("tags") or {}).get("rotate")))


def parse_probe(data: dict[str, Any], file: str) -> ClipInfo:
    """Разбирает JSON от ffprobe. Ошибка — если в файле нет ни видео, ни звука."""
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audio = any(s.get("codec_type") == "audio" for s in streams)
    fmt = data.get("format") or {}

    # Обложка в mp4/mp3 — тоже «видеодорожка», но из одного кадра.
    real = [(i, s) for i, s in enumerate(videos) if not (s.get("disposition") or {}).get("attached_pic")]
    if not real and not audio:
        raise FFmpegError("no audio or video streams")

    duration = _number(fmt.get("duration"))
    if not real:
        if duration <= 0:
            duration = max((_number(s.get("duration")) for s in streams), default=0.0)
        return ClipInfo(file=file, duration=duration, width=0, height=0, fps=0.0,
                        has_audio=True, has_video=False)

    index, stream = real[0]
    width, height = int(_number(stream.get("width"))), int(_number(stream.get("height")))
    if width <= 0 or height <= 0:
        raise FFmpegError("video stream without size")
    if abs(_rotation(stream)) % 180 == 90:
        width, height = height, width
    if duration <= 0:
        duration = _number(stream.get("duration"))
    fps = _frame_rate(stream.get("avg_frame_rate")) or _frame_rate(stream.get("r_frame_rate"))
    if not 1 <= fps <= 240:
        fps = 30.0
    return ClipInfo(file=file, duration=duration, width=width, height=height, fps=round(fps, 3),
                    has_audio=audio, has_video=True, vstream=index)


def parse_silence(log: str, duration: float) -> list[tuple[float, float]]:
    """Интервалы тишины из вывода фильтра silencedetect."""
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in log.splitlines():
        if (match := _SILENCE_START_RE.search(line)) is not None:
            start = max(0.0, float(match.group(1)))
        elif (match := _SILENCE_END_RE.search(line)) is not None and start is not None:
            silences.append((start, min(duration, float(match.group(1)))))
            start = None
    if start is not None and start < duration:  # тишина до самого конца
        silences.append((start, duration))
    return [(a, b) for a, b in silences if b > a]


def _lower_priority() -> None:  # pragma: no cover - выполняется в дочернем процессе
    try:
        os.nice(10)
    except OSError:
        pass


class FFmpeg:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", *, timeout: float = 900.0) -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._timeout = timeout

    @property
    def available(self) -> bool:
        return shutil.which(self._ffmpeg) is not None and shutil.which(self._ffprobe) is not None

    async def _exec(self, binary: str, args: list[str], *, cwd: Path | None, timeout: float | None) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                binary, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                # Монтаж не должен мешать боту отвечать: ffmpeg работает с пониженным приоритетом.
                preexec_fn=_lower_priority if os.name == "posix" else None,
            )
        except OSError as exc:
            raise FFmpegError(f"cannot start {binary}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout or self._timeout)
        except BaseException:  # таймаут или отмена задачи: не оставляем ffmpeg работать впустую
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def run(self, args: list[str], *, cwd: Path | None = None, timeout: float | None = None) -> str:
        """Запускает ffmpeg и возвращает его лог (stderr)."""
        try:
            code, _, log = await self._exec(self._ffmpeg, args, cwd=cwd, timeout=timeout)
        except TimeoutError as exc:
            raise FFmpegError("ffmpeg timed out") from exc
        if code != 0:
            raise FFmpegError(f"ffmpeg exited with {code}: {log.strip()[-1500:]}")
        return log

    async def probe(self, path: Path) -> ClipInfo:
        args = ["-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
        try:
            code, out, log = await self._exec(self._ffprobe, args, cwd=None, timeout=60)
        except TimeoutError as exc:
            raise FFmpegError("ffprobe timed out") from exc
        if code != 0:
            raise FFmpegError(f"ffprobe exited with {code}: {log.strip()[-500:]}")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise FFmpegError("ffprobe returned bad JSON") from exc
        return parse_probe(data if isinstance(data, dict) else {}, path.name)

    async def detect_silence(
        self, path: Path, duration: float, *, noise_db: int = -30, min_silence: float = 0.6
    ) -> list[tuple[float, float]]:
        log = await self.run([
            "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-map", "0:a:0", "-vn",
            "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-",
        ])
        return parse_silence(log, duration)

    async def capabilities(self) -> Capabilities:
        async def listing(flag: str) -> str:
            try:
                _, out, _ = await self._exec(self._ffmpeg, ["-hide_banner", flag], cwd=None, timeout=30)
            except TimeoutError:
                return ""
            return out

        version_out, encoders, filters = await listing("-version"), await listing("-encoders"), await listing("-filters")
        match = re.search(r"ffmpeg version (\S+)", version_out)

        def has(listing: str, name: str) -> bool:
            return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", listing, re.MULTILINE) is not None

        return Capabilities(
            version=f"ffmpeg {match.group(1)}" if match else "ffmpeg",
            x264=has(encoders, "libx264"),
            aac=has(encoders, "aac"),
            ass=has(filters, "ass"),
            deshake=has(filters, "deshake"),
            afftdn=has(filters, "afftdn"),
        )

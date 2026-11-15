"""Общие фикстуры. Клипы для тестов монтажа генерирует сам ffmpeg (если он установлен)."""

import shutil
import subprocess
from pathlib import Path

import pytest

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, timeout=60)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    """talk.mp4 — 3 с, 320×240, звук с тишиной на 1–2 с; silent.mp4 — 2 с, 180×320, без звука;
    music.m4a — 3 с мелодии."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg is not installed")
    folder = tmp_path_factory.mktemp("media")
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=48000",
            "-filter_complex", "[1:a]volume=enable='between(t,1,2)':volume=0[a]",
            "-map", "0:v", "-map", "[a]", "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", str(folder / "talk.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=180x320:rate=25", "-t", "2", "-c:v", "libx264",
            "-preset", "ultrafast", str(folder / "silent.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100", "-t", "3", "-c:a", "aac",
            str(folder / "music.m4a"))
    return folder

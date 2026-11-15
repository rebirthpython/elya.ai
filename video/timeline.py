"""От плана к кускам для ffmpeg: вырезание пауз, длительности с точностью до кадра, размер кадра."""

from __future__ import annotations

from dataclasses import dataclass

from video.ffmpeg import ClipInfo
from video.plan import CIRCLE_MAX_SECONDS, FORMATS, EditPlan

MIN_PIECE = 0.25  # секунд: более короткие обрывки речи не оставляем
MAX_PIECES = 150  # больше — слишком долго собирать; паузы тогда режем крупнее
CIRCLE_SIDE = 640  # кружок в Telegram — до 640×640
GIF_SIDE = 720


@dataclass(frozen=True)
class Piece:
    """Кусок итогового видео из одного клипа (clip — с 1)."""

    clip: int
    start: float  # секунда в исходном клипе
    source_duration: float  # сколько секунд исходника берём
    duration: float  # длительность в итоговом видео — целое число кадров
    speed: float = 1.0
    fade_in: bool = False  # плавный переход на стыке фрагментов
    fade_out: bool = False


def speech_intervals(
    silences: list[tuple[float, float]], duration: float, *, pad: float = 0.15, join_gap: float = 0.3
) -> list[tuple[float, float]]:
    """Где звучит речь: всё, кроме тишины, с запасом по краям, чтобы не обрезать слова."""
    spans: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(silences):
        if start > cursor:
            spans.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        spans.append((cursor, duration))

    merged: list[tuple[float, float]] = []
    for start, end in ((max(0.0, a - pad), min(duration, b + pad)) for a, b in spans):
        if merged and start - merged[-1][1] <= join_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [(round(a, 3), round(b, 3)) for a, b in merged if b - a >= MIN_PIECE]


def build_pieces(
    plan: EditPlan,
    clips: list[ClipInfo],
    fps: float,
    *,
    speech: dict[int, list[tuple[float, float]]] | None = None,
    max_seconds: float,
) -> tuple[list[Piece], bool]:
    """Куски итогового видео и признак, что конец пришлось обрезать по лимиту длины.

    speech — интервалы речи по номерам клипов (для «убрать паузы»). Если в фрагменте
    не нашлось речи вовсе, он выпадает целиком; если речи нет во всём видео — паузы не режем.
    """
    if plan.output == "circle":
        max_seconds = min(max_seconds, CIRCLE_MAX_SECONDS)
    spans: list[tuple[int, float, float, float, bool, bool]] = []
    last = len(plan.timeline) - 1
    for index, segment in enumerate(plan.timeline):
        parts = [(segment.start, segment.end)]
        if plan.remove_pauses and speech is not None and segment.clip in speech:
            parts = [
                (max(a, segment.start), min(b, segment.end))
                for a, b in speech[segment.clip]
                if b > segment.start and a < segment.end
            ]
            parts = [(a, b) for a, b in parts if b - a >= MIN_PIECE]
        for n, (start, end) in enumerate(parts):
            fade_in = plan.transitions and index > 0 and n == 0
            fade_out = plan.transitions and index < last and n == len(parts) - 1
            spans.append((segment.clip, start, end, segment.speed, fade_in, fade_out))
    if not spans and plan.remove_pauses:
        return build_pieces(plan.but(remove_pauses=False), clips, fps, max_seconds=max_seconds)

    pieces: list[Piece] = []
    total = 0.0
    trimmed = False
    for clip, start, end, speed, fade_in, fade_out in spans:
        frames = round((end - start) / speed * fps)
        room = int((max_seconds - total) * fps + 1e-6)
        if frames > room:
            frames, trimmed = room, True
        if frames < 2:
            if trimmed:
                break
            continue
        duration = frames / fps
        pieces.append(Piece(clip, round(start, 3), round(duration * speed, 4), duration, speed, fade_in, fade_out))
        total += duration
        if trimmed:
            break
    return pieces, trimmed


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def canvas_size(plan: EditPlan, clips: list[ClipInfo], max_side: int) -> tuple[int, int]:
    """Размер кадра итогового видео: без увеличения исходника и не больше max_side."""
    first = clips[plan.timeline[0].clip - 1]
    width, height = first.width, first.height
    if plan.rotate in (90, 270):
        width, height = height, width
    limit = max_side
    ratio = FORMATS.get(plan.format)
    if plan.output == "circle":
        limit, ratio = CIRCLE_SIDE, (1, 1)
    elif plan.output == "gif":
        limit = min(limit, GIF_SIDE)

    if ratio is None:
        scale = min(1.0, limit / max(width, height))
        return _even(width * scale), _even(height * scale)
    rw, rh = ratio
    # Короткая сторона кадра — не больше короткой стороны исходника, длинная — не больше limit.
    short = min(min(width, height), limit * min(rw, rh) / max(rw, rh))
    if rw <= rh:
        return _even(short), _even(short * rh / rw)
    return _even(short * rw / rh), _even(short)


def output_fps(plan: EditPlan, clips: list[ClipInfo]) -> int:
    fps = round(clips[plan.timeline[0].clip - 1].fps or 30)
    fps = min(60, max(15, fps))
    if plan.output in ("gif", "circle"):
        fps = min(fps, 30)
    return fps

"""Субтитры и надписи: из расшифровки речи — короткие фразы, из них и надписей — файл ASS для ffmpeg."""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.speech import Timed, TimedTranscript
from video.plan import TextOverlay

FONT = "DejaVu Sans"  # есть почти везде; если нет, libass возьмёт похожий шрифт с кириллицей
_SENTENCE_END = (".", "!", "?", "…")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def caption_chars(width: int, height: int) -> int:
    """Сколько символов помещается в субтитр из одной-двух строк."""
    font = font_sizes(width, height)[0]
    per_line = width * 0.88 / (font * 0.56)
    return int(max(16, min(42, per_line * 1.6)))


def font_sizes(width: int, height: int) -> tuple[int, int]:
    """Кегль субтитров и надписей от размера кадра."""
    base = min(width, height)
    return max(12, round(base * 0.06)), max(14, round(base * 0.075))


def _clean(word: str) -> str:
    return " ".join(word.split())


def _from_words(words: list[Timed], max_chars: int, max_seconds: float, gap: float) -> list[Cue]:
    cues: list[Cue] = []
    current: list[Timed] = []

    def flush() -> None:
        if current:
            text = " ".join(_clean(w.text) for w in current).replace(" ,", ",").replace(" .", ".")
            cues.append(Cue(current[0].start, current[-1].end, text))
            current.clear()

    for word in words:
        if not _clean(word.text):
            continue
        if current:
            length = sum(len(_clean(w.text)) + 1 for w in current) + len(_clean(word.text))
            ended = _clean(current[-1].text).endswith(_SENTENCE_END)
            if (
                length > max_chars
                or word.start - current[-1].end > gap
                or word.end - current[0].start > max_seconds
                or (ended and length > max_chars // 2)
            ):
                flush()
        current.append(word)
    flush()
    return cues


def _from_segments(segments: list[Timed], max_chars: int) -> list[Cue]:
    """Без пословного времени: делим фразу на куски и распределяем время по длине текста."""
    cues: list[Cue] = []
    for segment in segments:
        words = _clean(segment.text).split()
        chunks: list[str] = []
        for word in words:
            if chunks and len(chunks[-1]) + 1 + len(word) <= max_chars:
                chunks[-1] += " " + word
            else:
                chunks.append(word)
        total = sum(len(c) for c in chunks) or 1
        cursor = segment.start
        for chunk in chunks:
            length = (segment.end - segment.start) * len(chunk) / total
            cues.append(Cue(cursor, cursor + length, chunk))
            cursor += length
    return cues


def build_cues(
    transcript: TimedTranscript, *, max_chars: int = 36, max_seconds: float = 3.5, gap: float = 0.7,
    total: float | None = None,
) -> list[Cue]:
    raw = (
        _from_words(transcript.words, max_chars, max_seconds, gap)
        if transcript.words
        else _from_segments(transcript.segments, max_chars)
    )
    cues: list[Cue] = []
    for i, cue in enumerate(raw):
        # Фраза держится на экране хотя бы 0,7 с, но не налезает на следующую.
        following = raw[i + 1].start if i + 1 < len(raw) else (total or cue.end + 1.0)
        end = min(max(cue.end + 0.15, cue.start + 0.7), following)
        if total is not None:
            end = min(end, total)
        if end - cue.start >= 0.2 and cue.text:
            cues.append(Cue(round(cue.start, 2), round(end, 2), cue.text))
    return cues


def ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    """Фигурные скобки и обратный слеш в ASS — команды оформления; текст их не должен содержать."""
    text = text.replace("\\", "/").replace("{", "(").replace("}", ")")
    return re.sub(r"\s+", " ", text).strip()


def build_ass(
    width: int, height: int, *, cues: list[Cue] = (), texts: list[TextOverlay] = (), total: float,
) -> str:
    caption_size, title_size = font_sizes(width, height)
    vertical = height > width
    margin_x = round(width * 0.06)
    caption_margin = round(height * (0.14 if vertical else 0.07))
    title_margin = round(height * 0.08)
    # Если внизу и субтитры, и надпись, надпись поднимаем над субтитрами.
    bottom_title_margin = caption_margin + caption_size * 3 if cues else title_margin

    def style(name: str, size: int, bold: int, align: int, margin_v: int) -> str:
        outline = max(1, round(size / 14))
        return (
            f"Style: {name},{FONT},{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
            f"{bold},0,0,0,100,100,0,0,1,{outline},{max(1, outline // 2)},{align},"
            f"{margin_x},{margin_x},{margin_v},1"
        )

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style("Caption", caption_size, -1, 2, caption_margin),
        style("top", title_size, -1, 8, title_margin),
        style("center", title_size, -1, 5, 0),
        style("bottom", title_size, -1, 2, bottom_title_margin),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for text in texts:
        start = min(text.start, max(0.0, total - 0.5))
        end = total if text.end is None else min(text.end, total)
        if end - start >= 0.3:
            lines.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},{text.position},,0,0,0,,"
                         f"{ass_escape(text.text)}")
    for cue in cues:
        lines.append(f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},Caption,,0,0,0,,"
                     f"{ass_escape(cue.text)}")
    return "\n".join(lines) + "\n"

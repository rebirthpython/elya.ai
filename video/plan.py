"""План монтажа: структура, разбор ответа модели, проверка по клипам и описание словами.

Модель возвращает план в JSON. Код приводит его к допустимым значениям: время — в пределах
клипа, скорость — в разумных границах, недоступное (музыка без трека, субтитры без
распознавания речи) выключается с пояснением. Описание того, что сделано, пишет код,
поэтому подпись к видео всегда совпадает с тем, что в нём есть.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from study.jsonutil import as_dict, as_list, as_str
from video.ffmpeg import ClipInfo

FORMATS: dict[str, tuple[int, int] | None] = {
    "original": None, "9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1), "4:5": (4, 5),
}
FORMAT_TITLES = {
    "9:16": "вертикальный 9:16", "16:9": "горизонтальный 16:9", "1:1": "квадрат 1:1", "4:5": "4:5 для ленты",
}
FITS = {"blur": "размытый фон", "crop": "края обрезаны", "pad": "чёрные полосы"}
FILTERS = {
    "none": "", "bw": "чёрно-белый", "warm": "тёплый", "cool": "холодный",
    "vivid": "сочные цвета", "vintage": "винтаж", "bright": "светлее",
}
OUTPUTS = {"video": "видео", "gif": "гифка", "circle": "кружок", "audio": "звук"}
POSITIONS = ("top", "center", "bottom")

SPEED_MIN, SPEED_MAX = 0.25, 4.0
VOLUME_MAX = 3.0
MUSIC_VOLUME_MIN, MUSIC_VOLUME_MAX = 0.05, 1.0
MAX_SEGMENTS = 50
MAX_TEXTS = 10
MAX_TEXT_CHARS = 80
MIN_SEGMENT = 0.1  # секунд: короче — уже не фрагмент
CIRCLE_MAX_SECONDS = 60

_FORMAT_ALIASES = {
    "vertical": "9:16", "portrait": "9:16", "reels": "9:16", "stories": "9:16", "shorts": "9:16",
    "horizontal": "16:9", "landscape": "16:9", "widescreen": "16:9",
    "square": "1:1", "instagram": "4:5", "source": "original", "none": "original", "": "original",
}
_OUTPUT_ALIASES = {
    "video_note": "circle", "round": "circle", "кружок": "circle", "animation": "gif",
    "mp3": "audio", "m4a": "audio", "sound": "audio", "mp4": "video", "": "video",
}
_TRUE = {"true", "yes", "да", "1", "on"}
_FALSE = {"false", "no", "нет", "0", "off", "", "none", "null"}
_END_WORDS = {"end", "конец", "до конца", "null", "none", ""}
_TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:[.,]\d+)?)$")


def fmt_time(seconds: float) -> str:
    """12.5 -> «0:12.5», 75 -> «1:15», 3725 -> «1:02:05»."""
    seconds = max(0.0, seconds)
    tenths = round(seconds * 10)
    whole, frac = divmod(tenths, 10)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
    return f"{text}.{frac}" if frac else text


def parse_time(value: Any) -> float | None:
    """Секунды из числа или строки «1:23», «1:23.5», «83», «83 с». None — «до конца».

    ValueError — если это не время.
    """
    if value is None or isinstance(value, bool):
        if isinstance(value, bool):
            raise ValueError(f"не время: {value!r}")
        return None
    if isinstance(value, (int, float)):
        if value != value or value < 0:
            raise ValueError(f"не время: {value!r}")
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"не время: {value!r}")
    text = value.strip().lower()
    if text in _END_WORDS:
        return None
    text = re.sub(r"\s*(сек(унд[аы]?)?|с|s)\.?$", "", text).strip()
    if (match := _TIME_RE.match(text)) is not None:
        hours, minutes, secs = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(secs.replace(",", "."))
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        raise ValueError(f"не время: {value!r}") from None
    if number != number or number < 0:
        raise ValueError(f"не время: {value!r}")
    return number


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return default


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None
    if isinstance(value, str):
        text = value.strip().lower().lstrip("x×").rstrip("x×").replace(",", ".")
        percent = text.endswith("%")
        try:
            number = float(text.rstrip("%").strip())
        except ValueError:
            return None
        return number / 100 if percent else number
    return None


@dataclass(frozen=True)
class Segment:
    """Фрагмент итогового видео: кусок клипа `clip` (с 1) от start до end, со скоростью speed."""

    clip: int
    start: float
    end: float
    speed: float = 1.0

    @property
    def duration(self) -> float:
        return (self.end - self.start) / self.speed


@dataclass(frozen=True)
class TextOverlay:
    """Надпись поверх видео. Время — в итоговом видео; end=None — до конца."""

    text: str
    start: float = 0.0
    end: float | None = None
    position: str = "bottom"


@dataclass(frozen=True)
class EditPlan:
    timeline: tuple[Segment, ...]
    remove_pauses: bool = False
    subtitles: bool = False
    texts: tuple[TextOverlay, ...] = ()
    format: str = "original"
    fit: str = "blur"
    rotate: int = 0
    mirror: bool = False
    stabilize: bool = False
    filter: str = "none"
    transitions: bool = False
    fade: bool = False
    volume: float = 1.0
    clean_audio: bool = False
    music: bool = False
    music_volume: float = 0.3
    output: str = "video"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timeline"] = [
            {"clip": s.clip, "from": round(float(s.start), 2), "to": round(float(s.end), 2),
             "speed": float(s.speed)}
            for s in self.timeline
        ]
        data["texts"] = [
            {"text": t.text, "from": round(float(t.start), 2),
             "to": None if t.end is None else round(float(t.end), 2), "position": t.position}
            for t in self.texts
        ]
        data["volume"], data["music_volume"] = float(self.volume), float(self.music_volume)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @property
    def key(self) -> str:
        """Одинаковые планы дают одинаковое видео — по ключу находим уже отправленный результат."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditPlan:
        """План из сохранённого состояния (он уже проверен, поэтому без замечаний)."""
        timeline = tuple(
            Segment(int(s["clip"]), float(s["from"]), float(s["to"]), float(s.get("speed", 1.0)))
            for s in data.get("timeline", [])
        )
        texts = tuple(
            TextOverlay(str(t["text"]), float(t.get("from", 0.0)),
                        None if t.get("to") is None else float(t["to"]), str(t.get("position", "bottom")))
            for t in data.get("texts", [])
        )
        simple = {k: v for k, v in data.items() if k not in ("timeline", "texts") and k in _FIELD_NAMES}
        return cls(timeline=timeline, texts=texts, **simple)

    def but(self, **changes: Any) -> EditPlan:
        return replace(self, **changes)

    @property
    def estimated_duration(self) -> float:
        """Длительность без учёта вырезанных пауз."""
        return sum(s.duration for s in self.timeline)


_FIELD_NAMES = set(EditPlan.__dataclass_fields__)


def default_plan(clips: list[ClipInfo]) -> EditPlan:
    """Все клипы целиком по порядку — с этого начинается монтаж."""
    return EditPlan(timeline=tuple(Segment(i + 1, 0.0, c.duration) for i, c in enumerate(clips)))


@dataclass(frozen=True)
class PlanContext:
    clips: list[ClipInfo]
    has_music: bool = False
    can_text: bool = True  # в ffmpeg есть libass
    can_subtitles: bool = False  # распознавание речи + libass
    subtitles_reason: str = ""
    can_stabilize: bool = True
    can_clean_audio: bool = True


@dataclass
class ParsedPlan:
    plan: EditPlan
    issues: list[str] = field(default_factory=list)  # ошибки модели: отправим ей на исправление
    notes: list[str] = field(default_factory=list)  # пояснения для человека
    need_transcript: bool = False
    note: str = ""  # что модель не смогла сделать — её словами


MUSIC_HINT = "🎵 Чтобы наложить музыку, пришли аудиофайл (mp3, m4a) прямо сюда — и напиши, что с ним сделать."


def _parse_segment(raw: Any, index: int, ctx: PlanContext, issues: list[str], notes: list[str]) -> Segment | None:
    item = as_dict(raw)
    where = f"timeline[{index}]"
    clip_raw = item.get("clip", 1 if len(ctx.clips) == 1 else None)
    clip_num = _as_float(clip_raw)
    if clip_num is None or clip_num != int(clip_num) or not 1 <= clip_num <= len(ctx.clips):
        issues.append(f"{where}: клипа {clip_raw!r} нет, есть клипы 1–{len(ctx.clips)}.")
        return None
    clip = ctx.clips[int(clip_num) - 1]
    try:
        start = parse_time(item.get("from", 0)) or 0.0
        end = parse_time(item.get("to"))
    except ValueError as exc:
        issues.append(f"{where}: {exc}. Время пиши секундами или как «м:сс».")
        return None
    end = clip.duration if end is None else min(end, clip.duration)
    if start >= clip.duration - MIN_SEGMENT:
        issues.append(
            f"{where}: клип {int(clip_num)} длится {fmt_time(clip.duration)}, "
            f"а фрагмент начинается с {fmt_time(start)}."
        )
        return None
    if end - start < MIN_SEGMENT:
        issues.append(f"{where}: конец фрагмента ({fmt_time(end)}) раньше начала ({fmt_time(start)}).")
        return None
    speed = _as_float(item.get("speed", 1.0))
    if speed is None or speed <= 0:
        speed = 1.0
    if not SPEED_MIN <= speed <= SPEED_MAX:
        speed = min(SPEED_MAX, max(SPEED_MIN, speed))
        note = f"⚡ Скорость можно менять от ×{SPEED_MIN:g} до ×{SPEED_MAX:g} — поставила ×{speed:g}."
        if note not in notes:
            notes.append(note)
    return Segment(int(clip_num), round(start, 3), round(end, 3), round(speed, 3))


def _parse_text(raw: Any, index: int, issues: list[str]) -> TextOverlay | None:
    if isinstance(raw, str):
        raw = {"text": raw}
    item = as_dict(raw)
    text = as_str(item.get("text"))
    if not text:
        return None
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1].rstrip() + "…"
    try:
        start = parse_time(item.get("from", 0)) or 0.0
        end = parse_time(item.get("to"))
    except ValueError as exc:
        issues.append(f"texts[{index}]: {exc}.")
        return None
    if end is not None and end - start < 0.3:
        issues.append(f"texts[{index}]: надпись заканчивается раньше, чем начинается.")
        return None
    position = as_str(item.get("position")).lower() or "bottom"
    if position not in POSITIONS:
        position = {"верх": "top", "up": "top", "middle": "center", "центр": "center"}.get(position, "bottom")
    return TextOverlay(text, round(start, 3), None if end is None else round(end, 3), position)


def _choice(data: dict[str, Any], key: str, allowed: dict[str, Any] | tuple[str, ...], current: str,
            issues: list[str], aliases: dict[str, str] | None = None) -> str:
    if key not in data:
        return current
    value = as_str(data[key]).lower()
    value = (aliases or {}).get(value, value)
    if key == "format":
        value = value.replace("x", ":").replace("×", ":").replace("/", ":").replace(" ", "")
    if value in allowed:
        return value
    issues.append(f"{key}: значение {data[key]!r} не поддерживается, можно: {', '.join(allowed)}.")
    return current


def parse_plan(data: dict[str, Any], ctx: PlanContext, base: EditPlan) -> ParsedPlan:
    """План из ответа модели поверх текущего: чего в ответе нет, остаётся как было."""
    issues: list[str] = []
    notes: list[str] = []

    timeline = base.timeline
    if "timeline" in data:
        raw_timeline = as_list(data["timeline"])
        segments = [s for i, raw in enumerate(raw_timeline[:MAX_SEGMENTS])
                    if (s := _parse_segment(raw, i, ctx, issues, notes)) is not None]
        if segments:
            timeline = tuple(segments)
        elif not issues:
            issues.append("timeline пуст: в видео должен остаться хотя бы один фрагмент.")
    # Клипа из старого плана могло не стать (например, план из другой сессии) — подстрахуемся.
    timeline = tuple(s for s in timeline if 1 <= s.clip <= len(ctx.clips)) or default_plan(ctx.clips).timeline

    texts = base.texts
    if "texts" in data:
        texts = tuple(t for i, raw in enumerate(as_list(data["texts"])[:MAX_TEXTS])
                      if (t := _parse_text(raw, i, issues)) is not None)

    rotate = base.rotate
    if "rotate" in data:
        degrees = _as_float(data["rotate"])
        if degrees is None or degrees % 90:
            issues.append("rotate: можно только 0, 90, 180 или 270.")
        else:
            rotate = int(degrees) % 360

    def flag(key: str, current: bool) -> bool:
        return _as_bool(data.get(key), current) if key in data else current

    def number(key: str, current: float, low: float, high: float) -> float:
        """Громкость: 1 — как есть; «150» модель иногда пишет в процентах."""
        value = _as_float(data.get(key)) if key in data else None
        if value is None:
            return current
        if value > 10:
            value /= 100
        return round(min(high, max(low, value)), 3)

    plan = EditPlan(
        timeline=timeline,
        remove_pauses=flag("remove_pauses", base.remove_pauses),
        subtitles=flag("subtitles", base.subtitles),
        texts=texts,
        format=_choice(data, "format", FORMATS, base.format, issues, _FORMAT_ALIASES),
        fit=_choice(data, "fit", FITS, base.fit, issues),
        rotate=rotate,
        mirror=flag("mirror", base.mirror),
        stabilize=flag("stabilize", base.stabilize),
        filter=_choice(data, "filter", FILTERS, base.filter, issues, {"": "none", "sepia": "vintage"}),
        transitions=flag("transitions", base.transitions),
        fade=flag("fade", base.fade),
        volume=number("volume", base.volume, 0.0, VOLUME_MAX),
        clean_audio=flag("clean_audio", base.clean_audio),
        music=flag("music", base.music),
        music_volume=number("music_volume", base.music_volume, MUSIC_VOLUME_MIN, MUSIC_VOLUME_MAX),
        output=_choice(data, "output", OUTPUTS, base.output, issues, _OUTPUT_ALIASES),
    )
    return ParsedPlan(
        plan=apply_limits(plan, ctx, notes),
        issues=issues,
        notes=notes,
        need_transcript=_as_bool(data.get("need_transcript"), False),
        note=as_str(data.get("note"))[:400],
    )


def apply_limits(plan: EditPlan, ctx: PlanContext, notes: list[str]) -> EditPlan:
    """Выключает то, что сейчас недоступно, и объясняет почему."""
    changes: dict[str, Any] = {}
    if plan.music and not ctx.has_music:
        changes["music"] = False
        notes.append(MUSIC_HINT)
    if plan.subtitles and not ctx.can_subtitles:
        changes["subtitles"] = False
        notes.append(f"💬 Субтитры сейчас недоступны: {ctx.subtitles_reason or 'нет распознавания речи'}.")
    if plan.texts and not ctx.can_text:
        changes["texts"] = ()
        notes.append("🔤 Надписи недоступны: ffmpeg на сервере собран без libass.")
    if plan.stabilize and not ctx.can_stabilize:
        changes["stabilize"] = False
        notes.append("📷 Стабилизация недоступна в этой сборке ffmpeg.")
    if plan.clean_audio and not ctx.can_clean_audio:
        changes["clean_audio"] = False
        notes.append("🎙 Шумоподавление недоступно в этой сборке ffmpeg.")
    if plan.remove_pauses and not any(ctx.clips[s.clip - 1].has_audio for s in plan.timeline):
        changes["remove_pauses"] = False
        notes.append("✂️ Паузы ищу по звуку, а в этом видео его нет.")
    if plan.output == "circle" and plan.format != "1:1":
        changes["format"] = "1:1"
    return plan.but(**changes) if changes else plan


# ---------- Описание словами ----------


def _timeline_line(plan: EditPlan, clips: list[ClipInfo]) -> str | None:
    whole = [s.start <= 0.05 and s.end >= clips[s.clip - 1].duration - 0.05 for s in plan.timeline]
    in_order = [s.clip for s in plan.timeline] == list(range(1, len(clips) + 1))
    same_speed = all(s.speed == 1 for s in plan.timeline)
    if all(whole) and in_order and same_speed:
        return f"🔗 Склеены клипы 1–{len(clips)}" if len(clips) > 1 else None

    parts = []
    for s, is_whole in zip(plan.timeline, whole):
        name = f"клип {s.clip}" if len(clips) > 1 else ""
        span = "целиком" if is_whole else f"{fmt_time(s.start)}–{fmt_time(s.end)}"
        piece = f"{name} {span}".strip() if name else span
        if s.speed != 1:
            piece += f" ×{s.speed:g}"
        parts.append(piece)
    if len(parts) > 6:
        parts = parts[:6] + [f"и ещё {len(plan.timeline) - 6}"]
    title = "✂️ Фрагмент" if len(plan.timeline) == 1 else "✂️ Фрагменты"
    return f"{title}: " + "; ".join(parts)


def describe(plan: EditPlan, clips: list[ClipInfo], *, removed_pauses: float = 0.0) -> list[str]:
    """Что сделано — по строке на изменение. Пишет код, а не модель."""
    lines: list[str] = []
    if (line := _timeline_line(plan, clips)) is not None:
        lines.append(line)
    if plan.remove_pauses:
        lines.append(f"🤐 Паузы вырезаны (−{fmt_time(removed_pauses)})" if removed_pauses >= 0.1
                     else "🤐 Пауз почти не нашлось — вырезать нечего")
    visual = plan.output != "audio"
    if visual and plan.subtitles:
        lines.append("💬 Субтитры из речи")
    if visual and plan.texts:
        quoted = ", ".join(f"«{t.text}»" for t in plan.texts[:3]) + (" …" if len(plan.texts) > 3 else "")
        lines.append(f"🔤 Надписи: {quoted}")
    if visual and plan.format != "original" and plan.output != "circle":
        lines.append(f"📐 Формат {FORMAT_TITLES[plan.format]}, {FITS[plan.fit]}")
    if visual and plan.rotate:
        lines.append(f"🔄 Повёрнуто на {plan.rotate}°")
    if visual and plan.mirror:
        lines.append("🪞 Отражено по горизонтали")
    if visual and plan.stabilize:
        lines.append("📷 Стабилизация")
    if visual and plan.filter != "none":
        lines.append(f"🎨 Фильтр: {FILTERS[plan.filter]}")
    if visual and plan.transitions and len(plan.timeline) > 1:
        lines.append("🌗 Плавные переходы")
    if plan.fade:
        lines.append("🌅 Плавное начало и конец")
    if plan.output != "gif":
        if plan.volume == 0:
            lines.append("🔇 Звук видео выключен")
        elif plan.volume != 1:
            lines.append(f"🔊 Громкость {round(plan.volume * 100)}%")
        if plan.clean_audio:
            lines.append("🎙 Звук почищен от шума и выровнен")
        if plan.music:
            lines.append(f"🎵 Музыка, громкость {round(plan.music_volume * 100)}%")
    if plan.output != "video":
        lines.append({"gif": "🖼 Гифка без звука", "circle": "⭕ Кружок", "audio": "🎧 Только звук"}[plan.output])
    return lines

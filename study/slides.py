"""Презентация: план, слайды, разбор ответа модели, проверка и нормализация.

Лимиты ниже — правила хорошей презентации, которые код требует от модели:
тезисы вместо абзацев, не больше 5 пунктов, короткие заголовки, заметки докладчика.
Если модель не уложилась даже после замечаний, normalize_presentation() доводит
слайды до лимитов сама, чтобы текст точно не вылез за границы слайда.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from study.jsonutil import ModelOutputError, as_dict, as_list, as_str, shorten, str_list

SLIDE_TYPES: dict[str, str] = {
    "bullets": "тезисы",
    "comparison": "сравнение",
    "timeline": "хронология",
    "stats": "цифры",
    "quote": "цитата",
    "section": "раздел",
    "summary": "выводы",
}

MIN_SLIDES, MAX_SLIDES = 3, 20
DEFAULT_SLIDES_HINT = "6–10"


@dataclass(frozen=True)
class Limits:
    title: int = 60
    bullet: int = 90
    bullets_min: int = 2
    bullets_max: int = 5
    column_points: int = 4
    column_point: int = 70
    events_min: int = 3
    events_max: int = 6
    event_text: int = 70
    event_date: int = 20
    stats_max: int = 3
    stat_value: int = 10
    stat_label: int = 60
    quote: int = 220
    notes_min: int = 40


LIMITS = Limits()
# Насколько можно превысить лимит, прежде чем код начнёт резать текст сам.
HARD_FACTOR = 1.35


@dataclass
class Column:
    heading: str
    points: list[str] = field(default_factory=list)


@dataclass
class Event:
    date: str
    text: str


@dataclass
class Stat:
    value: str
    label: str


@dataclass
class Slide:
    type: str
    title: str
    bullets: list[str] = field(default_factory=list)
    left: Column | None = None
    right: Column | None = None
    events: list[Event] = field(default_factory=list)
    items: list[Stat] = field(default_factory=list)
    quote: str = ""
    author: str = ""
    subtitle: str = ""
    notes: str = ""


@dataclass
class Presentation:
    title: str
    subtitle: str
    slides: list[Slide]


@dataclass
class OutlineSlide:
    type: str
    title: str
    goal: str = ""


@dataclass
class Outline:
    title: str
    subtitle: str
    slides: list[OutlineSlide]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Outline:
        return parse_outline(data)

    def as_text(self) -> str:
        lines = [f"**{self.title}**"]
        if self.subtitle:
            lines.append(f"_{self.subtitle}_")
        lines.append("")
        for number, slide in enumerate(self.slides, 1):
            kind = SLIDE_TYPES.get(slide.type, slide.type)
            lines.append(f"{number}. {slide.title} — _{kind}_")
        return "\n".join(lines)


def _slide_type(value: Any) -> str:
    kind = as_str(value).lower()
    return kind if kind in SLIDE_TYPES else "bullets"


def parse_outline(data: dict[str, Any]) -> Outline:
    slides = []
    for item in as_list(data.get("slides")):
        item = as_dict(item)
        if title := as_str(item.get("title")):
            slides.append(OutlineSlide(_slide_type(item.get("type")), title, as_str(item.get("goal"))))
    outline = Outline(as_str(data.get("title")), as_str(data.get("subtitle")), slides)
    if not outline.title or not outline.slides:
        raise ModelOutputError("в плане нет названия презентации или слайдов (title, slides)")
    return outline


def validate_outline(outline: Outline, requested: int | None) -> list[str]:
    issues = []
    count = len(outline.slides)
    if requested and count != requested:
        issues.append(f"В плане {count} слайдов, а нужно ровно {requested}.")
    elif not MIN_SLIDES <= count <= MAX_SLIDES:
        issues.append(f"В плане {count} слайдов: нужно от {MIN_SLIDES} до {MAX_SLIDES}.")
    if outline.slides and outline.slides[-1].type != "summary":
        issues.append("Последний слайд должен быть типа summary (выводы).")
    if count >= 5 and all(s.type in ("bullets", "summary") for s in outline.slides):
        issues.append(
            "Все слайды — списки. Сделай 1–3 слайда другого типа (comparison, timeline, stats, quote), "
            "где это подходит по смыслу."
        )
    return issues


def requested_slide_count(text: str) -> int | None:
    """«Фотосинтез, 8 слайдов» -> 8."""
    match = re.search(r"(\d{1,2})\s*(?:-?\s*ти|-?\s*и)?\s*слайд", text.lower())
    if not match:
        return None
    return min(max(int(match.group(1)), MIN_SLIDES), MAX_SLIDES)


def _column(value: Any) -> Column | None:
    data = as_dict(value)
    points = str_list(data.get("points"))
    heading = as_str(data.get("heading"))
    return Column(heading, points) if heading or points else None


def parse_presentation(data: dict[str, Any]) -> Presentation:
    slides = []
    for item in as_list(data.get("slides")):
        item = as_dict(item)
        slides.append(
            Slide(
                type=_slide_type(item.get("type")),
                title=as_str(item.get("title")),
                bullets=str_list(item.get("bullets")),
                left=_column(item.get("left")),
                right=_column(item.get("right")),
                events=[
                    Event(as_str(e.get("date")), as_str(e.get("text")))
                    for e in map(as_dict, as_list(item.get("events")))
                    if as_str(e.get("text"))
                ],
                items=[
                    Stat(as_str(s.get("value")), as_str(s.get("label")))
                    for s in map(as_dict, as_list(item.get("items")))
                    if as_str(s.get("value"))
                ],
                quote=as_str(item.get("quote")).strip("«»\"“”"),
                author=as_str(item.get("author")),
                subtitle=as_str(item.get("subtitle")),
                notes=as_str(item.get("notes")),
            )
        )
    presentation = Presentation(as_str(data.get("title")), as_str(data.get("subtitle")), slides)
    if not presentation.title or not presentation.slides:
        raise ModelOutputError("в презентации нет названия или слайдов (title, slides)")
    return presentation


def _too_long(texts: list[str], limit: int) -> list[str]:
    return [t for t in texts if len(t) > limit]


def validate_presentation(presentation: Presentation, outline: Outline | None) -> list[str]:
    """Замечания по слайдам. Пустой список — презентация принята."""
    lim = LIMITS
    issues: list[str] = []
    if outline is not None and len(presentation.slides) != len(outline.slides):
        issues.append(
            f"Слайдов {len(presentation.slides)}, а в утверждённом плане {len(outline.slides)}: "
            "сделай слайды строго по плану."
        )

    for number, slide in enumerate(presentation.slides, 1):
        where = f"Слайд {number} ({slide.type})"
        if not slide.title and slide.type != "quote":
            issues.append(f"{where}: нет заголовка.")
        if len(slide.title) > lim.title:
            issues.append(f"{where}: заголовок длиннее {lim.title} символов — сократи.")
        if slide.type not in ("section",) and len(slide.notes) < lim.notes_min:
            issues.append(f"{where}: добавь notes — 2–4 предложения, что сказать на этом слайде.")

        if slide.type in ("bullets", "summary"):
            count = len(slide.bullets)
            if not lim.bullets_min <= count <= lim.bullets_max:
                issues.append(
                    f"{where}: пунктов {count}, нужно {lim.bullets_min}–{lim.bullets_max}. "
                    "Лишнее перенеси в notes или на отдельный слайд."
                )
            if _too_long(slide.bullets, lim.bullet):
                issues.append(
                    f"{where}: есть пункты длиннее {lim.bullet} символов. Оставь тезис, "
                    "пояснение перенеси в notes."
                )
        elif slide.type == "comparison":
            if slide.left is None or slide.right is None:
                issues.append(f"{where}: нужны обе колонки left и right с heading и points.")
            else:
                for column in (slide.left, slide.right):
                    if not column.points or len(column.points) > lim.column_points:
                        issues.append(f"{where}: в колонке «{column.heading}» нужно 1–{lim.column_points} пункта.")
                    if _too_long(column.points, lim.column_point):
                        issues.append(
                            f"{where}: в колонке «{column.heading}» пункты длиннее {lim.column_point} символов."
                        )
        elif slide.type == "timeline":
            if not lim.events_min <= len(slide.events) <= lim.events_max:
                issues.append(f"{where}: событий {len(slide.events)}, нужно {lim.events_min}–{lim.events_max}.")
            if _too_long([e.text for e in slide.events], lim.event_text):
                issues.append(f"{where}: описания событий длиннее {lim.event_text} символов.")
        elif slide.type == "stats":
            if not 1 <= len(slide.items) <= lim.stats_max:
                issues.append(f"{where}: чисел {len(slide.items)}, нужно 1–{lim.stats_max}.")
            for stat in slide.items:
                if len(stat.value) > lim.stat_value or not re.search(r"\d", stat.value):
                    issues.append(
                        f"{where}: value «{stat.value}» должно быть коротким числом (до "
                        f"{lim.stat_value} символов), пояснение — в label. Если реальных чисел нет, "
                        "сделай слайд типа bullets."
                    )
        elif slide.type == "quote":
            if not slide.quote or not slide.author:
                issues.append(f"{where}: нужны quote и author. Если точной цитаты нет — сделай bullets.")
            elif len(slide.quote) > lim.quote:
                issues.append(f"{where}: цитата длиннее {lim.quote} символов — выбери короче.")
    return issues


def _as_bullets(slide: Slide) -> Slide:
    """Превращает слайд особого типа, для которого не хватило данных, в обычный список."""
    bullets = list(slide.bullets)
    for column in (slide.left, slide.right):
        if column:
            bullets.extend(f"{column.heading}: {p}" if column.heading else p for p in column.points)
    bullets.extend(f"{e.date} — {e.text}" if e.date else e.text for e in slide.events)
    bullets.extend(f"{s.value} — {s.label}" for s in slide.items)
    if slide.quote:
        bullets.append(f"«{slide.quote}» — {slide.author}" if slide.author else f"«{slide.quote}»")
    return Slide("bullets", slide.title, bullets=bullets, notes=slide.notes)


def normalize_presentation(presentation: Presentation) -> Presentation:
    """Доводит слайды до жёстких лимитов вёрстки. Вызывается после всех попыток исправления.

    Пункты, которые не помещаются на слайд, не выбрасываются, а переносятся в заметки докладчика.
    """
    lim = LIMITS

    def cut(text: str, limit: int) -> str:
        return shorten(text, int(limit * HARD_FACTOR))

    slides: list[Slide] = []
    for slide in presentation.slides:
        if slide.type == "comparison" and (slide.left is None or slide.right is None):
            slide = _as_bullets(slide)
        elif slide.type == "timeline" and len(slide.events) < 2:
            slide = _as_bullets(slide)
        elif slide.type == "stats" and not slide.items:
            slide = _as_bullets(slide)
        elif slide.type == "quote" and not slide.quote:
            slide = _as_bullets(slide)

        overflow: list[str] = []
        slide.title = cut(slide.title, lim.title)
        if slide.type in ("bullets", "summary"):
            overflow += slide.bullets[lim.bullets_max :]
            slide.bullets = [cut(b, lim.bullet) for b in slide.bullets[: lim.bullets_max]]
            if not slide.bullets:
                continue  # пустой слайд хуже, чем его отсутствие
        for column in (slide.left, slide.right):
            if column:
                column.heading = cut(column.heading, 40)
                overflow += column.points[lim.column_points :]
                column.points = [cut(p, lim.column_point) for p in column.points[: lim.column_points]]
        overflow += [f"{e.date} — {e.text}" for e in slide.events[lim.events_max :]]
        slide.events = [
            Event(cut(e.date, lim.event_date), cut(e.text, lim.event_text))
            for e in slide.events[: lim.events_max]
        ]
        overflow += [f"{s.value} — {s.label}" for s in slide.items[lim.stats_max :]]
        slide.items = [
            Stat(cut(s.value, lim.stat_value), cut(s.label, lim.stat_label))
            for s in slide.items[: lim.stats_max]
        ]
        slide.quote = cut(slide.quote, lim.quote)
        slide.subtitle = cut(slide.subtitle, 100)
        if overflow:
            extra = "Не поместилось на слайд: " + "; ".join(overflow)
            slide.notes = f"{slide.notes}\n\n{extra}".strip()
        slides.append(slide)

    if not slides:
        raise ModelOutputError("после проверки не осталось ни одного слайда")
    return Presentation(shorten(presentation.title, 90), shorten(presentation.subtitle, 120), slides)

"""Конспект: структура, разбор ответа модели и проверка качества.

Проверки здесь — главная гарантия качества: модель сначала выписывает из материала
все термины, числа, имена и акценты, а код потом проверяет, что каждый из них
действительно попал в конспект, и что объём соответствует выбранному режиму.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from study.jsonutil import ModelOutputError, as_dict, as_list, as_str, str_list

# Режим -> название для пользователя.
NOTE_MODES: dict[str, str] = {
    "full": "подробный конспект",
    "short": "краткое сокращение лекции",
    "details": "главные детали",
}

# Доля объёма исходного текста, в которую должен укладываться результат (мин, макс).
# Проверяется только для материала длиннее MIN_SOURCE_FOR_RATIO символов.
MODE_RATIO: dict[str, tuple[float, float]] = {
    "full": (0.15, 0.6),
    "short": (0.04, 0.22),
    "details": (0.05, 0.5),
}
MIN_SOURCE_FOR_RATIO = 3000
MAX_POINT_CHARS = 350
MAX_MISSING_LISTED = 25


@dataclass
class Term:
    term: str
    definition: str = ""


@dataclass
class Fact:
    value: str
    meaning: str = ""


@dataclass
class Section:
    heading: str
    points: list[str] = field(default_factory=list)
    definitions: list[Term] = field(default_factory=list)


@dataclass
class Extraction:
    """Всё, что нельзя потерять при сокращении: выписывается из материала первым шагом."""

    topic: str = ""
    outline: list[str] = field(default_factory=list)
    terms: list[Term] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    emphasis: list[str] = field(default_factory=list)
    organizational: list[str] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)


@dataclass
class Notes:
    mode: str
    title: str
    summary: str = ""
    sections: list[Section] = field(default_factory=list)
    key_terms: list[Term] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    exam_hints: list[str] = field(default_factory=list)
    organizational: list[str] = field(default_factory=list)
    self_check: list[str] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)
    from_topic: bool = False  # составлено без материала, по общим знаниям

    def plain_text(self) -> str:
        """Весь текст конспекта одной строкой: для проверки полноты и объёма."""
        parts = [self.title, self.summary]
        for section in self.sections:
            parts.append(section.heading)
            parts.extend(section.points)
            parts.extend(f"{t.term} {t.definition}" for t in section.definitions)
        parts.extend(f"{t.term} {t.definition}" for t in self.key_terms)
        parts.extend(f"{f.value} {f.meaning}" for f in self.facts)
        parts.extend(self.exam_hints + self.organizational + self.self_check)
        return "\n".join(p for p in parts if p)


def _terms(value: Any) -> list[Term]:
    terms = []
    for item in as_list(value):
        item = as_dict(item)
        if term := as_str(item.get("term")):
            terms.append(Term(term, as_str(item.get("definition"))))
    return terms


def _facts(value: Any) -> list[Fact]:
    facts = []
    for item in as_list(value):
        item = as_dict(item)
        if fact := as_str(item.get("value")):
            facts.append(Fact(fact, as_str(item.get("meaning"))))
    return facts


def parse_extraction(data: dict[str, Any]) -> Extraction:
    return Extraction(
        topic=as_str(data.get("topic")),
        outline=str_list(data.get("outline")),
        terms=_terms(data.get("terms")),
        facts=_facts(data.get("facts")),
        emphasis=str_list(data.get("emphasis")),
        organizational=str_list(data.get("organizational")),
        unclear=str_list(data.get("unclear")),
    )


def parse_notes(data: dict[str, Any], mode: str, *, from_topic: bool = False) -> Notes:
    sections = []
    for item in as_list(data.get("sections")):
        item = as_dict(item)
        points = str_list(item.get("points"))
        definitions = _terms(item.get("definitions"))
        heading = as_str(item.get("heading"))
        if points or definitions:
            sections.append(Section(heading or "Без названия", points, definitions))

    notes = Notes(
        mode=mode,
        title=as_str(data.get("title")),
        summary=as_str(data.get("summary")),
        sections=sections,
        key_terms=_terms(data.get("key_terms")),
        facts=_facts(data.get("facts")),
        exam_hints=str_list(data.get("exam_hints")),
        organizational=str_list(data.get("organizational")),
        self_check=str_list(data.get("self_check")),
        unclear=str_list(data.get("unclear")),
        from_topic=from_topic,
    )
    if not notes.title or not (notes.sections or notes.key_terms or notes.facts):
        raise ModelOutputError("в конспекте нет заголовка или содержания (title, sections)")
    return notes


# ---------- Проверка полноты: попал ли термин/факт в конспект ----------

_SPACES_RE = re.compile(r"[\s  ]+")
_DIGIT_GROUP_RE = re.compile(r"(?<=\d)[   ](?=\d{3}\b)")
_WORD_RE = re.compile(r"\w+")


def _normalize(text: str) -> str:
    text = _DIGIT_GROUP_RE.sub("", text.lower().replace("ё", "е"))
    return _SPACES_RE.sub(" ", text).strip()


def _stem(word: str) -> str:
    """Грубая основа слова: отрезает окончание, чтобы «мембрана» нашлась в «мембраны»."""
    if word.isdigit() or len(word) < 5:
        return word
    return word[:-2] if len(word) >= 7 else word[:-1]


class CoverageIndex:
    """Поиск фраз в тексте с учётом склонений и разных пробелов в числах."""

    def __init__(self, text: str) -> None:
        self._text = _normalize(text)
        self._compact = self._text.replace(" ", "")

    def contains(self, phrase: str) -> bool:
        needle = _normalize(phrase)
        if not needle or needle.replace(" ", "") in self._compact:
            return True
        words = [w for w in _WORD_RE.findall(needle) if len(w) >= 4 or w.isdigit()]
        return bool(words) and all(_stem(w) in self._text for w in words)


def missing_items(extraction: Extraction, notes: Notes) -> list[str]:
    """Термины и факты из материала, которых нет в конспекте."""
    index = CoverageIndex(notes.plain_text())
    missing = [t.term for t in extraction.terms if not index.contains(t.term)]
    missing += [
        f"{f.value} ({f.meaning})" if f.meaning else f.value
        for f in extraction.facts
        if not index.contains(f.value)
    ]
    return missing


def validate_notes(notes: Notes, extraction: Extraction | None, source_chars: int) -> list[str]:
    """Список замечаний к конспекту. Пустой список — конспект принят."""
    issues: list[str] = []
    if not notes.summary:
        issues.append("Нет summary: напиши суть материала в 2–3 предложениях.")
    if notes.mode != "details" and len(notes.sections) < 2:
        issues.append("Слишком мало разделов: раздели материал минимум на 2 смысловых раздела.")
    for section in notes.sections:
        long_points = [p for p in section.points if len(p) > MAX_POINT_CHARS]
        if long_points:
            issues.append(
                f"В разделе «{section.heading}» есть пункты длиннее {MAX_POINT_CHARS} символов: "
                "раздели их на несколько коротких пунктов, по одной мысли в каждом."
            )
    if notes.mode == "full" and not notes.self_check:
        issues.append("Добавь 3–5 вопросов для самопроверки (self_check).")

    if extraction is not None:
        missing = missing_items(extraction, notes)
        if missing:
            listed = "; ".join(missing[:MAX_MISSING_LISTED])
            more = f" и ещё {len(missing) - MAX_MISSING_LISTED}" if len(missing) > MAX_MISSING_LISTED else ""
            issues.append(
                "В конспекте потерялись сведения из материала — добавь их в подходящие разделы, "
                f"key_terms или facts: {listed}{more}."
            )
        if extraction.emphasis and not notes.exam_hints:
            issues.append(
                "Автор материала выделил важные моменты, а exam_hints пуст. Перенеси их туда: "
                + "; ".join(extraction.emphasis[:10])
            )
        if extraction.organizational and not notes.organizational:
            issues.append(
                "Потерялась организационная информация (задания, сроки) — заполни organizational: "
                + "; ".join(extraction.organizational[:10])
            )

    if source_chars >= MIN_SOURCE_FOR_RATIO:
        low, high = MODE_RATIO.get(notes.mode, (0.0, 1.0))
        length = len(notes.plain_text())
        if length > source_chars * high:
            issues.append(
                f"Слишком длинно для режима «{NOTE_MODES[notes.mode]}»: {length} символов при "
                f"материале {source_chars}. Сократи примерно до {int(source_chars * high * 0.8)} "
                "символов: убери повторы, примеры и второстепенное, сохранив термины и факты."
            )
        elif length < source_chars * low:
            issues.append(
                f"Слишком коротко для режима «{NOTE_MODES[notes.mode]}»: {length} символов при "
                f"материале {source_chars}. Раскрой разделы подробнее, примерно до "
                f"{int(source_chars * low * 1.5)} символов."
            )
    return issues

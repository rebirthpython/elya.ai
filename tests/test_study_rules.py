"""Правила качества: разбор ответа модели, полнота конспекта, лимиты слайдов, файлы."""

import io

import pytest
from docx import Document

from study.jsonutil import ModelOutputError, extract_json_object, shorten
from study.notes import (
    CoverageIndex,
    Extraction,
    Fact,
    Notes,
    Section,
    Term,
    missing_items,
    parse_notes,
    validate_notes,
)
from study.slides import (
    LIMITS,
    Column,
    Outline,
    OutlineSlide,
    Presentation,
    Slide,
    Stat,
    normalize_presentation,
    parse_presentation,
    requested_slide_count,
    validate_outline,
    validate_presentation,
)
from study.sources import SourceError, extract_text, safe_filename


# ---------- JSON из ответа модели ----------

@pytest.mark.parametrize("text", [
    '{"a": 1}',
    'Вот результат:\n```json\n{"a": 1}\n```\nГотово!',
    'Конечно! {"a": 1} Обращайся.',
    '{"a": 1,}',
])
def test_extract_json_object_tolerates_wrappers(text):
    assert extract_json_object(text) == {"a": 1}


@pytest.mark.parametrize("text", ["нет json", "[1, 2]", '{"a": '])
def test_extract_json_object_rejects_garbage(text):
    with pytest.raises(ModelOutputError):
        extract_json_object(text)


def test_shorten_cuts_on_word_boundary():
    assert shorten("короткий", 20) == "короткий"
    assert shorten("Крепостной труд тормозил развитие", 20) == "Крепостной труд…"


# ---------- Полнота конспекта ----------

def test_coverage_handles_inflection_numbers_and_formulas():
    index = CoverageIndex(
        "Клеточной мембраны нет. Реформа 1861 г. затронула 23 000 000 крестьян. E=mc² — формула."
    )
    assert index.contains("клеточная мембрана")
    assert index.contains("1861")
    assert index.contains("23 000 000")
    assert index.contains("E = mc²")
    assert not index.contains("митохондрия")
    assert not index.contains("1862")


def _notes(**overrides) -> Notes:
    values = dict(
        mode="full",
        title="Реформа 1861 года",
        summary="Отмена крепостного права.",
        sections=[
            Section("Причины", ["Поражение в **Крымской войне**", "Крестьянские волнения"]),
            Section("Итоги", ["Крестьяне получили свободу"], [Term("Отрезки", "часть земли")]),
        ],
        self_check=["Почему реформа началась сверху?"],
    )
    values.update(overrides)
    return Notes(**values)


def test_missing_items_lists_lost_terms_and_facts():
    extraction = Extraction(
        terms=[Term("Отрезки"), Term("Временнообязанные крестьяне")],
        facts=[Fact("1861", "год реформы"), Fact("49 лет", "срок выкупа")],
    )
    assert missing_items(extraction, _notes()) == ["Временнообязанные крестьяне", "49 лет (срок выкупа)"]


def test_validate_notes_demands_lost_facts_emphasis_and_organizational():
    extraction = Extraction(
        facts=[Fact("49 лет", "срок выкупа")],
        emphasis=["Причины реформы будут на экзамене"],
        organizational=["ДЗ: параграф 5"],
    )
    issues = "\n".join(validate_notes(_notes(), extraction, source_chars=0))
    assert "49 лет" in issues
    assert "exam_hints" in issues and "будут на экзамене" in issues
    assert "organizational" in issues and "параграф 5" in issues


def test_validate_notes_checks_volume_for_mode():
    long_notes = _notes(mode="short", self_check=[], sections=[
        Section("Раздел", ["пункт " * 60] * 3), Section("Раздел 2", ["пункт " * 60] * 3),
    ])
    issues = validate_notes(long_notes, Extraction(), source_chars=5000)
    assert any("Слишком длинно" in i for i in issues)

    short_full = _notes()
    issues = validate_notes(short_full, Extraction(), source_chars=20000)
    assert any("Слишком коротко" in i for i in issues)


def test_validate_notes_accepts_good_notes():
    extraction = Extraction(terms=[Term("Отрезки")], facts=[Fact("1861")])
    assert validate_notes(_notes(), extraction, source_chars=0) == []


def test_parse_notes_skips_empty_sections_and_requires_content():
    data = {
        "title": "Т",
        "summary": "С",
        "sections": [{"heading": "Пусто", "points": []}, {"heading": "Есть", "points": ["п", 5, None]}],
        "key_terms": [{"term": "x", "definition": "y"}, {"definition": "без термина"}],
    }
    notes = parse_notes(data, "full")
    assert [s.heading for s in notes.sections] == ["Есть"]
    assert notes.sections[0].points == ["п", "5"]
    assert [t.term for t in notes.key_terms] == ["x"]
    with pytest.raises(ModelOutputError):
        parse_notes({"title": "Т"}, "full")


# ---------- Презентации ----------

@pytest.mark.parametrize("text,expected", [
    ("Фотосинтез, 8 слайдов", 8),
    ("сделай на 12-ти слайдах", 12),
    ("про войну 1812 года", None),
    ("50 слайдов", 20),
])
def test_requested_slide_count(text, expected):
    assert requested_slide_count(text) == expected


def test_validate_outline_wants_exact_count_summary_and_variety():
    outline = Outline("Т", "", [OutlineSlide("bullets", f"Слайд {i}") for i in range(6)])
    issues = "\n".join(validate_outline(outline, requested=8))
    assert "ровно 8" in issues and "summary" in issues and "Все слайды — списки" in issues


def test_validate_presentation_reports_layout_violations():
    presentation = Presentation("Т", "", [
        Slide("bullets", "Заголовок", bullets=["пункт"] * 7, notes="коротко"),
        Slide("stats", "Цифры", items=[Stat("очень много людей", "подпись")], notes="н" * 50),
        Slide("comparison", "Сравнение", left=Column("А", ["x"]), notes="н" * 50),
    ])
    issues = "\n".join(validate_presentation(presentation, None))
    assert "пунктов 7" in issues
    assert "notes" in issues
    assert "коротким числом" in issues
    assert "обе колонки" in issues


def test_normalize_presentation_enforces_limits_and_keeps_overflow_in_notes():
    long = "слово " * 40
    presentation = Presentation("Т", "", [
        Slide("bullets", long, bullets=[f"пункт {i}" for i in range(8)], notes="Заметка"),
        Slide("comparison", "Без второй колонки", left=Column("А", ["x", "y"])),
        Slide("quote", "Пустая цитата"),
        Slide("bullets", "Пустой слайд"),
    ])
    result = normalize_presentation(presentation)
    first = result.slides[0]
    assert len(first.bullets) == LIMITS.bullets_max
    assert len(first.title) <= LIMITS.title * 1.35 + 1
    assert "Не поместилось на слайд: пункт 5; пункт 6; пункт 7" in first.notes
    assert result.slides[1].type == "bullets"  # сравнение без пары превращается в список
    assert result.slides[1].bullets == ["А: x", "А: y"]
    assert [s.title for s in result.slides] == [first.title, "Без второй колонки"]


def test_parse_presentation_falls_back_to_bullets_for_unknown_type():
    data = {"title": "Т", "slides": [{"type": "mindmap", "title": "Х", "bullets": ["а"]}]}
    assert parse_presentation(data).slides[0].type == "bullets"


# ---------- Файлы ----------

def test_extract_text_from_txt_and_docx():
    assert extract_text("lecture.txt", "Лекция\r\n\r\n\r\n\r\nтекст  \n".encode("cp1251")) == "Лекция\n\nтекст"

    document = Document()
    document.add_paragraph("Первый абзац")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Термин", "Определение"
    buffer = io.BytesIO()
    document.save(buffer)
    text = extract_text("Лекция.DOCX", buffer.getvalue())
    assert "Первый абзац" in text and "Термин | Определение" in text


@pytest.mark.parametrize("name,data,message", [
    ("photo.jpg", b"x", "формат"),
    ("empty.txt", b"   ", "не нашлось текста"),
    ("broken.docx", b"not a zip", "повреждён"),
])
def test_extract_text_errors_are_user_friendly(name, data, message):
    with pytest.raises(SourceError, match=message):
        extract_text(name, data)


def test_safe_filename():
    assert safe_filename('Реформа: "итоги"/выводы?', ".pptx") == "Реформа итогивыводы.pptx"
    assert safe_filename("...", ".docx") == "Эля.docx"

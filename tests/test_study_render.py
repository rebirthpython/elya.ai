"""Сборка файлов: PPTX открывается, анимации ссылаются на настоящие фигуры, DOCX содержит всё."""

import io

import pytest
from docx import Document
from lxml import etree
from pptx import Presentation as PptxPresentation
from pptx.oxml.ns import qn

from study.notes import Fact, Notes, Section, Term
from study.pptx_anim import Effect, timing_xml
from study.render_docx import TOPIC_DISCLAIMER, notes_to_markdown, render_notes
from study.render_pptx import CLOSING_TITLE, fit_font_size, render_presentation
from study.slides import Column, Event, Presentation, Slide, Stat
from study.themes import THEMES

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _presentation() -> Presentation:
    return Presentation("Отмена крепостного права", "Причины и итоги", [
        Slide("bullets", "Причины", bullets=["Крымская **война**", "Волнения", "Отсталость"], notes="Скажи о войне"),
        Slide("timeline", "Ход", events=[Event("1856", "Речь"), Event("1857", "Комитет"), Event("1861", "Манифест")]),
        Slide("comparison", "Кто что получил", left=Column("Крестьяне", ["Свобода"]),
              right=Column("Помещики", ["Выкуп", "Отрезки"])),
        Slide("stats", "Цифры", items=[Stat("23 млн", "освобождено"), Stat("49 лет", "выкуп")]),
        Slide("quote", "Цитата", quote="Лучше сверху, чем снизу", author="Александр II"),
        Slide("section", "Последствия", subtitle="Что изменилось"),
        Slide("summary", "Выводы", bullets=["Свобода", "Зависимость"], notes="Итог"),
    ])


def _clicks(slide) -> list[list[str]]:
    """Для каждого щелчка — список эффектов (presetID) внутри него."""
    timing = slide._element.find(qn("p:timing"))
    if timing is None:
        return []
    main = next(c for c in timing.iter(f"{P}cTn") if c.get("nodeType") == "mainSeq")
    steps = main.find(f"{P}childTnLst")
    return [
        [c.get("presetID") for c in step.iter(f"{P}cTn") if c.get("presetClass") == "entr"]
        for step in steps
    ]


@pytest.mark.parametrize("theme_key", list(THEMES))
def test_presentation_structure_animations_and_notes(theme_key):
    data = render_presentation(_presentation(), THEMES[theme_key])
    pptx = PptxPresentation(io.BytesIO(data))
    slides = list(pptx.slides)

    assert len(slides) == len(_presentation().slides) + 2  # титульный и финальный
    assert CLOSING_TITLE in "".join(s.text_frame.text for s in slides[-1].shapes if s.has_text_frame)
    assert slides[1].notes_slide.notes_text_frame.text == "Скажи о войне"

    for slide in slides:
        element = slide._element
        children = [etree.QName(c).localname for c in element]
        assert children.index("transition") < children.index("timing")  # порядок по схеме
        shape_ids = {s.shape_id for s in slide.shapes}
        targets = {int(t.get("spid")) for t in element.iter(f"{P}spTgt")}
        assert targets and targets <= shape_ids  # анимации ссылаются на существующие фигуры
        ctn_ids = [c.get("id") for c in element.iter(f"{P}cTn")]
        assert len(ctn_ids) == len(set(ctn_ids))

    assert len(_clicks(slides[1])) == 3  # три пункта — три щелчка
    assert all(len(step) == 3 for step in _clicks(slides[2]))  # точка, дата и текст события вместе
    assert _clicks(slides[3]) == [["2"], ["2"]]  # колонки сравнения вылетают по очереди
    assert len(_clicks(slides[4])) == 2  # два числа


def test_bullet_animation_targets_paragraphs():
    data = render_presentation(_presentation(), THEMES["classic"])
    slide = list(PptxPresentation(io.BytesIO(data)).slides)[1]
    ranges = [(r.get("st"), r.get("end")) for r in slide._element.iter(f"{P}pRg")]
    assert sorted(set(ranges)) == [("0", "0"), ("1", "1"), ("2", "2")]
    builds = list(slide._element.iter(f"{P}bldP"))
    assert [b.get("build") for b in builds] == ["p"]


def test_timing_xml_auto_start_and_unknown_effect():
    xml = timing_xml([[Effect(5, "zoom"), Effect(6, "fade")]], auto_first=True)
    assert 'evt="onBegin"' in xml and 'nodeType="afterEffect"' in xml and 'nodeType="withEffect"' in xml
    with pytest.raises(ValueError):
        timing_xml([[Effect(5, "spin")]])


def test_fit_font_size_shrinks_long_text():
    short = fit_font_size(["Коротко"], 10, 1, max_pt=36, min_pt=12)
    long = fit_font_size(["Очень длинный пункт про развитие экономики " * 6], 10, 1, max_pt=36, min_pt=12)
    assert short == 36 and 12 <= long < short


def _notes(**overrides) -> Notes:
    values = dict(
        mode="full", title="Фотосинтез", summary="Свет превращается в энергию связей.",
        sections=[Section("Световая фаза", ["Идёт в **тилакоидах**"], [Term("Фотолиз", "расщепление воды")])],
        key_terms=[Term("Хлорофилл", "пигмент")], facts=[Fact("1771", "опыт Пристли")],
        exam_hints=["Уравнение — наизусть"], organizational=["ДЗ: §12"],
        self_check=["Откуда берётся кислород?"], unclear=["обрыв фразы"],
    )
    values.update(overrides)
    return Notes(**values)


def test_notes_docx_contains_every_block():
    document = Document(io.BytesIO(render_notes(_notes())))
    text = "\n".join(p.text for p in document.paragraphs)
    cells = "\n".join(c.text for t in document.tables for row in t.rows for c in row.cells)
    for expected in ("Фотосинтез", "Световая фаза", "Идёт в тилакоидах", "Фотолиз — расщепление воды",
                     "Проверь себя", "Откуда берётся кислород?", "обрыв фразы"):
        assert expected in text
    for expected in ("Свет превращается", "Уравнение — наизусть", "Хлорофилл", "1771", "ДЗ: §12"):
        assert expected in cells
    bold = [r.text for p in document.paragraphs for r in p.runs if r.bold]
    assert "тилакоидах" in bold


def test_notes_markdown_and_topic_disclaimer():
    short = notes_to_markdown(_notes(from_topic=True), full=False)
    assert TOPIC_DISCLAIMER in short and "**Разделы:** Световая фаза" in short
    full = notes_to_markdown(_notes(), full=True)
    assert "- Идёт в **тилакоидах**" in full and "**1771** — опыт Пристли" in full
    document = Document(io.BytesIO(render_notes(_notes(from_topic=True))))
    assert any(TOPIC_DISCLAIMER in p.text for p in document.paragraphs)

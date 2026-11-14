"""Сборка конспекта в .docx и короткой версии для чата."""

from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.table import _Cell

from study.notes import NOTE_MODES, Fact, Notes, Term

ACCENT = RGBColor(0x1E, 0x3A, 0x5F)
MUTED = RGBColor(0x6B, 0x72, 0x80)
SUMMARY_FILL = "EEF3FA"
IMPORTANT_FILL = "FFF4D6"
ORG_FILL = "F1F1F1"
HEADER_FILL = "DCE6F2"
TOPIC_DISCLAIMER = (
    "Составлено без исходного материала, по общим знаниям Эли. "
    "Сверь даты, цифры и определения с учебником."
)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _add_markdown(paragraph, text: str, *, size: float | None = None, color: RGBColor | None = None) -> None:
    position = 0
    pieces: list[tuple[str, bool]] = []
    for match in _BOLD_RE.finditer(text):
        if match.start() > position:
            pieces.append((text[position : match.start()], False))
        pieces.append((match.group(1), True))
        position = match.end()
    if position < len(text):
        pieces.append((text[position:], False))
    for chunk, bold in pieces:
        run = paragraph.add_run(chunk)
        run.bold = bold or None
        if size:
            run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = color


def _shade(cell: _Cell, fill: str) -> None:
    properties = cell._tc.get_or_add_tcPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), fill)
    properties.append(shading)


def _box(document, title: str, lines: list[str], fill: str, *, numbered: bool = False) -> None:
    """Цветная плашка: суть, «важно», организационное."""
    table = document.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = table.rows[0].cells[0]
    _shade(cell, fill)
    heading = cell.paragraphs[0]
    run = heading.add_run(title)
    run.bold = True
    run.font.color.rgb = ACCENT
    for index, line in enumerate(lines, 1):
        paragraph = cell.add_paragraph()
        prefix = f"{index}. " if numbered else ("• " if len(lines) > 1 else "")
        _add_markdown(paragraph, prefix + line)
    document.add_paragraph()


def _two_columns(document, header: tuple[str, str], rows: list[tuple[str, str]]) -> None:
    table = document.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    for cell, text in zip(table.rows[0].cells, header, strict=True):
        _shade(cell, HEADER_FILL)
        cell.paragraphs[0].add_run(text).bold = True
    for left, right in rows:
        cells = table.add_row().cells
        cells[0].paragraphs[0].add_run(left).bold = True
        _add_markdown(cells[1].paragraphs[0], right)
    table.autofit = False
    for column, width in zip(table.columns, (Cm(5), Cm(12)), strict=True):
        column.width = width
        for cell in column.cells:
            cell.width = width
    document.add_paragraph()


def _terms_rows(terms: list[Term]) -> list[tuple[str, str]]:
    return [(t.term, t.definition or "—") for t in terms]


def _facts_rows(facts: list[Fact]) -> list[tuple[str, str]]:
    return [(f.value, f.meaning or "—") for f in facts]


def render_notes(notes: Notes) -> bytes:
    document = Document()
    for section in document.sections:
        section.left_margin = section.right_margin = Cm(2)
        section.top_margin = section.bottom_margin = Cm(1.8)
    base = document.styles["Normal"]
    base.font.name = "Calibri"
    base.font.size = Pt(11)
    for level in (1, 2):
        document.styles[f"Heading {level}"].font.color.rgb = ACCENT

    title = document.add_heading(notes.title, level=0)
    title.runs[0].font.color.rgb = ACCENT
    meta = document.add_paragraph()
    meta_run = meta.add_run(NOTE_MODES.get(notes.mode, "конспект").capitalize())
    meta_run.italic = True
    meta_run.font.color.rgb = MUTED
    if notes.from_topic:
        disclaimer = document.add_paragraph()
        warn = disclaimer.add_run(TOPIC_DISCLAIMER)
        warn.italic = True
        warn.font.color.rgb = MUTED

    if notes.summary:
        _box(document, "Суть", [notes.summary], SUMMARY_FILL)
    if notes.exam_hints:
        _box(document, "⚠ Важно запомнить", notes.exam_hints, IMPORTANT_FILL)

    for section in notes.sections:
        document.add_heading(section.heading, level=1)
        for point in section.points:
            _add_markdown(document.add_paragraph(style="List Bullet"), point)
        for term in section.definitions:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.6)
            run = paragraph.add_run(f"{term.term} — ")
            run.bold = True
            run.font.color.rgb = ACCENT
            _add_markdown(paragraph, term.definition)

    if notes.key_terms:
        document.add_heading("Термины", level=1)
        _two_columns(document, ("Термин", "Определение"), _terms_rows(notes.key_terms))
    if notes.facts:
        document.add_heading("Даты, числа, имена", level=1)
        _two_columns(document, ("Что", "Что это значит"), _facts_rows(notes.facts))
    if notes.organizational:
        _box(document, "Организационное", notes.organizational, ORG_FILL)
    if notes.self_check:
        document.add_heading("Проверь себя", level=1)
        for question in notes.self_check:
            _add_markdown(document.add_paragraph(style="List Number"), question)
    if notes.unclear:
        document.add_heading("Сверь с записью", level=2)
        for item in notes.unclear:
            paragraph = document.add_paragraph(style="List Bullet")
            run = paragraph.add_run(item)
            run.italic = True
            run.font.color.rgb = MUTED

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def notes_to_markdown(notes: Notes, *, full: bool) -> str:
    """Версия для чата. full=False — только суть и план, остальное в файле."""
    lines = [f"📝 **{notes.title}**", ""]
    if notes.from_topic:
        lines += [f"_{TOPIC_DISCLAIMER}_", ""]
    if notes.summary:
        lines += [notes.summary, ""]
    if notes.exam_hints:
        lines += ["⚠️ **Важно запомнить**", *(f"- {h}" for h in notes.exam_hints), ""]
    if full:
        for section in notes.sections:
            lines.append(f"**{section.heading}**")
            lines += [f"- {p}" for p in section.points]
            lines += [f"- **{t.term}** — {t.definition}" for t in section.definitions]
            lines.append("")
        if notes.facts:
            lines += ["**Даты, числа, имена**", *(f"- **{f.value}** — {f.meaning}" for f in notes.facts), ""]
        if notes.organizational:
            lines += ["📌 **Организационное**", *(f"- {o}" for o in notes.organizational), ""]
    else:
        lines.append("**Разделы:** " + "; ".join(s.heading for s in notes.sections))
        counts = []
        if notes.key_terms:
            counts.append(f"терминов: {len(notes.key_terms)}")
        if notes.facts:
            counts.append(f"дат, чисел и имён: {len(notes.facts)}")
        if notes.self_check:
            counts.append(f"вопросов для самопроверки: {len(notes.self_check)}")
        if counts:
            lines.append("В файле: " + ", ".join(counts) + ".")
    return "\n".join(lines).strip()

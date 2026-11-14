"""Сборка .pptx из проверенной презентации: вёрстка по теме, анимации, заметки докладчика."""

from __future__ import annotations

import io
import re
from collections.abc import Sequence
from dataclasses import dataclass

from lxml import etree
from pptx import Presentation as PptxPresentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.shapes.autoshape import Shape
from pptx.slide import Slide as PptxSlide
from pptx.util import Emu, Inches, Pt

from study.pptx_anim import Effect, add_animations, add_transition
from study.slides import Presentation, Slide
from study.themes import Theme

SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN_X = 0.8
CONTENT_W = SLIDE_W - 2 * MARGIN_X
BODY_TOP, BODY_BOTTOM = 2.05, 6.75
CLOSING_TITLE = "Спасибо за внимание!"
CLOSING_SUBTITLE = "Готова ответить на вопросы"

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# Средняя ширина символа в долях кегля: с запасом для кириллицы и жирного начертания.
_CHAR_WIDTH = 0.55
_LINE_HEIGHT = 1.25
# Средняя ширина русского текста в долях кегля (обычный, жирный), измерена по шрифтам
# с запасом ~5%. Для Trebuchet и Georgia — по более широким заменам (Inter, DejaVu Serif),
# которые подставляют LibreOffice и Google Slides, если шрифта нет.
_FONT_WIDTH: dict[str, tuple[float, float]] = {
    "Calibri": (0.49, 0.50),
    "Arial": (0.54, 0.58),
    "Trebuchet MS": (0.57, 0.59),
    "Georgia": (0.61, 0.68),
}
_DEFAULT_WIDTH = (0.58, 0.62)
_DIGIT_EXTRA = 0.08  # крупные цифры: запас на широкие начертания


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor.from_string(hex_color)


def _plain(text: str) -> str:
    return _BOLD_RE.sub(r"\1", text)


def _wrapped_lines(text: str, chars_per_line: int) -> int:
    lines, current = 1, 0
    for word in _plain(text).split():
        length = len(word)
        if current and current + 1 + length > chars_per_line:
            lines += 1 + (length - 1) // chars_per_line
            current = length % chars_per_line or chars_per_line
        else:
            current += length + (1 if current else 0)
    return lines


def fit_font_size(
    paragraphs: Sequence[str],
    width: float,
    height: float,
    *,
    max_pt: int,
    min_pt: int,
    gap: float = 0.5,
    indent: float = 0.0,
    char_width: float = _CHAR_WIDTH,
) -> int:
    """Наибольший кегль, при котором текст поместится в рамку (в дюймах).

    Оценка грубая, но консервативная: символы считаются шире среднего.
    gap — отступ после абзаца в долях кегля, indent — отступ под маркер списка,
    char_width — средняя ширина символа в долях кегля (для крупных жирных цифр больше).
    """
    for size in range(max_pt, min_pt - 1, -1):
        chars_per_line = max(3, int((width - indent) * 72 / (size * char_width)))
        lines = sum(_wrapped_lines(p, chars_per_line) for p in paragraphs)
        total = lines * size * _LINE_HEIGHT + max(0, len(paragraphs) - 1) * size * gap
        if total <= height * 72:
            return size
    return min_pt


@dataclass(frozen=True)
class TextStyle:
    size: int
    color: str
    font: str
    bold: bool = False
    italic: bool = False


def _set_bullet(paragraph, char: str, color: str, size: int) -> None:
    """Маркер списка своего цвета; элементы добавляются после интервалов (порядок схемы)."""
    p_pr = paragraph._p.get_or_add_pPr()
    indent = Emu(Pt(size) * 1.1)
    p_pr.set("marL", str(int(indent)))
    p_pr.set("indent", str(-int(indent)))
    bu_clr = etree.SubElement(p_pr, qn("a:buClr"))
    etree.SubElement(bu_clr, qn("a:srgbClr")).set("val", color)
    etree.SubElement(p_pr, qn("a:buFont")).set("typeface", "Arial")
    etree.SubElement(p_pr, qn("a:buChar")).set("char", char)


def _add_runs(paragraph, text: str, style: TextStyle, accent: str | None) -> None:
    """**жирный** из ответа модели -> жирный текст цвета акцента."""
    position = 0
    for match in _BOLD_RE.finditer(text):
        if match.start() > position:
            _run(paragraph, text[position : match.start()], style, None)
        _run(paragraph, match.group(1), style, accent, bold=True)
        position = match.end()
    if position < len(text):
        _run(paragraph, text[position:], style, None)


def _run(paragraph, text: str, style: TextStyle, color: str | None, *, bold: bool = False) -> None:
    run = paragraph.add_run()
    run.text = text
    font = run.font
    font.size = Pt(style.size)
    font.name = style.font
    font.bold = style.bold or bold
    font.italic = style.italic
    font.color.rgb = _rgb(color or style.color)


def _text_frame(shape: Shape, *, anchor=MSO_ANCHOR.TOP, margins: float = 0.05) -> object:
    frame = shape.text_frame
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.vertical_anchor = anchor
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(frame, side, Inches(margins))
    return frame


def _fill_paragraphs(
    frame,
    items: Sequence[tuple[str, TextStyle]],
    *,
    align=PP_ALIGN.LEFT,
    gap: float = 0.5,
    bullet: tuple[str, str] | None = None,
    accent: str | None = None,
) -> None:
    for index, (text, style) in enumerate(items):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = align
        paragraph.line_spacing = 1.05
        if index < len(items) - 1:
            paragraph.space_after = Pt(style.size * gap)
        if bullet is not None:
            _set_bullet(paragraph, bullet[0], bullet[1], style.size)
        _add_runs(paragraph, text, style, accent)


def _textbox(
    slide: PptxSlide,
    box: tuple[float, float, float, float],
    items: Sequence[tuple[str, TextStyle]],
    *,
    anchor=MSO_ANCHOR.TOP,
    align=PP_ALIGN.LEFT,
    gap: float = 0.5,
    bullet: tuple[str, str] | None = None,
    accent: str | None = None,
) -> Shape:
    x, y, w, h = box
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = _text_frame(shape, anchor=anchor)
    _fill_paragraphs(frame, items, align=align, gap=gap, bullet=bullet, accent=accent)
    return shape


def _rect(
    slide: PptxSlide,
    box: tuple[float, float, float, float],
    color: str,
    *,
    shape_type=MSO_SHAPE.RECTANGLE,
) -> Shape:
    x, y, w, h = box
    shape = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    shape.line.fill.background()
    shape.shadow.inherit = False
    if shape_type == MSO_SHAPE.ROUNDED_RECTANGLE:
        shape.adjustments[0] = 0.08
    return shape


class _Renderer:
    def __init__(self, presentation: Presentation, theme: Theme) -> None:
        self.data = presentation
        self.theme = theme
        self.pptx = PptxPresentation()
        self.pptx.slide_width = Inches(SLIDE_W)
        self.pptx.slide_height = Inches(SLIDE_H)
        self._blank = self.pptx.slide_layouts[6]
        self._section_number = 0

    # ---------- общие элементы ----------

    def _new_slide(self, *, cover: bool = False) -> PptxSlide:
        slide = self.pptx.slides.add_slide(self._blank)
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = _rgb(self.theme.cover_background if cover else self.theme.background)
        add_transition(slide, self.theme.transition)
        return slide

    def _char_width(self, *, heading: bool, bold: bool, digits: bool = False) -> float:
        font = self.theme.heading_font if heading else self.theme.body_font
        regular, bold_width = _FONT_WIDTH.get(font, _DEFAULT_WIDTH)
        return (bold_width if bold else regular) + (_DIGIT_EXTRA if digits else 0)

    def _fit(self, texts: Sequence[str], width: float, height: float, *, heading: bool = False,
             bold: bool = False, digits: bool = False, **kwargs) -> int:
        char_width = self._char_width(heading=heading, bold=bold, digits=digits)
        return fit_font_size(texts, width, height, char_width=char_width, **kwargs)

    def _text_height(self, texts: Sequence[str], width: float, size: int, *, heading: bool = False,
                     bold: bool = False) -> float:
        """Высота текста в дюймах при заданном кегле (та же оценка, что в fit_font_size)."""
        per_line = max(3, int(width * 72 / (size * self._char_width(heading=heading, bold=bold))))
        return sum(_wrapped_lines(t, per_line) for t in texts) * size * _LINE_HEIGHT / 72

    def _style(self, size: int, *, color: str | None = None, heading: bool = False,
               bold: bool = False, italic: bool = False) -> TextStyle:
        t = self.theme
        return TextStyle(size, color or t.text, t.heading_font if heading else t.body_font, bold, italic)

    def _title(self, slide: PptxSlide, title: str) -> None:
        size = self._fit([title], CONTENT_W, 1.2, max_pt=36, min_pt=24, heading=True, bold=True)
        _textbox(
            slide, (MARGIN_X, 0.5, CONTENT_W, 1.25),
            [(title, self._style(size, heading=True, bold=True))],
            anchor=MSO_ANCHOR.BOTTOM,
        )
        _rect(slide, (MARGIN_X + 0.05, 1.82, 1.1, 0.07), self.theme.accent)

    def _footer(self, slide: PptxSlide, number: int) -> None:
        t = self.theme
        _textbox(
            slide, (MARGIN_X, 6.95, 9.0, 0.35),
            [(self.data.title, self._style(11, color=t.muted))],
        )
        _textbox(
            slide, (SLIDE_W - MARGIN_X - 1.0, 6.95, 1.0, 0.35),
            [(str(number), self._style(11, color=t.muted))],
            align=PP_ALIGN.RIGHT,
        )

    def _notes(self, slide: PptxSlide, notes: str) -> None:
        if notes:
            slide.notes_slide.notes_text_frame.text = notes

    # ---------- типы слайдов ----------

    def cover(self) -> None:
        t = self.theme
        slide = self._new_slide(cover=True)
        title_size = self._fit([self.data.title], 11.0, 2.2, max_pt=54, min_pt=32, heading=True, bold=True)
        _rect(slide, (MARGIN_X + 0.05, 2.05, 1.6, 0.1), t.cover_accent)
        title = _textbox(
            slide, (MARGIN_X, 2.35, 11.0, 2.3),
            [(self.data.title, self._style(title_size, color=t.cover_text, heading=True, bold=True))],
        )
        steps = [[Effect(title.shape_id, "fade")]]
        if self.data.subtitle:
            subtitle = _textbox(
                slide, (MARGIN_X, 4.8, 11.0, 1.2),
                [(self.data.subtitle, self._style(24, color=t.cover_muted))],
            )
            steps[0].append(Effect(subtitle.shape_id, "fade"))
        add_animations(slide, steps, auto_first=True)

    def bullets(self, data: Slide, number: int, *, summary: bool = False) -> PptxSlide:
        t = self.theme
        slide = self._new_slide()
        self._title(slide, data.title)
        height = BODY_BOTTOM - BODY_TOP - 0.2
        width = CONTENT_W - (0.6 if summary else 0)
        size = self._fit(data.bullets, width, height, max_pt=30, min_pt=16, gap=0.6, indent=0.45)
        if summary:
            _rect(slide, (MARGIN_X, BODY_TOP, CONTENT_W, height + 0.1), t.card,
                  shape_type=MSO_SHAPE.ROUNDED_RECTANGLE)
        box = (MARGIN_X + (0.3 if summary else 0), BODY_TOP + (0.15 if summary else 0.05), width, height)
        body = _textbox(
            slide, box, [(b, self._style(size)) for b in data.bullets],
            gap=0.6, bullet=("✓" if summary else "■", t.accent), accent=t.accent,
            anchor=MSO_ANCHOR.MIDDLE if summary else MSO_ANCHOR.TOP,
        )
        add_animations(
            slide,
            [[Effect(body.shape_id, t.bullet_effect, i)] for i in range(len(data.bullets))],
        )
        return slide

    def comparison(self, data: Slide, number: int) -> PptxSlide:
        t = self.theme
        slide = self._new_slide()
        self._title(slide, data.title)
        gap, padding = 0.4, 0.35
        width = (CONTENT_W - gap) / 2
        height = BODY_BOTTOM - BODY_TOP
        inner_w, inner_h = width - 2 * padding, height - 2 * padding
        columns = [c for c in (data.left, data.right) if c is not None]

        # Заголовки колонок одного кегля; под пункты остаётся высота за вычетом самого высокого.
        heading_size = min(
            self._fit([c.heading], inner_w, 1.0, max_pt=26, min_pt=16, heading=True, bold=True)
            for c in columns
        )
        heading_h = max(self._text_height([c.heading], inner_w, heading_size, heading=True, bold=True)
                        for c in columns) + heading_size * 0.6 / 72
        size = min(
            self._fit(c.points, inner_w, inner_h - heading_h, max_pt=24, min_pt=12, gap=0.5, indent=0.4)
            for c in columns
        )
        steps = []
        for index, (column, effect) in enumerate(zip(columns, ("fly_left", "fly_right"), strict=False)):
            x = MARGIN_X + index * (width + gap)
            card = _rect(slide, (x, BODY_TOP, width, height), t.card, shape_type=MSO_SHAPE.ROUNDED_RECTANGLE)
            frame = _text_frame(card, margins=padding)
            accent = t.accent if index == 0 else t.accent2
            _fill_paragraphs(
                frame, [(column.heading, self._style(heading_size, color=accent, heading=True, bold=True))]
            )
            for point in column.points:
                paragraph = frame.add_paragraph()
                paragraph.line_spacing = 1.05
                paragraph.space_before = Pt(size * 0.5)
                _set_bullet(paragraph, "•", accent, size)
                _add_runs(paragraph, point, self._style(size), accent)
            steps.append([Effect(card.shape_id, effect)])
        add_animations(slide, steps)
        return slide

    def timeline(self, data: Slide, number: int) -> PptxSlide:
        t = self.theme
        slide = self._new_slide()
        self._title(slide, data.title)
        events = data.events
        line_y = 3.55
        _rect(slide, (MARGIN_X, line_y, CONTENT_W, 0.05), t.muted)
        slot = CONTENT_W / len(events)
        # Каждое событие в своей рамке: кегль — наименьший из подходящих, чтобы все были одинаковы.
        text_size = min(
            self._fit([e.text], slot - 0.3, BODY_BOTTOM - line_y - 0.6, max_pt=22, min_pt=12)
            for e in events
        )
        date_size = min(
            (self._fit([e.date], slot - 0.2, 0.8, max_pt=30, min_pt=14, heading=True, bold=True)
             for e in events if e.date),
            default=24,
        )
        steps = []
        for index, event in enumerate(events):
            center = MARGIN_X + slot * (index + 0.5)
            dot = _rect(slide, (center - 0.17, line_y - 0.15, 0.34, 0.34), t.accent, shape_type=MSO_SHAPE.OVAL)
            date = _textbox(
                slide, (center - slot / 2 + 0.1, BODY_TOP + 0.15, slot - 0.2, line_y - BODY_TOP - 0.35),
                [(event.date, self._style(date_size, color=t.accent, heading=True, bold=True))],
                anchor=MSO_ANCHOR.BOTTOM, align=PP_ALIGN.CENTER,
            )
            text = _textbox(
                slide, (center - slot / 2 + 0.12, line_y + 0.4, slot - 0.24, BODY_BOTTOM - line_y - 0.4),
                [(event.text, self._style(text_size))],
                align=PP_ALIGN.CENTER, accent=t.accent,
            )
            steps.append([
                Effect(dot.shape_id, "zoom"),
                Effect(date.shape_id, "fade"),
                Effect(text.shape_id, "rise"),
            ])
        add_animations(slide, steps)
        return slide

    def stats(self, data: Slide, number: int) -> PptxSlide:
        t = self.theme
        slide = self._new_slide()
        self._title(slide, data.title)
        count = len(data.items)
        gap = 0.4
        width = (CONTENT_W - gap * (count - 1)) / count
        top, height = BODY_TOP + 0.35, 3.9
        value_size = min(
            self._fit([s.value], width - 0.7, 1.3, max_pt=66, min_pt=28, heading=True, bold=True, digits=True)
            for s in data.items
        )
        label_size = min(
            self._fit([s.label], width - 0.6, height - 2.2, max_pt=22, min_pt=13) for s in data.items
        )
        steps = []
        for index, stat in enumerate(data.items):
            x = MARGIN_X + index * (width + gap)
            card = _rect(slide, (x, top, width, height), t.card, shape_type=MSO_SHAPE.ROUNDED_RECTANGLE)
            frame = _text_frame(card, anchor=MSO_ANCHOR.MIDDLE, margins=0.3)
            _fill_paragraphs(
                frame,
                [
                    (stat.value, self._style(value_size, color=t.accent, heading=True, bold=True)),
                    (stat.label, self._style(label_size)),
                ],
                align=PP_ALIGN.CENTER, gap=0.35,
            )
            steps.append([Effect(card.shape_id, "zoom")])
        add_animations(slide, steps)
        return slide

    def quote(self, data: Slide, number: int) -> PptxSlide:
        t = self.theme
        slide = self._new_slide()
        if data.title:
            _textbox(slide, (MARGIN_X, 0.55, CONTENT_W, 0.6),
                     [(data.title, self._style(18, color=t.muted, heading=True, bold=True))])
        _textbox(slide, (MARGIN_X - 0.05, 1.0, 1.5, 1.4),
                 [("“", self._style(120, color=t.accent, heading=True, bold=True))])
        size = self._fit([data.quote], 11.2, 2.9, max_pt=36, min_pt=20, gap=0, heading=True)
        quote = _textbox(
            slide, (MARGIN_X + 0.1, 2.3, 11.2, 3.0),
            [(data.quote, self._style(size, heading=True, italic=True))],
        )
        steps = [[Effect(quote.shape_id, "fade")]]
        if data.author:
            author = _textbox(slide, (MARGIN_X + 0.1, 5.5, 11.2, 0.7),
                              [(f"— {data.author}", self._style(22, color=t.accent, bold=True))])
            steps.append([Effect(author.shape_id, "rise")])
        add_animations(slide, steps, auto_first=True)
        return slide

    def section(self, data: Slide, number: int) -> PptxSlide:
        t = self.theme
        self._section_number += 1
        slide = self._new_slide(cover=True)
        _textbox(slide, (MARGIN_X, 1.6, 4.0, 1.6),
                 [(f"{self._section_number:02d}", self._style(96, color=t.cover_accent, heading=True, bold=True))])
        size = self._fit([data.title], 11.0, 1.8, max_pt=48, min_pt=30, heading=True, bold=True)
        title = _textbox(slide, (MARGIN_X, 3.3, 11.0, 1.9),
                         [(data.title, self._style(size, color=t.cover_text, heading=True, bold=True))])
        steps = [[Effect(title.shape_id, "rise")]]
        if data.subtitle:
            subtitle = _textbox(slide, (MARGIN_X, 5.3, 11.0, 1.0),
                                [(data.subtitle, self._style(22, color=t.cover_muted))])
            steps[0].append(Effect(subtitle.shape_id, "fade"))
        add_animations(slide, steps, auto_first=True)
        return slide

    def closing(self) -> None:
        t = self.theme
        slide = self._new_slide(cover=True)
        title = _textbox(
            slide, (MARGIN_X, 2.4, CONTENT_W, 1.5),
            [(CLOSING_TITLE, self._style(54, color=t.cover_text, heading=True, bold=True))],
            align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE,
        )
        subtitle = _textbox(
            slide, (MARGIN_X, 4.1, CONTENT_W, 0.8),
            [(CLOSING_SUBTITLE, self._style(24, color=t.cover_muted))],
            align=PP_ALIGN.CENTER,
        )
        add_animations(slide, [[Effect(title.shape_id, "zoom"), Effect(subtitle.shape_id, "fade")]],
                       auto_first=True)

    def render(self) -> bytes:
        self.cover()
        handlers = {
            "bullets": self.bullets,
            "summary": lambda s, n: self.bullets(s, n, summary=True),
            "comparison": self.comparison,
            "timeline": self.timeline,
            "stats": self.stats,
            "quote": self.quote,
            "section": self.section,
        }
        for index, data in enumerate(self.data.slides):
            number = index + 2  # титульный слайд — первый
            slide = handlers.get(data.type, self.bullets)(data, number)
            if data.type not in ("section",):
                self._footer(slide, number)
            self._notes(slide, data.notes)
        self.closing()

        buffer = io.BytesIO()
        self.pptx.save(buffer)
        return buffer.getvalue()


def render_presentation(presentation: Presentation, theme: Theme) -> bytes:
    return _Renderer(presentation, theme).render()


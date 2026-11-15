"""Готовые файлы на выходе: реферат — DOCX, проект — ZIP, веб-страница — HTML с предпросмотром.

Модель отвечает обычным текстом (Markdown), а этот модуль собирает из ответа файлы:

- просили реферат, доклад, эссе, «в ворде» — ответ превращается в .docx с оформлением
  как для учебной работы (Times New Roman 14, полуторный интервал, поля, номера страниц);
- в ответе несколько файлов кода с именами (`Файл `src/app.py`:` перед блоком) или просили
  проект/архив — файлы собираются в .zip с папками;
- в ответе веб-страница (блок ```html с <html> или <body>) — отдельный .html, который
  в приложении можно сразу посмотреть.

Презентации делает учебный конвейер (study, как /slides) — здесь только распознаётся просьба.

Файлы хранятся в базе (user_files) под случайным токеном: по нему приложение скачивает
файл и показывает предпросмотр, а ссылки на файлы лежат в сообщении истории (attachments).
"""

from __future__ import annotations

import io
import json
import re
import secrets
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from database.models import UserFile
from services.code import code_blocks
from study.sources import safe_filename

MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "html": "text/html",
}
MIN_DOCUMENT_CHARS = 600  # короче — это не реферат, а ответ в чат
MAX_ZIP_FILES = 60
KEEP_FILES = 50  # сколько последних файлов пользователя хранить
FILE_TTL = timedelta(days=30)

# ---------- что просят ----------

_DOC_NOUNS = (
    r"реферат\w*|доклад\w*|эссе|сочинени\w*|курсов\w*|дипломн\w*|научн\w+\s+работ\w*|"
    r"стать[юяи]\b|отч[её]т\w*|документ\w*|резюме|заявлени\w*|служебн\w+\s+записк\w*|письм[оа]\b|"
    r"конспект\w*|инструкци\w*|договор\w*|протокол\w*"
)
_ACTION = (
    r"напиши(?:те)?|написать|сделай(?:те)?|сделать|подготовь(?:те)?|составь(?:те)?|"
    r"оформи(?:те)?|создай(?:те)?|сгенерируй(?:те)?|набросай|нужен|нужна|нужно|хочу|дай|пришли|скинь"
)
# Явно про файл Word — всегда документ.
_WORD_FILE_RE = re.compile(r"\bdocx\b|\.docx|\bв\s+ворд\w*|\bв\s+word\b|\bворд\w*\s+файл|\bфайл\w*\s+(?:в\s+)?(?:ворд|word)", re.IGNORECASE)
_STRONG_DOC_RE = re.compile(r"\b(?:реферат\w*|доклад\w*|эссе|сочинени\w*|курсов\w+\s+работ\w*|курсовую|курсовая|дипломн\w*)", re.IGNORECASE)
_DOC_REQUEST_RE = re.compile(rf"\b(?:{_ACTION})\b(?:\s+\S+){{0,4}}?\s+(?:{_DOC_NOUNS})", re.IGNORECASE)
# «Как написать реферат?» — вопрос о том, как писать, а не просьба написать.
_HOW_RE = re.compile(r"^\s*(?:как|почему|зачем|что\s+такое|чем\s+отличает)", re.IGNORECASE)

_PRESENTATION_RE = re.compile(
    rf"\b(?:{_ACTION}|собери)\b(?:\s+\S+){{0,4}}?\s+(?:презентаци\w*|слайд\w*)|\bpptx\b|"
    r"\bpower\s*point\b|\bпа?уэр\s*поинт\w*",
    re.IGNORECASE,
)
_PROJECT_RE = re.compile(
    rf"\bzip\b|\bархив\w*|\b(?:{_ACTION})\b(?:\s+\S+){{0,4}}?\s+(?:проект\w*|сайт\w*|приложени\w*|бот\w*|игр[уаы]\b|лендинг\w*)",
    re.IGNORECASE,
)


def wants_document(text: str) -> bool:
    """Просят готовый текстовый документ (реферат, доклад, «в ворде»)."""
    if not text or _HOW_RE.search(text):
        return bool(text) and bool(_WORD_FILE_RE.search(text))
    return bool(_WORD_FILE_RE.search(text) or _STRONG_DOC_RE.search(text) or _DOC_REQUEST_RE.search(text))


def wants_presentation(text: str) -> bool:
    return bool(text) and not _HOW_RE.search(text) and bool(_PRESENTATION_RE.search(text))


def wants_project(text: str) -> bool:
    return bool(text) and not _HOW_RE.search(text) and bool(_PROJECT_RE.search(text))


DOCUMENT_RULE = (
    "\nСобеседник просит готовый документ — бот сам приложит его файлом Word (.docx). "
    "Напиши документ целиком и сразу с первой строки: «# Название», затем разделы «## …» "
    "(для реферата — введение, основная часть из нескольких разделов, заключение и список "
    "литературы). Пиши связным текстом, абзацами; списки и таблицы — где они уместны. "
    "Без вступлений вроде «Вот реферат», без вопросов и предложений в конце — только сам "
    "документ. Не сокращай: объём — как у настоящей работы такого рода.\n"
)
PROJECT_RULE = (
    "\nСобеседник просит проект из файлов — бот соберёт их в ZIP-архив. Дай все нужные файлы "
    "целиком, без заглушек. Перед каждым блоком кода — отдельная строка с путём файла: "
    "Файл `src/main.py`: (пути относительно корня проекта). Добавь README.md с тем, как "
    "запустить. Если это сайт без сборки — главная страница index.html.\n"
)


# ---------- файлы из ответа ----------


@dataclass(frozen=True, slots=True)
class OutputFile:
    name: str
    data: bytes
    kind: str  # docx | pptx | zip | html
    preview: bool = False  # можно посмотреть в приложении (HTML)

    @property
    def mime(self) -> str:
        return MIME[self.kind]


_PATH_HINT_RE = re.compile(r"`?((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,10}|Dockerfile|Makefile|README)`?")
_COMMENT_NAME_RE = re.compile(
    r"^\s*(?:#|//|--|;|<!--|/\*)\s*(?:file(?:name)?|файл)?\s*:?\s*`?((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,10})`?\s*(?:-->|\*/)?\s*$",
    re.IGNORECASE,
)


def safe_path(path: str) -> str | None:
    """Путь внутри архива: только безопасные части, без «..» и абсолютных путей."""
    parts = []
    for part in str(path).replace("\\", "/").split("/"):
        part = re.sub(r"[^\w.+-]+", "_", part.strip()).strip("._") if part not in (".", "..") else ""
        if part:
            parts.append(part[:80])
    if not parts or "." not in parts[-1] and parts[-1] not in ("Dockerfile", "Makefile", "README"):
        return None
    return "/".join(parts[:8])


def _named_blocks(text: str) -> list[tuple[str, str, str]]:
    """(путь, язык, код) для блоков, у которых есть имя файла: в строке перед блоком
    («Файл `src/app.py`:», «### index.html») или комментарием в первой строке кода."""
    result = []
    for block in code_blocks(text):
        before = text[: block.start].rstrip().rsplit("\n", 1)
        line = before[-1].strip() if before else ""
        name = None
        if line and len(line) <= 160 and not line.startswith("```"):
            hints = _PATH_HINT_RE.findall(line)
            name = hints[-1] if hints else None
        if name is None:
            first = block.code.split("\n", 1)[0]
            if match := _COMMENT_NAME_RE.match(first):
                name = match.group(1)
        path = safe_path(name) if name else None
        if path:
            result.append((path, block.language, block.code))
    return result


def _html_page(text: str) -> tuple[str, str] | None:
    """(имя, код) полной веб-страницы из ответа; None — её нет."""
    named = {code: path for path, _, code in _named_blocks(text)}
    for block in code_blocks(text):
        code = block.code
        lower = code.lower()
        is_html = block.language in ("html", "htm") or named.get(code, "").lower().endswith((".html", ".htm"))
        if is_html and ("<html" in lower or "<body" in lower or "<!doctype" in lower):
            name = PurePosixPath(named.get(code, "index.html")).name
            return (name if name.lower().endswith((".html", ".htm")) else "index.html"), code
    return None


def _slug(text: str, fallback: str) -> str:
    words = re.findall(r"[\w-]+", text.lower())[:5]
    return "-".join(words)[:40] or fallback


_FILLER_WORDS_RE = re.compile(
    rf"\b(?:{_ACTION}|мне|нам|пожалуйста|плиз|проект\w*|zip|архив\w*|в\s+виде|файл\w*|код\w*|"
    r"полн\w+|готов\w+|простой|простую|простое|небольш\w+|с|на|для|и)\b",
    re.IGNORECASE,
)


def _project_name(request: str) -> str:
    """«Сделай мне проект бота на aiogram» → «бота-aiogram»."""
    words = re.findall(r"[\w-]+", _FILLER_WORDS_RE.sub(" ", request).lower())[:4]
    return "-".join(words)[:40] or "project"


def _title(markdown: str, fallback: str) -> str:
    for line in markdown.split("\n"):
        if match := re.match(r"^\s*#{1,2}\s+(.+?)\s*#*\s*$", line):
            return re.sub(r"[*_`]", "", match.group(1)).strip()
    return fallback


def _topic(request: str) -> str:
    """«Напиши реферат про Петра I» → «Петр I»: тема для имени файла."""
    text = re.sub(rf"^.*?\b(?:{_DOC_NOUNS})\b", "", request, count=1, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(?:на\s+тему|про|о|об|по)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:в\s+ворд\w*|в\s+word|docx|файлом)\b", "", text, flags=re.IGNORECASE)
    return " ".join(text.split()).strip(" .,:;!?«»\"") or "Документ"


def build_outputs(request: str, answer: str, *, code_mode: bool = False) -> list[OutputFile]:
    """Файлы, которые стоит приложить к ответу."""
    outputs: list[OutputFile] = []
    named = _named_blocks(answer)
    if len(named) >= 2:  # один файл архивом не нужен — он и так приходит файлом
        name = safe_filename(_project_name(request), ".zip", "project")
        outputs.append(OutputFile(name=name, data=make_zip(named), kind="zip"))
    if page := _html_page(answer):
        outputs.append(OutputFile(name=page[0], data=page[1].encode("utf-8"), kind="html", preview=True))
    code_share = sum(len(block.code) for block in code_blocks(answer))
    if (
        wants_document(request)
        and not code_mode
        and len(answer) >= MIN_DOCUMENT_CHARS
        and code_share < len(answer) / 2
    ):
        title = _title(answer, _topic(request))
        outputs.append(OutputFile(name=safe_filename(title, ".docx"), data=markdown_to_docx(answer), kind="docx"))
    return outputs


def make_zip(files: list[tuple[str, str, str]]) -> bytes:
    out = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, _, code in files[:MAX_ZIP_FILES]:
            name, number = path, 2
            while name.lower() in used:
                stem, dot, suffix = path.rpartition(".")
                name = f"{stem}_{number}.{suffix}" if dot else f"{path}_{number}"
                number += 1
            used.add(name.lower())
            archive.writestr(name, code.rstrip("\n") + "\n")
    return out.getvalue()


# ---------- Markdown → Word ----------

_INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|_[^_\s][^_]*_|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def _runs(paragraph, text: str, *, bold: bool = False) -> None:
    from docx.shared import Pt

    for part in _INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith(("**", "__")) and len(part) > 4:
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt(12)
        elif part.startswith(("*", "_")) and part.endswith(part[0]) and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            run.italic = True
        elif part.startswith("[") and "](" in part:
            label, _, url = part[1:-1].partition("](")
            run = paragraph.add_run(f"{label} ({url})")
        else:
            run = paragraph.add_run(part)
        if bold:
            run.bold = True


def _page_number(section) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if tag:
            element = OxmlElement("w:fldChar")
            element.set(qn("w:fldCharType"), tag)
        else:
            element = OxmlElement("w:instrText")
            element.set(qn("xml:space"), "preserve")
            element.text = text
        run._r.append(element)


def markdown_to_docx(markdown: str) -> bytes:
    """Markdown-ответ модели → документ Word с оформлением учебной работы."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    document = Document()
    section = document.sections[0]
    section.left_margin, section.right_margin = Cm(3), Cm(1.5)
    section.top_margin = section.bottom_margin = Cm(2)
    _page_number(section)

    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(14)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.ONE_POINT_FIVE
    normal.paragraph_format.space_after = Pt(0)
    for level, size in ((1, 16), (2, 14), (3, 14)):
        style = document.styles[f"Heading {level}"]
        style.font.name = "Times New Roman"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        fonts = style.element.rPr.rFonts
        for attribute in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            fonts.attrib.pop(qn(attribute), None)  # шрифт темы перебивает Times New Roman
        fonts.set(qn("w:eastAsia"), "Times New Roman")
        style.paragraph_format.space_before = Pt(12)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.keep_with_next = True

    lines = markdown.replace("\r\n", "\n").split("\n")
    index = 0
    first_heading = True

    def body(text: str):
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        paragraph.paragraph_format.first_line_indent = Cm(1.25)
        _runs(paragraph, text)
        return paragraph

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or re.fullmatch(r"[-*_]{3,}", stripped):
            index += 1
            continue
        if stripped.startswith("```"):
            code = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            paragraph = document.add_paragraph()
            run = paragraph.add_run("\n".join(code))
            run.font.name = "Consolas"
            run.font.size = Pt(11)
            paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
            continue
        if heading := re.match(r"^(#{1,6})\s+(.+?)\s*#*$", stripped):
            level = len(heading.group(1))
            text = re.sub(r"[*_`]", "", heading.group(2))
            if level == 1 and first_heading:
                paragraph = document.add_paragraph()
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = paragraph.add_run(text.upper() if len(text) < 80 else text)
                run.bold = True
                run.font.size = Pt(16)
                paragraph.paragraph_format.space_after = Pt(12)
            else:
                paragraph = document.add_heading(text, level=min(3, max(1, level - 1 if level > 1 else 1)))
                if level <= 2:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            first_heading = False
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{2,}", lines[index + 1]):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
                    rows.append(cells)
                index += 1
            width = max(len(row) for row in rows)
            table = document.add_table(rows=len(rows), cols=width)
            table.style = "Table Grid"
            for r, row in enumerate(rows):
                for c in range(width):
                    cell = table.cell(r, c)
                    cell.paragraphs[0].paragraph_format.first_line_indent = Cm(0)
                    _runs(cell.paragraphs[0], row[c] if c < len(row) else "", bold=r == 0)
            document.add_paragraph()
            continue
        if bullet := re.match(r"^(\s*)[-*•+]\s+(.+)$", line):
            paragraph = document.add_paragraph(style="List Bullet" if len(bullet.group(1)) < 2 else "List Bullet 2")
            _runs(paragraph, bullet.group(2))
            index += 1
            continue
        if number := re.match(r"^(\s*)\d+[.)]\s+(.+)$", line):
            paragraph = document.add_paragraph(style="List Number" if len(number.group(1)) < 2 else "List Number 2")
            _runs(paragraph, number.group(2))
            index += 1
            continue
        if stripped.startswith(">"):
            paragraph = body(stripped.lstrip("> ").strip())
            paragraph.paragraph_format.left_indent = Cm(1)
            for run in paragraph.runs:
                run.italic = True
            index += 1
            continue
        # Абзац: строки до пустой строки или до начала другого элемента.
        text = [stripped]
        index += 1
        while index < len(lines):
            nxt = lines[index].strip()
            if not nxt or re.match(r"^(#{1,6}\s|```|\||[-*•+]\s|\d+[.)]\s|>)", nxt):
                break
            text.append(nxt)
            index += 1
        body(" ".join(text))

    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


# ---------- хранилище ----------


@dataclass(frozen=True, slots=True)
class StoredFile:
    token: str
    name: str
    kind: str
    size: int
    preview: bool = False

    def as_dict(self) -> dict:
        return {"token": self.token, "name": self.name, "kind": self.kind, "size": self.size, "preview": self.preview}


def attachments_json(files: list[StoredFile]) -> str | None:
    return json.dumps([f.as_dict() for f in files], ensure_ascii=False) if files else None


def parse_attachments(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("token"), str)] if isinstance(data, list) else []


class FileStore:
    """Готовые файлы: скачивание из приложения по токену (без него файл не найти)."""

    def __init__(self, session_factory: async_sessionmaker, *, keep: int = KEEP_FILES, ttl: timedelta = FILE_TTL) -> None:
        self._session_factory = session_factory
        self._keep = keep
        self._ttl = ttl

    async def save(self, user_id: int, output: OutputFile) -> StoredFile:
        token = secrets.token_urlsafe(24)
        async with self._session_factory.begin() as session:
            session.add(UserFile(token=token, user_id=user_id, name=output.name[:200], kind=output.kind,
                                 mime=output.mime, data=output.data))
            await session.flush()
            old = (
                await session.scalars(
                    select(UserFile.id).where(UserFile.user_id == user_id).order_by(UserFile.id.desc()).offset(self._keep)
                )
            ).all()
            if old:
                await session.execute(delete(UserFile).where(UserFile.id.in_(old)))
            await session.execute(delete(UserFile).where(UserFile.created_at < datetime.now(timezone.utc) - self._ttl))
        return StoredFile(token=token, name=output.name, kind=output.kind, size=len(output.data), preview=output.preview)

    async def get(self, token: str) -> UserFile | None:
        if not token or len(token) > 64:
            return None
        async with self._session_factory() as session:
            return await session.scalar(select(UserFile).where(UserFile.token == token))

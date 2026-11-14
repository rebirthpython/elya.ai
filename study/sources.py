"""Извлечение текста из присланных файлов: .txt, .md, .docx, .pdf."""

from __future__ import annotations

import io
import re
from pathlib import PurePath

SUPPORTED_EXTENSIONS = (".txt", ".md", ".docx", ".pdf")
MAX_FILE_BYTES = 10 * 1024 * 1024
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_TRAILING_SPACES_RE = re.compile(r"[ \t]+\n")


class SourceError(Exception):
    """Файл не удалось прочитать; текст ошибки можно показать пользователю."""


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = _TRAILING_SPACES_RE.sub("\n", text)
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _docx_text(data: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise SourceError("PDF защищён паролем — сними защиту или пришли текст.")
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text(filename: str, data: bytes) -> str:
    extension = PurePath(filename.lower()).suffix
    if extension not in SUPPORTED_EXTENSIONS:
        raise SourceError(
            "Такой формат я не читаю. Подойдут " + ", ".join(SUPPORTED_EXTENSIONS) + " или просто текст."
        )
    if len(data) > MAX_FILE_BYTES:
        raise SourceError("Файл слишком большой (больше 10 МБ).")

    try:
        if extension == ".docx":
            text = _docx_text(data)
        elif extension == ".pdf":
            text = _pdf_text(data)
        else:
            text = _decode(data)
    except SourceError:
        raise
    except Exception as exc:  # повреждённый файл: библиотеки бросают самые разные ошибки
        raise SourceError("Не получилось прочитать файл — возможно, он повреждён.") from exc

    text = clean_text(text)
    if not text:
        hint = " Похоже, это скан: в нём картинки, а не текст." if extension == ".pdf" else ""
        raise SourceError("В файле не нашлось текста." + hint)
    return text


def safe_filename(title: str, extension: str, fallback: str = "Эля") -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", title).strip(" .")
    name = " ".join(name.split())[:60].rstrip(" .") or fallback
    return f"{name}{extension}"

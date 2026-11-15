"""Режим кода: чтение присланных файлов с кодом и длинные блоки кода из ответа — файлами.

Файл превращается в текст сообщения (имя + блок кода + вопрос), поэтому модель видит его
так же, как вставленный в чат код, а в истории он остаётся для следующих вопросов.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_CODE_FILE_BYTES = 1024 * 1024
DEFAULT_FILE_QUESTION = (
    "Разбери этот файл: коротко объясни, что делает код, и укажи на ошибки и слабые места, "
    "если они есть."
)

# Расширение файла → язык для блока кода.
_CODE_LANGUAGES = {
    "py": "python", "pyw": "python", "pyi": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "jsx",
    "ts": "typescript", "tsx": "tsx", "vue": "vue", "svelte": "svelte",
    "html": "html", "htm": "html", "css": "css", "scss": "scss", "less": "less",
    "java": "java", "kt": "kotlin", "kts": "kotlin", "scala": "scala", "groovy": "groovy",
    "gradle": "groovy", "swift": "swift", "dart": "dart",
    "c": "c", "h": "c", "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp", "cs": "csharp",
    "go": "go", "rs": "rust", "rb": "ruby", "php": "php", "lua": "lua", "pl": "perl", "r": "r",
    "ex": "elixir", "exs": "elixir", "hs": "haskell", "pas": "pascal", "asm": "asm",
    "sh": "bash", "bash": "bash", "zsh": "bash", "ps1": "powershell", "bat": "batch",
    "cmd": "batch", "sql": "sql", "graphql": "graphql", "proto": "protobuf", "tf": "hcl",
    "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml", "xml": "xml",
    "ini": "ini", "cfg": "ini", "conf": "", "env": "", "diff": "diff", "patch": "diff",
}
# Обычный текст: в режиме кода читается так же, но вне его не выдаёт «файл с кодом».
_TEXT_LANGUAGES = {"txt": "", "log": "", "md": "markdown", "csv": "csv"}
_SPECIAL_NAMES = {"dockerfile": "dockerfile", "makefile": "makefile", "cmakelists.txt": "cmake"}

# Язык блока кода из ответа модели → расширение файла.
_EXTENSIONS = {
    "python": "py", "py": "py", "python3": "py", "javascript": "js", "js": "js", "node": "js",
    "jsx": "jsx", "typescript": "ts", "ts": "ts", "tsx": "tsx", "vue": "vue", "html": "html",
    "css": "css", "scss": "scss", "java": "java", "kotlin": "kt", "kt": "kt", "scala": "scala",
    "swift": "swift", "dart": "dart", "c": "c", "cpp": "cpp", "c++": "cpp", "csharp": "cs",
    "cs": "cs", "c#": "cs", "go": "go", "golang": "go", "rust": "rs", "rs": "rs", "ruby": "rb",
    "rb": "rb", "php": "php", "lua": "lua", "perl": "pl", "r": "r", "bash": "sh", "sh": "sh",
    "shell": "sh", "zsh": "sh", "powershell": "ps1", "ps1": "ps1", "batch": "bat", "bat": "bat",
    "sql": "sql", "json": "json", "yaml": "yaml", "yml": "yaml", "toml": "toml", "xml": "xml",
    "ini": "ini", "markdown": "md", "md": "md", "diff": "diff", "graphql": "graphql",
    "protobuf": "proto", "hcl": "tf", "elixir": "ex", "haskell": "hs",
}

_FENCE_RUN_RE = re.compile(r"`{3,}")
_BLOCK_RE = re.compile(
    r"^[ \t]*```([\w+#.-]*)[ \t]*\n(.*?)\n[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE
)
_NAME_RE = re.compile(r"[\w.-]+\.[A-Za-z0-9]{1,10}|Dockerfile|Makefile")
_UNSAFE_NAME_RE = re.compile(r"[^\w.+-]+")


class CodeFileError(ValueError):
    """Файл нельзя прочитать как код: empty | too_large | binary."""


@dataclass(frozen=True, slots=True)
class CodeFile:
    name: str
    language: str
    text: str
    total_chars: int  # длина до обрезки

    @property
    def truncated(self) -> bool:
        return len(self.text) < self.total_chars


@dataclass(frozen=True, slots=True)
class CodeBlock:
    language: str
    code: str
    start: int  # позиция блока в ответе


def clean_filename(name: str | None) -> str:
    """Имя файла приходит от пользователя или модели: оставляем безопасное короткое имя."""
    base = PurePosixPath(str(name or "").replace("\\", "/")).name
    base = _UNSAFE_NAME_RE.sub("_", base).strip("._")[:80]
    return base or "file.txt"


def language_for(filename: str) -> str:
    """Язык для блока кода по имени файла; пустая строка — неизвестен."""
    name = clean_filename(filename).lower()
    if name in _SPECIAL_NAMES:
        return _SPECIAL_NAMES[name]
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    return _CODE_LANGUAGES.get(extension, _TEXT_LANGUAGES.get(extension, ""))


def looks_like_code(filename: str | None) -> bool:
    """По имени файла: это исходный код или конфиг (а не лекция в .txt или .pdf)."""
    name = clean_filename(filename).lower()
    return name in _SPECIAL_NAMES or ("." in name and name.rsplit(".", 1)[-1] in _CODE_LANGUAGES)


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings = ("utf-16",)
    elif b"\x00" in data:
        raise CodeFileError("binary")
    else:
        # cp1251 — старые файлы с Windows; он декодирует почти всё, поэтому идёт последним.
        encodings = ("utf-8-sig", "cp1251")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CodeFileError("binary")


def read_code_file(data: bytes, filename: str | None, *, max_chars: int) -> CodeFile:
    """Проверяет файл и возвращает его текст; слишком длинный обрезается по границе строки."""
    if len(data) > MAX_CODE_FILE_BYTES:
        raise CodeFileError("too_large")
    text = _decode(data).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")
    if not text.strip():
        raise CodeFileError("empty")
    total = len(text)
    if total > max_chars:
        cut = text.rfind("\n", 0, max_chars)
        text = text[: cut if cut > max_chars // 2 else max_chars].rstrip()
    name = clean_filename(filename)
    return CodeFile(name=name, language=language_for(name), text=text, total_chars=total)


def file_message(file: CodeFile, question: str = "") -> str:
    """Сообщение для модели и истории: имя файла, его содержимое и вопрос."""
    # Ограда длиннее любой серии ` внутри файла, иначе Markdown-файл разорвал бы блок.
    longest = max((len(run) for run in _FENCE_RUN_RE.findall(file.text)), default=2)
    fence = "`" * max(3, longest + 1)
    parts = [f"Файл `{file.name}`:\n{fence}{file.language}\n{file.text}\n{fence}"]
    if file.truncated:
        parts.append(
            f"(Файл длинный: здесь только первые {len(file.text)} из {file.total_chars} символов.)"
        )
    parts.append(question.strip() or DEFAULT_FILE_QUESTION)
    return "\n\n".join(parts)


def code_blocks(text: str) -> list[CodeBlock]:
    """Закрытые блоки ```код``` из ответа модели, по порядку."""
    return [
        CodeBlock(language=m.group(1).lower(), code=m.group(2).strip("\n"), start=m.start())
        for m in _BLOCK_RE.finditer(text)
    ]


def _hinted_name(text: str, block: CodeBlock) -> str | None:
    """Имя файла из строки прямо перед блоком («Файл `main.py`:»), если оно там есть."""
    lines = text[: block.start].rstrip().rsplit("\n", 1)
    line = lines[-1].strip() if lines else ""
    if not line or len(line) > 120:
        return None
    names = _NAME_RE.findall(line)
    return clean_filename(names[-1]) if names else None


_FILE_REQUEST_RE = re.compile(
    r"файл(?:ом|ами|ы)\b|(?:пришли|скинь|отправь|дай|сохрани)(?:те)?\s+(?:\S+\s+){0,2}?(?:файл|как\s+файл)|"
    r"\bв\s+виде\s+файл|\bas\s+a?\s*files?\b",
    re.IGNORECASE,
)


def wants_files(request: str) -> bool:
    """Просят прислать код файлами («пришли файлом», «скинь файлы»)."""
    return bool(request) and _FILE_REQUEST_RE.search(request) is not None


def is_code_name(name: str | None) -> bool:
    """Имя похоже на файл с кодом или текстом (bot.py, requirements.txt, Dockerfile)."""
    if not name:
        return False
    lower = name.lower()
    extension = lower.rsplit(".", 1)[-1] if "." in lower else ""
    return lower in _SPECIAL_NAMES or extension in _CODE_LANGUAGES or extension in _TEXT_LANGUAGES or extension in ("html", "htm", "md")


def attachments(
    text: str, *, min_lines: int = 50, min_chars: int = 3000, limit: int = 5, all_blocks: bool = False
) -> list[tuple[str, bytes]]:
    """Блоки кода из ответа как файлы (имя, содержимое): длинные (их неудобно копировать из
    чата), подписанные именем файла («Файл `bot.py`:») — любой длины, а если all_blocks
    (просили «пришли файлом») — все."""
    result: list[tuple[str, bytes]] = []
    used: set[str] = set()
    for block in code_blocks(text):
        hinted = _hinted_name(text, block)
        named = hinted is not None and is_code_name(hinted)
        long = block.code.count("\n") + 1 >= min_lines or len(block.code) >= min_chars
        if not (all_blocks or named or long):
            continue
        extension = _EXTENSIONS.get(block.language, "txt")
        original = name = hinted if named else f"code.{extension}"
        stem, dot, suffix = original.rpartition(".")
        number = 2
        while name.lower() in used:
            name = f"{stem}_{number}{dot}{suffix}" if dot else f"{original}_{number}"
            number += 1
        used.add(name.lower())
        result.append((name, (block.code + "\n").encode("utf-8")))
        if len(result) >= limit:
            break
    return result

"""Сколько строк кода в проекте: для экрана «Как я устроена?» в Mini App."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Исходники, которые пишут люди: код, стили, разметка, скрипты и документация.
CODE_SUFFIXES = frozenset({".py", ".js", ".css", ".html", ".sh", ".md"})
# Окружение, кэш, данные и сторонние пакеты к коду бота не относятся.
SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "data",
})


def count_code_lines(root: Path) -> int:
    total = 0
    for folder, dirs, files in os.walk(root):
        # Лишние папки отсекаются сразу, без обхода: в .venv тысячи файлов.
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1] not in CODE_SUFFIXES:
                continue
            try:
                with open(os.path.join(folder, name), "rb") as file:
                    total += sum(1 for _ in file)
            except OSError:
                continue
    return total


@lru_cache(maxsize=1)
def project_code_lines() -> int:
    """Считается один раз за запуск: код меняется только с обновлением и перезапуском."""
    return count_code_lines(PROJECT_ROOT)

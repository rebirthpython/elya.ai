"""Сколько строк кода в проекте: для экрана «Как я устроена?» в Mini App."""

from __future__ import annotations

import os
import subprocess
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


def _git_files(root: Path) -> list[str] | None:
    """Файлы проекта по git: посторонние файлы в папке бота не считаются. None — git нет."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return [os.path.join(root, name) for name in result.stdout.decode("utf-8", "replace").split("\0") if name]


def _walk_files(root: Path) -> list[str]:
    files: list[str] = []
    for folder, dirs, names in os.walk(root):
        # Лишние папки отсекаются сразу, без обхода: в .venv тысячи файлов.
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        files.extend(os.path.join(folder, name) for name in names)
    return files


def count_code_lines(root: Path, *, use_git: bool = True) -> int:
    files = (_git_files(root) if use_git else None) or _walk_files(root)
    total = 0
    for path in files:
        if os.path.splitext(path)[1] not in CODE_SUFFIXES:
            continue
        try:
            with open(path, "rb") as file:
                total += sum(1 for _ in file)
        except OSError:
            continue
    return total


@lru_cache(maxsize=1)
def project_code_lines() -> int:
    """Считается один раз за запуск: код меняется только с обновлением и перезапуском."""
    return count_code_lines(PROJECT_ROOT)

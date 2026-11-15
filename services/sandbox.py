"""Песочница для кода: Эля запускает Python, видит вывод и графики и исправляет ошибки.

Код никогда не выполняется в процессе бота. Каждый запуск — отдельный процесс в новой
пустой папке, без переменных окружения бота (токенов и ключей там нет), без ввода
с клавиатуры, с лимитами времени, процессора, памяти, размера файлов и объёма вывода.
Насколько сильно он отделён от сервера, зависит от способа (SANDBOX_BACKEND):

- docker — отдельный контейнер на каждый запуск: без сети (--network none), файловая
  система только для чтения (кроме папки запуска), лимиты памяти и числа процессов
  через cgroups, без root и без привилегий. Самая надёжная изоляция; образ собирается
  из sandbox/Dockerfile (numpy, pandas, matplotlib).
- bwrap — bubblewrap: свои пространства имён (сеть, процессы, пользователи), видны только
  системные библиотеки и папка запуска. Почти как контейнер, но без демона.
- process — обычный дочерний процесс: лимиты ресурсов, запреты через audit-хуки Python
  (сеть, запуск программ, ctypes, файлы вне папки), а если бот работает от root — ещё
  и чужой пользователь nobody. Подходит, когда нет ни Docker, ни bubblewrap (Termux,
  хостинг), но это базовая защита, а не полноценная изоляция.

auto (по умолчанию) выбирает лучшее из доступного; off выключает запуск кода.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import shutil
import signal
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

BACKENDS = ("docker", "bwrap", "process")
RUNNER = Path(__file__).with_name("sandbox_runner.py")
BOT_DIR = Path(__file__).resolve().parents[1]
RUNNER_NAME = ".runner.py"
MAIN_NAME = "main.py"
IMAGE_EXTENSIONS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
                    "webp": "image/webp", "svg": "image/svg+xml"}
# Какие пакеты для вычислений и графиков проверить при старте (их назовут модели).
PROBE_PACKAGES = ("numpy", "pandas", "matplotlib", "scipy", "sympy", "seaborn", "sklearn", "PIL")
_NOBODY = 65534
_PROBE_CODE = (
    "import importlib.util, json, sys\n"
    f"names = {list(PROBE_PACKAGES)!r}\n"
    "found = [n for n in names if importlib.util.find_spec(n) is not None]\n"
    "if 'matplotlib' in found:\n"
    "    import matplotlib\n"
    "    matplotlib.use('Agg')\n"
    "    import matplotlib.font_manager\n"
    "print(json.dumps({'python': sys.version.split()[0], 'packages': found}))\n"
)
ISOLATION_TEXT = {
    "docker": "отдельный Docker-контейнер без сети",
    "bwrap": "изолированное окружение bubblewrap без сети",
    "process": "отдельный процесс с лимитами (базовая защита)",
}


class SandboxError(Exception):
    """Песочница недоступна (не собран образ, нет bubblewrap и т. п.)."""


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    timeout: float = 15.0  # секунды настоящего времени на запуск
    memory_mb: int = 512
    file_mb: int = 16  # самый большой файл, который код может записать
    output_chars: int = 20000  # больше — процесс останавливается
    max_files: int = 8  # сколько файлов из папки запуска вернуть
    max_file_bytes: int = 8 * 1024 * 1024
    pids: int = 64  # процессов и потоков (Docker)
    cpus: float = 1.0  # ядер (Docker)


@dataclass(frozen=True, slots=True)
class SandboxFile:
    name: str
    data: bytes

    @property
    def extension(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""

    @property
    def is_image(self) -> bool:
        return self.extension in IMAGE_EXTENSIONS and self.extension != "svg"

    @property
    def mime(self) -> str:
        return IMAGE_EXTENSIONS.get(self.extension, "application/octet-stream")


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    files: tuple[SandboxFile, ...] = ()
    duration: float = 0.0
    timed_out: bool = False
    output_cut: bool = False  # вывода слишком много — процесс остановлен
    reason: str = ""  # cpu | memory | file | crash | sandbox — почему остановлен
    backend: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.output_cut and not self.reason

    @property
    def images(self) -> tuple[SandboxFile, ...]:
        return tuple(f for f in self.files if f.is_image)


@dataclass(slots=True)
class _Process:
    argv: list[str]
    env: dict[str, str]
    cwd: str | None = None
    preexec: object = None
    container: str | None = None  # имя контейнера Docker — чтобы остановить его по таймауту
    extra_time: float = 0.0  # запуск контейнера тоже занимает время


@dataclass(slots=True)
class _Probe:
    python: str = ""
    packages: list[str] = field(default_factory=list)


def _env(home: str, path: str = "/usr/local/bin:/usr/bin:/bin") -> dict[str, str]:
    """Окружение кода: ничего из окружения бота (там токены и ключи)."""
    return {
        "PATH": path,
        "HOME": home,
        "TMPDIR": f"{home}/.tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": f"{home}/.mpl",
        # Одна нить вычислений: так numpy укладывается в лимит памяти и не забирает все ядра.
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


def _python_flags() -> list[str]:
    # -I: без PYTHON* из окружения и без пользовательских пакетов; -B: без .pyc; utf-8 всегда.
    return ["-I", "-B", "-X", "utf8"]


async def _quiet(*argv: str, timeout: float = 15.0) -> int:
    """Код возврата служебной команды (docker image inspect, bwrap true); -1 — не запустилась."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return -1
    try:
        return await asyncio.wait_for(process.wait(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return -1


class Sandbox:
    def __init__(
        self,
        backend: str,
        limits: SandboxLimits | None = None,
        *,
        python: str | None = None,
        image: str = "elya-sandbox",
        root: str | Path | None = None,
        max_parallel: int = 2,
        max_runs: int = 3,
        docker: str = "docker",
        bwrap: str = "bwrap",
    ) -> None:
        if backend not in BACKENDS:
            raise ValueError(f"unknown sandbox backend: {backend}")
        self.backend = backend
        self.limits = limits or SandboxLimits()
        self.python = python or sys.executable
        self.image = image
        self.root = Path(root) if root else Path(tempfile.gettempdir()) / "elya-sandbox"
        self.max_runs = max_runs  # запусков на один ответ Эли
        self._docker = docker
        self._bwrap = bwrap
        self._slots = asyncio.Semaphore(max(1, max_parallel))
        self._drop = False  # процесс от имени nobody (бот работает от root)
        self._probe = _Probe()
        self._fonts: Path | None = None  # кэш шрифтов matplotlib: без него каждый график +2 с

    # ---------- описание ----------

    @property
    def isolation(self) -> str:
        return ISOLATION_TEXT[self.backend]

    @property
    def packages(self) -> list[str]:
        return list(self._probe.packages)

    @property
    def python_version(self) -> str:
        return self._probe.python

    def describe(self) -> str:
        """Одна строка для /run и логов."""
        limits = self.limits
        packages = ", ".join(self.packages) or "только стандартная библиотека"
        return (
            f"{self.isolation}; Python {self.python_version or '3'}; {packages}; "
            f"до {limits.timeout:g} с и {limits.memory_mb} МБ памяти, без интернета"
        )

    # ---------- запуск ----------

    async def start(self) -> None:
        """Проверяет, что песочница работает, и узнаёт версию Python и пакеты.
        SandboxError — запуск кода недоступен (причина в сообщении)."""
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o711 if self.backend != "docker" else 0o755)
        if self.backend == "docker" and await _quiet(self._docker, "image", "inspect", self.image) != 0:
            raise SandboxError(
                f"нет Docker-образа {self.image}: собери его командой docker build -t {self.image} sandbox"
            )
        if self.backend == "process" and hasattr(os, "geteuid") and os.geteuid() == 0:
            # Бот работает от root — код запускается от имени nobody, если тому доступен интерпретатор.
            self._drop = True
        probe = await self._run(_PROBE_CODE, keep_fonts=True)
        if probe.exit_code != 0 and self._drop:
            logger.warning("Sandbox: the interpreter is not accessible to nobody, running as the bot user")
            self._drop = False
            probe = await self._run(_PROBE_CODE, keep_fonts=True)
        if probe.exit_code != 0:
            detail = (probe.stderr or probe.stdout or probe.reason or "no output").strip().splitlines()
            raise SandboxError(f"проверочный запуск не удался: {detail[-1] if detail else probe.reason}")
        try:
            info = json.loads(probe.stdout.strip().splitlines()[-1])
            self._probe = _Probe(python=str(info.get("python", "")), packages=list(info.get("packages", [])))
        except (ValueError, IndexError, AttributeError):
            raise SandboxError("проверочный запуск вернул непонятный ответ") from None

    async def run(self, code: str) -> RunResult:
        """Выполняет код и возвращает вывод, ошибки и созданные файлы (графики — картинки)."""
        async with self._slots:
            return await self._run(code)

    async def _run(self, code: str, *, keep_fonts: bool = False) -> RunResult:
        work = Path(tempfile.mkdtemp(prefix="run-", dir=self.root))
        started = time.monotonic()
        try:
            self._prepare(work, code)
            process = self._command(work)
            result = await self._execute(process, work)
            if keep_fonts:
                self._keep_fonts(work)
            files = self._collect(work)
            return RunResult(
                exit_code=result[0],
                stdout=result[1],
                stderr=result[2],
                files=files,
                duration=time.monotonic() - started,
                timed_out=result[3],
                output_cut=result[4],
                reason=result[5],
                backend=self.backend,
            )
        except OSError as exc:
            logger.warning("Sandbox run failed: %s", exc)
            return RunResult(exit_code=None, stderr=str(exc), reason="sandbox", backend=self.backend,
                             duration=time.monotonic() - started)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _prepare(self, work: Path, code: str) -> None:
        (work / MAIN_NAME).write_text(code, encoding="utf-8")
        shutil.copyfile(RUNNER, work / RUNNER_NAME)
        (work / ".tmp").mkdir()
        (work / ".mpl").mkdir()
        if self._fonts is not None and self._fonts.is_dir():
            for cache in self._fonts.glob("fontlist-*.json"):
                shutil.copyfile(cache, work / ".mpl" / cache.name)
        if self.backend == "docker" or self._drop:
            # Код работает от другого пользователя (nobody): папка запуска должна быть ему доступна.
            for path in (work, work / ".tmp", work / ".mpl"):
                os.chmod(path, 0o777)
            for path in [work / MAIN_NAME, work / RUNNER_NAME, *(work / ".mpl").iterdir()]:
                os.chmod(path, 0o644)
            if self._drop:
                for path in [work, work / ".tmp", work / ".mpl", *(work / ".mpl").iterdir()]:
                    os.chown(path, _NOBODY, _NOBODY)

    def _config(self, deny: list[str]) -> str:
        limits = self.limits
        return json.dumps({
            # Процессорное время чуть больше настоящего: обычно срабатывает таймаут, а лимит CPU
            # страхует, если бот не успел остановить процесс.
            "cpu": math.ceil(limits.timeout) + 1,
            "memory": limits.memory_mb,
            "file": limits.file_mb,
            "deny": deny,
            "guard": True,
        })

    def _command(self, work: Path) -> _Process:
        if self.backend == "docker":
            name = f"elya-sbx-{secrets.token_hex(6)}"
            limits = self.limits
            env = _env("/work")
            argv = [
                self._docker, "run", "--rm", "-i", "--name", name,
                "--network", "none",
                "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m",
                "--cpus", f"{limits.cpus:g}", "--pids-limit", str(limits.pids),
                "--read-only", "--tmpfs", "/tmp:rw,size=64m",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", f"{_NOBODY}:{_NOBODY}",
                "--ulimit", "nofile=256:256", "--ulimit", "core=0",
                "-v", f"{work}:/work:rw", "-w", "/work",
            ]
            for key, value in env.items():
                argv += ["-e", f"{key}={value}"]
            # В контейнере память держит cgroup, а лимит адресного пространства мешал бы numpy.
            argv += [self.image, "python", *_python_flags(), f"/work/{RUNNER_NAME}", self._config([])]
            return _Process(argv=argv, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, container=name,
                            extra_time=10.0)
        if self.backend == "bwrap":
            argv = [
                self._bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                "--hostname", "sandbox",
                "--ro-bind", "/usr", "/usr",
            ]
            for path in ("/lib", "/lib64", "/lib32", "/bin", "/sbin", "/etc/alternatives", "/etc/ld.so.cache",
                         "/etc/fonts", "/etc/localtime"):
                argv += ["--ro-bind-try", path, path]
            for path in self._python_dirs():
                argv += ["--ro-bind", path, path]
            argv += [
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--bind", str(work), "/work", "--chdir", "/work", "--clearenv",
            ]
            for key, value in _env("/work").items():
                argv += ["--setenv", key, value]
            argv += [self.python, *_python_flags(), f"/work/{RUNNER_NAME}", self._config([])]
            return _Process(argv=argv, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        # process: дочерний процесс в папке запуска; папка бота закрыта audit-хуками.
        deny = [str(BOT_DIR), str(Path.home())]
        preexec = _drop_privileges if self._drop else None
        argv = [self.python, *_python_flags(), str(work / RUNNER_NAME), self._config(deny)]
        return _Process(argv=argv, env=_env(str(work), os.environ.get("PATH", "/usr/bin:/bin")), cwd=str(work),
                        preexec=preexec)

    def _python_dirs(self) -> list[str]:
        """Папки интерпретатора вне /usr (виртуальное окружение, /opt/python) — для bwrap."""
        if self.python == sys.executable:
            candidates = {sys.prefix, sys.base_prefix}
        else:
            # /opt/venv/bin/python → /opt/venv, и то же для настоящего файла интерпретатора.
            candidates = {os.path.dirname(os.path.dirname(path))
                          for path in (os.path.abspath(self.python), os.path.realpath(self.python))}
        dirs = []
        for path in sorted(os.path.realpath(p) for p in candidates):
            if path in ("/", "/usr") or path.startswith("/usr/") or not os.path.isdir(path):
                continue
            dirs.append(path)
        return dirs

    async def _execute(self, spec: _Process, work: Path) -> tuple[int | None, str, str, bool, bool, str]:
        """(код выхода, stdout, stderr, таймаут, вывод обрезан, причина остановки)."""
        limits = self.limits
        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd,
            env=spec.env,
            start_new_session=True,
            preexec_fn=spec.preexec,  # noqa: PLW1509 — только setuid/setgid, без логики
        )
        cap = limits.output_chars * 4  # байты: кириллица — 2 байта на символ
        overflow = asyncio.Event()
        buffers = (bytearray(), bytearray())

        async def pump(stream: asyncio.StreamReader, buffer: bytearray) -> None:
            while chunk := await stream.read(65536):
                room = cap - len(buffer)
                if room > 0:
                    buffer += chunk[:room]
                if len(chunk) > room:
                    overflow.set()

        pumps = asyncio.gather(pump(process.stdout, buffers[0]), pump(process.stderr, buffers[1]))
        waiter = asyncio.ensure_future(process.wait())
        flooded = asyncio.ensure_future(overflow.wait())
        timed_out = output_cut = False
        try:
            done, _ = await asyncio.wait({waiter, flooded}, timeout=limits.timeout + spec.extra_time,
                                         return_when=asyncio.FIRST_COMPLETED)
            if waiter not in done:
                output_cut = flooded in done
                timed_out = not output_cut
                await self._kill(process, spec)
                await waiter
        finally:
            flooded.cancel()
            if not waiter.done():
                await self._kill(process, spec)
            try:
                await asyncio.wait_for(pumps, 5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — вывод уже прочитан, сколько успели
                pumps.cancel()
        code = process.returncode
        reason = "" if timed_out or output_cut else _stop_reason(code, self.backend)
        if spec.container and code in (125, 126, 127) and not buffers[0]:
            reason = "sandbox"  # Docker не смог запустить контейнер
        return (
            code,
            _decode(buffers[0], limits.output_chars),
            _decode(buffers[1], limits.output_chars),
            timed_out,
            output_cut,
            reason,
        )

    async def _kill(self, process: asyncio.subprocess.Process, spec: _Process) -> None:
        if spec.container:
            await _quiet(self._docker, "kill", spec.container, timeout=10)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    def _keep_fonts(self, work: Path) -> None:
        caches = list((work / ".mpl").glob("fontlist-*.json"))
        if not caches:
            return
        target = self.root / "mpl-fonts"
        target.mkdir(exist_ok=True)
        for cache in caches:
            shutil.copyfile(cache, target / cache.name)
        self._fonts = target

    def _collect(self, work: Path) -> tuple[SandboxFile, ...]:
        """Файлы, которые создал код (графики первыми). Ссылки и служебные папки пропускаются."""
        limits = self.limits
        found: list[tuple[int, float, str, Path]] = []
        for path in _walk(work, depth=2):
            relative = path.relative_to(work).as_posix()
            if relative in (MAIN_NAME, RUNNER_NAME):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > limits.max_file_bytes or info.st_size == 0:
                continue
            extension = relative.rsplit(".", 1)[-1].lower() if "." in relative else ""
            found.append((0 if extension in IMAGE_EXTENSIONS else 1, info.st_mtime, relative, path))
        found.sort(key=lambda item: (item[0], item[1], item[2]))
        files = []
        for _, _, relative, path in found[: limits.max_files]:
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as file:
                    data = file.read(limits.max_file_bytes + 1)
            except OSError:
                continue
            if len(data) <= limits.max_file_bytes:
                files.append(SandboxFile(name=relative, data=data))
        return tuple(files)


def _walk(root: Path, depth: int):
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.is_dir(follow_symlinks=False):
            if depth > 1:
                yield from _walk(Path(entry.path), depth - 1)
        else:
            yield Path(entry.path)


def _drop_privileges() -> None:  # pragma: no cover — выполняется в дочернем процессе
    os.setgroups([])
    os.setgid(_NOBODY)
    os.setuid(_NOBODY)


def _decode(data: bytes, limit: int) -> str:
    text = bytes(data).decode("utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit]


def _stop_reason(code: int | None, backend: str) -> str:
    """Почему процесс остановился, если не сам: процессор, память, размер файла, падение."""
    if code is None or code >= 0 and backend != "docker":
        return ""
    number = -code if code < 0 else code - 128 if code > 128 else 0
    if number == signal.SIGXCPU:
        return "cpu"
    if number == signal.SIGXFSZ:
        return "file"
    if number == signal.SIGKILL:
        return "memory"  # таймаут отмечается отдельно; иначе это OOM killer
    if number in (signal.SIGSEGV, signal.SIGBUS, signal.SIGABRT, signal.SIGILL):
        return "crash"
    return ""


async def detect_backend(choice: str, *, image: str = "elya-sandbox", docker: str = "docker",
                         bwrap: str = "bwrap") -> str | None:
    """Способ запуска: заданный явно или лучший доступный (auto); None — выключено."""
    choice = (choice or "auto").strip().lower()
    if choice in ("off", "none", "false", "0", "disabled"):
        return None
    if choice in BACKENDS:
        return choice
    if shutil.which(docker) and await _quiet(docker, "image", "inspect", image, timeout=10) == 0:
        return "docker"
    if shutil.which(bwrap) and await _quiet(
        bwrap, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
        "true", timeout=10,
    ) == 0:
        return "bwrap"
    return "process"


async def create_sandbox(settings) -> Sandbox | None:
    """Песочница по настройкам (SANDBOX_*); None — выключена или не запустилась."""
    backend = await detect_backend(settings.sandbox_backend, image=settings.sandbox_image)
    if backend is None:
        logger.info("Code sandbox: off")
        return None
    sandbox = Sandbox(
        backend,
        SandboxLimits(timeout=settings.sandbox_timeout, memory_mb=settings.sandbox_memory_mb),
        python=settings.sandbox_python or None,
        image=settings.sandbox_image,
        root=settings.sandbox_dir or None,
        max_runs=settings.sandbox_max_runs,
    )
    try:
        await sandbox.start()
    except (SandboxError, OSError) as exc:
        logger.warning("Code sandbox (%s) is unavailable: %s", backend, exc)
        return None
    level = logger.warning if backend == "process" else logger.info
    level("Code sandbox: %s", sandbox.describe())
    return sandbox

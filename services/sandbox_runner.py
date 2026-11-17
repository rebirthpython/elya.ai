"""Запускается внутри песочницы (services.sandbox), а не в процессе бота.

Порядок: лимиты ресурсов → запреты (сеть, процессы, чужие файлы) → код из main.py →
открытые графики matplotlib сохраняются картинками. Только стандартная библиотека:
скрипт должен работать в любом интерпретаторе, в том числе в контейнере.

Аргумент — JSON с настройками: cpu (с), memory (МБ), file (МБ), read (папки, которые
можно читать), guard (ставить ли запреты — в контейнере они дублируют изоляцию).
"""

import json
import os
import sys

CONFIG = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
WORK = os.path.realpath(os.getcwd())
MAIN = "main.py"
# Графики сохраняются с такими именами; бот пришлёт их картинками.
FIGURE_NAME = "figure_{}.png"


def _address_space_mb() -> int:
    """Сколько адресного пространства процесс уже занял (VmSize), МБ; 0 — неизвестно."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as status:
            for line in status:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _limit_resources() -> None:
    try:
        import resource
    except ImportError:  # не Unix — лимиты держит внешний таймаут
        return

    def cap(name: str, value: int, slack: int = 0) -> None:
        kind = getattr(resource, name, None)
        if kind is None or value <= 0:
            return
        try:
            _, hard = resource.getrlimit(kind)
            top = value + slack
            if hard != resource.RLIM_INFINITY:
                value, top = min(value, hard), min(top, hard)
            resource.setrlimit(kind, (value, top))
        except (ValueError, OSError):
            pass

    mb = 1024 * 1024
    memory = int(CONFIG.get("memory", 512))
    # На Android (распределитель Scudo) процесс уже на старте резервирует гигабайты адресного
    # пространства. Лимит ниже этого ломает любое новое выделение памяти и даже импорт модулей,
    # поэтому тогда лимит адресного пространства отсчитывается сверх уже занятого. Настоящую
    # память всё равно ограничивает RLIMIT_DATA.
    reserved = _address_space_mb()
    address_space = memory + reserved if reserved > memory // 2 else memory
    # Сначала SIGXCPU (понятная причина остановки), через секунду — SIGKILL.
    cap("RLIMIT_CPU", int(CONFIG.get("cpu", 10)), slack=1)
    cap("RLIMIT_AS", address_space * mb)
    cap("RLIMIT_DATA", memory * mb)
    cap("RLIMIT_FSIZE", int(CONFIG.get("file", 16)) * mb)
    cap("RLIMIT_NOFILE", 128)
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError, AttributeError):
        pass


def _inside(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def _system_roots() -> list[str]:
    """Где лежат Python и его пакеты: это можно читать (но не менять)."""
    import site
    import sysconfig

    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    roots.update(path for path in sysconfig.get_paths().values() if path)
    try:
        roots.update(site.getsitepackages())
    except AttributeError:
        pass
    roots.update(path for path in sys.path if path and os.path.isdir(path))
    return sorted(root for root in roots if root and root != "/")


def _shared_roots() -> list[str]:
    """Системные папки с библиотеками, шрифтами и часовыми поясами."""
    return ["/usr/lib", "/usr/lib64", "/usr/local/lib", "/usr/share", "/lib", "/lib64", "/etc/fonts",
            "/etc/localtime", "/etc/ld.so.cache"]


def _install_guard() -> None:
    """Запреты через audit-хуки Python: сеть, запуск программ, ctypes, файлы вне песочницы.

    Это вторая линия защиты: в Docker и bubblewrap код и так изолирован, а в режиме
    process хуки — главное, что не даёт коду прочитать файлы бота или выйти в сеть."""
    python = tuple(os.path.realpath(p) for p in _system_roots())
    readable = python + tuple(os.path.realpath(p) for p in [WORK, *_shared_roots(), *CONFIG.get("read", ())] if p) + (
        "/dev/null", "/dev/urandom", "/dev/random", "/dev/zero",
        "/proc/cpuinfo", "/proc/meminfo", "/sys/devices/system/cpu", "/sys/fs/cgroup",
    )
    # Папка бота и другие закрытые места: запрещены, даже если лежат внутри разрешённых.
    hidden = tuple(os.path.realpath(p) for p in CONFIG.get("deny", ()) if p)

    def closed(path: str) -> bool:
        # Python и его пакеты читать можно, даже если виртуальное окружение лежит в папке бота.
        return (_inside(path, hidden) and not _inside(path, python)) or not _inside(path, readable)

    writable = (WORK, "/dev/null")
    proc_self = os.path.realpath("/proc/self")

    def resolve(path) -> str | None:
        if isinstance(path, int):
            return None  # уже открытый дескриптор
        try:
            path = os.fsdecode(path)
        except TypeError:
            return None
        if path.startswith("/proc/self/"):
            return path
        try:
            return os.path.realpath(path)
        except (OSError, ValueError):
            return path

    def deny(what: str):
        raise PermissionError(f"В песочнице запрещено: {what}")

    blocked_prefixes = ("socket.", "subprocess.", "os.exec", "os.posix_spawn", "os.spawn",
                        "os.fork", "os.forkpty", "pty.", "ctypes.", "os.kill", "os.killpg",
                        "signal.pthread_kill", "webbrowser.", "urllib.", "http.", "ftplib.",
                        "smtplib.", "poplib.", "imaplib.", "nntplib.", "telnetlib.")
    blocked = {"os.system", "os.startfile", "os.setuid", "os.chroot", "resource.setrlimit",
               "resource.prlimit", "sys.setprofile", "sys.settrace",
               "shutil.chown", "os.chown", "os.truncate", "mmap.__new__",
               "gc.get_objects", "gc.get_referrers", "gc.get_referents", "sys.remote_exec"}
    write_events = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.link",
                    "os.symlink", "os.utime", "shutil.rmtree", "shutil.move", "shutil.copyfile",
                    "shutil.copytree", "shutil.make_archive", "shutil.unpack_archive"}
    list_events = {"os.listdir", "os.scandir", "glob.glob", "os.walk"}

    def hook(event: str, args: tuple) -> None:
        if event in blocked or event.startswith(blocked_prefixes):
            if event.startswith(("socket.", "urllib.", "http.", "ftplib.", "smtplib.", "poplib.", "imaplib.",
                                 "nntplib.", "telnetlib.", "webbrowser.")):
                deny("доступ в интернет и к сети")
            if event == "resource.setrlimit" or event == "resource.prlimit":
                deny("менять лимиты")
            deny(f"{event} (запуск программ, ctypes и системные вызовы)")
        if event == "open":
            path = resolve(args[0])
            if path is None:
                return
            mode = args[1] if len(args) > 1 and args[1] is not None else "r"
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
            )
            if path.startswith(("/proc/self/", proc_self)) and not writing:
                return
            if writing and not _inside(path, writable):
                deny(f"запись вне папки песочницы ({args[0]})")
            if closed(path):
                deny(f"чтение файлов вне песочницы ({args[0]})")
        elif event in write_events:
            for arg in args[:2]:
                path = resolve(arg) if isinstance(arg, (str, bytes, os.PathLike)) else None
                if path is not None and not _inside(path, writable):
                    deny(f"изменение файлов вне папки песочницы ({arg})")
        elif event in list_events:
            path = resolve(args[0]) if args and isinstance(args[0], (str, bytes, os.PathLike)) else WORK
            if path is not None and closed(path):
                deny(f"просмотр папок вне песочницы ({args[0]})")
        elif event == "sqlite3.connect":
            database = args[0] if args else ""
            path = resolve(database) if database not in (":memory:", "", b":memory:") else None
            if path is not None and not _inside(path, writable):
                deny("базы данных вне папки песочницы")

    try:
        # ctypes загружается заранее: pandas импортирует его при старте, а при импорте ctypes
        # открывает библиотеку Python. Дальше любые вызовы через ctypes запрещены хуком.
        import ctypes  # noqa: F401
    except ImportError:
        pass
    sys.addaudithook(hook)


def _wants_plots(source: str) -> bool:
    return any(word in source for word in ("matplotlib", "pyplot", "seaborn", ".plot(", ".hist(", "plt."))


_saved: set[int] = set()


def _save_figures() -> list[str]:
    """Открытые фигуры matplotlib → figure_N.png в папке песочницы."""
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None:
        return []
    names = []
    for number in plt.get_fignums():
        figure = plt.figure(number)
        key = id(figure)
        if key in _saved:
            continue
        _saved.add(key)
        name = FIGURE_NAME.format(len(_saved))
        try:
            figure.savefig(os.path.join(WORK, name), dpi=int(CONFIG.get("dpi", 130)), bbox_inches="tight")
            names.append(name)
        except Exception as exc:  # noqa: BLE001 — график не главное, вывод важнее
            print(f"[песочница] не удалось сохранить график: {exc}", file=sys.stderr)
    return names


def _prepare_plots() -> None:
    """Agg вместо окна; plt.show() сохраняет графики, а не пытается открыть окно."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    try:
        import warnings

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 — нет matplotlib: пусть код сам сообщит об ошибке импорта
        return
    warnings.filterwarnings("ignore", message=".*non-interactive.*")

    def show(*args, **kwargs) -> None:
        _save_figures()
        plt.close("all")

    plt.show = show


def _print_error(exc: BaseException) -> None:
    """Traceback только из кода пользователя (main.py), без строк самого раннера."""
    import traceback

    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != MAIN:
        tb = tb.tb_next
    lines = traceback.format_exception(type(exc), exc, tb)
    sys.stderr.write("".join(lines))


def main() -> int:
    with open(os.path.join(WORK, MAIN), encoding="utf-8") as file:
        source = file.read()
    _limit_resources()
    if _wants_plots(source):
        _prepare_plots()  # до запретов: matplotlib при первом импорте строит кэш шрифтов
    if CONFIG.get("guard", True):
        _install_guard()
    sys.argv = [MAIN]
    sys.path.insert(0, WORK)
    namespace = {"__name__": "__main__", "__file__": MAIN, "__builtins__": __builtins__}
    code = 0
    try:
        exec(compile(source, MAIN, "exec"), namespace)  # noqa: S102 — это и есть песочница
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if exc.code is not None and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
    except MemoryError:
        sys.stderr.write("MemoryError: не хватило памяти (лимит песочницы)\n")
        code = 1
    except BaseException as exc:  # noqa: BLE001 — любая ошибка кода — это результат запуска
        _print_error(exc)
        code = 1
    finally:
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
    _save_figures()
    return code


if __name__ == "__main__":
    sys.exit(main())

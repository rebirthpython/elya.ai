"""Песочница: настоящие запуски кода, изоляция от бота и лимиты ресурсов."""

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

from config.settings import ConfigError, load_settings
from services import sandbox as sandbox_module
from services.sandbox import BOT_DIR, Sandbox, SandboxLimits, create_sandbox, detect_backend

posix_only = pytest.mark.skipif(os.name != "posix", reason="limits and process groups need POSIX")
needs_matplotlib = pytest.mark.skipif(importlib.util.find_spec("matplotlib") is None, reason="no matplotlib")
HAS_BWRAP = shutil.which("bwrap") is not None


@pytest.fixture
async def box(tmp_path):
    sandbox = Sandbox("process", SandboxLimits(timeout=4, memory_mb=512, output_chars=5000), root=tmp_path / "sbx")
    await sandbox.start()
    return sandbox


@posix_only
async def test_runs_code_and_reports_version_and_packages(box):
    assert box.python_version == sys.version.split()[0]
    assert "отдельный процесс" in box.describe() and "без интернета" in box.describe()
    result = await box.run("print('привет', 2 ** 100)")
    assert result.ok and result.exit_code == 0
    assert result.stdout == "привет 1267650600228229401496703205376\n"
    assert result.files == () and result.backend == "process"


@posix_only
async def test_error_traceback_points_to_the_users_code(box):
    result = await box.run("def half(x):\n    return 1 / x\n\nprint(half(0))\n")
    assert result.exit_code == 1 and not result.ok
    assert 'File "main.py", line 2, in half' in result.stderr
    assert "ZeroDivisionError: division by zero" in result.stderr
    assert "runner" not in result.stderr and "exec(" not in result.stderr


@posix_only
async def test_exit_codes_and_no_keyboard_input(box):
    assert (await box.run("import sys\nsys.exit(3)")).exit_code == 3
    assert "EOFError" in (await box.run("input('имя?')")).stderr


@posix_only
async def test_bot_secrets_are_not_in_the_environment(box, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-secret")
    result = await box.run("import os\nprint(sorted(os.environ))\nprint(os.environ.get('BOT_TOKEN'))")
    assert "SECRET" not in result.stdout and "BOT_TOKEN" not in result.stdout.split("\n")[0]
    assert result.stdout.strip().endswith("None")


@posix_only
@pytest.mark.parametrize(
    "code, message",
    [
        ("import socket\nsocket.create_connection(('1.1.1.1', 80), timeout=1)", "доступ в интернет"),
        ("import urllib.request\nurllib.request.urlopen('http://example.com')", "доступ в интернет"),
        ("import subprocess\nsubprocess.run(['id'])", "subprocess.Popen"),
        ("import os\nos.system('id')", "os.system"),
        ("import os\nos.fork()", "os.fork"),
        ("import ctypes\nctypes.CDLL(None)", "ctypes"),
        (f"print(open({str(BOT_DIR / 'main.py')!r}).read())", "чтение файлов вне песочницы"),
        (f"import os\nprint(os.listdir({str(BOT_DIR)!r}))", "просмотр папок"),
        ("print(open('/etc/passwd').read())", "чтение файлов вне песочницы"),
        ("print(open('/proc/1/environ', 'rb').read())", "чтение файлов вне песочницы"),
        ("import os\nos.symlink('/etc/passwd', 'p.txt')", "изменение файлов вне папки"),
        ("open('/tmp/elya-escape.txt', 'w').write('x')", "запись вне папки"),
        ("import resource\nresource.setrlimit(resource.RLIMIT_CPU, (999, 999))", "менять лимиты"),
    ],
)
async def test_dangerous_things_are_blocked(box, code, message):
    result = await box.run(code)
    assert result.exit_code == 1
    assert "PermissionError: В песочнице запрещено" in result.stderr and message in result.stderr
    assert not Path("/tmp/elya-escape.txt").exists()


@posix_only
async def test_infinite_loop_is_stopped_by_the_timeout(tmp_path):
    sandbox = Sandbox("process", SandboxLimits(timeout=1.5), root=tmp_path)
    result = await sandbox.run("print('старт', flush=True)\nwhile True:\n    pass")
    assert result.timed_out and not result.ok
    assert result.stdout == "старт\n"  # что успело напечататься — видно
    assert 1.4 < result.duration < 6


@posix_only
async def test_endless_output_is_cut(tmp_path):
    sandbox = Sandbox("process", SandboxLimits(timeout=10, output_chars=2000), root=tmp_path)
    result = await sandbox.run("while True:\n    print('x' * 100)")
    assert result.output_cut and not result.timed_out
    assert len(result.stdout) <= 2000 and result.duration < 5


@posix_only
async def test_memory_and_file_size_limits(tmp_path):
    sandbox = Sandbox("process", SandboxLimits(timeout=10, memory_mb=256, file_mb=1), root=tmp_path)
    memory = await sandbox.run("data = bytearray(1024 ** 3)\nprint(len(data))")
    assert memory.exit_code == 1 and "MemoryError" in memory.stderr
    big = await sandbox.run("open('big.bin', 'wb').write(b'0' * (5 * 1024 * 1024))")
    assert big.exit_code != 0 and "File too large" in big.stderr
    assert all(len(item.data) <= 1024 * 1024 for item in big.files)  # не больше лимита


@posix_only
async def test_created_files_are_returned_and_the_folder_is_removed(box):
    code = (
        "import json, os\n"
        "open('data.csv', 'w').write('a,b\\n1,2\\n')\n"
        "os.makedirs('out', exist_ok=True)\n"
        "open('out/result.json', 'w').write(json.dumps({'ok': True}))\n"
        "open('.hidden', 'w').write('служебный')\n"
        "print(os.getcwd())\n"
    )
    result = await box.run(code)
    assert result.ok
    assert {item.name for item in result.files} == {"data.csv", "out/result.json"}
    assert next(f for f in result.files if f.name == "data.csv").data == b"a,b\n1,2\n"
    assert not Path(result.stdout.strip()).exists()  # папка запуска удалена


@posix_only
@needs_matplotlib
async def test_plots_come_back_as_png(box):
    code = (
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3], [3, 1, 2])\nplt.title('Продажи')\nplt.show()\n"
        "plt.figure()\nplt.bar(['a', 'b'], [1, 2])\n"  # без show — тоже сохраняется
        "print('готово')\n"
    )
    result = await box.run(code)
    assert result.ok, result.stderr
    assert [item.name for item in result.images] == ["figure_1.png", "figure_2.png"]
    assert all(item.data.startswith(b"\x89PNG") and item.mime == "image/png" for item in result.images)


@posix_only
async def test_runs_wait_for_a_free_slot(tmp_path):
    import asyncio

    sandbox = Sandbox("process", SandboxLimits(timeout=5), root=tmp_path, max_parallel=1)
    results = await asyncio.gather(*(sandbox.run(f"print({i})") for i in range(3)))
    assert [r.stdout for r in results] == ["0\n", "1\n", "2\n"]


@pytest.mark.skipif(not HAS_BWRAP, reason="bubblewrap is not installed")
async def test_bubblewrap_backend(tmp_path):
    if await detect_backend("auto", docker="no-such-docker") != "bwrap":
        pytest.skip("bubblewrap cannot create namespaces here")
    sandbox = Sandbox("bwrap", SandboxLimits(timeout=5), root=tmp_path)
    await sandbox.start()
    result = await sandbox.run("import os\nprint(os.getcwd(), os.path.exists('/home'), os.path.exists('/etc/passwd'))")
    assert result.stdout.strip() == "/work False False"  # видна только папка запуска и /usr
    blocked = await sandbox.run("import socket\nsocket.create_connection(('1.1.1.1', 80), timeout=1)")
    assert blocked.exit_code == 1


def test_docker_command_isolates_the_container(tmp_path):
    sandbox = Sandbox("docker", SandboxLimits(timeout=10, memory_mb=300, pids=32), image="elya-sandbox")
    spec = sandbox._command(tmp_path)
    argv = " ".join(spec.argv)
    for part in (
        "run --rm -i", "--network none", "--memory 300m", "--memory-swap 300m", "--pids-limit 32",
        "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--user 65534:65534",
        f"-v {tmp_path}:/work:rw", "elya-sandbox python -I -B",
    ):
        assert part in argv
    assert spec.container and spec.container.startswith("elya-sbx-")
    assert "BOT_TOKEN" not in argv and set(spec.env) == {"PATH"}


def test_bwrap_command_shares_no_network_and_only_system_folders(tmp_path):
    spec = Sandbox("bwrap")._command(tmp_path)
    argv = spec.argv
    assert "--unshare-all" in argv and "--die-with-parent" in argv and "--clearenv" in argv
    assert ["--bind", str(tmp_path), "/work"] == argv[argv.index("--bind"): argv.index("--bind") + 3]
    assert str(BOT_DIR) not in [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")] or (
        Path(sys.prefix).resolve().is_relative_to(BOT_DIR)
    )


async def test_backend_choice(monkeypatch):
    assert await detect_backend("off") is None
    assert await detect_backend("docker") == "docker"
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: None)
    assert await detect_backend("auto") == "process"


async def test_missing_docker_image_disables_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("SANDBOX_DIR", str(tmp_path))
    monkeypatch.setenv("SANDBOX_IMAGE", "elya-sandbox-missing")

    async def fail(*argv, timeout=15.0):
        return 1

    monkeypatch.setattr(sandbox_module, "_quiet", fail)
    monkeypatch.setenv("BOT_TOKEN", "42:TEST")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert await create_sandbox(load_settings(env_file=None)) is None


def test_settings(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "42:TEST")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    settings = load_settings(env_file=None)
    assert (settings.sandbox_backend, settings.sandbox_timeout, settings.sandbox_memory_mb) == ("auto", 15.0, 512)
    assert settings.sandbox_max_runs == 3 and settings.sandbox_image == "elya-sandbox"
    monkeypatch.setenv("SANDBOX_BACKEND", "Docker")
    monkeypatch.setenv("SANDBOX_TIMEOUT", "30")
    assert load_settings(env_file=None).sandbox_backend == "docker"
    assert load_settings(env_file=None).sandbox_timeout == 30.0
    monkeypatch.setenv("SANDBOX_BACKEND", "vm")
    with pytest.raises(ConfigError, match="SANDBOX_BACKEND"):
        load_settings(env_file=None)


@posix_only
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/status and mmap flags are Linux-specific")
async def test_works_when_the_process_reserves_gigabytes_at_start_like_android(tmp_path):
    """На Android (Scudo) любой процесс сразу резервирует гигабайты адресного пространства:
    лимит памяти не должен ломать запуск, но по-прежнему должен останавливать большие выделения."""
    wrapper = tmp_path / "android-python"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import ctypes, runpy, sys\n"
        "libc = ctypes.CDLL(None)\n"
        "libc.mmap.restype = ctypes.c_void_p\n"
        "libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]\n"
        "libc.mmap(None, 6 << 30, 0, 0x02 | 0x20 | 0x4000, -1, 0)  # PROT_NONE, MAP_PRIVATE|ANONYMOUS|NORESERVE\n"
        "args = sys.argv[1:]\n"
        "while args and args[0].startswith('-'):\n"
        "    args = args[2:] if args[0] == '-X' else args[1:]\n"
        "sys.argv = args\n"
        "runpy.run_path(args[0], run_name='__main__')\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    sandbox = Sandbox("process", SandboxLimits(timeout=10, memory_mb=256), python=str(wrapper), root=tmp_path / "sbx")
    await sandbox.start()
    result = await sandbox.run("import decimal, json\nprint(json.dumps([str(decimal.Decimal(1) / 3)]))")
    assert result.ok, result.stderr
    big = await sandbox.run("data = bytearray(1024 ** 3)\nprint(len(data))")
    assert big.exit_code == 1 and "MemoryError" in big.stderr

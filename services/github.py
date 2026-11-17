"""GitHub: подключить репозиторий, отправить туда файлы из ответа одним коммитом, прочитать файл.

Подключение — личный токен GitHub (classic со scope `repo` или fine-grained с правами
Contents: Read and write и, чтобы создавать репозитории, Administration: Read and write).
Токен можно задать в .env (GITHUB_TOKEN) или прислать боту командой /github — тогда он
хранится в базе зашифрованным (ключ выводится из BOT_TOKEN), а сообщение с ним бот удаляет.

Коммит из нескольких файлов делается через Git Data API: блобы → дерево → коммит →
перенос ветки. Пустой репозиторий сначала получает README через Contents API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from database.models import GitHubAccount

logger = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT = 30.0
MAX_FILE_BYTES = 1024 * 1024  # файл из репозитория в чат — до 1 МБ
MAX_TREE_PATHS = 300
MAX_PUSH_FILES = 60
REMEMBER_BUNDLES = 50
TOKEN_URL = "https://github.com/settings/tokens/new?scopes=repo&description=Elya"
FINE_GRAINED_URL = "https://github.com/settings/personal-access-tokens/new"
_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_REPO_RE = re.compile(r"^(?:https?://github\.com/)?([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class GitHubError(Exception):
    """Ошибка GitHub с текстом для пользователя. kind: auth | not_found | forbidden | conflict |
    invalid | rate_limit | unavailable | not_connected | no_repo."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def find_token(text: str) -> str | None:
    match = _TOKEN_RE.search(text or "")
    return match.group(0) if match else None


def parse_repo(text: str) -> tuple[str, str] | None:
    """«owner/name» или ссылка на репозиторий → (owner, name)."""
    match = _REPO_RE.match((text or "").strip())
    return (match.group(1), match.group(2)) if match else None


def clean_repo_name(text: str) -> str | None:
    """Имя нового репозитория: латиница, цифры, «-», «_», «.»."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-.")[:100]
    return name if name and _NAME_RE.match(name) else None


# ---------- шифрование токена в базе ----------


class TokenCipher:
    """Шифрование токена: HMAC-SHA256 в режиме счётчика + проверка целостности (HMAC).
    Только стандартная библиотека — работает и на сервере, и в Termux."""

    def __init__(self, secret: str) -> None:
        root = hashlib.sha256(("elya-github:" + secret).encode()).digest()
        self._enc = hmac.new(root, b"enc", hashlib.sha256).digest()
        self._mac = hmac.new(root, b"mac", hashlib.sha256).digest()

    def _stream(self, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            out += hmac.new(self._enc, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:length])

    def encrypt(self, text: str) -> str:
        data = text.encode()
        nonce = secrets.token_bytes(16)
        body = bytes(a ^ b for a, b in zip(data, self._stream(nonce, len(data))))
        tag = hmac.new(self._mac, nonce + body, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(nonce + tag + body).decode()

    def decrypt(self, value: str) -> str | None:
        try:
            raw = base64.urlsafe_b64decode(value.encode())
        except (ValueError, TypeError):
            return None
        nonce, tag, body = raw[:16], raw[16:32], raw[32:]
        if not hmac.compare_digest(tag, hmac.new(self._mac, nonce + body, hashlib.sha256).digest()[:16]):
            return None
        return bytes(a ^ b for a, b in zip(body, self._stream(nonce, len(body)))).decode(errors="replace")


# ---------- состояние ----------


@dataclass(frozen=True, slots=True)
class GitHubStatus:
    connected: bool
    login: str = ""
    repo: str = ""  # owner/name
    branch: str = ""
    from_env: bool = False  # токен из .env (GITHUB_TOKEN), отключить из чата нельзя

    def as_dict(self) -> dict:
        return {
            "connected": self.connected, "login": self.login, "repo": self.repo,
            "branch": self.branch, "from_env": self.from_env,
        }


@dataclass(frozen=True, slots=True)
class PushResult:
    repo: str
    branch: str
    files: int
    commit_url: str
    tree_url: str


@dataclass(frozen=True, slots=True)
class Bundle:
    """Файлы из ответа, которые можно отправить в репозиторий кнопкой."""

    user_id: int
    files: tuple[tuple[str, str], ...]  # (путь, содержимое)
    title: str


class GitHubService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        http: httpx.AsyncClient,
        *,
        secret: str,
        env_token: str = "",
        api: str = API,
    ) -> None:
        self._session_factory = session_factory
        self._http = http
        self._cipher = TokenCipher(secret)
        self._env_token = env_token.strip()
        self._api = api.rstrip("/")
        self._bundles: OrderedDict[int, Bundle] = OrderedDict()
        self._next_bundle = 0
        self._tree_cache: dict[str, tuple[float, list[str]]] = {}
        # Фоновые обновления списка файлов (ключ репозитория → задача).
        self._refreshing: dict[str, asyncio.Task] = {}

    # ---------- HTTP ----------

    async def _request(self, token: str, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = await self._http.request(method, f"{self._api}{path}", headers=headers, timeout=TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError("unavailable", "GitHub сейчас не отвечает. Попробуй чуть позже.") from exc
        if response.status_code < 400:
            return response
        try:
            detail = str(response.json().get("message", ""))
        except ValueError:
            detail = response.text[:200]
        logger.warning("GitHub %s %s → %s: %s", method, path, response.status_code, detail)
        status = response.status_code
        if status == 401:
            raise GitHubError("auth", "GitHub не принял токен: он неверный, отозван или истёк. Подключи заново: /github")
        if status == 403 and "rate limit" in detail.lower():
            raise GitHubError("rate_limit", "GitHub ограничил число запросов. Попробуй через несколько минут.")
        if status == 403:
            raise GitHubError("forbidden", "У токена нет прав на это действие. Нужны права Contents: Read and write "
                                           "(а для нового репозитория — Administration). Подробнее: /github")
        if status == 404:
            raise GitHubError("not_found", "Не нашла на GitHub: проверь имя репозитория и что токен даёт к нему доступ.")
        if status in (409, 422):
            raise GitHubError("conflict" if status == 409 else "invalid", f"GitHub отказал: {detail or 'конфликт'}.")
        raise GitHubError("unavailable", f"GitHub ответил ошибкой {status}. Попробуй чуть позже.")

    # ---------- аккаунт ----------

    async def _account(self, user_id: int) -> GitHubAccount | None:
        async with self._session_factory() as session:
            return await session.get(GitHubAccount, user_id)

    async def _token(self, user_id: int) -> tuple[str | None, bool]:
        row = await self._account(user_id)
        if row is not None and row.token:
            token = self._cipher.decrypt(row.token)
            if token:
                return token, False
        return (self._env_token or None), bool(self._env_token)

    async def status(self, user_id: int) -> GitHubStatus:
        row = await self._account(user_id)
        token, from_env = await self._token(user_id)
        if token is None:
            return GitHubStatus(connected=False)
        return GitHubStatus(
            connected=True,
            login=(row.login if row else "") or "",
            repo=(row.repo if row else "") or "",
            branch=(row.branch if row else "") or "",
            from_env=from_env,
        )

    async def connect(self, user_id: int, token: str) -> GitHubStatus:
        """Проверяет токен (GET /user) и сохраняет его зашифрованным."""
        token = token.strip()
        if not find_token(token):
            raise GitHubError("invalid", "Это не похоже на токен GitHub: он начинается с ghp_ или github_pat_.")
        data = (await self._request(token, "GET", "/user")).json()
        login = str(data.get("login", ""))
        async with self._session_factory.begin() as session:
            row = await session.get(GitHubAccount, user_id)
            if row is None:
                row = GitHubAccount(user_id=user_id)
                session.add(row)
            row.token = self._cipher.encrypt(token)
            row.login = login
        return await self.status(user_id)

    async def disconnect(self, user_id: int) -> None:
        async with self._session_factory.begin() as session:
            row = await session.get(GitHubAccount, user_id)
            if row is not None:
                await session.delete(row)

    async def _login(self, user_id: int, token: str) -> str:
        row = await self._account(user_id)
        if row is not None and row.login:
            return row.login
        login = str((await self._request(token, "GET", "/user")).json().get("login", ""))
        await self._save(user_id, login=login)
        return login

    async def _save(self, user_id: int, **values) -> None:
        async with self._session_factory.begin() as session:
            row = await session.get(GitHubAccount, user_id)
            if row is None:
                row = GitHubAccount(user_id=user_id, token="")
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)

    async def _need_token(self, user_id: int) -> str:
        token, _ = await self._token(user_id)
        if token is None:
            raise GitHubError("not_connected", "GitHub не подключён. Подключи: /github")
        return token

    async def _need_repo(self, user_id: int) -> tuple[str, str, str]:
        token = await self._need_token(user_id)
        status = await self.status(user_id)
        if not status.repo:
            raise GitHubError("no_repo", "Репозиторий не выбран. Выбери или создай: /repo")
        return token, status.repo, status.branch or "main"

    # ---------- репозитории ----------

    async def repos(self, user_id: int, limit: int = 10) -> list[str]:
        token = await self._need_token(user_id)
        response = await self._request(token, "GET", "/user/repos", params={"sort": "pushed", "per_page": limit})
        return [str(item["full_name"]) for item in response.json() if isinstance(item, dict) and "full_name" in item]

    async def use_repo(self, user_id: int, full_name: str) -> GitHubStatus:
        parsed = parse_repo(full_name)
        if parsed is None:
            raise GitHubError("invalid", "Напиши репозиторий как владелец/имя, например anna/my-bot.")
        token = await self._need_token(user_id)
        data = (await self._request(token, "GET", f"/repos/{parsed[0]}/{parsed[1]}")).json()
        permissions = data.get("permissions") or {}
        if permissions and not permissions.get("push"):
            raise GitHubError("forbidden", "В этот репозиторий у тебя нет прав на запись.")
        await self._save(user_id, repo=str(data["full_name"]), branch=str(data.get("default_branch") or "main"))
        return await self.status(user_id)

    async def create_repo(self, user_id: int, name: str, *, private: bool = True, description: str = "") -> GitHubStatus:
        clean = clean_repo_name(name)
        if clean is None:
            raise GitHubError("invalid", "Имя репозитория — латиница, цифры, «-» и «_», например my-telegram-bot.")
        token = await self._need_token(user_id)
        data = (
            await self._request(
                token, "POST", "/user/repos",
                json={"name": clean, "private": private, "auto_init": True, "description": description[:300]},
            )
        ).json()
        await self._save(user_id, repo=str(data["full_name"]), branch=str(data.get("default_branch") or "main"))
        return await self.status(user_id)

    async def tree(self, user_id: int) -> list[str]:
        """Пути файлов в ветке (кэш на минуту). Пустой репозиторий — пустой список."""
        token, repo, branch = await self._need_repo(user_id)
        key = f"{repo}@{branch}"
        cached = self._tree_cache.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        try:
            data = (await self._request(token, "GET", f"/repos/{repo}/git/trees/{branch}", params={"recursive": "1"})).json()
        except GitHubError as exc:
            if exc.kind in ("not_found", "conflict"):
                return []
            raise
        paths = [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"][:MAX_TREE_PATHS]
        self._tree_cache[key] = (time.monotonic(), paths)
        return paths

    async def read_file(self, user_id: int, path: str) -> tuple[str, str]:
        """(путь, текст) файла из выбранного репозитория."""
        token, repo, branch = await self._need_repo(user_id)
        path = path.strip().lstrip("/")
        if not path or ".." in path.split("/"):
            raise GitHubError("invalid", "Напиши путь к файлу, например bot.py или src/main.py.")
        data = (await self._request(token, "GET", f"/repos/{repo}/contents/{path}", params={"ref": branch})).json()
        if isinstance(data, list) or data.get("type") != "file":
            raise GitHubError("invalid", f"«{path}» — это папка, а не файл.")
        if int(data.get("size") or 0) > MAX_FILE_BYTES:
            raise GitHubError("invalid", "Файл больше 1 МБ — такой в чат не помещу.")
        raw = base64.b64decode(data.get("content") or "")
        if b"\x00" in raw[:4096]:
            raise GitHubError("invalid", "Это двоичный файл, а не текст.")
        return data.get("path", path), raw.decode("utf-8", errors="replace")

    async def push(self, user_id: int, files: list[tuple[str, str]], message: str) -> PushResult:
        """Один коммит со всеми файлами в выбранную ветку."""
        if not files:
            raise GitHubError("invalid", "Нечего отправлять: в ответе нет файлов с именами.")
        token, repo, branch = await self._need_repo(user_id)
        files = files[:MAX_PUSH_FILES]
        message = (message or "Update from Elya").strip()[:200]
        head = await self._head(token, repo, branch)
        if head is None:
            # Пустой репозиторий: Git Data API не работает, пока нет ни одного коммита.
            first_path, first_content = files[0]
            await self._request(
                token, "PUT", f"/repos/{repo}/contents/{first_path}",
                json={"message": message, "content": base64.b64encode(first_content.encode()).decode(), "branch": branch},
            )
            head = await self._head(token, repo, branch)
            files = files[1:]
            if head is None:
                raise GitHubError("unavailable", "Не получилось создать первый коммит. Попробуй ещё раз.")
            if not files:
                return self._result(repo, branch, 1, head)
        base = (await self._request(token, "GET", f"/repos/{repo}/git/commits/{head}")).json()
        tree = [
            {"path": path, "mode": "100755" if content.startswith("#!") else "100644", "type": "blob", "content": content}
            for path, content in files
        ]
        new_tree = (
            await self._request(token, "POST", f"/repos/{repo}/git/trees", json={"base_tree": base["tree"]["sha"], "tree": tree})
        ).json()
        commit = (
            await self._request(
                token, "POST", f"/repos/{repo}/git/commits",
                json={"message": message, "tree": new_tree["sha"], "parents": [head]},
            )
        ).json()
        await self._request(token, "PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": commit["sha"]})
        self._tree_cache.pop(f"{repo}@{branch}", None)
        return self._result(repo, branch, len(files), commit["sha"])

    @staticmethod
    def _result(repo: str, branch: str, count: int, sha: str) -> PushResult:
        return PushResult(
            repo=repo, branch=branch, files=count,
            commit_url=f"https://github.com/{repo}/commit/{sha}",
            tree_url=f"https://github.com/{repo}/tree/{branch}",
        )

    async def _head(self, token: str, repo: str, branch: str) -> str | None:
        try:
            data = (await self._request(token, "GET", f"/repos/{repo}/git/ref/heads/{branch}")).json()
        except GitHubError as exc:
            if exc.kind in ("not_found", "conflict"):
                return None
            raise
        return data.get("object", {}).get("sha")

    # ---------- файлы из ответа ----------

    def remember(self, user_id: int, files: list[tuple[str, str]], title: str) -> int:
        """Запомнить файлы из ответа для кнопки «Отправить в GitHub»."""
        self._next_bundle += 1
        self._bundles[self._next_bundle] = Bundle(user_id=user_id, files=tuple(files), title=title)
        while len(self._bundles) > REMEMBER_BUNDLES:
            self._bundles.popitem(last=False)
        return self._next_bundle

    def bundle(self, user_id: int, bundle_id: int) -> Bundle | None:
        bundle = self._bundles.get(bundle_id)
        return bundle if bundle is not None and bundle.user_id == user_id else None

    def bundle_any(self, bundle_id: int) -> Bundle | None:
        return self._bundles.get(bundle_id)

    def last_bundle(self, user_id: int) -> tuple[int, Bundle] | None:
        for bundle_id in reversed(self._bundles):
            if self._bundles[bundle_id].user_id == user_id:
                return bundle_id, self._bundles[bundle_id]
        return None

    async def _prompt_tree(self, user_id: int) -> list[str]:
        """Список файлов для промпта без ожидания GitHub: устаревший кэш отдаётся сразу,
        а свежий список загружается в фоне к следующему сообщению."""
        _, repo, branch = await self._need_repo(user_id)
        key = f"{repo}@{branch}"
        cached = self._tree_cache.get(key)
        if cached is None:
            return await self.tree(user_id)
        if time.monotonic() - cached[0] >= 60 and key not in self._refreshing:
            task = asyncio.create_task(self.tree(user_id))
            self._refreshing[key] = task

            def done(finished: asyncio.Task) -> None:
                self._refreshing.pop(key, None)
                if not finished.cancelled() and finished.exception() is not None:
                    logger.warning("GitHub file list refresh failed: %s", finished.exception())

            task.add_done_callback(done)
        return cached[1]

    async def prompt_block(self, user_id: int) -> str:
        """Раздел системного промпта: какой репозиторий подключён и какие в нём файлы."""
        status = await self.status(user_id)
        if not status.connected or not status.repo:
            return ""
        try:
            paths = await self._prompt_tree(user_id)
        except GitHubError:
            paths = []
        listing = ", ".join(paths[:80]) + (" …" if len(paths) > 80 else "") if paths else "репозиторий пока пустой"
        return (
            f"\nПодключён репозиторий GitHub {status.repo} (ветка {status.branch or 'main'}). Файлы: {listing}.\n"
            "Когда пишешь код для этого проекта, давай файлы целиком и перед каждым блоком — строку "
            "с путём от корня репозитория: Файл `bot.py`: — под ответом появится кнопка «Отправить в "
            "GitHub», и файлы уйдут одним коммитом. Сама ничего не отправляешь и не говоришь, что "
            "отправила. Чтобы ты увидела файл из репозитория, собеседник пишет /pull путь.\n"
        )


def commit_message(request: str) -> str:
    """Сообщение коммита из просьбы пользователя."""
    text = " ".join(str(request).split())
    text = re.sub(r"^(?:эля[,!]?\s+)", "", text, flags=re.IGNORECASE)
    return (text[:70].rstrip(" ,.:;") + ("…" if len(text) > 70 else "")) or "Update from Elya"

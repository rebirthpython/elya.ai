"""Разбор ссылок: статья — доступный текст, видео — доступные субтитры.

Пользователь присылает ссылку («https://… о чём тут?» или просто ссылку) — бот скачивает
страницу, вынимает основной текст (без меню, рекламы и комментариев) и кладёт его в
разговор как документ (services.documents). Дальше работает обычный механизм документов:
к каждому сообщению подбираются подходящие фрагменты, так что по статье можно задавать
вопросы и потом.

Что умеет:
- HTML-страницы: основной текст из <article>/<main>, а если их нет — из всей страницы;
  articleBody из JSON-LD, если там текста больше; заголовок, автор, дата, описание.
- PDF и обычный текст по прямой ссылке.
- YouTube: субтитры — сначала авторские, затем автоматические; русские, затем английские,
  затем любые. Фрагменты помечены таймкодами. Субтитров нет — честно говорим, что есть
  только название и описание.
- Страницы с <video><track src="….vtt"> и прямые ссылки на .vtt/.srt.

Только то, что доступно без входа: платный доступ, капчу и закрытые видео бот не обходит.
Адреса внутренней сети (localhost, 10.*, 192.168.* …) не открываются.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from services.documents import Chunk, DocumentError, ParsedDocument, _clean, _split, parse_document

logger = logging.getLogger(__name__)

MAX_LINKS = 3  # сколько ссылок из одного сообщения читать
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024  # PDF по ссылке
MAX_TEXT_CHARS = 400_000  # ~200 страниц: больше в разговоре всё равно не нужно
MAX_REDIRECTS = 5
SHORT_TEXT_CHARS = 400  # меньше — похоже, доступна только часть (платный доступ, сайт на скриптах)
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
VIDEO_CHUNK_CHARS = 1500

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

DEFAULT_ARTICLE_QUESTION = (
    "Выдели главное из статьи по ссылке: о чём она (1–2 предложения), ключевые мысли и факты "
    "по пунктам, вывод автора. В конце предложи 2–3 вопроса, которые можно задать по статье."
)
DEFAULT_VIDEO_QUESTION = (
    "Выдели главное из видео по его субтитрам: о чём оно (1–2 предложения), ключевые мысли "
    "по пунктам с таймкодами. В конце предложи 2–3 вопроса, которые можно задать по видео."
)
DEFAULT_LINKS_QUESTION = (
    "Коротко выдели главное по каждой ссылке, а потом скажи, чем материалы похожи и чем отличаются."
)

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'«»`]+", re.IGNORECASE)
_TRAILING = ".,;:!?…)]}»\"'"
_TIME_FORMAT = "{:02d}:{:02d}"


class LinkError(Exception):
    """Ссылку не удалось прочитать; текст можно показать пользователю и модели."""


@dataclass(frozen=True, slots=True)
class LinkStarted:
    """Событие ChatService.ask_stream: Эля читает ссылку (для индикатора в интерфейсе)."""

    url: str
    video: bool = False

    @property
    def host(self) -> str:
        return display_host(self.url)

    def status_text(self) -> str:
        if self.video:
            return f"🎬 Смотрю субтитры видео: {self.host}…"
        return f"🔗 Читаю ссылку: {self.host}…"


# ---------- Поиск ссылок в сообщении ----------


def find_urls(text: str, limit: int = MAX_LINKS) -> list[str]:
    """Ссылки из сообщения (без повторов, без завершающей пунктуации), не больше limit."""
    found: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0)
        # Закрывающая скобка — часть ссылки, только если открывающая тоже в ней (Википедия).
        while url and url[-1] in _TRAILING:
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            url = url[:-1]
        if url.lower().startswith("www."):
            url = "https://" + url
        parts = urlsplit(url)
        if not parts.hostname or "." not in parts.hostname:
            continue
        if url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


def strip_urls(text: str) -> str:
    """Сообщение без ссылок: остался ли вопрос."""
    return " ".join(_URL_RE.sub(" ", text or "").split())


def display_host(url: str) -> str:
    host = (urlsplit(url).hostname or url).lower()
    return host[4:] if host.startswith("www.") else host


def youtube_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    candidate = None
    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        if parts.path == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        else:
            match = re.match(r"/(?:shorts|embed|live|v)/([^/?#]+)", parts.path)
            candidate = match.group(1) if match else None
    if candidate and re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        return candidate
    return None


def is_video_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return youtube_id(url) is not None or path.endswith((".vtt", ".srt"))


# ---------- Безопасность: только публичные адреса ----------

Resolver = Callable[[str], Awaitable[list[str]]]


async def _system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def check_public(url: str, resolver: Resolver = _system_resolver) -> None:
    """LinkError, если адрес ведёт во внутреннюю сеть или схема не http(s)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LinkError("Я открываю только обычные ссылки http:// и https://.")
    if parts.username or parts.password:
        raise LinkError("Ссылки с логином и паролем я не открываю.")
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise LinkError("Это адрес внутренней сети — такие ссылки я не открываю.")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(a.split("%")[0]) for a in await resolver(host)]
        except (OSError, ValueError) as exc:
            raise LinkError(f"Сайт {host} не находится — проверь ссылку.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise LinkError("Это адрес внутренней сети — такие ссылки я не открываю.")


# ---------- Текст из HTML ----------

_VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
_SKIP_TAGS = frozenset(
    "script style noscript svg nav footer aside form button iframe template select "
    "dialog canvas object figure".split()
)
_BLOCK_TAGS = frozenset(
    "p div section article main h1 h2 h3 h4 h5 h6 li ul ol blockquote pre td th tr dd dt "
    "table header br hr figcaption caption details summary".split()
)
_HEADINGS = frozenset({"h1", "h2", "h3", "h4"})
_SKIP_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:comments?|sidebar|related|share|sharing|social|subscribe|subscription|"
    r"cookie|advert|ads|promo|banner|menu|breadcrumbs?|navbar|nav|footer|popup|modal|"
    r"newsletter|recommend\w*|widget|toolbar|tags)(?:$|[\s_-])",
    re.IGNORECASE,
)
_CONTENT_CLASS_RE = re.compile(
    r"article[_-]?(?:body|content|text)|post[_-]?(?:body|content)|entry[_-]?content|"
    r"story[_-]?body|news[_-]?text|tm-article-body",
    re.IGNORECASE,
)


@dataclass(slots=True)
class _Open:
    tag: str
    skip: bool  # служебная часть страницы: скрипты, меню по тегу/роли
    content: bool
    soft: bool = False  # похоже на служебное только по классу — может оказаться ошибкой


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Open] = []
        # текст, в основном тексте, заголовок, внутри «служебного» по классу
        self.blocks: list[tuple[str, bool, bool, bool]] = []
        self.current: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self.lang = ""
        self.ld_json: list[str] = []
        self.tracks: list[str] = []
        self._in_title = False
        self._in_ld = False
        self._ld_buffer: list[str] = []
        self._heading = False

    @property
    def skipping(self) -> bool:
        return any(item.skip for item in self.stack)

    @property
    def soft_skipping(self) -> bool:
        return any(item.soft for item in self.stack)

    @property
    def in_content(self) -> bool:
        return any(item.content for item in self.stack)

    def flush(self) -> None:
        text = " ".join("".join(self.current).split())
        if text:
            self.blocks.append((text, self.in_content, self._heading, self.soft_skipping))
        self.current = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: (value or "") for name, value in attrs}
        if tag == "html":
            self.lang = values.get("lang", "")[:10]
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or values.get("itemprop") or "").lower()
            if key and values.get("content"):
                self.meta.setdefault(key, values["content"].strip())
            return
        elif tag == "title":
            self._in_title = True
        elif tag == "track" and values.get("src"):
            if values.get("kind", "subtitles").lower() in ("subtitles", "captions"):
                self.tracks.append(values["src"])
        if tag == "script" and "ld+json" in values.get("type", "").lower():
            self._in_ld = True
            self._ld_buffer = []
        if tag in _VOID:
            if tag in _BLOCK_TAGS:
                self.flush()
            return
        if tag in _BLOCK_TAGS:
            self.flush()
        marker = f"{values.get('class', '')} {values.get('id', '')}"
        role = values.get("role", "").lower()
        skip = (
            tag in _SKIP_TAGS
            or role in ("navigation", "banner", "contentinfo", "complementary", "dialog")
            or values.get("aria-hidden") == "true"
            or "hidden" in values
        )
        soft = tag not in ("body", "html") and bool(_SKIP_CLASS_RE.search(marker)) and not _CONTENT_CLASS_RE.search(marker)
        content = (
            tag in ("article", "main")
            or role == "main"
            or values.get("itemprop") == "articleBody"
            or bool(_CONTENT_CLASS_RE.search(marker))
        )
        self.stack.append(_Open(tag, skip, content, soft))
        if tag in _HEADINGS:
            self._heading = True
        if tag == "li" and not self.skipping:
            self.current.append("• ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld_json.append("".join(self._ld_buffer))
        if tag in _VOID:
            return
        if not any(item.tag == tag for item in self.stack):
            return  # лишний закрывающий тег — так бывает в реальном HTML
        if tag in _BLOCK_TAGS:
            self.flush()
        while self.stack:
            item = self.stack.pop()
            if item.tag == tag:
                break
        if tag in _HEADINGS:
            self._heading = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._in_ld:
            self._ld_buffer.append(data)
            return
        if not self.skipping:
            self.current.append(data)

    def close(self) -> None:
        super().close()
        self.flush()


@dataclass(frozen=True, slots=True)
class PageText:
    title: str
    text: str
    description: str = ""
    author: str = ""
    published: str = ""
    site: str = ""
    lang: str = ""
    tracks: tuple[str, ...] = ()


def _ld_article(raw: list[str]) -> dict:
    """Первый объект JSON-LD со статьёй (articleBody или headline)."""
    def walk(node):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            yield node
            for key in ("@graph", "mainEntity"):
                if key in node:
                    yield from walk(node[key])

    for chunk in raw:
        try:
            data = json.loads(chunk.strip())
        except ValueError:
            continue
        for node in walk(data):
            if node.get("articleBody") or (node.get("headline") and "Article" in str(node.get("@type", ""))):
                return node
    return {}


def _ld_author(node: dict) -> str:
    author = node.get("author")
    if isinstance(author, list):
        author = author[0] if author else None
    if isinstance(author, dict):
        return str(author.get("name") or "")
    return str(author or "")


def extract_page(html: str) -> PageText:
    """Основной текст страницы и сведения о ней."""
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — html.parser почти всё прощает, но на всякий случай
        logger.debug("HTML parse error", exc_info=True)
    def size(blocks) -> int:
        return sum(len(block[0]) for block in blocks)

    clean = [block for block in parser.blocks if not block[3]]
    main = [block for block in clean if block[1]]
    # Основной текст (<article>, <main>), иначе вся страница без служебного; если служебным
    # по ошибке оказалось почти всё (класс «menu» у обёртки страницы) — вообще всё.
    chosen = main if size(main) >= 500 else clean if size(clean) >= SHORT_TEXT_CHARS else parser.blocks

    lines: list[str] = []
    for text, _, heading, _ in chosen:
        if lines and lines[-1].lstrip("# ") == text:
            continue
        lines.append(f"## {text}" if heading and len(text) < 200 else text)
    text = "\n\n".join(lines)

    meta = parser.meta
    article = _ld_article(parser.ld_json)
    body = " ".join(str(article.get("articleBody") or "").split())
    if len(body) > len(text) * 1.2:
        text = str(article.get("articleBody"))

    title = (
        meta.get("og:title")
        or str(article.get("headline") or "")
        or " ".join(parser.title.split())
        or meta.get("twitter:title", "")
    )
    return PageText(
        title=unescape(title).strip()[:200],
        text=_clean(text),
        description=unescape(meta.get("og:description") or meta.get("description") or "").strip()[:1000],
        author=(meta.get("author") or meta.get("article:author") or _ld_author(article)).strip()[:120],
        published=(meta.get("article:published_time") or str(article.get("datePublished") or ""))[:10],
        site=(meta.get("og:site_name") or "").strip()[:80],
        lang=parser.lang,
        tracks=tuple(parser.tracks),
    )


# ---------- Субтитры ----------


@dataclass(frozen=True, slots=True)
class Cue:
    start: float  # секунды
    text: str


def _timecode(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else _TIME_FORMAT.format(minutes, secs)


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    # В субтитрах YouTube сущности бывают экранированы дважды: &amp;#39;
    return " ".join(unescape(unescape(text)).split())


def parse_timedtext(data: str) -> list[Cue]:
    """XML субтитров YouTube: формат 1 (<text start=…>) или srv3 (<p t=… мс>)."""
    cues: list[Cue] = []
    for match in re.finditer(r"<text\b([^>]*)>(.*?)</text>", data, re.DOTALL):
        start = re.search(r'start="([\d.]+)"', match.group(1))
        text = _strip_tags(match.group(2))
        if start and text:
            cues.append(Cue(float(start.group(1)), text))
    if cues:
        return cues
    for match in re.finditer(r"<p\b([^>]*)>(.*?)</p>", data, re.DOTALL):
        start = re.search(r'\bt="(\d+)"', match.group(1))
        text = _strip_tags(match.group(2))
        if start and text:
            cues.append(Cue(int(start.group(1)) / 1000, text))
    return cues


_CUE_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}"
)


def parse_vtt(data: str) -> list[Cue]:
    """WebVTT и SRT: блоки «время --> время» и текст под ними."""
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", data.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            match = _CUE_TIME_RE.search(line)
            if match:
                hours, minutes, seconds = int(match.group(1) or 0), int(match.group(2)), int(match.group(3))
                text = _strip_tags(" ".join(lines[i + 1:]))
                if text:
                    cues.append(Cue(hours * 3600 + minutes * 60 + seconds, text))
                break
    return cues


def cue_chunks(cues: list[Cue], limit: int = VIDEO_CHUNK_CHARS) -> list[Chunk]:
    """Субтитры — фрагментами с таймкодами: «02:15–04:40». Повторы подряд убираются
    (у автоматических субтитров строки часто дублируются)."""
    chunks: list[Chunk] = []
    current: list[str] = []
    start = None
    size = 0
    last = ""
    total = 0
    for cue in cues:
        if cue.text == last:
            continue
        last = cue.text
        if start is None:
            start = cue.start
        current.append(cue.text)
        size += len(cue.text) + 1
        total += len(cue.text)
        if size >= limit:
            chunks.append(Chunk(f"{_timecode(start)}–{_timecode(cue.start)}", " ".join(current)))
            current, start, size = [], None, 0
        if total > MAX_TEXT_CHARS:
            break
    if current and start is not None:
        chunks.append(Chunk(f"{_timecode(start)}–{_timecode(cues[-1].start)}", " ".join(current)))
    return chunks


_LANGUAGE_NAMES = {"ru": "русские", "en": "английские", "uk": "украинские", "de": "немецкие",
                   "fr": "французские", "es": "испанские"}


def pick_track(tracks: list[dict]) -> dict | None:
    """Авторские субтитры лучше автоматических; русские, затем английские, затем любые."""
    def rank(track: dict) -> tuple[int, int]:
        language = str(track.get("languageCode") or "").split("-")[0].lower()
        auto = track.get("kind") == "asr"
        order = {"ru": 0, "en": 1}.get(language, 2)
        return (order, 1 if auto else 0)

    usable = [track for track in tracks if track.get("baseUrl")]
    return min(usable, key=rank) if usable else None


def track_label(track: dict) -> str:
    language = str(track.get("languageCode") or "").split("-")[0].lower()
    name = _LANGUAGE_NAMES.get(language, language or "на другом языке")
    return f"субтитры {name}" + (", автоматические" if track.get("kind") == "asr" else "")


# ---------- Чтение ссылок ----------


@dataclass(slots=True)
class _Fetched:
    url: str
    content_type: str
    data: bytes
    charset: str | None


def _decode_html(data: bytes, charset: str | None) -> str:
    candidates = [charset] if charset else []
    head = data[:4096].decode("ascii", errors="ignore")
    match = re.search(r"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", head, re.IGNORECASE)
    if match:
        candidates.append(match.group(1))
    candidates += ["utf-8", "cp1251"]
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _name(title: str, url: str) -> str:
    title = " ".join(title.split())
    return (title or display_host(url))[:120]


def _duration(seconds: int) -> str:
    return _timecode(seconds) if seconds else ""


class LinkReader:
    """Скачивает ссылку и превращает её в документ для разговора."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        resolver: Resolver = _system_resolver,
    ) -> None:
        # Отдельный клиент: свой User-Agent, переходы по редиректам — вручную, с проверкой адреса.
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT, headers=BROWSER_HEADERS, follow_redirects=False)
        self._resolver = resolver

    async def close(self) -> None:
        await self._http.aclose()

    async def _fetch(
        self, url: str, *, limit: int = MAX_PAGE_BYTES, method: str = "GET", **kwargs
    ) -> _Fetched:
        for _ in range(MAX_REDIRECTS + 1):
            await check_public(url, self._resolver)
            try:
                async with self._http.stream(method, url, **kwargs) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        if method == "POST":
                            method, kwargs = "GET", {}
                        continue
                    if response.status_code in (401, 403):
                        raise LinkError("Сайт не пускает без входа или блокирует ботов — текст недоступен.")
                    if response.status_code == 404:
                        raise LinkError("Страница не найдена (404) — проверь ссылку.")
                    if response.status_code == 429:
                        raise LinkError("Сайт просит подождать: слишком много запросов. Попробуй позже.")
                    if response.status_code >= 400:
                        raise LinkError(f"Сайт ответил ошибкой {response.status_code}.")
                    content_type = response.headers.get("content-type", "").lower()
                    declared = int(response.headers.get("content-length") or 0)
                    if declared > limit:
                        raise LinkError("По ссылке слишком большой файл — я читаю до 20 МБ.")
                    chunks: list[bytes] = []
                    size = 0
                    async for piece in response.aiter_bytes():
                        size += len(piece)
                        if size > limit:
                            if content_type.startswith("text/html"):
                                break  # страница огромная — хватит начала
                            raise LinkError("По ссылке слишком большой файл — я читаю до 20 МБ.")
                        chunks.append(piece)
                    return _Fetched(str(response.url), content_type, b"".join(chunks), response.charset_encoding)
            except httpx.TimeoutException as exc:
                raise LinkError("Сайт слишком долго не отвечает.") from exc
            except httpx.HTTPError as exc:
                raise LinkError("Не получилось открыть сайт — он недоступен или ссылка неверная.") from exc
        raise LinkError("Слишком много перенаправлений — страницу не открыть.")

    async def read(self, url: str) -> ParsedDocument:
        """Документ со статьёй или субтитрами. LinkError — с понятной причиной."""
        video = youtube_id(url)
        if video:
            return await self._youtube(url, video)
        fetched = await self._fetch(url, limit=MAX_FILE_BYTES)
        kind = fetched.content_type.split(";")[0].strip()
        path = urlsplit(fetched.url).path.lower()
        if kind == "application/pdf" or path.endswith(".pdf") or fetched.data[:5] == b"%PDF-":
            return self._pdf(url, fetched)
        if path.endswith((".vtt", ".srt")) or kind == "text/vtt":
            return self._subtitles_file(url, fetched)
        if kind in ("text/plain", "text/markdown"):
            text = _clean(_decode_html(fetched.data, fetched.charset))[:MAX_TEXT_CHARS]
            return self._article(url, PageText(title="", text=text))
        if kind and not ("html" in kind or "xml" in kind):
            raise LinkError(
                "По ссылке не статья, а файл другого типа. Я читаю веб-страницы, PDF и текст; "
                "картинку или документ можно прислать прямо в чат."
            )
        if len(fetched.data) > MAX_PAGE_BYTES:
            fetched.data = fetched.data[:MAX_PAGE_BYTES]
        html = _decode_html(fetched.data, fetched.charset)
        page = await asyncio.to_thread(extract_page, html)
        if len(page.text) < SHORT_TEXT_CHARS and page.tracks:
            # Страница с видео и субтитрами — читаем субтитры.
            try:
                return await self._track(url, page, urljoin(fetched.url, page.tracks[0]))
            except LinkError:
                pass
        return self._article(url, page)

    # --- статьи ---

    def _article(self, url: str, page: PageText) -> ParsedDocument:
        text = page.text[:MAX_TEXT_CHARS]
        partial = len(page.text) < SHORT_TEXT_CHARS
        if partial and page.description and page.description not in text:
            text = (page.description + "\n\n" + text).strip()
        if not text:
            raise LinkError(
                "На странице не нашлось текста: возможно, она собирается скриптами, закрыта "
                "платным доступом или просит войти. Скопируй текст статьи в чат — разберу его."
            )
        chunks = _article_chunks(text)
        head = []
        if page.title:
            head.append(f"Заголовок: {page.title}")
        head.append(f"Адрес: {url}")
        if page.site:
            head.append(f"Сайт: {page.site}")
        if page.author:
            head.append(f"Автор: {page.author}")
        if page.published:
            head.append(f"Дата: {page.published}")
        if partial:
            head.append(
                "Внимание: доступна только малая часть текста (платный доступ, вход или страница "
                "на скриптах) — предупреди об этом собеседника."
            )
        chunks.insert(0, Chunk("о статье", "\n".join(head)))
        info = f"статья с {display_host(url)}" + (", доступна только часть текста" if partial else "")
        return ParsedDocument(
            name=_name(page.title, url), kind="link", info=info, chunks=tuple(chunks), truncated=partial
        )

    def _pdf(self, url: str, fetched: _Fetched) -> ParsedDocument:
        filename = urlsplit(fetched.url).path.rsplit("/", 1)[-1] or "document.pdf"
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        try:
            parsed = parse_document(filename, fetched.data)
        except DocumentError as exc:
            raise LinkError(str(exc)) from exc
        return ParsedDocument(
            name=parsed.name, kind="pdf", info=f"{parsed.info}, по ссылке с {display_host(url)}",
            chunks=parsed.chunks, truncated=parsed.truncated,
        )

    # --- видео ---

    def _subtitles_file(self, url: str, fetched: _Fetched) -> ParsedDocument:
        cues = parse_vtt(_decode_html(fetched.data, fetched.charset))
        if not cues:
            raise LinkError("В файле субтитров не нашлось текста.")
        name = urlsplit(fetched.url).path.rsplit("/", 1)[-1] or "субтитры"
        return self._video_document(url, name, cues, "субтитры из файла")

    async def _track(self, url: str, page: PageText, track_url: str) -> ParsedDocument:
        fetched = await self._fetch(track_url, limit=MAX_PAGE_BYTES)
        cues = parse_vtt(_decode_html(fetched.data, fetched.charset))
        if not cues:
            raise LinkError("Субтитры пустые.")
        return self._video_document(url, page.title or display_host(url), cues, "субтитры со страницы",
                                    description=page.description)

    def _video_document(
        self, url: str, title: str, cues: list[Cue], source: str, *,
        author: str = "", description: str = "", length: int = 0, site: str = "",
    ) -> ParsedDocument:
        chunks = cue_chunks(cues)
        head = [f"Видео: {title}", f"Адрес: {url}"]
        if author:
            head.append(f"Автор: {author}")
        if length:
            head.append(f"Длительность: {_timecode(length)}")
        head.append(f"Текст ниже — {source}, с таймкодами.")
        if description:
            head.append(f"Описание: {description[:800]}")
        chunks.insert(0, Chunk("о видео", "\n".join(head)))
        info = ", ".join(part for part in (site or "видео", _duration(length), source) if part)
        return ParsedDocument(name=_name(title, url), kind="video", info=info, chunks=tuple(chunks))

    async def _youtube(self, url: str, video_id: str) -> ParsedDocument:
        player = await self._youtube_player(video_id)
        details = player.get("videoDetails") or {}
        title = str(details.get("title") or "")
        author = str(details.get("author") or "")
        description = str(details.get("shortDescription") or "")
        try:
            length = int(details.get("lengthSeconds") or 0)
        except ValueError:
            length = 0
        status = (player.get("playabilityStatus") or {}).get("status", "OK")
        tracks = (
            ((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks")
            or []
        )
        track = pick_track(tracks)
        if track is not None:
            base = re.sub(r"&fmt=[^&]*", "", str(track["baseUrl"]))
            try:
                fetched = await self._fetch(urljoin("https://www.youtube.com", base), limit=MAX_PAGE_BYTES)
                cues = parse_timedtext(fetched.data.decode("utf-8", errors="replace"))
            except LinkError:
                logger.warning("YouTube subtitles download failed for %s", video_id, exc_info=True)
                cues = []
            if cues:
                return self._video_document(
                    url, title or f"YouTube {video_id}", cues, track_label(track),
                    author=author, description=description, length=length, site="YouTube",
                )
        if not title and status not in ("OK", ""):
            reason = (player.get("playabilityStatus") or {}).get("reason") or ""
            raise LinkError(
                "YouTube не отдаёт это видео" + (f" ({reason})" if reason else "")
                + " — возможно, оно закрыто, удалено или с ограничением по возрасту."
            )
        if not title and not description:
            raise LinkError("Не получилось получить сведения о видео с YouTube.")
        # Субтитров нет — есть только название и описание.
        head = [f"Видео: {title}", f"Адрес: {url}"]
        if author:
            head.append(f"Автор: {author}")
        if length:
            head.append(f"Длительность: {_timecode(length)}")
        head.append(
            "Внимание: у этого видео нет доступных субтитров, поэтому содержание самого видео "
            "неизвестно — есть только название и описание. Обязательно скажи об этом собеседнику."
        )
        chunks = [Chunk("о видео", "\n".join(head))]
        if description.strip():
            chunks += [Chunk("описание", part) for part in _split(_clean(description))]
        info = ", ".join(part for part in ("YouTube", _duration(length), "субтитров нет — только описание") if part)
        return ParsedDocument(name=_name(title, url), kind="video", info=info, chunks=tuple(chunks), truncated=True)

    async def _youtube_player(self, video_id: str) -> dict:
        """Сведения о видео (videoDetails, captions): страница видео, затем API плеера."""
        watch = f"https://www.youtube.com/watch?v={video_id}&hl=ru"
        cookies = {"Cookie": "CONSENT=YES+cb; SOCS=CAI"}  # без страницы согласия на cookie в ЕС
        page = await self._fetch(watch, headers=cookies)
        html = page.data.decode("utf-8", errors="replace")
        player = _initial_player(html)
        tracks = ((player.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}).get("captionTracks")
        if tracks:
            return player
        # Ссылки на субтитры со страницы часто требуют токен — клиент Android получает их без него.
        key = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([\w-]+)"', html)
        if key:
            try:
                answer = await self._fetch(
                    f"https://www.youtube.com/youtubei/v1/player?key={key.group(1)}",
                    method="POST",
                    json={
                        "context": {"client": {"clientName": "ANDROID", "clientVersion": "20.10.38", "hl": "ru"}},
                        "videoId": video_id,
                    },
                    headers=cookies,
                )
                api = json.loads(answer.data.decode("utf-8", errors="replace"))
                if isinstance(api, dict):
                    api_tracks = (
                        (api.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}
                    ).get("captionTracks")
                    if api_tracks or not player:
                        if not api.get("videoDetails") and player.get("videoDetails"):
                            api["videoDetails"] = player["videoDetails"]
                        return api
            except (LinkError, ValueError):
                logger.info("YouTube player API unavailable for %s", video_id, exc_info=True)
        if not player:
            meta = extract_page(html)
            player = {"videoDetails": {"title": meta.title, "shortDescription": meta.description}}
        return player


def _initial_player(html: str) -> dict:
    match = re.search(r"ytInitialPlayerResponse\s*=\s*", html)
    if not match:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _article_chunks(text: str) -> list[Chunk]:
    """Статья — фрагментами; пометка — раздел (подзаголовок), если он есть."""
    chunks: list[Chunk] = []
    section = ""
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        for part in _split(buffer.strip()):
            if part:
                label = f"раздел «{section[:60]}»" if section else f"фрагмент {len(chunks) + 1}"
                chunks.append(Chunk(label, part))
        buffer = ""

    for paragraph in text.split("\n\n"):
        if paragraph.startswith("## "):
            flush()
            section = paragraph[3:].strip()
            buffer = section + "\n\n"
            continue
        if len(buffer) + len(paragraph) > 1500 and buffer.strip():
            flush()
        buffer += paragraph + "\n\n"
    flush()
    return chunks


def failure_block(failures: list[tuple[str, str]]) -> str:
    """Правило для модели: эти ссылки прочитать не удалось."""
    lines = "\n".join(f"- {url}: {reason}" for url, reason in failures)
    return (
        "\nСобеседник прислал ссылки, но бот не смог их прочитать:\n"
        f"{lines}\n"
        "Скажи об этом честно и коротко объясни причину. Не пересказывай содержимое по памяти "
        "и не выдумывай его; предложи вставить текст статьи в чат или прислать файл.\n"
    )

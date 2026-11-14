"""Поиск в интернете (выдача Google) и ссылки на источники в ответах Эли.

Модель сама решает, нужен ли поиск: вместо ответа она выводит одну строку
<search>запрос</search>. Бот ищет запрос в Google через внешний API, передаёт модели
пронумерованные результаты, а к её ответу со сносками [1], [2] добавляет список
источников со ссылками. Собственный API поиска Google (Custom Search JSON API) закрыт
для новых пользователей и отключается 1 января 2027 года, поэтому выдача Google
берётся у сервисов Serper.dev и SerpApi; провайдеры перебираются по приоритету.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

from config.settings import SearchConfig
from services.usage import UsageTracker

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 200
MAX_TITLE_LENGTH = 120
MAX_SNIPPET_LENGTH = 400
# Поиск должен быть быстрым: зависший сервис не должен на минуту задерживать ответ.
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

SEARCH_OPEN = "<search>"
SEARCH_CLOSE = "</search>"
_MARKER_RE = re.compile(r"<search>(.*?)(?:</search>|$)", re.IGNORECASE | re.DOTALL)
# Мусор, который модели иногда ставят перед служебной строкой.
_LEAD_JUNK = " \t\r\n`*"

# Явная просьба поискать: «погугли», «найди в интернете», «дай ссылки на источники».
_VERB = (
    r"(?:найди|найти|поищи|поискать|ищи|искать|посмотри|посмотреть|глянь|проверь|проверить|"
    r"узнай|узнать|пишут|говорят|почитай|загляни)"
)
_PLACE = r"(?:в|по|из)\s+(?:интернет[еау]?|инете|инет[ау]?|гугл[еу]?|google|сети)"
_SEARCH_REQUEST_RE = re.compile(
    r"\b(?:по|за|про)гугл|\bгугл(?:ани|ни)\b|\bgoogle\s+it\b"
    rf"|\b{_VERB}\b[^.!?\n]{{0,60}}?\b{_PLACE}\b|\b{_PLACE}\b[^.!?\n]{{0,60}}?\b{_VERB}\b"
    r"|\bссылк\w*\s+на\s+источник|\bс\s+(?:источниками|ссылками)\b|\bисточник\w*\s+со\s+ссылк"
    r"|\b(?:дай|скинь|пришли|кинь)\w*\s+(?:мне\s+)?(?:ссылк|источник)",
    re.IGNORECASE,
)

_CITATION_RE = re.compile(r"\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]")
SOURCES_HEADER = "**Источники:**"
# Список источников всегда в конце ответа; в контекст модели он не попадает,
# чтобы она не начала сама дописывать к ответам списки ссылок.
_SOURCES_BLOCK_RE = re.compile(r"\n*" + re.escape(SOURCES_HEADER) + r"\n.*\Z", re.DOTALL)


class SearchError(Exception):
    """Поиск не удался: нет провайдеров, исчерпан лимит, сеть или ошибка API."""


class SearchLimitError(SearchError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"daily search limit reached ({limit})")
        self.limit = limit


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    date: str = ""

    @property
    def domain(self) -> str:
        host = urlsplit(self.url).hostname or ""
        return host.removeprefix("www.")


@dataclass(frozen=True, slots=True)
class SearchStarted:
    """Событие ChatService.ask_stream: Эля ищет в интернете (для индикатора в интерфейсе)."""

    query: str


# ------------------------------------------------------------------ провайдеры ----


def _clean(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _result(title: object, url: object, snippet: object, date: object = "") -> SearchResult | None:
    url = str(url or "").strip()
    if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
        return None
    return SearchResult(
        title=_clean(title, MAX_TITLE_LENGTH) or urlsplit(url).hostname or url,
        url=url,
        snippet=_clean(snippet, MAX_SNIPPET_LENGTH),
        date=_clean(date, 40),
    )


class SearchProvider(ABC):
    def __init__(self, name: str, title: str, http: httpx.AsyncClient) -> None:
        self.name = name
        self.title = title
        self._http = http

    @abstractmethod
    async def search(self, query: str, *, language: str, region: str) -> list[SearchResult]:
        """До 10 результатов выдачи Google; ошибки — SearchError."""
        raise NotImplementedError

    async def _send(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = await self._http.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            raise SearchError(f"{self.name}: network error {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SearchError(f"{self.name}: HTTP {response.status_code} {response.text[:200]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise SearchError(f"{self.name}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise SearchError(f"{self.name}: response is not a JSON object")
        if data.get("error"):
            raise SearchError(f"{self.name}: {str(data['error'])[:200]}")
        return data


class SerperProvider(SearchProvider):
    """Выдача Google через Serper.dev: https://serper.dev (2 500 бесплатных запросов)."""

    URL = "https://google.serper.dev/search"

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        super().__init__("serper", "Serper.dev (Google)", http)
        self._api_key = api_key

    async def search(self, query: str, *, language: str, region: str) -> list[SearchResult]:
        payload: dict[str, object] = {"q": query}
        if language:
            payload["hl"] = language
        if region:
            payload["gl"] = region
        data = await self._send(
            "POST", self.URL, json=payload, headers={"X-API-KEY": self._api_key}
        )
        found: list[SearchResult | None] = []
        box = data.get("answerBox")
        if isinstance(box, dict):
            found.append(
                _result(box.get("title"), box.get("link"), box.get("answer") or box.get("snippet"))
            )
        graph = data.get("knowledgeGraph")
        if isinstance(graph, dict):
            found.append(
                _result(graph.get("title"), graph.get("descriptionLink") or graph.get("website"),
                        graph.get("description"))
            )
        for item in data.get("organic") or []:
            if isinstance(item, dict):
                found.append(
                    _result(item.get("title"), item.get("link"), item.get("snippet"), item.get("date"))
                )
        return [item for item in found if item is not None]


class SerpApiProvider(SearchProvider):
    """Выдача Google через SerpApi: https://serpapi.com (250 бесплатных запросов в месяц)."""

    URL = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        super().__init__("serpapi", "SerpApi (Google)", http)
        self._api_key = api_key

    async def search(self, query: str, *, language: str, region: str) -> list[SearchResult]:
        params = {"engine": "google", "q": query, "api_key": self._api_key}
        if language:
            params["hl"] = language
        if region:
            params["gl"] = region
        data = await self._send("GET", self.URL, params=params)
        found: list[SearchResult | None] = []
        box = data.get("answer_box")
        if isinstance(box, dict):
            found.append(
                _result(box.get("title"), box.get("link"), box.get("answer") or box.get("snippet"))
            )
        graph = data.get("knowledge_graph")
        if isinstance(graph, dict):
            source = graph.get("source") if isinstance(graph.get("source"), dict) else {}
            found.append(
                _result(graph.get("title"), source.get("link") or graph.get("website"),
                        graph.get("description"))
            )
        for item in data.get("organic_results") or []:
            if isinstance(item, dict):
                found.append(
                    _result(item.get("title"), item.get("link"), item.get("snippet"), item.get("date"))
                )
        return [item for item in found if item is not None]


PROVIDER_CLASSES: dict[str, type[SearchProvider]] = {
    "serper": SerperProvider,
    "serpapi": SerpApiProvider,
}


def build_search_providers(config: SearchConfig, http: httpx.AsyncClient) -> list[SearchProvider]:
    return [PROVIDER_CLASSES[name](key, http) for name, key in config.providers]


# ------------------------------------------------------------------- сервис ----


class WebSearch:
    """Ищет запрос у первого сработавшего провайдера и отдаёт до max_results результатов.

    Упавший провайдер на время пропускается (как модели в AIClient); число поисков
    в сутки ограничено, чтобы автоматический поиск не съел бесплатную квоту.
    """

    def __init__(
        self,
        providers: Sequence[SearchProvider],
        *,
        max_results: int = 6,
        language: str = "ru",
        region: str = "",
        auto: bool = True,
        usage: UsageTracker | None = None,
        cooldown: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("at least one search provider is required")
        self._providers = list(providers)
        self.max_results = max_results
        self.language = language
        self.region = region
        self.auto = auto
        self._usage = usage
        self._cooldown = cooldown
        self._clock = clock
        self._paused_until: dict[str, float] = {}

    @property
    def provider_titles(self) -> list[str]:
        return [p.title for p in self._providers]

    @property
    def daily_limit(self) -> int:
        return self._usage.daily_limit if self._usage else 0

    async def used_today(self) -> int:
        return await self._usage.used_today() if self._usage else 0

    async def search(self, query: str) -> list[SearchResult]:
        query = " ".join(query.split())[:MAX_QUERY_LENGTH]
        if not query:
            raise SearchError("empty query")
        errors: list[str] = []
        for provider in self._providers:
            if self._paused_until.get(provider.name, 0.0) > self._clock():
                errors.append(f"{provider.name}: paused")
                continue
            if self._usage and not await self._usage.try_consume():
                raise SearchLimitError(self._usage.daily_limit)
            try:
                results = await provider.search(query, language=self.language, region=self.region)
            except SearchError as exc:
                logger.warning("Web search failed: %s", exc)
                errors.append(str(exc))
                self._paused_until[provider.name] = self._clock() + self._cooldown
                continue
            self._paused_until.pop(provider.name, None)
            unique: dict[str, SearchResult] = {}
            for item in results:
                unique.setdefault(item.url.rstrip("/"), item)
            found = list(unique.values())[: self.max_results]
            logger.info("Web search: provider=%s results=%d", provider.name, len(found))
            return found
        raise SearchError("; ".join(errors) or "no search providers")


# ------------------------------------------------- служебная строка <search> ----


def wants_search(text: str) -> bool:
    """Собеседник прямо просит поискать в интернете или дать источники."""
    return bool(_SEARCH_REQUEST_RE.search(text))


def parse_query(text: str) -> str:
    """Запрос из <search>…</search>; пустая строка — служебной строки нет."""
    match = _MARKER_RE.search(text)
    if match is None:
        return ""
    return " ".join(match.group(1).replace("\n", " ").split())[:MAX_QUERY_LENGTH].strip(" \"«»'")


def strip_markers(text: str) -> str:
    """Убирает служебные строки, если модель вставила их посреди обычного ответа."""
    return re.sub(r"<search>.*?</search>\s*", "", text, flags=re.IGNORECASE | re.DOTALL).strip()


class MarkerDetector:
    """Смотрит на начало потокового ответа и решает: это поиск или обычный текст.

    Пока по первым символам не ясно, текст придерживается; потом либо отдаётся
    целиком (обычный ответ), либо копится до закрывающего тега (поиск).
    """

    def __init__(self) -> None:
        self._buffer = ""
        self.mode: str | None = None  # None — пока не ясно, "text" или "search"

    @property
    def complete(self) -> bool:
        """Запрос дописан до </search> — дальше поток можно не читать."""
        return self.mode == "search" and SEARCH_CLOSE in self._buffer.lower()

    @property
    def query(self) -> str:
        return parse_query(self._buffer) if self.mode == "search" else ""

    def feed(self, piece: str) -> str:
        """Новый кусок потока → текст, который можно показать прямо сейчас."""
        if self.mode == "text":
            return piece
        self._buffer += piece
        if self.mode == "search":
            return ""
        head = self._buffer.lstrip(_LEAD_JUNK)
        if not head:
            return ""
        probe = head[: len(SEARCH_OPEN)].lower()
        if head.lower().startswith(SEARCH_OPEN):
            self.mode = "search"
            return ""
        if SEARCH_OPEN.startswith(probe):
            return ""  # «<sea» — ждём продолжения
        self.mode = "text"
        return self._buffer

    def flush(self) -> str:
        """Поток кончился, а решение не принято (ответ из пары символов) — это текст."""
        if self.mode is None:
            self.mode = "text"
            return self._buffer
        return ""


# --------------------------------------------------------------- источники ----


def _link_title(title: str) -> str:
    # Квадратные скобки в заголовке сломали бы Markdown-ссылку.
    return title.replace("[", "(").replace("]", ")").replace("\n", " ")


def _link_url(url: str) -> str:
    return quote(url, safe=":/?#@!$&'*+,;=%-._~")


_CODE_RE = re.compile(r"(```.*?(?:```|\Z)|`[^`\n]+`)", re.DOTALL)


def _citation_numbers(group: str) -> list[int]:
    return [int(part) for part in re.split(r"[,;]", group)]


def _in_prose(text: str, transform: Callable[[str], str]) -> str:
    """Применяет transform только к тексту вне кода: arr[1] в коде — не сноска."""
    parts = _CODE_RE.split(text)
    return "".join(part if index % 2 else transform(part) for index, part in enumerate(parts))


def _cited_numbers(text: str, count: int) -> list[int]:
    """Номера результатов, на которые есть сноски, в порядке первого упоминания."""
    cited: list[int] = []
    prose = "".join(_CODE_RE.split(text)[::2])
    for match in _CITATION_RE.finditer(prose):
        for number in _citation_numbers(match.group(1)):
            if 1 <= number <= count and number not in cited:
                cited.append(number)
    return cited


def _renumber(text: str, mapping: dict[int, int]) -> str:
    """Сноски получают номера по порядку списка источников; сноски на несуществующие
    результаты ([9] при шести результатах) убираются вместе с пробелом перед ними."""

    def fix(match: re.Match[str]) -> str:
        numbers = sorted({mapping[n] for n in _citation_numbers(match.group(2)) if n in mapping})
        return f"{match.group(1)}[{', '.join(map(str, numbers))}]" if numbers else ""

    pattern = re.compile(r"([ \t]*)" + _CITATION_RE.pattern)
    return _in_prose(text, lambda part: pattern.sub(fix, part))


def attach_sources(text: str, results: Sequence[SearchResult], *, fallback: int = 3) -> str:
    """Ответ модели + список источников. В списке — результаты, на которые есть сноски,
    пронумерованные по порядку упоминания (в тексте номера меняются так же). Если сносок
    нет, в списке первые fallback результатов: по ним модель и отвечала."""
    text = strip_markers(text)
    if not results:
        return text
    cited = _cited_numbers(text, len(results))
    order = cited or list(range(1, min(fallback, len(results)) + 1))
    text = _renumber(text, {old: new for new, old in enumerate(cited, start=1)}).rstrip()
    lines = []
    for number, index in enumerate(order, start=1):
        item = results[index - 1]
        lines.append(f"{number}. [{_link_title(item.title)}]({_link_url(item.url)}) — {item.domain}")
    return f"{text}\n\n{SOURCES_HEADER}\n" + "\n".join(lines)


def strip_sources(text: str) -> str:
    """Ответ без списка источников — для контекста модели."""
    return _SOURCES_BLOCK_RE.sub("", text)


def results_block(query: str, results: Sequence[SearchResult], today: str) -> str:
    """Результаты поиска для системного промпта."""
    lines = [f"Результаты поиска в Google по запросу «{query}» (сегодня {today}):"]
    for number, item in enumerate(results, start=1):
        date = f" ({item.date})" if item.date else ""
        lines.append(f"[{number}] {item.title} — {item.domain}{date}\n{item.snippet or '(без описания)'}")
    return "\n\n".join(lines)

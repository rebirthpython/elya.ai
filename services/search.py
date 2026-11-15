"""Поиск в интернете и ссылки на источники в ответах Эли.

Модель сама решает, нужен ли поиск: вместо ответа она выводит одну строку
<search>запрос</search>. Бот ищет запрос через внешний API, передаёт модели
пронумерованные результаты, а к её ответу со сносками [1], [2] добавляет список
источников со ссылками. Провайдеры перебираются по приоритету:

- Serper.dev и SerpApi — выдача Google (нужен ключ). Собственный API поиска Google
  (Custom Search JSON API) закрыт для новых пользователей и отключается 1 января 2027 года;
- Tavily — поисковый API для ИИ-ассистентов (нужен ключ);
- Brave Search API — собственный индекс Brave (нужен ключ);
- DuckDuckGo — HTML-версия выдачи, работает без ключа и подключена по умолчанию,
  чтобы поиск был и без регистрации где-либо.
"""

from __future__ import annotations

import html
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlsplit

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

# Вопросы, ответ на которые без интернета заведомо устарел: погода, курсы, свежие новости.
# Лёгкие модели не всегда сами решают поискать, поэтому тут поиск обязателен.
_FRESH_DATA_RE = re.compile(
    r"\bпогод(?:а|ы|е|у|ой|н\w*)\b|\bweather\b"
    r"|\bкурс\w*\s+(?:доллар|евро|рубл|юан|тенге|гривн|валют|биткоин|bitcoin|btc|крипт|акци)"
    r"|\b(?:последн|свеж|главн|сегодняшн)\w*\s+новост"
    r"|\bчто\s+(?:сейчас\s+)?происходит\s+в\b"
    r"|\bсч[её]т\s+(?:в\s+)?матч|\bкто\s+(?:выиграл|победил)\b",
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


_TAG_RE = re.compile(r"<[^>]+>")


def _clean(value: object, limit: int) -> str:
    # Некоторые API (Brave) выделяют слова запроса тегами <strong> и экранируют символы.
    text = " ".join(html.unescape(_TAG_RE.sub("", str(value or ""))).split())
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
        """До 10 результатов поиска; ошибки — SearchError."""
        raise NotImplementedError

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = await self._http.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            raise SearchError(f"{self.name}: network error {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SearchError(f"{self.name}: HTTP {response.status_code} {response.text[:200]}")
        return response

    async def _send(self, method: str, url: str, **kwargs) -> dict:
        response = await self._request(method, url, **kwargs)
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


class TavilyProvider(SearchProvider):
    """Поиск Tavily: https://tavily.com (1 000 бесплатных запросов в месяц, без карты)."""

    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        super().__init__("tavily", "Tavily", http)
        self._api_key = api_key

    async def search(self, query: str, *, language: str, region: str) -> list[SearchResult]:
        payload = {"query": query, "max_results": 10, "search_depth": "basic"}
        data = await self._send(
            "POST", self.URL, json=payload, headers={"Authorization": f"Bearer {self._api_key}"}
        )
        found = [
            _result(item.get("title"), item.get("url"), item.get("content"), item.get("published_date"))
            for item in data.get("results") or []
            if isinstance(item, dict)
        ]
        return [item for item in found if item is not None]


class BraveProvider(SearchProvider):
    """Brave Search API: https://brave.com/search/api (бесплатный кредит каждый месяц)."""

    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        super().__init__("brave", "Brave Search", http)
        self._api_key = api_key

    async def search(self, query: str, *, language: str, region: str) -> list[SearchResult]:
        params = {"q": query, "count": "10"}
        if language:
            params["search_lang"] = language
        if region:
            params["country"] = region.upper()
        data = await self._send(
            "GET",
            self.URL,
            params=params,
            headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
        )
        web = data.get("web") if isinstance(data.get("web"), dict) else {}
        found = [
            _result(item.get("title"), item.get("url"), item.get("description"), item.get("age"))
            for item in web.get("results") or []
            if isinstance(item, dict)
        ]
        return [item for item in found if item is not None]


def _duckduckgo_url(href: str) -> str:
    """Ссылки выдачи ведут через редирект //duckduckgo.com/l/?uddg=<адрес>; достаём адрес."""
    href = html.unescape(href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if (parts.hostname or "").endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg")
        return target[0] if target else ""
    return href


class _DuckDuckGoParser(HTMLParser):
    """Разбирает html.duckduckgo.com: блоки .result с заголовком .result__a и .result__snippet.
    Рекламные блоки (.result--ad) пропускаются."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._field: str | None = None  # "title" или "snippet" — внутри какого тега текст
        self._depth = 0  # вложенность тегов внутри текущего поля
        self._skip = False  # текущий блок — реклама

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if self._field is not None:
            self._depth += 1
            return
        if tag == "div" and "result" in classes:
            self._skip = "result--ad" in classes
            return
        if self._skip:
            return
        if tag == "a" and "result__a" in classes:
            self.items.append({"title": "", "url": _duckduckgo_url(dict(attrs).get("href") or ""), "snippet": ""})
            self._field, self._depth = "title", 0
        elif tag in ("a", "div", "td") and "result__snippet" in classes and self.items:
            self._field, self._depth = "snippet", 0

    def handle_endtag(self, tag: str) -> None:
        if self._field is None:
            return
        if self._depth:
            self._depth -= 1
        else:
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._field is not None and self.items:
            self.items[-1][self._field] += data


class DuckDuckGoProvider(SearchProvider):
    """Выдача DuckDuckGo без ключа: HTML-версия https://html.duckduckgo.com.

    Официального API у выдачи нет, поэтому при частых запросах DuckDuckGo может
    временно показывать проверку «вы не робот» — тогда поиск сообщает об ошибке,
    а WebSearch переходит к следующему провайдеру. Для надёжности лучше добавить
    ключ Tavily, Brave или Serper.
    """

    URL = "https://html.duckduckgo.com/html/"
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0 Mobile Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://html.duckduckgo.com/",
    }

    def __init__(self, api_key: str = "", http: httpx.AsyncClient | None = None) -> None:
        super().__init__("duckduckgo", "DuckDuckGo", http)

    @staticmethod
    def region_code(language: str, region: str) -> str:
        """Параметр kl: «ru-ru», «kz-ru», «us-en»…; «wt-wt» — без региона."""
        if region:
            return f"{region}-{language or 'en'}"
        if language == "ru":
            return "ru-ru"
        return "wt-wt"

    async def search(self, query: str, *, language: str, region: str) -> list[SearchResult]:
        response = await self._request(
            "POST",
            self.URL,
            data={"q": query, "kl": self.region_code(language, region)},
            headers=self.HEADERS,
        )
        page = response.text
        parser = _DuckDuckGoParser()
        parser.feed(page)
        parser.close()
        found = [_result(item["title"], item["url"], item["snippet"]) for item in parser.items]
        results = [item for item in found if item is not None]
        if not results and ("anomaly" in page or "captcha" in page.lower()):
            raise SearchError(f"{self.name}: DuckDuckGo asks for a captcha (too many requests)")
        return results


PROVIDER_CLASSES: dict[str, type[SearchProvider]] = {
    "serper": SerperProvider,
    "serpapi": SerpApiProvider,
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "duckduckgo": DuckDuckGoProvider,
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


def needs_fresh_data(text: str) -> bool:
    """Вопрос о погоде, курсах или свежих новостях: без поиска ответ будет устаревшим."""
    return bool(_FRESH_DATA_RE.search(text))


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
    lines = [f"Результаты поиска в интернете по запросу «{query}» (сегодня {today}):"]
    for number, item in enumerate(results, start=1):
        date = f" ({item.date})" if item.date else ""
        lines.append(f"[{number}] {item.title} — {item.domain}{date}\n{item.snippet or '(без описания)'}")
    return "\n\n".join(lines)

"""Разбор ссылок: текст статьи, субтитры видео, защита от внутренних адресов и ответ Эли."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from ai.client import AIClient
from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.documents import LINKS_RULE, DocumentStore
from services.links import (
    DEFAULT_ARTICLE_QUESTION,
    DEFAULT_VIDEO_QUESTION,
    LinkError,
    LinkReader,
    LinkStarted,
    check_public,
    cue_chunks,
    extract_page,
    find_urls,
    parse_timedtext,
    parse_vtt,
    pick_track,
    strip_urls,
    youtube_id,
)
from services.reminders import Reminders
from tests.test_bot_flow import OWNER_ID
from tests.test_bot_flow import _settings as bot_settings
from tests.test_code_mode import CodeProvider
from tests.test_documents import make_pdf

PARAGRAPH = (
    "Учёные из Новосибирска показали, что новый катализатор ускоряет реакцию в десять раз. "
    "Это позволит удешевить производство удобрений и снизить выбросы."
)

ARTICLE_HTML = f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<title>Новости — Сайт</title>
<meta property="og:title" content="Катализатор ускорил реакцию в десять раз">
<meta property="og:site_name" content="Наука сегодня">
<meta name="author" content="Анна Петрова">
<meta property="article:published_time" content="2026-11-10T09:00:00Z">
<script>var tracking = "не должно попасть в текст";</script>
<style>.x {{ color: red }}</style>
</head><body>
<nav><a href="/">Главная</a> <a href="/news">Новости</a></nav>
<div class="cookie-banner">Мы используем cookie</div>
<article>
  <h1>Катализатор ускорил реакцию</h1>
  <p>{PARAGRAPH}</p>
  <h2>Как это работает</h2>
  <p>{PARAGRAPH} Катализатор состоит из никеля и меди.</p>
  <ul><li>Дешевле в 3 раза</li><li>Работает при комнатной температуре</li></ul>
  <div class="share-buttons">Поделиться ВКонтакте</div>
</article>
<aside>Читайте также: курс валют</aside>
<div class="comments"><p>Комментарий: ерунда какая-то</p></div>
<footer>© Наука сегодня</footer>
</body></html>"""


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def reader(handler) -> LinkReader:
    return LinkReader(httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=public_resolver)


# ---------- ссылки в тексте ----------


def test_find_urls_strips_punctuation_and_keeps_wikipedia_parentheses():
    text = (
        "Глянь https://example.com/a?b=1. И ещё (https://ru.wikipedia.org/wiki/Python_(язык)) "
        "и www.habr.com/ru/articles/1/, а https://example.com/a?b=1 — повтор"
    )
    assert find_urls(text) == [
        "https://example.com/a?b=1",
        "https://ru.wikipedia.org/wiki/Python_(язык)",
        "https://www.habr.com/ru/articles/1/",
    ]
    assert find_urls("нет ссылок, только example") == []
    assert strip_urls("https://example.com/a  ") == ""
    assert strip_urls("о чём тут? https://example.com/a") == "о чём тут?"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://youtube.com/shorts/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
    ],
)
def test_youtube_id(url):
    assert youtube_id(url) == "dQw4w9WgXcQ"


def test_youtube_id_ignores_other_pages():
    assert youtube_id("https://www.youtube.com/@channel") is None
    assert youtube_id("https://example.com/watch?v=dQw4w9WgXcQ") is None


# ---------- безопасность ----------


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8080/", "http://127.0.0.1/", "http://10.0.0.5/admin", "http://[::1]/",
     "http://169.254.169.254/latest/meta-data", "ftp://example.com/file", "http://user:pass@example.com/"],
)
async def test_check_public_refuses_internal_addresses(url):
    with pytest.raises(LinkError):
        await check_public(url, public_resolver)


async def test_check_public_refuses_names_that_resolve_inside():
    async def private(host):
        return ["192.168.1.10"]

    with pytest.raises(LinkError, match="внутренней сети"):
        await check_public("https://intranet.example.com/", private)
    await check_public("https://example.com/", public_resolver)


async def test_redirect_into_the_internal_network_is_refused():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    with pytest.raises(LinkError, match="внутренней сети"):
        await reader(handler).read("https://example.com/short")


# ---------- текст статьи ----------


def test_extract_page_keeps_the_article_and_drops_the_rest():
    page = extract_page(ARTICLE_HTML)
    assert page.title == "Катализатор ускорил реакцию в десять раз"
    assert page.author == "Анна Петрова"
    assert page.published == "2026-11-10"
    assert page.site == "Наука сегодня"
    assert "## Как это работает" in page.text
    assert "• Дешевле в 3 раза" in page.text
    assert "никеля и меди" in page.text
    for junk in ("tracking", "Главная", "cookie", "Поделиться", "Читайте также", "Комментарий", "©"):
        assert junk not in page.text


def test_extract_page_takes_article_body_from_json_ld():
    body = "Полный текст статьи. " * 60
    html = (
        "<html><head><script type='application/ld+json'>"
        + json.dumps({"@context": "https://schema.org", "@type": "NewsArticle",
                      "headline": "Заголовок", "articleBody": body, "author": {"name": "Иван"}})
        + "</script></head><body><div id='app'>Загрузка…</div></body></html>"
    )
    page = extract_page(html)
    assert page.title == "Заголовок"
    assert page.author == "Иван"
    assert page.text.startswith("Полный текст статьи.")


def test_extract_page_survives_a_wrapper_with_a_menu_class():
    html = f"<body><div class='page has-menu'><p>{PARAGRAPH * 4}</p></div></body>"
    assert "катализатор" in extract_page(html).text


async def test_reader_turns_an_article_into_a_document():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              content=ARTICLE_HTML.encode())

    parsed = await reader(handler).read("https://nauka.example.com/news/1")
    assert parsed.kind == "link"
    assert parsed.name == "Катализатор ускорил реакцию в десять раз"
    assert parsed.info == "статья с nauka.example.com"
    assert parsed.chunks[0].label == "о статье"
    assert "Автор: Анна Петрова" in parsed.chunks[0].text
    assert "Адрес: https://nauka.example.com/news/1" in parsed.chunks[0].text
    assert any(chunk.label == "раздел «Как это работает»" for chunk in parsed.chunks)


async def test_reader_decodes_windows_1251_pages():
    html = f"<html><head><meta charset='windows-1251'></head><body><article><p>{PARAGRAPH * 5}</p></article></body></html>"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html.encode("cp1251"))

    parsed = await reader(handler).read("https://old.example.ru/")
    assert "катализатор" in parsed.chunks[1].text


async def test_reader_warns_when_only_a_bit_of_text_is_available():
    html = ("<html><head><meta property='og:title' content='Платная статья'>"
            "<meta property='og:description' content='Анонс: учёные нашли катализатор.'></head>"
            "<body><article><p>Оформите подписку, чтобы читать дальше.</p></article></body></html>")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html.encode())

    parsed = await reader(handler).read("https://paywall.example.com/a")
    assert parsed.truncated
    assert "доступна только часть" in parsed.info
    assert "Внимание" in parsed.chunks[0].text
    assert "Анонс" in parsed.chunks[1].text


async def test_reader_reads_pdf_by_link():
    pdf = make_pdf(["First page text", "Second page about mitochondria"])

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=pdf)

    parsed = await reader(handler).read("https://example.com/files/paper.pdf")
    assert parsed.kind == "pdf"
    assert parsed.name == "paper.pdf"
    assert "по ссылке с example.com" in parsed.info
    assert parsed.chunks[1].label == "стр. 2"


@pytest.mark.parametrize(
    ("status", "text"), [(403, "без входа"), (404, "404"), (500, "ошибкой 500")]
)
async def test_reader_explains_http_errors(status, text):
    def handler(request):
        return httpx.Response(status)

    with pytest.raises(LinkError, match=text):
        await reader(handler).read("https://example.com/a")


async def test_reader_refuses_binary_files():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG....")

    with pytest.raises(LinkError, match="другого типа"):
        await reader(handler).read("https://example.com/cat.png")


# ---------- субтитры ----------


def test_parse_timedtext_both_formats():
    classic = ('<?xml version="1.0"?><transcript><text start="0.5" dur="2">Привет, &amp;#39;мир&amp;#39;</text>'
               '<text start="65.2" dur="3">Вторая строка</text></transcript>')
    cues = parse_timedtext(classic)
    assert [(c.start, c.text) for c in cues] == [(0.5, "Привет, 'мир'"), (65.2, "Вторая строка")]
    srv3 = '<timedtext><body><p t="1500" d="2000"><s>Раз</s><s> два</s></p></body></timedtext>'
    assert [(c.start, c.text) for c in parse_timedtext(srv3)] == [(1.5, "Раз два")]


def test_parse_vtt_and_srt():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Анна>Здравствуйте\n\n01:02:03.500 --> 01:02:05.000\nПока"
    assert [(c.start, c.text) for c in parse_vtt(vtt)] == [(1, "Здравствуйте"), (3723, "Пока")]
    srt = "1\r\n00:00:05,000 --> 00:00:07,000\r\nПервая\r\nвторая\r\n\r\n2\r\n00:00:09,000 --> 00:00:10,000\r\nТретья"
    assert [c.text for c in parse_vtt(srt)] == ["Первая вторая", "Третья"]


def test_cue_chunks_label_timecodes_and_drop_repeats():
    from services.links import Cue

    cues = [Cue(i * 10, f"фраза номер {i // 2}") for i in range(200)]
    chunks = cue_chunks(cues, limit=300)
    assert chunks[0].label.startswith("00:00–")
    assert chunks[0].text.count("фраза номер 0") == 1
    assert chunks[-1].label.endswith("33:10")


def test_pick_track_prefers_manual_russian():
    tracks = [
        {"baseUrl": "a", "languageCode": "en"},
        {"baseUrl": "b", "languageCode": "ru", "kind": "asr"},
        {"baseUrl": "c", "languageCode": "ru"},
    ]
    assert pick_track(tracks)["baseUrl"] == "c"
    assert pick_track(tracks[:2])["baseUrl"] == "b"
    assert pick_track([{"baseUrl": "d", "languageCode": "de"}, tracks[0]])["baseUrl"] == "a"
    assert pick_track([]) is None


def _youtube_handler(captions: bool, *, page_tracks: bool = False):
    player = {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"title": "Как работают катализаторы", "author": "Химия просто",
                         "lengthSeconds": "754", "shortDescription": "Разбираем катализ."},
    }
    tracks = {"captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [
        {"baseUrl": "https://www.youtube.com/api/timedtext?v=dQw4w9WgXcQ&lang=ru&fmt=srv3",
         "languageCode": "ru", "kind": "asr"}]}}}
    page_player = {**player, **tracks} if page_tracks else player
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/watch":
            html = ('<html><head><meta property="og:title" content="Как работают катализаторы"></head><body>'
                    f'<script>var ytInitialPlayerResponse = {json.dumps(page_player)};var x=1;</script>'
                    '<script>ytcfg.set({"INNERTUBE_API_KEY":"KEY123"})</script></body></html>')
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=html.encode())
        if request.url.path == "/youtubei/v1/player":
            assert request.url.params["key"] == "KEY123"
            body = json.loads(request.content)
            assert body["videoId"] == "dQw4w9WgXcQ"
            return httpx.Response(200, json={**player, **(tracks if captions else {})})
        if request.url.path == "/api/timedtext":
            assert "fmt" not in request.url.params
            xml = ('<transcript><text start="0" dur="2">катализатор ускоряет реакцию</text>'
                   '<text start="75" dur="2">но сам не расходуется</text></transcript>')
            return httpx.Response(200, headers={"content-type": "text/xml"}, content=xml.encode())
        return httpx.Response(404)

    return handler, requests


async def test_youtube_video_is_read_from_subtitles():
    handler, requests = _youtube_handler(captions=True)
    parsed = await reader(handler).read("https://youtu.be/dQw4w9WgXcQ")
    assert parsed.kind == "video"
    assert parsed.name == "Как работают катализаторы"
    assert parsed.info == "YouTube, 12:34, субтитры русские, автоматические"
    assert "Автор: Химия просто" in parsed.chunks[0].text
    assert parsed.chunks[1].label == "00:00–01:15"
    assert "сам не расходуется" in parsed.chunks[1].text
    assert [r.url.path for r in requests] == ["/watch", "/youtubei/v1/player", "/api/timedtext"]


async def test_youtube_subtitles_from_the_page_skip_the_player_api():
    handler, requests = _youtube_handler(captions=False, page_tracks=True)
    parsed = await reader(handler).read("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert "катализатор ускоряет" in parsed.chunks[1].text
    assert "/youtubei/v1/player" not in [r.url.path for r in requests]


async def test_youtube_without_subtitles_keeps_only_the_description():
    handler, _ = _youtube_handler(captions=False)
    parsed = await reader(handler).read("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert parsed.truncated
    assert "субтитров нет" in parsed.info
    assert "нет доступных субтитров" in parsed.chunks[0].text
    assert parsed.chunks[1].text == "Разбираем катализ."


async def test_page_with_a_subtitle_track():
    html = ("<html><head><title>Лекция</title></head><body><video src='/v.mp4'>"
            "<track kind='captions' src='/subs/ru.vtt' srclang='ru'></video></body></html>")
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nСегодня говорим о катализе"

    def handler(request):
        if request.url.path.endswith(".vtt"):
            return httpx.Response(200, headers={"content-type": "text/vtt"}, content=vtt.encode())
        return httpx.Response(200, headers={"content-type": "text/html"}, content=html.encode())

    parsed = await reader(handler).read("https://lectures.example.com/1")
    assert parsed.kind == "video"
    assert parsed.name == "Лекция"
    assert "говорим о катализе" in parsed.chunks[1].text


# ---------- ответ Эли ----------


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'links.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


def _service(db, provider, handler):
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([provider], max_tokens=256, temperature=0.5)
    reminders = Reminders(db, client, clock=lambda: datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc))
    store = DocumentStore(db)
    service = ChatService(bot_settings(), client, memory, reminders=reminders, documents=store,
                          links=reader(handler))
    return service, memory, store


def _article_handler(request):
    return httpx.Response(200, headers={"content-type": "text/html"}, content=ARTICLE_HTML.encode())


async def test_bare_link_gets_the_main_points(db):
    provider = CodeProvider(reply="Главное: катализатор ускоряет реакцию в 10 раз.")
    service, memory, store = _service(db, provider, _article_handler)

    events = [e async for e in service.ask_stream(OWNER_ID, "https://nauka.example.com/news/1")]
    assert isinstance(events[0], LinkStarted)
    assert events[0].status_text() == "🔗 Читаю ссылку: nauka.example.com…"
    assert provider.question == DEFAULT_ARTICLE_QUESTION
    assert LINKS_RULE in provider.system
    assert "никеля и меди" in provider.system
    assert "Комментарий" not in provider.system

    docs = await store.list(OWNER_ID)
    assert [d.kind for d in docs] == ["link"]
    history = await memory.get_history(OWNER_ID)
    assert history[0].content == "https://nauka.example.com/news/1"

    # Дальше можно спрашивать по статье — без новой загрузки.
    await service.ask(OWNER_ID, "А из чего состоит катализатор в этой статье?")
    assert "никеля и меди" in provider.system


async def test_link_with_a_question_answers_it(db):
    provider = CodeProvider(reply="Из никеля и меди.")
    service, _, _ = _service(db, provider, _article_handler)
    await service.ask(OWNER_ID, "Из чего катализатор? https://nauka.example.com/news/1")
    assert provider.question == "Из чего катализатор? https://nauka.example.com/news/1"
    assert "никеля и меди" in provider.system


async def test_video_link_asks_for_main_points_with_timecodes(db):
    provider = CodeProvider(reply="О катализе (00:00).")
    handler, _ = _youtube_handler(captions=True)
    service, _, _ = _service(db, provider, handler)
    events = [e async for e in service.ask_stream(OWNER_ID, "https://youtu.be/dQw4w9WgXcQ")]
    assert events[0].video and events[0].status_text().startswith("🎬")
    assert provider.question == DEFAULT_VIDEO_QUESTION
    assert "[00:00–01:15]" in provider.system


async def test_unreadable_link_is_reported_honestly(db):
    provider = CodeProvider(reply="Не смогла открыть ссылку.")
    service, _, store = _service(db, provider, lambda request: httpx.Response(403))
    await service.ask(OWNER_ID, "https://closed.example.com/a")
    assert "бот не смог их прочитать" in provider.system
    assert "без входа" in provider.system
    assert await store.list(OWNER_ID) == []


async def test_links_are_ignored_in_photos_and_without_documents(db):
    provider = CodeProvider(reply="ok")
    calls = []

    def handler(request):
        calls.append(request)
        return _article_handler(request)

    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([provider], max_tokens=256, temperature=0.5)
    service = ChatService(bot_settings(), client, memory, links=reader(handler))
    assert not service.links_enabled
    await service.ask(OWNER_ID, "https://nauka.example.com/news/1")
    assert calls == []

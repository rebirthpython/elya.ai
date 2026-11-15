"""Документы: разбор PDF, Word, таблиц и текста, подбор фрагментов со ссылками на страницы,
сравнение файлов, бот (/docs, файлы и альбомы) и Mini App (/api/document)."""

import asyncio
import io
from datetime import date, datetime, timezone

import pytest
from aiogram import Bot
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient
from ai.prompts import build_system_prompt
from bot.handlers import documents as documents_handler
from database.session import create_engine, create_session_factory, init_db
from main import BOT_COMMANDS, create_dispatcher
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.documents import (
    DEFAULT_DOCUMENT_QUESTION,
    MAX_DOCUMENT_BYTES,
    DocumentError,
    DocumentStore,
    build_context,
    is_document,
    parse_document,
    terms,
)
from services.documents import _index as index_document
from services.documents import DocumentInfo
from services.instructions import InstructionsStore
from services.longterm import LongTermMemory
from services.modes import CODE, ModeStore
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.reminders import Reminders
from services.usage import UsageTracker
from tests.test_bot_flow import OWNER_ID, _callback_update, _message_update, _photo_update
from tests.test_bot_flow import _settings as bot_settings
from tests.test_code_mode import CodeProvider, DocumentSession
from tests.test_webapp import _settings as web_settings
from tests.test_webapp import auth
from web.server import create_web_app


# ---------- образцы файлов ----------


def make_pdf(pages: list[str]) -> bytes:
    """Простой PDF: на каждой странице строки текста (латиница — шрифт Helvetica)."""
    objects: list[bytes] = []
    count = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode())
    font_id = 3 + 2 * count
    for i, text in enumerate(pages):
        lines = []
        for n, line in enumerate(text.split("\n")):
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"BT /F1 12 Tf 72 {720 - 16 * n} Td ({safe}) Tj ET")
        stream = "\n".join(lines).encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def make_docx(paragraphs: list[str | None], *, headings: dict[int, str] | None = None, table=None) -> bytes:
    """None в списке — разрыв страницы."""
    from docx import Document
    from docx.enum.text import WD_BREAK

    document = Document()
    for index, text in enumerate(paragraphs):
        if headings and index in headings:
            document.add_heading(headings[index], level=1)
        if text is None:
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        else:
            document.add_paragraph(text)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def make_xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Продажи"
    sheet.append(["Месяц", "Выручка", "Дата"])
    sheet.append(["Январь", 1200.5, date(2026, 1, 31)])
    sheet.append(["Февраль", 1500, date(2026, 2, 28)])
    sheet.append([])
    sheet.append(["Итого", 2700.5, None])
    other = workbook.create_sheet("Пусто")
    other["A1"] = None
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


BIOLOGY = [
    "Chapter 1. The cell\nThe cell is the basic unit of life.",
    "Mitochondria produce energy for the cell.\nThey are called the powerhouse.",
    "Photosynthesis happens in chloroplasts of plant cells.",
]


# ---------- разбор файлов ----------


def test_pdf_is_split_by_pages():
    parsed = parse_document("bio.pdf", make_pdf(BIOLOGY))
    assert parsed.kind == "pdf" and parsed.info == "PDF, 3 стр."
    assert [chunk.label for chunk in parsed.chunks] == ["стр. 1", "стр. 2", "стр. 3"]
    assert "powerhouse" in parsed.chunks[1].text


def test_pdf_without_text_is_called_a_scan():
    with pytest.raises(DocumentError, match="скан"):
        parse_document("scan.pdf", make_pdf(["", ""]))


def test_docx_counts_pages_by_page_breaks():
    data = make_docx(["Введение в тему.", None, "Глава про клетки.", None, "Выводы."])
    parsed = parse_document("отчёт.docx", data)
    labels = [chunk.label for chunk in parsed.chunks]
    assert labels == ["стр. 1", "стр. 2", "стр. 3"] and parsed.info == "Word, 3 стр."
    assert "Глава про клетки" in parsed.chunks[1].text


def test_docx_without_pages_uses_sections_and_paragraphs():
    data = make_docx(
        ["Первый абзац.", "Второй абзац.", "Третий абзац."],
        headings={2: "Итоги"},
        table=[["Имя", "Оценка"], ["Аня", "5"]],
    )
    parsed = parse_document("заметки.docx", data)
    labels = [chunk.label for chunk in parsed.chunks]
    assert labels[0] == "абз. 1–2"
    assert labels[1].startswith("раздел «Итоги»")
    assert "Аня | 5" in parsed.chunks[-1].text


def test_xlsx_rows_keep_their_numbers_and_header():
    parsed = parse_document("отчёт.xlsx", make_xlsx())
    assert parsed.info == "Excel, 1 лист, 4 строк"
    chunk = parsed.chunks[0]
    assert chunk.label == "лист «Продажи», строки 1–5"
    assert "2: Январь | 1200.5 | 2026-01-31" in chunk.text and "5: Итого | 2700.5" in chunk.text


def test_csv_with_semicolons_and_header_in_every_chunk():
    rows = ["Фамилия;Балл"] + [f"Ученик{i};{i}" for i in range(400)]
    parsed = parse_document("оценки.csv", "\n".join(rows).encode("cp1251"))
    assert len(parsed.chunks) > 1 and parsed.info == "таблица CSV, 401 строк"
    assert parsed.chunks[0].label.startswith("строки 1–")
    assert parsed.chunks[1].text.startswith("(шапка, строка 1: Фамилия | Балл)")


def test_plain_text_is_labelled_by_lines():
    parsed = parse_document("лекция.md", ("строка\n" * 600).encode())
    assert parsed.kind == "text" and parsed.chunks[0].label.startswith("строки 1–")


@pytest.mark.parametrize(
    "name, data, message",
    [
        ("old.doc", b"x", "сохрани файл как .docx"),
        ("old.xls", b"x", ".xlsx или .csv"),
        ("photo.png", b"x", "Такой формат я не читаю"),
        ("empty.txt", b"", "Файл пустой"),
        ("broken.docx", b"not a zip", "повреждён"),
        ("big.txt", b"a" * (MAX_DOCUMENT_BYTES + 1), "до 20 МБ"),
    ],
)
def test_unreadable_files_are_explained(name, data, message):
    with pytest.raises(DocumentError, match=message):
        parse_document(name, data)


def test_document_extensions():
    assert all(is_document(name) for name in ("a.pdf", "B.DOCX", "c.xlsx", "d.csv", "e.txt", "f.md"))
    assert not any(is_document(name) for name in ("a.doc", "b.py", "c.png", None))


def test_terms_cut_word_endings():
    assert terms("Уравнения и уравнений") == ["уравне", "уравне"]
    assert terms("Что такое митохондрия на стр 12?") == ["митохо", "12"]


# ---------- подбор фрагментов ----------


def _indexed(doc_id: int, name: str, data: bytes):
    parsed = parse_document(name, data)
    return index_document(DocumentInfo(doc_id, parsed.name, parsed.kind, parsed.info), parsed.chunks)


def test_question_gets_the_right_page_with_a_citation_rule():
    docs = [_indexed(1, "bio.pdf", make_pdf(BIOLOGY))]
    context = build_context(docs, "What produce energy? mitochondria")
    assert context.relevant
    assert "[стр. 2]\nMitochondria produce energy" in context.block
    assert "(стр. 12)" in context.block  # пример ссылки для модели
    assert "Photosynthesis" not in context.block


def test_named_page_is_included_even_without_matching_words():
    docs = [_indexed(1, "bio.pdf", make_pdf(BIOLOGY))]
    context = build_context(docs, "что на стр. 3?")
    assert "[стр. 3]\nPhotosynthesis" in context.block


def test_unrelated_message_only_mentions_the_documents():
    docs = [_indexed(1, "bio.pdf", make_pdf(BIOLOGY))]
    context = build_context(docs, "какая завтра погода в Москве")
    assert not context.relevant
    assert "«bio.pdf» — PDF, 3 стр." in context.block and "Mitochondria" not in context.block


def test_fresh_document_without_question_gets_an_overview():
    docs = [_indexed(1, "bio.pdf", make_pdf(BIOLOGY))]
    context = build_context(docs, DEFAULT_DOCUMENT_QUESTION, fresh=[1])
    assert context.relevant and "[стр. 1]" in context.block


def test_compare_shows_the_differences_of_both_files():
    first = make_pdf(["Price: 100 rub\nTerm: 10 days", "Signature: Ivanov"])
    second = make_pdf(["Price: 150 rub\nTerm: 10 days", "Signature: Ivanov"])
    docs = [_indexed(1, "v1.pdf", first), _indexed(2, "v2.pdf", second)]
    context = build_context(docs, "сравни эти два файла")
    assert "текст совпадает примерно на" in context.block
    assert "[«v1.pdf», стр. 1]\nPrice: 100 rub" in context.block
    assert "[«v2.pdf», стр. 1]\nPrice: 150 rub" in context.block
    assert "(«Договор.docx», стр. 3)" in context.block  # при нескольких файлах — с именем


def test_budget_limits_the_fragments():
    pages = [f"Topic {i}: cells and energy " + "word " * 200 for i in range(40)]
    docs = [_indexed(1, "big.pdf", make_pdf(pages))]
    context = build_context(docs, "cells energy", budget=3000)
    assert context.block.count("[стр.") <= 3


def test_prompt_includes_the_documents_block():
    prompt = build_system_prompt("Эля", documents="Документы:\n1. «a.pdf»")
    assert "Документы:\n1. «a.pdf»" in prompt
    assert "Документы ты читаешь" in prompt
    assert "Документы:\n1." not in build_system_prompt("Эля")


# ---------- хранилище и сервис диалога ----------


@pytest.fixture
async def db(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'docs.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_store_keeps_the_last_documents_per_user(db):
    store = DocumentStore(db, max_documents=2)
    parsed = parse_document("bio.pdf", make_pdf(BIOLOGY))
    first = await store.add(1, parsed)
    second = await store.add(1, parsed)
    await store.add(2, parsed)
    third = await store.add(1, parsed)
    assert [info.id for info in await store.list(1)] == [second.id, third.id]
    assert first.id not in [info.id for info in await store.list(1)]
    assert await store.count(2) == 1

    # Новый объект (как после перезапуска) читает фрагменты из базы.
    context = await DocumentStore(db).context(1, "mitochondria energy")
    assert "Mitochondria" in context.block

    assert await store.delete(1, second.id) and not await store.delete(2, third.id)
    assert await store.clear(1) == 1 and await store.list(1) == []


def _service(db, provider):
    settings = bot_settings()
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    client = AIClient([provider], max_tokens=256, temperature=0.5)
    reminders = Reminders(db, client, clock=lambda: datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc))
    store = DocumentStore(db)
    service = ChatService(settings, client, memory, reminders=reminders, documents=store)
    return service, memory, store


async def test_service_answers_from_the_document_and_keeps_a_note_in_history(db):
    provider = CodeProvider(reply="Митохондрии дают энергию (стр. 2).")
    service, memory, store = _service(db, provider)
    info = await store.add(OWNER_ID, parse_document("bio.pdf", make_pdf(BIOLOGY)))

    await service.ask(OWNER_ID, "", documents=(info.id,))
    assert provider.question == DEFAULT_DOCUMENT_QUESTION
    assert "[стр. 1]" in provider.system

    await service.ask(OWNER_ID, "Where is the energy produced? mitochondria")
    assert "[стр. 2]\nMitochondria" in provider.system
    history = await memory.get_history(OWNER_ID)
    assert history[0].content == "📄 «bio.pdf»"
    assert history[2].content == "Where is the energy produced? mitochondria"


async def test_document_caption_is_not_taken_for_a_reminder(db):
    provider = CodeProvider()
    service, memory, store = _service(db, provider)
    info = await store.add(OWNER_ID, parse_document("bio.pdf", make_pdf(BIOLOGY)))
    await service.ask(OWNER_ID, "напомни, что тут написано про клетки", documents=(info.id,))
    assert provider.calls  # ответила модель, а не механизм напоминаний
    assert (await memory.get_history(OWNER_ID))[0].content.startswith("📄 «bio.pdf»\nнапомни")


# ---------- бот ----------


@pytest.fixture
async def bot_env(db, monkeypatch):
    monkeypatch.setattr(documents_handler, "ALBUM_DELAY", 0.05)
    provider = CodeProvider(reply="Ответ (стр. 2)")
    usage = UsageTracker(db, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    modes = ModeStore(db)
    store = DocumentStore(db)
    service = ChatService(bot_settings(), ai_client, memory, modes=modes, documents=store)
    session = DocumentSession()
    bot = Bot(token="42:TEST", session=session)
    files: dict[str, bytes] = {}

    async def download(file, *args, **kwargs):
        return io.BytesIO(files[file.file_id])

    bot.download = download
    dp = create_dispatcher(bot_settings(), ai_client, memory, usage, chat_service=service, modes=modes)
    return {"bot": bot, "dp": dp, "session": session, "provider": provider, "store": store,
            "files": files, "memory": memory, "modes": modes}


def _file_update(env, name, data, caption=None, group=None):
    update = _photo_update(caption, document={"file_name": name, "file_size": len(data)})
    file_id = f"f{update.update_id}"
    env["files"][file_id] = data
    message = update.message.model_copy(
        update={
            "document": update.message.document.model_copy(update={"file_id": file_id}),
            "media_group_id": group,
        }
    )
    return update.model_copy(update={"message": message})


async def _feed(env, update):
    await env["dp"].feed_update(env["bot"], update)


def test_docs_command_is_in_the_menu():
    assert "docs" in [command.command for command in BOT_COMMANDS]


async def test_document_without_caption_is_confirmed_without_the_model(bot_env):
    env = bot_env
    await _feed(env, _file_update(env, "bio.pdf", make_pdf(BIOLOGY)))
    text = env["session"].sent_texts()[-1]
    assert "Прочитала «bio.pdf» — PDF, 3 стр." in text and "сравнить" in text
    assert env["provider"].calls == []

    await _feed(env, _message_update("what do mitochondria produce"))
    assert "[стр. 2]\nMitochondria" in env["provider"].system
    assert env["session"].sent_texts()[-1] == "Ответ (стр. 2)"


async def test_document_with_caption_is_answered_right_away(bot_env):
    env = bot_env
    await _feed(env, _file_update(env, "bio.pdf", make_pdf(BIOLOGY), caption="что на стр. 3?"))
    assert "[стр. 3]\nPhotosynthesis" in env["provider"].system
    assert env["provider"].question == "что на стр. 3?"
    assert (await env["memory"].get_history(OWNER_ID))[0].content == "📄 «bio.pdf»\nчто на стр. 3?"


async def test_album_of_two_files_is_compared_once(bot_env):
    env = bot_env
    first = make_pdf(["Price: 100 rub", "Same text"])
    second = make_pdf(["Price: 150 rub", "Same text"])
    await asyncio.gather(
        _feed(env, _file_update(env, "v1.pdf", first, caption="сравни эти два файла", group="g1")),
        _feed(env, _file_update(env, "v2.pdf", second, group="g1")),
    )
    await asyncio.sleep(0.2)
    assert len(env["provider"].calls) == 1
    assert "Price: 100 rub" in env["provider"].system and "Price: 150 rub" in env["provider"].system


async def test_bad_document_is_explained(bot_env):
    env = bot_env
    await _feed(env, _file_update(env, "scan.pdf", make_pdf([""])))
    assert "скан" in env["session"].sent_texts()[-1]
    await _feed(env, _file_update(env, "old.doc", b"x"))
    assert ".docx" in env["session"].sent_texts()[-1]
    assert env["provider"].calls == [] and await env["store"].list(OWNER_ID) == []


async def test_code_mode_still_reads_text_files_as_code(bot_env):
    env = bot_env
    await env["modes"].set(OWNER_ID, CODE)
    await _feed(env, _file_update(env, "notes.txt", b"print(1)\n"))
    assert env["provider"].question.startswith("Файл `notes.txt`")
    await _feed(env, _file_update(env, "bio.pdf", make_pdf(BIOLOGY)))
    assert "Прочитала «bio.pdf»" in env["session"].sent_texts()[-1]


async def test_docs_command_lists_and_removes(bot_env):
    env = bot_env
    await _feed(env, _message_update("/docs"))
    assert "пока нет документов" in env["session"].sent_texts()[-1]

    await _feed(env, _file_update(env, "bio.pdf", make_pdf(BIOLOGY)))
    await _feed(env, _file_update(env, "notes.txt", b"hello"))
    await _feed(env, _message_update("/docs"))
    text = env["session"].sent_texts()[-1]
    assert "1. «bio.pdf» — PDF, 3 стр." in text and "2. «notes.txt»" in text

    first = (await env["store"].list(OWNER_ID))[0]
    await _feed(env, _callback_update(f"docs:del:{first.id}"))
    assert [info.name for info in await env["store"].list(OWNER_ID)] == ["notes.txt"]
    await _feed(env, _callback_update("docs:clear:0"))
    assert await env["store"].list(OWNER_ID) == []


async def test_clear_history_removes_documents(bot_env):
    env = bot_env
    await _feed(env, _file_update(env, "bio.pdf", make_pdf(BIOLOGY)))
    await _feed(env, _callback_update("clear:1"))
    assert await env["store"].list(OWNER_ID) == []


# ---------- Mini App ----------


@pytest.fixture
async def web_env(db):
    provider = CodeProvider(reply="Ответ (стр. 2)")
    settings = web_settings()
    usage = UsageTracker(db, 100)
    ai_client = AIClient([provider], max_tokens=256, temperature=0.5, usage=usage)
    memory = MemoryManager(MessageRepository(db), history_limit=30)
    preferences = PreferencesStore(db)
    long_memory = LongTermMemory(db)
    reminders = Reminders(db)
    instructions = InstructionsStore(db)
    store = DocumentStore(db)
    app = create_web_app(
        settings,
        ChatService(settings, ai_client, memory, preferences, long_memory, reminders,
                    instructions=instructions, documents=store),
        memory, usage, ai_client, SlidingWindowLimiter(10), preferences, long_memory,
        reminders, None, instructions,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    yield {"client": client, "provider": provider, "store": store, "memory": memory}
    await client.close()


def _upload(name: str, data: bytes) -> FormData:
    form = FormData()
    form.add_field("file", data, filename=name, content_type="application/octet-stream")
    return form


async def test_app_uploads_a_document_and_asks_about_it(web_env):
    client = web_env["client"]
    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["documents"] == []

    response = await client.post("/api/document", data=_upload("bio.pdf", make_pdf(BIOLOGY)), headers=auth())
    assert response.status == 200
    data = await response.json()
    document = data["document"]
    assert document["name"] == "bio.pdf" and document["info"] == "PDF, 3 стр."
    assert data["documents"] == [document] and data["truncated"] is False

    response = await client.post(
        "/api/chat/stream", json={"text": "", "documents": [document["id"]]}, headers=auth()
    )
    assert response.status == 200
    assert '"type": "done"' in await response.text()
    assert web_env["provider"].question == DEFAULT_DOCUMENT_QUESTION
    history = await web_env["memory"].get_history(OWNER_ID)
    assert history[0].content == "📄 «bio.pdf»"

    state = await (await client.get("/api/state", headers=auth())).json()
    assert [d["name"] for d in state["documents"]] == ["bio.pdf"]


async def test_app_explains_bad_files_and_removes_documents(web_env):
    client = web_env["client"]
    response = await client.post("/api/document", data=_upload("scan.pdf", make_pdf([""])), headers=auth())
    assert response.status == 415 and "скан" in (await response.json())["error"]["message"]

    first = (await (await client.post("/api/document", data=_upload("a.txt", b"hello"), headers=auth())).json())["document"]
    await client.post("/api/document", data=_upload("b.txt", b"world"), headers=auth())
    response = await client.post("/api/documents/delete", json={"id": first["id"]}, headers=auth())
    assert [d["name"] for d in (await response.json())["documents"]] == ["b.txt"]

    await client.post("/api/chat/new", json={}, headers=auth())
    assert await web_env["store"].list(OWNER_ID) == []

    await client.post("/api/document", data=_upload("c.txt", b"x"), headers=auth())
    response = await client.post("/api/documents/delete", json={"all": True}, headers=auth())
    assert (await response.json())["documents"] == []
    assert (await client.post("/api/documents/delete", json={"id": "x"}, headers=auth())).status == 400


async def test_app_empty_text_still_needs_a_document(web_env):
    response = await web_env["client"].post("/api/chat/stream", json={"text": ""}, headers=auth())
    assert response.status == 400

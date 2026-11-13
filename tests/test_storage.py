import pytest

from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.usage import UsageTracker


@pytest.fixture
async def session_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_history_roundtrip_and_clear(session_factory):
    memory = MemoryManager(MessageRepository(session_factory, store_limit=100), history_limit=10)
    await memory.save_exchange(1, "привет", "здравствуй")
    await memory.save_exchange(1, "как дела?", "отлично")
    await memory.save_exchange(2, "чужое", "сообщение")

    history = await memory.get_history(1)
    assert [(m.role, m.content) for m in history] == [
        ("user", "привет"), ("assistant", "здравствуй"),
        ("user", "как дела?"), ("assistant", "отлично"),
    ]
    assert await memory.clear_history(1) == 4
    assert await memory.get_history(1) == []
    assert await memory.history_size(2) == 2


async def test_history_limit_returns_newest(session_factory):
    memory = MemoryManager(MessageRepository(session_factory, store_limit=100), history_limit=3)
    for i in range(5):
        await memory.save_exchange(1, f"q{i}", f"a{i}")
    assert [m.content for m in await memory.get_history(1)] == ["a3", "q4", "a4"]


async def test_old_messages_are_pruned(session_factory):
    repo = MessageRepository(session_factory, store_limit=10)
    for i in range(20):
        await repo.add_exchange(1, f"q{i}", f"a{i}")
    assert await repo.count(1) == 10
    rows = await repo.get_recent(1, 100)
    assert rows[0].content == "q15" and rows[-1].content == "a19"


async def test_usage_tracker_limit(session_factory):
    usage = UsageTracker(session_factory, daily_limit=2)
    assert await usage.try_consume() is True
    assert await usage.try_consume() is True
    assert await usage.try_consume() is False
    assert await usage.used_today() == 2

    unlimited = UsageTracker(session_factory, daily_limit=0)
    assert await unlimited.try_consume() is True


async def test_preferences_store_defaults_update_and_validation(session_factory):
    from services.preferences import Preferences, PreferencesStore

    store = PreferencesStore(session_factory)
    assert await store.get(1) == Preferences("friendly", "normal")
    assert await store.update(1, style="caring") == Preferences("caring", "normal")
    assert await store.update(1, verbosity="detailed") == Preferences("caring", "detailed")
    assert await store.get(1) == Preferences("caring", "detailed")
    with pytest.raises(ValueError):
        await store.update(1, style="rude")
    assert await store.get(1) == Preferences("caring", "detailed")


def test_describe_database_resolves_sqlite_path_and_hides_passwords(tmp_path, monkeypatch):
    from database.session import describe_database

    monkeypatch.chdir(tmp_path)
    assert describe_database("sqlite+aiosqlite:///data/assistant.db") == (
        f"SQLite {tmp_path / 'data' / 'assistant.db'}"
    )
    described = describe_database("postgresql+asyncpg://elya:secret@db:5432/elya")
    assert "secret" not in described and "db:5432/elya" in described

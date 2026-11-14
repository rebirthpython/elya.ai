import pytest

from ai.prompts import build_system_prompt
from database.session import create_engine, create_session_factory, init_db
from services.instructions import (
    MAX_INSTRUCTION_LENGTH,
    Instructions,
    InstructionsStore,
    clean_instruction,
)


@pytest.fixture
async def store(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'i.db'}")
    await init_db(engine)
    yield InstructionsStore(create_session_factory(engine))
    await engine.dispose()


def test_clean_instruction_keeps_paragraphs():
    raw = "  Я   учусь\r\nна врача \n\n\n\n Люблю   списки  "
    assert clean_instruction(raw) == "Я учусь\nна врача\n\nЛюблю списки"


def test_prompt_block():
    full = Instructions("Я студентка-медик", "Без воды, с примерами", True)
    block = full.prompt_block()
    assert "Что знать о собеседнике:\nЯ студентка-медик" in block
    assert "Как отвечать:\nБез воды, с примерами" in block
    assert "не важнее правил выше" in block
    assert "Как отвечать" not in Instructions("только о себе", "", True).prompt_block()
    assert Instructions("x", "y", False).prompt_block() == ""
    assert Instructions().prompt_block() == "" and Instructions().is_empty


def test_system_prompt_includes_the_block():
    prompt = build_system_prompt("Эля", instructions=Instructions("Я Аня", "Кратко", True).prompt_block())
    assert "Я Аня" in prompt and prompt.index("Я Аня") < prompt.index("Текущая дата")
    assert "Инструкции, которые собеседник" not in build_system_prompt("Эля")


async def test_store_roundtrip_partial_updates_and_limits(store):
    assert await store.get(1) == Instructions()
    assert await store.update(1, about="  Я Аня  ") == Instructions("Я Аня", "", True)
    assert await store.update(1, style="Кратко") == Instructions("Я Аня", "Кратко", True)
    assert await store.update(1, enabled=False) == Instructions("Я Аня", "Кратко", False)
    assert await store.update(1, about="") == Instructions("", "Кратко", False)
    with pytest.raises(ValueError):
        await store.update(1, style="я" * (MAX_INSTRUCTION_LENGTH + 1))
    assert (await store.get(1)).style == "Кратко"
    assert await store.get(2) == Instructions()  # у каждого свои

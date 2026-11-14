"""Конвейер: шаги, исправления по замечаниям, бережный расход запросов."""

import json

import pytest

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult
from study.pipeline import TOPIC_MAX_CHARS, Material, StudyError, StudyService

LECTURE = (
    "Сегодня, значит, поговорим про реформу. Ну вот. В 1861 году Александр II подписал манифест. "
    "Запомните: выкупные платежи растянулись на 49 лет — это будет на экзамене. "
) * 10


class ScriptedProvider(BaseProvider):
    """Отвечает заранее заданными ответами по очереди и запоминает запросы."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__("script", "Script", ("m",))
        self.replies = list(replies)
        self.requests: list = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.requests.append({"messages": messages, "max_tokens": max_tokens, "temperature": temperature})
        return CompletionResult(text=self.replies.pop(0), provider="script", model=model)


def _service(replies: list[str]) -> tuple[StudyService, ScriptedProvider]:
    provider = ScriptedProvider(replies)
    client = AIClient([provider], max_tokens=100, temperature=0.9)
    return StudyService(client, assistant_name="Эля", max_tokens=8000), provider


EXTRACTION = json.dumps({
    "topic": "Реформа 1861",
    "terms": [{"term": "Выкупные платежи", "definition": "плата за землю"}],
    "facts": [{"value": "1861", "meaning": "год реформы"}, {"value": "49 лет", "meaning": "срок выкупа"}],
    "emphasis": ["Срок выкупа будет на экзамене"],
}, ensure_ascii=False)


def _notes_json(points: list[str], hints: list[str]) -> str:
    return json.dumps({
        "title": "Реформа 1861 года",
        "summary": "Отмена крепостного права и выкуп земли.",
        "sections": [
            {"heading": "Манифест", "points": ["В 1861 году подписан манифест"]},
            {"heading": "Выкуп", "points": points},
        ],
        "key_terms": [{"term": "Выкупные платежи", "definition": "плата за землю"}],
        "exam_hints": hints,
        "self_check": ["Почему выкуп затянулся?"],
    }, ensure_ascii=False)


async def test_notes_are_repaired_until_nothing_is_lost():
    service, provider = _service([
        EXTRACTION,
        "Вот конспект:\n```json\n" + _notes_json(["Крестьяне платили за землю"], []) + "\n```",
        _notes_json(["Выкуп растянулся на **49 лет**"], ["Срок выкупа — на экзамене"]),
    ])
    notes = await service.make_notes(Material(LECTURE), "full")

    assert len(provider.requests) == 3  # выписка, конспект, одно исправление
    repair = provider.requests[2]["messages"][-1].content
    assert "49 лет" in repair and "exam_hints" in repair
    assert provider.requests[1]["messages"][1].content.count("Ключевые сведения") == 1
    assert "Выкуп растянулся на **49 лет**" in notes.sections[1].points
    assert notes.exam_hints == ["Срок выкупа — на экзамене"]
    # учебные запросы идут с длинным ответом и низкой температурой, а не с настройками чата
    assert {r["max_tokens"] for r in provider.requests} == {8000}
    assert {r["temperature"] for r in provider.requests} == {0.4}


async def test_notes_from_topic_skip_extraction_and_are_marked():
    service, provider = _service([_notes_json(["Выкуп"], [])])
    notes = await service.make_notes(Material("Отмена крепостного права, 8 класс"), "short")
    assert len(provider.requests) == 1
    assert notes.from_topic
    system = provider.requests[0]["messages"][0].content
    assert "Материала нет" in system and "сокращение лекции" in system


async def test_unusable_answers_raise_after_one_repair():
    service, provider = _service([EXTRACTION, "не json", "опять не json"])
    with pytest.raises(StudyError):
        await service.make_notes(Material(LECTURE), "details")
    assert len(provider.requests) == 3


async def test_notes_with_remaining_issues_are_accepted_after_repair():
    bad = _notes_json(["Платили за землю"], [])
    service, provider = _service([EXTRACTION, bad, bad])
    notes = await service.make_notes(Material(LECTURE), "full")
    assert len(provider.requests) == 3 and notes.title == "Реформа 1861 года"


def _outline(n: int) -> str:
    slides = [{"type": "bullets", "title": f"Слайд {i}"} for i in range(n - 2)]
    slides += [{"type": "timeline", "title": "Хронология"}, {"type": "summary", "title": "Выводы"}]
    return json.dumps({"title": "Реформа", "subtitle": "", "slides": slides}, ensure_ascii=False)


async def test_outline_respects_requested_slide_count():
    service, provider = _service([_outline(5), _outline(8)])
    outline = await service.make_outline(Material("Реформа 1861 года, 8 слайдов"))
    assert len(outline.slides) == 8
    assert "Ровно 8 слайдов" in provider.requests[0]["messages"][0].content
    assert "нужно ровно 8" in provider.requests[1]["messages"][-1].content


async def test_outline_feedback_overrides_requested_count():
    service, provider = _service([_outline(5)])
    previous = await _service([_outline(6)])[0].make_outline(Material("Реформа, 6 слайдов"))
    outline = await service.make_outline(
        Material("Реформа, 6 слайдов"), previous=previous, feedback="убери один слайд"
    )
    assert len(outline.slides) == 5 and len(provider.requests) == 1  # без «исправлений» назад к 6


async def test_outline_feedback_is_sent_with_previous_plan():
    service, provider = _service([_outline(6)])
    previous = await _service([_outline(6)])[0].make_outline(Material("Реформа"))
    await service.make_outline(Material("Реформа"), previous=previous, feedback="убери второй слайд")
    messages = provider.requests[0]["messages"]
    assert messages[2].role == "assistant" and "Слайд 0" in messages[2].content
    assert "убери второй слайд" in messages[3].content


async def test_presentation_is_repaired_then_normalized():
    outline = await _service([_outline(3)])[0].make_outline(Material("Тема"))
    notes = "Что сказать: подробное пояснение к слайду для докладчика."
    too_many = {"title": "Реформа", "slides": [
        {"type": "bullets", "title": "Слайд 0", "bullets": [f"п{i}" for i in range(8)], "notes": notes},
        {"type": "timeline", "title": "Хронология", "events": [{"date": "1861", "text": "Манифест"}], "notes": notes},
        {"type": "summary", "title": "Выводы", "bullets": ["а", "б"], "notes": notes},
    ]}
    service, provider = _service([json.dumps(too_many, ensure_ascii=False)] * 2)
    presentation = await service.make_presentation(Material("Тема"), outline)

    repair = provider.requests[1]["messages"][-1].content
    assert "пунктов 8" in repair and "событий 1" in repair
    first, second = presentation.slides[0], presentation.slides[1]
    assert len(first.bullets) == 5 and "Не поместилось на слайд" in first.notes
    assert second.type == "bullets"  # хронология из одного события стала списком


def test_material_topic_detection():
    assert Material("Фотосинтез").is_topic
    assert not Material("x" * TOPIC_MAX_CHARS).is_topic
    assert Material("Фотосинтез, 7 слайдов").requested_slides == 7
    assert Material("x" * TOPIC_MAX_CHARS + " 7 слайдов").requested_slides is None

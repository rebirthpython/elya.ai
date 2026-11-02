from ai.context import build_context
from ai.providers.base import ChatMessage


def _history(n: int, size: int = 10) -> list[ChatMessage]:
    return [
        ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"{i:02d}" + "x" * (size - 2))
        for i in range(n)
    ]


def test_context_structure():
    ctx = build_context("SYS", _history(4), "вопрос", max_messages=10, max_chars=1000)
    assert ctx[0] == ChatMessage(role="system", content="SYS")
    assert ctx[-1] == ChatMessage(role="user", content="вопрос")
    assert [m.role for m in ctx[1:-1]] == ["user", "assistant", "user", "assistant"]


def test_context_limits_messages_and_starts_with_user():
    ctx = build_context("SYS", _history(10), "q", max_messages=3, max_chars=1000)
    middle = ctx[1:-1]
    # из последних 3 (7=assistant, 8=user, 9=assistant) ведущий assistant отброшен
    assert [m.content[:2] for m in middle] == ["08", "09"]
    assert middle[0].role == "user"


def test_context_respects_char_budget_keeping_newest():
    ctx = build_context("SYS", _history(10, size=100), "q" * 50, max_messages=10, max_chars=360)
    middle = ctx[1:-1]
    assert sum(len(m.content) for m in middle) + 50 <= 360
    assert middle[-1].content.startswith("09")


def test_current_message_always_included():
    ctx = build_context("SYS", _history(4), "q" * 5000, max_messages=10, max_chars=1000)
    assert len(ctx) == 2
    assert ctx[-1].content == "q" * 5000

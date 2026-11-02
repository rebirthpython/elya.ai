from bot.utils.formatting import markdown_to_html
from bot.utils.text import TELEGRAM_MESSAGE_LIMIT, split_text


def test_short_text_is_single_chunk():
    assert split_text("  привет  ") == ["привет"]


def test_long_text_respects_limit_and_keeps_content():
    paragraphs = [f"Абзац номер {i}. " + "слово " * 60 for i in range(40)]
    text = "\n\n".join(p.strip() for p in paragraphs)
    chunks = split_text(text, limit=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_text_without_spaces_is_hard_cut():
    chunks = split_text("x" * 9000)
    assert all(len(c) <= TELEGRAM_MESSAGE_LIMIT for c in chunks)
    assert "".join(chunks) == "x" * 9000


def test_code_fence_is_balanced_across_chunks():
    code = "\n".join(f"line_{i} = {i}" for i in range(300))
    chunks = split_text(f"Пример:\n```python\n{code}\n```\nГотово.", limit=1000)
    assert len(chunks) > 1
    assert all(c.count("```") % 2 == 0 for c in chunks)
    assert all(len(c) <= 1000 for c in chunks)


def test_markdown_basic_formatting():
    result = markdown_to_html("## Заголовок\n**жирный** и *курсив*, ~~нет~~\n- пункт")
    assert "<b>Заголовок</b>" in result
    assert "<b>жирный</b>" in result
    assert "<i>курсив</i>" in result
    assert "<s>нет</s>" in result
    assert "• пункт" in result


def test_markdown_escapes_html_and_code():
    result = markdown_to_html("a < b & `x<y>` and\n```\nif a < b:\n    pass\n```")
    assert "a &lt; b &amp;" in result
    assert "<code>x&lt;y&gt;</code>" in result
    assert "<pre>if a &lt; b:\n    pass</pre>" in result


def test_markdown_does_not_break_snake_case_and_math():
    result = markdown_to_html("my_var_name и 2*3*4")
    assert result == "my_var_name и 2*3*4"


def test_markdown_links_only_http():
    assert markdown_to_html("[сайт](https://example.com/a_b_c)") == (
        '<a href="https://example.com/a_b_c">сайт</a>'
    )
    assert "<a" not in markdown_to_html("[x](javascript:alert(1))")

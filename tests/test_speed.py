import asyncio

import httpx

from ai.client import AIClient
from ai.providers.base import BaseProvider, ChatMessage, CompletionResult
from ai.providers.openai_compatible import OpenAICompatibleProvider
from bot.utils.speed import speed_line

MESSAGES = [ChatMessage(role="user", content="hi")]


def _result(**kw):
    base = dict(text="x", provider="p", model="openai/gpt-oss-120b", tokens=100, first_token_s=1.24, generation_s=2.0)
    return CompletionResult(**{**base, **kw})


def test_speed_line_shows_rate_latency_and_model():
    assert speed_line(_result()) == "⚡ 50 ток/с · 1.2 с до ответа · gpt-oss-120b"


def test_speed_line_marks_estimated_tokens_and_hides_rate_for_instant_answers():
    assert speed_line(_result(tokens_estimated=True)).startswith("⚡ ~50 ток/с")
    assert "ток/с" not in speed_line(_result(generation_s=0.05))


def test_speed_line_is_empty_without_measurements():
    assert speed_line(CompletionResult(text="x", provider="reminders", model="")) == ""


class Streamer(BaseProvider):
    def __init__(self, tokens=0):
        super().__init__("s", "S", ("m",))
        self.tokens = tokens

    async def complete(self, messages, *, model, max_tokens, temperature):
        raise AssertionError

    async def stream(self, messages, *, model, max_tokens, temperature):
        await asyncio.sleep(0.1)
        yield "привет"
        await asyncio.sleep(0.2)
        yield " мир" * 10
        yield CompletionResult(text="привет" + " мир" * 10, provider="s", model=model, tokens=self.tokens)


async def test_client_measures_first_token_and_generation_time():
    client = AIClient([Streamer()], max_tokens=10, temperature=0.1)
    result = [e async for e in client.stream(MESSAGES)][-1]
    assert 0.08 < result.first_token_s < 2.0
    assert 0.15 < result.generation_s < 3.0
    assert result.tokens_estimated and result.tokens == round(len(result.text) / 3)


async def test_provider_usage_tokens_are_used_when_reported():
    client = AIClient([Streamer(tokens=42)], max_tokens=10, temperature=0.1)
    result = [e async for e in client.stream(MESSAGES)][-1]
    assert result.tokens == 42 and not result.tokens_estimated


async def test_openai_provider_reads_usage_from_stream_and_groq_extension():
    def sse(*chunks):
        return "".join(f"data: {c}\n\n" for c in chunks) + "data: [DONE]\n\n"

    async def run(body):
        handler = lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            name="t", title="T", base_url="https://x/v1", api_key="k", models=("m",), http_client=client
        )
        return [e async for e in provider.stream(MESSAGES, model="m", max_tokens=5, temperature=0.1)][-1]

    plain = sse('{"choices":[{"delta":{"content":"hi"}}]}',
                '{"choices":[],"usage":{"completion_tokens":7}}')
    groq = sse('{"choices":[{"delta":{"content":"hi"}}]}',
               '{"choices":[{"delta":{},"finish_reason":"stop"}],"x_groq":{"usage":{"completion_tokens":9}}}')
    assert (await run(plain)).tokens == 7
    assert (await run(groq)).tokens == 9

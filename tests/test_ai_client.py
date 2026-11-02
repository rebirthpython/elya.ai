import json

import httpx
import pytest

from ai.client import AIClient, AllProvidersFailedError, DailyLimitExceededError
from ai.providers.base import (
    AuthError,
    BadResponseError,
    BaseProvider,
    ChatMessage,
    CompletionResult,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
    RegionNotSupportedError,
)
from ai.providers.openai_compatible import OpenAICompatibleProvider

MESSAGES = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]


def _provider(handler) -> OpenAICompatibleProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleProvider(
        name="test", title="Test", base_url="https://api.test/v1/", api_key="secret",
        models=("m1",), http_client=client,
    )


async def _call(handler):
    return await _provider(handler).complete(MESSAGES, model="m1", max_tokens=100, temperature=0.5)


async def test_successful_completion_sends_openai_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "m1",
            "choices": [{"message": {"role": "assistant", "content": "<think>hmm</think> Привет!"},
                         "finish_reason": "stop"}],
        })

    result = await _call(handler)
    assert result.text == "Привет!"
    assert result.truncated is False
    assert seen["url"] == "https://api.test/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["messages"][1] == {"role": "user", "content": "hi"}
    assert seen["body"]["max_tokens"] == 100


async def test_truncated_flag():
    def handler(request):
        choice = {"message": {"content": "abc"}, "finish_reason": "length"}
        return httpx.Response(200, json={"choices": [choice]})

    assert (await _call(handler)).truncated is True


@pytest.mark.parametrize(
    ("status", "body", "headers", "error_type"),
    [
        (429, {"error": {"message": "quota"}}, {"retry-after": "12"}, RateLimitError),
        (401, {"error": {"message": "bad key"}}, {}, AuthError),
        (400, [{"error": {"code": 400, "message": "API key not valid."}}], {}, AuthError),
        (400, [{"error": {"message": "User location is not supported for the API use."}}], {},
         RegionNotSupportedError),
        (404, {"error": {"message": "no such model"}}, {}, ModelNotFoundError),
        (503, "oops", {}, ProviderError),
    ],
)
async def test_http_errors_are_classified(status, body, headers, error_type):
    def handler(request):
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    with pytest.raises(error_type) as info:
        await _call(handler)
    if error_type is RateLimitError:
        assert info.value.retry_after == 12.0


async def test_error_inside_200_and_empty_content():
    def with_error(request):
        return httpx.Response(200, json={"error": {"code": 429, "message": "upstream limit"}})

    def empty(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})

    with pytest.raises(RateLimitError):
        await _call(with_error)
    with pytest.raises(BadResponseError):
        await _call(empty)


async def test_timeout_is_unavailable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as info:
        await _call(handler)
    assert "timeout" in str(info.value)


class FakeProvider(BaseProvider):
    def __init__(self, name, models, outcomes):
        super().__init__(name, name.title(), tuple(models))
        self.outcomes = outcomes  # model -> list of exceptions/str
        self.calls: list[str] = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append(model)
        outcome = self.outcomes[model].pop(0) if len(self.outcomes[model]) > 1 else self.outcomes[model][0]
        if isinstance(outcome, Exception):
            raise outcome
        return CompletionResult(text=outcome, provider=self.name, model=model)


class FakeUsage:
    def __init__(self, limit):
        self.daily_limit = limit
        self.used = 0

    async def try_consume(self):
        if self.used >= self.daily_limit:
            return False
        self.used += 1
        return True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _rate(provider, model):
    return RateLimitError("limit", provider=provider, model=model, status_code=429)


async def test_fallback_to_next_model_and_provider():
    a = FakeProvider("a", ["a1", "a2"], {"a1": [_rate("a", "a1")], "a2": [AuthError("k", provider="a")]})
    b = FakeProvider("b", ["b1"], {"b1": ["ok from b"]})
    client = AIClient([a, b], max_tokens=10, temperature=0.1, clock=Clock())
    result = await client.complete(MESSAGES)
    assert result.text == "ok from b"
    assert a.calls == ["a1", "a2"] and b.calls == ["b1"]


async def test_rate_limited_model_is_skipped_during_cooldown():
    clock = Clock()
    a = FakeProvider("a", ["a1", "a2"], {"a1": [_rate("a", "a1"), "a1 back"], "a2": ["from a2"]})
    client = AIClient([a], max_tokens=10, temperature=0.1, base_cooldown=60, clock=clock)

    assert (await client.complete(MESSAGES)).text == "from a2"
    assert (await client.complete(MESSAGES)).text == "from a2"
    assert a.calls == ["a1", "a2", "a2"]

    clock.now += 61
    assert (await client.complete(MESSAGES)).text == "a1 back"


async def test_provider_level_error_skips_other_models():
    a = FakeProvider("a", ["a1", "a2"], {"a1": [RegionNotSupportedError("geo", provider="a")], "a2": ["x"]})
    client = AIClient([a], max_tokens=10, temperature=0.1, clock=Clock())
    with pytest.raises(AllProvidersFailedError) as info:
        await client.complete(MESSAGES)
    assert a.calls == ["a1"]
    assert info.value.kind == "config"
    # провайдер на паузе: запроса нет, но причина ошибки сохраняется
    with pytest.raises(AllProvidersFailedError) as info:
        await client.complete(MESSAGES)
    assert a.calls == ["a1"]
    assert info.value.kind == "config"


async def test_all_failed_kinds():
    a = FakeProvider("a", ["a1"], {"a1": [_rate("a", "a1")]})
    client = AIClient([a], max_tokens=10, temperature=0.1, clock=Clock())
    with pytest.raises(AllProvidersFailedError) as info:
        await client.complete(MESSAGES)
    assert info.value.kind == "rate_limit"
    # повторный вызов: модель на паузе, запроса нет, kind всё равно rate_limit
    with pytest.raises(AllProvidersFailedError) as info:
        await client.complete(MESSAGES)
    assert info.value.kind == "rate_limit"
    assert a.calls == ["a1"]


async def test_daily_limit_counts_every_attempt():
    a = FakeProvider("a", ["a1", "a2"], {"a1": [BadResponseError("bad", provider="a")], "a2": ["ok"]})
    usage = FakeUsage(limit=3)
    client = AIClient([a], max_tokens=10, temperature=0.1, usage=usage, clock=Clock())
    assert (await client.complete(MESSAGES)).text == "ok"
    assert usage.used == 2
    # 3-я попытка (a1) ещё укладывается в лимит, 4-я (a2) — уже нет
    with pytest.raises(DailyLimitExceededError):
        await client.complete(MESSAGES)
    assert usage.used == 3

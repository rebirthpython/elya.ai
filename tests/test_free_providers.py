"""Много бесплатных провайдеров: паузы для пропавших и зависших моделей, ошибки сети,
потолок токенов, GigaChat с OAuth-токеном и сборка провайдеров из настроек."""

import json

import certifi
import httpx
import pytest

from ai.client import AIClient, AllProvidersFailedError
from ai.providers import build_providers
from ai.providers.base import (
    AuthError,
    BaseProvider,
    CertificateError,
    ChatMessage,
    CompletionResult,
    ModelNotFoundError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnreachableError,
)
from ai.providers.gigachat import GigaChatProvider
from ai.providers.openai_compatible import OpenAICompatibleProvider
from config.settings import ProviderConfig

MESSAGES = [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="hi")]


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeProvider(BaseProvider):
    def __init__(self, name, models, outcomes, max_output_tokens=0):
        super().__init__(name, name.title(), tuple(models), max_output_tokens=max_output_tokens)
        self.outcomes = outcomes  # model -> список исключений/строк, последний повторяется
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append((model, max_tokens))
        queue = self.outcomes[model]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, Exception):
            raise outcome
        return CompletionResult(text=outcome, provider=self.name, model=model)

    async def aclose(self):
        self.closed = True

    def models_called(self):
        return [model for model, _ in self.calls]


# ---------- AIClient ----------


async def test_missing_model_is_skipped_for_hours():
    clock = Clock()
    a = FakeProvider("a", ["gone", "ok"], {
        "gone": [ModelNotFoundError("no such model", provider="a", model="gone"), "back"],
        "ok": ["from ok"],
    })
    client = AIClient([a], max_tokens=10, temperature=0.1, clock=clock)
    assert (await client.complete(MESSAGES)).text == "from ok"
    assert client.is_paused("a", "gone") and not client.is_paused("a")

    clock.now += 3 * 3600  # дольше обычного максимума паузы (1 час), но меньше 6 часов
    assert (await client.complete(MESSAGES)).text == "from ok"
    assert a.models_called() == ["gone", "ok", "ok"]

    clock.now += 3 * 3600 + 1
    assert (await client.complete(MESSAGES)).text == "back"


async def test_timed_out_model_is_not_waited_for_again():
    clock = Clock()
    a = FakeProvider("a", ["slow", "fast"], {
        "slow": [ProviderTimeoutError("timeout", provider="a", model="slow")],
        "fast": ["quick"],
    })
    client = AIClient([a], max_tokens=10, temperature=0.1, base_cooldown=60, clock=clock)
    await client.complete(MESSAGES)
    await client.complete(MESSAGES)
    assert a.models_called() == ["slow", "fast", "fast"]
    clock.now += 61
    await client.complete(MESSAGES)
    assert a.models_called()[-2:] == ["slow", "fast"]


async def test_plain_server_error_does_not_pause_the_model():
    a = FakeProvider("a", ["m"], {
        "m": [ProviderUnavailableError("HTTP 503", provider="a", model="m", status_code=503), "ok"],
    })
    client = AIClient([a], max_tokens=10, temperature=0.1, clock=Clock())
    with pytest.raises(AllProvidersFailedError) as info:
        await client.complete(MESSAGES)
    assert info.value.kind == "unavailable"
    assert (await client.complete(MESSAGES)).text == "ok"


async def test_unreachable_provider_skips_all_its_models():
    a = FakeProvider("a", ["a1", "a2"], {
        "a1": [ProviderUnreachableError("cannot connect", provider="a", model="a1")],
        "a2": ["never"],
    })
    b = FakeProvider("b", ["b1"], {"b1": ["from b"]})
    client = AIClient([a, b], max_tokens=10, temperature=0.1, clock=Clock())
    assert (await client.complete(MESSAGES)).text == "from b"
    assert (await client.complete(MESSAGES)).text == "from b"
    assert a.models_called() == ["a1"] and client.is_paused("a")


async def test_all_models_missing_is_reported_as_config_problem():
    a = FakeProvider("a", ["x"], {"x": [ModelNotFoundError("gone", provider="a", model="x")]})
    b = FakeProvider("b", ["y"], {"y": [AuthError("bad key", provider="b")]})
    client = AIClient([a, b], max_tokens=10, temperature=0.1, clock=Clock())
    with pytest.raises(AllProvidersFailedError) as info:
        await client.complete(MESSAGES)
    assert info.value.kind == "config"


async def test_provider_token_cap_limits_max_tokens():
    capped = FakeProvider("capped", ["c"], {"c": [ModelNotFoundError("x", provider="capped")]},
                          max_output_tokens=4000)
    free = FakeProvider("free", ["f"], {"f": ["ok"]})
    client = AIClient([capped, free], max_tokens=2048, temperature=0.1, clock=Clock())
    await client.complete(MESSAGES, max_tokens=8192)
    assert capped.calls == [("c", 4000)]  # длинный ответ урезан до лимита тарифа
    assert free.calls == [("f", 8192)]  # у остальных — как просили
    await client.complete(MESSAGES)
    assert free.calls[-1] == ("f", 2048)


async def test_aclose_closes_every_provider():
    a, b = FakeProvider("a", ["a1"], {"a1": ["x"]}), FakeProvider("b", ["b1"], {"b1": ["y"]})
    client = AIClient([a, b], max_tokens=10, temperature=0.1)
    await client.aclose()
    assert a.closed and b.closed


# ---------- классификация ошибок OpenAI-совместимого API ----------


def _provider(handler, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="test", title="Test", base_url="https://api.test/v1", api_key="k",
        models=("m1",), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


async def _call(handler):
    return await _provider(handler).complete(MESSAGES, model="m1", max_tokens=100, temperature=0.5)


@pytest.mark.parametrize(
    ("status", "message", "error_type"),
    [
        (400, "The model `llama3-70b-8192` has been decommissioned and is no longer supported.",
         ModelNotFoundError),
        (400, "Unknown model: openai/gpt-9", ModelNotFoundError),
        (400, "Invalid model: mistral-tiny", ModelNotFoundError),
        (422, "Model 'x' not found", ModelNotFoundError),
        (400, "The requested model 'x/y' is not supported by any provider you have enabled.",
         ModelNotFoundError),
        (400, "max_tokens is too large", None),  # обычная ошибка запроса — не «модель пропала»
        (403, "Country, region, or territory not supported", None),
    ],
)
async def test_missing_model_is_recognised_in_400(status, message, error_type):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": message}})

    with pytest.raises(Exception) as info:
        await _call(handler)
    if error_type is None:
        assert not isinstance(info.value, ModelNotFoundError)
    else:
        assert isinstance(info.value, error_type)


@pytest.mark.parametrize(
    ("exc", "error_type"),
    [
        (httpx.ConnectError("[Errno -2] Name or service not known"), ProviderUnreachableError),
        (httpx.ConnectTimeout("connect timed out"), ProviderUnreachableError),
        (httpx.ReadTimeout("slow"), ProviderTimeoutError),
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
         CertificateError),
    ],
)
async def test_network_errors_are_classified(exc, error_type):
    def handler(request):
        raise exc

    with pytest.raises(error_type) as info:
        await _call(handler)
    if error_type is CertificateError:
        assert info.value.provider_level and "TEST_CA_BUNDLE" in str(info.value)


async def test_network_errors_in_stream_are_classified():
    def handler(request):
        raise httpx.ConnectError("refused")

    provider = _provider(handler)
    with pytest.raises(ProviderUnreachableError):
        async for _ in provider.stream(MESSAGES, model="m1", max_tokens=10, temperature=0.5):
            pass


# ---------- GigaChat ----------

AUTH_URL = "https://ngw.test:9443/api/v2/oauth"


class GigaServer:
    """Имитация GigaChat: выдаёт токены и отвечает на запросы с действующим токеном."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.tokens_issued = 0
        self.valid: set[str] = set()
        self.auth_requests: list[httpx.Request] = []
        self.chat_requests: list[httpx.Request] = []
        self.reject_key = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == AUTH_URL:
            self.auth_requests.append(request)
            if self.reject_key:
                return httpx.Response(401, json={"code": 6, "message": "Can't decode 'Authorization' header"})
            self.tokens_issued += 1
            token = f"tok{self.tokens_issued}"
            self.valid.add(token)
            expires_ms = int((self.clock.now + 1800) * 1000)
            return httpx.Response(200, json={"access_token": token, "expires_at": expires_ms})
        self.chat_requests.append(request)
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token not in self.valid:
            return httpx.Response(401, json={"status": 401, "message": "Token has expired"})
        body = json.loads(request.content)
        if body.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "При"}}]},
                {"choices": [{"delta": {"content": "вет"}, "finish_reason": "stop"}]},
            ]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": "Привет"}, "finish_reason": "stop"}],
        })


def _giga(server: GigaServer, clock: Clock) -> GigaChatProvider:
    return GigaChatProvider(
        name="gigachat", title="GigaChat", base_url="https://giga.test/api/v1",
        auth_url=AUTH_URL, auth_key="Basic c2VjcmV0", scope="GIGACHAT_API_PERS",
        models=("GigaChat-2",), clock=clock,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
    )


async def _ask(provider):
    return await provider.complete(MESSAGES, model="GigaChat-2", max_tokens=100, temperature=0.5)


async def test_gigachat_exchanges_key_for_token_and_reuses_it():
    clock = Clock(1_700_000_000.0)
    server = GigaServer(clock)
    provider = _giga(server, clock)

    assert (await _ask(provider)).text == "Привет"
    assert (await _ask(provider)).text == "Привет"
    assert server.tokens_issued == 1
    auth = server.auth_requests[0]
    assert auth.headers["authorization"] == "Basic c2VjcmV0"  # префикс «Basic » не задваивается
    assert auth.headers["rquid"] and auth.content == b"scope=GIGACHAT_API_PERS"
    chat = server.chat_requests[0]
    assert str(chat.url) == "https://giga.test/api/v1/chat/completions"
    assert chat.headers["authorization"] == "Bearer tok1"
    assert json.loads(chat.content)["model"] == "GigaChat-2"


async def test_gigachat_refreshes_token_before_it_expires():
    clock = Clock(1_700_000_000.0)
    server = GigaServer(clock)
    provider = _giga(server, clock)
    await _ask(provider)
    clock.now += 1800 - 60  # до истечения меньше запаса — берём новый токен заранее
    await _ask(provider)
    assert server.tokens_issued == 2


async def test_gigachat_retries_once_when_token_was_revoked():
    clock = Clock(1_700_000_000.0)
    server = GigaServer(clock)
    provider = _giga(server, clock)
    await _ask(provider)
    server.valid.clear()  # сервер отозвал токен раньше срока
    clock.now += 60
    assert (await _ask(provider)).text == "Привет"
    assert server.tokens_issued == 2

    pieces = []
    server.valid.clear()
    clock.now += 60
    async for event in provider.stream(MESSAGES, model="GigaChat-2", max_tokens=10, temperature=0.5):
        pieces.append(event)
    assert pieces[:-1] == ["При", "вет"] and pieces[-1].text == "Привет"
    assert server.tokens_issued == 3


async def test_gigachat_bad_key_is_a_provider_level_error():
    clock = Clock(1_700_000_000.0)
    server = GigaServer(clock)
    server.reject_key = True
    provider = _giga(server, clock)
    with pytest.raises(AuthError) as info:
        await _ask(provider)
    assert info.value.provider_level
    assert len(server.auth_requests) == 1 and server.chat_requests == []


# ---------- сборка провайдеров ----------


async def test_build_providers_kinds_and_own_clients():
    shared = httpx.AsyncClient()
    configs = (
        ProviderConfig("gemini", "Gemini", "https://g.test", "k", ("g1",)),
        ProviderConfig("github", "GitHub", "https://gh.test", "k", ("o1",), max_output_tokens=4000,
                       ca_bundle=certifi.where()),
        ProviderConfig("gigachat", "GigaChat", "https://giga.test", "key", ("GigaChat-2",),
                       kind="gigachat", auth_url=AUTH_URL, scope="GIGACHAT_API_PERS"),
    )
    gemini, github, giga = build_providers(configs, shared)
    assert type(gemini) is OpenAICompatibleProvider and isinstance(giga, GigaChatProvider)
    assert github.max_output_tokens == 4000
    assert gemini._http is shared and giga._http is shared
    assert github._http is not shared  # свой клиент с дополнительным сертификатом
    await AIClient([gemini, github, giga], max_tokens=10, temperature=0.1).aclose()
    assert github._http.is_closed and not shared.is_closed
    await shared.aclose()

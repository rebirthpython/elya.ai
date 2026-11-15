"""Сервисы картинок: запросы к API, разбор ответов, ошибки и перебор по приоритету."""

import base64
import json
from dataclasses import replace

import httpx
import pytest

from config.settings import ImageProviderConfig
from database.models import DailyImageUsage
from database.session import create_engine, create_session_factory, init_db
from services.imagegen import (
    CloudflareBackend,
    ImageGenerationError,
    ImageGenerator,
    ImageLimitError,
    ImageProviderError,
    OpenAIImagesBackend,
    build_image_backends,
)
from services.usage import UsageTracker

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
B64_JPEG = base64.b64encode(JPEG).decode()

CF = ImageProviderConfig(
    "cloudflare", "Cloudflare Workers AI", "cloudflare",
    "https://api.cloudflare.com/client/v4/accounts/abc123/ai/run", "cf-token",
    "@cf/black-forest-labs/flux-1-schnell",
)
POLL = ImageProviderConfig(
    "pollinations", "Pollinations", "openai", "https://gen.pollinations.ai/v1", "sk_poll",
    "black-forest-labs/flux.1-schnell",
)
CUSTOM = ImageProviderConfig(
    "custom", "Custom OpenAI-compatible Images API", "openai", "https://api.example.com/v1", "key",
    "gpt-image-1",
)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _backend(config, handler):
    return build_image_backends([config], _client(handler))[0]


async def test_cloudflare_flux_sends_only_prompt_and_decodes_base64():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": {"image": B64_JPEG}, "success": True, "errors": []})

    backend = _backend(CF, handler)
    assert isinstance(backend, CloudflareBackend)
    image = await backend.generate("a cat in a hat", width=1024, height=1536, seed=7)
    assert image.data == JPEG and image.mime == "image/jpeg" and image.extension == "jpg"
    assert image.provider == "Cloudflare Workers AI" and image.model == CF.model
    assert seen["url"] == f"{CF.base_url}/@cf/black-forest-labs/flux-1-schnell"
    assert seen["auth"] == "Bearer cf-token"
    # FLUX.1 schnell на Cloudflare не принимает лишних полей
    assert seen["body"] == {"prompt": "a cat in a hat"}
    assert image.data_url().startswith("data:image/jpeg;base64,")


async def test_cloudflare_other_models_get_size_and_may_return_binary():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    config = replace(CF, model="@cf/leonardo/phoenix-1.0")
    image = await _backend(config, handler).generate("castle", width=1536, height=1024, seed=42)
    assert image.data == JPEG
    assert seen["body"] == {"prompt": "castle", "width": 1536, "height": 1024, "seed": 42}


async def test_pollinations_sends_seed_and_reads_b64_json():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    backend = _backend(POLL, handler)
    assert isinstance(backend, OpenAIImagesBackend)
    image = await backend.generate("a fox", width=1024, height=1024, seed=99)
    assert image.mime == "image/png" and image.data == PNG
    assert seen["url"] == "https://gen.pollinations.ai/v1/images/generations"
    assert seen["auth"] == "Bearer sk_poll"
    assert seen["body"] == {
        "model": "black-forest-labs/flux.1-schnell", "prompt": "a fox", "n": 1,
        "size": "1024x1024", "seed": 99,
    }


async def test_custom_openai_api_gets_no_seed_and_url_answer_is_downloaded():
    seen = {"requests": []}

    def handler(request):
        seen["requests"].append(request)
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/a.png"}]})
        assert "authorization" not in request.headers  # ключ не уходит на чужой адрес
        return httpx.Response(200, content=PNG)

    image = await _backend(CUSTOM, handler).generate("a fox", width=1536, height=1024, seed=1)
    assert image.data == PNG
    assert "seed" not in seen["body"] and seen["body"]["size"] == "1536x1024"
    assert str(seen["requests"][1].url) == "https://cdn.example.com/a.png"


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"retry-after": "30"}), "rate_limit"),
        (httpx.Response(402, json={"error": {"message": "Insufficient pollen balance"}}), "rate_limit"),
        (httpx.Response(400, json={"success": False, "errors": [{"code": 4006, "message": "you have used up your daily free allocation of 10,000 neurons"}]}), "rate_limit"),
        (httpx.Response(401, json={"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}), "config"),
        (httpx.Response(404, json={"error": "model not found"}), "config"),
        (httpx.Response(400, json={"success": False, "errors": [{"code": 3030, "message": "Input prompt contains NSFW content"}]}), "rejected"),
        (httpx.Response(500, text="boom"), "unavailable"),
        (httpx.Response(200, json={"result": {"image": base64.b64encode(b"not an image").decode()}}), "unavailable"),
        (httpx.Response(200, json={"result": {}}), "unavailable"),
        (httpx.Response(200, text="not json"), "unavailable"),
    ],
)
async def test_cloudflare_errors_are_classified(response, kind):
    with pytest.raises(ImageProviderError) as info:
        await _backend(CF, lambda request: response).generate("x", width=1024, height=1024, seed=1)
    assert info.value.kind == kind and info.value.provider == "cloudflare"
    if response.status_code == 429:
        assert info.value.retry_after == 30


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)),
        lambda r: httpx.Response(200, json={"data": []}),
        lambda r: httpx.Response(200, json={"data": [{"url": "ftp://x"}]}),
    ],
)
async def test_openai_network_and_format_errors(handler):
    with pytest.raises(ImageProviderError) as info:
        await _backend(POLL, handler).generate("x", width=1024, height=1024, seed=1)
    assert info.value.kind == "unavailable"


class FakeBackend:
    def __init__(self, name, *, errors=(), image=PNG):
        self.name, self.title, self.model = name, name.title(), f"{name}-model"
        self.errors = list(errors)
        self.image = image
        self.calls = []

    async def generate(self, prompt, *, width, height, seed):
        from services.imagegen import GeneratedImage

        self.calls.append((prompt, width, height, seed))
        if self.errors:
            kind = self.errors.pop(0)
            raise ImageProviderError("fail", provider=self.name, kind=kind)
        return GeneratedImage(self.image, "image/png", self.title, self.model)


async def test_generator_falls_back_and_pauses_a_limited_service():
    now = [0.0]
    first = FakeBackend("first", errors=["rate_limit"])
    second = FakeBackend("second")
    generator = ImageGenerator([first, second], clock=lambda: now[0], base_cooldown=60)
    assert generator.enabled and generator.titles == ["First", "Second"]

    image = await generator.generate("a cat", orientation="portrait")
    assert image.provider == "Second"
    assert first.calls[0][1:3] == (1024, 1536)

    await generator.generate("a dog")  # первый сервис на паузе
    assert len(first.calls) == 1 and len(second.calls) == 2

    now[0] += 301  # пауза по лимиту — 5 × base_cooldown
    await generator.generate("a bird")
    assert len(first.calls) == 2


async def test_generator_uses_new_seed_each_time_and_skips_nothing_after_a_one_off_failure():
    flaky = FakeBackend("flaky", errors=["unavailable"])
    other = FakeBackend("other")
    generator = ImageGenerator([flaky, other])
    await generator.generate("x")
    await generator.generate("x")
    assert len(flaky.calls) == 2  # после сбоя сервис не ставится на паузу
    seeds = {call[3] for call in other.calls + flaky.calls}
    assert len(seeds) >= 2


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (["rejected", "rate_limit"], "rejected"),
        (["rate_limit", "unavailable"], "rate_limit"),
        (["config", "config"], "config"),
        (["config", "unavailable"], "unavailable"),
    ],
)
async def test_generator_reports_the_most_useful_failure(kinds, expected):
    backends = [FakeBackend(f"b{i}", errors=[kind]) for i, kind in enumerate(kinds)]
    with pytest.raises(ImageGenerationError) as info:
        await ImageGenerator(backends).generate("x")
    assert info.value.kind == expected


async def test_generator_without_backends_and_daily_limit(tmp_path):
    with pytest.raises(ImageGenerationError):
        await ImageGenerator([]).generate("x")
    assert not ImageGenerator([]).enabled

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'img.db'}")
    await init_db(engine)
    usage = UsageTracker(create_session_factory(engine), 2, table=DailyImageUsage)
    text_usage = UsageTracker(create_session_factory(engine), 100)
    generator = ImageGenerator([FakeBackend("only")], usage=usage)
    await generator.generate("1")
    await generator.generate("2")
    with pytest.raises(ImageLimitError) as info:
        await generator.generate("3")
    assert info.value.limit == 2
    assert await usage.used_today() == 2 and await text_usage.used_today() == 0  # счётчики раздельные
    await engine.dispose()

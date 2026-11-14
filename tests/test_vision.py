import base64
import json

import httpx
import pytest

from ai.client import AIClient, NoVisionModelError
from ai.context import build_context
from ai.providers.base import BaseProvider, ChatMessage, CompletionResult
from ai.providers.openai_compatible import OpenAICompatibleProvider
from config.settings import load_settings
from services.vision import (
    DEFAULT_QUESTION,
    MAX_IMAGE_BYTES,
    ImageError,
    history_text,
    sniff_image,
    to_data_url,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64


# ---------- картинки ----------


@pytest.mark.parametrize(
    ("data", "mime"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"), (GIF, "image/gif"),
     (b"<svg></svg>", None), (b"%PDF-1.7", None), (b"", None)],
)
def test_sniff_image(data, mime):
    assert sniff_image(data) == mime


def test_to_data_url_and_errors():
    url = to_data_url(JPEG)
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == JPEG
    for bad, reason in ((b"", "empty"), (b"hello", "not_image"), (JPEG + b"0" * MAX_IMAGE_BYTES, "too_large")):
        with pytest.raises(ImageError) as info:
            to_data_url(bad)
        assert str(info.value) == reason


def test_history_text():
    assert history_text("  что это?  ") == "📷 Фото: что это?"
    assert history_text("") == "📷 Фото"


# ---------- формат запроса ----------


async def test_provider_sends_images_as_content_parts():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Кот"}, "finish_reason": "stop"}]})

    provider = OpenAICompatibleProvider(
        name="t", title="T", base_url="https://api.test/v1", api_key="k", models=("m",),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    url = to_data_url(PNG)
    await provider.complete(
        [ChatMessage("system", "sys"), ChatMessage("user", "Что это?", images=(url,))],
        model="m", max_tokens=10, temperature=0.1,
    )
    system, user = seen["body"]["messages"]
    assert system == {"role": "system", "content": "sys"}  # без картинок — обычная строка
    assert user["content"] == [
        {"type": "text", "text": "Что это?"},
        {"type": "image_url", "image_url": {"url": url}},
    ]


def test_context_attaches_images_only_to_the_current_message():
    history = [ChatMessage("user", "привет"), ChatMessage("assistant", "привет!")]
    ctx = build_context("SYS", history, "что это?", max_messages=10, max_chars=1000, images=("data:x",))
    assert [m.images for m in ctx] == [(), (), (), ("data:x",)]


# ---------- выбор моделей ----------


class Recorder(BaseProvider):
    def __init__(self, name, models, vision):
        super().__init__(name, name, models, vision)
        self.calls = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append(model)
        return CompletionResult(text=f"{self.name}/{model}", provider=self.name, model=model)


async def test_photos_go_only_to_vision_models():
    text_only = Recorder("router", ("llama",), ())
    gemini = Recorder("gemini", ("flash", "lite"), ("flash",))
    client = AIClient([text_only, gemini], max_tokens=10, temperature=0.1)
    assert client.has_vision and [(p.name, m) for p, m in client.vision_models] == [("gemini", "flash")]

    photo = [ChatMessage("user", "что это?", images=("data:x",))]
    assert (await client.complete(photo)).text == "gemini/flash"
    # выбранная в настройках текстовая модель для фото игнорируется
    assert (await client.complete(photo, prefer=("router", "llama"))).text == "gemini/flash"
    assert text_only.calls == []
    # без картинок — обычный порядок
    assert (await client.complete([ChatMessage("user", "привет")])).text == "router/llama"


async def test_no_vision_model_is_a_clear_error():
    client = AIClient([Recorder("router", ("llama",), ())], max_tokens=10, temperature=0.1)
    assert client.has_vision is False
    with pytest.raises(NoVisionModelError):
        await client.complete([ChatMessage("user", "что это?", images=("data:x",))])


# ---------- настройки ----------


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("AI_PROVIDERS", "GEMINI_API_KEY", "GEMINI_MODELS", "GEMINI_VISION_MODELS",
                "GROQ_API_KEY", "GROQ_MODELS", "GROQ_VISION_MODELS", "OPENROUTER_API_KEY",
                "OPENROUTER_MODELS", "OPENROUTER_VISION_MODELS", "WEBAPP_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1")
    return monkeypatch


def test_vision_models_defaults_and_overrides(clean_env):
    clean_env.setenv("AI_PROVIDERS", "gemini,groq,openrouter")
    clean_env.setenv("GEMINI_API_KEY", "g")
    clean_env.setenv("GROQ_API_KEY", "q")
    clean_env.setenv("GROQ_MODELS", "llama-3.3-70b-versatile")
    clean_env.setenv("OPENROUTER_API_KEY", "o")
    clean_env.setenv("OPENROUTER_MODELS", "a:free")
    gemini, groq, openrouter = load_settings(env_file=None).providers
    assert gemini.vision_models == gemini.models  # «*» — все модели Gemini
    assert groq.vision_models == ("meta-llama/llama-4-scout-17b-16e-instruct",)
    assert openrouter.vision_models == ()

    clean_env.setenv("GEMINI_VISION_MODELS", "-")
    clean_env.setenv("OPENROUTER_VISION_MODELS", "qwen/qwen2.5-vl:free, x/y")
    gemini, _, openrouter = load_settings(env_file=None).providers
    assert gemini.vision_models == ()
    assert openrouter.vision_models == ("qwen/qwen2.5-vl:free", "x/y")

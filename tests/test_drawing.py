"""Рисование в диалоге: распознавание просьбы, промпт, история, «ещё вариант», настройки."""

import json

import pytest

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult, ProviderUnavailableError
from ai.prompts import build_system_prompt
from config.settings import ConfigError, load_settings
from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.drawing import (
    AGAIN_TEXT,
    DrawingService,
    DrawPlan,
    fallback_plan,
    history_text,
    plan_from_model,
    strip_intent,
)
from services.imagegen import GeneratedImage, ImageGenerationError, ImageGenerator, ImageProviderError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.mark.parametrize(
    "text",
    [
        "нарисуй кота в шляпе",
        "Нарисуйте, пожалуйста, закат над морем",
        "можешь нарисовать собаку?",
        "Сгенерируй мне картинку с драконом",
        "создай красивую картинку моря",
        "сделай аватарку в стиле аниме",
        "изобрази город будущего",
        "нарисуешь мне единорога?",
        "А теперь нарисуй его зимой",
        "придумай логотип для кофейни",
        "draw a cat",
        "generate an image of a castle",
    ],
)
def test_drawing_requests_are_recognized(text):
    assert DrawingService.looks_like_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "как научиться рисовать?",
        "я люблю рисовать",
        "что изображено на фото",
        "картинка не грузится",
        "напомни завтра в 9 позвонить маме",
        "сделай конспект",
        "withdraw money",
        "",
    ],
)
def test_ordinary_messages_are_not_drawing_requests(text):
    assert not DrawingService.looks_like_request(text)


@pytest.mark.parametrize(
    ("text", "description"),
    [
        ("нарисуй кота в шляпе", "кота в шляпе"),
        ("Эля, нарисуй-ка мне, пожалуйста, слона", "слона"),
        ("можешь нарисовать собаку?", "собаку"),
        ("сделай аватарку в стиле аниме", "аватарку в стиле аниме"),
        ("Сгенерируй мне картинку с драконом", "картинку с драконом"),
        ("нарисуй", "нарисуй"),
    ],
)
def test_strip_intent_keeps_the_description(text, description):
    assert strip_intent(text) == description


def test_plan_from_model_reads_json_and_validates_fields():
    raw = '```json\n{"draw": true, "prompt": "A fluffy cat wearing a top hat, watercolor", ' \
          '"title": "Кот в шляпе.", "orientation": "portrait"}\n```'
    plan = plan_from_model(raw, "нарисуй кота в шляпе", explicit=False)
    assert plan == DrawPlan(
        request="нарисуй кота в шляпе",
        prompt="A fluffy cat wearing a top hat, watercolor",
        title="Кот в шляпе",
        orientation="portrait",
    )
    odd = plan_from_model('{"draw": true, "prompt": "A cat", "orientation": "diagonal"}', "нарисуй кота", explicit=False)
    assert odd.orientation == "square" and odd.title == "кота"


def test_plan_from_model_declines_and_falls_back():
    assert plan_from_model('{"draw": false}', "нарисуй мне план на неделю", explicit=False) is None
    # командой /draw просят картинку явно — рисуем даже при «draw: false»
    assert plan_from_model('{"draw": false}', "план", explicit=True) == fallback_plan("план")
    assert plan_from_model("не JSON", "нарисуй кота", explicit=False) == fallback_plan("нарисуй кота")
    assert plan_from_model('{"draw": true, "prompt": ""}', "нарисуй кота", explicit=False).prompt == "кота"


def test_history_text_mentions_title_and_prompt():
    text = history_text(DrawPlan("нарисуй кота", "A cat", "Кот"))
    assert text.startswith("🎨 Нарисовала картинку: Кот") and "A cat" in text


class PromptWriter(BaseProvider):
    def __init__(self, answer: str | None = None, *, fail: bool = False) -> None:
        super().__init__("writer", "Writer AI", ("w1",))
        self.answer = answer
        self.fail = fail
        self.calls: list = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append(messages)
        if self.fail:
            raise ProviderUnavailableError("down", provider=self.name, model=model)
        return CompletionResult(text=self.answer, provider=self.name, model=model)


class FakeBackend:
    def __init__(self, errors=()):
        self.name, self.title, self.model = "fake", "Fake Images", "fake-1"
        self.errors = list(errors)
        self.prompts: list = []

    async def generate(self, prompt, *, width, height, seed):
        self.prompts.append((prompt, width, height))
        if self.errors:
            raise ImageProviderError("fail", provider=self.name, kind=self.errors.pop(0))
        return GeneratedImage(PNG, "image/png", self.title, self.model)


@pytest.fixture
async def memory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'draw.db'}")
    await init_db(engine)
    yield MemoryManager(MessageRepository(create_session_factory(engine), store_limit=500), history_limit=30)
    await engine.dispose()


def _service(memory, writer=None, backend=None):
    ai = AIClient([writer], max_tokens=256, temperature=0.5) if writer else None
    backend = backend or FakeBackend()
    return DrawingService(ImageGenerator([backend]), ai, memory, assistant_name="Эля"), backend


ANSWER = json.dumps(
    {"draw": True, "prompt": "A cozy cabin in snowy mountains at sunset", "title": "Домик в горах", "orientation": "landscape"},
    ensure_ascii=False,
)


async def test_plan_uses_recent_dialog_and_draw_saves_history(memory):
    await memory.save_exchange(111, "Мне нравятся горы", "Горы — это прекрасно!")
    writer = PromptWriter(ANSWER)
    service, backend = _service(memory, writer)
    assert service.enabled

    plan = await service.plan(111, "  нарисуй   домик там  ")
    system, user = writer.calls[0]
    assert "Если сообщение не просьба создать картинку" in system.content
    assert "Пользователь: Мне нравятся горы" in user.content and "Эля: Горы — это прекрасно!" in user.content
    assert user.content.endswith("Просьба: нарисуй домик там")
    assert plan.orientation == "landscape" and plan.request == "нарисуй домик там"

    drawing = await service.draw(111, plan)
    assert drawing.caption == "🎨 Домик в горах" and drawing.image.data == PNG
    assert backend.prompts == [("A cozy cabin in snowy mountains at sunset", 1536, 1024)]
    history = await memory.get_history(111)
    assert history[-2].content == "нарисуй домик там"
    assert history[-1].content == history_text(plan)

    again = await service.again(111, drawing.id)
    assert again.id != drawing.id and again.plan == plan
    history = await memory.get_history(111)
    assert history[-2].content == AGAIN_TEXT and len(history) == 6
    assert await service.again(222, drawing.id) is None  # чужая картинка
    assert await service.again(111, 999) is None


async def test_explicit_requests_skip_the_decision_and_ai_failure_falls_back(memory):
    writer = PromptWriter(ANSWER)
    service, _ = _service(memory, writer)
    await service.plan(111, "домик", explicit=True)
    assert "draw всегда true" in writer.calls[0][0].content

    broken, _ = _service(memory, PromptWriter(fail=True))
    plan = await broken.plan(111, "нарисуй кота в шляпе")
    assert plan == fallback_plan("нарисуй кота в шляпе") and plan.prompt == "кота в шляпе"

    no_ai, _ = _service(memory)
    assert (await no_ai.plan(111, "нарисуй слона")).prompt == "слона"


async def test_not_a_drawing_request_returns_none(memory):
    service, _ = _service(memory, PromptWriter('{"draw": false}'))
    assert await service.plan(111, "нарисуй мне план на неделю") is None


async def test_failed_drawing_keeps_history_clean(memory):
    service, _ = _service(memory, None, FakeBackend(errors=["rejected"]))
    with pytest.raises(ImageGenerationError):
        await service.draw(111, fallback_plan("нарисуй кота"))
    assert await memory.get_history(111) == []


async def test_old_drawings_are_forgotten(memory):
    service, _ = _service(memory)
    service._remember = 2
    first = await service.draw(1, fallback_plan("нарисуй кота"))
    await service.draw(1, fallback_plan("нарисуй собаку"))
    await service.draw(1, fallback_plan("нарисуй слона"))
    assert await service.again(1, first.id) is None


def test_system_prompt_tells_whether_elya_can_draw():
    assert "Картинки ты рисовать умеешь" in build_system_prompt("Эля", can_draw=True)
    assert "Рисовать картинки ты сейчас не можешь" in build_system_prompt("Эля", can_draw=False)
    neutral = build_system_prompt("Эля")
    assert "Картинки ты" not in neutral and "Рисовать картинки" not in neutral


# ---------- настройки ----------

IMAGE_ENV = [
    "IMAGE_PROVIDERS", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_IMAGE_MODEL",
    "POLLINATIONS_API_KEY", "POLLINATIONS_IMAGE_MODEL", "IMAGE_CUSTOM_BASE_URL", "IMAGE_CUSTOM_API_KEY",
    "IMAGE_CUSTOM_MODEL", "IMAGE_DAILY_LIMIT",
]


@pytest.fixture
def image_env(monkeypatch):
    for key in IMAGE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("AI_PROVIDERS", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    return monkeypatch


def test_images_are_off_without_keys(image_env):
    settings = load_settings(env_file=None)
    assert settings.image_providers == () and not settings.images_enabled
    assert settings.image_daily_limit == 50


def test_image_providers_are_loaded_in_order(image_env):
    image_env.setenv("IMAGE_PROVIDERS", "pollinations, cloudflare, custom")
    image_env.setenv("CLOUDFLARE_ACCOUNT_ID", "abc123")
    image_env.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    image_env.setenv("POLLINATIONS_API_KEY", "sk_poll")
    image_env.setenv("IMAGE_CUSTOM_BASE_URL", "https://api.openai.com/v1/")
    image_env.setenv("IMAGE_CUSTOM_API_KEY", "sk-openai")
    image_env.setenv("IMAGE_CUSTOM_MODEL", "gpt-image-1")
    image_env.setenv("IMAGE_DAILY_LIMIT", "0")
    settings = load_settings(env_file=None)
    assert [p.name for p in settings.image_providers] == ["pollinations", "cloudflare", "custom"]
    poll, cf, custom = settings.image_providers
    assert poll.kind == "openai" and poll.base_url == "https://gen.pollinations.ai/v1" and poll.api_key == "sk_poll"
    assert poll.model == "black-forest-labs/flux.1-schnell"
    assert cf.kind == "cloudflare" and cf.model == "@cf/black-forest-labs/flux-1-schnell"
    assert cf.base_url == "https://api.cloudflare.com/client/v4/accounts/abc123/ai/run"
    assert custom.base_url == "https://api.openai.com/v1" and custom.model == "gpt-image-1"
    assert settings.images_enabled and settings.image_daily_limit == 0


def test_cloudflare_needs_both_values(image_env):
    image_env.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    assert load_settings(env_file=None).image_providers == ()


@pytest.mark.parametrize(
    "env",
    [
        {"IMAGE_PROVIDERS": "midjourney"},
        {"CLOUDFLARE_ACCOUNT_ID": "abc/../x", "CLOUDFLARE_API_TOKEN": "t"},
        {"IMAGE_PROVIDERS": "custom", "IMAGE_CUSTOM_BASE_URL": "https://x/v1"},
        {"IMAGE_DAILY_LIMIT": "-1"},
    ],
)
def test_bad_image_settings(image_env, env):
    for key, value in env.items():
        image_env.setenv(key, value)
    with pytest.raises(ConfigError):
        load_settings(env_file=None)

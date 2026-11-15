"""Генерация и правка картинок: сервисы-рисовальщики и перебор по приоритету.

Поддерживаются два вида API:
- Cloudflare Workers AI — POST /accounts/{id}/ai/run/{model}; ответ — JSON с картинкой
  в base64 ({"result": {"image": "..."}}) или сама картинка, в зависимости от модели.
  Правка — FLUX.2 [klein]: multipart с prompt и исходником в input_image_0;
- OpenAI-совместимый POST {base_url}/images/generations (Pollinations, OpenAI и другие);
  ответ — {"data": [{"b64_json": "..."}]} или {"data": [{"url": "..."}]}.
  Правка — POST {base_url}/images/edits (multipart: model, prompt, image).

Как и с текстовыми моделями, если сервис упёрся в лимит или недоступен, пробуется следующий,
а упёршийся временно пропускается.
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from config.settings import ImageProviderConfig
from services.usage import UsageTracker
from services.vision import sniff_image

logger = logging.getLogger(__name__)

# Telegram принимает фото до 10 МБ; картинки FLUX весят обычно 100–500 КБ.
MAX_RESULT_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT = 120.0
ORIENTATIONS = ("square", "portrait", "landscape")
# Размеры подходят и для FLUX, и для OpenAI-совместимых моделей (gpt-image принимает именно эти).
SIZES = {
    "square": (1024, 1024),
    "portrait": (1024, 1536),
    "landscape": (1536, 1024),
}
# Признаки исчерпанной бесплатной квоты в текстах ошибок (Cloudflare пишет про «neurons»).
_QUOTA_MARKERS = ("daily free allocation", "neurons", "quota", "insufficient balance", "pollen")


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    mime: str
    provider: str  # название сервиса для людей
    model: str

    @property
    def extension(self) -> str:
        return {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(self.mime, "jpg")

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


class ImageProviderError(Exception):
    """Ошибка одного сервиса.

    kind: rate_limit — лимит или квота; config — ключ, аккаунт или модель;
    rejected — сервис отказался рисовать по такому описанию (фильтр, слишком длинно);
    unavailable — сеть, таймаут, сбой сервиса или непонятный ответ.
    """

    def __init__(self, message: str, *, provider: str, kind: str, retry_after: float | None = None) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.kind = kind
        self.retry_after = retry_after


class ImageLimitError(Exception):
    """Исчерпан дневной лимит картинок (IMAGE_DAILY_LIMIT)."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"daily image limit reached ({limit})")
        self.limit = limit


class ImageGenerationError(Exception):
    """Ни один сервис не нарисовал картинку."""

    def __init__(self, errors: list[ImageProviderError]) -> None:
        super().__init__("; ".join(str(e) for e in errors) or "all image services are cooling down")
        self.errors = errors

    @property
    def kind(self) -> str:
        """rejected | rate_limit | config | unavailable — для понятного сообщения."""
        kinds = {e.kind for e in self.errors}
        if "rejected" in kinds:
            return "rejected"
        if not self.errors or "rate_limit" in kinds:
            return "rate_limit"
        if kinds == {"config"}:
            return "config"
        return "unavailable"


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


def _error_detail(response: httpx.Response) -> str:
    """Короткий текст ошибки из ответа сервиса (для логов и классификации)."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or errors[0])[:300]  # Cloudflare
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]  # OpenAI, Pollinations
        if error:
            return str(error)[:300]
    return str(data)[:300]


# Исходник для правки: Cloudflare FLUX.2 принимает картинки меньше 512×512,
# OpenAI-совместимым сервисам хватает 1024 по длинной стороне.
CLOUDFLARE_SOURCE_SIDE = 511
SOURCE_SIDE = 1024
MAX_SOURCE_BYTES = 20 * 1024 * 1024


class SourceImageError(ValueError):
    """Присланный файл не открывается как картинка."""


@dataclass(frozen=True, slots=True)
class SourceImage:
    """Картинка для правки, уже повёрнутая по EXIF и сжатая в JPEG."""

    data: bytes  # не больше SOURCE_SIDE по длинной стороне
    width: int
    height: int

    def scaled(self, side: int) -> bytes:
        """JPEG не больше side по длинной стороне."""
        if max(self.width, self.height) <= side:
            return self.data
        return _encode(_open(self.data), side)

    def output_size(self, longest: int = 1024) -> tuple[int, int]:
        """Размер результата с теми же пропорциями: длинная сторона longest, кратно 16."""
        scale = longest / max(self.width, self.height)
        width = max(256, min(1920, round(self.width * scale / 16) * 16))
        height = max(256, min(1920, round(self.height * scale / 16) * 16))
        return width, height


def _open(data: bytes):
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image.load()
    except Exception as exc:  # noqa: BLE001 — Pillow бросает разные ошибки на битых файлах
        raise SourceImageError("not an image") from exc
    if image.mode in ("RGBA", "LA", "P"):
        # Прозрачность — на белый фон: JPEG её не хранит, а сервисам так понятнее.
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        return background
    return image.convert("RGB")


def _encode(image, side: int) -> bytes:
    from PIL import Image

    image = image.copy()
    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=92)
    return out.getvalue()


def prepare_source(data: bytes) -> SourceImage:
    """Готовит присланную картинку к правке. SourceImageError — это не картинка."""
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise SourceImageError("empty or too large")
    image = _open(data)
    encoded = _encode(image, SOURCE_SIDE)
    width, height = image.size
    scale = min(1.0, SOURCE_SIDE / max(width, height))
    return SourceImage(data=encoded, width=max(1, round(width * scale)), height=max(1, round(height * scale)))


class ImageBackend(ABC):
    def __init__(self, config: ImageProviderConfig, http_client: httpx.AsyncClient) -> None:
        self.name = config.name
        self.title = config.title
        self.model = config.model
        self.edit_model = config.edit_model
        self._config = config
        self._http = http_client

    @abstractmethod
    async def generate(self, prompt: str, *, width: int, height: int, seed: int) -> GeneratedImage:
        """Рисует картинку; ошибки — ImageProviderError."""

    async def edit(self, prompt: str, source: SourceImage, *, seed: int) -> GeneratedImage:
        """Правит картинку по описанию; ошибки — ImageProviderError."""
        raise self._fail("editing is not supported", "config")

    def _fail(self, message: str, kind: str, retry_after: float | None = None) -> ImageProviderError:
        return ImageProviderError(message, provider=self.name, kind=kind, retry_after=retry_after)

    async def _post(self, url: str, payload: dict, *, files: dict | None = None) -> httpx.Response:
        """JSON или, если есть files, multipart (поля payload — текстом)."""
        headers = {"Authorization": f"Bearer {self._config.api_key}"} if self._config.api_key else {}
        if files is not None:
            body = {"data": {key: str(value) for key, value in payload.items()}, "files": files}
        else:
            body = {"json": payload}
        try:
            response = await self._http.post(url, headers=headers, timeout=REQUEST_TIMEOUT, **body)
        except httpx.TimeoutException as exc:
            raise self._fail("timeout", "unavailable") from exc
        except httpx.HTTPError as exc:
            raise self._fail(f"network error: {type(exc).__name__}", "unavailable") from exc
        if response.status_code != 200:
            raise self._classify(response)
        return response

    def _classify(self, response: httpx.Response) -> ImageProviderError:
        status = response.status_code
        detail = _error_detail(response) or f"HTTP {status}"
        message = f"HTTP {status}: {detail}"
        lower = detail.lower()
        if status in (402, 429):
            return self._fail(message, "rate_limit", _retry_after(response))
        if status in (401, 403, 404):
            return self._fail(message, "config")
        if any(marker in lower for marker in _QUOTA_MARKERS):
            return self._fail(message, "rate_limit", _retry_after(response))
        if status in (400, 413, 422):
            return self._fail(message, "rejected")
        return self._fail(message, "unavailable")

    def _image(self, data: bytes, model: str | None = None) -> GeneratedImage:
        if len(data) > MAX_RESULT_BYTES:
            raise self._fail(f"image is too large ({len(data)} bytes)", "unavailable")
        mime = sniff_image(data)
        if mime is None:
            raise self._fail("response is not an image", "unavailable")
        return GeneratedImage(data=data, mime=mime, provider=self.title, model=model or self.model)

    def _decode(self, encoded: object, model: str | None = None) -> GeneratedImage:
        if not isinstance(encoded, str) or not encoded:
            raise self._fail("no image in the response", "unavailable")
        if encoded.startswith("data:"):
            encoded = encoded.partition(",")[2]
        try:
            return self._image(base64.b64decode(encoded, validate=False), model)
        except (binascii.Error, ValueError) as exc:
            raise self._fail("bad base64 image", "unavailable") from exc


class CloudflareBackend(ImageBackend):
    """Workers AI. FLUX.1 [schnell] принимает только prompt (и steps) и рисует квадрат 1024×1024;
    другим моделям (например, @cf/leonardo/*) передаются ещё размеры и seed."""

    async def generate(self, prompt: str, *, width: int, height: int, seed: int) -> GeneratedImage:
        payload: dict = {"prompt": prompt[:2048]}
        if "flux-1-schnell" not in self.model:
            payload.update(width=width, height=height, seed=seed)
        response = await self._post(f"{self._config.base_url}/{self.model}", payload)
        return self._result(response, self.model)

    async def edit(self, prompt: str, source: SourceImage, *, seed: int) -> GeneratedImage:
        """FLUX.2 [klein]/[dev]: multipart, исходник — input_image_0 (меньше 512×512)."""
        if not self.edit_model:
            raise self._fail("editing is not configured", "config")
        width, height = source.output_size()
        fields = {"prompt": prompt[:2048], "width": width, "height": height, "seed": seed}
        files = {"input_image_0": ("source.jpg", source.scaled(CLOUDFLARE_SOURCE_SIDE), "image/jpeg")}
        response = await self._post(f"{self._config.base_url}/{self.edit_model}", fields, files=files)
        return self._result(response, self.edit_model)

    def _result(self, response: httpx.Response, model: str) -> GeneratedImage:
        if response.headers.get("content-type", "").startswith("image/"):
            return self._image(response.content, model)
        try:
            data = response.json()
        except ValueError as exc:
            raise self._fail("bad JSON", "unavailable") from exc
        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(data, dict) and data.get("success") is False:
            raise self._fail(_error_detail(response), "rejected")
        image = result.get("image") if isinstance(result, dict) else None
        return self._decode(image, model)


class OpenAIImagesBackend(ImageBackend):
    """POST {base_url}/images/generations. response_format не передаётся: модели gpt-image
    его не принимают; картинка придёт в b64_json или ссылкой — подходят оба варианта."""

    def __init__(self, config: ImageProviderConfig, http_client: httpx.AsyncClient, *, send_seed: bool) -> None:
        super().__init__(config, http_client)
        self._send_seed = send_seed  # официальный API OpenAI не знает параметра seed

    async def generate(self, prompt: str, *, width: int, height: int, seed: int) -> GeneratedImage:
        payload: dict = {"model": self.model, "prompt": prompt, "n": 1, "size": f"{width}x{height}"}
        if self._send_seed:
            payload["seed"] = seed
        response = await self._post(f"{self._config.base_url}/images/generations", payload)
        return await self._result(response, self.model)

    async def edit(self, prompt: str, source: SourceImage, *, seed: int) -> GeneratedImage:
        """POST {base_url}/images/edits — multipart, как у OpenAI: model, prompt, image."""
        if not self.edit_model:
            raise self._fail("editing is not configured", "config")
        fields: dict = {"model": self.edit_model, "prompt": prompt, "n": 1}
        if self._send_seed:
            fields["seed"] = seed
        files = {"image": ("source.jpg", source.data, "image/jpeg")}
        response = await self._post(f"{self._config.base_url}/images/edits", fields, files=files)
        return await self._result(response, self.edit_model)

    async def _result(self, response: httpx.Response, model: str) -> GeneratedImage:
        try:
            data = response.json()
            item = data["data"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise self._fail("no image in the response", "unavailable") from exc
        if not isinstance(item, dict):
            raise self._fail("no image in the response", "unavailable")
        if item.get("b64_json"):
            return self._decode(item["b64_json"], model)
        if isinstance(item.get("url"), str) and item["url"].startswith(("https://", "http://")):
            return await self._download(item["url"], model)
        raise self._fail("no image in the response", "unavailable")

    async def _download(self, url: str, model: str | None = None) -> GeneratedImage:
        try:
            response = await self._http.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise self._fail(f"download failed: {type(exc).__name__}", "unavailable") from exc
        if response.status_code != 200:
            raise self._fail(f"download failed: HTTP {response.status_code}", "unavailable")
        return self._image(response.content, model)


def build_image_backends(
    configs: tuple[ImageProviderConfig, ...] | list[ImageProviderConfig], http_client: httpx.AsyncClient
) -> list[ImageBackend]:
    backends: list[ImageBackend] = []
    for config in configs:
        if config.kind == "cloudflare":
            backends.append(CloudflareBackend(config, http_client))
        else:
            backends.append(OpenAIImagesBackend(config, http_client, send_seed=config.name == "pollinations"))
    return backends


class ImageGenerator:
    """Перебирает сервисы по приоритету и соблюдает дневной лимит картинок."""

    def __init__(
        self,
        backends: list[ImageBackend],
        *,
        usage: UsageTracker | None = None,
        base_cooldown: float = 60.0,
        max_cooldown: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backends = backends
        self._usage = usage
        self._base_cooldown = base_cooldown
        self._max_cooldown = max_cooldown
        self._clock = clock
        # имя сервиса -> (конец паузы, ошибка, из-за которой пауза)
        self._cooldowns: dict[str, tuple[float, ImageProviderError]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._backends)

    @property
    def titles(self) -> list[str]:
        return [b.title for b in self._backends]

    @property
    def can_edit(self) -> bool:
        return any(getattr(b, "edit_model", "") for b in self._backends)

    @property
    def usage(self) -> UsageTracker | None:
        return self._usage

    def _paused(self, name: str) -> ImageProviderError | None:
        entry = self._cooldowns.get(name)
        return entry[1] if entry is not None and entry[0] > self._clock() else None

    def _cool_down(self, error: ImageProviderError, key: str | None = None) -> None:
        if error.kind == "config":
            delay = self._base_cooldown * 10
        elif error.kind == "rate_limit":
            delay = error.retry_after if error.retry_after is not None else self._base_cooldown * 5
        else:
            return  # сбой или отказ по конкретному описанию — в следующий раз пробуем снова
        self._cooldowns[key or error.provider] = (self._clock() + min(delay, self._max_cooldown), error)

    async def generate(
        self, prompt: str, *, orientation: str = "square", seed: int | None = None
    ) -> GeneratedImage:
        if not self._backends:
            raise ImageGenerationError([])
        if self._usage and not await self._usage.try_consume():
            raise ImageLimitError(self._usage.daily_limit)
        width, height = SIZES.get(orientation, SIZES["square"])
        # Новый seed — новая картинка: сервисы кешируют одинаковые запросы.
        seed = seed if seed is not None else random.randint(1, 2_000_000_000)
        errors: list[ImageProviderError] = []
        for backend in self._backends:
            paused = self._paused(backend.name)
            if paused is not None:
                errors.append(paused)
                continue
            started = self._clock()
            try:
                image = await backend.generate(prompt, width=width, height=height, seed=seed)
            except ImageProviderError as exc:
                logger.warning("Image generation failed: %s", exc)
                errors.append(exc)
                self._cool_down(exc)
                continue
            self._cooldowns.pop(backend.name, None)
            logger.info(
                "Image generated: provider=%s model=%s bytes=%d time=%.1fs",
                backend.name, backend.model, len(image.data), self._clock() - started,
            )
            return image
        raise ImageGenerationError(errors)

    async def edit(self, prompt: str, source: SourceImage, *, seed: int | None = None) -> GeneratedImage:
        """Правит картинку: сервисы с моделью правки по приоритету. Дневной лимит — общий
        с рисованием. Паузы у правки свои: лимит рисования не мешает править и наоборот."""
        backends = [b for b in self._backends if getattr(b, "edit_model", "")]
        if not backends:
            raise ImageGenerationError([])
        if self._usage and not await self._usage.try_consume():
            raise ImageLimitError(self._usage.daily_limit)
        seed = seed if seed is not None else random.randint(1, 2_000_000_000)
        errors: list[ImageProviderError] = []
        for backend in backends:
            key = f"{backend.name}:edit"
            paused = self._paused(key)
            if paused is not None:
                errors.append(paused)
                continue
            started = self._clock()
            try:
                image = await backend.edit(prompt, source, seed=seed)
            except ImageProviderError as exc:
                logger.warning("Image edit failed: %s", exc)
                errors.append(exc)
                self._cool_down(exc, key)
                continue
            self._cooldowns.pop(key, None)
            logger.info(
                "Image edited: provider=%s model=%s bytes=%d time=%.1fs",
                backend.name, backend.edit_model, len(image.data), self._clock() - started,
            )
            return image
        raise ImageGenerationError(errors)

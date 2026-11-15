"""Загрузка и валидация конфигурации из переменных окружения (.env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv


class ConfigError(Exception):
    """Ошибка конфигурации: бот не может стартовать с такими настройками."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Настройки одного OpenAI-совместимого AI-провайдера."""

    name: str
    title: str
    base_url: str
    api_key: str
    models: tuple[str, ...]
    vision_models: tuple[str, ...] = ()
    # Потолок max_tokens для провайдера (0 — без ограничения): у некоторых бесплатных
    # тарифов ответ ограничен, и запрос с большим max_tokens отклоняется целиком.
    max_output_tokens: int = 0
    # Протокол: "openai" — обычный OpenAI-совместимый API, "gigachat" — GigaChat (OAuth).
    kind: str = "openai"
    # Свой корневой сертификат (PEM) для HTTPS, например НУЦ Минцифры для GigaChat.
    ca_bundle: str = ""
    # Только для GigaChat: адрес выдачи токена и область доступа.
    auth_url: str = ""
    scope: str = ""


@dataclass(frozen=True, slots=True)
class ImageProviderConfig:
    """Сервис, который рисует картинки по текстовому описанию.

    kind: "cloudflare" — Workers AI (POST {base_url}/{model});
          "openai" — OpenAI-совместимый POST {base_url}/images/generations.
    edit_model — модель, которая правит присланную картинку по описанию (Cloudflare:
    FLUX.2 [klein]; OpenAI-совместимые: POST {base_url}/images/edits); пусто — не умеет.
    """

    name: str
    title: str
    kind: str
    base_url: str
    api_key: str
    model: str
    edit_model: str = ""


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Поиск в интернете: Serper.dev, SerpApi, Tavily, Brave (по ключу) и DuckDuckGo (без ключа)."""

    # Пары (провайдер, API-ключ) в порядке приоритета; пусто — поиск выключен.
    providers: tuple[tuple[str, str], ...] = ()
    # True — Эля сама решает, когда искать; False — только по явной просьбе.
    auto: bool = True
    language: str = "ru"
    region: str = ""
    max_results: int = 6
    # Поисков в сутки (UTC), 0 — без ограничения.
    daily_limit: int = 100

    @property
    def enabled(self) -> bool:
        return bool(self.providers)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    providers: tuple[ProviderConfig, ...]
    database_url: str
    assistant_name: str
    history_max_messages: int
    history_max_chars: int
    max_output_tokens: int
    temperature: float
    request_timeout: float
    rate_limit_per_minute: int
    daily_request_limit: int
    log_level: str
    # Конспекты и презентации: длина ответа модели и максимальный размер материала.
    study_max_output_tokens: int = 8192
    study_max_input_chars: int = 60000
    # Режим кода (/code): ответы длиннее и точнее, в контекст помещаются файлы с кодом.
    code_max_output_tokens: int = 4096
    code_temperature: float = 0.2
    code_history_max_chars: int = 40000
    code_max_file_chars: int = 30000
    # GitHub: токен для репозиториев (необязательно — можно подключить командой /github).
    github_repo_token: str = ""
    # Песочница для запуска кода (services/sandbox.py): auto | docker | bwrap | process | off.
    sandbox_backend: str = "auto"
    sandbox_timeout: float = 15.0
    sandbox_memory_mb: int = 512
    sandbox_max_runs: int = 3
    sandbox_image: str = "elya-sandbox"
    sandbox_python: str = ""
    sandbox_dir: str = ""
    # Документы (PDF, Word, таблицы): сколько символов фрагментов добавлять к вопросу.
    documents_context_chars: int = 10000
    # Mini App (HTML5-интерфейс внутри Telegram). Пустой webapp_url — Mini App выключен.
    webapp_url: str = ""
    webapp_host: str = "0.0.0.0"
    webapp_port: int = 8080
    webapp_auth_max_age: int = 86400
    # Голосовые: ключ Groq (тот же, что для провайдера groq) и модель распознавания
    speech_api_key: str = ""
    speech_model: str = "whisper-large-v3-turbo"
    # Монтаж видео (нужен ffmpeg). Пустой video_workdir — временная папка системы.
    video_workdir: str = ""
    video_max_clips: int = 10
    video_max_seconds: int = 600
    video_max_side: int = 1920
    video_timeout: float = 900.0
    video_upload_max_mb: int = 100  # из приложения можно загрузить больше, чем 20 МБ из чата
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    # Картинки: сервисы в порядке приоритета (пусто — рисование выключено) и дневной лимит.
    image_providers: tuple[ImageProviderConfig, ...] = ()
    image_daily_limit: int = 50
    search: SearchConfig = SearchConfig()
    # Язык голосовых (ISO 639-1); пустая строка — Whisper определяет язык сам
    speech_language: str = "ru"

    @property
    def webapp_enabled(self) -> bool:
        return bool(self.webapp_url)

    @property
    def images_enabled(self) -> bool:
        return bool(self.image_providers)

    @property
    def image_editing_enabled(self) -> bool:
        return any(provider.edit_model for provider in self.image_providers)


# Известные провайдеры с OpenAI-совместимым API (POST {base_url}/chat/completions).
# models — значения по умолчанию; их можно переопределить через <NAME>_MODELS.
# vision — модели для запросов с фото (<NAME>_VISION_MODELS): «*» — все из models.
# keyless — работает без ключа (ключ необязателен и только поднимает лимиты).
# max_tokens — потолок длины ответа на бесплатном тарифе (<NAME>_MAX_TOKENS).
# {account_id} в base_url подставляется из <NAME>_ACCOUNT_ID.
# Списки моделей бесплатных тарифов меняются: исчезнувшая модель отвечает 404,
# AIClient откладывает её на несколько часов и переходит к следующей.
KNOWN_PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "title": "Google Gemini API",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": "gemini-flash-latest,gemini-flash-lite-latest",
        "vision": "*",  # все модели Gemini понимают картинки
    },
    "github": {
        "title": "GitHub Models",
        "base_url": "https://models.github.ai/inference",
        "models": "openai/gpt-4.1,openai/gpt-4.1-mini,openai/gpt-4o-mini",
        "vision": "*",
        "max_tokens": "4000",  # бесплатный тариф: до 4000 токенов ответа
    },
    "mistral": {
        "title": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "models": "mistral-large-latest,mistral-medium-latest,mistral-small-latest",
        "vision": "*",
    },
    "groq": {
        "title": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models": "openai/gpt-oss-120b,qwen/qwen3.8-27b,llama-3.3-70b-versatile,openai/gpt-oss-20b",
        "vision": "meta-llama/llama-4-scout-17b-16e-instruct",
    },
    "cerebras": {
        "title": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "models": "gpt-oss-120b",
    },
    "openrouter": {
        "title": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        # openrouter/free — роутер, сам выбирающий доступную бесплатную модель.
        "models": "google/gemma-4-31b-it:free,openrouter/free",
        "vision": "google/gemma-4-31b-it:free",
    },
    "nvidia": {
        "title": "NVIDIA NIM",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "models": "openai/gpt-oss-120b,nvidia/nemotron-3-ultra-550b-a55b,meta/llama-3.3-70b-instruct",
        "vision": "meta/llama-4-maverick-17b-128e-instruct",
    },
    "cloudflare": {
        "title": "Cloudflare Workers AI",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "models": (
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast,@cf/zai-org/glm-4.7-flash,"
            "@cf/openai/gpt-oss-120b"
        ),
        "vision": "@cf/meta/llama-4-scout-17b-16e-instruct",
    },
    "zai": {
        "title": "Z.ai (GLM)",
        "base_url": "https://api.z.ai/api/paas/v4",
        "models": "glm-4.7-flash,glm-4.5-flash",
        "vision": "glm-4.6v-flash",
    },
    "gigachat": {
        "title": "GigaChat",
        "kind": "gigachat",
        "base_url": "https://gigachat.devices.sberbank.ru/api/v1",
        "auth_url": "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        "scope": "GIGACHAT_API_PERS",
        "models": "GigaChat-2",
    },
    "cohere": {
        "title": "Cohere",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "models": "command-a-03-2025,command-r-plus-08-2024",
        "vision": "command-a-vision-07-2025",
    },
    "huggingface": {
        "title": "Hugging Face",
        "base_url": "https://router.huggingface.co/v1",
        "models": "openai/gpt-oss-120b,Qwen/Qwen3-235B-A22B-Instruct-2507",
    },
    # ---- Работают без ключа: запасной вариант на крайний случай ----
    "ovh": {
        "title": "OVHcloud AI Endpoints",
        "base_url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "models": "Qwen3.5-397B-A17B,gpt-oss-120b,Meta-Llama-3_3-70B-Instruct",
        "vision": "Qwen2.5-VL-72B-Instruct",
        "keyless": "1",
    },
    "kilo": {
        "title": "Kilo Gateway",
        "base_url": "https://api.kilo.ai/api/gateway",
        "models": "kilo-auto/free",
        "keyless": "1",
    },
    "llm7": {
        "title": "LLM7.io",
        "base_url": "https://api.llm7.io/v1",
        "models": "gpt-oss:20b,mistral-Nemo-Instruct-2407",
        "keyless": "1",
    },
    "pollinations": {
        "title": "Pollinations",
        "base_url": "https://gen.pollinations.ai/v1",
        "models": "openai,mistral",
        "vision": "openai",
        "keyless": "1",
    },
    "custom": {
        "title": "Custom OpenAI-compatible API",
        "base_url": "",
        "models": "",
        "keyless": "1",
    },
}

# Порядок по умолчанию: сначала сильные модели с щедрыми лимитами, в конце — сервисы
# без ключа. Провайдеры без ключа (кроме keyless) пропускаются, так что для подключения
# нового достаточно вписать его ключ в .env.
DEFAULT_PROVIDER_ORDER = (
    "gemini,github,mistral,groq,cerebras,openrouter,nvidia,cloudflare,zai,gigachat,"
    "cohere,huggingface,ovh,kilo,llm7,pollinations"
)


# Сервисы картинок. Ключ — <NAME>_..., модель — <NAME>_IMAGE_MODEL (или IMAGE_CUSTOM_MODEL).
KNOWN_IMAGE_PROVIDERS: dict[str, dict[str, str]] = {
    "cloudflare": {
        "title": "Cloudflare Workers AI",
        "kind": "cloudflare",
        # FLUX.1 [schnell]: бесплатно в пределах 10 000 «нейронов» в сутки (~170 картинок).
        "model": "@cf/black-forest-labs/flux-1-schnell",
        # FLUX.2 [klein] 4B правит картинку по описанию («убери фон», «сделай зимним»).
        "edit_model": "@cf/black-forest-labs/flux-2-klein-4b",
    },
    "pollinations": {
        "title": "Pollinations",
        "kind": "openai",
        "base_url": "https://gen.pollinations.ai/v1",
        "model": "black-forest-labs/flux.1-schnell",
        "edit_model": "kontext",
    },
    "custom": {
        "title": "Custom OpenAI-compatible Images API",
        "kind": "openai",
        "base_url": "",
        "model": "",
        "edit_model": "",
    },
}
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4/accounts"


def _load_sandbox_backend() -> str:
    value = (_get_str("SANDBOX_BACKEND", "auto") or "auto").lower()
    allowed = ("auto", "docker", "bwrap", "process", "off")
    if value not in allowed:
        raise ConfigError(f"SANDBOX_BACKEND должен быть одним из: {', '.join(allowed)} (сейчас {value!r})")
    return value


def _get_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = _get_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} должно быть >= {minimum}, получено: {value}")
    return value


def _get_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = _get_str(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом, получено: {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} должно быть в диапазоне [{minimum}, {maximum}], получено: {value}")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = _get_str(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "да"):
        return True
    if raw in ("0", "false", "no", "off", "нет"):
        return False
    raise ConfigError(f"{name} должно быть true или false, получено: {raw!r}")


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for item in _split_csv(raw):
        try:
            ids.add(int(item))
        except ValueError as exc:
            raise ConfigError(f"ALLOWED_USER_IDS: некорректный Telegram ID {item!r}") from exc
    return frozenset(ids)


def _load_webapp_url() -> str:
    """Публичный адрес Mini App. Telegram открывает Mini App только по HTTPS."""
    url = _get_str("WEBAPP_URL")
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise ConfigError(
            "WEBAPP_URL должен быть публичным HTTPS-адресом без параметров "
            f"(например, https://elya.example.com), получено: {url!r}"
        )
    # Интерфейс подключает файлы по относительным путям, поэтому адрес — всегда «папка».
    return url.rstrip("/") + "/"


def _load_webapp_port() -> int:
    # Хостинги (Render, Railway, Koyeb) сообщают нужный порт через PORT.
    port = _get_int("WEBAPP_PORT", _get_int("PORT", 8080, minimum=1), minimum=1)
    if port > 65535:
        raise ConfigError(f"WEBAPP_PORT должен быть в диапазоне 1–65535, получено: {port}")
    return port


def _load_video_max_side() -> int:
    side = _get_int("VIDEO_MAX_SIDE", 1920, minimum=240)
    if side > 3840:
        raise ConfigError(f"VIDEO_MAX_SIDE должно быть не больше 3840, получено: {side}")
    return side


def _load_speech_language() -> str:
    # Явный язык нужен Whisper: угадывая сам, он путает короткие русские голосовые с английскими.
    language = (_get_str("SPEECH_LANGUAGE", "ru") or "ru").lower()
    if language == "auto":
        return ""
    if not re.fullmatch(r"[a-z]{2,3}", language):
        raise ConfigError(
            f"SPEECH_LANGUAGE должен быть кодом языка (ru, en, uk…) или auto, получено: {language!r}"
        )
    return language


def _load_provider(name: str) -> ProviderConfig | None:
    """Возвращает конфиг провайдера или None, если он не настроен (нет ключа или адреса)."""
    known = KNOWN_PROVIDERS.get(name)
    if known is None:
        raise ConfigError(
            f"AI_PROVIDERS: неизвестный провайдер {name!r}. "
            f"Доступны: {', '.join(KNOWN_PROVIDERS)}"
        )
    prefix = name.upper()
    api_key = _get_str(f"{prefix}_API_KEY")
    if not api_key and name == "cloudflare":
        # Тот же токен Workers AI, что и для рисования: второй раз вписывать не нужно.
        api_key = _get_str("CLOUDFLARE_API_TOKEN")
    base_url = _get_str(f"{prefix}_BASE_URL", known["base_url"]).rstrip("/")
    models = tuple(_split_csv(_get_str(f"{prefix}_MODELS", known["models"])))

    if not base_url:
        return None  # custom без адреса
    # Сервисы без ключа (и локальные серверы вроде Ollama) работают и так.
    if not api_key and not known.get("keyless"):
        return None

    if "{account_id}" in base_url:
        account_id = _get_str(f"{prefix}_ACCOUNT_ID")
        if not account_id:
            raise ConfigError(
                f"Для провайдера {name!r} нужен ещё ID аккаунта: задайте {prefix}_ACCOUNT_ID."
            )
        base_url = base_url.replace("{account_id}", account_id)

    if not models:
        raise ConfigError(
            f"Для провайдера {name!r} не указаны модели: задайте {prefix}_MODELS "
            "(через запятую, в порядке приоритета)."
        )
    # <NAME>_VISION_MODELS: модели для запросов с фото; «*» — те же, что в <NAME>_MODELS,
    # «-» — провайдер фото не получает.
    vision_raw = _get_str(f"{prefix}_VISION_MODELS", known.get("vision", "")) or ""
    if vision_raw == "*":
        vision_models = models
    elif vision_raw == "-":
        vision_models = ()
    else:
        vision_models = tuple(_split_csv(vision_raw))

    ca_bundle = _get_str(f"{prefix}_CA_BUNDLE")
    if ca_bundle and not os.path.isfile(ca_bundle):
        raise ConfigError(f"{prefix}_CA_BUNDLE: файл сертификата не найден: {ca_bundle!r}")

    kind = known.get("kind", "openai")
    return ProviderConfig(
        name=name,
        title=known["title"],
        base_url=base_url,
        api_key=api_key,
        models=models,
        vision_models=vision_models,
        max_output_tokens=_get_int(
            f"{prefix}_MAX_TOKENS", int(known.get("max_tokens", "0")), minimum=0
        ),
        kind=kind,
        ca_bundle=ca_bundle,
        auth_url=_get_str(f"{prefix}_AUTH_URL", known.get("auth_url", "")) if kind == "gigachat" else "",
        scope=_get_str(f"{prefix}_SCOPE", known.get("scope", "")) if kind == "gigachat" else "",
    )


def _load_image_provider(name: str) -> ImageProviderConfig | None:
    """Конфиг сервиса картинок или None, если для него не заданы ключи."""
    known = KNOWN_IMAGE_PROVIDERS.get(name)
    if known is None:
        raise ConfigError(
            f"IMAGE_PROVIDERS: неизвестный сервис {name!r}. "
            f"Доступны: {', '.join(KNOWN_IMAGE_PROVIDERS)}"
        )
    if name == "cloudflare":
        account_id = _get_str("CLOUDFLARE_ACCOUNT_ID")
        api_key = _get_str("CLOUDFLARE_API_TOKEN")
        if not account_id or not api_key:
            return None
        if not account_id.isalnum():
            raise ConfigError(
                "CLOUDFLARE_ACCOUNT_ID — это идентификатор аккаунта из панели Cloudflare "
                f"(32 символа, буквы и цифры), получено: {account_id!r}"
            )
        base_url = f"{CLOUDFLARE_API_BASE}/{account_id}/ai/run"
        model = _get_str("CLOUDFLARE_IMAGE_MODEL", known["model"]) or known["model"]
        edit_model = _edit_model("CLOUDFLARE_EDIT_MODEL", known["edit_model"])
    elif name == "custom":
        base_url = _get_str("IMAGE_CUSTOM_BASE_URL").rstrip("/")
        api_key = _get_str("IMAGE_CUSTOM_API_KEY")
        model = _get_str("IMAGE_CUSTOM_MODEL")
        if not base_url:
            return None
        if not model:
            raise ConfigError(
                "Для IMAGE_CUSTOM_BASE_URL нужна модель: задайте IMAGE_CUSTOM_MODEL "
                "(например, gpt-image-1 или black-forest-labs/flux.1-schnell)."
            )
        # По умолчанию правит та же модель (gpt-image-1 это умеет); «off» — не править.
        edit_model = _edit_model("IMAGE_CUSTOM_EDIT_MODEL", model)
    else:
        prefix = name.upper()
        api_key = _get_str(f"{prefix}_API_KEY")
        if not api_key:
            return None
        base_url = known["base_url"]
        model = _get_str(f"{prefix}_IMAGE_MODEL", known["model"]) or known["model"]
        edit_model = _edit_model(f"{prefix}_EDIT_MODEL", known["edit_model"])
    return ImageProviderConfig(
        name=name,
        title=known["title"],
        kind=known["kind"],
        base_url=base_url,
        api_key=api_key,
        model=model,
        edit_model=edit_model,
    )


def _edit_model(variable: str, default: str) -> str:
    """Модель для правки картинок; «off» — не править через этот сервис."""
    value = _get_str(variable, default) or default
    return "" if value.lower() in ("off", "none", "0", "false") else value


def _load_image_providers() -> tuple[ImageProviderConfig, ...]:
    names = _split_csv(_get_str("IMAGE_PROVIDERS", "cloudflare,pollinations").lower())
    return tuple(
        cfg for cfg in (_load_image_provider(name) for name in dict.fromkeys(names)) if cfg
    )


# Провайдеры поиска: имя → переменная с ключом ("" — ключ не нужен).
SEARCH_PROVIDERS: dict[str, str] = {
    "serper": "SERPER_API_KEY",
    "serpapi": "SERPAPI_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_API_KEY",
    "duckduckgo": "",
}


def _load_search() -> SearchConfig:
    """Провайдеры из SEARCH_PROVIDERS, у которых есть ключ, в порядке приоритета.

    DuckDuckGo работает без ключа; при SEARCH_DUCKDUCKGO=true (по умолчанию) он добавляется
    последним запасным вариантом, даже если его нет в SEARCH_PROVIDERS, поэтому поиск
    работает и без регистрации в платных сервисах.
    """
    names = _split_csv(_get_str("SEARCH_PROVIDERS", ",".join(SEARCH_PROVIDERS)).lower())
    duckduckgo = _get_bool("SEARCH_DUCKDUCKGO", True)
    providers: list[tuple[str, str]] = []
    for name in dict.fromkeys(names):
        if name not in SEARCH_PROVIDERS:
            raise ConfigError(
                f"SEARCH_PROVIDERS: неизвестный провайдер поиска {name!r}. "
                f"Доступны: {', '.join(SEARCH_PROVIDERS)}"
            )
        if not SEARCH_PROVIDERS[name]:
            if duckduckgo:
                providers.append((name, ""))
        elif key := _get_str(SEARCH_PROVIDERS[name]):
            providers.append((name, key))
    if duckduckgo and "duckduckgo" not in names:
        providers.append(("duckduckgo", ""))
    max_results = _get_int("SEARCH_MAX_RESULTS", 6, minimum=1)
    if max_results > 10:
        raise ConfigError(f"SEARCH_MAX_RESULTS должно быть от 1 до 10, получено: {max_results}")
    return SearchConfig(
        providers=tuple(providers),
        auto=_get_bool("SEARCH_AUTO", True),
        language=_get_str("SEARCH_LANGUAGE", "ru").lower(),
        region=_get_str("SEARCH_REGION").lower(),
        max_results=max_results,
        daily_limit=_get_int("SEARCH_DAILY_LIMIT", 100, minimum=0),
    )


def load_settings(env_file: str | None = ".env") -> Settings:
    """Читает .env (если есть) и переменные окружения, валидирует их."""
    if env_file:
        load_dotenv(env_file, override=False)

    bot_token = _get_str("BOT_TOKEN")
    if not bot_token:
        raise ConfigError("Не задан BOT_TOKEN (токен от @BotFather).")

    allowed_user_ids = _parse_user_ids(_get_str("ALLOWED_USER_IDS"))
    if not allowed_user_ids:
        raise ConfigError(
            "Не задан ALLOWED_USER_IDS. Укажите свой Telegram ID (узнать можно у @userinfobot), "
            "иначе ботом сможет пользоваться кто угодно."
        )

    provider_names = _split_csv(_get_str("AI_PROVIDERS", DEFAULT_PROVIDER_ORDER).lower())
    if not provider_names:
        raise ConfigError("AI_PROVIDERS пуст: укажите хотя бы одного провайдера.")
    providers = tuple(
        cfg for cfg in (_load_provider(name) for name in dict.fromkeys(provider_names)) if cfg
    )
    if not providers:
        raise ConfigError(
            "Ни один AI-провайдер не настроен. Укажите ключ хотя бы для одного из "
            f"AI_PROVIDERS={','.join(provider_names)} (например, GEMINI_API_KEY)."
        )

    return Settings(
        bot_token=bot_token,
        allowed_user_ids=allowed_user_ids,
        providers=providers,
        database_url=_get_str("DATABASE_URL", "sqlite+aiosqlite:///data/assistant.db"),
        assistant_name=_get_str("ASSISTANT_NAME", "Эля") or "Эля",
        history_max_messages=_get_int("HISTORY_MAX_MESSAGES", 30, minimum=2),
        history_max_chars=_get_int("HISTORY_MAX_CHARS", 12000, minimum=1000),
        max_output_tokens=_get_int("MAX_OUTPUT_TOKENS", 2048, minimum=64),
        temperature=_get_float("TEMPERATURE", 0.7, minimum=0.0, maximum=2.0),
        request_timeout=_get_float("REQUEST_TIMEOUT", 60.0, minimum=5.0, maximum=600.0),
        rate_limit_per_minute=_get_int("RATE_LIMIT_PER_MINUTE", 10, minimum=1),
        daily_request_limit=_get_int("DAILY_REQUEST_LIMIT", 300, minimum=0),
        log_level=_get_str("LOG_LEVEL", "INFO").upper() or "INFO",
        study_max_output_tokens=_get_int("STUDY_MAX_OUTPUT_TOKENS", 8192, minimum=1024),
        study_max_input_chars=_get_int("STUDY_MAX_INPUT_CHARS", 60000, minimum=1000),
        code_max_output_tokens=_get_int("CODE_MAX_OUTPUT_TOKENS", 4096, minimum=256),
        code_temperature=_get_float("CODE_TEMPERATURE", 0.2, minimum=0.0, maximum=2.0),
        code_history_max_chars=_get_int("CODE_HISTORY_MAX_CHARS", 40000, minimum=1000),
        code_max_file_chars=_get_int("CODE_MAX_FILE_CHARS", 30000, minimum=1000),
        documents_context_chars=_get_int("DOCUMENTS_CONTEXT_CHARS", 10000, minimum=2000),
        github_repo_token=_get_str("GITHUB_REPO_TOKEN"),
        sandbox_backend=_load_sandbox_backend(),
        sandbox_timeout=_get_float("SANDBOX_TIMEOUT", 15.0, minimum=1.0, maximum=300.0),
        sandbox_memory_mb=_get_int("SANDBOX_MEMORY_MB", 512, minimum=64),
        sandbox_max_runs=_get_int("SANDBOX_MAX_RUNS", 3, minimum=1),
        sandbox_image=_get_str("SANDBOX_IMAGE", "elya-sandbox") or "elya-sandbox",
        sandbox_python=_get_str("SANDBOX_PYTHON"),
        sandbox_dir=_get_str("SANDBOX_DIR"),
        webapp_url=_load_webapp_url(),
        webapp_host=_get_str("WEBAPP_HOST", "0.0.0.0") or "0.0.0.0",
        webapp_port=_load_webapp_port(),
        webapp_auth_max_age=_get_int("WEBAPP_AUTH_MAX_AGE", 86400, minimum=60),
        speech_api_key=_get_str("GROQ_API_KEY"),
        speech_model=_get_str("SPEECH_MODEL", "whisper-large-v3-turbo") or "whisper-large-v3-turbo",
        video_workdir=_get_str("VIDEO_WORKDIR"),
        video_max_clips=_get_int("VIDEO_MAX_CLIPS", 10, minimum=1),
        video_max_seconds=_get_int("VIDEO_MAX_SECONDS", 600, minimum=10),
        video_max_side=_load_video_max_side(),
        video_timeout=_get_float("VIDEO_TIMEOUT", 900.0, minimum=30.0, maximum=7200.0),
        video_upload_max_mb=_get_int("VIDEO_UPLOAD_MAX_MB", 100, minimum=1),
        ffmpeg_path=_get_str("FFMPEG_PATH", "ffmpeg") or "ffmpeg",
        ffprobe_path=_get_str("FFPROBE_PATH", "ffprobe") or "ffprobe",
        image_providers=_load_image_providers(),
        image_daily_limit=_get_int("IMAGE_DAILY_LIMIT", 50, minimum=0),
        search=_load_search(),
        speech_language=_load_speech_language(),
    )

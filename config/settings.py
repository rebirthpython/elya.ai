"""Загрузка и валидация конфигурации из переменных окружения (.env)."""

from __future__ import annotations

import os
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


@dataclass(frozen=True, slots=True)
class ImageProviderConfig:
    """Сервис, который рисует картинки по текстовому описанию.

    kind: "cloudflare" — Workers AI (POST {base_url}/{model});
          "openai" — OpenAI-совместимый POST {base_url}/images/generations.
    """

    name: str
    title: str
    kind: str
    base_url: str
    api_key: str
    model: str


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

    @property
    def webapp_enabled(self) -> bool:
        return bool(self.webapp_url)

    @property
    def images_enabled(self) -> bool:
        return bool(self.image_providers)


# Известные провайдеры с OpenAI-совместимым API (POST {base_url}/chat/completions).
# models — значения по умолчанию; их можно переопределить через <NAME>_MODELS.
KNOWN_PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "title": "Google Gemini API",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": "gemini-flash-latest,gemini-flash-lite-latest",
        "vision": "*",  # все модели Gemini понимают картинки
    },
    "openrouter": {
        "title": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": "",
    },
    "groq": {
        "title": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models": "",
        "vision": "meta-llama/llama-4-scout-17b-16e-instruct",
    },
    "mistral": {
        "title": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "models": "mistral-small-latest",
    },
    "custom": {
        "title": "Custom OpenAI-compatible API",
        "base_url": "",
        "models": "",
    },
}


# Сервисы картинок. Ключ — <NAME>_..., модель — <NAME>_IMAGE_MODEL (или IMAGE_CUSTOM_MODEL).
KNOWN_IMAGE_PROVIDERS: dict[str, dict[str, str]] = {
    "cloudflare": {
        "title": "Cloudflare Workers AI",
        "kind": "cloudflare",
        # FLUX.1 [schnell]: бесплатно в пределах 10 000 «нейронов» в сутки (~170 картинок).
        "model": "@cf/black-forest-labs/flux-1-schnell",
    },
    "pollinations": {
        "title": "Pollinations",
        "kind": "openai",
        "base_url": "https://gen.pollinations.ai/v1",
        "model": "black-forest-labs/flux.1-schnell",
    },
    "custom": {
        "title": "Custom OpenAI-compatible Images API",
        "kind": "openai",
        "base_url": "",
        "model": "",
    },
}
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4/accounts"


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


def _load_provider(name: str) -> ProviderConfig | None:
    """Возвращает конфиг провайдера или None, если для него не задан ключ."""
    known = KNOWN_PROVIDERS.get(name)
    if known is None:
        raise ConfigError(
            f"AI_PROVIDERS: неизвестный провайдер {name!r}. "
            f"Доступны: {', '.join(KNOWN_PROVIDERS)}"
        )
    prefix = name.upper()
    api_key = _get_str(f"{prefix}_API_KEY")
    base_url = _get_str(f"{prefix}_BASE_URL", known["base_url"]).rstrip("/")
    models = tuple(_split_csv(_get_str(f"{prefix}_MODELS", known["models"])))

    if name == "custom":
        # Локальные серверы (например, Ollama) могут работать без ключа.
        if not base_url:
            return None
    elif not api_key:
        return None

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
    return ProviderConfig(
        name=name,
        title=known["title"],
        base_url=base_url,
        api_key=api_key,
        models=models,
        vision_models=vision_models,
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
    else:
        prefix = name.upper()
        api_key = _get_str(f"{prefix}_API_KEY")
        if not api_key:
            return None
        base_url = known["base_url"]
        model = _get_str(f"{prefix}_IMAGE_MODEL", known["model"]) or known["model"]
    return ImageProviderConfig(
        name=name,
        title=known["title"],
        kind=known["kind"],
        base_url=base_url,
        api_key=api_key,
        model=model,
    )


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

    provider_names = _split_csv(_get_str("AI_PROVIDERS", "gemini,openrouter").lower())
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
    )

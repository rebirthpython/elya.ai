"""Загрузка и валидация конфигурации из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    providers: tuple[ProviderConfig, ...]
    database_url: str
    assistant_name: str
    history_max_messages: int
    history_max_chars: int
    history_store_limit: int
    max_output_tokens: int
    temperature: float
    request_timeout: float
    rate_limit_per_minute: int
    daily_request_limit: int
    log_level: str


# Известные провайдеры с OpenAI-совместимым API (POST {base_url}/chat/completions).
# models — значения по умолчанию; их можно переопределить через <NAME>_MODELS.
KNOWN_PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "title": "Google Gemini API",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": "gemini-flash-latest,gemini-flash-lite-latest",
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
    },
    "custom": {
        "title": "Custom OpenAI-compatible API",
        "base_url": "",
        "models": "",
    },
}


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
    return ProviderConfig(
        name=name,
        title=known["title"],
        base_url=base_url,
        api_key=api_key,
        models=models,
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
        history_store_limit=_get_int("HISTORY_STORE_LIMIT", 500, minimum=10),
        max_output_tokens=_get_int("MAX_OUTPUT_TOKENS", 2048, minimum=64),
        temperature=_get_float("TEMPERATURE", 0.7, minimum=0.0, maximum=2.0),
        request_timeout=_get_float("REQUEST_TIMEOUT", 60.0, minimum=5.0, maximum=600.0),
        rate_limit_per_minute=_get_int("RATE_LIMIT_PER_MINUTE", 10, minimum=1),
        daily_request_limit=_get_int("DAILY_REQUEST_LIMIT", 300, minimum=0),
        log_level=_get_str("LOG_LEVEL", "INFO").upper() or "INFO",
    )

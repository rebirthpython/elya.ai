import os

import certifi
import pytest

from config.settings import DEFAULT_PROVIDER_ORDER, KNOWN_PROVIDERS, ConfigError, load_settings

ENV_KEYS = [
    "BOT_TOKEN", "ALLOWED_USER_IDS", "AI_PROVIDERS", "DAILY_REQUEST_LIMIT", "TEMPERATURE",
    "WEBAPP_URL", "WEBAPP_HOST", "WEBAPP_PORT", "WEBAPP_AUTH_MAX_AGE", "SPEECH_LANGUAGE",
]
KEYLESS = [name for name in DEFAULT_PROVIDER_ORDER.split(",") if KNOWN_PROVIDERS[name].get("keyless")]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Все настройки провайдеров: <ИМЯ>_API_KEY, <ИМЯ>_MODELS, <ИМЯ>_BASE_URL и т. д.
    prefixes = tuple(f"{name.upper()}_" for name in KNOWN_PROVIDERS)
    for key in list(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111, 222")


def test_minimal_gemini_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    settings = load_settings(env_file=None)
    assert settings.allowed_user_ids == frozenset({111, 222})
    # провайдеры без ключа пропущены, сервисы без ключа (keyless) стоят в конце
    assert [p.name for p in settings.providers] == ["gemini", *KEYLESS]
    assert settings.providers[0].models == ("gemini-flash-latest", "gemini-flash-lite-latest")


def test_provider_order_and_custom_without_key(monkeypatch):
    monkeypatch.setenv("AI_PROVIDERS", "custom, gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("CUSTOM_BASE_URL", "http://localhost:11434/v1/")
    monkeypatch.setenv("CUSTOM_MODELS", "qwen2.5:7b")
    settings = load_settings(env_file=None)
    assert [p.name for p in settings.providers] == ["custom", "gemini"]
    assert settings.providers[0].base_url == "http://localhost:11434/v1"


@pytest.mark.parametrize(
    "env",
    [
        {"ALLOWED_USER_IDS": ""},
        {"ALLOWED_USER_IDS": "abc", "GEMINI_API_KEY": "k"},
        {"AI_PROVIDERS": "unknown", "GEMINI_API_KEY": "k"},
        {"AI_PROVIDERS": "openrouter", "OPENROUTER_API_KEY": "k", "OPENROUTER_MODELS": " , "},
        {"AI_PROVIDERS": "custom", "CUSTOM_BASE_URL": "http://localhost:11434/v1"},  # нет моделей
        {"AI_PROVIDERS": "cloudflare", "CLOUDFLARE_API_KEY": "k"},  # нет ID аккаунта
        {"GEMINI_API_KEY": "k", "GEMINI_CA_BUNDLE": "/nonexistent/ca.pem"},
        {"GEMINI_API_KEY": "k", "TEMPERATURE": "5"},
        {"GEMINI_API_KEY": "k", "WEBAPP_URL": "http://elya.example"},  # Telegram требует HTTPS
        {"GEMINI_API_KEY": "k", "WEBAPP_URL": "elya.example"},
        {"GEMINI_API_KEY": "k", "WEBAPP_URL": "https://elya.example/?demo"},
        {"GEMINI_API_KEY": "k", "WEBAPP_PORT": "70000"},
        {"AI_PROVIDERS": "gemini,openrouter"},  # ни одного ключа
    ],
)
def test_invalid_configs(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError):
        load_settings(env_file=None)


def test_mini_app_is_off_by_default(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    settings = load_settings(env_file=None)
    assert settings.webapp_url == "" and settings.webapp_enabled is False


def test_mini_app_settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("WEBAPP_URL", "https://elya.example/app/")
    monkeypatch.setenv("WEBAPP_PORT", "9000")
    settings = load_settings(env_file=None)
    assert settings.webapp_enabled is True
    assert settings.webapp_url == "https://elya.example/app/"
    assert (settings.webapp_host, settings.webapp_port) == ("0.0.0.0", 9000)
    assert settings.webapp_auth_max_age == 86400


def test_mistral_provider_has_default_model(monkeypatch):
    monkeypatch.setenv("AI_PROVIDERS", "mistral")
    monkeypatch.setenv("MISTRAL_API_KEY", "m-key")
    provider = load_settings(env_file=None).providers[0]
    assert provider.base_url == "https://api.mistral.ai/v1"
    assert provider.models == ("mistral-large-latest", "mistral-medium-latest", "mistral-small-latest")
    assert provider.vision_models == provider.models


def test_every_known_provider_is_ready_to_use():
    """Достаточно вписать ключ: у каждого провайдера есть адрес и модели по умолчанию."""
    order = DEFAULT_PROVIDER_ORDER.split(",")
    assert len(order) == len(set(order))
    assert set(order) == set(KNOWN_PROVIDERS) - {"custom"}
    for name in order:
        known = KNOWN_PROVIDERS[name]
        assert known["title"] and known["base_url"].startswith("https://"), name
        assert known["models"], name
    # бесплатные сервисы без ключа — запасной вариант, они в самом конце
    assert order[-len(KEYLESS):] == KEYLESS


def test_all_providers_with_keys(monkeypatch):
    for name in KNOWN_PROVIDERS:
        monkeypatch.setenv(f"{name.upper()}_API_KEY", f"{name}-key")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    providers = load_settings(env_file=None).providers
    assert [p.name for p in providers] == DEFAULT_PROVIDER_ORDER.split(",")
    assert all(p.models for p in providers)
    by_name = {p.name: p for p in providers}
    assert by_name["cloudflare"].base_url == (
        "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1"
    )
    assert by_name["github"].max_output_tokens == 4000
    assert by_name["gemini"].max_output_tokens == 0
    assert by_name["ovh"].api_key == "ovh-key"  # ключ необязателен, но передаётся
    giga = by_name["gigachat"]
    assert giga.kind == "gigachat" and giga.api_key == "gigachat-key"
    assert giga.auth_url.startswith("https://ngw.devices.sberbank.ru")
    assert giga.scope == "GIGACHAT_API_PERS"
    assert {p.kind for p in providers if p.name != "gigachat"} == {"openai"}


def test_keyless_provider_works_without_key(monkeypatch):
    monkeypatch.setenv("AI_PROVIDERS", "ovh")
    provider = load_settings(env_file=None).providers[0]
    assert provider.name == "ovh" and provider.api_key == ""
    assert provider.vision_models  # у OVH есть модель для фото


def test_provider_overrides(monkeypatch):
    monkeypatch.setenv("AI_PROVIDERS", "github,gigachat")
    monkeypatch.setenv("GITHUB_API_KEY", "ghp")
    monkeypatch.setenv("GITHUB_MAX_TOKENS", "2000")
    monkeypatch.setenv("GITHUB_CA_BUNDLE", certifi.where())
    monkeypatch.setenv("GIGACHAT_API_KEY", "auth-key")
    monkeypatch.setenv("GIGACHAT_SCOPE", "GIGACHAT_API_B2B")
    monkeypatch.setenv("GIGACHAT_MODELS", "GigaChat-2-Max,GigaChat-2")
    github, giga = load_settings(env_file=None).providers
    assert github.max_output_tokens == 2000 and github.ca_bundle == certifi.where()
    assert giga.scope == "GIGACHAT_API_B2B"
    assert giga.models == ("GigaChat-2-Max", "GigaChat-2")



def test_video_settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    for key in ("VIDEO_MAX_CLIPS", "VIDEO_MAX_SECONDS", "VIDEO_MAX_SIDE", "VIDEO_TIMEOUT", "VIDEO_WORKDIR",
                "FFMPEG_PATH", "FFPROBE_PATH"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(env_file=None)
    assert (settings.video_max_clips, settings.video_max_seconds, settings.video_max_side) == (10, 600, 1920)
    assert settings.ffmpeg_path == "ffmpeg" and settings.video_workdir == ""

    monkeypatch.setenv("VIDEO_MAX_SIDE", "1280")
    monkeypatch.setenv("FFMPEG_PATH", "/opt/ffmpeg/bin/ffmpeg")
    settings = load_settings(env_file=None)
    assert settings.video_max_side == 1280 and settings.ffmpeg_path == "/opt/ffmpeg/bin/ffmpeg"

    monkeypatch.setenv("VIDEO_MAX_SIDE", "8000")
    with pytest.raises(ConfigError, match="VIDEO_MAX_SIDE"):
        load_settings(env_file=None)


def test_code_mode_settings(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    for key in ("CODE_MAX_OUTPUT_TOKENS", "CODE_TEMPERATURE", "CODE_HISTORY_MAX_CHARS", "CODE_MAX_FILE_CHARS"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(env_file=None)
    assert (settings.code_max_output_tokens, settings.code_temperature) == (4096, 0.2)
    assert (settings.code_history_max_chars, settings.code_max_file_chars) == (40000, 30000)

    monkeypatch.setenv("CODE_MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setenv("CODE_TEMPERATURE", "0")
    settings = load_settings(env_file=None)
    assert (settings.code_max_output_tokens, settings.code_temperature) == (8192, 0.0)

    monkeypatch.setenv("CODE_TEMPERATURE", "5")
    with pytest.raises(ConfigError):
        load_settings(env_file=None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "ru"), ("", "ru"), ("EN", "en"), (" uk ", "uk"), ("auto", ""), ("Auto", "")],
)
def test_speech_language(monkeypatch, raw, expected):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    if raw is not None:
        monkeypatch.setenv("SPEECH_LANGUAGE", raw)
    assert load_settings(env_file=None).speech_language == expected


@pytest.mark.parametrize("raw", ["russian", "r", "ru-RU"])
def test_speech_language_invalid(monkeypatch, raw):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("SPEECH_LANGUAGE", raw)
    with pytest.raises(ConfigError):
        load_settings(env_file=None)

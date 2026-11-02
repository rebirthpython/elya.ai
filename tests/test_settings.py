import pytest

from config.settings import ConfigError, load_settings

ENV_KEYS = [
    "BOT_TOKEN", "ALLOWED_USER_IDS", "AI_PROVIDERS", "GEMINI_API_KEY", "GEMINI_MODELS",
    "OPENROUTER_API_KEY", "OPENROUTER_MODELS", "GROQ_API_KEY", "GROQ_MODELS",
    "CUSTOM_BASE_URL", "CUSTOM_API_KEY", "CUSTOM_MODELS", "DAILY_REQUEST_LIMIT", "TEMPERATURE",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111, 222")


def test_minimal_gemini_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    settings = load_settings(env_file=None)
    assert settings.allowed_user_ids == frozenset({111, 222})
    assert [p.name for p in settings.providers] == ["gemini"]  # openrouter без ключа пропущен
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
        {"AI_PROVIDERS": "openrouter", "OPENROUTER_API_KEY": "k"},  # нет моделей
        {"GEMINI_API_KEY": "k", "TEMPERATURE": "5"},
        {},  # ни одного ключа
    ],
)
def test_invalid_configs(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError):
        load_settings(env_file=None)

"""Mini App: проверка initData и HTTP API поверх настоящего aiohttp-приложения."""

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ai.client import AIClient
from ai.providers.base import BaseProvider, CompletionResult, ProviderUnavailableError
from config.settings import ProviderConfig, Settings
from database.session import create_engine, create_session_factory, init_db
from memory.manager import MemoryManager
from memory.repository import MessageRepository
from services.chat import ChatService
from services.longterm import MAX_FACTS, LongTermMemory
from services.reminders import Reminders
from services.preferences import PreferencesStore
from services.rate_limit import SlidingWindowLimiter
from services.usage import UsageTracker
from web.auth import InitDataError, extract_init_data, verify_init_data
from web.server import MAX_TEXT_LENGTH, create_web_app

TOKEN = "42:TEST"
OWNER_ID = 111
STRANGER_ID = 999


def sign_init_data(
    user_id: int = OWNER_ID,
    *,
    token: str = TOKEN,
    auth_date: float | None = None,
    user: dict | None = None,
    with_user: bool = True,
) -> str:
    """Собирает initData так же, как это делает Telegram."""
    fields = {"auth_date": str(int(auth_date if auth_date is not None else time.time())), "query_id": "AAE"}
    if with_user:
        payload = user or {"id": user_id, "first_name": "Аня", "language_code": "ru"}
        fields["user"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def auth(init_data: str | None = None) -> dict[str, str]:
    return {"Authorization": f"tma {init_data if init_data is not None else sign_init_data()}"}


class ScriptedProvider(BaseProvider):
    def __init__(self, name: str, models: tuple[str, ...], *, fail: bool = False) -> None:
        super().__init__(name, f"{name.title()} AI", models)
        self.fail = fail
        self.calls: list[tuple[str, list]] = []

    async def complete(self, messages, *, model, max_tokens, temperature):
        self.calls.append((model, messages))
        if self.fail:
            raise ProviderUnavailableError("timeout", provider=self.name, model=model)
        return CompletionResult(
            text=f"**Ответ** на: {messages[-1].content}", provider=self.name, model=model
        )


def _settings(**overrides) -> Settings:
    values = dict(
        bot_token=TOKEN, allowed_user_ids=frozenset({OWNER_ID}),
        providers=(ProviderConfig("alpha", "Alpha AI", "http://x", "", ("a1",)),),
        database_url="", assistant_name="Эля", history_max_messages=30, history_max_chars=12000,
        history_store_limit=500, max_output_tokens=256, temperature=0.5, request_timeout=30,
        rate_limit_per_minute=10, daily_request_limit=100, log_level="INFO",
        webapp_url="https://elya.example", webapp_auth_max_age=3600,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'web.db'}")
    await init_db(engine)
    session_factory = create_session_factory(engine)
    memory = MemoryManager(MessageRepository(session_factory, store_limit=500), history_limit=30)
    preferences = PreferencesStore(session_factory)
    long_memory = LongTermMemory(session_factory)
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)  # пятница
    reminders = Reminders(session_factory, clock=lambda: now)
    clients: list[TestClient] = []

    async def make(*, providers=None, daily_limit=100, rate_limit=10, speech=None, **settings_overrides):
        providers = providers or [
            ScriptedProvider("alpha", ("a1", "a2")),
            ScriptedProvider("beta", ("b1",)),
        ]
        settings = _settings(**settings_overrides)
        usage = UsageTracker(session_factory, daily_limit)
        ai_client = AIClient(providers, max_tokens=256, temperature=0.5, usage=usage)
        app = create_web_app(
            settings,
            ChatService(settings, ai_client, memory, preferences, long_memory, reminders),
            memory,
            usage,
            ai_client,
            SlidingWindowLimiter(rate_limit),
            preferences,
            long_memory,
            reminders,
            speech,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client, providers

    yield {
        "make": make, "memory": memory, "preferences": preferences, "long_memory": long_memory,
        "reminders": reminders,
    }
    for client in clients:
        await client.close()
    await engine.dispose()


# ---------- initData ----------


def test_valid_init_data_returns_user():
    user = verify_init_data(sign_init_data(), TOKEN, max_age=3600)
    assert user.id == OWNER_ID and user.first_name == "Аня"


@pytest.mark.parametrize(
    "init_data",
    [
        sign_init_data(token="1:OTHER"),  # подписано чужим токеном
        sign_init_data().replace("111", "999"),  # данные изменены после подписи
        sign_init_data(auth_date=time.time() - 7200),  # устарело
        sign_init_data(auth_date=time.time() + 3600),  # «из будущего»
        sign_init_data(with_user=False),  # нет пользователя
        sign_init_data(user={"id": 5, "first_name": "Bot", "is_bot": True}),
        "auth_date=1&user=%7B%7D",  # нет подписи
        "мусор",
    ],
)
def test_bad_init_data_is_rejected(init_data):
    with pytest.raises(InitDataError):
        verify_init_data(init_data, TOKEN, max_age=3600)


def test_init_data_age_is_checked_against_given_time():
    signed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    init_data = sign_init_data(auth_date=signed_at.timestamp())
    assert verify_init_data(init_data, TOKEN, max_age=60, now=signed_at + timedelta(seconds=59))
    with pytest.raises(InitDataError):
        verify_init_data(init_data, TOKEN, max_age=60, now=signed_at + timedelta(seconds=61))


@pytest.mark.parametrize("header", [None, "", "tma", "tma   ", "Bearer abc", "abc"])
def test_authorization_header_must_carry_init_data(header):
    with pytest.raises(InitDataError):
        extract_init_data(header)


def test_authorization_header_is_parsed():
    assert extract_init_data("TMA  a=1&b=2 ") == "a=1&b=2"


# ---------- статика ----------


async def test_interface_is_served_with_security_headers(env):
    client, _ = await env["make"]()

    page = await client.get("/")
    assert page.status == 200
    html = await page.text()
    assert "telegram-web-app.js" in html and "assets/app.js" in html
    assert "script-src 'self' https://telegram.org" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert page.headers["Cache-Control"] == "no-cache"

    for asset in ("app.js", "markdown.js", "styles.css", "icon.svg"):
        response = await client.get(f"/assets/{asset}")
        assert response.status == 200, asset
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    assert (await client.get("/healthz")).status == 200
    for hidden in ("/main.py", "/.env", "/assets/%2e%2e/%2e%2e/main.py", "/assets/"):
        assert (await client.get(hidden)).status in (403, 404), hidden


# ---------- доступ ----------


@pytest.mark.parametrize("path", ["/api/state", "/api/chat", "/api/clear", "/api/unknown"])
async def test_api_rejects_requests_without_valid_init_data(env, path):
    client, providers = await env["make"]()
    for headers in ({}, {"Authorization": "tma hash=abc"}, auth(sign_init_data(token="1:OTHER"))):
        response = await client.post(path, json={"text": "привет"}, headers=headers)
        assert response.status == 401
        assert (await response.json())["error"]["code"] == "unauthorized"
        assert response.headers["Cache-Control"] == "no-store"
    assert providers[0].calls == []


async def test_stranger_is_forbidden(env):
    client, providers = await env["make"]()
    headers = auth(sign_init_data(STRANGER_ID))

    assert (await client.get("/api/state", headers=headers)).status == 403
    response = await client.post("/api/chat", json={"text": "привет"}, headers=headers)
    assert response.status == 403
    assert (await response.json())["error"]["code"] == "forbidden"
    assert providers[0].calls == []
    assert await env["memory"].get_history(STRANGER_ID) == []


async def test_expired_session_is_rejected(env):
    client, _ = await env["make"](webapp_auth_max_age=600)
    stale = auth(sign_init_data(auth_date=time.time() - 601))
    assert (await client.get("/api/state", headers=stale)).status == 401


# ---------- API ----------


async def test_state_describes_assistant_models_and_history(env):
    await env["memory"].save_exchange(OWNER_ID, "вопрос из бота", "ответ в боте")
    client, _ = await env["make"]()

    response = await client.get("/api/state", headers=auth())
    assert response.status == 200
    data = await response.json()

    assert data["assistant"] == {"name": "Эля"}
    assert data["user"] == {"first_name": "Аня"}
    assert [(m["role"], m["content"]) for m in data["history"]] == [
        ("user", "вопрос из бота"), ("assistant", "ответ в боте"),
    ]
    assert abs(data["history"][0]["ts"] - time.time()) < 60
    assert [m["id"] for m in data["models"]] == ["alpha:a1", "alpha:a2", "beta:b1"]
    assert data["models"][0] == {"id": "alpha:a1", "provider": "Alpha AI", "model": "a1"}
    assert data["providers"] == ["Alpha AI", "Beta AI"]
    assert data["usage"] == {"used": 0, "limit": 100}
    assert data["limits"] == {"max_text_length": MAX_TEXT_LENGTH}
    assert data["preferences"] == {"style": "friendly", "verbosity": "normal"}
    assert data["memory"] == {
        "enabled": False, "facts": [], "max_facts": MAX_FACTS, "max_fact_length": 200,
    }
    assert data["reminders"] == {"timezone": None, "items": [], "max": 50, "max_text": 300}
    assert data["voice"] is False
    assert data["vision"] is False
    assert [o["id"] for o in data["preference_options"]["style"]] == [
        "friendly", "caring", "playful", "business",
    ]
    assert [o["id"] for o in data["preference_options"]["verbosity"]] == [
        "short", "normal", "detailed",
    ]


async def test_chat_replies_and_shares_memory_with_the_bot(env):
    await env["memory"].save_exchange(OWNER_ID, "вопрос из бота", "ответ в боте")
    client, providers = await env["make"]()

    response = await client.post("/api/chat", json={"text": "  привет из приложения  "}, headers=auth())
    assert response.status == 200
    data = await response.json()
    assert data["reply"]["content"] == "**Ответ** на: привет из приложения"
    assert data["reply"]["role"] == "assistant"
    assert (data["reply"]["provider"], data["reply"]["model"]) == ("alpha", "a1")
    assert data["reply"]["truncated"] is False
    assert data["usage"] == {"used": 1, "limit": 100}

    model, context = providers[0].calls[0]
    assert context[0].role == "system"
    assert "Ты — Эля" in context[0].content and "«Аня»" in context[0].content
    assert [m.content for m in context[1:]] == [
        "вопрос из бота", "ответ в боте", "привет из приложения",
    ]
    assert [m.content for m in await env["memory"].get_history(OWNER_ID)] == [
        "вопрос из бота", "ответ в боте",
        "привет из приложения", "**Ответ** на: привет из приложения",
    ]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"text": ""}, "bad_request"),
        ({"text": "   \n "}, "bad_request"),
        ({"text": 42}, "bad_request"),
        ({}, "bad_request"),
        ([1, 2], "bad_request"),
        ("не json", "bad_request"),
        ({"text": "а" * (MAX_TEXT_LENGTH + 1)}, "too_long"),
    ],
)
async def test_chat_rejects_invalid_input(env, body, code):
    client, providers = await env["make"]()
    kwargs = {"data": body} if isinstance(body, str) else {"json": body}
    response = await client.post("/api/chat", headers=auth(), **kwargs)
    assert response.status == 400
    assert (await response.json())["error"]["code"] == code
    assert providers[0].calls == []


async def test_oversized_body_gets_json_error(env):
    client, providers = await env["make"]()
    response = await client.post("/api/chat", data=b"x" * 200_000, headers=auth())
    assert response.status == 413
    assert "error" in await response.json()
    assert providers[0].calls == []


async def test_longest_allowed_message_is_accepted(env):
    client, _ = await env["make"]()
    response = await client.post("/api/chat", json={"text": "я" * MAX_TEXT_LENGTH}, headers=auth())
    assert response.status == 200


async def test_preferred_model_goes_first_and_unknown_means_auto(env):
    client, providers = await env["make"]()
    alpha, beta = providers

    response = await client.post("/api/chat", json={"text": "раз", "model": "beta:b1"}, headers=auth())
    assert (await response.json())["reply"]["model"] == "b1"
    assert alpha.calls == [] and len(beta.calls) == 1

    for unknown in ("gamma:x", "beta:", 5, None):
        response = await client.post("/api/chat", json={"text": "два", "model": unknown}, headers=auth())
        assert (await response.json())["reply"]["model"] == "a1", unknown


async def test_preferred_model_falls_back_when_it_fails(env):
    providers = [ScriptedProvider("alpha", ("a1",)), ScriptedProvider("beta", ("b1",), fail=True)]
    client, _ = await env["make"](providers=providers)
    response = await client.post("/api/chat", json={"text": "привет", "model": "beta:b1"}, headers=auth())
    assert response.status == 200
    assert (await response.json())["reply"]["model"] == "a1"


async def test_rate_limit_returns_429_with_retry_after(env):
    client, providers = await env["make"](rate_limit=2)
    for _ in range(2):
        assert (await client.post("/api/chat", json={"text": "привет"}, headers=auth())).status == 200

    response = await client.post("/api/chat", json={"text": "ещё"}, headers=auth())
    assert response.status == 429
    error = (await response.json())["error"]
    assert error["code"] == "rate_limited" and 1 <= error["retry_after"] <= 60
    assert len(providers[0].calls) == 2
    # чтение истории лимитом сообщений не ограничено
    assert (await client.get("/api/state", headers=auth())).status == 200


async def test_ai_failure_is_reported_and_history_stays_clean(env):
    providers = [ScriptedProvider("alpha", ("a1",), fail=True)]
    client, _ = await env["make"](providers=providers)

    response = await client.post("/api/chat", json={"text": "ты тут?"}, headers=auth())
    assert response.status == 503
    error = (await response.json())["error"]
    assert error["code"] == "ai_unavailable" and "не отвечает" in error["message"]
    assert await env["memory"].get_history(OWNER_ID) == []


async def test_daily_limit_is_reported(env):
    client, providers = await env["make"](daily_limit=1)
    assert (await client.post("/api/chat", json={"text": "раз"}, headers=auth())).status == 200

    response = await client.post("/api/chat", json={"text": "два"}, headers=auth())
    assert response.status == 429
    assert (await response.json())["error"]["code"] == "daily_limit"
    assert len(providers[0].calls) == 1


async def test_clear_removes_only_own_history(env):
    await env["memory"].save_exchange(OWNER_ID, "моё", "ответ")
    await env["memory"].save_exchange(222, "чужое", "ответ")
    client, _ = await env["make"](allowed_user_ids=frozenset({OWNER_ID, 222}))

    response = await client.post("/api/clear", json={}, headers=auth())
    assert response.status == 200 and await response.json() == {"deleted": 2}
    assert await env["memory"].get_history(OWNER_ID) == []
    assert len(await env["memory"].get_history(222)) == 2

    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["history"] == []


async def test_unknown_api_route_and_wrong_method_return_json(env):
    client, _ = await env["make"]()
    missing = await client.get("/api/nope", headers=auth())
    assert missing.status == 404 and "error" in await missing.json()
    wrong = await client.get("/api/chat", headers=auth())
    assert wrong.status == 405 and "error" in await wrong.json()


async def test_preferences_are_saved_and_shape_the_prompt(env):
    client, providers = await env["make"]()

    response = await client.post("/api/preferences", json={"style": "business"}, headers=auth())
    assert response.status == 200
    assert await response.json() == {"preferences": {"style": "business", "verbosity": "normal"}}

    response = await client.post("/api/preferences", json={"verbosity": "short"}, headers=auth())
    assert (await response.json())["preferences"] == {"style": "business", "verbosity": "short"}

    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["preferences"] == {"style": "business", "verbosity": "short"}

    await client.post("/api/chat", json={"text": "привет"}, headers=auth())
    system = providers[0].calls[0][1][0].content
    assert "деловой" in system and "на «вы»" in system
    assert "Отвечай коротко" in system


@pytest.mark.parametrize(
    "body",
    [{"style": "rude"}, {"verbosity": "endless"}, {"style": 5}, [1], "не json"],
)
async def test_invalid_preferences_are_rejected(env, body):
    client, _ = await env["make"]()
    kwargs = {"data": body} if isinstance(body, str) else {"json": body}
    response = await client.post("/api/preferences", headers=auth(), **kwargs)
    assert response.status == 400
    assert await env["preferences"].get(OWNER_ID) == await env["preferences"].get(0)


async def test_preferences_are_per_user(env):
    client, _ = await env["make"](allowed_user_ids=frozenset({OWNER_ID, 222}))
    await client.post("/api/preferences", json={"style": "playful"}, headers=auth())
    assert (await env["preferences"].get(OWNER_ID)).style == "playful"
    assert (await env["preferences"].get(222)).style == "friendly"


# ---------- долговременная память ----------


async def test_memory_toggle_add_list_delete(env):
    client, _ = await env["make"]()

    response = await client.post("/api/memory", json={"enabled": True}, headers=auth())
    assert (await response.json())["enabled"] is True

    response = await client.post("/api/memory/add", json={"text": "  Любит чай без сахара "}, headers=auth())
    data = await response.json()
    assert response.status == 200 and data["added"]["text"] == "Любит чай без сахара"
    first_id = data["added"]["id"]
    await client.post("/api/memory/add", json={"text": "Живёт в Казани"}, headers=auth())

    again = await (await client.post("/api/memory/add", json={"text": "любит чай без сахара"}, headers=auth())).json()
    assert again["added"] is None and len(again["facts"]) == 2  # дубликат не добавлен

    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["memory"]["enabled"] is True
    assert [f["text"] for f in state["memory"]["facts"]] == ["Любит чай без сахара", "Живёт в Казани"]

    data = await (await client.post("/api/memory/delete", json={"id": first_id}, headers=auth())).json()
    assert [f["text"] for f in data["facts"]] == ["Живёт в Казани"]
    data = await (await client.post("/api/memory/delete", json={"all": True}, headers=auth())).json()
    assert data["facts"] == [] and data["enabled"] is True


@pytest.mark.parametrize(
    ("text", "code"),
    [("да", "too_short"), ("а" * 250, "too_long"), ("мой пароль 12345", "sensitive")],
)
async def test_memory_add_validates(env, text, code):
    client, _ = await env["make"]()
    response = await client.post("/api/memory/add", json={"text": text}, headers=auth())
    assert response.status == 400
    assert (await response.json())["error"]["code"] == code
    assert await env["long_memory"].list(OWNER_ID) == []


async def test_memory_full_returns_409(env):
    client, _ = await env["make"]()
    import random

    rng = random.Random(1)
    for i in range(MAX_FACTS):
        # непохожие друг на друга, иначе их склеит защита от дубликатов
        text = "".join(rng.choice("абвгдежзиклмнопрстуфхцчшэюя") for _ in range(16))
        data = await (await client.post("/api/memory/add", json={"text": text}, headers=auth())).json()
        assert data["added"] is not None, text
    response = await client.post("/api/memory/add", json={"text": "Совсем другой факт про собаку"}, headers=auth())
    assert response.status == 409 and (await response.json())["error"]["code"] == "memory_full"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/memory", {"enabled": "yes"}),
        ("/api/memory", {}),
        ("/api/memory/add", {"text": 5}),
        ("/api/memory/delete", {"id": "1"}),
        ("/api/memory/delete", {"id": True}),
        ("/api/memory/delete", {}),
    ],
)
async def test_memory_endpoints_reject_bad_input(env, path, body):
    client, _ = await env["make"]()
    response = await client.post(path, json=body, headers=auth())
    assert response.status == 400


async def test_memory_is_private_per_user_and_requires_access(env):
    client, _ = await env["make"](allowed_user_ids=frozenset({OWNER_ID, 222}))
    await client.post("/api/memory/add", json={"text": "Мой секретный факт"}, headers=auth())
    mine = (await env["long_memory"].list(OWNER_ID))[0]

    other = auth(sign_init_data(222))
    state = await (await client.get("/api/state", headers=other)).json()
    assert state["memory"]["facts"] == []
    await client.post("/api/memory/delete", json={"id": mine.id}, headers=other)  # чужой факт не удалить
    assert len(await env["long_memory"].list(OWNER_ID)) == 1

    for path in ("/api/memory", "/api/memory/add", "/api/memory/delete"):
        assert (await client.post(path, json={}, headers={})).status == 401
        assert (await client.post(path, json={}, headers=auth(sign_init_data(STRANGER_ID)))).status == 403


async def test_enabled_memory_reaches_the_prompt_via_the_app(env):
    client, providers = await env["make"]()
    await client.post("/api/memory", json={"enabled": True}, headers=auth())
    await client.post("/api/memory/add", json={"text": "Любит чай без сахара"}, headers=auth())
    await client.post("/api/chat", json={"text": "привет"}, headers=auth())
    assert "Любит чай без сахара" in providers[0].calls[0][1][0].content


# ---------- напоминания ----------


async def test_timezone_is_saved_from_the_phone(env):
    client, _ = await env["make"]()
    response = await client.post("/api/timezone", json={"tz": "Europe/Moscow"}, headers=auth())
    assert response.status == 200 and (await response.json())["timezone"] == "Europe/Moscow"
    state = await (await client.get("/api/state", headers=auth())).json()
    assert state["reminders"]["timezone"] == "Europe/Moscow"
    response = await client.post("/api/timezone", json={"tz": "Mars/Base"}, headers=auth())
    assert response.status == 400 and (await response.json())["error"]["code"] == "bad_timezone"
    for body in ({}, {"tz": 5}, {"tz": "x" * 100}):
        assert (await client.post("/api/timezone", json=body, headers=auth())).status == 400


async def test_add_list_delete_reminders_in_the_users_timezone(env):
    client, _ = await env["make"]()
    await client.post("/api/timezone", json={"tz": "Europe/Moscow"}, headers=auth())

    body = {"text": "  Позвонить   маме ", "when": "2026-11-14T09:00", "repeat": "weekly"}
    data = await (await client.post("/api/reminders/add", json=body, headers=auth())).json()
    [item] = data["items"]
    assert item["text"] == "Позвонить маме" and item["repeat"] == "weekly"
    # 09:00 по Москве = 06:00 UTC
    assert item["due"] == int(datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc).timestamp())

    await client.post("/api/reminders/add", json={"text": "Раньше", "when": "2026-11-13T20:00:30"}, headers=auth())
    state = await (await client.get("/api/state", headers=auth())).json()
    assert [r["text"] for r in state["reminders"]["items"]] == ["Раньше", "Позвонить маме"]

    data = await (await client.post("/api/reminders/delete", json={"id": item["id"]}, headers=auth())).json()
    assert [r["text"] for r in data["items"]] == ["Раньше"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"text": "x", "when": "2026-11-13T15:00"}, "past"),  # сейчас 15:00 по Москве
        ({"text": "x", "when": "2026-11-12T09:00"}, "past"),
        ({"text": "x", "when": "2028-01-01T09:00"}, "far"),
        ({"text": "x", "when": "завтра"}, "bad_request"),
        ({"text": "x", "when": "2026-11-14T09:00+03:00"}, "bad_request"),
        ({"text": "  ", "when": "2026-11-14T09:00"}, "bad_request"),
        ({"text": "я" * 301, "when": "2026-11-14T09:00"}, "too_long"),
        ({"text": "x", "when": "2026-11-14T09:00", "repeat": "yearly"}, "bad_request"),
        ({"text": 5, "when": "2026-11-14T09:00"}, "bad_request"),
        ({"when": "2026-11-14T09:00"}, "bad_request"),
    ],
)
async def test_add_reminder_validates(env, body, code):
    client, _ = await env["make"]()
    await client.post("/api/timezone", json={"tz": "Europe/Moscow"}, headers=auth())
    response = await client.post("/api/reminders/add", json=body, headers=auth())
    assert response.status == 400 and (await response.json())["error"]["code"] == code
    assert await env["reminders"].list_pending(OWNER_ID) == []


async def test_add_reminder_needs_a_timezone(env):
    client, _ = await env["make"]()
    response = await client.post("/api/reminders/add", json={"text": "x", "when": "2026-11-14T09:00"}, headers=auth())
    assert response.status == 400 and (await response.json())["error"]["code"] == "no_timezone"


async def test_reminders_are_private_and_protected(env):
    client, _ = await env["make"](allowed_user_ids=frozenset({OWNER_ID, 222}))
    await client.post("/api/timezone", json={"tz": "UTC+3"}, headers=auth())
    await client.post("/api/reminders/add", json={"text": "моё", "when": "2026-11-14T09:00"}, headers=auth())
    mine = (await env["reminders"].list_pending(OWNER_ID))[0]

    other = auth(sign_init_data(222))
    assert (await (await client.get("/api/state", headers=other)).json())["reminders"]["items"] == []
    await client.post("/api/reminders/delete", json={"id": mine.id}, headers=other)
    assert len(await env["reminders"].list_pending(OWNER_ID)) == 1

    for path in ("/api/timezone", "/api/reminders/add", "/api/reminders/delete"):
        assert (await client.post(path, json={}, headers={})).status == 401
        assert (await client.post(path, json={}, headers=auth(sign_init_data(STRANGER_ID)))).status == 403


async def test_ordinary_chat_messages_do_not_touch_reminders(env):
    client, _ = await env["make"]()
    await client.post("/api/timezone", json={"tz": "Europe/Moscow"}, headers=auth())
    response = await client.post("/api/chat", json={"text": "расскажи про Рим"}, headers=auth())
    assert (await response.json())["reply"]["provider"] == "alpha"
    assert await env["reminders"].list_pending(OWNER_ID) == []


# ---------- голосовой ввод ----------


class FakeSpeech:
    def __init__(self, text="привет из голосового", error=None):
        self.text, self.error, self.calls = text, error, []

    async def transcribe(self, audio, filename="voice.ogg", content_type="audio/ogg"):
        from services.speech import SpeechError

        self.calls.append((len(audio), filename, content_type))
        if self.error:
            raise SpeechError(self.error)
        return self.text


async def test_voice_is_transcribed(env):
    speech = FakeSpeech()
    client, providers = await env["make"](speech=speech)
    assert (await (await client.get("/api/state", headers=auth())).json())["voice"] is True

    audio = b"\x1aE\xdf\xa3" + b"a" * 300_000  # больше общего лимита 64 КБ
    response = await client.post(
        "/api/voice", data=audio, headers={**auth(), "Content-Type": "audio/webm;codecs=opus"}
    )
    assert response.status == 200 and await response.json() == {"text": "привет из голосового"}
    assert speech.calls == [(len(audio), "voice.webm", "audio/webm")]
    assert providers[0].calls == []  # ответ Эли — отдельным запросом /api/chat
    assert await env["memory"].get_history(OWNER_ID) == []


@pytest.mark.parametrize(
    ("content_type", "body", "status", "code"),
    [
        ("text/plain", b"abc", 415, "bad_audio"),
        ("audio/webm", b"", 400, "bad_request"),
        ("audio/webm", b"a" * (5 * 1024 * 1024 + 10), 413, "too_large"),
    ],
)
async def test_voice_validates(env, content_type, body, status, code):
    speech = FakeSpeech()
    client, _ = await env["make"](speech=speech)
    response = await client.post("/api/voice", data=body, headers={**auth(), "Content-Type": content_type})
    assert response.status == status and (await response.json())["error"]["code"] == code
    assert speech.calls == []


async def test_voice_not_configured_and_failures(env):
    client, _ = await env["make"]()
    response = await client.post("/api/voice", data=b"abc", headers={**auth(), "Content-Type": "audio/ogg"})
    assert response.status == 503 and (await response.json())["error"]["code"] == "speech_off"

    client, _ = await env["make"](speech=FakeSpeech(error="down"))
    response = await client.post("/api/voice", data=b"abc", headers={**auth(), "Content-Type": "audio/mp4"})
    assert response.status == 502 and (await response.json())["error"]["code"] == "speech_failed"


async def test_voice_requires_access(env):
    speech = FakeSpeech()
    client, _ = await env["make"](speech=speech)
    headers = {"Content-Type": "audio/ogg"}
    assert (await client.post("/api/voice", data=b"abc", headers=headers)).status == 401
    stranger = {**auth(sign_init_data(STRANGER_ID)), **headers}
    assert (await client.post("/api/voice", data=b"abc", headers=stranger)).status == 403
    assert speech.calls == []


# ---------- фото ----------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class SeeingProvider(ScriptedProvider):
    def __init__(self, name, models):
        super().__init__(name, models)
        self.vision_models = models


def _photo_form(image=PNG, text=None, model=None, filename="photo.jpg"):
    from aiohttp import FormData

    form = FormData()
    if text is not None:
        form.add_field("text", text)
    if model is not None:
        form.add_field("model", model)
    if image is not None:
        form.add_field("image", image, filename=filename, content_type="image/jpeg")
    return form


async def test_photo_is_answered_by_a_vision_model(env):
    text_only = ScriptedProvider("alpha", ("a1",))
    seeing = SeeingProvider("beta", ("b1",))
    client, _ = await env["make"](providers=[text_only, seeing])
    assert (await (await client.get("/api/state", headers=auth())).json())["vision"] is True

    big = PNG + b"\x00" * 200_000  # больше общего лимита 64 КБ
    response = await client.post("/api/photo", data=_photo_form(big, "Что это за цветок?", "alpha:a1"), headers=auth())
    data = await response.json()
    assert response.status == 200 and data["reply"]["model"] == "b1"
    assert text_only.calls == []
    model, context = seeing.calls[0]
    assert context[-1].content == "Что это за цветок?" and context[-1].images[0].startswith("data:image/png;base64,")
    history = await env["memory"].get_history(OWNER_ID)
    assert history[0].content == "📷 Фото: Что это за цветок?"

    response = await client.post("/api/photo", data=_photo_form(text=None), headers=auth())
    assert response.status == 200
    assert seeing.calls[-1][1][-1].content == "Что на этом фото? Опиши коротко и по делу."


@pytest.mark.parametrize(
    ("form_kwargs", "status", "code"),
    [
        ({"image": b"not an image"}, 415, "bad_image"),
        ({"image": PNG + b"\x00" * (5 * 1024 * 1024)}, 413, "too_large"),
        ({"image": None, "text": "без картинки"}, 400, "bad_request"),
        ({"text": "я" * 4001}, 400, "too_long"),
    ],
)
async def test_photo_validates(env, form_kwargs, status, code):
    seeing = SeeingProvider("beta", ("b1",))
    client, _ = await env["make"](providers=[seeing])
    response = await client.post("/api/photo", data=_photo_form(**form_kwargs), headers=auth())
    assert response.status == status and (await response.json())["error"]["code"] == code
    assert seeing.calls == []


async def test_photo_rejects_json_and_needs_a_vision_model(env):
    client, providers = await env["make"]()
    response = await client.post("/api/photo", json={"text": "x"}, headers=auth())
    assert response.status == 400
    response = await client.post("/api/photo", data=_photo_form(), headers=auth())
    assert response.status == 503 and (await response.json())["error"]["code"] == "no_vision"
    assert providers[0].calls == []
    assert await env["memory"].get_history(OWNER_ID) == []


async def test_photo_requires_access_and_respects_rate_limit(env):
    seeing = SeeingProvider("beta", ("b1",))
    client, _ = await env["make"](providers=[seeing], rate_limit=1)
    assert (await client.post("/api/photo", data=_photo_form())).status == 401
    assert (await client.post("/api/photo", data=_photo_form(), headers=auth(sign_init_data(STRANGER_ID)))).status == 403
    assert (await client.post("/api/photo", data=_photo_form(), headers=auth())).status == 200
    response = await client.post("/api/photo", data=_photo_form(), headers=auth())
    assert response.status == 429 and (await response.json())["error"]["code"] == "rate_limited"
    assert len(seeing.calls) == 1


# ---------- потоковый ответ ----------


class StreamingProvider(BaseProvider):
    def __init__(self, name, pieces, fail_after=None, fail_first=False):
        super().__init__(name, f"{name.title()} AI", (f"{name}1",))
        self.pieces, self.fail_after, self.fail_first, self.calls = pieces, fail_after, fail_first, 0

    async def complete(self, messages, *, model, max_tokens, temperature):
        raise AssertionError("stream expected")

    async def stream(self, messages, *, model, max_tokens, temperature):
        self.calls += 1
        if self.fail_first:
            raise ProviderUnavailableError("down", provider=self.name, model=model)
        for index, piece in enumerate(self.pieces):
            if index == self.fail_after:
                raise ProviderUnavailableError("down", provider=self.name, model=model)
            yield piece
        yield CompletionResult("".join(self.pieces), self.name, model)


async def _ndjson(response):
    import json as _json

    return [_json.loads(line) for line in (await response.text()).splitlines() if line.strip()]


async def test_stream_endpoint_sends_pieces_then_done(env):
    client, _ = await env["make"](providers=[StreamingProvider("alpha", ["При", "вет, ", "Аня!"])])
    response = await client.post("/api/chat/stream", json={"text": "привет"}, headers=auth())
    assert response.status == 200
    assert response.headers["Content-Type"].startswith("application/x-ndjson")
    assert response.headers["Cache-Control"] == "no-store"
    events = await _ndjson(response)
    assert [e["text"] for e in events[:-1]] == ["При", "вет, ", "Аня!"]
    done = events[-1]
    assert done["type"] == "done" and done["reply"]["content"] == "Привет, Аня!"
    assert done["reply"]["model"] == "alpha1" and done["usage"]["used"] == 1
    assert [m.content for m in await env["memory"].get_history(OWNER_ID)] == ["привет", "Привет, Аня!"]


async def test_stream_endpoint_errors_before_the_answer_are_plain_json(env):
    client, _ = await env["make"](providers=[StreamingProvider("alpha", ["x"], fail_first=True)])
    response = await client.post("/api/chat/stream", json={"text": "привет"}, headers=auth())
    assert response.status == 503 and (await response.json())["error"]["code"] == "ai_unavailable"
    for body, code in (({"text": " "}, "bad_request"), ({"text": "я" * 4001}, "too_long")):
        response = await client.post("/api/chat/stream", json=body, headers=auth())
        assert response.status == 400 and (await response.json())["error"]["code"] == code
    assert (await client.post("/api/chat/stream", json={"text": "x"})).status == 401


async def test_stream_endpoint_reports_a_broken_answer_and_keeps_history_clean(env):
    client, _ = await env["make"](providers=[StreamingProvider("alpha", ["Нач", "ало"], fail_after=1)])
    response = await client.post("/api/chat/stream", json={"text": "привет"}, headers=auth())
    events = await _ndjson(response)
    assert events[0] == {"type": "delta", "text": "Нач"}
    assert events[-1]["type"] == "error" and events[-1]["error"]["code"] == "ai_unavailable"
    assert await env["memory"].get_history(OWNER_ID) == []


async def test_stream_endpoint_respects_rate_limit(env):
    client, _ = await env["make"](providers=[StreamingProvider("alpha", ["ok"])], rate_limit=1)
    assert (await client.post("/api/chat/stream", json={"text": "a"}, headers=auth())).status == 200
    response = await client.post("/api/chat/stream", json={"text": "b"}, headers=auth())
    assert response.status == 429 and (await response.json())["error"]["code"] == "rate_limited"

"""GigaChat (Сбер): бесплатный тариф Freemium для физлиц, работает с серверов в России.

Чат-эндпоинт почти совпадает с OpenAI, отличается авторизация: ключ авторизации
(GIGACHAT_API_KEY) обменивается на токен доступа, который живёт 30 минут.
Сертификаты серверов выданы НУЦ Минцифры: если система им не доверяет,
укажите корневой сертификат в GIGACHAT_CA_BUNDLE.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable

import httpx

from ai.providers.base import (
    AuthError,
    BadResponseError,
    ChatMessage,
    CompletionResult,
    RateLimitError,
)
from ai.providers.openai_compatible import OpenAICompatibleProvider, _error_detail

# Токен обновляется заранее, чтобы не истёк посреди запроса.
_REFRESH_MARGIN = 120.0
# Отказ с токеном моложе этого — проблема в ключе, а не в истёкшем токене.
_FRESH_TOKEN_AGE = 5.0


class GigaChatProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        name: str,
        title: str,
        base_url: str,
        auth_url: str,
        auth_key: str,
        scope: str,
        models: tuple[str, ...],
        http_client: httpx.AsyncClient,
        vision_models: tuple[str, ...] = (),
        max_output_tokens: int = 0,
        owns_client: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(
            name=name,
            title=title,
            base_url=base_url,
            api_key="",
            models=models,
            http_client=http_client,
            vision_models=vision_models,
            max_output_tokens=max_output_tokens,
            owns_client=owns_client,
        )
        self._auth_url = auth_url
        # В кабинете ключ показывают и как «Basic …», и без префикса — принимаем оба.
        self._auth_key = auth_key.removeprefix("Basic ").strip()
        self._scope = scope
        self._clock = clock
        self._token = ""
        self._expires_at = 0.0
        self._obtained_at = 0.0
        self._lock = asyncio.Lock()

    async def _headers(self, model: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._access_token(model)}"}

    async def _access_token(self, model: str) -> str:
        async with self._lock:
            if self._token and self._expires_at - self._clock() > _REFRESH_MARGIN:
                return self._token
            headers = {
                "Authorization": f"Basic {self._auth_key}",
                "RqUID": str(uuid.uuid4()),
                "Accept": "application/json",
            }
            try:
                response = await self._http.post(
                    self._auth_url, data={"scope": self._scope}, headers=headers
                )
            except httpx.HTTPError as exc:
                raise self._network_error(exc, model) from exc
            try:
                data = response.json()
            except ValueError:
                data = None
            if response.status_code != 200:
                detail = _error_detail(data)[0] if data is not None else response.text[:300]
                error = self._classify(response.status_code, detail, model)
                if isinstance(error, RateLimitError) or response.status_code >= 500:
                    raise error
                # Неверный ключ или область доступа (scope): дело в настройках, а не в модели.
                raise AuthError(
                    f"token request failed: {detail or response.status_code}",
                    provider=self.name,
                    model=model,
                    status_code=response.status_code,
                )
            token = data.get("access_token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise BadResponseError("no access_token in response", provider=self.name, model=model)
            expires_at = data.get("expires_at")
            now = self._clock()
            if isinstance(expires_at, (int, float)) and expires_at > 0:
                # GigaChat отдаёт время истечения в миллисекундах.
                self._expires_at = expires_at / 1000 if expires_at > 1e11 else float(expires_at)
            else:
                self._expires_at = now + 30 * 60
            self._token = token
            self._obtained_at = now
            return token

    def _forget_stale_token(self) -> bool:
        """Сбрасывает токен после 401. True — токен был не новым, и стоит повторить запрос."""
        stale = bool(self._token) and self._clock() - self._obtained_at > _FRESH_TOKEN_AGE
        self._token = ""
        self._expires_at = 0.0
        return stale

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        options = {"model": model, "max_tokens": max_tokens, "temperature": temperature}
        try:
            return await super().complete(messages, **options)
        except AuthError as exc:
            if exc.status_code != 401 or not self._forget_stale_token():
                raise
            return await super().complete(messages, **options)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str | CompletionResult]:
        options = {"model": model, "max_tokens": max_tokens, "temperature": temperature}
        emitted = False
        try:
            async for event in super().stream(messages, **options):
                emitted = True
                yield event
            return
        except AuthError as exc:
            # Токен отозван или истёк раньше срока: до первого куска ответа можно повторить.
            if emitted or exc.status_code != 401 or not self._forget_stale_token():
                raise
        async for event in super().stream(messages, **options):
            yield event

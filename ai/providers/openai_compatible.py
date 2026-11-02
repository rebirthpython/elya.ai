"""Провайдер для любых OpenAI-совместимых API (Gemini, OpenRouter, Groq, Ollama и др.).

Использует только стандартный эндпоинт POST {base_url}/chat/completions через HTTPX,
поэтому не зависит от SDK конкретных компаний.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ai.providers.base import (
    AuthError,
    BadResponseError,
    BaseProvider,
    ChatMessage,
    CompletionResult,
    InsufficientCreditsError,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    RegionNotSupportedError,
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_REGION_MARKERS = (
    "location is not supported",
    "not available in your country",
    "not available in your region",
    "unsupported_country",
)
_AUTH_MARKERS = ("api key", "api_key", "unauthorized", "invalid authentication")


def _error_detail(payload: Any) -> tuple[str, int | None]:
    """Достаёт текст и код ошибки из тела ответа (форматы OpenAI/OpenRouter/Gemini)."""
    if isinstance(payload, list) and payload:
        payload = payload[0]  # Gemini OpenAI-compat иногда возвращает список ошибок
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, dict):
            message = err.get("message") or err.get("status") or ""
            code = err.get("code")
            return str(message)[:300], code if isinstance(code, int) else None
        if isinstance(err, str):
            return err[:300], None
    return str(payload)[:300], None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # контент в виде списка частей
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


class OpenAICompatibleProvider(BaseProvider):
    def __init__(
        self,
        *,
        name: str,
        title: str,
        base_url: str,
        api_key: str,
        models: tuple[str, ...],
        http_client: httpx.AsyncClient,
    ) -> None:
        super().__init__(name, title, models)
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._http = http_client

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        try:
            response = await self._http.post(self._url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError("timeout", provider=self.name, model=model) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"network error: {type(exc).__name__}", provider=self.name, model=model
            ) from exc

        try:
            data = response.json()
        except ValueError:
            data = None

        if response.status_code != 200:
            detail, _ = _error_detail(data) if data is not None else (response.text[:300], None)
            raise self._classify(
                response.status_code,
                detail,
                model,
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
            )

        if not isinstance(data, dict):
            raise BadResponseError("response is not a JSON object", provider=self.name, model=model)
        if data.get("error"):
            # OpenRouter может вернуть ошибку апстрима с HTTP 200.
            detail, code = _error_detail(data)
            raise self._classify(code or 502, detail, model)

        return self._parse_completion(data, model)

    def _parse_completion(self, data: dict[str, Any], model: str) -> CompletionResult:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise BadResponseError("no choices in response", provider=self.name, model=model)

        choice = choices[0]
        message = choice.get("message") or {}
        text = _THINK_RE.sub("", _extract_text(message.get("content"))).strip()
        finish_reason = choice.get("finish_reason")

        if not text:
            reason = "blocked by safety filter" if finish_reason in ("content_filter", "safety") \
                else f"empty response (finish_reason={finish_reason})"
            raise BadResponseError(reason, provider=self.name, model=model)

        return CompletionResult(
            text=text,
            provider=self.name,
            model=str(data.get("model") or model),
            truncated=finish_reason == "length",
        )

    def _classify(
        self, status: int, detail: str, model: str, *, retry_after: float | None = None
    ) -> ProviderError:
        lower = detail.lower()
        kwargs = {"provider": self.name, "model": model, "status_code": status}
        message = detail or f"HTTP {status}"

        if any(marker in lower for marker in _REGION_MARKERS):
            return RegionNotSupportedError(message, **kwargs)
        if status == 429:
            return RateLimitError(message, retry_after=retry_after, **kwargs)
        if status in (401, 403) or any(marker in lower for marker in _AUTH_MARKERS):
            return AuthError(message, **kwargs)
        if status == 402:
            return InsufficientCreditsError(message, **kwargs)
        if status == 404:
            return ModelNotFoundError(message, **kwargs)
        if status == 408 or status >= 500:
            return ProviderUnavailableError(message, **kwargs)
        return BadResponseError(message, **kwargs)

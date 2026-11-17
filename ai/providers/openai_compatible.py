"""Провайдер для любых OpenAI-совместимых API (Gemini, GitHub Models, Groq, Mistral,
OpenRouter, NVIDIA, Cloudflare, Ollama и др.).

Использует только стандартный эндпоинт POST {base_url}/chat/completions через HTTPX,
поэтому не зависит от SDK конкретных компаний.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ai.providers.base import (
    AuthError,
    BadResponseError,
    BaseProvider,
    CertificateError,
    ChatMessage,
    CompletionResult,
    InsufficientCreditsError,
    ModelNotFoundError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderUnreachableError,
    RateLimitError,
    RegionNotSupportedError,
)

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_REGION_MARKERS = (
    "location is not supported",
    "not available in your country",
    "not available in your region",
    "unsupported_country",
    "unsupported_region",
    "region is not supported",
    "country, region, or territory not supported",
)
_AUTH_MARKERS = ("api key", "api_key", "unauthorized", "invalid authentication")
# Модель убрали из каталога или переименовали: многие провайдеры отвечают на это 400, а не 404.
_MODEL_MISSING_RE = re.compile(
    r"model_not_found|unknown[ _]model|invalid model|no such model|not a valid model"
    r"|no endpoints found|not supported by any provider"
    r"|\bmodel\b[^.]{0,120}?\b(?:does not exist|not found|decommissioned"
    r"|no longer (?:supported|available)|has been deprecated)",
    re.IGNORECASE,
)


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


def _wire_message(message: ChatMessage) -> dict[str, Any]:
    """Сообщение в формате OpenAI; с картинками — массив частей (текст + image_url)."""
    if not message.images:
        return {"role": message.role, "content": message.content}
    parts: list[dict[str, Any]] = [{"type": "text", "text": message.content}]
    parts.extend({"type": "image_url", "image_url": {"url": url}} for url in message.images)
    return {"role": message.role, "content": parts}


_THINK_OPEN = "<think>"


def _visible_text(raw: str) -> str:
    """Текст, который можно показать, пока ответ ещё идёт: без блоков <think>…</think>,
    без незакрытого <think> и без недописанного начала тега в самом конце."""
    text = _THINK_RE.sub("", raw)
    lower = text.lower()
    start = lower.find(_THINK_OPEN)
    if start != -1:
        text = text[:start]
    for size in range(len(_THINK_OPEN) - 1, 0, -1):
        if text.lower().endswith(_THINK_OPEN[:size]):
            text = text[:-size]
            break
    return text.lstrip()


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
        vision_models: tuple[str, ...] = (),
        max_output_tokens: int = 0,
        owns_client: bool = False,
        reasoning_effort: str = "",
    ) -> None:
        super().__init__(name, title, models, vision_models, max_output_tokens)
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._http = http_client
        # Меньше скрытых размышлений — раньше первое слово ответа.
        self._reasoning_effort = reasoning_effort
        # Модели, которые отклонили reasoning_effort: им он больше не отправляется.
        self._no_reasoning: set[str] = set()
        # Свой клиент (например, со своим корневым сертификатом) закрывается вместе с провайдером.
        self._owns_client = owns_client

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    def _payload(
        self, messages: list[ChatMessage], model: str, max_tokens: int, temperature: float, *, stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_wire_message(m) for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stream:
            payload["stream"] = True
        if self._reasoning_effort and model not in self._no_reasoning:
            payload["reasoning_effort"] = self._reasoning_effort
        return payload

    def _drop_reasoning(self, payload: dict[str, Any], status: int, detail: str, model: str) -> bool:
        """Модель не принимает reasoning_effort: запомнить и повторить запрос без него."""
        if "reasoning_effort" not in payload or status not in (400, 422) or "reasoning" not in detail.lower():
            return False
        logger.info("%s/%s does not accept reasoning_effort, retrying without it", self.name, model)
        self._no_reasoning.add(model)
        return True

    async def _headers(self, model: str) -> dict[str, str]:
        """Заголовки авторизации запроса; провайдеры с токенами переопределяют."""
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _network_error(self, exc: httpx.HTTPError, model: str) -> ProviderError:
        kwargs = {"provider": self.name, "model": model}
        if isinstance(exc, httpx.ConnectError) and "CERTIFICATE_VERIFY_FAILED" in str(exc):
            return CertificateError(
                f"TLS certificate is not trusted; set {self.name.upper()}_CA_BUNDLE "
                "to the root certificate (PEM)",
                **kwargs,
            )
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return ProviderUnreachableError(f"cannot connect: {type(exc).__name__}", **kwargs)
        if isinstance(exc, httpx.TimeoutException):
            return ProviderTimeoutError("timeout", **kwargs)
        return ProviderUnavailableError(f"network error: {type(exc).__name__}", **kwargs)

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        headers = await self._headers(model)
        while True:
            payload = self._payload(messages, model, max_tokens, temperature, stream=False)
            try:
                response = await self._http.post(self._url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                raise self._network_error(exc, model) from exc

            try:
                data = response.json()
            except ValueError:
                data = None
            if response.status_code == 200:
                break
            detail, _ = _error_detail(data) if data is not None else (response.text[:300], None)
            if self._drop_reasoning(payload, response.status_code, detail, model):
                continue
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

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str | CompletionResult]:
        """Потоковый ответ (stream: true, Server-Sent Events): куски текста, затем итог."""
        headers = await self._headers(model)
        raw_parts: list[str] = []
        shown = ""
        finish_reason = None
        served_model = model
        try:
            while True:
                payload = self._payload(messages, model, max_tokens, temperature, stream=True)
                async with self._http.stream("POST", self._url, json=payload, headers=headers) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        try:
                            data = json.loads(body)
                        except ValueError:
                            data = None
                        detail = _error_detail(data)[0] if data is not None else body[:300].decode("utf-8", "replace")
                        if self._drop_reasoning(payload, response.status_code, detail, model):
                            continue
                        raise self._classify(
                            response.status_code,
                            detail,
                            model,
                            retry_after=_parse_retry_after(response.headers.get("retry-after")),
                        )
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        # Сервер проигнорировал stream — пришёл обычный JSON-ответ.
                        try:
                            data = json.loads(await response.aread())
                        except ValueError as exc:
                            raise BadResponseError("response is not JSON", provider=self.name, model=model) from exc
                        if not isinstance(data, dict):
                            raise BadResponseError("response is not a JSON object", provider=self.name, model=model)
                        if data.get("error"):
                            detail, code = _error_detail(data)
                            raise self._classify(code or 502, detail, model)
                        result = self._parse_completion(data, model)
                        yield result.text
                        yield result
                        return

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        chunk_raw = line[5:].strip()
                        if chunk_raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(chunk_raw)
                        except ValueError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if chunk.get("error"):
                            detail, code = _error_detail(chunk)
                            raise self._classify(code or 502, detail, model)
                        served_model = str(chunk.get("model") or served_model)
                        choices = chunk.get("choices")
                        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        piece = _extract_text(delta.get("content")) if isinstance(delta, dict) else ""
                        if not piece:
                            continue
                        raw_parts.append(piece)
                        visible = _visible_text("".join(raw_parts))
                        if visible.startswith(shown) and len(visible) > len(shown):
                            yield visible[len(shown):]
                            shown = visible
                break  # ответ прочитан целиком
        except httpx.HTTPError as exc:
            raise self._network_error(exc, model) from exc

        text = _THINK_RE.sub("", "".join(raw_parts)).strip()
        if not text:
            reason = "blocked by safety filter" if finish_reason in ("content_filter", "safety") \
                else f"empty response (finish_reason={finish_reason})"
            raise BadResponseError(reason, provider=self.name, model=model)
        yield CompletionResult(
            text=text, provider=self.name, model=served_model, truncated=finish_reason == "length"
        )

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
        if status == 404 or (
            status in (400, 410, 422) and _MODEL_MISSING_RE.search(detail)
        ):
            return ModelNotFoundError(message, **kwargs)
        if status == 408 or status >= 500:
            return ProviderUnavailableError(message, **kwargs)
        return BadResponseError(message, **kwargs)
